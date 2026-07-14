"""Deterministic morphology generation for RoboPilot project adapters.

The simulator depends only on a small, explicit differential-drive ABI:
``base_link``, ``left_wheel_joint``, ``right_wheel_joint`` and ``camera_link``.
Changing morphology is therefore a spec update followed by regeneration rather
than a free-form edit to a generated URDF.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from jinja2 import Environment, FileSystemLoader, StrictUndefined


TEMPLATE_DIR = Path(__file__).with_name("templates")
DEFAULT_SPEC: dict[str, Any] = {
    "name": "red_ball_rover",
    "morphology": {
        "type": "differential_drive",
        "body_length_m": 0.42,
        "body_width_m": 0.30,
        "body_height_m": 0.12,
        "body_mass_kg": 2.4,
        "wheel_radius_m": 0.075,
        "wheel_width_m": 0.045,
        "wheel_mass_kg": 0.16,
        "wheel_track_m": 0.31,
        "camera_height_m": 0.11,
    },
    "sensors": ["robot_rgb", "odometry", "joint_state"],
    "task": {"type": "drive_to_target", "target": "red_ball"},
}


def _positive(name: str, value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if numeric <= 0:
        raise ValueError(f"{name} must be positive")
    return numeric


def normalize_spec(spec: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a validated rover spec with stable defaults.

    Only morphology fields necessary for the golden adapter are accepted here;
    extra project/task fields survive unchanged for the rest of the platform.
    """

    incoming = dict(spec or {})
    morphology_in = incoming.get("morphology") or {}
    if not isinstance(morphology_in, Mapping):
        raise ValueError("morphology must be an object")
    if morphology_in.get("type", "differential_drive") != "differential_drive":
        raise ValueError("only differential_drive morphology is supported")

    defaults = DEFAULT_SPEC["morphology"]
    morphology = {**defaults, **dict(morphology_in), "type": "differential_drive"}
    aliases = {
        "body_length_m": "body_length_m",
        "body_width_m": "body_width_m",
        "body_height_m": "body_height_m",
        "body_mass_kg": "body_mass_kg",
        "wheel_radius_m": "wheel_radius_m",
        "wheel_width_m": "wheel_width_m",
        "wheel_mass_kg": "wheel_mass_kg",
        "wheel_track_m": "wheel_track_m",
        "camera_height_m": "camera_height_m",
    }
    for key, label in aliases.items():
        morphology[key] = _positive(label, morphology[key])
    if morphology["wheel_track_m"] <= morphology["wheel_width_m"]:
        raise ValueError("wheel_track_m must exceed wheel_width_m")

    merged: dict[str, Any] = {**DEFAULT_SPEC, **incoming}
    merged["name"] = str(incoming.get("name") or DEFAULT_SPEC["name"])
    merged["morphology"] = morphology
    return merged


def render_diff_drive_urdf(spec: Mapping[str, Any] | None = None) -> str:
    """Render the canonical rover URDF from a validated project spec."""

    normalized = normalize_spec(spec)
    m = normalized["morphology"]
    body_height = m["body_height_m"]
    wheel_radius = m["wheel_radius_m"]
    context = {
        "robot_name": normalized["name"],
        "body_length": m["body_length_m"],
        "body_width": m["body_width_m"],
        "body_height": body_height,
        "body_mass": m["body_mass_kg"],
        "wheel_radius": wheel_radius,
        "wheel_width": m["wheel_width_m"],
        "wheel_mass": m["wheel_mass_kg"],
        "wheel_y": m["wheel_track_m"] / 2.0,
        # Wheel centres are below base_link; base pose is set by the simulator.
        "wheel_z": -(body_height / 2.0),
        "camera_x": m["body_length_m"] * 0.35,
        "camera_z": m["camera_height_m"],
    }
    environment = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )
    return environment.get_template("diff_drive.urdf.j2").render(**context)


def write_morphology(workspace: str | Path, spec: Mapping[str, Any] | None = None) -> Path:
    """Write ``spec.json`` and the generated ``robot.urdf`` into a workspace."""

    directory = Path(workspace).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    normalized = normalize_spec(spec)
    (directory / "spec.json").write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")
    urdf_path = directory / "robot.urdf"
    urdf_path.write_text(render_diff_drive_urdf(normalized), encoding="utf-8")
    return urdf_path
