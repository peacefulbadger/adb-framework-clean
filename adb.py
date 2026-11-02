"""Thin, synchronous wrapper around the Android Debug Bridge binary.

Deliberately dumb: no game logic, no retries-and-heuristics, no state machine.
The sidecar's job is to turn adb invocations into a byte stream and a dispatch
call, nothing more. Everything interesting happens in the orchestrator.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

# MuMuPlayerGlobal ships several adb binaries; nx_device/.../shell/adb.exe is the
# one that matches the emulator's adb server version.
DEFAULT_ADB_CANDIDATES: tuple[str, ...] = (
    r"C:\Program Files\Netease\MuMuPlayerGlobal-12.0\nx_device\12.0\shell\adb.exe",
    r"C:\Program Files\Netease\MuMuPlayerGlobal-12.0\nx_main\adb.exe",
    r"C:\Program Files\Netease\MuMuPlayerGlobal-12.0\shell\adb.exe",
)

# MuMu listens on 16384 by default; 7555 is the classic MuMu port and
# emulator-5554 shows up when the emulator registers itself.
DEFAULT_SERIAL_CANDIDATES: tuple[str, ...] = (
    "127.0.0.1:16384",
    "127.0.0.1:7555",
    "emulator-5554",
)

KEYCODE_BACK = 4


class AdbError(RuntimeError):
    """An adb invocation failed or the device is not usable."""


@dataclass(slots=True)
class AdbConfig:
    adb_path: str = ""
    serial: str = ""
    connect_timeout_s: float = 5.0
    command_timeout_s: float = 15.0
    extra_args: tuple[str, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class AdbResult:
    returncode: int
    stdout: bytes
    stderr: str
    duration_ms: float


# --------------------------------------------------------------------------- #
# Held touch ("joystick") backends
# --------------------------------------------------------------------------- #
#
# `input swipe` is one atomic gesture: it lifts the finger when it returns, so it
# cannot express "push the joystick and keep it pushed while the orchestrator
# thinks". A hold therefore needs a transport that leaves the pointer down across
# separate adb calls. Which transport works on the target emulator is a genuine
# Phase-0 unknown (plan section 11, risk table):
#
#   motion_event   `input motionevent DOWN/MOVE/UP` -- cleanest, but the
#                  subcommand only exists on Android 11+ and some vendor builds
#                  strip it. There is no documented capability query, so `auto`
#                  probes the `input` usage text (see AdbClient).
#   sendevent      raw /dev/input writes -- universal in principle, but needs a
#                  readable touchscreen node, the device's own ABS ranges, the
#                  right multitouch protocol (A vs B) and usually more
#                  permissions than the emulator shell has. Best effort only.
#   swipe_repeat   back-to-back tiny `input swipe x y x y` gestures -- always
#                  works, but the finger lifts between gestures, so a joystick
#                  springs back for the adb round-trip. Documented accuracy
#                  penalty; last resort.
#
# All three are duck-typed behind `HoldBackend` so the server never branches on
# transport availability.


class HoldBackend(Protocol):
    """A persistent touch that survives across separate HTTP requests."""

    name: str

    def down(self, x: int, y: int) -> None:
        """Put a finger down at (x, y) and leave it down."""

    def move(self, x: int, y: int) -> None:
        """Drag the held finger to (x, y); no-op if supported only via down/up."""

    def up(self, x: int, y: int) -> None:
        """Lift the finger at (x, y)."""


class MotionEventHoldBackend:
    """Hold via `input motionevent`, which keeps one injected pointer alive.

    Preferred when available: it is a real Android input event stream with no
    device-node discovery, no root and no axis-range translation.
    """

    name = "motion_event"

    def __init__(self, adb: "AdbClient") -> None:
        self._adb = adb

    def _event(self, action: str, x: int, y: int) -> None:
        self._adb.shell("input", "motionevent", action, str(int(x)), str(int(y)))

    def down(self, x: int, y: int) -> None:
        self._event("DOWN", x, y)

    def move(self, x: int, y: int) -> None:
        self._event("MOVE", x, y)

    def up(self, x: int, y: int) -> None:
        self._event("UP", x, y)


@dataclass(frozen=True, slots=True)
class Touchscreen:
    """A touchscreen device advertised by `getevent -pl`."""

    node: str
    name: str
    x_min: int
    x_max: int
    y_min: int
    y_max: int

    def clamp(self, x: int, y: int) -> tuple[int, int]:
        """Clamp framebuffer-ish coordinates into the node's ABS range."""
        return (
            min(max(int(x), self.x_min), self.x_max),
            min(max(int(y), self.y_min), self.y_max),
        )


