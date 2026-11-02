"""Windows-side capture/actuator sidecar.

Two endpoints carry the whole contract:

    GET  /frame   -> image bytes (JPEG by default, PNG on request) + timing
                     headers (X-Frame-Seq, X-Capture-Epoch-Ms, X-Response-Epoch-Ms)
    POST /action  -> dispatch a tap / swipe / back / wait / joystick / camera
                     action through adb

The sidecar holds no game knowledge. It is a remote-controlled camera and finger.

Actions are either *discrete* (tap, swipe, tap_xy, move_vector, camera) or
*stateful* (hold, release). A hold keeps the touchscreen pressed across HTTP
requests, which is why every action is serialized through one lock: a hold must
not be interleaved with another gesture, and a second `hold` is rejected instead
of silently moving the finger.

Run:
    py -m sidecar_windows.server --config configs/sidecar.toml
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal, Union

# Allow `python sidecar_windows/server.py` as well as `python -m sidecar_windows.server`.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from adb import AdbClient, AdbConfig, AdbError, HoldBackend
from capture import CaptureLoop, FrameFormat

VERSION = "0.1.0"

# Coordinate space the orchestrator speaks: the emulator's current framebuffer.
DEFAULT_FRAME_WIDTH = 1280
DEFAULT_FRAME_HEIGHT = 720

# On-screen joystick geometry for the pinned 1280x720 game client. The orchestrator
# speaks direction + magnitude; the sidecar turns that into a screen point.
DEFAULT_JOYSTICK_CENTER: tuple[int, int] = (215, 545)
DEFAULT_JOYSTICK_RADIUS = 105.0
DEFAULT_CAMERA_PX_PER_DEG = 4.0

HOLD_BACKEND_KINDS: tuple[str, ...] = ("auto", "motion_event", "sendevent", "swipe_repeat")


class ActionRequest(BaseModel):
    """Legacy discrete input events (kept byte-for-byte compatible).

    Coordinates are in framebuffer pixels (1280x720 for the pinned HSR client),
    which is also the space `input tap` uses on this emulator build -- verified
    empirically in Phase 0.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["tap", "swipe", "back", "wait"]
    x: int | None = None
    y: int | None = None
    x2: int | None = None
    y2: int | None = None
    duration_ms: int = Field(default=100, ge=0, le=10_000)
    label: str | None = None

    @model_validator(mode="after")
    def _check_fields(self) -> "ActionRequest":
        if self.type == "tap" and (self.x is None or self.y is None):
            raise ValueError("tap requires x and y")
        if self.type == "swipe" and None in (self.x, self.y, self.x2, self.y2):
            raise ValueError("swipe requires x, y, x2, y2")
        for name in ("x", "y", "x2", "y2"):
            value = getattr(self, name)
            if value is not None and not (0 <= value < 8192):
                raise ValueError(f"{name} out of plausible range: {value}")
        return self


class MoveVectorAction(BaseModel):
    """Push the joystick in a direction for a fixed time, then release.

    The client uses this to walk a known distance; direction is in degrees with
    0 = up/forward and angles increasing counter-clockwise on screen.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["move_vector"]
    dir_deg: float = Field(ge=0.0, le=360.0)
    duration_ms: int = Field(ge=100, le=2_000)
    magnitude: float = Field(default=1.0, ge=0.0, le=1.0)


class HoldAction(BaseModel):
    """Start a persistent joystick hold; released later by `release`."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["hold"]
    dir_deg: float = Field(ge=0.0, le=360.0)
    magnitude: float = Field(default=1.0, ge=0.0, le=1.0)


class ReleaseAction(BaseModel):
    """End a persistent joystick hold. Idempotent when nothing is held."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["release"]


class CameraAction(BaseModel):
    """Turn the in-game camera by dragging horizontally across mid-screen."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["camera"]
    delta_deg: float = Field(ge=-180.0, le=180.0)
    duration_ms: int = Field(default=250, ge=100, le=800)


