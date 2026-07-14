from __future__ import annotations

import json
from pathlib import Path

from server.main import _project_preview
from server.session_store import ProjectStore


def test_only_matching_simulator_adapter_is_marked_runnable(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "sessions")
    rover = store.get_or_create("rover-demo")
    hand = store.create_fresh_project("robotic-hand")[0]
    (hand.workspace / "spec.json").write_text(
        json.dumps({"name": "Five finger hand", "morphology": "robotic_hand"}), encoding="utf-8"
    )

    assert _project_preview(rover)["can_run"] is True
    preview = _project_preview(hand)
    assert preview["can_run"] is False
    assert preview["morphology"] == "robotic_hand"
    assert "adapter" in preview["message"]