_GETEVENT_DEVICE_RE = re.compile(r"^add device \d+:\s*(\S+)\s*$")
_GETEVENT_NAME_RE = re.compile(r"^\s*name:\s*(.*?)\s*$")
_GETEVENT_ABS_RE = re.compile(
    r"^\s*(ABS_MT_POSITION_[XY])\s*:\s*value\s+-?\d+,\s*min\s+(-?\d+),\s*max\s+(-?\d+)"
)


def _parse_getevent_pl(text: str) -> Touchscreen | None:
    """Return the first input device advertising ABS_MT_POSITION_X *and* _Y.

    `getevent -pl` prints one block per device, e.g.::

        add device 2: /dev/input/event3
          name:     "mtk-tpd"
          events:
            KEY (0001): BTN_TOUCH
            ABS (0003): ABS_MT_POSITION_X : value 0, min 0, max 1079, ...
                        ABS_MT_POSITION_Y : value 0, min 0, max 2399, ...

    Volume/power keys and sensors also advertise ABS axes, which is why both
    position axes are required, not just "any ABS device".
    """
    found: list[Touchscreen] = []
    node: str | None = None
    name = ""
    ranges: dict[str, tuple[int, int]] = {}

    def flush() -> None:
        if node is None:
            return
        x_range = ranges.get("ABS_MT_POSITION_X")
        y_range = ranges.get("ABS_MT_POSITION_Y")
        if x_range is None or y_range is None:
            return
        found.append(
            Touchscreen(
                node=node,
                name=name,
                x_min=x_range[0],
                x_max=x_range[1],
                y_min=y_range[0],
                y_max=y_range[1],
            )
        )

    for line in text.splitlines():
        device_match = _GETEVENT_DEVICE_RE.match(line)
        if device_match:
            flush()
            node = device_match.group(1)
            name = ""
            ranges = {}
            continue
        if node is None:
            continue
        name_match = _GETEVENT_NAME_RE.match(line)
        if name_match and not name:
            name = name_match.group(1).strip().strip('"')
            continue
        abs_match = _GETEVENT_ABS_RE.match(line)
        if abs_match:
            ranges[abs_match.group(1)] = (int(abs_match.group(2)), int(abs_match.group(3)))
    flush()
    return found[0] if found else None


class SendeventHoldBackend:
    """Hold by writing Linux input events straight to the touchscreen node.

    Protocol B (slot-less) sequence, which is what most emulated touchscreens
    accept:

        down: ABS_MT_TRACKING_ID=<id>, ABS_MT_POSITION_X, ABS_MT_POSITION_Y,
              BTN_TOUCH=1, SYN_REPORT
        move: ABS_MT_POSITION_X, ABS_MT_POSITION_Y, SYN_REPORT
        up:   ABS_MT_TRACKING_ID=-1, BTN_TOUCH=0, SYN_REPORT

    This is the most fragile backend: the node must be readable by the shell
    user, the device may want protocol A or a different BTN_TOUCH pairing, and
    `sendevent` takes decimal type/code/value triples. Discovery and range
    parsing are deliberately separated so failures produce one clear AdbError
    instead of a mystery mid-gesture.
    """

    name = "sendevent"

    EV_SYN = 0
    EV_KEY = 1
    EV_ABS = 3
    SYN_REPORT = 0
    BTN_TOUCH = 330
    ABS_MT_POSITION_X = 53
    ABS_MT_POSITION_Y = 54
    ABS_MT_TRACKING_ID = 57

    def __init__(self, adb: "AdbClient") -> None:
        self._adb = adb
        self._device: Touchscreen | None = None
        self._next_tracking_id = 1

    def discover(self) -> Touchscreen:
        """Find (once) the touchscreen node and its ABS ranges."""
        if self._device is not None:
            return self._device
        result = self._adb.shell("getevent", "-pl")
        text = result.stdout.decode("utf-8", "replace")
        device = _parse_getevent_pl(text)
        if device is None:
            excerpt = " ".join(text.split())[:300]
            raise AdbError(
                "sendevent hold backend: no input device advertising "
                f"ABS_MT_POSITION_X/Y in `getevent -pl` output ({excerpt!r})"
            )
        self._device = device
        return device

    def _emit(self, node: str, event_type: int, code: int, value: int) -> None:
        self._adb.shell(
            "sendevent", node, str(int(event_type)), str(int(code)), str(int(value))
        )

    def _sync(self, node: str) -> None:
        self._emit(node, self.EV_SYN, self.SYN_REPORT, 0)

    def down(self, x: int, y: int) -> None:
        device = self.discover()
        x, y = device.clamp(x, y)
        tracking_id = self._next_tracking_id
        self._next_tracking_id += 1
        self._emit(device.node, self.EV_ABS, self.ABS_MT_TRACKING_ID, tracking_id)
        self._emit(device.node, self.EV_ABS, self.ABS_MT_POSITION_X, x)
        self._emit(device.node, self.EV_ABS, self.ABS_MT_POSITION_Y, y)
        self._emit(device.node, self.EV_KEY, self.BTN_TOUCH, 1)
        self._sync(device.node)

    def move(self, x: int, y: int) -> None:
        device = self.discover()
        x, y = device.clamp(x, y)
        self._emit(device.node, self.EV_ABS, self.ABS_MT_POSITION_X, x)
        self._emit(device.node, self.EV_ABS, self.ABS_MT_POSITION_Y, y)
        self._sync(device.node)

    def up(self, x: int, y: int) -> None:
        device = self.discover()
        # -1 is the protocol-B "no contact" tracking id; on the wire it is
        # 0xFFFFFFFF, which `sendevent`'s integer parser wraps correctly.
        self._emit(device.node, self.EV_ABS, self.ABS_MT_TRACKING_ID, -1)
        self._emit(device.node, self.EV_KEY, self.BTN_TOUCH, 0)
        self._sync(device.node)


