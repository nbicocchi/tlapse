"""In-memory HEVC encoding and atomic fragmented MP4 updates."""

from fractions import Fraction
from concurrent.futures import Future, ThreadPoolExecutor
import logging
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
import shutil
import selectors
import struct
import subprocess
import tempfile


logger = logging.getLogger(__name__)


class SafeVideoWriter:
    """Encode batches in memory and publish MP4 fragments atomically."""

    def __init__(self, output, fps, crf=26, maxrate="6M", background=False):
        self.output = output
        self.crf = crf
        self.maxrate = maxrate
        number = maxrate[:-1] if maxrate[-1] in "kKMG" else maxrate
        self.bufsize = str(int(float(number) * {
            "k": 1000, "K": 1000, "M": 1000000, "G": 1000000000,
        }.get(maxrate[-1], 1)) * 2)
        self.fps = Fraction(str(fps)).limit_denominator(100000)
        # Choose the whole-second FPS multiple nearest 120 frames; ties go up.
        # Fractional FPS still require a whole number of buffered frames.
        batch_seconds = max(1, math.floor(120 / self.fps + Fraction(1, 2)))
        self.batch_size = math.ceil(batch_seconds * self.fps)
        self.frames = []
        self.count = 0
        self.sequence = 0
        self.shape = None
        self.closed = False
        self.failed = False
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="tlapse-video",
        ) if background else None
        self.pending = None
        self.queue = Queue(maxsize=self.batch_size) if background else None
        self.buffered = 0
        self.worker = self.executor.submit(self._consume) if background else None

    def write(self, frame):
        if self.pending is not None and self.pending.done():
            self.pending.result()
        if self.closed or self.failed:
            raise RuntimeError("Video writer is closed or has failed")
        if self.shape is not None and frame.shape != self.shape:
            raise RuntimeError("Frame dimensions changed during video recording")
        self.shape = frame.shape
        if self.queue is not None:
            count = self.buffered + 1
            completion = Future() if count == self.batch_size else None
            self._enqueue(("frame", frame.copy(), completion))
            self.buffered = count % self.batch_size
            if completion is not None:
                self.pending = completion
            logger.debug("Queued averaged video frame: %d/%d", count, self.batch_size)
            return
        self.frames.append(frame.copy())
        logger.debug("Buffered video frame: %d/%d", len(self.frames), self.batch_size)
        if len(self.frames) >= self.batch_size:
            self.flush()

    @staticmethod
    def boxes(data, start=0, end=None):
        end = len(data) if end is None else end
        while start < end:
            if end - start < 8:
                raise RuntimeError("Incomplete MP4 header")
            size, kind = struct.unpack_from(">I4s", data, start)
            header = 8
            if size == 1:
                if end - start < 16:
                    raise RuntimeError("Incomplete extended MP4 header")
                size = struct.unpack_from(">Q", data, start + 8)[0]
                header = 16
            elif size == 0:
                size = end - start
            if size < header or start + size > end:
                raise RuntimeError("Incomplete MP4 box")
            yield kind, start, start + header, start + size
            start += size

    def fragment(self, encoded):
        """Align timestamps of independently encoded batches without re-encoding.

        default_base_moof makes data offsets relative. Only tfdt and mfhd
        need updating when concatenating matching fragments. Per-batch mfra
        indexes must not be copied into the final file.
        """
        data = bytearray(encoded)
        parts = []
        sequence = self.sequence
        for kind, start, payload, end in self.boxes(data):
            if kind in (b"ftyp", b"moov"):
                if self.count == 0:
                    parts.append(bytes(data[start:end]))
            elif kind == b"moof":
                sequence += 1
                for child, _, child_payload, child_end in self.boxes(data, payload, end):
                    if child == b"mfhd":
                        struct.pack_into(">I", data, child_payload + 4, sequence)
                    elif child == b"traf":
                        for field, _, field_payload, _ in self.boxes(data, child_payload, child_end):
                            if field == b"tfdt":
                                fmt = ">Q" if data[field_payload] == 1 else ">I"
                                offset = field_payload + 4
                                ticks = struct.unpack_from(fmt, data, offset)[0]
                                ticks += self.count * self.fps.denominator
                                struct.pack_into(fmt, data, offset, ticks)
                parts.append(bytes(data[start:end]))
            elif kind == b"mdat":
                parts.append(bytes(data[start:end]))
        if sequence == self.sequence:
            raise RuntimeError("FFmpeg did not produce any MP4 fragments")
        return b"".join(parts), sequence

    def flush(self):
        if self.queue is not None:
            if self.worker.done():
                self.worker.result()
            if self.buffered:
                completion = Future()
                self._enqueue(("flush", None, completion))
                self.pending = completion
                self.buffered = 0
            return
        if not self.frames:
            return
        if self.pending is not None:
            # Bound memory to one encoding batch plus the current capture batch.
            self.pending.result()
        frames = self.frames
        self.frames = []
        self._encode_batch(frames)

    def _enqueue(self, item):
        while True:
            if self.worker.done():
                self.worker.result()
                raise RuntimeError("Video worker has stopped")
            try:
                self.queue.put(item, timeout=0.1)
                if self.failed or self.worker.done():
                    self.worker.result()
                return
            except Full:
                continue

    def _consume(self):
        frames = []
        completion = None
        try:
            while True:
                kind, frame, completion = self.queue.get()
                if kind == "stop":
                    return
                if kind == "frame":
                    frames.append(frame)
                if len(frames) == self.batch_size or kind == "flush":
                    if frames:
                        self._encode_batch(frames)
                        frames = []
                    if completion is not None:
                        completion.set_result(None)
        except BaseException as exc:
            self.failed = True
            if completion is not None:
                completion.set_exception(exc)
            while True:
                try:
                    _, _, waiting = self.queue.get_nowait()
                except Empty:
                    break
                if waiting is not None:
                    waiting.set_exception(exc)
            raise

    def _encode_batch(self, frames):
        try:
            self._flush(frames)
        except BaseException:
            self.failed = True
            raise

    def _flush(self, frames):
        height, width = self.shape[:2]
        logger.debug("Encoding %d video frames (%dx%d)", len(frames), width, height)
        command = [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "rawvideo", "-pixel_format", "bgr24",
                "-video_size", f"{width}x{height}", "-framerate", str(self.fps),
                "-i", "pipe:0", "-frames:v", str(len(frames)), "-an", "-c:v", "libx265",
                "-preset", "fast", "-crf", str(self.crf),
                "-maxrate", self.maxrate, "-bufsize", self.bufsize, "-tag:v", "hvc1",
                "-bf", "0", "-g", str(self.batch_size), "-x265-params", "scenecut=0",
                # x265 requires even dimensions of at least 16 pixels.
                "-vf", "pad='max(16,ceil(iw/2)*2)':'max(16,ceil(ih/2)*2)'",
                "-pix_fmt", "yuv420p",
                "-video_track_timescale", str(self.fps.numerator),
                "-movflags", "+empty_moov+default_base_moof+frag_keyframe",
                "-f", "mp4", "pipe:1",
            ]
        result = self._run_encoder(command, frames)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg error: {result.stderr.decode(errors='replace').strip()}")
        fragment, sequence = self.fragment(result.stdout)
        # Copy compressed video only, never intermediate images. The published
        # file stays unchanged until the new fragment has been synced to disk.
        fd, name = tempfile.mkstemp(prefix=".timelapse-", suffix=".mp4", dir=self.output.parent)
        pending = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                if self.count:
                    with self.output.open("rb") as previous:
                        shutil.copyfileobj(previous, stream)
                stream.write(fragment)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, self.output)
            directory_fd = os.open(self.output.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            pending.unlink(missing_ok=True)
        self.count += len(frames)
        self.sequence = sequence
        logger.info(
            "Video updated: %s (%d frames, %.2f seconds)",
            self.output.resolve(), self.count, self.count / float(self.fps),
        )

    @staticmethod
    def _run_encoder(command, frames):
        """Multiplex FFmpeg pipes in the video worker, without a feeder thread."""
        buffers = (memoryview(frame).cast("B") for frame in frames)
        current = next(buffers, None)
        output, errors = [], []
        with subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        ) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    for stream in (process.stdin, process.stdout, process.stderr):
                        os.set_blocking(stream.fileno(), False)
                    if current is None:
                        process.stdin.close()
                    else:
                        selector.register(process.stdin, selectors.EVENT_WRITE)
                    selector.register(process.stdout, selectors.EVENT_READ, output)
                    selector.register(process.stderr, selectors.EVENT_READ, errors)
                    while selector.get_map():
                        for key, _ in selector.select():
                            stream = key.fileobj
                            if stream is process.stdin:
                                try:
                                    written = os.write(stream.fileno(), current[:65536])
                                    current = current[written:]
                                except BrokenPipeError:
                                    current = None
                                except BlockingIOError:
                                    continue
                                if current is not None and not current:
                                    current = next(buffers, None)
                                if current is None:
                                    selector.unregister(stream)
                                    stream.close()
                            else:
                                try:
                                    data = os.read(stream.fileno(), 65536)
                                except BlockingIOError:
                                    continue
                                if data:
                                    key.data.append(data)
                                else:
                                    selector.unregister(stream)
                                    stream.close()
                return subprocess.CompletedProcess(
                    command, process.wait(), b"".join(output), b"".join(errors),
                )
            except BaseException:
                process.kill()
                process.wait()
                raise

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                if not self.failed:
                    self.flush()
                if self.pending is not None:
                    self.pending.result()
            finally:
                if self.executor is not None:
                    try:
                        if not self.worker.done():
                            self._enqueue(("stop", None, None))
                        self.worker.result()
                    finally:
                        self.executor.shutdown(wait=True)
