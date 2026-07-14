# PRD — RoboPilot: "v0 for Robotics"

**One-liner:** Describe a robotics project in English → watch it work in simulation in minutes, with a persistent AI coding agent that sees the system, debugs its own code, and iterates conversationally.

**Build context:** Hackathon build, live demo on own laptop. Development environment: WSL2 (Ubuntu) on Windows. All code, execution, and files live in the WSL2 Linux filesystem. NEVER read/write via `/mnt/c/`. UI is viewed in a Windows browser at `localhost` (WSL2 auto-forwards ports).

---

## 1. Assumptions (edit if wrong)

- Demo is live; a cached fallback video/session must exist.
- Judges are mixed technical/business.
- API: Codex SDK is the intelligence layer. It runs a persistent Codex thread per project session and is configured with a server-side `CODEX_API_KEY`; the browser never receives a key.
- ROS 2 package is generated as an export artifact (file tree) only — never executed live.
- The differential-drive rover is the first project adapter and benchmark, not the product boundary. Later adapters may build against other simulators or ROS 2.
- Continuous conversation, user edits, and on-demand numerical/visual inspection are in scope.

## 2. Problem (for pitch, not for build)

The idea→prototype loop in robotics takes weeks (URDF authoring, ROS boilerplate, sim setup, controller tuning). Software collapsed this loop with v0/Copilot; robotics never got that collapse. RoboPilot is that collapse: generative pipeline + a coding agent that has *eyes into the physics simulation*.

**Differentiator vs Cursor/Claude Code:** general coding agents edit files blind. RoboPilot's agent receives simulation telemetry and robot-camera frames as tool output, so it debugs physical behavior ("drifts left") not just syntax.

## 3. Scope

### In scope (v1)
1. Persistent project chat: English prompt → structured project/robot spec (JSON), then ongoing user messages against the same workspace and Codex thread.
2. Spec → parameterized URDF from a template library (differential-drive rover with camera = golden path; 4-DOF arm template = stretch).
3. Codex SDK agent creates and changes project files for the task, including a Python controller.
4. PyBullet simulation is available to the agent through on-demand project tools; it returns telemetry plus selected visual evidence.
5. Autonomous self-heal loop: agent reads sim errors/telemetry/selected visual evidence, patches files, and re-runs within an operational budget.
6. Iterate loop: user sends follow-up messages ("make the wheels bigger", "why does it drift?") against the live workspace; agent edits and re-simulates.
7. Web UI (single page): persistent chat/activity, simulator viewport, robot-POV camera panel, collapsible directory tree with editable workspace files/diffs, telemetry chart, run controls, a project picker/new-project control, and a server-approved Codex model selector.
8. ROS 2 package export: generate `package.xml`, node file, launch file into the workspace (display only).
9. Per-stage "glass-box" annotations: one-sentence explanation of each pipeline decision, shown in the log.

### Out of scope (do NOT build)
Gazebo live generation, real hardware, auth/accounts, cross-device/cloud persistence, multi-robot, mobile UI, React (see stack), fleet features, CAD import.

## 4. Golden-path demo (the build target)

1. User types: "A two-wheeled rover with a camera that finds and drives to a red ball."
2. Pipeline stages light up in UI: Spec → Morphology → Controller → Simulate.
3. Three.js viewport shows the rover; PiP panel shows the rover's own camera; rover reaches the ball.
4. Rehearsed self-heal: seed a known failure class (e.g., controller uses wrong joint index → telemetry shows zero motion) → agent reads telemetry, explains, fixes, re-runs successfully. This must be reliably reproducible.
5. User iterates live: "double the wheel radius" → URDF re-parameterized → sim re-runs → visible change in viewport.
6. User can continue the same conversation, inspect the generated files, and directly edit them. Finale: open file tree showing generated ROS 2 package. (Optional pre-tested Gazebo launch NOT part of build; presenter handles it manually if at all.)

## 5. Architecture