class SwipeRepeatHoldBackend:
    """Hold by looping tiny `input swipe` gestures around the hold point.

    Every swipe lifts the finger when it finishes, so the joystick springs back
    for the adb round-trip between gestures. That is a real accuracy penalty
    (plan section 11): held movement drifts between gestures and precise
    long holds are unreliable. It exists because it needs nothing beyond
    `input swipe`, which every Android build has.
    """

    name = "swipe_repeat"

    def __init__(self, adb: "AdbClient", *, gesture_ms: int = 300) -> None:
        if gesture_ms <= 0:
            raise ValueError("gesture_ms must be positive")
        self._adb = adb
        self.gesture_ms = int(gesture_ms)
        self._lock = threading.Lock()
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._point = (0, 0)

    def down(self, x: int, y: int) -> None:
        x, y = int(x), int(y)
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("swipe_repeat hold is already active")
            stop = threading.Event()
            self._stop = stop
            self._point = (x, y)
        # First gesture synchronously: contact exists (and adb errors surface to
        # the caller) before `down` returns.
        self._adb.swipe(x, y, x, y, self.gesture_ms)
        thread = threading.Thread(
            target=self._repeat, args=(stop,), name="swipe-repeat-hold", daemon=True
        )
        with self._lock:
            self._thread = thread
        thread.start()

    def move(self, x: int, y: int) -> None:
        with self._lock:
            self._point = (int(x), int(y))

    def up(self, x: int, y: int) -> None:
        with self._lock:
            stop = self._stop
            thread = self._thread
            self._stop = None
            self._thread = None
        if stop is not None:
            stop.set()
        if thread is not None:
            # A swipe may be in flight; it ends with its own UP.
            thread.join(timeout=self.gesture_ms / 1000.0 + 2.0)

    def _repeat(self, stop: threading.Event) -> None:
        while not stop.is_set():
            with self._lock:
                x, y = self._point
            try:
                self._adb.swipe(x, y, x, y, self.gesture_ms)
            except AdbError:
                # The hold is lost; there is no caller to raise to. The server's
                # next action will surface the device problem.
                return


