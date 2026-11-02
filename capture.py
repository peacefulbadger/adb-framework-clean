"""Background screencap loop.

One thread, one adb call per iteration. The loop keeps the *latest* frame in
memory; HTTP readers never wait for a capture, they get whatever the loop last
produced plus enough timing metadata to decide whether it is too old to act on.

Timestamps are deliberately conservative: `capture_epoch_ms` is taken *before*
the screencap call starts, so a reader's computed age is an upper bound on the
true age of the pixels. Being pessimistic here is the whole point -- the
orchestrator rejects stale frames, and it must not reject them late.

Two transport formats:

    png   plan-literal, but the emulator compresses on-device: ~315 ms/frame
    jpeg  raw framebuffer (~132 ms) encoded here in ~10 ms -- the default

Neither is compressed any further; see `imaging.py` for the measurements.
"""

from __future__ import annotations

import struct
import threading
import time
from dataclasses import dataclass
from typing import Literal

from adb import AdbClient, AdbError
from imaging import decode_screencap_raw, encode_jpeg

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

FrameFormat = Literal["jpeg", "png"]


def png_size(png: bytes) -> tuple[int, int]:
    """Read width/height from the IHDR chunk without decoding the image."""
    if len(png) < 24 or not png.startswith(PNG_MAGIC):
        raise AdbError("not a PNG")
    width, height = struct.unpack(">II", png[16:24])
    return int(width), int(height)


@dataclass(frozen=True, slots=True)
class Frame:
    seq: int
    data: bytes
    content_type: str
    width: int
    height: int
    capture_epoch_ms: int
    capture_monotonic_s: float
    capture_duration_ms: float
    encode_duration_ms: float

    @property
    def age_ms(self) -> float:
        return (time.monotonic() - self.capture_monotonic_s) * 1000.0

    @property
    def is_png(self) -> bool:
        return self.content_type == "image/png"


class CaptureLoop:
    def __init__(
        self,
        adb: AdbClient,
        *,
        target_hz: float = 6.0,
        frame_format: FrameFormat = "jpeg",
        jpeg_quality: int = 80,
        failure_backoff_s: float = 1.0,
    ) -> None:
        if target_hz <= 0:
            raise ValueError("target_hz must be positive")
        if frame_format not in ("jpeg", "png"):
            raise ValueError(f"unsupported frame_format: {frame_format}")
        self._adb = adb
        self._target_hz = float(target_hz)
        self._period_s = 1.0 / float(target_hz)
        self._frame_format: FrameFormat = frame_format
        self._jpeg_quality = int(jpeg_quality)
        self._failure_backoff_s = failure_backoff_s

        self._lock = threading.Lock()
        self._latest: Frame | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._frames = 0
        self._errors = 0
        self._consecutive_errors = 0
        self._last_error: str | None = None
        self._started_epoch_ms: int | None = None
        self._recent_capture_times: list[float] = []

    # ------------------------------------------------------------------ control

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("capture loop already started")
        self._stop.clear()
        self._started_epoch_ms = int(time.time() * 1000)
        self._thread = threading.Thread(target=self._run, name="capture-loop", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout_s)
            self._thread = None

    def __enter__(self) -> "CaptureLoop":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # ------------------------------------------------------------------- access

    def latest(self) -> Frame | None:
        with self._lock:
            return self._latest

    def wait_for_frame(self, timeout_s: float = 5.0) -> Frame | None:
        """Block until at least one frame exists. For startup and tests only."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            frame = self.latest()
            if frame is not None:
                return frame
            time.sleep(0.01)
        return None

    def health(self) -> dict:
        frame = self.latest()
        with self._lock:
            capture_times = list(self._recent_capture_times)
            stats = {
                "running": self._thread is not None and self._thread.is_alive(),
                "target_hz": self._target_hz,
                "frame_format": self._frame_format,
                "jpeg_quality": self._jpeg_quality,
                "frames_captured": self._frames,
                "capture_errors": self._errors,
                "consecutive_errors": self._consecutive_errors,
                "last_error": self._last_error,
                "started_epoch_ms": self._started_epoch_ms,
                "last_frame_seq": frame.seq if frame else None,
                "last_frame_age_ms": round(frame.age_ms, 1) if frame else None,
                "last_frame_bytes": len(frame.data) if frame else None,
                "effective_hz": round(_rate(capture_times), 3),
            }
        return stats

    # --------------------------------------------------------------------- loop

    def _run(self) -> None:
        while not self._stop.is_set():
            iteration_start = time.monotonic()
            # Timestamp before the capture: conservative upper bound on age.
            capture_monotonic = time.monotonic()
            capture_epoch_ms = int(time.time() * 1000)
            try:
                data, content_type, width, height, capture_ms, encode_ms = self._grab()
            except Exception as exc:  # noqa: BLE001 - loop must survive anything
                with self._lock:
                    self._errors += 1
                    self._consecutive_errors += 1
                    self._last_error = f"{type(exc).__name__}: {exc}"
                self._stop.wait(self._failure_backoff_s)
                continue

            with self._lock:
                self._frames += 1
                self._consecutive_errors = 0
                self._latest = Frame(
                    seq=self._frames,
                    data=data,
                    content_type=content_type,
                    width=width,
                    height=height,
                    capture_epoch_ms=capture_epoch_ms,
                    capture_monotonic_s=capture_monotonic,
                    capture_duration_ms=capture_ms,
                    encode_duration_ms=encode_ms,
                )
                self._recent_capture_times.append(capture_monotonic)
                if len(self._recent_capture_times) > 32:
                    del self._recent_capture_times[:-32]

            # Pace the loop; if capture overran the period, go again immediately.
            elapsed = time.monotonic() - iteration_start
            remaining = self._period_s - elapsed
            if remaining > 0:
                self._stop.wait(remaining)

    def _grab(self) -> tuple[bytes, str, int, int, float, float]:
        if self._frame_format == "png":
            png, adb_ms = self._adb.screencap_png()
            width, height = png_size(png)
            return png, "image/png", width, height, adb_ms, 0.0

        # JPEG mode: capture PNG from adb (reliable across builds),
        # decode on host, encode as JPEG.  MuMu's raw screencap format
        # is non-standard; PNG→JPEG adds ~10 ms host overhead vs
        # ~250 ms on-device compression savings — net still faster.
        png, adb_ms = self._adb.screencap_png()
        t0 = time.perf_counter()
        bgr = decode_screencap_raw(png)
        jpeg = encode_jpeg(bgr, self._jpeg_quality)
        encode_ms = (time.perf_counter() - t0) * 1000.0
        height, width = bgr.shape[:2]
        return jpeg, "image/jpeg", int(width), int(height), adb_ms, encode_ms


def _rate(times: list[float]) -> float:
    if len(times) < 2:
        return 0.0
    span = times[-1] - times[0]
    if span <= 0:
        return 0.0
    return (len(times) - 1) / span