```
Windows (glass only)                WSL2 Ubuntu (everything real)
┌──────────────────┐   localhost   ┌─────────────────────────────────┐
│ Chrome: web UI    │◄────────────►│ FastAPI server (uvicorn)        │
│ VS Code Remote-WSL│               │  ├── REST: /session /message    │
└──────────────────┘               │  ├── WebSocket: /ws (events)    │
                                   │  ├── Project/session orchestrator│
                                   │  ├── Codex SDK runner (Node/TS) │
                                   │  └── Simulator adapter (PyBullet)│
                                   │ Workspace: ./sessions/          │
                                   └─────────────────────────────────┘
```

- Backend: Python 3.11+, FastAPI + uvicorn, `pybullet`, `websockets` via FastAPI, `numpy`. The official Codex SDK integration is a small local Node/TypeScript runner service using `@openai/codex-sdk`; FastAPI remains the application/API layer.
- Frontend: **vanilla JS single page** (no build step). CDN: Three.js + urdf-loader (NASA JPL `urdf-loaders`), Monaco editor, Chart.js, marked.js for log rendering.
- Sim: PyBullet in `p.DIRECT` (headless). `getCameraImage()` with software renderer (ER_TINY_RENDERER) for robot-POV frames — WSL2-safe, no GPU/GL dependency.

## 6. Persistent project agent and stages

| Stage | Mechanism | Output |
|---|---|---|
| 1. Spec | Single constrained model call, JSON-only response | `spec.json` (morphology type, dimensions, sensors, task) |
| 2. Morphology | Deterministic Python: fill URDF template with spec params (Jinja2) | `robot.urdf` |
| 3. Build/Edit | Persistent Codex SDK thread, workspace file tools + RoboPilot tools | `controller.py` and changed project files |
| 4. Observe | Codex chooses `run_project`, numerical inspection, and visual evidence tools | run record + evidence references |
| 5. Heal/Validate | Same agent loop continues within its budget; user can continue it in later chat turns | fixed files + explanation |
| 6. Export | Deterministic Python: ROS 2 package scaffold from templates | `ros2_ws/` tree |

**Critical design rule:** Stages 1, 2, 6 are constrained/deterministic (reliability). Stages 3–5 are an open, persistent agentic project loop. The agent must NOT free-edit the URDF from scratch; morphology changes update the spec then re-run stage 2. Controller and project logic edits are free-form inside the project workspace. Each completed run is tied to an artifact revision, so user and agent changes remain inspectable and reversible.

## 7. Agent project tools and data contract (heart of the product)

Exposed to the Codex agent through the local RoboPilot project-tool server. Codex selects evidence on demand rather than receiving raw sensor streams in every prompt.

Input:
```json
{ "duration_s": 10, "mode": "batch | live", "scenario": "default" }
```
Output:
```json
{
  "status": "success | crashed | timeout | task_failed | task_success",
  "stderr": "string (truncated 2000 chars)",
  "telemetry_summary": {
    "final_base_pose": {"xyz": [..], "rpy": [..]},
    "distance_to_target_m": 0.12,
    "total_yaw_drift_deg": 3.4,
    "mean_wheel_velocities": {"left": 8.2, "right": 8.3},
    "collisions": 0,
    "max_tilt_deg": 2.1,
    "sim_time_s": 10.0
  },
  "run_id": "run_123",
  "artifact_revision": "rev_17",
  "evidence": {
    "frames": [{"camera":"robot_rgb", "t": 10.0, "path":"runs/run_123/frames/final.jpg"}],
    "signals_path": "runs/run_123/signals.jsonl"
  }
}
```
Task success criterion for golden path: `distance_to_target_m < 0.3` with `collisions == 0`.

The minimum tool set is: `run_project`, `get_run_summary`, `inspect_telemetry`, `get_visual_frame`, `get_visual_clip`, `compare_runs`, and workspace file read/edit tools. Visual tools return local image paths for Codex image input; base64 is used only for browser events. The simulator records robot pose, commanded/measured joint states, contacts, task metrics, controller logs, and camera frames. Full-resolution raw data stays on disk; the agent receives summaries or requested windows.

