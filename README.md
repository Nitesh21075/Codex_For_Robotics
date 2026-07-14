# RoboPilot

RoboPilot is a persistent, multimodal project workspace. Its validated PyBullet
adapters cover differential-drive rovers, articulated hands/arms, and bounded
kinematic rail trains. The platform boundary is a Codex agent that can edit a
project, request targeted numerical or visual evidence, run it, and continue
the same conversation.

## Local setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cd codex_runner && npm install && cd ..
cp .env.example .env
```

Put the project API key in `.env` as `CODEX_API_KEY`. Do not put it in `web/`,
browser storage, logs, or session files. Start the server from the repository
root:

```bash
.venv/bin/uvicorn server.main:app --env-file .env
```

Open `http://127.0.0.1:8000`. Without a key, the UI, simulator, replay, and
MCP tool surface still work; chat reports that Codex needs configuration.

For uninterrupted Codex turns, run without `--reload` as shown above. Uvicorn
reload restarts the backend whenever server or web source changes, which also
ends the local Codex SDK child process and disconnects its WebSocket clients.
For short UI/backend development only, use:

```bash
.venv/bin/uvicorn server.main:app --reload --reload-dir server --reload-dir web --env-file .env
```

Do not watch `sessions/`: Codex writes project files there and a reload would
interrupt an active turn.

## Working with projects

The project picker in the header reopens that project's workspace. The
conversation picker beside it lets you resume any saved conversation (including
its visible chat history and Codex memory), while its **+** starts a separate
conversation in the same project. Use the project **+** to create a separate
project; this does not reset the current one. The model selector applies to the next chat or
**Continue autonomously** turn. The selector includes GPT-5.6 Luna and Terra
with low, medium, high, and extra-high reasoning effort, plus GPT-5.5; the API
key remains server-side in `.env`.

For the rover adapter, routine behaviour belongs in `mission.json` (target
seeking, circle motion, or explicit wheel velocities). Codex can still edit
`controller.py` when a request needs a genuinely different algorithm. Each run
is retained under `sessions/<project>/workspace/runs/` and can be selected from
the recorded-run picker in the camera panel.

`rail_train` is also runnable through a bounded PyBullet kinematic adapter.
It consumes `mission.json` route nodes and an optional `train_controller.py`,
and records traction, braking, speed, route progress, frames, and telemetry.
It is intentionally not presented as high-fidelity wheel/rail or flexible
coupler physics; those require a future dedicated dynamics adapter.

For a new robot name that fits an existing approved family (for example, an
airport monorail using the rail-train family or a custom gripper using the
articulated family), Codex can now create a project-local `adapter.json` and
validate it with a short real run. A draft remains non-runnable in the UI;
only a successful launch promotes it to `validated`. This is intentionally not
unrestricted server-plugin authoring: project agents cannot alter RoboPilot's
global simulator code or register arbitrary executable adapters.

## Architecture

- `server/` owns sessions, workspace revisions, WebSocket events, simulator
  orchestration, and the session-scoped MCP evidence server.
- `codex_runner/` uses the official `@openai/codex-sdk` to retain one Codex
  thread per saved conversation. It runs with workspace-write access, network disabled,
  and the RoboPilot MCP tool server configured for that workspace.
- `contracts/project_tools.json` freezes the agent-tool and event boundary.
  The session MCP server includes adapter-capability discovery, draft creation,
  and validation tools alongside run and evidence tools.
- `web/` is a light-theme browser cockpit with chat/activity, project and model
  controls, a collapsible workspace directory tree, editable code, batch
  replay, live simulator display, camera evidence, and telemetry.

## Verification

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_sim_tool.py tests/test_project_tools.py
npm --prefix codex_runner run check
node --check web/app.js
```

The simulator's standalone smoke run is:

```bash
PYTHONPATH=. .venv/bin/python -m server.sim_tool --project-dir /tmp/robopilot-sim-smoke --duration-s 6 --fresh
```
