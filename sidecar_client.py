"""Thin HTTP client for the sidecar's GET /frame, POST /action, GET /health endpoints.

All vision/exploration code talks only through this client, enabling the replay
harness to inject the same interface.

Structured error types:
    - SidecarDown        : connection refused / timeout
    - CaptureStalled     : no new frame within expected window
    - InputNotAcknowledged: POST /action returned ok:false
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class SidecarError(Exception):
    """Base exception for sidecar communication failures."""


class SidecarDown(SidecarError):
    """The sidecar is unreachable (connection refused / timeout)."""


class CaptureStalled(SidecarError):
    """No new frame received within the expected time window."""


class InputNotAcknowledged(SidecarError):
    """POST /action returned ok:false with an error message."""


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Frame:
    """A frame returned by GET /frame."""

    data: bytes
    seq: int
    width: int
    height: int
    capture_epoch_ms: int
    response_epoch_ms: int
    server_age_ms: float
    content_type: str = "image/jpeg"

    @property
    def age_ms(self) -> float:
        """Age of this frame at the moment it was received (server-side)."""
        return self.server_age_ms


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """Result of GET /health."""

    ok: bool
    version: str
    uptime_s: float
    last_frame_ms: int | None
    actions_dispatched: int
    hold_active: bool
    hold_backend: str
    capture: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Result of POST /action."""

    ok: bool
    action_type: str
    dispatched_epoch_ms: int
    dispatch_ms: float
    error: str | None


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class SidecarClient:
    """HTTP client for the sidecar.

    Parameters
    ----------
    host :
        Sidecar bind address (default 127.0.0.1).
    port :
        Sidecar port (default 8765).
    connect_timeout :
        Seconds to wait for the initial connection.
    read_timeout :
        Seconds to wait for a response body.
    retry_count :
        Number of retries on transient failures (connection errors only).
    retry_backoff_s :
        Exponential backoff base in seconds between retries.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        connect_timeout: float = 5.0,
        read_timeout: float = 30.0,
        retry_count: int = 2,
        retry_backoff_s: float = 0.5,
    ) -> None:
        self._base_url = f"http://{host}:{port}"
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._retry_count = retry_count
        self._retry_backoff_s = retry_backoff_s

    # ------------------------------------------------------------------ frame

    def get_frame(self) -> Frame:
        """Fetch the latest frame from GET /frame.

        Raises SidecarDown on connection failure, CaptureStalled if no frame
        is available yet.
        """
        for attempt in range(1 + self._retry_count):
            try:
                with httpx.Client(timeout=httpx.Timeout(self._read_timeout)) as client:
                    resp = client.get(f"{self._base_url}/frame")
                    resp.raise_for_status()
                    return Frame(
                        data=resp.content,
                        seq=int(resp.headers["X-Frame-Seq"]),
                        width=int(resp.headers["X-Frame-Width"]),
                        height=int(resp.headers["X-Frame-Height"]),
                        capture_epoch_ms=int(resp.headers["X-Capture-Epoch-Ms"]),
                        response_epoch_ms=int(resp.headers["X-Response-Epoch-Ms"]),
                        server_age_ms=float(resp.headers["X-Server-Age-Ms"]),
                        content_type=resp.headers.get("X-Content-Format", "image/jpeg"),
                    )
            except httpx.ConnectError as exc:
                logger.warning("sidecar connection failed (attempt %d/%d): %s", attempt + 1, self._retry_count + 1, exc)
                if attempt < self._retry_count:
                    time.sleep(self._retry_backoff_s * (2 ** attempt))
                else:
                    raise SidecarDown(f"could not connect to sidecar at {self._base_url}") from exc
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 503:
                    raise CaptureStalled("sidecar has no frame yet") from exc
                raise
        # Should not reach here, but type-checker wants it.
        raise CaptureStalled("unexpected: no frame after retries")

    def get_frame_meta(self) -> dict[str, Any]:
        """Fetch frame metadata only (GET /frame/meta)."""
        for attempt in range(1 + self._retry_count):
            try:
                with httpx.Client(timeout=httpx.Timeout(self._read_timeout)) as client:
                    resp = client.get(f"{self._base_url}/frame/meta")
                    resp.raise_for_status()
                    return resp.json()
            except httpx.ConnectError as exc:
                logger.warning("sidecar connection failed (attempt %d/%d): %s", attempt + 1, self._retry_count + 1, exc)
                if attempt < self._retry_count:
                    time.sleep(self._retry_backoff_s * (2 ** attempt))
                else:
                    raise SidecarDown(f"could not connect to sidecar at {self._base_url}") from exc
        raise CaptureStalled("no meta after retries")

    # ------------------------------------------------------------------ action

    def post_action(self, action: dict[str, Any]) -> ActionResult:
        """Dispatch an action via POST /action.

        Parameters
        ----------
        action :
            Dict matching one of the sidecar action schemas, e.g.::

                {"type": "move_vector", "dir_deg": 0, "duration_ms": 500}

        Raises InputNotAcknowledged when the sidecar returns ok:false.
        """
        for attempt in range(1 + self._retry_count):
            try:
                with httpx.Client(timeout=httpx.Timeout(self._read_timeout)) as client:
                    resp = client.post(f"{self._base_url}/action", json=action)
                    resp.raise_for_status()
                    body = resp.json()
                    result = ActionResult(
                        ok=bool(body["ok"]),
                        action_type=str(body["type"]),
                        dispatched_epoch_ms=int(body["dispatched_epoch_ms"]),
                        dispatch_ms=float(body["dispatch_ms"]),
                        error=body.get("error"),
                    )
                    if not result.ok:
                        raise InputNotAcknowledged(
                            f"action {result.action_type} failed: {result.error}"
                        )
                    return result
            except InputNotAcknowledged:
                # Logical failure — don't retry; caller should route to recovery.
                raise
            except httpx.ConnectError as exc:
                logger.warning("sidecar connection failed (attempt %d/%d): %s", attempt + 1, self._retry_count + 1, exc)
                if attempt < self._retry_count:
                    time.sleep(self._retry_backoff_s * (2 ** attempt))
                else:
                    raise SidecarDown(f"could not connect to sidecar at {self._base_url}") from exc
        raise SidecarDown("unexpected")

    def move_vector(self, dir_deg: float, duration_ms: int = 500, magnitude: float = 1.0) -> ActionResult:
        """Convenience wrapper for a joystick move_vector action."""
        return self.post_action({
            "type": "move_vector",
            "dir_deg": float(dir_deg) % 360.0,
            "duration_ms": int(duration_ms),
            "magnitude": float(magnitude),
        })

    def hold(self, dir_deg: float, magnitude: float = 1.0) -> ActionResult:
        """Start a persistent joystick hold."""
        return self.post_action({
            "type": "hold",
            "dir_deg": float(dir_deg) % 360.0,
            "magnitude": float(magnitude),
        })

    def release(self) -> ActionResult:
        """Release the active joystick hold."""
        return self.post_action({"type": "release"})

    def camera(self, delta_deg: float, duration_ms: int = 250) -> ActionResult:
        """Turn the in-game camera by delta_deg degrees."""
        return self.post_action({
            "type": "camera",
            "delta_deg": float(delta_deg),
            "duration_ms": int(duration_ms),
        })

    def tap_xy(self, x: int, y: int) -> ActionResult:
        """Tap a framebuffer pixel."""
        return self.post_action({
            "type": "tap_xy",
            "x": int(x),
            "y": int(y),
        })

    # ------------------------------------------------------------------ health

    def get_health(self) -> HealthStatus:
        """Fetch sidecar health status from GET /health."""
        for attempt in range(1 + self._retry_count):
            try:
                with httpx.Client(timeout=httpx.Timeout(self._read_timeout)) as client:
                    resp = client.get(f"{self._base_url}/health")
                    resp.raise_for_status()
                    body = resp.json()
                    return HealthStatus(
                        ok=bool(body["ok"]),
                        version=str(body["version"]),
                        uptime_s=float(body["uptime_s"]),
                        last_frame_ms=body.get("last_frame_ms"),
                        actions_dispatched=int(body["actions_dispatched"]),
                        hold_active=bool(body.get("hold_active", False)),
                        hold_backend=str(body.get("hold_backend", "unknown")),
                        capture=body.get("capture", {}),
                    )
            except httpx.ConnectError as exc:
                logger.warning("sidecar connection failed (attempt %d/%d): %s", attempt + 1, self._retry_count + 1, exc)
                if attempt < self._retry_count:
                    time.sleep(self._retry_backoff_s * (2 ** attempt))
                else:
                    raise SidecarDown(f"could not connect to sidecar at {self._base_url}") from exc
        raise SidecarDown("unexpected")

    # ------------------------------------------------------------------ replay

    def close(self) -> None:
        """Release any held resources. No-op for httpx-based client."""
