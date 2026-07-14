from __future__ import annotations

from pathlib import Path

import pytest

from server.project_tools import ProjectToolError, ProjectTools


def test_stub_run_produces_numerical_and_visual_evidence(tmp_path: Path) -> None:
    tools = ProjectTools(tmp_path / "project", simulator=object())

    result = tools.run_project(duration_s=3, mode="batch", scenario="default")

    assert result["status"] == "task_success"
    assert result["evidence"]["fallback"] is True
    assert tools.get_run_summary(result["run_id"])["run_id"] == result["run_id"]
    telemetry = tools.inspect_telemetry(
        result["run_id"], ["dist_to_target_m", "left_wheel_velocity_rad_s"], start_s=1, end_s=2
    )
    assert telemetry["samples"]
    assert all(1 <= sample["t"] <= 2 for sample in telemetry["samples"])
    frame = tools.get_visual_frame(result["run_id"], "robot_rgb", t=2.9)
    assert frame["path"].startswith("runs/")
    assert (tmp_path / "project" / frame["path"]).read_bytes().startswith(b"\xff\xd8")


def test_stub_failure_exposes_a_clear_stall_signature(tmp_path: Path) -> None:
    tools = ProjectTools(tmp_path, simulator=object())
    result = tools.run_project(2, "batch", "seeded_wrong_joint_failure")

    assert result["status"] == "task_failed"
    telemetry = tools.inspect_telemetry(
        result["run_id"], ["left_command_rad_s", "left_wheel_velocity_rad_s"]
    )
    assert all(point["signals"]["left_command_rad_s"] > 0 for point in telemetry["samples"])
    assert all(point["signals"]["left_wheel_velocity_rad_s"] == 0 for point in telemetry["samples"])


def test_compare_runs_reports_candidate_minus_base_deltas(tmp_path: Path) -> None:
    tools = ProjectTools(tmp_path, simulator=object())
    failed = tools.run_project(2, "batch", "stall failure")
    passed = tools.run_project(2, "batch", "default")

    comparison = tools.compare_runs(failed["run_id"], passed["run_id"])

    assert comparison["metrics"]["distance_to_target_m"]["delta"] < 0
    assert comparison["candidate_status"] == "task_success"


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("get_run_summary", ("../../etc",)),
        ("get_visual_frame", ("run_stub_0001", "../robot_rgb")),
        ("inspect_telemetry", ("run_stub_0001", ["../../secret"])),
    ],
)
def test_paths_and_signal_names_are_capability_limited(tmp_path: Path, method: str, args: tuple[object, ...]) -> None:
    tools = ProjectTools(tmp_path)
    with pytest.raises(ProjectToolError):
        getattr(tools, method)(*args)


def test_tool_rejects_invalid_run_parameters(tmp_path: Path) -> None:
    tools = ProjectTools(tmp_path)
    with pytest.raises(ProjectToolError):
        tools.run_project(0, "batch", "default")
    with pytest.raises(ProjectToolError):
        tools.run_project(1, "interactive", "default")
