"""Emit a deterministic WebSocket-shaped event stream for UI/manual testing.

The browser uses ``mock_ws.js`` because browsers require JavaScript MIME types. This
small companion is useful when a JSONL recording is needed for a server-side demo.
"""

from __future__ import annotations

import json
from typing import Iterator


def events(project_id: str = "rover-demo") -> Iterator[dict]:
    """Yield a compact, contract-compatible successful rover run."""
    seq = 1
    for stage in ("spec", "morphology", "build", "observe"):
        yield {"type": "stage", "project_id": project_id, "run_id": "run_12", "seq": seq, "stage": stage, "status": "start", "note": f"Starting {stage}."}
        seq += 1
        yield {"type": "stage", "project_id": project_id, "run_id": "run_12", "seq": seq, "stage": stage, "status": "done", "note": "Complete."}
        seq += 1
    for index in range(0, 81, 8):
        t = index / 8
        yield {"type": "sim_state", "project_id": project_id, "run_id": "run_12", "seq": seq, "t": t, "base_pos": [-2.55 + index / 80 * 4.65, 0.0, 0.12], "base_quat": [0, 0, 0.027, 0.999], "joints": {"left_wheel": 7.8, "right_wheel": 8.05}}
        seq += 1
        yield {"type": "telemetry", "project_id": project_id, "run_id": "run_12", "seq": seq, "t": t, "series": {"dist_to_target": max(0.17, 4.72 - index * 0.057), "yaw_drift": 2.7}}
        seq += 1


if __name__ == "__main__":
    for event in events():
        print(json.dumps(event))
