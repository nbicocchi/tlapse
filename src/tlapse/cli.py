"""Command-line arguments and logging configuration for RTSP timelapses."""

import argparse
import logging
import math
import os
import re
from pathlib import Path
import shutil

from tlapse.capture import run


def speedup_value(value):
    """Accept a numeric speed multiplier with an optional x suffix."""
    try:
        return float(value.lower().removesuffix("x"))
    except ValueError:
        raise argparse.ArgumentTypeError("speedup must be a number, optionally followed by x")


def maxrate_value(value):
    """Validate a positive bitrate using decimal FFmpeg unit suffixes."""
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?[kKMG]?", value):
        raise argparse.ArgumentTypeError("maxrate must be a positive bitrate, e.g. 6000000 or 6M")
    number = value[:-1] if value[-1] in "kKMG" else value
    rate = float(number) * {"k": 1000, "K": 1000, "M": 1000000,
                            "G": 1000000000}.get(value[-1], 1)
    if not math.isfinite(rate) or rate < 1:
        raise argparse.ArgumentTypeError("maxrate must be finite and at least 1 bit/s")
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="tlapse",
        description="Create an RTSP timelapse as timestamped PNG images or an MP4 video.",
        epilog="Options also accept TLAPSE_<OPTION> environment variables (hyphens become "
               "underscores; --frames uses TLAPSE_AVG_FRAMES). "
               "Command-line values override environment values.",
    )
    parser.add_argument("--url", help="RTSP stream URL (required via --url or TLAPSE_URL)")
    parser.add_argument(
        "--duration", type=float, default=60,
        help="Capture duration in seconds, or inf to run indefinitely (default: 60)",
    )
    parser.add_argument(
        "--speedup", type=speedup_value, default=25,
        help="Speed multiplier, e.g. 24 or 24x; capture interval = speedup / fps (default: 25)",
    )
    parser.add_argument(
        "--frames", "--average-frames", type=int, default=16,
        help="Rolling window size: average the latest source frames at each sample (default: 16)",
    )
    parser.add_argument(
        "--mode", choices=("images", "video"), default="video",
        help="Save PNG images or MP4 videos (default: video)",
    )
    parser.add_argument(
        "--fps", type=float, default=25,
        help="Playback frames per second for the output video (default: 25)",
    )
    parser.add_argument(
        "--crf", type=int, default=26,
        help="HEVC quality from 0 to 51; lower is higher quality (default: 26)",
    )
    parser.add_argument(
        "--maxrate", type=maxrate_value, default="6M",
        help="Maximum HEVC bitrate in bits/s, optionally suffixed k, K, M or G (default: 6M)",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("./output"),
        help="Existing output directory (default: ./output)",
    )
    parser.add_argument(
        "--log-level", type=str.upper,
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"), default="INFO",
        help="Logging verbosity (default: INFO)",
    )
    # Parse explicit CLI values first, so even invalid environment values can be
    # overridden. Suppress defaults to distinguish explicit options from defaults.
    defaults = {}
    for action in parser._actions:
        if action.dest != "help":
            defaults[action.dest] = action.default
            action.default = argparse.SUPPRESS
    args = parser.parse_args(argv)
    for action in parser._actions:
        if action.dest not in defaults or hasattr(args, action.dest):
            continue
        variable = "TLAPSE_" + ("AVG_FRAMES" if action.dest == "frames" else action.dest.upper())
        value = os.environ.get(variable, defaults[action.dest])
        if isinstance(value, str):
            try:
                value = action.type(value) if action.type else value
            except (ValueError, TypeError, argparse.ArgumentTypeError):
                parser.error(f"{variable}: invalid value {value!r}")
        if action.choices is not None and value not in action.choices:
            parser.error(f"{variable}: invalid choice {value!r} (choose from {', '.join(action.choices)})")
        setattr(args, action.dest, value)
    if not args.url:
        parser.error("--url or TLAPSE_URL is required")
    if args.frames <= 0:
        parser.error("--frames must be greater than zero")
    if math.isnan(args.duration) or args.duration <= 0:
        parser.error("--duration must be greater than zero or inf")
    if not math.isfinite(args.speedup) or args.speedup <= 0:
        parser.error("--speedup must be a finite number greater than zero")
    if not math.isfinite(args.fps) or args.fps <= 0:
        parser.error("--fps must be a finite number greater than zero")
    args.interval = args.speedup / args.fps
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("--speedup / --fps must yield a finite interval greater than zero")
    if not 0 <= args.crf <= 51:
        parser.error("--crf must be between 0 and 51")
    if not args.output.is_dir():
        parser.error("--output must be an existing directory")
    if args.mode == "video" and shutil.which("ffmpeg") is None:
        parser.error("Video mode requires ffmpeg to be installed on the system")
    return args


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("tlapse").setLevel(args.log_level)
    return run(args)
