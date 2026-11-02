# HSR ADB Framework — Agent Context

## Overview

This is a minimal, self-contained framework for **observing** (capturing frames) and **issuing commands** (touch/input) to an Android emulator via ADB. It was built for Honkai: Star Rail (HSR) autonomous exploration but is emulator-agnostic.

**Architecture:** A Windows-side HTTP sidecar server runs on the host, captures screenshots from the emulator via `adb screencap`, and dispatches touch events via `adb shell input`. An orchestrator (Python client) talks to the sidecar over HTTP.

```
┌─────────────┐     HTTP      ┌──────────────┐     ADB      ┌──────────────┐
│  Orchestrator│ ◄──────────► │  server.py   │ ◄──────────► │  Emulator    │
│  (Python)    │   :8765       │  (uvicorn)   │  adb shell   │  (MuMu/etc.) │
└─────────────┘               └──────────────┘              └──────────────┘
                                    │
                          ┌─────────┴─────────┐
                          │                   │
                    capture frames      inject touch
                    (screencap)         (motion_event)
```

## Files

| File | Role | Must Read |
|------|------|-----------|
| `server.py` | HTTP API server — the core of the framework | **Yes** |
| `adb.py` | ADB wrapper — hold backends, capture primitives, joystick | **Yes** |
| `capture.py` | Background screencap loop — keeps latest frame in memory | Yes |
| `imaging.py` | PNG/JPEG encode/decode using OpenCV | Yes |
| `sidecar_client.py` | HTTP client for the sidecar | **Yes** |
| `configs.py` | TOML config loader with typed dataclasses | Yes |
| `sidecar.toml` | Sidecar configuration | Yes |
| `launch.py` | One-command launcher (starts sidecar subprocess) | No |
| `platform-tools/adb.exe` | Android Debug Bridge executable | No |
| `README.md` | User-facing documentation | No |
| `pyproject.toml` | Package metadata and dependencies | No |

## Architecture

### Server (`server.py`)

FastAPI application with these endpoints:

#### `GET /health`
Returns sidecar health, frame stats, and hold backend status.

**Response:**
```json
{
  "ok": true,
  "version": "0.1.0",
  "uptime_s": 123.456,
  "actions_dispatched": 42,
  "last_frame_ms": 1700000000000,
  "hold_active": false,
  "hold_backend": "motion_event",
  "capture": {
    "running": true,
    "target_hz": 6.0,
    "frame_format": "jpeg",
    "frames_captured": 720,
    "effective_hz": 5.95,
    "last_frame_age_ms": 297.0
  }
}
```

#### `GET /frame`
Returns the latest captured frame as JPEG bytes.

**Headers:**
- `X-Frame-Seq`: Frame sequence number (monotonically increasing)
- `X-Content-Format`: MIME type (`image/jpeg` or `image/png`)
- `X-Frame-Width`: Frame width in pixels
- `X-Frame-Height`: Frame height in pixels
- `X-Capture-Epoch-Ms`: Capture timestamp (ms since epoch)
- `X-Response-Epoch-Ms`: Response timestamp (ms since epoch)
- `X-Server-Age-Ms`: Age of frame at response time (ms)
- `X-Capture-Duration-Ms`: Time taken to capture (ms)
- `Cache-Control`: `no-store`

**Status codes:** 200 (success), 503 (no frame yet)

#### `POST /action`
Dispatches an action through ADB. All actions are serialized through a single lock — a hold must not interleave with gestures.

**Request body** (discriminated on `type`):

| Type | Fields | Description |
|------|--------|-------------|
| `move_vector` | `dir_deg`, `duration_ms`, `magnitude` | Push joystick in direction for duration |
| `hold` | `dir_deg`, `magnitude` | Start persistent joystick hold |
| `release` | _(none)_ | End persistent joystick hold |
| `camera` | `delta_deg`, `duration_ms` | Drag camera horizontally |
| `tap_xy` | `x`, `y` | Tap framebuffer pixel |
| `tap` | `x`, `y`, `duration_ms` | Legacy tap (with hold duration) |
| `swipe` | `x`, `y`, `x2`, `y2`, `duration_ms` | Swipe gesture |
| `back` | _(none)_ | Press Android back button |
| `wait` | `duration_ms` | Sleep |