class TapXYAction(BaseModel):
    """Tap a framebuffer pixel without the legacy field-name baggage."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["tap_xy"]
    x: int = Field(ge=0, lt=8192)
    y: int = Field(ge=0, lt=8192)


# Discriminated on `type`, so every payload shape is validated with
# extra="forbid" and unknown keys become a 422 instead of being silently ignored.
Action = Annotated[
    Union[
        ActionRequest,
        MoveVectorAction,
        HoldAction,
        ReleaseAction,
        CameraAction,
        TapXYAction,
    ],
    Field(discriminator="type"),
]


class ActionResponse(BaseModel):
    ok: bool
    type: str
    dispatched_epoch_ms: int
    dispatch_ms: float
    frame_seq_at_dispatch: int | None = None
    error: str | None = None


def joystick_point(
    center: tuple[int, int], radius: float, dir_deg: float, magnitude: float
) -> tuple[int, int]:
    """Map (direction, magnitude) to a framebuffer point.

    0 degrees is up/forward and angles increase counter-clockwise *on screen*,
    so the screen-space offset is ``(-sin, -cos)`` (screen y grows down):

        dir  0 -> (cx,       cy - r)
        dir 90 -> (cx - r,   cy)
        dir 180 -> (cx,      cy + r)
        dir 270 -> (cx + r,  cy)
    """
    radians = math.radians(float(dir_deg) % 360.0)
    x = center[0] - radius * magnitude * math.sin(radians)
    y = center[1] - radius * magnitude * math.cos(radians)
    return int(round(x)), int(round(y))


def camera_drag_endpoints(
    width: int, height: int, delta_deg: float, px_per_deg: float
) -> tuple[int, int, int, int]:
    """Return (x1, y1, x2, y2) for a horizontal mid-screen camera drag.

    Positive delta means "look right". The game's camera follows the finger
    inversely, so looking right is a right-to-left drag. The gesture is centred
    on mid-screen and clamped to the framebuffer; a zero delta yields a
    zero-length drag, which callers should skip rather than send.
    """
    distance = abs(float(delta_deg)) * float(px_per_deg)
    half = min(int(round(distance)), max(width - 1, 0)) // 2
    mid_x = width // 2
    mid_y = height // 2
    if delta_deg >= 0:
        x1, x2 = mid_x + half, mid_x - half
    else:
        x1, x2 = mid_x - half, mid_x + half
    x1 = min(max(x1, 0), width - 1)
    x2 = min(max(x2, 0), width - 1)
    y = min(max(mid_y, 0), height - 1)
    return x1, y, x2, y


class SidecarState:
    def __init__(
        self,
        adb: AdbClient,
        capture: CaptureLoop,
        config: dict,
        *,
        joystick_center: tuple[int, int],
        joystick_radius: float,
        camera_px_per_deg: float,
        hold_backend_kind: str,
        started_monotonic_s: float | None = None,
    ) -> None:
        self.adb = adb
        self.capture = capture
        self.config = config
        self.actions_dispatched = 0
        # One lock for every action: holds must not interleave with gestures.
        self.action_lock = threading.Lock()
        self.joystick_center = joystick_center
        self.joystick_radius = joystick_radius
        self.camera_px_per_deg = camera_px_per_deg
        self.hold_backend_kind = hold_backend_kind
        self._hold_backend: HoldBackend | None = None
        # Point of the active hold (None = no finger down). Remembering the
        # point is enough to release exactly where the finger went down.
        self.hold_point: tuple[int, int] | None = None
        # Uptime means "since the capture loop started", not "since this object
        # was built", so the lifespan passes the timestamp it took after start().
        self.started_monotonic_s = (
            time.monotonic() if started_monotonic_s is None else started_monotonic_s
        )

    def resolve_hold_backend(self) -> HoldBackend:
        """Lazily pick a held-touch transport; cached by AdbClient."""
        if self._hold_backend is None:
            self._hold_backend = self.adb.make_hold_backend(self.hold_backend_kind)
        return self._hold_backend

    @property
    def hold_backend_name(self) -> str:
        """Resolved transport, or the configured preference before first use."""
        if self._hold_backend is not None:
            return self._hold_backend.name
        return self.hold_backend_kind


def _dispatch(sc: SidecarState, action: Action) -> str | None:
    """Execute one already-validated action.

    Returns a human-readable error for *logical* refusals (a second hold). adb
    failures propagate as ``AdbError`` and are converted to HTTP 200 ``ok:false``
    by the endpoint: the client routes those to recovery, they are not request
    bugs the client can fix by retrying the same payload.
    """
    if action.type == "hold":
        if sc.hold_point is not None:
            return "a hold is already active; release it before starting another"
        point = joystick_point(
            sc.joystick_center, sc.joystick_radius, action.dir_deg, action.magnitude
        )
        sc.resolve_hold_backend().down(*point)
        sc.hold_point = point
    elif action.type == "release":
        if sc.hold_point is not None:
            point = sc.hold_point
            sc.resolve_hold_backend().up(*point)
            sc.hold_point = None
    elif action.type == "move_vector":
        # move_vector is a self-contained down/up, so it cannot run on top of an
        # active hold: the two share the same injected pointer and the release
        # would leave the remembered hold point out of sync with the device.
        if sc.hold_point is not None:
            return "a hold is already active; release it before move_vector"
        point = joystick_point(
            sc.joystick_center, sc.joystick_radius, action.dir_deg, action.magnitude
        )
        backend = sc.resolve_hold_backend()
        backend.down(*point)
        try:
            time.sleep(action.duration_ms / 1000.0)
        finally:
            # Never leave the joystick pushed if the call is interrupted.
            backend.up(*point)
    elif action.type == "camera":
        frame = sc.capture.latest()
        width = frame.width if frame is not None else DEFAULT_FRAME_WIDTH
        height = frame.height if frame is not None else DEFAULT_FRAME_HEIGHT
        x1, y1, x2, y2 = camera_drag_endpoints(
            width, height, action.delta_deg, sc.camera_px_per_deg
        )
        if x1 != x2:
            sc.adb.camera_drag(x1, y1, x2, y2, action.duration_ms)
    elif action.type == "tap_xy":
        sc.adb.tap(action.x, action.y)
    elif action.type == "tap":
        sc.adb.tap(action.x, action.y, action.duration_ms)  # type: ignore[arg-type]
    elif action.type == "swipe":
        sc.adb.swipe(  # type: ignore[arg-type]
            action.x, action.y, action.x2, action.y2, action.duration_ms
        )
    elif action.type == "back":
        sc.adb.back()
    else:  # wait
        time.sleep(action.duration_ms / 1000.0)
    return None


def create_app(
    adb: AdbClient | None = None,
    *,
    target_hz: float = 6.0,
    frame_format: FrameFormat = "jpeg",
    jpeg_quality: int = 80,
    joystick_center: tuple[int, int] = DEFAULT_JOYSTICK_CENTER,
    joystick_radius: float = DEFAULT_JOYSTICK_RADIUS,
    camera_px_per_deg: float = DEFAULT_CAMERA_PX_PER_DEG,
    hold_backend: str = "auto",
) -> FastAPI:
    if joystick_radius <= 0:
        raise ValueError(f"joystick_radius must be positive, got {joystick_radius}")
    if camera_px_per_deg <= 0:
        raise ValueError(f"camera_px_per_deg must be positive, got {camera_px_per_deg}")
    if hold_backend not in HOLD_BACKEND_KINDS:
        raise ValueError(
            f"hold_backend must be one of {HOLD_BACKEND_KINDS}, got {hold_backend!r}"
        )
    center = (int(joystick_center[0]), int(joystick_center[1]))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = adb or AdbClient(AdbConfig())
        loop = CaptureLoop(
            client, target_hz=target_hz, frame_format=frame_format, jpeg_quality=jpeg_quality
        )
        loop.start()
        capture_started_monotonic_s = time.monotonic()
        # Don't accept traffic before there is something to serve; a client that
        # connects during startup should get a frame, not a 503.
        loop.wait_for_frame(timeout_s=3.0)
        app.state.sidecar = SidecarState(
            client,
            loop,
            {
                "target_hz": target_hz,
                "frame_format": frame_format,
                "jpeg_quality": jpeg_quality,
                "joystick_center": center,
                "joystick_radius": joystick_radius,
                "camera_px_per_deg": camera_px_per_deg,
                "hold_backend": hold_backend,
            },
            joystick_center=center,
            joystick_radius=joystick_radius,
            camera_px_per_deg=camera_px_per_deg,
            hold_backend_kind=hold_backend,
            started_monotonic_s=capture_started_monotonic_s,
        )
        try:
            yield
        finally:
            loop.stop()

    app = FastAPI(title="HSR capture/actuator sidecar", version=VERSION, lifespan=lifespan)

    def state(request: Request) -> SidecarState:
        return request.app.state.sidecar

    @app.get("/health")
    def health(request: Request) -> dict:
        sc = state(request)
        capture_health = sc.capture.health()
        frame = sc.capture.latest()
        return {
            "ok": capture_health["frames_captured"] > 0 and capture_health["last_frame_age_ms"] is not None,
            "version": VERSION,
            "adb_path": sc.adb.adb_path,
            "device_serial": sc.adb.serial,
            "actions_dispatched": sc.actions_dispatched,
            "last_frame_ms": frame.capture_epoch_ms if frame is not None else None,
            "uptime_s": round(time.monotonic() - sc.started_monotonic_s, 3),
            "hold_active": sc.hold_point is not None,
            "hold_backend": sc.hold_backend_name,
            "capture": capture_health,
        }

    @app.get("/frame")
    def get_frame(request: Request) -> Response:
        sc = state(request)
        frame = sc.capture.latest()
        if frame is None:
            raise HTTPException(status_code=503, detail="no frame captured yet")
        response_epoch_ms = int(time.time() * 1000)
        return Response(
            content=frame.data,
            media_type=frame.content_type,
            headers={
                "X-Frame-Seq": str(frame.seq),
                "X-Content-Format": frame.content_type,
                "X-Frame-Width": str(frame.width),
                "X-Frame-Height": str(frame.height),
                "X-Capture-Epoch-Ms": str(frame.capture_epoch_ms),
                "X-Response-Epoch-Ms": str(response_epoch_ms),
                "X-Server-Age-Ms": f"{response_epoch_ms - frame.capture_epoch_ms}",
                "X-Capture-Duration-Ms": f"{frame.capture_duration_ms:.1f}",
                "Cache-Control": "no-store",
            },
        )

    @app.get("/frame/meta")
    def get_frame_meta(request: Request) -> dict:
        """Metadata only -- used by tests and by the LAN probe."""
        sc = state(request)
        frame = sc.capture.latest()
        if frame is None:
            raise HTTPException(status_code=503, detail="no frame captured yet")
        now_ms = int(time.time() * 1000)
        return {
            "seq": frame.seq,
            "width": frame.width,
            "height": frame.height,
            "bytes": len(frame.data),
            "capture_epoch_ms": frame.capture_epoch_ms,
            "response_epoch_ms": now_ms,
            "server_age_ms": now_ms - frame.capture_epoch_ms,
            "capture_duration_ms": round(frame.capture_duration_ms, 1),
        }

    @app.post("/action", response_model=ActionResponse)
    def post_action(request: Request, action: Action) -> ActionResponse:
        sc = state(request)
        started = time.perf_counter()
        dispatched_epoch_ms = int(time.time() * 1000)
        error: str | None = None
        with sc.action_lock:
            try:
                error = _dispatch(sc, action)
            except AdbError as exc:
                error = str(exc)
        if error is None:
            sc.actions_dispatched += 1
        frame = sc.capture.latest()
        return ActionResponse(
            ok=error is None,
            type=action.type,
            dispatched_epoch_ms=dispatched_epoch_ms,
            dispatch_ms=round((time.perf_counter() - started) * 1000.0, 1),
            frame_seq_at_dispatch=frame.seq if frame else None,
            error=error,
        )

    @app.get("/stats")
    def stats(request: Request) -> dict:
        sc = state(request)
        return {
            "capture": sc.capture.health(),
            "actions_dispatched": sc.actions_dispatched,
        }

    return app


def load_config(path: Path | None) -> dict:
    if path is None:
        return {}
    import tomllib

    with path.open("rb") as fh:
        return tomllib.load(fh)


def _config_joystick(config: dict) -> dict:
    """Read the [joystick] table, tolerating its absence and bad shapes loudly."""
    section = config.get("joystick", {})
    if not isinstance(section, dict):
        raise ValueError("[joystick] must be a TOML table")
    center = section.get("center", [DEFAULT_JOYSTICK_CENTER[0], DEFAULT_JOYSTICK_CENTER[1]])
    if not isinstance(center, (list, tuple)) or len(center) != 2:
        raise ValueError("[joystick].center must be [x, y]")
    return {
        "joystick_center": (int(center[0]), int(center[1])),
        "joystick_radius": float(section.get("radius", DEFAULT_JOYSTICK_RADIUS)),
        "camera_px_per_deg": float(
            section.get("camera_px_per_deg", DEFAULT_CAMERA_PX_PER_DEG)
        ),
        "hold_backend": str(section.get("hold_backend", "auto")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HSR capture/actuator sidecar")
    parser.add_argument("--host", default=None, help="bind address (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default 8765)")
    parser.add_argument("--target-hz", type=float, default=None, help="capture rate (default 6.0)")
    parser.add_argument(
        "--frame-format", choices=("jpeg", "png"), default=None,
        help="image encoding: jpeg (fast, default) or png (plan-literal, slower)",
    )
    parser.add_argument("--jpeg-quality", type=int, default=None, help="JPEG quality 1-100 (default 80)")
    parser.add_argument("--adb-path", default=None, help="path to adb.exe")
    parser.add_argument("--serial", default=None, help="adb serial, e.g. 127.0.0.1:16384")
    parser.add_argument("--config", type=Path, default=None, help="TOML config file")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    section = config.get("sidecar", {})
    host = args.host or section.get("host", "0.0.0.0")
    port = args.port or int(section.get("port", 8765))
    target_hz = args.target_hz or float(section.get("target_hz", 6.0))
    frame_format = args.frame_format or section.get("frame_format", "jpeg")
    jpeg_quality = args.jpeg_quality or int(section.get("jpeg_quality", 80))
    adb_path = args.adb_path or section.get("adb_path", "")
    serial = args.serial or section.get("serial", "")
    joystick = _config_joystick(config)

    import uvicorn

    app = create_app(
        AdbClient(AdbConfig(adb_path=adb_path, serial=serial)),
        target_hz=target_hz,
        frame_format=frame_format,
        jpeg_quality=jpeg_quality,
        **joystick,
    )
    print(json.dumps({
        "event": "starting", "host": host, "port": port,
        "target_hz": target_hz, "frame_format": frame_format,
        **joystick,
    }))
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
