"""Capture scheduling, averaging, and interruption tests."""

import unittest
from fractions import Fraction
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch

import numpy as np

from tests.helpers import run_timelapse
from tlapse import capture, cli


class CaptureTests(unittest.TestCase):
    def test_receiver_owns_window_and_copies_reused_camera_buffer(self):
        release = threading.Event()
        reads = []

        class Camera:
            def __init__(self):
                self.frame = np.zeros((2, 2, 3), dtype=np.uint8)
                self.count = 0

            def read(self):
                reads.append(threading.get_ident())
                self.count += 1
                if self.count > 8:
                    release.wait(timeout=5)
                self.frame.fill(self.count)
                return True, self.frame

        receiver = capture.FrameReceiver(Camera(), 3, float("inf"))
        receiver.start()
        try:
            with receiver.condition:
                ready = receiver.condition.wait_for(
                    lambda: receiver.window and receiver.window[-1][0, 0, 0] == 8,
                    timeout=5,
                )
            self.assertTrue(ready)
            snapshot = receiver.snapshot()
            self.assertEqual([int(frame[0, 0, 0]) for frame in snapshot], [6, 7, 8])
            self.assertNotIn(threading.get_ident(), reads)
        finally:
            receiver.stopped.set()
            release.set()
            receiver.stop()

    def test_receiver_propagates_read_errors(self):
        class Camera:
            def read(self):
                raise OSError("camera disconnected")

        receiver = capture.FrameReceiver(Camera(), 3, float("inf"))
        receiver.start()
        try:
            with self.assertRaisesRegex(RuntimeError, "camera disconnected"):
                receiver.wait(timeout=5)
        finally:
            receiver.stop()

    def test_speedup_controls_capture_schedule(self):
        result, images = run_timelapse("5", speedup="25x", fps=25)
        self.assertEqual(result, 0)
        self.assertEqual([item[0] for item in images], [1, 2, 3, 4, 5])

    def test_full_day_at_24x_without_source_fps_metadata(self):
        # Fake capture has no FPS metadata API. Simulate actual frame arrivals,
        # not a declared source rate; encoding is tested separately with FFmpeg.
        for source_fps in (2, 0.5):
            with self.subTest(source_fps=source_fps):
                writers = []

                class Writer:
                    def __init__(self, output, fps, crf, maxrate, background):
                        self.output = output
                        self.fps = Fraction(str(fps))
                        self.count = 0
                        self.frames = []
                        self.closed = False
                        writers.append(self)

                    def write(self, frame):
                        self.count += 1

                    def close(self):
                        self.closed = True

                with patch.object(capture, "SafeVideoWriter", Writer), \
                        patch.object(cli.shutil, "which", return_value="ffmpeg"):
                    result, _ = run_timelapse(
                        "86400", mode="video", fps=25, speedup=24,
                        frames=16, source_fps=source_fps,
                    )
                self.assertEqual(result, 0)
                self.assertEqual(len(writers), 1)
                self.assertEqual(writers[0].count, 90000)
                self.assertEqual(writers[0].count / writers[0].fps, 3600)
                self.assertTrue(writers[0].closed)

    def test_delayed_reads_do_not_shorten_output(self):
        result, images = run_timelapse(
            "24", speedup=24, fps=25, source_fps=2, frames=16,
            read_delays=[0.5, 0.5, 5, 0.5, 4],
        )
        self.assertEqual(result, 0)
        self.assertEqual(len(images), 25)

    def test_output_fps_does_not_change_speedup(self):
        for fps in (10, 25, 30):
            with self.subTest(fps=fps):
                result, images = run_timelapse(
                    "24", speedup=24, fps=fps, source_fps=2, frames=16,
                )
                self.assertEqual(result, 0)
                self.assertEqual(len(images), fps)


    def test_duration_interval_and_average(self):
        result, images = run_timelapse("5")
        self.assertEqual(result, 0)
        self.assertEqual([item[0] for item in images], [2, 4])
        for (_, path, image), expected in zip(images, [8, 16]):
            self.assertRegex(path, r"tlapse_image_\d{8}_\d{6}(?:_\d+)?\.png$")
            np.testing.assert_array_equal(image, np.full((2, 2, 3), expected))

    def test_infinite_duration_can_be_interrupted(self):
        result, images = run_timelapse("inf", interrupt=True)
        self.assertEqual(result, 130)
        self.assertEqual(len(images), 1)

    def test_rolling_window_discards_old_frames(self):
        result, images = run_timelapse("5", frames=3)
        self.assertEqual(result, 0)
        self.assertEqual([item[0] for item in images], [2, 4])
        # At t=2 the newest values are 6, 7, 8; at t=4: 14, 15, 16.
        for (_, _, image), expected in zip(images, [7, 15]):
            np.testing.assert_array_equal(image, np.full((2, 2, 3), expected))

    def test_window_of_one_uses_latest_frame(self):
        result, images = run_timelapse("5", frames=1)
        self.assertEqual(result, 0)
        for (_, _, image), expected in zip(images, [8, 16]):
            np.testing.assert_array_equal(image, np.full((2, 2, 3), expected))

    def test_average_is_computed_only_when_sampling_without_running_sum(self):
        with patch.object(np, "zeros", wraps=np.zeros) as accumulator, \
                patch.object(np, "rint", wraps=np.rint) as average:
            result, images = run_timelapse("5", frames=3)
        self.assertEqual(result, 0)
        self.assertEqual(len(images), 2)
        self.assertEqual(average.call_count, 2)
        accumulator.assert_not_called()
        for (_, _, image), expected in zip(images, [7, 15]):
            np.testing.assert_array_equal(image, np.full((2, 2, 3), expected))

    def test_window_of_one_skips_floating_point_averaging(self):
        with patch.object(np, "rint", wraps=np.rint) as average:
            result, images = run_timelapse("5", frames=1)
        self.assertEqual(result, 0)
        self.assertEqual(len(images), 2)
        average.assert_not_called()

    def test_sampled_average_does_not_overflow_uint8(self):
        result, images = run_timelapse("60", frames=16, speedup=1500, fps=25)
        self.assertEqual(result, 0)
        self.assertEqual(len(images), 1)
        # Latest values 225..240 average to 232.5, rounded to even (232).
        np.testing.assert_array_equal(images[0][2], np.full((2, 2, 3), 232))

    def test_stop_between_samples_does_not_emit_extra_frame(self):
        result, images = run_timelapse("1.5", frames=3)
        self.assertEqual(result, 0)
        self.assertEqual(images, [])

    def test_partially_filled_window_is_sampled_without_waiting(self):
        result, images = run_timelapse("2.4", frames=100)
        self.assertEqual(result, 0)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0][0], 2)
        np.testing.assert_array_equal(images[0][2], np.full((2, 2, 3), 4))

    def test_interval_takes_priority_over_frame_count(self):
        result, images = run_timelapse("5", frames=100)
        self.assertEqual(result, 0)
        self.assertEqual([item[0] for item in images], [2, 4])
        for (_, _, image), expected in zip(images, [4, 8]):
            np.testing.assert_array_equal(image, np.full((2, 2, 3), expected))

    def test_no_image_without_frames(self):
        result, images = run_timelapse("0.1")
        self.assertEqual(result, 0)
        self.assertEqual(images, [])

    def test_timestamp_collisions_do_not_overwrite_files(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            for extension in (".png", ".mp4"):
                stem = "tlapse_test_20261026_120000"
                for suffix in ("", "_1", "_2"):
                    output = capture.output_path(directory, stem, extension)
                    self.assertEqual(output.name, f"{stem}{suffix}{extension}")
                    output.write_bytes(b"existing output")
