"""Command-line validation, defaults, and logging tests."""

import contextlib
import io
import os
import re
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from tlapse import cli
from tests.helpers import run_timelapse


class CliTests(unittest.TestCase):
    def setUp(self):
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("TLAPSE_")}
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_environment_parameters(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "TLAPSE_URL": "rtsp://example/environment",
            "TLAPSE_DURATION": "inf", "TLAPSE_SPEEDUP": "24x",
            "TLAPSE_AVG_FRAMES": "8", "TLAPSE_MODE": "images",
            "TLAPSE_FPS": "30", "TLAPSE_CRF": "20", "TLAPSE_MAXRATE": "8M",
            "TLAPSE_OUTPUT": directory,
            "TLAPSE_LOG_LEVEL": "debug",
        }):
            args = cli.parse_args([])
            for name, expected in {
                "url": "rtsp://example/environment", "duration": float("inf"),
                "speedup": 24, "frames": 8, "mode": "images", "fps": 30,
                "crf": 20, "maxrate": "8M", "output": Path(directory),
                "log_level": "DEBUG", "interval": 24 / 30,
            }.items():
                with self.subTest(name=name):
                    self.assertEqual(getattr(args, name), expected)

    def test_cli_overrides_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "TLAPSE_URL": "rtsp://example/environment", "TLAPSE_DURATION": "invalid",
            "TLAPSE_SPEEDUP": "invalid", "TLAPSE_AVG_FRAMES": "invalid",
            "TLAPSE_MODE": "invalid", "TLAPSE_FPS": "invalid",
            "TLAPSE_CRF": "invalid", "TLAPSE_MAXRATE": "invalid",
            "TLAPSE_OUTPUT": "/nonexistent", "TLAPSE_LOG_LEVEL": "invalid",
        }):
            args = cli.parse_args([
                "--url", "rtsp://example/cli", "--duration", "10",
                "--speedup", "12x", "--average-frames", "2", "--mode", "images",
                "--fps", "24", "--crf", "22", "--maxrate", "4M",
                "--output", directory, "--log-level", "warning",
            ])
            for name, expected in {
                "url": "rtsp://example/cli", "duration": 10, "speedup": 12,
                "frames": 2, "mode": "images", "fps": 24, "crf": 22,
                "output": Path(directory), "log_level": "WARNING", "maxrate": "4M",
            }.items():
                with self.subTest(name=name):
                    self.assertEqual(getattr(args, name), expected)

    def test_invalid_environment_parameters(self):
        for variable, value in (
            ("DURATION", "invalid"), ("DURATION", "0"), ("SPEEDUP", "nan"),
            ("AVG_FRAMES", "1.5"), ("AVG_FRAMES", "0"), ("FPS", "0"),
            ("CRF", "52"), ("MODE", "invalid"), ("MAXRATE", "0M"),
            ("MAXRATE", "invalid"),
            ("LOG_LEVEL", "invalid"), ("OUTPUT", "/nonexistent"),
        ):
            with self.subTest(variable=variable, value=value), \
                    patch.dict(os.environ, {"TLAPSE_" + variable: value}), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    cli.parse_args(["--url", "rtsp://example"])
                self.assertEqual(exc.exception.code, 2)

    def test_url_is_required(self):
        for environment in ({}, {"TLAPSE_URL": ""}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    cli.parse_args([])
                self.assertEqual(exc.exception.code, 2)

    def test_defaults(self):
        args = cli.parse_args(["--url", "rtsp://example/stream"])
        self.assertEqual(args.duration, 60)
        self.assertEqual(args.interval, 1)
        self.assertEqual(args.speedup, 25)
        self.assertEqual(args.frames, 16)
        self.assertEqual(args.fps, 25)
        self.assertEqual(args.crf, 26)
        self.assertEqual(args.maxrate, "6M")
        self.assertFalse(hasattr(args, "rotation_hours"))
        self.assertEqual(args.mode, "video")
        self.assertEqual(args.output, Path("./output"))

    def test_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            args = cli.parse_args(["--url", "rtsp://example", "--output", directory])
            self.assertEqual(args.output, Path(directory))

    def test_compose_environment_is_valid(self):
        compose = (Path(__file__).resolve().parents[1] / "compose.yaml").read_text()
        values = dict(re.findall(r'^      TLAPSE_(\w+): "?([^"\n]+)"?$', compose, re.MULTILINE))
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"TLAPSE_" + key: value for key, value in values.items()},
        ):
            args = cli.parse_args(["--output", directory])
        self.assertEqual(args.url, values["URL"])
        for variable, attribute in (
            ("MODE", "mode"),
            ("SPEEDUP", "speedup"), ("AVG_FRAMES", "frames"), ("FPS", "fps"),
            ("CRF", "crf"), ("MAXRATE", "maxrate"), ("LOG_LEVEL", "log_level"),
        ):
            with self.subTest(variable=variable):
                actual = getattr(args, attribute)
                self.assertEqual(type(actual)(values[variable]), actual)
        self.assertEqual(values["DURATION"], "inf")
        self.assertEqual(values["OUTPUT"], "/data")
        self.assertNotIn("${", compose)
        self.assertEqual((Path(__file__).resolve().parents[1] / ".env").read_text(), "")

    def test_docker_does_not_default_to_help(self):
        dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
        self.assertIn("CMD []", dockerfile)

    def test_speedup_derives_interval_from_fps(self):
        for speedup in ("24", "24x", "24X"):
            for fps in (25, 30):
                with self.subTest(speedup=speedup, fps=fps):
                    args = cli.parse_args([
                        "--url", "rtsp://example", "--speedup", speedup, "--fps", str(fps),
                    ])
                    self.assertEqual(args.speedup, 24)
                    self.assertEqual(args.interval, 24 / fps)
                    self.assertAlmostEqual(86400 / args.interval / fps, 3600)

    def test_output_rejects_file_and_missing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "file.mp4"
            file.touch()
            for output in (file, Path(directory) / "missing"):
                with self.subTest(output=output), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        cli.parse_args(["--url", "rtsp://example", "--output", str(output)])

    def test_custom_video_parameters(self):
        args = cli.parse_args([
            "--url", "rtsp://example/stream", "--crf", "20",
        ])
        self.assertEqual(args.crf, 20)

    def test_maxrate_units(self):
        for rate in ("6000000", "6000k", "6000K", "6M", "2.5M", "1G"):
            with self.subTest(rate=rate):
                args = cli.parse_args(["--url", "rtsp://example", "--maxrate", rate])
                self.assertEqual(args.maxrate, rate)

    def test_rotation_option_is_removed(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.parse_args([
                    "--url", "rtsp://example/stream", "--mode", "video",
                    "--rotation-hours", "24",
                ])

    def test_obsolete_rotation_environment_is_ignored(self):
        with patch.dict(os.environ, {"TLAPSE_ROTATION_HOURS": "invalid"}):
            args = cli.parse_args(["--url", "rtsp://example/stream"])
        self.assertFalse(hasattr(args, "rotation_hours"))

    def test_default_logging_omits_frame_details(self):
        with self.assertLogs("tlapse", level="DEBUG") as logs:
            result, _ = run_timelapse("5")
        self.assertEqual(result, 0)
        self.assertTrue(all(record.levelname == "INFO" for record in logs.records))
        self.assertIn("Starting timelapse", logs.output[0])
        for parameter in ("url=<redacted>", "mode=images", "output=", "duration=5.0 s",
                          "speedup=50.0x", "average_frames=2", "fps=25.0", "crf=26", "maxrate=6M",
                          "rotation=daily-at-local-midnight", "log_level=INFO"):
            self.assertIn(parameter, logs.output[0])
        self.assertNotIn("interval=", logs.output[0])
        self.assertIn("Timelapse completed: 2 output frames saved", logs.output[-1])
        self.assertNotIn("rtsp://example", "\n".join(logs.output))

    def test_debug_logging_includes_frame_details(self):
        with self.assertLogs("tlapse", level="DEBUG") as logs:
            result, _ = run_timelapse("5", log_level="debug")
        self.assertEqual(result, 0)
        self.assertTrue(any(record.levelname == "DEBUG" for record in logs.records))
        self.assertIn("Updated averaging window", "\n".join(logs.output))
        self.assertIn("Output frame 1 saved", "\n".join(logs.output))

    def test_invalid_parameters(self):
        for option, value in [("--duration", "nan"), ("--duration", "0"),
                              ("--speedup", "inf"), ("--speedup", "0"),
                              ("--speedup", "nan"), ("--speedup", "-1"),
                              ("--speedup", "invalid"), ("--speedup", "0x"),
                              ("--frames", "0"), ("--fps", "0"),
                              ("--fps", "nan"), ("--crf", "-1"),
                              ("--crf", "52"), ("--crf", "1.5"),
                              ("--maxrate", "0"), ("--maxrate", "-6M"),
                              ("--maxrate", "nan"), ("--maxrate", "inf"),
                              ("--maxrate", "6MB"), ("--maxrate", ""),
                              ("--maxrate", "0.1"), ("--maxrate", "9" * 400)]:
            with self.subTest(option=option, value=value), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    cli.parse_args(["--url", "rtsp://example", option, value])
                self.assertEqual(exc.exception.code, 2)
