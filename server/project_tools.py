"""Capability-limited project evidence tools for RoboPilot agents.

The agent never receives an arbitrary filesystem or shell capability through this
module.  Every artifact is rooted in one project session and is addressed by a
run id, signal name, camera name, or timestamp.  The concrete PyBullet adapter
can be dropped in later; until then the deterministic fallback makes the rest
of the platform usable and testable.
"""

from __future__ import annotations

import importlib
import json
import math
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


MIN_DURATION_S = 0.1
MAX_DURATION_S = 120.0
MAX_SIGNALS = 8
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_RUN_PREFIX = "run_"

# A tiny valid JPEG.  A real simulator overwrites these fallback frames with
# robot-camera images; having an image in the fallback keeps the visual tool
# contract stable for the UI and Codex image input path.
_FALLBACK_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb00430008060607060508"
    "070707090909080a0c140d0c0b0b0c19120f131d1a1f1e1d1a1c1c20242e2720"
    "222c231c1c28372d2c30313434341f27393d38323c2e333432ffc0000b080001"
    "000101011100ffc40014000100000000000000000000000000000000ffc40014"
    "10010000000000000000000000000000000000ffda0008010100003f00d2cf20"
    "ffd9"
)


class ProjectToolError(ValueError):
    """Raised for invalid agent tool arguments or unavailable run artifacts."""


