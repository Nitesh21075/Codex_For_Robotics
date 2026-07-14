from __future__ import annotations

import json
from pathlib import Path

from server.main import _project_preview
from server.session_store import ProjectStore


def test_matching_simulator_adapters_are_marked_runnable(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "sessions")
    rover = store.get_or_create("rover-demo")
    hand = store.create_fresh_project("robotic-hand")[0]
    (hand.workspace / "spec.json").write_text(
        json.dumps({"name": "Five finger hand", "morphology": "robotic_hand"}), encoding="utf-8"
    )

    assert _project_preview(rover)["can_run"] is True
    preview = _project_preview(hand)
    assert preview["can_run"] is True
    assert preview["morphology"] == "robotic_hand"
    assert "adapter" in preview["message"]

    train = store.create_fresh_project("rail-train")[0]
    (train.workspace / "spec.json").write_text(
        json.dumps({"name": "Short train", "morphology": "rail_train"}), encoding="utf-8"
    )
    assert _project_preview(train)["can_run"] is True

    (hand.workspace / "spec.json").write_text(json.dumps({"morphology": "underwater_vehicle"}), encoding="utf-8")
    assert _project_preview(hand)["can_run"] is False


def test_validated_project_local_adapter_is_runnable_but_draft_is_not(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "sessions")
    project = store.create_fresh_project("monorail")[0]
    (project.workspace / "spec.json").write_text(
        json.dumps({"morphology": "airport_monorail"}), encoding="utf-8"
    )
    (project.workspace / "adapter.json").write_text(
        json.dumps({"morphology": "airport_monorail", "family": "rail_train_kinematic", "status": "draft"}),
        encoding="utf-8",
    )
    assert _project_preview(project)["can_run"] is False
    assert "awaiting validation" in _project_preview(project)["message"]

    (project.workspace / "adapter.json").write_text(
        json.dumps({"morphology": "airport_monorail", "family": "rail_train_kinematic", "status": "validated"}),
        encoding="utf-8",
    )
    assert _project_preview(project)["can_run"] is True
