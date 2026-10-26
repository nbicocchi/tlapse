"""HEVC encoding, atomic updates, rotation, and graceful shutdown tests."""

import json
from datetime import datetime
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

import numpy as np

from tlapse import capture, cli, video
from tests.helpers import run_timelapse


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Requires FFmpeg")
class SafeVideoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "img_test.mp4"
        self.writer = video.SafeVideoWriter(self.output, 25)
        self.addCleanup(self.writer.close)
        self.frame = np.full((16, 16, 3), 100, dtype=np.uint8)

    def frame_count(self):
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
             "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(self.output)],
            capture_output=True, text=True, check=True,
        )
        return int(result.stdout.strip())

    def test_each_snapshot_is_a_complete_video(self):
        batch_size = self.writer.batch_size
        for _ in range(batch_size - 1):
            self.writer.write(self.frame)
        self.assertFalse(self.output.exists())
        self.writer.write(self.frame)
        self.assertEqual(self.frame_count(), batch_size)
        for _ in range(batch_size):
            self.writer.write(self.frame)
        self.assertEqual(self.frame_count(), 2 * batch_size)
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])

    def test_batch_size_is_fps_multiple_nearest_120(self):
        for fps, expected in ((24, 120), (25, 125), (30, 120), (50, 100),
                              (48, 144), (60, 120), (240, 240),
                              (29.97, 120), (2.5, 120), (0.5, 120)):
            with self.subTest(fps=fps):
                writer = video.SafeVideoWriter(self.output, fps)
                self.assertEqual(writer.batch_size, expected)
                writer.close()

    def test_close_flushes_partial_batch(self):
        for _ in range(3):
            self.writer.write(self.frame)
        self.writer.close()
        self.assertEqual(self.frame_count(), 3)
        self.writer.close()

    def test_24_fps_commits_120_frame_batches_with_keyframes(self):
        self.writer.close()
        self.writer = video.SafeVideoWriter(self.output, 24)
        self.addCleanup(self.writer.close)
        for index in range(240):
            self.writer.write(self.frame)
            if index == 118:
                self.assertFalse(self.output.exists())
            elif index == 119:
                self.assertEqual(self.frame_count(), 120)
        self.assertEqual(self.frame_count(), 240)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "packet=flags", "-of", "json", str(self.output)],
            capture_output=True, text=True, check=True,
        )
        packets = json.loads(probe.stdout)["packets"]
        self.assertEqual([i for i, packet in enumerate(packets) if "K" in packet["flags"]],
                         [0, 120])

    def test_only_batch_starts_are_i_frames_even_with_scene_changes(self):
        for fps in (24, 25, 29.97):
            with self.subTest(fps=fps), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "scenes.mp4"
                writer = video.SafeVideoWriter(output, fps)
                self.addCleanup(writer.close)
                count = 2 * writer.batch_size + 7
                rng = np.random.default_rng(42)
                for index in range(count):
                    # Abrupt cuts inside batches must not introduce I-frames.
                    scene = (index // 15) % 3
                    if scene == 2:
                        frame = rng.integers(0, 256, (64, 96, 3), dtype=np.uint8)
                    else:
                        frame = np.full((64, 96, 3), scene * 255, dtype=np.uint8)
                    writer.write(frame)
                writer.close()
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "frame=pict_type,key_frame", "-of", "json", str(output)],
                    capture_output=True, text=True, check=True,
                )
                frames = json.loads(probe.stdout)["frames"]
                self.assertEqual(len(frames), count)
                starts = [0, writer.batch_size, 2 * writer.batch_size]
                self.assertEqual([i for i, frame in enumerate(frames)
                                  if frame["pict_type"] == "I"], starts)
                self.assertEqual([i for i, frame in enumerate(frames)
                                  if frame["key_frame"] == 1], starts)
                self.assertTrue(all(frame["pict_type"] == "P" for i, frame in enumerate(frames)
                                    if i not in starts))

    def test_custom_crf_reaches_ffmpeg(self):
        with patch.object(video.SafeVideoWriter, "_run_encoder", wraps=video.SafeVideoWriter._run_encoder) as encode:
            result, _ = run_timelapse(
                "5", mode="video", output=self.output.parent, crf=18,
            )
        self.assertEqual(result, 0)
        command = encode.call_args.args[0]
        self.assertEqual(command[command.index("-crf") + 1], "18")

    def test_capture_continues_while_encoding_a_batch(self):
        started = threading.Event()
        captured = threading.Event()
        encode = video.SafeVideoWriter._run_encoder
        write = video.SafeVideoWriter.write

        def slow_encoding(*args, **kwargs):
            started.set()
            if not captured.wait(timeout=5):
                raise RuntimeError("Capture did not continue during encoding")
            return encode(*args, **kwargs)

        def write_and_wait_for_encoder(writer, frame):
            write(writer, frame)
            if writer.pending is not None:
                self.assertTrue(started.wait(timeout=5))

        def capture_log(message, *args):
            if message.startswith("Updated averaging window") and started.is_set():
                captured.set()

        with patch.object(video.SafeVideoWriter, "_run_encoder", side_effect=slow_encoding), \
                patch.object(video.SafeVideoWriter, "write", write_and_wait_for_encoder), \
                patch.object(capture.logger, "debug", side_effect=capture_log):
            result, _ = run_timelapse(
                "260", mode="video", output=self.output.parent, fps=24,
            )
        self.assertEqual(result, 0)
        self.assertTrue(captured.is_set())
        self.output = next(self.output.parent.glob("tlapse_video_*.mp4"))
        self.assertEqual(self.frame_count(), 130)

    def test_receiving_sampling_and_encoding_use_three_distinct_threads(self):
        receivers, samplers, encoders = set(), set(), set()
        pause = threading.Event()
        encode = video.SafeVideoWriter._run_encoder
        write = video.SafeVideoWriter.write

        class Camera:
            def open(self, *args):
                return True

            def read(self):
                receivers.add(threading.get_ident())
                pause.wait(0.002)
                return True, np.full((64, 96, 3), 100, dtype=np.uint8)

            def release(self):
                pass

        def sample(writer, frame):
            samplers.add(threading.get_ident())
            write(writer, frame)

        def record_encoding(*args):
            encoders.add(threading.get_ident())
            return encode(*args)

        cv2 = types.SimpleNamespace(
            VideoCapture=Camera, CAP_FFMPEG=0, CAP_PROP_OPEN_TIMEOUT_MSEC=1,
            CAP_PROP_READ_TIMEOUT_MSEC=2, error=RuntimeError,
        )
        args = cli.parse_args([
            "--url", "rtsp://example", "--duration", "0.35", "--speedup", "0.024",
            "--fps", "24", "--frames", "3", "--output", str(self.output.parent),
        ])
        with patch.dict("sys.modules", {"cv2": cv2}), \
                patch.object(video.SafeVideoWriter, "write", sample), \
                patch.object(video.SafeVideoWriter, "_run_encoder", side_effect=record_encoding):
            self.assertEqual(capture.run(args), 0)
        self.assertEqual(len(receivers), 1)
        self.assertEqual(samplers, {threading.get_ident()})
        self.assertEqual(len(encoders), 1)
        self.assertEqual(len(receivers | samplers | encoders), 3)

    def test_background_failure_preserves_published_video_and_is_reported(self):
        self.writer.close()
        self.writer = video.SafeVideoWriter(self.output, 24, background=True)
        self.addCleanup(self.writer.close)
        for _ in range(120):
            self.writer.write(self.frame)
        self.writer.pending.result(timeout=10)
        previous = self.output.read_bytes()
        failure = subprocess.CompletedProcess([], 1, stderr=b"encoding failed")
        with patch.object(video.SafeVideoWriter, "_run_encoder", return_value=failure):
            for _ in range(120):
                self.writer.write(self.frame)
            with self.assertRaisesRegex(RuntimeError, "encoding failed"):
                self.writer.pending.result(timeout=10)
        with self.assertRaisesRegex(RuntimeError, "encoding failed"):
            self.writer.close()
        self.assertEqual(self.output.read_bytes(), previous)
        self.assertEqual(self.frame_count(), 120)

    def test_encoder_input_streams_frames_without_serializing_the_batch(self):
        class NoBytesFrame(np.ndarray):
            def tobytes(self, *args, **kwargs):
                raise AssertionError("Frame data must be streamed, not copied to bytes")

        frame = np.arange(24, dtype=np.uint8).reshape(2, 4, 3).view(NoBytesFrame)
        result = video.SafeVideoWriter._run_encoder(
            [sys.executable, "-c",
             "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
            [frame, frame],
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, bytes(range(24)) * 2)

    def test_decoded_motion_remains_continuous_across_batch_boundaries(self):
        self.writer.close()
        self.writer = video.SafeVideoWriter(self.output, 24, background=True)
        self.addCleanup(self.writer.close)
        for index in range(247):
            frame = np.zeros((64, 320, 3), dtype=np.uint8)
            frame[16:48, index:index + 16] = 255
            self.writer.write(frame)
        self.writer.close()
        decoded = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(self.output),
             "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1"],
            capture_output=True, check=True,
        )
        frames = np.frombuffer(decoded.stdout, dtype=np.uint8).reshape(-1, 64, 320)
        positions = [int(np.flatnonzero(frame[32] > 128)[0]) for frame in frames]
        self.assertEqual(positions, list(range(247)))

    def test_encoder_early_exit_closes_input_pipe(self):
        result = video.SafeVideoWriter._run_encoder(
            [sys.executable, "-c", "raise SystemExit(7)"],
            [np.zeros((1024, 1024, 3), dtype=np.uint8)],
        )
        self.assertEqual(result.returncode, 7)

    def test_encoder_start_failure_is_reported(self):
        with self.assertRaises(FileNotFoundError):
            video.SafeVideoWriter._run_encoder(
                ["/nonexistent/tlapse-ffmpeg"],
                [np.zeros((1024, 1024, 3), dtype=np.uint8)],
            )

    def test_preset_and_maxrate_reach_ffmpeg(self):
        for rate, buffer in (("6M", "12000000"), ("2.5M", "5000000"),
                             ("800k", "1600000"), ("4000000", "8000000")):
            with self.subTest(rate=rate), tempfile.TemporaryDirectory() as directory:
                with patch.object(video.SafeVideoWriter, "_run_encoder", wraps=video.SafeVideoWriter._run_encoder) as encode:
                    result, _ = run_timelapse(
                        "5", mode="video", output=Path(directory), maxrate=rate,
                    )
                self.assertEqual(result, 0)
                command = encode.call_args.args[0]
                self.assertEqual(command[command.index("-preset") + 1], "fast")
                self.assertEqual(command[command.index("-maxrate") + 1], rate)
                self.assertEqual(command[command.index("-bufsize") + 1], buffer)

    def test_slow_sources_preserve_speedup_in_encoded_video(self):
        for source_fps in (2, 0.5):
            with self.subTest(source_fps=source_fps), tempfile.TemporaryDirectory() as directory:
                result, _ = run_timelapse(
                    "24", mode="video", output=Path(directory), speedup=24,
                    fps=25, frames=16, source_fps=source_fps,
                )
                self.assertEqual(result, 0)
                output = next(Path(directory).glob("tlapse_video_*.mp4"))
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                     "-show_entries", "stream=nb_read_frames,duration", "-of", "json", str(output)],
                    capture_output=True, text=True, check=True,
                )
                stream = json.loads(probe.stdout)["streams"][0]
                self.assertEqual(int(stream["nb_read_frames"]), 25)
                self.assertAlmostEqual(float(stream["duration"]), 1)

    def test_video_rotation_preserves_all_frames_and_resets_timestamps(self):
        # Cross local midnight after the first sample, including a partial batch.
        for fps in (2, 2.5):
            with self.subTest(fps=fps), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                result, images = run_timelapse(
                    "7", mode="video", output=output, fps=fps,
                    wall_start=datetime(2026, 10, 26, 23, 59, 57),
                )
                self.assertEqual(result, 0)
                self.assertEqual(images, [])
                videos = sorted(Path(directory).glob("tlapse_video_*.mp4"))
                self.assertEqual(len(videos), 2)
                counts = []
                for video in videos:
                    result = subprocess.run(
                        ["ffprobe", "-v", "error", "-show_entries", "packet=pts_time",
                         "-of", "csv=p=0", str(video)],
                        capture_output=True, text=True, check=True,
                    )
                    timestamps = [float(line) for line in result.stdout.splitlines()]
                    counts.append(len(timestamps))
                    self.assertEqual(timestamps[0], 0)
                self.assertEqual(counts, [1, 2])
                self.assertIn("20261026_235959", videos[0].name)
                self.assertIn("20261027_000001", videos[1].name)
                self.assertEqual(len(list(Path(directory).iterdir())), 2)

    def test_rotation_does_not_create_an_empty_next_video(self):
        result, _ = run_timelapse(
            "3", mode="video", output=self.output.parent, fps=2,
            wall_start=datetime(2026, 10, 26, 23, 59, 57),
        )
        self.assertEqual(result, 0)
        videos = list(self.output.parent.glob("tlapse_video_*.mp4"))
        self.assertEqual(len(videos), 1)
        self.output = videos[0]
        self.assertEqual(self.frame_count(), 1)

    def test_ctrl_c_flushes_sampled_rolling_average_and_video_buffer(self):
        original_handler = signal.getsignal(signal.SIGINT)
        result, images = run_timelapse(
            "inf", interrupt="sigint", frames=100, mode="video", output=self.output.parent,
        )
        self.assertEqual(result, 130)
        self.assertEqual(images, [])
        self.assertEqual(signal.getsignal(signal.SIGINT), original_handler)
        self.output = next(self.output.parent.glob("tlapse_video_*.mp4"))
        self.assertEqual(self.frame_count(), 1)
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])

    def test_ctrl_c_during_encoding_waits_for_commit(self):
        run = video.SafeVideoWriter._run_encoder
        write = video.SafeVideoWriter.write
        sampled = []

        def record_sample(writer, frame):
            sampled.append(frame)
            write(writer, frame)

        def interrupted_encoding(*args, **kwargs):
            signal.raise_signal(signal.SIGINT)
            return run(*args, **kwargs)

        with patch.object(video.SafeVideoWriter, "_run_encoder", side_effect=interrupted_encoding), \
                patch.object(video.SafeVideoWriter, "write", record_sample):
            result, _ = run_timelapse(
                "inf", mode="video", output=self.output.parent, fps=2,
            )
        self.assertEqual(result, 130)
        self.output = next(self.output.parent.glob("tlapse_video_*.mp4"))
        # Capture may sample another frame before the worker's signal arrives.
        self.assertGreaterEqual(len(sampled), 120)
        self.assertEqual(self.frame_count(), len(sampled))

    def test_fractional_fps_and_odd_dimensions(self):
        self.writer.close()
        self.writer = video.SafeVideoWriter(self.output, 29.97)
        self.addCleanup(self.writer.close)
        for _ in range(243):
            self.writer.write(np.zeros((15, 17, 3), dtype=np.uint8))
        self.writer.close()
        self.assertEqual(self.frame_count(), 243)
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "packet=pts_time",
             "-of", "csv=p=0", str(self.output)],
            capture_output=True, text=True, check=True,
        )
        np.testing.assert_allclose(
            [float(line) for line in result.stdout.splitlines()], np.arange(243) / 29.97, atol=1e-6,
        )

    def test_fragment_timestamps_are_continuous(self):
        for _ in range(253):
            self.writer.write(self.frame)
        self.writer.close()
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "packet=pts_time", "-of", "csv=p=0", str(self.output)],
            capture_output=True, text=True, check=True,
        )
        timestamps = [float(line) for line in result.stdout.splitlines()]
        np.testing.assert_allclose(timestamps, np.arange(253) / 25, atol=1e-6)

    def test_encoder_failure_preserves_published_video(self):
        self.writer.write(self.frame)
        self.writer.flush()
        previous = self.output.read_bytes()
        failure = subprocess.CompletedProcess([], 1, stderr=b"encoding failed")
        with patch.object(video.SafeVideoWriter, "_run_encoder", return_value=failure):
            self.writer.write(self.frame)
            with self.assertRaisesRegex(RuntimeError, "encoding failed"):
                self.writer.flush()
        self.assertEqual(self.output.read_bytes(), previous)
        self.assertEqual(self.frame_count(), 1)

    def test_sigkill_during_update_preserves_published_video(self):
        self.writer.write(self.frame)
        self.writer.flush()
        previous = self.output.read_bytes()
        code = '''
import os
from pathlib import Path
import signal
import sys
from unittest.mock import patch
import numpy as np
from tlapse.video import SafeVideoWriter

writer = SafeVideoWriter(Path(sys.argv[1]), 25)
def interrupted_commit(*args, **kwargs):
    os.kill(os.getpid(), signal.SIGKILL)
with patch("tlapse.video.os.replace", side_effect=interrupted_commit):
    writer.write(np.zeros((16, 16, 3), dtype=np.uint8))
    writer.flush()
'''
        result = subprocess.run([sys.executable, "-c", code, str(self.output)],
                                cwd=Path(__file__).resolve().parents[1], timeout=30)
        self.assertEqual(result.returncode, -signal.SIGKILL)
        self.assertEqual(self.output.read_bytes(), previous)
        self.assertEqual(self.frame_count(), 1)


if __name__ == "__main__":
    unittest.main()
