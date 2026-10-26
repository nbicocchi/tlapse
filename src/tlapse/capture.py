"""RTSP acquisition, frame averaging, output scheduling, and graceful shutdown."""

from collections import deque
from datetime import datetime
import logging
import math
import os
import signal
import threading
import time

from tlapse.video import SafeVideoWriter


logger = logging.getLogger(__name__)


class FrameReceiver:
    """Own camera reads and the rolling window independently of sampling."""

    def __init__(self, capture, frames, deadline):
        self.capture = capture
        self.window = deque(maxlen=frames)
        self.deadline = deadline
        self.condition = threading.Condition()
        self.stopped = threading.Event()
        self.updated = False
        self.done = False
        self.error = None
        self.failures = 0
        self.thread = threading.Thread(target=self._run, name="tlapse-receiver")

    def start(self):
        self.thread.start()

    def _read_once(self):
        ok, frame = self.capture.read()
        now = time.monotonic()
        if self.stopped.is_set():
            return
        valid = ok and frame is not None and frame.size > 0
        with self.condition:
            if valid and self.window:
                valid = frame.shape == self.window[0].shape
            if valid and now <= self.deadline:
                self.window.append(frame.copy())
                logger.debug("Updated averaging window: %d/%d frames",
                             len(self.window), self.window.maxlen)
            self.updated = True
            self.condition.notify_all()
        if valid:
            self.failures = 0
        elif now < self.deadline:
            self.failures += 1
            if self.failures == 1:
                logger.warning("Invalid stream frame; retrying")
            if self.failures >= 10:
                raise RuntimeError(
                    f"10 consecutive invalid reads: captured "
                    f"only {len(self.window)}/{self.window.maxlen} frames in the window"
                )

    def _run(self):
        try:
            while not self.stopped.is_set() and time.monotonic() < self.deadline:
                self._read_once()
        except Exception as exc:
            with self.condition:
                self.error = exc
        finally:
            with self.condition:
                self.done = True
                self.condition.notify_all()

    def wait(self, timeout):
        with self.condition:
            self.condition.wait_for(lambda: self.updated or self.done, timeout)
            self.updated = False
            if self.error is not None:
                raise RuntimeError(f"Camera receiver failed: {self.error}") from self.error

    def snapshot(self):
        # Frames are owned and never modified, so only references need copying.
        with self.condition:
            return list(self.window)

    def stop(self):
        self.stopped.set()
        self.thread.join()


def output_path(directory, stem, extension):
    """Keep second-resolution timestamps without overwriting existing files."""
    path = directory / f"{stem}{extension}"
    index = 1
    while path.exists():
        path = directory / f"{stem}_{index}{extension}"
        index += 1
    return path