Simulator-only ground truth (for example, exact target position) is explicitly labelled `oracle/evaluation`; robot-facing observations remain camera, odometry, joint state, IMU, lidar, and other sensor equivalents. A future ROS 2 adapter maps `/tf`, `/odom`, `/joint_states`, image/camera-info, diagnostics, and bounded rosbag windows to this same contract.

Agent guardrails: `cwd` locked to the project session directory; no network or host-destructive access; project tools are capability-limited. Default autonomous budget is 25 agent turns, 12 simulation runs, and a wall-clock timeout per user request. Reaching a budget pauses safely with the complete project/thread state preserved; the user can select **Continue autonomously** or send a new instruction. Every safe progress/tool/edit event is appended to `agent_log.jsonl` (fallback narration if UI dies).

## 8. WebSocket event schema (server → browser, JSON per message)

```json
{"type":"stage",     "project_id":"...", "run_id":"...", "seq":1, "stage":"spec|morphology|build|observe|heal|export", "status":"start|done|error", "note":"glass-box one-liner"}
{"type":"agent_step","project_id":"...", "seq":2, "role":"progress|tool_call|tool_result|edit", "summary":"...", "file":"controller.py?", "lines":[12,18]}
{"type":"sim_state", "project_id":"...", "run_id":"...", "seq":3, "t":1.23, "base_pos":[x,y,z], "base_quat":[x,y,z,w], "joints":{"left_wheel":0.5,"right_wheel":0.52}}
{"type":"sim_frame", "jpeg_b64":"...", "t":1.2}
{"type":"telemetry", "t":1.23, "series":{"dist_to_target":0.8,"yaw_drift":1.1}}
{"type":"files",     "tree":[...], "changed":["controller.py"]}
{"type":"chat",      "role":"assistant", "text":"..."}
```
`sim_state` is 20 Hz during batch replay. In **live** mode the simulator advances in real time and streams state for display/control; it remains the single owner of physics while Codex and the browser interact through the project service. `sim_frame` is 5 Hz max. Include sim-time `t` in every packet for interpolation.

Browser → server: `{"type":"user_message","text":"..."}` and `{"type":"new_session"}` via REST.

## 9. UI layout (single page, light theme)

```
┌─────────────┬──────────────────────────┬──────────────┐
│ Chat +      │  Three.js viewport       │ Monaco tabs: │
│ activity    │  (replay/live sim state, │ spec.json    │
│ (pipeline   │   orbit controls, target │ robot.urdf   │
│ stage chips │   ball, ground grid)     │ controller.py│
│ light up)   ├──────────────────────────┤ ros2_ws tree │
│             │ Robot-POV PiP │ Chart.js ├──────────────┤
│ [input box] │ (camera feed) │ telemetry│ changes/diff │
└─────────────┴──────────────────────────┴──────────────┘
```
Monaco displays actual workspace files, highlights Codex edits at their affected lines, and permits direct user edits. Saving a user edit creates a project revision visible to Codex on its next turn. Selecting a chat activity, telemetry event, or run opens the associated code revision and simulator time. Header controls: Run, Stop, Continue autonomously, live/batch mode, and Compare runs. Activity is safe progress/tool/edit narration, never raw private reasoning. Use a warm off-white canvas, white panels, slate text, blue primary actions, green success, amber attention, and red failure.

## 10. Repo structure