class AdbClient:
    def __init__(self, config: AdbConfig | None = None) -> None:
        self.config = config or AdbConfig()
        self._adb_path = self._resolve_adb_path()
        self._serial = self._resolve_serial()
        # Resolved hold transports, keyed by name plus "auto" so a probe only
        # ever runs once per process.
        self._hold_backends: dict[str, HoldBackend] = {}

    # ---------------------------------------------------------------- discovery

    def _resolve_adb_path(self) -> str:
        configured = self.config.adb_path
        if configured:
            path = Path(configured)
            if not path.is_file():
                raise AdbError(f"configured adb_path does not exist: {path}")
            return str(path)

        for candidate in DEFAULT_ADB_CANDIDATES:
            if Path(candidate).is_file():
                return candidate

        # Fall back to PATH.
        try:
            found = subprocess.run(
                ["where", "adb"], capture_output=True, text=True, timeout=10
            )
        except OSError as exc:  # pragma: no cover - platform dependent
            raise AdbError(f"could not locate adb: {exc}") from exc
        lines = [ln.strip() for ln in found.stdout.splitlines() if ln.strip()]
        if not lines:
            raise AdbError(
                "could not locate adb.exe; pass --adb-path or set it in the config"
            )
        return lines[0]

    def _resolve_serial(self) -> str:
        if self.config.serial:
            return self.config.serial
        result = self._run(["devices"], timeout=self.config.connect_timeout_s, with_serial=False)
        text = result.stdout.decode("utf-8", "replace")
        online = [
            ln.split("\t")[0].strip()
            for ln in text.splitlines()[1:]
            if "\tdevice" in ln
        ]
        for candidate in DEFAULT_SERIAL_CANDIDATES:
            if candidate in online:
                return candidate
        if online:
            return online[0]
        raise AdbError(f"no online adb device; `adb devices` said:\n{text.strip()}")

    @property
    def adb_path(self) -> str:
        return self._adb_path

    @property
    def serial(self) -> str:
        return self._serial

    # ------------------------------------------------------------------ plumbing

    def _run(
        self,
        args: list[str],
        *,
        binary: bool = False,
        timeout: float | None = None,
        with_serial: bool = True,
        allow_failure: bool = False,
    ) -> AdbResult:
        argv = [self._adb_path]
        # `self._serial` does not exist until discovery finishes; `adb devices`
        # is one of the calls used to discover it.
        if with_serial and getattr(self, "_serial", ""):
            argv += ["-s", self._serial]
        argv += args

        t0 = time.perf_counter()
        effective_timeout = timeout or self.config.command_timeout_s
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                timeout=effective_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdbError(
                f"adb timed out after {effective_timeout:g}s: {' '.join(args)}"
            ) from exc
        except OSError as exc:
            raise AdbError(f"failed to run {argv[0]}: {exc}") from exc
        duration_ms = (time.perf_counter() - t0) * 1000.0

        stderr = proc.stderr.decode("utf-8", "replace").strip()
        if proc.returncode != 0 and allow_failure:
            return AdbResult(proc.returncode, proc.stdout, stderr, duration_ms)
        if proc.returncode != 0:
            raise AdbError(
                f"adb {' '.join(args)} exited {proc.returncode}: {stderr or '<no stderr>'}"
            )
        return AdbResult(proc.returncode, proc.stdout, stderr, duration_ms)

    # --------------------------------------------------------------- operations

    def shell(self, *args: str) -> AdbResult:
        """Run an arbitrary `adb shell` command. Escape hatch, used sparingly."""
        return self._run(["shell", *args])

    def state(self) -> str:
        result = self._run(["get-state"], timeout=self.config.connect_timeout_s)
        return result.stdout.decode("utf-8", "replace").strip() or "unknown"

    def screencap_png(self) -> tuple[bytes, float]:
        """Return (png_bytes, adb_duration_ms).

        `exec-out` keeps the PNG binary-clean; shell redirection would corrupt it
        on Windows (CRLF translation).

        Note: the PNG is compressed *on the device*. On the MuMu emulator that
        costs ~315 ms per frame, which caps the capture loop at ~3 Hz. Prefer
        `screencap_raw` unless you specifically want PNG.
        """
        result = self._run(["exec-out", "screencap", "-p"])
        if not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            head = result.stdout[:32]
            raise AdbError(f"screencap did not return a PNG (head={head!r})")
        return result.stdout, result.duration_ms

    def screencap_raw(self) -> tuple[bytes, float]:
        """Return (raw_framebuffer_bytes, adb_duration_ms).

        ~132 ms on the same emulator: no on-device compression. The payload is
        w, h, format(, colorspace) followed by RGBA pixels; see
        `sidecar_windows.imaging.decode_screencap_raw`.
        """
        result = self._run(["exec-out", "screencap"])
        if len(result.stdout) < 64:
            raise AdbError(f"screencap returned {len(result.stdout)} bytes; device may be asleep")
        return result.stdout, result.duration_ms

    def tap(self, x: int, y: int, duration_ms: int = 0) -> float:
        args = ["shell", "input", "tap", str(int(x)), str(int(y))]
        return self._run(args).duration_ms

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 200) -> float:
        args = [
            "shell", "input", "swipe",
            str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)),
            str(int(duration_ms)),
        ]
        return self._run(args).duration_ms

    def camera_drag(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 250) -> float:
        """Horizontal look-around swipe; same transport as a normal swipe.

        Kept as a named method so the server expresses camera intent instead of
        raw coordinates, and so the duration default can differ (camera drags
        are deliberately slower than taps -- faster drags under-rotate because
        the game samples input per frame).
        """
        return self.swipe(x1, y1, x2, y2, duration_ms)

    # ------------------------------------------------------------- held touch

    def make_hold_backend(self, kind: str = "auto") -> HoldBackend:
        """Return (and cache) a held-touch transport.

        ``kind`` is ``auto|motion_event|sendevent|swipe_repeat``. ``auto`` tries
        motion_event (probe the shell `input` usage text), then sendevent
        (discover a touchscreen node), then falls back to swipe_repeat.
        """
        known = ("auto", "motion_event", "sendevent", "swipe_repeat")
        if kind not in known:
            raise ValueError(f"unknown hold backend {kind!r}; expected one of {known}")

        if kind == "auto":
            cached_auto = self._hold_backends.get("auto")
            if cached_auto is not None:
                return cached_auto
            backend = self._select_auto_hold_backend()
            self._hold_backends["auto"] = backend
            self._hold_backends.setdefault(backend.name, backend)
            return backend

        cached = self._hold_backends.get(kind)
        if cached is not None:
            return cached
        factories: dict[str, Callable[["AdbClient"], HoldBackend]] = {
            "motion_event": MotionEventHoldBackend,
            "sendevent": SendeventHoldBackend,
            "swipe_repeat": SwipeRepeatHoldBackend,
        }
        backend = factories[kind](self)
        self._hold_backends[kind] = backend
        return backend

    def _select_auto_hold_backend(self) -> HoldBackend:
        if self._probe_motion_event():
            return MotionEventHoldBackend(self)
        sendevent = SendeventHoldBackend(self)
        try:
            sendevent.discover()
        except AdbError:
            return SwipeRepeatHoldBackend(self)
        return sendevent

    def _probe_motion_event(self) -> bool:
        """Best-effort capability probe for `input motionevent`.

        There is no clean query: `input --help` is not portable and the
        subcommand exists only on newer Android. `input` with no subcommand
        prints the usage text, which lists `motionevent` exactly on builds that
        have it. The second probe (`input motionevent` with no coordinates) is a
        backstop for vendors that print usage only when the subcommand is
        reached; it cannot inject a gesture because DOWN/MOVE/UP and x/y are
        missing. Output is matched case-insensitively because stderr wording
        varies.
        """
        for probe in (["shell", "input"], ["shell", "input", "motionevent"]):
            result = self._run(probe, allow_failure=True)
            text = result.stdout.decode("utf-8", "replace") + "\n" + result.stderr
            if "motionevent" in text.lower():
                return True
        return False

    def touch_down(self, x: int, y: int) -> None:
        """Start a persistent hold at (x, y) using the auto-selected backend."""
        self.make_hold_backend("auto").down(int(x), int(y))

    def touch_move(self, x: int, y: int) -> None:
        """Drag the currently held finger to (x, y)."""
        self.make_hold_backend("auto").move(int(x), int(y))

    def touch_up(self, x: int, y: int) -> None:
        """Release the persistent hold at (x, y)."""
        self.make_hold_backend("auto").up(int(x), int(y))

    def back(self) -> float:
        return self._run(["shell", "input", "keyevent", str(KEYCODE_BACK)]).duration_ms

    def display_size(self) -> tuple[int, int]:
        """Return the *current* (rotated) display size, i.e. framebuffer pixels."""
        out = self._run(["shell", "wm", "size"]).stdout.decode("utf-8", "replace")
        for line in out.splitlines():
            if ":" in line and "x" in line:
                try:
                    dims = line.split(":", 1)[1].strip()
                    w, h = dims.split("x", 1)
                    return int(w), int(h)
                except ValueError:
                    continue
        raise AdbError(f"could not parse `wm size` output: {out!r}")

    def current_focus(self) -> str:
        out = self._run(["shell", "dumpsys", "window"]).stdout.decode("utf-8", "replace")
        for line in out.splitlines():
            if "mCurrentFocus=" in line:
                return line.split("mCurrentFocus=", 1)[1].strip()
        return ""

    def package_running(self, package: str) -> bool:
        # `pidof` exits non-zero when the process is absent; that is a False,
        # not an error.
        result = self._run(["shell", "pidof", package], allow_failure=True)
        return bool(result.stdout.decode("utf-8", "replace").strip())