def _as_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProjectToolError(f"{name} must be a number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ProjectToolError(f"{name} must be finite")
    return numeric


def _safe_name(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
        raise ProjectToolError(f"{name} contains unsupported characters")
    return value


class ProjectTools:
    """Session-rooted implementation of the frozen project-tools contract.

    ``simulator`` is optional and deliberately small: an adapter may expose a
    ``run_project(session_root=..., duration_s=..., mode=..., scenario=...)``
    method/function that returns the canonical run summary.  If it is absent or
    fails to load, ``run_project`` writes a deterministic local run record.
    """

    def __init__(self, session_root: str | Path, simulator: Any | None = None) -> None:
        self.session_root = Path(session_root).expanduser().resolve()
        self.session_root.mkdir(parents=True, exist_ok=True)
        self.runs_root = self._resolve_within("runs")
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self._simulator = simulator

    def run_project(self, duration_s: float, mode: str, scenario: str) -> dict[str, Any]:
        """Run through the installed simulator, or create a deterministic stub run."""
        duration = _as_number(duration_s, "duration_s")
        if not MIN_DURATION_S <= duration <= MAX_DURATION_S:
            raise ProjectToolError(
                f"duration_s must be between {MIN_DURATION_S} and {MAX_DURATION_S}"
            )
        if mode not in {"batch", "live"}:
            raise ProjectToolError("mode must be 'batch' or 'live'")
        if not isinstance(scenario, str) or not scenario or len(scenario) > 80:
            raise ProjectToolError("scenario must be a non-empty string up to 80 characters")

        simulator = self._simulator if self._simulator is not None else self._load_simulator()
        if simulator is not None:
            try:
                result = self._invoke_simulator(simulator, duration, mode, scenario)
                if isinstance(result, Mapping):
                    return self._normalise_external_summary(dict(result))
            except (ImportError, ModuleNotFoundError, AttributeError, NotImplementedError):
                # The sim core is optional while the platform skeleton is being
                # assembled.  Its deterministic evidence fallback is intentional.
                pass
            except RuntimeError as exc:
                # ``sim_tool`` can be importable while PyBullet is not yet
                # installed.  Treat a missing optional simulator dependency as
                # unavailable, but do not hide ordinary simulator failures.
                if not isinstance(exc.__cause__, ModuleNotFoundError):
                    raise
        return self._write_fallback_run(duration, mode, scenario)

    def get_run_summary(self, run_id: str) -> dict[str, Any]:
        """Return the compact summary written by the simulator for ``run_id``."""
        run_dir = self._run_dir(run_id)
        summary_path = self._artifact_path(run_dir, "summary.json")
        if not summary_path.is_file():
            # ``sim_tool`` calls this canonical record ``run_record.json``.
            summary_path = self._artifact_path(run_dir, "run_record.json")
        if not summary_path.is_file():
            raise ProjectToolError(f"summary is unavailable for run_id '{run_id}'")
        return self._read_json(summary_path, "summary")

    def inspect_telemetry(
        self,
        run_id: str,
        signals: Iterable[str],
        start_s: float | None = None,
        end_s: float | None = None,
    ) -> dict[str, Any]:
        """Return a bounded, timestamped numerical signal window.

        The raw json-lines record remains local.  This operation caps requested
        signal names and returns at most 500 samples so an agent cannot turn an
        observation request into an unbounded context payload.
        """
        run_dir = self._run_dir(run_id)
        requested = list(signals) if not isinstance(signals, str) else []
        if not requested or len(requested) > MAX_SIGNALS:
            raise ProjectToolError(f"signals must contain 1 to {MAX_SIGNALS} names")
        requested = [_safe_name(signal, "signal") for signal in requested]
        if len(set(requested)) != len(requested):
            raise ProjectToolError("signals must not contain duplicates")

        start = 0.0 if start_s is None else _as_number(start_s, "start_s")
        end = math.inf if end_s is None else _as_number(end_s, "end_s")
        if start < 0 or end < start:
            raise ProjectToolError("telemetry window must satisfy 0 <= start_s <= end_s")

        signals_path = self._artifact_path(run_dir, "signals.jsonl")
        if not signals_path.is_file():
            raise ProjectToolError(f"telemetry is unavailable for run_id '{run_id}'")
        samples: list[dict[str, Any]] = []
        available: set[str] = set()
        with signals_path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                point = json.loads(line)
                t = _as_number(point.get("t"), "telemetry timestamp")
                point_signals = point.get("signals")
                # Sim adapters may persist a compact flat jsonl row.  Convert
                # it at this boundary so Codex always gets the frozen nested
                # response shape without duplicating raw evidence on disk.
                if point_signals is None:
                    point_signals = {key: value for key, value in point.items() if key != "t"}
                if not isinstance(point_signals, Mapping):
                    continue
                available.update(str(key) for key in point_signals)
                if start <= t <= end and len(samples) < 500:
                    samples.append(
                        {
                            "t": t,
                            "signals": {
                                name: point_signals[name]
                                for name in requested
                                if name in point_signals
                            },
                        }
                    )
        unknown = [name for name in requested if name not in available]
        if unknown:
            raise ProjectToolError(f"unknown telemetry signals: {', '.join(unknown)}")
        return {
            "run_id": run_id,
            "window": {"start_s": start, "end_s": None if math.isinf(end) else end},
            "signals": requested,
            "samples": samples,
            "sample_limit": 500,
        }

    def get_visual_frame(self, run_id: str, camera: str, t: float | None = None) -> dict[str, Any]:
        """Return the nearest session-local visual-evidence path for one camera."""
        run_dir = self._run_dir(run_id)
        camera_name = _safe_name(camera, "camera")
        requested_t = None if t is None else _as_number(t, "t")
        if requested_t is not None and requested_t < 0:
            raise ProjectToolError("t must be non-negative")
        index_path = self._artifact_path(run_dir, "frames", "index.json")
        if index_path.is_file():
            frames = self._read_json(index_path, "frame index")
        else:
            # The PyBullet core keeps frame references in its durable run
            # record instead of a separate index; both layouts are supported.
            frames = self.get_run_summary(run_id).get("evidence", {}).get("frames", [])
        if not isinstance(frames, list):
            raise ProjectToolError("frame index is malformed")
        candidates = [frame for frame in frames if frame.get("camera") == camera_name]
        if not candidates:
            raise ProjectToolError(f"camera '{camera_name}' is unavailable")
        chosen = (
            max(candidates, key=lambda frame: _as_number(frame.get("t"), "frame timestamp"))
            if requested_t is None
            else min(
                candidates,
                key=lambda frame: abs(_as_number(frame.get("t"), "frame timestamp") - requested_t),
            )
        )
        relative = chosen.get("path")
        if not isinstance(relative, str):
            raise ProjectToolError("frame index is malformed")
        path = self._resolve_within(relative)
        if not path.is_file() or run_dir not in path.parents:
            raise ProjectToolError("frame path is invalid or unavailable")
        return {
            "run_id": run_id,
            "camera": camera_name,
            "t": _as_number(chosen["t"], "frame timestamp"),
            "path": self._relative(path),
            "source": chosen.get("source", "robot_sensor"),
        }

    def compare_runs(self, base_run_id: str, candidate_run_id: str) -> dict[str, Any]:
        """Compare stable task metrics, with candidate-minus-base deltas."""
        base = self.get_run_summary(base_run_id)
        candidate = self.get_run_summary(candidate_run_id)
        keys = ("distance_to_target_m", "collisions", "max_tilt_deg", "sim_time_s")
        base_metrics = base.get("telemetry_summary", {})
        candidate_metrics = candidate.get("telemetry_summary", {})
        metrics: dict[str, dict[str, float | None]] = {}
        for key in keys:
            before = base_metrics.get(key)
            after = candidate_metrics.get(key)
            if isinstance(before, (int, float)) and isinstance(after, (int, float)):
                metrics[key] = {"base": before, "candidate": after, "delta": after - before}
            else:
                metrics[key] = {"base": None, "candidate": None, "delta": None}
        return {
            "base_run_id": base_run_id,
            "candidate_run_id": candidate_run_id,
            "base_status": base.get("status"),
            "candidate_status": candidate.get("status"),
            "metrics": metrics,
        }

    def _load_simulator(self) -> Any | None:
        try:
            return importlib.import_module("server.sim_tool")
        except ModuleNotFoundError as exc:
            if exc.name in {"server.sim_tool", "sim_tool"}:
                return None
            raise

    def _invoke_simulator(
        self, simulator: Any, duration_s: float, mode: str, scenario: str
    ) -> Any:
        runner: Callable[..., Any] | None = getattr(simulator, "run_project", None)
        if runner is None and callable(simulator):
            runner = simulator
        if runner is None:
            raise AttributeError("simulator has no run_project callable")
        try:
            return runner(
                session_root=self.session_root,
                duration_s=duration_s,
                mode=mode,
                scenario=scenario,
            )
        except TypeError as exc:
            # The standalone simulator's public ABI names its root
            # ``workspace``.  Retain support while the platform-level adapter
            # uses the more explicit session-root name.
            if "session_root" not in str(exc):
                raise
            return runner(
                workspace=self.session_root,
                duration_s=duration_s,
                mode=mode,
                scenario=scenario,
            )

    def _normalise_external_summary(self, summary: dict[str, Any]) -> dict[str, Any]:
        run_id = summary.get("run_id")
        if isinstance(run_id, str):
            run_dir = self._run_dir(run_id)  # validates both id and session confinement
            evidence = summary.get("evidence")
            if isinstance(evidence, Mapping):
                normalised_evidence = dict(evidence)
                frames = evidence.get("frames")
                if isinstance(frames, list):
                    normalised_frames: list[Any] = []
                    for frame in frames:
                        if isinstance(frame, Mapping) and isinstance(frame.get("path"), str):
                            safe_path = self._resolve_within(frame["path"])
                            if safe_path.is_file() and run_dir in safe_path.parents:
                                normalised_frames.append({**frame, "path": self._relative(safe_path)})
                                continue
                        normalised_frames.append(frame)
                    normalised_evidence["frames"] = normalised_frames
                signals_path = evidence.get("signals_path")
                if isinstance(signals_path, str):
                    safe_signals = self._resolve_within(signals_path)
                    if safe_signals.is_file() and run_dir in safe_signals.parents:
                        normalised_evidence["signals_path"] = self._relative(safe_signals)
                summary["evidence"] = normalised_evidence
        return summary

    def _write_fallback_run(self, duration_s: float, mode: str, scenario: str) -> dict[str, Any]:
        run_id = self._next_stub_run_id()
        run_dir = self._run_dir(run_id)
        frames_dir = self._artifact_path(run_dir, "frames")
        frames_dir.mkdir(parents=True, exist_ok=False)
        failing = any(word in scenario.lower() for word in ("failure", "stall", "wrong_joint"))
        final_distance = 1.5 if failing else 0.18
        status = "task_failed" if failing else "task_success"
        final_t = round(duration_s, 3)
        points = self._fallback_points(final_t, final_distance, failing)
        signals_path = self._artifact_path(run_dir, "signals.jsonl")
        with signals_path.open("w", encoding="utf-8") as stream:
            for point in points:
                stream.write(json.dumps(point, separators=(",", ":")) + "\n")
        frame_entries: list[dict[str, Any]] = []
        for label, timestamp in (("start", 0.0), ("final", final_t)):
            frame_path = self._artifact_path(frames_dir, f"{label}.jpg")
            frame_path.write_bytes(_FALLBACK_JPEG)
            frame_entries.append(
                {
                    "camera": "robot_rgb",
                    "t": timestamp,
                    "path": self._relative(frame_path),
                    "source": "robot_sensor",
                }
            )
        self._artifact_path(frames_dir, "index.json").write_text(
            json.dumps(frame_entries, indent=2), encoding="utf-8"
        )
        summary = {
            "status": status,
            "stderr": "stub simulator: wheel motion stalled" if failing else "",
            "telemetry_summary": {
                "final_base_pose": {"xyz": [1.32 if not failing else 0.0, 0.0, 0.1], "rpy": [0.0, 0.0, 0.0]},
                "distance_to_target_m": final_distance,
                "total_yaw_drift_deg": 0.0,
                "mean_wheel_velocities": {"left": 0.0 if failing else 8.0, "right": 0.0 if failing else 8.0},
                "collisions": 0,
                "max_tilt_deg": 0.0,
                "sim_time_s": final_t,
            },
            "run_id": run_id,
            "artifact_revision": "stub",
            "mode": mode,
            "scenario": scenario,
            "evidence": {
                "frames": [frame_entries[-1]],
                "signals_path": self._relative(signals_path),
                "fallback": True,
            },
        }
        self._artifact_path(run_dir, "summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        return summary

    @staticmethod
    def _fallback_points(final_t: float, final_distance: float, failing: bool) -> list[dict[str, Any]]:
        samples = max(2, min(21, int(math.ceil(final_t)) + 1))
        result: list[dict[str, Any]] = []
        for index in range(samples):
            ratio = index / (samples - 1)
            t = round(final_t * ratio, 3)
            distance = 1.5 if failing else round(1.5 - (1.5 - final_distance) * ratio, 4)
            wheel_velocity = 0.0 if failing else 8.0
            result.append(
                {
                    "t": t,
                    "signals": {
                        "dist_to_target_m": distance,
                        "left_command_rad_s": 8.0,
                        "right_command_rad_s": 8.0,
                        "left_wheel_velocity_rad_s": wheel_velocity,
                        "right_wheel_velocity_rad_s": wheel_velocity,
                        "yaw_drift_deg": 0.0,
                    },
                }
            )
        return result

    def _next_stub_run_id(self) -> str:
        highest = 0
        for child in self.runs_root.iterdir():
            match = re.fullmatch(r"run_stub_(\d{4})", child.name)
            if match:
                highest = max(highest, int(match.group(1)))
        return f"run_stub_{highest + 1:04d}"

    def _run_dir(self, run_id: str) -> Path:
        name = _safe_name(run_id, "run_id")
        if not name.startswith(_RUN_PREFIX):
            raise ProjectToolError("run_id must start with 'run_'")
        path = self._resolve_within("runs", name)
        if path.exists() and not path.is_dir():
            raise ProjectToolError("run path is not a directory")
        return path

    def _artifact_path(self, base: Path, *parts: str) -> Path:
        path = base.joinpath(*parts).resolve()
        if base != path and base not in path.parents:
            raise ProjectToolError("artifact path escapes its run directory")
        return path

    def _resolve_within(self, *parts: str) -> Path:
        path = self.session_root.joinpath(*parts).resolve()
        if path != self.session_root and self.session_root not in path.parents:
            raise ProjectToolError("path escapes session root")
        return path

    def _relative(self, path: Path) -> str:
        safe_path = path.resolve()
        if self.session_root not in safe_path.parents:
            raise ProjectToolError("path escapes session root")
        return safe_path.relative_to(self.session_root).as_posix()

    @staticmethod
    def _read_json(path: Path, label: str) -> Any:
        try:
            with path.open(encoding="utf-8") as stream:
                return json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectToolError(f"{label} is malformed") from exc


def create_project_tools(session_root: str | Path, simulator: Any | None = None) -> ProjectTools:
    """Small factory used by the FastAPI session and Codex bridge layers."""
    return ProjectTools(session_root=session_root, simulator=simulator)