```
./
├── server/
│   ├── main.py              # FastAPI app, WS hub, session mgmt
│   ├── codex_bridge.py      # FastAPI client for local TypeScript Codex runner
│   ├── project_tools.py     # capability-limited simulator/evidence tools
│   ├── mcp_server.py        # session-scoped stdio MCP server
│   ├── session_store.py     # project workspace/revision persistence
│   ├── sim_tool.py          # PyBullet batch/live runner + run records
│   ├── morphology.py        # Jinja2 URDF templating
│   └── templates/
│       ├── diff_drive.urdf.j2
├── web/
│   ├── index.html
│   ├── app.js               # WS client, panels, state
│   ├── mock_ws.js           # browser demo stream
│   ├── mock_ws.py           # JSONL mock-event emitter
│   └── style.css
├── codex_runner/
│   ├── package.json
│   └── src/index.ts         # @openai/codex-sdk persistent-thread runner
├── contracts/
│   └── project_tools.json   # frozen tool, run-record, and event schemas
├── tests/                   # simulator and project-tool smoke tests
├── sessions/                # runtime workspaces (gitignored)
├── demo/
│   ├── golden_prompt.txt
│   ├── seeded_failure.md    # exact recipe for the rehearsed self-heal
│   └── fallback/            # recorded session JSONL + screen video
└── PRD.md (this file)
```

## 11. Multi-agent delegation plan (for the orchestrating Codex instance)

The main Codex agent receiving this PRD acts as **lead/integrator**. It owns contracts, dependencies, project skeleton, Codex bridge, integration, and acceptance checks. Delegate independently implementable slices with these boundaries and frozen integration contracts:

- **Agent A — Sim core:** `server/sim_tool.py`, `server/morphology.py`, URDF templates, and simulator tests. Contract: frozen run record/tool schema in §7. Include a standalone CLI test.
- **Agent B — Frontend:** all `web/` files and `web/mock_ws.py`. Contract: frozen events in §8; work only against mock events, never block on backend integration.
- **Agent C — Project-tool adapter:** `server/project_tools.py` and `server/mcp_server.py`. Contract: local capability-limited tool server over JSON lines/MCP; may use a deterministic simulator stub until Agent A lands.
- **Lead — Platform and intelligence:** server API/session orchestration, TypeScript Codex runner/bridge, event relay, spec/export stages, and integration.

Integration rule: contracts in §7–§8 are frozen; any agent needing a change must surface it to lead, which updates this PRD first. Each agent writes a smoke test; lead runs end-to-end integration after A+B land.

## 12. Milestones (ordered by demo-criticality — cut from the bottom)

1. **M1 (must):** simulator + diff-drive template + hand-written controller reaches ball; canonical numerical/visual run record is correct.
2. **M2 (must):** persistent Codex SDK project thread can inspect tools, edit the workspace, run the project, and continue on a second user message.
3. **M3 (must):** light UI shows chat/activity, batch replay, code/diff, and stages end-to-end.
4. **M4 (must):** autonomous repair has a clear failure signal, visible evidence, and a safe continuation checkpoint; recorded fallback captured.
5. **M5 (should):** live simulator mode streams and accepts browser controls.
6. **M6 (should):** visual evidence panel, telemetry inspection, user code edits, and run comparison.
7. **M7 (nice):** ROS 2 export tree, glass-box polish, arm template, and ROS 2 runtime adapter.

## 13. Risks & mitigations

- **Agent flails live:** 25-turn/12-run/timeout checkpoints preserve state and require an explicit continuation; `demo/fallback/` recorded session + video, and app supports replaying a recorded JSONL through the UI.
- **WSL2 rendering:** use ER_TINY_RENDERER for getCameraImage; never depend on GL. Set `.wslconfig` memory ≥ 8GB.
- **Token burn:** telemetry summarized (never raw arrays to the model); snapshots only on failure or explicit request.
- **Filesystem:** every project workspace is under `./sessions/`; assert at startup that cwd is not under `/mnt/`.

## 14. Pitch beats (presenter notes, not build items)

Problem (weeks-long idea→prototype loop) → Insight (coding agents are blind to physics; ours has eyes: telemetry + robot camera as tool output) → Live demo (prompt → moving robot → self-heal → live iterate) → Glass-box (teaches while it builds) → Roadmap (same telemetry JSON schema from real sensors = hardware later; Gazebo/ROS 2 already exported today).
