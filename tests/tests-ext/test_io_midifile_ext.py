""" Unit tests for Key """

import os
import isobar as iso
from isobar.io.midifile import MidiFileOutputDevice, MidiFileInputDevice
import pytest
from tests import dummy_timeline


def test_io_midifile_write_rests(dummy_timeline):
    events = {
        iso.EVENT_NOTE: iso.PSequence([60, None, None, 62], 1),
        iso.EVENT_DURATION: iso.PSequence([0.5, 1.5, 1, 1], 1),
        iso.EVENT_GATE: iso.PSequence([2, 0.5, 1, 1], 1),
        iso.EVENT_AMPLITUDE: iso.PSequence([64, 32, 16, 8], 1)
    }

    midifile = MidiFileOutputDevice("output.mid")
    dummy_timeline.output_device = midifile
    dummy_timeline.schedule(events)
    dummy_timeline.run()
    midifile.write()

    d = MidiFileInputDevice("output.mid").read()

    # for key in events.keys():
    #     assert isinstance(d[key], iso.PSequence)
    #     assert list(d[key]) == list(events[key])

    for key in events.keys():
        assert isinstance(d[key], iso.PSequence)
        if key == iso.EVENT_NOTE:

            for i, note in enumerate(list(events[key])):
                if note is not None:
                    return
            assert list(d[key]) == [e or 0 for e in events[key][:i]]
        else:
            assert list(d[key]) == list(events[key])

    os.unlink("output.mid")