def run(args):
    # Set capture options before importing OpenCV and opening the stream.
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
    try:
        import cv2
        import numpy as np
    except ImportError:
        logger.error("Missing dependencies: pip install opencv-python-headless numpy")
        return 1

    capture = cv2.VideoCapture()
    saved = 0
    video = None
    video_day = None
    window = []
    receiver = None
    stop_requested = False
    status = 0
    last_output = None

    def request_stop(signum, frame):
        nonlocal stop_requested
        # Do not raise KeyboardInterrupt during encoding or atomic commits.
        stop_requested = True

    previous_sigint = signal.signal(signal.SIGINT, request_stop)
    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    try:
        logger.info(
            "Starting timelapse: url=<redacted>, mode=%s, output=%s, "
            "duration=%s s, speedup=%sx, average_frames=%d, fps=%s, "
            "crf=%d, maxrate=%s, rotation=daily-at-local-midnight, log_level=%s",
            args.mode, args.output.resolve(), args.duration, args.speedup, args.frames,
            args.fps, args.crf, args.maxrate, args.log_level,
        )
        opened = capture.open(
            args.url,
            cv2.CAP_FFMPEG,
            [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000,
                cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000,
            ],
        )
        if not opened:
            raise RuntimeError("Unable to open the RTSP stream")

        start = time.monotonic()
        deadline = start + args.duration
        receiver = FrameReceiver(capture, args.frames, deadline)
        receiver.start()

        def rotate_video(now):
            nonlocal video
            if video is not None and now.date() != video_day:
                video.close()
                logger.warning(
                    "Daily video completed: %s (%d frames); "
                    "the next frame will start a new file",
                    video.output.resolve(), video.count,
                )
                video = None

        def emit(average, now):
            nonlocal saved, video, video_day, last_output
            timestamp = now.strftime("%Y%m%d_%H%M%S")
            if args.mode == "video":
                rotate_video(now)
                if video is None:
                    output = output_path(args.output, f"tlapse_video_{timestamp}", ".mp4")
                    video = SafeVideoWriter(
                        output, args.fps, crf=args.crf, maxrate=args.maxrate, background=True,
                    )
                    video_day = now.date()
                video.write(average)
                output = video.output
            else:
                output = output_path(args.output, f"tlapse_image_{timestamp}", ".png")
                if not cv2.imwrite(str(output), average):
                    raise RuntimeError(f"Unable to save {output}")
            saved += 1
            action = "buffered" if args.mode == "video" else "saved"
            logger.debug(
                "Output frame %d %s (averaged %d frames): %s",
                saved, action, len(window), output.resolve(),
            )
            last_output = average

        def window_average():
            if not window:
                return None
            if len(window) == 1:
                return window[0].copy()
            # Allocate an accumulator only at sampling time, without stacking
            # all source frames into an additional window-sized array.
            total = window[0].astype(np.float64)
            for frame in window[1:]:
                total += frame
            total /= len(window)
            np.rint(total, out=total)
            return total.astype(np.uint8)

        def catch_up(target):
            # Missing source frames or slow encoding must not shorten playback.
            # Reuse the last average (or the current window before first output).
            if saved >= target:
                return
            fallback = last_output if last_output is not None else window_average()
            if fallback is None:
                return
            while saved < target:
                logger.debug("Repeating frame to preserve speedup: %d/%d", saved + 1, target)
                emit(fallback, datetime.now())

        while not stop_requested:
            # Finalize yesterday's buffered frames even between output samples.
            rotate_video(datetime.now())
            now = time.monotonic()
            if now >= deadline:
                break
            receiver.wait(timeout=min(0.1, deadline - now))
            if stop_requested:
                break
            now = time.monotonic()
            target = math.floor((min(now, deadline) - start) / args.interval)
            if target > saved:
                window = receiver.snapshot()
                if window:
                    catch_up(target - 1)
                    emit(window_average(), datetime.now())
                    window = []
            if now >= deadline:
                break
        window = receiver.snapshot()
        elapsed = max(0, min(time.monotonic(), deadline) - start)
        catch_up(math.floor(elapsed / args.interval))
        if stop_requested:
            status = 130
    except (RuntimeError, OSError, cv2.error) as exc:
        logger.error("Capture failed: %s", exc, exc_info=logger.isEnabledFor(logging.DEBUG))
        status = 1
    except KeyboardInterrupt:
        # Support callers that raise KeyboardInterrupt directly.
        status = 130
        try:
            if receiver is not None:
                window = receiver.snapshot()
            if window:
                catch_up(math.floor((min(time.monotonic(), deadline) - start) / args.interval))
        except (RuntimeError, OSError, cv2.error) as exc:
            logger.error("Failed to save pending frames: %s", exc,
                         exc_info=logger.isEnabledFor(logging.DEBUG))
            status = 1
    finally:
        try:
            if receiver is not None:
                receiver.stop()
            if video is not None:
                logger.info("Finalizing video; waiting for pending frames to be saved")
                video.close()
        except (RuntimeError, OSError, cv2.error) as exc:
            logger.error("Video finalization failed: %s", exc,
                         exc_info=logger.isEnabledFor(logging.DEBUG))
            status = 1
        finally:
            capture.release()
            signal.signal(signal.SIGINT, previous_sigint)
            signal.signal(signal.SIGTERM, previous_sigterm)
    if status in (0, 130):
        message = "Capture interrupted" if status == 130 else "Timelapse completed"
        logger.info("%s: %d output frames saved", message, saved)
    return status
