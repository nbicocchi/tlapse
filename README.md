# tlapse

Create timelapses from an RTSP camera as HEVC MP4 videos or PNG sequences. Supports rolling-window averaging, configurable speedup, daily video rotation, and atomic video updates.

## Local installation

Requires **Python 3.10+** and **FFmpeg with `libx265`**. On Debian/Ubuntu:

```bash
sudo apt install python3 python3-venv python3-pip ffmpeg
```

Clone the project, create a virtual environment, and install the application and its dependencies:

```bash
git clone https://github.com/nbicocchi/tlapse.git
cd tlapse
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
mkdir -p output
```

The installation provides the `tlapse` command. In each new terminal, activate the environment with `source .venv/bin/activate`, or invoke `.venv/bin/tlapse` directly from the project directory.

Record 60 seconds of activity at 24x speedup, producing approximately 2.5 seconds of video:

```bash
tlapse --url "rtsp://camera-address:8554/stream" \
  --output output --duration 60 --speedup 24 --fps 24 \
  --frames 16 --crf 26 --maxrate 6M
```

- Use `--duration inf` for continuous recording, or `--mode images` for PNG output.
- Press **Ctrl+C** to stop and save pending video frames.
- Run `tlapse --help` for available options; `python -m tlapse` is also supported.
- Run `deactivate` to leave the virtual environment.

## Container usage

Docker includes all runtime dependencies; no host Python installation or venv is needed. From the project directory:

### Docker Compose

Create `output`, then edit `compose.yaml`: set `TLAPSE_URL`, capture settings, and `user` to your host UID/GID (`id -u`, `id -g`). The bind mount maps `./output` to `/data`.

```bash
mkdir -p output
docker compose up -d --build
docker compose logs -f tlapse
```

Exit the log viewer with Ctrl+C; the container keeps running. Stop gracefully with:

```bash
docker compose stop
```

For a foreground run, use `docker compose up --build --menu=false` and stop with Ctrl+C. Omit `--menu=false` if unsupported. After source changes, rebuild the image; configuration changes require `docker compose up`, not just `restart`.

Override settings for a one-off run:

```bash
docker compose run --rm -e TLAPSE_DURATION=60 -e TLAPSE_MAXRATE=8M tlapse
```

Set `TZ` in Compose (for example, `Europe/Rome`) to choose the timezone used for filenames and daily rotation. The camera must be reachable from the container.

### Docker without Compose

```bash
docker build -t tlapse:local .
mkdir -p output
docker run --rm -it \
  --user "$(id -u):$(id -g)" --stop-timeout 120 \
  -v "$PWD/output:/data" tlapse:local \
  --url "rtsp://camera-address:8554/stream" \
  --output /data --duration 60 --speedup 24 --fps 24
```

## Configuration

Precedence: **CLI > environment > defaults**. The application does not load `.env` files; Compose settings are defined directly in `compose.yaml`. Do not commit camera credentials.

| Option | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `--url` | `TLAPSE_URL` | Required | RTSP camera URL; capture uses TCP. |
| `--duration` | `TLAPSE_DURATION` | `60` | Capture seconds; `inf` runs indefinitely. |
| `--speedup` | `TLAPSE_SPEEDUP` | `25` | Positive playback speed multiplier; `24x` is also accepted. |
| `--frames`, `--average-frames` | `TLAPSE_AVG_FRAMES` | `16` | Number of latest source frames to average; `1` disables averaging. |
| `--mode` | `TLAPSE_MODE` | `video` | `video` or `images`. |
| `--fps` | `TLAPSE_FPS` | `25` | Output frame rate. Sampling interval = speedup / FPS. |
| `--crf` | `TLAPSE_CRF` | `26` | HEVC quality, 0–51; lower means higher quality. |
| `--maxrate` | `TLAPSE_MAXRATE` | `6M` | Video bitrate limit; accepts bits/s or suffixes `k`, `K`, `M`, `G`. VBV buffer = twice this value; short peaks may exceed the limit. |
| `--output` | `TLAPSE_OUTPUT` | `./output` | Existing output directory. |
| `--log-level` | `TLAPSE_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |

Example using environment variables:

```bash
TLAPSE_URL="rtsp://camera-address:8554/stream" TLAPSE_MAXRATE=6M tlapse --duration 60
```

## Processing and safety

- **Three application threads:** receive frames and update the rolling window; compute averages at sampling time; consume the bounded queue and encode/save video. Averaging does not block camera reads.
- **Encoding:** HEVC, preset `fast`. Batches use the whole-second FPS multiple nearest 120 frames: 120 at 24 FPS, 125 at 25 FPS. Each batch starts with one I-frame. At 24 FPS and 144x speedup, a full update represents 12 minutes of capture.
- **Timing:** playback duration ≈ capture duration / speedup. Missed samples repeat an available frame to preserve timing. There is no motion interpolation. Ten consecutive invalid reads stop capture; reconnection is not automatic.
- **Output:** `tlapse_video_<timestamp>.mp4` or `tlapse_image_<timestamp>.png`. Videos rotate at local midnight; existing names receive a suffix rather than being overwritten.
- **Safe updates:** compressed video is copied, extended, synced, and atomically replaced. No intermediate images are written in video mode. Ctrl+C/SIGTERM flush partial batches; SIGKILL can lose uncommitted frames and leave temporary files. PNG writes are not atomic.
- **Resources:** RAM holds the rolling window, averaging workspace, and up to one worker batch plus one queued batch. Raw 3632×1632 BGR frames use about 17.8 MB each. Atomic updates temporarily require two copies of the compressed video on disk. If the queue fills, sampling waits while reception continues.
- **Diagnostics:** use `--log-level DEBUG` or `TLAPSE_LOG_LEVEL=DEBUG`. Playback requires HEVC support.

## Tests

With the virtual environment active and the project installed:

```bash
python -m unittest discover -s tests -t . -v
```

The complete suite requires FFmpeg with `libx265` and `ffprobe`.
