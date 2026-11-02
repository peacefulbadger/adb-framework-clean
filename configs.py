"""Configuration loader for mapping.toml and sidecar.toml.

Reads the TOML config files and provides typed accessors with defaults.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path


def _load_toml(path: Path | None) -> dict:
    """Load a TOML file, returning an empty dict if the path is None or missing."""
    if path is None or not path.is_file():
        return {}
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    with path.open("rb") as fh:
        return tomllib.load(fh)


@dataclass(frozen=True, slots=True)
class SidecarConfig:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass(frozen=True, slots=True)
class EmulatorConfig:
    kind: str = "mumu"
    resolution: tuple[int, int] = (1920, 1080)


@dataclass(frozen=True, slots=True)
class MappingConfig:
    budget_s: float = 600.0
    cell_size_m: float = 0.25
    probe_ms: int = 300
    probe_displacement_min: float = 0.05
    stuck_ms: float = 1500
    frame_timeout_ms: float = 2000
    frontier_min_gain_cells: int = 4
    loop_closure_min_matches: int = 30
    checkpoint_interval_s: float = 15.0
    keyframe_spacing_m: float = 0.5
    camera_px_per_deg: float = 4.0
    calibrated_meters_per_pixel: float = 80.0
    camera_settle_s: float = 5.0


@dataclass(frozen=True, slots=True)
class RecoveryConfig:
    reset_app_path: str = "emulator_control/reset_app.py"
    resume_from_checkpoint: bool = True


@dataclass(frozen=True, slots=True)
class AppConfig:
    sidecar: SidecarConfig = field(default_factory=SidecarConfig)
    emulator: EmulatorConfig = field(default_factory=EmulatorConfig)
    mapping: MappingConfig = field(default_factory=MappingConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)

    # HUD mask regions per resolution: list of [x, y, w, h] rectangles.
    hud_mask_regions: list[list[int]] = field(default_factory=list)

    @classmethod
    def load(cls, config_dir: Path | str = "configs") -> AppConfig:
        """Load configuration from the given directory.

        Looks for ``mapping.toml`` (primary) and falls back to ``sidecar.toml``
        for sidecar-specific settings.
        """
        config_dir = Path(config_dir)
        mapping_cfg = _load_toml(config_dir / "mapping.toml")
        sidecar_cfg = _load_toml(config_dir / "sidecar.toml")

        # Merge: mapping.toml takes precedence; sidecar.toml fills gaps.
        merged: dict = {}
        merged.update(sidecar_cfg)
        merged.update(mapping_cfg)

        sidecar_section = merged.get("sidecar", {})
        emulator_section = merged.get("emulator", {})
        mapping_section = merged.get("mapping", {})
        recovery_section = merged.get("recovery", {})

        # Joystick section from sidecar.toml
        joystick_section = merged.get("joystick", {})

        return cls(
            sidecar=SidecarConfig(
                host=str(sidecar_section.get("host", "127.0.0.1")),
                port=int(sidecar_section.get("port", 8765)),
            ),
            emulator=EmulatorConfig(
                kind=str(emulator_section.get("kind", "mumu")),
                resolution=tuple(int(x) for x in emulator_section.get("resolution", [1920, 1080])),
            ),
            mapping=MappingConfig(
                budget_s=float(mapping_section.get("budget_s", 600)),
                cell_size_m=float(mapping_section.get("cell_size_m", 0.25)),
                probe_ms=int(mapping_section.get("probe_ms", 300)),
                probe_displacement_min=float(mapping_section.get("probe_displacement_min", 0.05)),
                stuck_ms=int(mapping_section.get("stuck_ms", 1500)),
                frame_timeout_ms=float(mapping_section.get("frame_timeout_ms", 2000)),
                frontier_min_gain_cells=int(mapping_section.get("frontier_min_gain_cells", 4)),
                loop_closure_min_matches=int(mapping_section.get("loop_closure_min_matches", 30)),
                checkpoint_interval_s=float(mapping_section.get("checkpoint_interval_s", 15.0)),
                keyframe_spacing_m=float(mapping_section.get("keyframe_spacing_m", 0.5)),
                camera_px_per_deg=float(joystick_section.get("camera_px_per_deg", 4.0)),
            ),
            recovery=RecoveryConfig(
                reset_app_path=str(recovery_section.get("reset_app_path", "emulator_control/reset_app.py")),
                resume_from_checkpoint=bool(recovery_section.get("resume_from_checkpoint", True)),
            ),
            hud_mask_regions=[
                list(map(int, r)) for r in mapping_section.get("hud_mask_regions", [])
            ],
        )


def joystick_point(
    center: tuple[int, int], radius: float, dir_deg: float, magnitude: float = 1.0
) -> tuple[int, int]:
    """Map (direction, magnitude) to a framebuffer point.

    0 degrees is up/forward and angles increase counter-clockwise *on screen*,
    so the screen-space offset is ``(-sin, -cos)`` (screen y grows down).
    """
    radians = math.radians(float(dir_deg) % 360.0)
    x = center[0] - radius * magnitude * math.sin(radians)
    y = center[1] - radius * magnitude * math.cos(radians)
    return int(round(x)), int(round(y))
