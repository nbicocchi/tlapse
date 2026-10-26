"""Deterministic RTSP capture fixtures shared by capture and video tests."""

import contextlib
import io
import signal
import types
from unittest.mock import patch

import numpy as np

from tlapse import capture, cli


def run_timelapse(duration, interrupt=False, frames=2, mode="images", output=None,
                  fps=25, log_level="INFO", crf=26, speedup=None,
                  source_fps=4, read_delays=None, wall_start=None, maxrate="6M"):
    clock = [0.0]
    images = []

    class Capture:
        released = False

        def open(self, *args):
            return True

        def read(self):
            clock[0] += next(delays, 1 / source_fps)
            if interrupt and clock[0] >= 3:
                if interrupt == "sigint":
                    signal.raise_signal(signal.SIGINT)
                else:
                    raise KeyboardInterrupt
            return True, np.full((2, 2, 3), int(clock[0] * 4) % 256, dtype=np.uint8)

        def release(self):
            self.released = True

    delays = iter(read_delays or ())
    stream = Capture()

    class DeterministicReceiver(capture.FrameReceiver):
        """Advance the virtual source one read at a time without real threads."""

        def start(self):
            pass

        def wait(self, timeout):
            self._read_once()

        def stop(self):
            self.stopped.set()

    def save(path, image):
        images.append((clock[0], path, image.copy()))
        return True

    cv2 = types.SimpleNamespace(
        VideoCapture=lambda: stream, CAP_FFMPEG=0,
        CAP_PROP_OPEN_TIMEOUT_MSEC=1, CAP_PROP_READ_TIMEOUT_MSEC=2,
        imwrite=save, error=RuntimeError,
    )
    argv = [
        "--url", "rtsp://example/stream",
        "--duration", duration, "--speedup", str(2 * fps if speedup is None else speedup),
        "--frames", str(frames),
        "--mode", mode, "--fps", str(fps), "--log-level", log_level,
        "--crf", str(crf), "--maxrate", maxrate,
    ]
    if output is not None:
        argv.extend(["--output", str(output)])
    with contextlib.ExitStack() as stack:
        if wall_start is not None:
            from datetime import timedelta
            wall_clock = stack.enter_context(patch.object(capture, "datetime"))
            wall_clock.now.side_effect = lambda: wall_start + timedelta(seconds=clock[0])
        stack.enter_context(patch.dict("sys.modules", {"cv2": cv2}))
        stack.enter_context(patch.object(capture, "FrameReceiver", DeterministicReceiver))
        stack.enter_context(patch.object(capture.time, "monotonic", side_effect=lambda: clock[0]))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        result = cli.main(argv)
    if not stream.released:
        raise AssertionError("Capture was not released")
    return result, images