**Response:**
```json
{
  "ok": true,
  "type": "move_vector",
  "dispatched_epoch_ms": 1700000000000,
  "dispatch_ms": 523.4,
  "frame_seq_at_dispatch": 720,
  "error": null
}
```

#### `GET /frame/meta`
Returns frame metadata only (no image data). Useful for checking freshness before fetching the full frame.

#### `GET /stats`
Returns capture health and action dispatch count.

### ADB Layer (`adb.py`)

Three hold backends for persistent touch (joystick):

| Backend | Method | Reliability | Notes |
|---------|--------|-------------|-------|
| `motion_event` | `input motionevent DOWN/MOVE/UP` | **Best** | Android 11+, auto-selected when available |
| `sendevent` | Raw `/dev/input/event*` writes | Good | Needs touchscreen node discovery, root-like permissions |
| `swipe_repeat` | Repeated `input swipe x y x y` | Fair | Always works but finger lifts between gestures |

**Auto-selection:** Tries `motion_event` first (probes `input --help`), then `sendevent` (discovers touchscreen node via `getevent -pl`), falls back to `swipe_repeat`.

**Key methods on `AdbClient`:**
- `shell(*args)` — Run arbitrary adb shell command
- `screencap_png()` — Capture PNG (reliable, ~315ms on MuMu)
- `tap(x, y, duration_ms)` — Quick tap
- `swipe(x1, y1, x2, y2, duration_ms)` — Swipe gesture
- `camera_drag(x1, y1, x2, y2, duration_ms)` — Horizontal drag for camera
- `back()` — Press back button
- `make_hold_backend(kind)` — Get hold backend by name
- `touch_down(x, y)`, `touch_move(x, y)`, `touch_up(x, y)` — Persistent hold using auto-selected backend

### Capture Loop (`capture.py`)

Background thread that captures frames at a target rate (default 6 Hz). Keeps the latest frame in memory. Readers get whatever was last captured plus timing metadata to decide if it's too old.

**Timestamps are conservative:** `capture_epoch_ms` is taken *before* the screencap call starts, so computed age is an upper bound. This ensures stale frames are rejected early, not late.

**Frame formats:**
- `jpeg`: Raw framebuffer → decode on host → encode as JPEG (~132ms adb + ~10ms encode = ~142ms total)
- `png`: Emulator compresses on-device (~315ms, caps loop at ~3 Hz)

### Imaging (`imaging.py`)

OpenCV-based image processing:
- `decode_screencap_raw(png_bytes)` → BGR numpy array (H, W, 3), uint8
- `encode_jpeg(bgr, quality)` → JPEG bytes
- `decode_jpeg(jpeg_bytes)` → BGR numpy array
- `grayscale(frame)` → Single-channel grayscale
- `resize(frame, width, height)` → Resized frame

