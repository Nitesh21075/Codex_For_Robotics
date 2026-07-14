"""Focused acceptance tests for the deterministic rover simulator."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from server.morphology import render_diff_drive_urdf, write_morphology


class MorphologyTests(unittest.TestCase):
    def test_template_has_simulator_joint_abi(self) -> None:
        urdf = render_diff_drive_urdf({"morphology": {"wheel_radius_m": 0.1}})
        self.assertIn('name="left_wheel_joint"', urdf)
        self.assertIn('name="right_wheel_joint"', urdf)
        self.assertIn('name="camera_link"', urdf)
        self.assertIn('radius="0.1"', urdf)


@unittest.skipUnless(importlib.util.find_spec("pybullet"), "PyBullet is not installed")
class SimulatorTests(unittest.TestCase):
    def test_baseline_controller_creates_canonical_success_record(self) -> None:
        from server.sim_tool import create_demo_project, run_simulation

        with tempfile.TemporaryDirectory() as directory:
            workspace = create_demo_project(directory)
            record = run_simulation(workspace, duration_s=6.0)
            self.assertEqual(record["status"], "task_success")
            self.assertLess(record["telemetry_summary"]["distance_to_target_m"], 0.3)
            self.assertEqual(record["telemetry_summary"]["collisions"], 0)
            self.assertGreaterEqual(len(record["evidence"]["frames"]), 3)
            for path in (record["record_path"], record["evidence"]["signals_path"], record["state_samples_path"]):
                self.assertTrue(Path(path).exists(), path)
            saved = json.loads(Path(record["record_path"]).read_text(encoding="utf-8"))
            self.assertEqual(saved["run_id"], record["run_id"])

    def test_rail_train_adapter_writes_canonical_evidence(self) -> None:
        from server.sim_tool import run_simulation

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "spec.json").write_text(json.dumps({"name": "test train", "morphology": "rail_train"}), encoding="utf-8")
            (workspace / "mission.json").write_text(json.dumps({"route": [{"node": "depot", "distance_m": 0}, {"node": "siding", "distance_m": 0.1}]}), encoding="utf-8")
            (workspace / "train_controller.py").write_text("def compute_train_command(observation):\n    return {'throttle': 1.0, 'brake': 0.0}\n", encoding="utf-8")
            record = run_simulation(workspace, duration_s=0.5)
            self.assertEqual(record["adapter"], "rail_train_kinematic")
            self.assertEqual(record["status"], "task_success")
            self.assertGreaterEqual(len(record["evidence"]["frames"]), 2)
            self.assertTrue(Path(record["record_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
