# HSR ADB Framework — Minimal Emulator Communication Layer

Minimal set of files for observing (capturing frames) and issuing commands
(touch/input) to an Android emulator via ADB.

## Files

| File | Role |
|------|------|
| `server.py` | HTTP API server (receives commands, dispatches via ADB) |
| `adb.py` | ADB wrapper: hold backends, capture, joystick primitives |
| `capture.py` | Frame capture controller (adb screencap loop) |
| `imaging.py` | PNG ↔ BGR encode/decode |
| `sidecar_client.py` | HTTP client for the sidecar server |
| `configs.py` | TOML config loader |
| `sidecar.toml` | Sidecar configuration |
| `launch.py` | One-command launcher (starts sidecar + runs mapper/calibration) |
| `platform-tools/adb.exe` | Android Debug Bridge executable |
| `platform-tools/fastboot.exe` | Fastboot utility |

## Setup

### 1. Start the ADB server and connect MuMuPlayer

```powershell
# Start ADB server
.\platform-tools\adb.exe start-server

# Connect to MuMuPlayer emulator at 127.0.0.1:7555
.\platform-tools\adb.exe connect 127.0.0.1:7555

# Verify connection
.\platform-tools\adb.exe devices
```

Expected output:
```
List of devices attached
127.0.0.1:7555    device
```

### 2. Start the sidecar server

```powershell
python3 server.py --config sidecar.toml
```

Or use the launcher:
```powershell
python3 launch.py
```

### 3. Verify connectivity

```powershell
python3 -c "
from sidecar_client import SidecarClient
client = SidecarClient(host='127.0.0.1', port=8765)
health = client.get_health()
print('Sidecar OK:', health.ok)
print('Frames captured:', health.capture.frames_captured)
print('Hold backend:', health.hold_backend)
"
```

## Configuration

Edit `sidecar.toml` to match your setup:

```toml
[sidecar]
host = "127.0.0.1"
port = 8765
hold_backend = "auto"           # auto|motion_event|sendevent|swipe_repeat
joystick_center = [215, 545]    # virtual joystick center (x, y)
joystick_radius = 105           # joystick radius in pixels
camera_px_per_deg = 4.0         # camera sensitivity (pixels per degree)
capture_target_hz = 6.0         # frame capture rate
capture_jpeg_quality = 80       # JPEG quality
```

## API

### GET /health
Returns sidecar health, frame stats, and hold backend status.

### GET /frame
Returns the latest captured frame as JPEG bytes.

### POST /action
Executes an action. Body:
```json
{
  "type": "move_vector",
  "dir_deg": 0,
  "duration_ms": 500,
  "magnitude": 1.0
}
```

Supported action types:
- `move_vector` — push virtual joystick
- `tap` / `tap_xy` — single touch press
- `swipe` — swipe gesture
- `camera` — drag camera rotation
- `back` — Android back button
- `hold` / `release` — hold/release touch at fixed point
- `wait` — sleep for duration

## Usage Examples

### Capture a frame
```powershell
python3 -c "
from sidecar_client import SidecarClient
client = SidecarClient('127.0.0.1', 8765)
frame = client.get_frame()
with open('screenshot.jpg', 'wb') as f:
    f.write(frame.data)
print(f'Captured {len(frame.data)} bytes')
"
```

### Push joystick forward for 2 seconds
```powershell
python3 -c "
from sidecar_client import SidecarClient
client = SidecarClient('127.0.0.1', 8765)
result = client.move_vector(dir_deg=0, duration_ms=2000, magnitude=1.0)
print('Move OK:', result.ok)
"
```

### Drag camera left by 30 degrees
```powershell
python3 -c "
from sidecar_client import SidecarClient
client = SidecarClient('127.0.0.1', 8765)
result = client.camera(delta_deg=-30)
print('Camera OK:', result.ok)
"
```

## Architecture

```
┌─────────────┐     HTTP      ┌──────────────┐     ADB      ┌──────────────┐
│  Client     │ ◄──────────► │  server.py   │ ◄──────────► │  MuMuPlayer  │
│ (Python)    │   :8765       │  (uvicorn)   │  adb shell   │  (127.0.0.1) │
└─────────────┘               └──────────────┘              └──────────────┘
                                    │
                          ┌─────────┴─────────┐
                          │                   │
                    capture frames      inject touch
                    (screencap)         (motion_event)
```