All functions return BGR format (OpenCV's native format). Frames from `GET /frame` are JPEG-encoded and must be decoded with `decode_jpeg()` before OpenCV processing.

### Client (`sidecar_client.py`)

HTTP client with structured error types:
- `SidecarDown` — Connection refused / timeout
- `CaptureStalled` — No frame within expected window
- `InputNotAcknowledged` — POST /action returned ok:false

**Key methods:**
- `get_frame()` → `Frame` dataclass (bytes, seq, width, height, timestamps)
- `get_frame_meta()` → dict of frame metadata
- `post_action(action_dict)` → `ActionResult` dataclass
- `move_vector(dir_deg, duration_ms, magnitude)` → convenience wrapper
- `hold(dir_deg, magnitude)` → start persistent hold
- `release()` → end persistent hold
- `camera(delta_deg, duration_ms)` → turn camera
- `tap_xy(x, y)` → tap pixel
- `get_health()` → `HealthStatus` dataclass

**Retry behavior:** 2 retries with exponential backoff (0.5s, 1.0s) on connection errors only. Logical failures (`InputNotAcknowledged`) are not retried.

## Coordinate System

### Direction angles
- **0°** = up/forward
- Angles increase **counter-clockwise on screen**
- Screen y grows downward (standard image coordinates)

### Joystick point calculation
```python
radians = math.radians(dir_deg % 360.0)
x = center[0] - radius * magnitude * math.sin(radians)
y = center[1] - radius * magnitude * math.cos(radians)
```

| dir_deg | Result |
|---------|--------|
| 0 | (cx, cy - r) — up |
| 90 | (cx - r, cy) — left |
| 180 | (cx, cy + r) — down |
| 270 | (cx + r, cy) — right |

### Camera drag
Positive delta = "look right". The game's camera follows the finger inversely, so looking right is a right-to-left drag. Centered on mid-screen.

## Configuration

### `sidecar.toml`

```toml
[sidecar]
host = "0.0.0.0"          # Bind address
port = 8765               # HTTP port
target_hz = 6             # Capture rate (6 Hz measured, ~5.95 effective)
frame_format = "jpeg"     # jpeg or png
jpeg_quality = 80         # JPEG quality 1-100
adb_path = ""             # Auto-detect MuMu adb binary
serial = ""               # Auto-detect connected device

[joystick]
center = [215, 545]       # Virtual joystick center (x, y) in framebuffer pixels
radius = 105              # Joystick radius in pixels
camera_px_per_deg = 4.0   # Camera sensitivity (pixels per degree)
hold_backend = "auto"     # auto | motion_event | sendevent | swipe_repeat
```

### Config loading (`configs.py`)

`AppConfig.load(config_dir)` loads `mapping.toml` and `sidecar.toml`, merges them (mapping.toml takes precedence), and returns typed dataclasses:
- `config.sidecar` — host, port
- `config.emulator` — kind, resolution
- `config.mapping` — all mapping parameters
- `config.recovery` — reset app path, checkpoint resume

## Usage Patterns

### Start the sidecar
```powershell
python3 launch.py
# Or directly:
python3 -m server --config sidecar.toml
```

### Capture a frame and process it
```python
from sidecar_client import SidecarClient
from imaging import decode_jpeg

client = SidecarClient("127.0.0.1", 8765)
frame = client.get_frame()
bgr = decode_jpeg(frame.data)  # numpy array (H, W, 3), uint8
```

### Move joystick forward for 2 seconds
```python
result = client.move_vector(dir_deg=0, duration_ms=2000, magnitude=1.0)
assert result.ok
```

### Persistent hold + move + release
```python
client.hold(dir_deg=0, magnitude=1.0)   # Push joystick forward
time.sleep(1)
client.release()                         # Release joystick
```

### Check health before acting
```python
health = client.get_health()
if not health.ok:
    raise RuntimeError("Sidecar unhealthy")
if health.capture["last_frame_age_ms"] > 2000:
    raise RuntimeError("Frame too stale")
```

## Important Notes

### Frame age matters
Always check `frame.age_ms` or `health.capture.last_frame_age_ms`. Frames older than ~2 seconds may be stale for real-time control. The sidecar's timestamps are conservative (upper bound on true age).

### Hold backend selection
The auto-selected backend is cached per process. On MuMuPlayer, `motion_event` is typically available and preferred. If the game doesn't respond to touch, try explicitly setting `hold_backend = "swipe_repeat"` in `sidecar.toml`.

### Joystick coordinates are emulator-specific
The default `[215, 545]` center works for HSR on MuMuPlayer at 1280×720. Different games/emulators/resolutions need different values. Verify by tapping the joystick area and checking if the character moves.

### Camera settling
After a `move_vector`, the in-game camera takes time to settle (~5 seconds for HSR's third-person camera). Capturing frames during this transition produces noisy VO data. Always wait for settling before processing frames after movement.

### Action serialization
All actions go through a single lock. A `hold` blocks other actions until `release` is called. `move_vector` cannot run while a hold is active (same injected pointer).

### Error handling
- `SidecarDown` → Check if sidecar is running, verify port/host
- `CaptureStalled` → Sidecar hasn't captured a frame yet (startup) or adb connection lost
- `InputNotAcknowledged` → Logical failure (e.g., hold already active), don't retry same payload

### Performance characteristics
- Capture loop: ~6 Hz target, ~5.95 Hz effective with p95 age of 297ms
- `move_vector` dispatch: ~500-1000ms (includes the sleep for duration_ms)
- Frame size: ~120-130 KB JPEG at quality 80, 1280×720

## Dependencies

Required Python packages:
- `numpy` — Array operations
- `opencv-python` — Image decode/encode
- `httpx` — HTTP client
- `pydantic` — Request/response validation
- `fastapi` + `uvicorn` — HTTP server
- `scipy`, `networkx` — For mapping/exploration (optional if only using core layer)

Python version: **3.10+** (uses `slots=True` in dataclasses)
