import copy
import logging
import math
import threading
import time
import traceback
from collections.abc import Iterable
from dataclasses import dataclass
from functools import partial
from typing import Callable, Any, Optional, Union

from .clock import Clock
from .event import EventDefaults
from .track import Track
from ..constants import (DEFAULT_TICKS_PER_BEAT, DEFAULT_TEMPO, EVENT_ACTION, EVENT_ACTION_ARGS, EVENT_DURATION)
from ..constants import INTERPOLATION_NONE
from ..exceptions import (
    TrackLimitReachedException,
    TrackNotFoundException,
    MultipleOutputDevicesException,
)
from ..io import MidiOutputDevice, OutputDevice
from ..pattern import PSequence
from ..util import make_clock_multiplier

log = logging.getLogger(__name__)


@dataclass
class Action:
    time: float
    function: Callable


class Timeline:
    def __init__(self,
                 tempo: float = DEFAULT_TEMPO,
                 output_device: Any = None,
                 clock_source: Any = None,
                 ticks_per_beat: int = DEFAULT_TICKS_PER_BEAT,
                 ):
        """
        A Timeline object encapsulates a number of Tracks, each of which
        represents a sequence of note or control events.

        Timing is driven by a `clock_source`, which can be a real-time Clock object, or an
        external source such as a `MidiInputDevice` clock.

        A Timeline typically runs until it is terminated by calling `stop()`.
        If you want the Timeline to terminate as soon as no more events are available,
        set `stop_when_done = True`.

        Args:
            tempo: The initial tempo, in bpm
            output_device: The default output device to send events to
            clock_source: The source of clocking events. If not specified, creates an internal Clock.
            ticks_per_beat: The timing resolution, in PPQN. Default is 480PPQN, which equates to approximately \
                            1ms resolution at 120bpm.
        """
        self._clock_source: Optional[Clock] = None
        if clock_source is None:
            clock_source = Clock(self, tempo, ticks_per_beat)
        self.set_clock_source(clock_source)

        self.clock_multipliers: dict[OutputDevice, Callable] = {}
        """
        Clock multipliers are helper generators that perform clock division/
        multiplication so that devices with different PPQN can work together. For
        example, isobar_ext's default internal clock runs at 480PPQN, but a MIDI device
        expects a 24PPQN tick, so a 20x clock divider is needed.
        """

        self.output_devices: list[OutputDevice] = []
        if output_device:
            self.add_output_device(output_device)
        """ The output devices that the timeline is able to schedule events on. """

        self.current_time: float = 0
        """ The current time, in beats. """

        self.max_tracks: int = 0
        """ If set, limits the number of tracks that can be created.
        Scheduling a track beyond this limit will generate an exception. """

        self.tracks: list[Track] = []
        """ The list of Track objects that are currently scheduled. """

        self.stop_when_done = False
        """ If True, stops the timeline when the last track is finished. """

        self.actions = []

        self.running: bool = False
        """ Indicates whether the timeline is currently running. """

        self.ignore_exceptions = False
        """
        If ignore_exceptions is True, exceptions do not halt the timeline,
        and instead simply generate a warning. This can be useful for contexts
        such as live performance that require a robust playback environment.
        """

        self.defaults = EventDefaults()
        """
        Defaults can be used to automatically set the parameters of future events.
        For example, setting defaults.quantize can be used to automatically quantize
        all future scheduling.
        """

        self.on_event_callback: Optional[Callable] = None
        """
        Optional callback to trigger each time an event is performed.
        Receives two parameters:
         - the Track that the event occurred on
         - the Event object
        """

    def get_clock_source(self) -> Clock:
        """
        The originating Clock object that sends timing ticks to this timeline.

        Returns:
            Clock: The clock source.
        """
        return self._clock_source

    def set_clock_source(self, clock_source: Clock) -> None:
        """
        Set the Clock object that will send timing ticks to this timeline.

        Args:
            clock_source: The clock source.
        """
        clock_source.clock_target = self
        self._clock_source = clock_source

    clock_source = property(get_clock_source, set_clock_source)
    """ The Clock object that sends timing ticks to this timeline. """

    def get_ticks_per_beat(self) -> int:
        """
        Query how many ticks are expected per beat.
        This varies based on the resolution of the clock source.

        Returns:
            The number of ticks per beat.
        """
        return self.clock_source.ticks_per_beat if self.clock_source else None

    def set_ticks_per_beat(self, ticks_per_beat: int):
        """
        Set the number of ticks per beat.

        Args:
            ticks_per_beat: The new number of ticks per beat. This can be set for internal clocks, but \
                            not for other clock sources (e.g. MIDI clocks).

        Raises:
            AttributeError: If the number of ticks per beat cannot be set (for example, for a MIDI clock source).
        """
        self.clock_source.ticks_per_beat = ticks_per_beat

    ticks_per_beat = property(get_ticks_per_beat, set_ticks_per_beat)
    """ The number of ticks that the clock source provides per beat. """

    @property
    def tick_duration(self):
        """
        Tick duration, in beats.
        """
        return 1.0 / self.ticks_per_beat

    def get_tempo(self) -> float:
        """
        Returns the tempo of this timeline's clock, or None if an external
        clock source is used (in which case the tempo is unknown).

        Returns:
            The tempo, in BPM.
        """
        return self.clock_source.tempo

    def set_tempo(self, tempo: float) -> None:
        """
        Set the tempo of this timeline's clock.
        If the timeline uses an external clock, this operation is invalid, and a
        RuntimeError is raised.

        Args:
            tempo: Tempo, in bpm
        """
        self.clock_source.tempo = tempo

    tempo = property(get_tempo, set_tempo)
    """ The tempo of the timeline, in beats per minute. """

    def seconds_to_beats(self, seconds: float) -> float:
        """
        Translates a duration in seconds to a duration in beats.

        Args:
            seconds: The duration to convert, in seconds.

        Returns:
            The equivalent duration, in beats.
        """
        return seconds * self.tempo / 60.0

    def beats_to_seconds(self, beats: float) -> float:
        """
        Translates a duration in beats to a duration in seconds.


        Args:
            beats: The number of beats to convert.

        Returns:
            The equivalent duration, in seconds.
        """
        return beats * 60.0 / self.tempo

    def tick(self):
        """
        Must be triggered once every tick to trigger new events. This is the core
        quantum of Timeline events, and is typically triggered automatically by the
        timeline's `clock_source`

        Raises:
            StopIteration: If `stop_when_done` is true and no more events are scheduled.
        """
        # --------------------------------------------------------------------------------
        # Each time we arrive at precisely a new beat, generate a debug msg.
        # Round to several decimal places to avoid 7.999999999 syndrome.
        # http://docs.python.org/tutorial/floatingpoint.html
        # --------------------------------------------------------------------------------
        if round(self.current_time, 8) % 1 == 0:
            log.debug(
                "--------------------------------------------------------------------------------"
            )
            log.debug(
                "Tick (%d active tracks, %d pending actions)"
                % (len(self.tracks), len(self.actions))
            )

        # --------------------------------------------------------------------------------
        # Process note-offs before scheduled actions, which may reset the timestamp
        # of the track.
        # --------------------------------------------------------------------------------
        for track in self.tracks[:]:
            track.process_note_offs()

        # --------------------------------------------------------------------------------
        # Copy self.actions because removing from it whilst using it = bad idea.
        # Perform actions before tracks are executed because an event might
        # include scheduling a quantized track, which should then be
        # immediately evaluated.
        # --------------------------------------------------------------------------------
        aligned_actions = []
        for idx, action in enumerate(self.actions[:]):
            # --------------------------------------------------------------------------------
            # The only event we currently get in a Timeline are add_track events
            #  -- which have a function object associated with them.
            #
            # Round to work around rounding errors.
            # http://docs.python.org/tutorial/floatingpoint.html
            # --------------------------------------------------------------------------------
            if isinstance(action, dict):
                action = Action(*action.values())
                # self.actions[idx] = action
            if round(action.time, 8) <= round(self.current_time, 8):
                action.function()
                self.actions.remove(action)
            else:
                aligned_actions.append(action)
                # self.actions.remove(action)
        self.actions = aligned_actions
        # --------------------------------------------------------------------------------
        # Copy self.tracks because removing from it whilst using it = bad idea
        # --------------------------------------------------------------------------------
        for track in self.tracks[:]:
            try:
                track.tick()
            except Exception as e:  # noqa: F841 (but we don't care if it's not used)
                if not self.ignore_exceptions:
                    raise
                tb = traceback.format_exc()
                log.warning(f"*** Exception in track: {tb}")
                self.tracks.remove(track)
            if track.is_finished and track.remove_when_done:
                self.tracks.remove(track)
                log.info(
                    "Timeline: Track finished, removing from scheduler (total tracks: %d)"
                    % len(self.tracks)
                )

        # --------------------------------------------------------------------------------
        # If we've run out of notes, raise a StopIteration.
        # --------------------------------------------------------------------------------
        if len(self.tracks) == 0 and not self.actions and self.stop_when_done:
            # TODO: Don't do this if we've never played any events, e.g.
            #       right after calling timeline.start(). Should at least
            #       wait for some events to happen first.
            raise StopIteration

        # --------------------------------------------------------------------------------
        # Tell our output devices to move forward a step.
        # --------------------------------------------------------------------------------
        for device in self.output_devices:
            clock_multiplier = self.clock_multipliers[device]
            ticks = next(clock_multiplier)

            for _ in range(ticks):
                device.tick()

        # --------------------------------------------------------------------------------
        # Increment beat count according to our current tick_length.
        # --------------------------------------------------------------------------------
        self.current_time += self.tick_duration

    def dump(self):
        """
        Output a summary of this Timeline object to stdout.
        """
        print(
            f'Timeline (clock: {self.clock_source}, tempo {self.clock_source.tempo or "unknown"})'
        )

        print((" - %d devices" % len(self.output_devices)))
        for device in self.output_devices:
            print(f"   - {device}")

        print((" - %d tracks" % len(self.tracks)))
        for tracks in self.tracks:
            print(f"   - {tracks}")

    def reset_to_beat(self):
        """
        Reset the timer to the last beat.
        Useful when a MIDI Stop/Reset message is received, or otherwise to re-establish beat sync.
        """

        self.current_time = round(self.current_time)
        for tracks in self.tracks:
            tracks.reset_to_beat()

    def reset(self):
        """
        Rewind the timeline and all tracks to t = 0.
        NOTE: This is not the same as re-initialising the timeline to its initial state
        as it erases any differences in scheduling times between tracks. More thought may be
        needed for different types of reset/rewind operation.
        """
        self.current_time = 0.0
        for track in self.tracks:
            track.reset()

    def background(self):
        """
        Run this Timeline in a background thread.
        """
        thread = threading.Thread(target=self.run)
        thread.daemon = True
        thread.start()

    def run(self, stop_when_done: bool = None) -> None:
        """
        Run this Timeline in the foreground.

        Args:
            stop_when_done: If set, returns when no tracks are currently \
                            scheduled; otherwise, keeps running indefinitely.
        """

        if stop_when_done is not None:
            self.stop_when_done = stop_when_done

        try:
            # --------------------------------------------------------------------------------
            # Start the clock. This might internal (eg a Clock object, running on
            # an independent thread), or external (eg a MIDI clock).
            # --------------------------------------------------------------------------------
            for device in self.output_devices:
                device.start()
            self.running = True
            self.clock_source.run()

        except StopIteration:
            # --------------------------------------------------------------------------------
            # This will be hit if every Pattern in a timeline is exhausted.
            # --------------------------------------------------------------------------------
            log.info("Timeline: Finished")
            self.running = False

        except Exception as e:
            print(f" *** Exception in Timeline thread: {e}")
            if not self.ignore_exceptions:
                raise e

    def start(self) -> None:
        """
        Starts the timeline running in the background.
        """
        self.background()

    def stop(self):
        """
        Stops the timeline running.
        """
        log.info("Timeline: Stopping")
        for device in self.output_devices:
            device.all_notes_off()
            device.stop()
        self.clock_source.stop()

    def warp(self, warper):
        """
        Apply a PWarp object to warp the clock's timing.
        """
        self.clock_source.warp(warper)

    def unwarp(self, warper):
        """
        Remove a PWarp object from our clock.
        """
        self.clock_source.warp(warper)

    def get_output_device(self) -> OutputDevice:
        """
        Query the timeline's current OutputDevice.
        If multiple output devices are currently set (e.g., for a timeline that generates
        both MIDI and OSC output), raises an exception.

        Returns:
            OutputDevice: The output device.

        Raises:
            MultipleOutputDevicesException: If multiple output devices exist.
        """
        if len(self.output_devices) != 1:
            raise MultipleOutputDevicesException(
                "output_device is ambiguous for Timelines with multiple outputs"
            )
        return self.output_devices[0]

    def set_output_device(self, output_device: OutputDevice) -> None:
        """
        Set a new device to send events to, removing any existing outputs.

        Args:
            output_device: The new output device.
        """
        self.output_devices = []
        self.add_output_device(output_device)

    output_device = property(get_output_device, set_output_device)
    """ The device that events are sent to. """

    def add_output_device(self, output_device: OutputDevice) -> None:
        """
        Append a new output device to the timeline's device list.
        """
        self.output_devices.append(output_device)
        self.clock_multipliers[output_device] = make_clock_multiplier(output_device.ticks_per_beat, self.ticks_per_beat)

    def schedule(self,
                 params: dict = None,
                 quantize: float = None,
                 delay: float = 0,
                 count: Optional[int] = None,
                 interpolate: str = INTERPOLATION_NONE,
                 output_device: Any = None,
                 remove_when_done: bool = True,
                 name: Optional[str] = None,
                 replace: bool = False,
                 track_index: Optional[int] = None,
                 sel_track_idx: Optional[int] = None
                 ) -> Track:
        """
        Schedule a new track within this Timeline.

        Args:
            params (dict):           Event dictionary. Keys are generally EVENT_* values, defined in constants.py. \
                                     If params is None, a new empty Track will be scheduled and returned. \
                                     This can be updated with Track.update(). \
                                     params can alternatively be a Pattern that generates a dict output.
            name (str):              Optional name for the track.
            quantize (float):        Quantize level, in beats. For example, 1.0 will begin executing the \
                                     events on the next whole beat.
            delay (float):           Delay time, in beats, before events should be executed. If `quantize` \
                                     and `delay` are both specified, quantization is applied, \
                                     and the event is scheduled `delay` beats after the quantization time.
            count (int):             Number of events to process, or unlimited if not specified.
            interpolate (int):       Interpolation mode for control segments.
            output_device:           Output device to send events to. Uses the Timeline default if not specified.
            remove_when_done (bool): If True, removes the Track from the Timeline when it is finished.
                                     Otherwise, retains the Track, so update() can later be called to schedule
                                     additional events on it.
            name (str):              Optional name to identify the Track. If given, can be used to update the track's
                                     parameters in future calls to schedule() by specifying replace=True.
            replace (bool):          Must be used in conjunction with the `name` parameter. Instead of scheduling a \
                                     new Track, this updates the parameters of an existing track with the same name.
            track_index (int):       When specified, inserts the Track at the given index.
                                     This can be used to set the priority of an event and ensure that it happens
                                     before another Track is evaluted, used in (e.g.) Track.update().
            sel_track_idx (int):     Track index to use for event arguments (default: None). This says about midinote
                                    track schedule us assigned to
        Returns:
            The new `Track` object.

        Raises:
            TrackLimitReachedException: If `max_tracks` has been reached.
        """
        if output_device is None:
            # --------------------------------------------------------------------------------
            # If no output device exists, send to the system default MIDI output.
            # --------------------------------------------------------------------------------
            if len(self.output_devices) == 0:
                self.add_output_device(MidiOutputDevice())
            output_device = self.output_devices[0]

        # --------------------------------------------------------------------------------
        # This is to ensure EVENT_ACTION split 1 element [1:]
        # --------------------------------------------------------------------------------
        if not params:
            params_list = [{}]
        elif isinstance(params, list):
            params_list = params
        else:
            params_list = [params]

        tracks_list = []
        params_list2 = []
        event_args = {}
        for param in params_list:
            if not isinstance(param, dict) and not isinstance(param, Iterable):
                param = dict(param)
            if isinstance(param, dict):
                action_fun = param.get(EVENT_ACTION, None)
                event_args = param.get(EVENT_ACTION_ARGS, {})
            else:
                action_fun, event_args = None, {}

            if action_fun and isinstance(action_fun, Iterable):
                attributes1 = vars(action_fun)
                # Get the attributes used by the class constructor
                constructor_attributes = list(PSequence.__init__.__code__.co_varnames[1:])

                # Filter the modified attributes to include only those used by the constructor
                attributes = {k: v for k, v in attributes1.copy().items() if
                              k in constructor_attributes}
                attributes2 = {k: v for k, v in attributes1.copy().items() if
                               k in constructor_attributes}
                action_fun2 = [
                                  partial(f, self) if isinstance(f, partial) else f
                                  for f in copy.copy(action_fun)
                              ][:1]
                attributes2['sequence'] = action_fun2
                action_fun2 = PSequence(**attributes2)
                params2 = copy.copy(param)
                params2[EVENT_ACTION] = action_fun2
                if bool(event_args):
                    params2[EVENT_ACTION_ARGS] = event_args
                dur2 = list(params2.pop(EVENT_DURATION, None))

                if dur2:
                    params2[EVENT_DURATION] = PSequence(dur2[:1], repeats=1)
                params_list2.append(params2)

                action_fun = [partial(f, self) if isinstance(f, partial) else f for f in copy.copy(action_fun)][1:]
                attributes['sequence'] = action_fun
                action_fun = PSequence(**attributes)
                param[EVENT_ACTION] = action_fun
                if event_args:
                    param[EVENT_ACTION_ARGS] = event_args

                dur = list(param.pop(EVENT_DURATION, None))

                if dur:
                    param["delay"] = dur[0]
                    param[EVENT_DURATION] = PSequence(dur[1:], repeats=1)

            elif action_fun:
                param[EVENT_ACTION] = action_fun
                if bool(event_args):
                    param[EVENT_ACTION_ARGS] = event_args

            params_list2.append(param)

        # --------------------------------------------------------------------------------
        # If replace=True is specified, updated the params of any existing track
        # with the same name. If none exists, proceed to create it as usual.
        # --------------------------------------------------------------------------------
        for param in params_list2:
            extra_delay = param.pop("delay", None) if isinstance(param, dict) else None
            if replace:
                if name is None:
                    raise ValueError("Must specify a track name if `replace` is specified")
                for existing_track in self.tracks:
                    if existing_track.name == name:
                        existing_track.update(param,
                                              quantize=quantize,
                                              delay=delay,
                                              interpolate=interpolate)
                    # TODO: Add unit test for update interpolate
                    # TODO: Add unit test around this (returning the track?)
                    return existing_track

            if self.max_tracks and len(self.tracks) >= self.max_tracks:
                raise TrackLimitReachedException(
                    "Timeline: Refusing to schedule track (hit limit of %d)" % self.max_tracks)

            def start_track(track_int):
                # --------------------------------------------------------------------------------
                # Add a new track.
                # --------------------------------------------------------------------------------
                if track_index is not None:
                    self.tracks.insert(track_index, track_int)
                else:
                    self.tracks.append(track_int)
                log.info("Timeline: Scheduled new track (total tracks: %d)" % len(self.tracks))

            if not bool(event_args) and sel_track_idx is not None:
                # if not bool(event_args):
                event_args = {"track_idx": sel_track_idx}
                if not bool(param.get(EVENT_ACTION_ARGS, {})):
                    param[EVENT_ACTION_ARGS] = event_args

            if isinstance(param, Track):
                track = param
                track.reset()
            else:
                # --------------------------------------------------------------------------------
                # Take a copy of params to avoid modifying the original
                # --------------------------------------------------------------------------------
                track = Track(
                    self,
                    max_event_count=count,
                    interpolate=interpolate,
                    output_device=output_device,
                    remove_when_done=remove_when_done,
                    name=name
                )

                track.update(copy.copy(param), quantize=quantize, delay=delay or extra_delay)
            tracks_list.append(track)

            start_track(track)

        if len(tracks_list) > 1:
            track = tracks_list
        elif len(tracks_list) == 1:
            track = tracks_list[0]
        else:
            track = None

        return track

    # --------------------------------------------------------------------------------
    # Backwards-compatibility
    # --------------------------------------------------------------------------------
    sched = schedule

    def unschedule(self, track):
        """
        Remove a track from playback.

        Args:
            track: The Track object.

        Raises:
            TrackNotFoundException: If the track is not playing.
        """
        if track not in self.tracks:
            raise TrackNotFoundException("Track is not currently scheduled")
        self.tracks.remove(track)

    def _schedule_action(
            self, function: Callable, quantize: float = 0.0, delay: float = 0.0
    ) -> None:
        """
        Schedule a function to be called at the given time offset.

        Args:
            function: The function to call
            quantize: The quantization level, in beats
            delay: The delay, in beats
        """
        scheduled_time = self.current_time
        if quantize:
            scheduled_time = quantize * math.ceil(float(self.current_time) / quantize)
        scheduled_time += delay
        action = Action(scheduled_time, function)
        self.actions.append(action)

    def get_track(self, track_id: Union[int, str]) -> Optional[Track]:
        """
        Get the Track corresponding to the given track_id.
        track_id can be a numeric index, or the name corresponding to a track.

        Args:
            track_id: An index or name

        Returns:
            The Track object, or None if not found.
        """
        if isinstance(track_id, int):
            return self.tracks[track_id]
        elif isinstance(track_id, str):
            return next((track for track in self.tracks if track.name == track_id), None)
        else:
            raise TypeError("Invalid type for track_id (must be an int or str)")

    def clear(self) -> None:
        """
        Remove all tracks.
        """
        for track in self.tracks[:]:
            self.unschedule(track)

    def wait(self):
        """
        Sleep until the timeline is finished.
        If the timeline never finishes, sleep forever.
        """
        while self.running:
            time.sleep(0.1)
