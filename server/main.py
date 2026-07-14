"""FastAPI entry point for the persistent RoboPilot project workspace."""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .codex_bridge import CodexBridge
from .project_tools import ProjectToolError
from .session_store import ProjectStore, SUPPORTED_CODEX_MODELS
from .sim_tool import run_simulation


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"
PIPELINE_STAGES = frozenset({"spec", "morphology", "build", "observe", "heal", "export"})
STAGE_NOTES = {
    "spec": "Inspecting the workspace and task evidence.",
    "morphology": "Updating robot structure or morphology assets.",
    "build": "Building or changing project artifacts.",
    "observe": "Running or inspecting simulator evidence.",
    "heal": "Validating or repairing after evidence.",
    "export": "Producing an integration/export artifact.",
}


@dataclass
class AppState:
    store: ProjectStore
    sockets: set[WebSocket] = field(default_factory=set)
    sequence: int = 0
    bridge: CodexBridge | None = None
    active_stages: dict[str, str] = field(default_factory=dict)

    async def broadcast(self, event: dict[str, Any]) -> None:
        for stage_event in self._stage_events(event):
            await self._deliver(stage_event)
        if event.get("type") == "thread_started":
            project_id, thread_id = event.get("project_id"), event.get("thread_id")
            if isinstance(project_id, str) and isinstance(thread_id, str):
                session = self.store.get_or_create(project_id)
                chat_session_id = event.get("chat_session_id")
                self.store.set_codex_thread(session, thread_id, chat_session_id if isinstance(chat_session_id, str) else None)
        if event.get("type") == "chat" and event.get("role") == "assistant":
            project_id, text = event.get("project_id"), event.get("text")
            if isinstance(project_id, str) and isinstance(text, str):
                session = self.store.get_or_create(project_id)
                chat_session_id = event.get("chat_session_id")
                self.store.append_chat_message(session, "assistant", text, chat_session_id if isinstance(chat_session_id, str) else None)
        await self._deliver(event)
        # A Codex file-change event only carries a diff summary. Re-read the
        # bounded visible workspace so the browser editor is updated live.
        if event.get("type") == "agent_step" and event.get("role") == "edit":
            project_id = event.get("project_id")
            if isinstance(project_id, str):
                session = self.store.get_or_create(project_id)
                if event.get("status") == "completed":
                    self.store.record_agent_edit(session)
                await self._deliver(
                    {
                        "type": "files",
                        "project_id": project_id,
                        "tree": self.store.file_tree(session),
                        "contents": self.store.read_visible_files(session),
                        "changed": [],
                        "artifact_revision": session.revision_id,
                    }
                )

    def _stage_events(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Derive a truthful current pipeline stage from concrete SDK events."""
        project_id = event.get("project_id")
        if not isinstance(project_id, str):
            return []
        event_type = event.get("type")
        if event_type == "stage":
            stage, status = event.get("stage"), event.get("status")
            if isinstance(stage, str) and stage in PIPELINE_STAGES:
                if status == "start":
                    self.active_stages[project_id] = stage
                elif status in {"done", "error"} and self.active_stages.get(project_id) == stage:
                    self.active_stages.pop(project_id, None)
            return []
        if event_type == "turn_started":
            self.active_stages.pop(project_id, None)
            return [{"type": "stage", "project_id": project_id, "stage": "reset", "status": "reset", "note": "Codex accepted the request; waiting for its first concrete action."}]
        if event_type in {"turn_completed", "turn_error", "runner_error", "turn_stopped"}:
            current = self.active_stages.pop(project_id, None)
            if current:
                status = "done" if event_type == "turn_completed" else "error"
                note = "Codex completed this stage." if status == "done" else "Codex stopped during this stage."
                return [{"type": "stage", "project_id": project_id, "stage": current, "status": status, "note": note}]
            return []
        if event_type != "agent_step":
            return []
        stage = event.get("stage")
        if not isinstance(stage, str) or stage not in PIPELINE_STAGES:
            return []
        if event.get("status") == "failed":
            if self.active_stages.get(project_id) == stage:
                self.active_stages.pop(project_id, None)
            return [{"type": "stage", "project_id": project_id, "stage": stage, "status": "error", "note": event.get("summary", "Stage activity failed.")}]
        previous = self.active_stages.get(project_id)
        if previous == stage:
            return []
        self.active_stages[project_id] = stage
        events: list[dict[str, Any]] = []
        if previous:
            events.append({"type": "stage", "project_id": project_id, "stage": previous, "status": "done", "note": "Moved to the next concrete stage."})
        events.append({"type": "stage", "project_id": project_id, "stage": stage, "status": "start", "note": STAGE_NOTES[stage]})
        return events

    async def _deliver(self, event: dict[str, Any]) -> None:
        self.sequence += 1
        event.setdefault("seq", self.sequence)
        failed: list[WebSocket] = []
        for socket in self.sockets:
            try:
                await socket.send_json(event)
            except RuntimeError:
                failed.append(socket)
        for socket in failed:
            self.sockets.discard(socket)

    async def send_to(self, socket: WebSocket, event: dict[str, Any]) -> None:
        """Send selection state only to the browser that requested it."""
        self.sequence += 1
        event.setdefault("seq", self.sequence)
        try:
            await socket.send_json(event)
        except RuntimeError:
            self.sockets.discard(socket)


state = AppState(store=ProjectStore(ROOT / "sessions"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    state.bridge = CodexBridge(ROOT, state.broadcast)
    yield
    if state.bridge:
        await state.bridge.close()


app = FastAPI(title="RoboPilot", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "robopilot",
        "codex": "configured" if state.bridge and state.bridge.configured else "needs_CODEX_API_KEY",
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    """Serve the declared SVG for browsers that still request favicon.ico."""
    return FileResponse(WEB_ROOT / "favicon.svg", media_type="image/svg+xml")


@app.get("/api/projects/{project_id}/runs/{run_id}/frames/{frame_name}")
def recorded_frame(project_id: str, run_id: str, frame_name: str) -> FileResponse:
    """Serve only a camera artifact already recorded inside this project."""
    if not run_id.startswith("run_") or Path(frame_name).name != frame_name or not frame_name.endswith(".jpg"):
        raise HTTPException(status_code=404, detail="recorded frame not found")
    try:
        session = state.store.get_or_create(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="project not found") from exc
    frame_path = (session.workspace / "runs" / run_id / "frames" / frame_name).resolve()
    frames_root = (session.workspace / "runs" / run_id / "frames").resolve()
    if frames_root not in frame_path.parents or not frame_path.is_file():
        raise HTTPException(status_code=404, detail="recorded frame not found")
    return FileResponse(frame_path, media_type="image/jpeg")


@app.websocket("/ws")
async def websocket_endpoint(socket: WebSocket) -> None:
    await socket.accept()
    state.sockets.add(socket)
    session = state.store.get_or_create()
    await _send_project_selected(session.project_id, recipient=socket)
    await state.send_to(
        socket,
        {
            "type": "files",
            "project_id": session.project_id,
            "tree": state.store.file_tree(session),
            "contents": state.store.read_visible_files(session),
            "changed": [],
            "artifact_revision": session.revision_id,
        }
    )
    try:
        while True:
            message = await socket.receive_json()
            if not isinstance(message, dict):
                continue
            await _handle_message(session.project_id, message, socket)
    except WebSocketDisconnect:
        state.sockets.discard(socket)


async def _handle_message(project_id: str, message: dict[str, Any], socket: WebSocket) -> None:
    message_type = message.get("type")
    try:
        requested_project_id = str(message.get("project_id", project_id))
        if message_type == "new_project":
            session, created = state.store.create_fresh_project(requested_project_id)
        else:
            session = state.store.get_or_create(requested_project_id)
            created = False
    except ValueError as exc:
        await state.broadcast({"type": "agent_step", "project_id": project_id, "role": "progress", "summary": f"Invalid project name: {exc}"})
        return
    if message_type in {"select_project", "new_project"}:
        await _send_project_selected(session.project_id, created=created, recipient=socket)
        return
    if message_type == "select_chat_session":
        chat_session_id = message.get("chat_session_id")
        if isinstance(chat_session_id, str):
            try:
                state.store.select_chat_session(session, chat_session_id)
            except ValueError as exc:
                await state.broadcast({"type": "agent_step", "project_id": session.project_id, "role": "progress", "summary": str(exc)})
                return
            await _send_project_selected(session.project_id, recipient=socket)
        return
    if message_type == "new_chat_session":
        state.store.create_chat_session(session)
        await _send_project_selected(session.project_id, recipient=socket)
        return
    selected_model = message.get("model")
    if isinstance(selected_model, str) and selected_model in SUPPORTED_CODEX_MODELS:
        state.store.set_codex_model(session, selected_model)
    if message_type == "user_message":
        text = message.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 10_000:
            return
        state.store.append_chat_message(session, "user", text)
        await state.broadcast({"type": "chat", "project_id": session.project_id, "role": "user", "text": text})
        if state.bridge:
            await state.bridge.submit(project_id=session.project_id, chat_session_id=session.active_chat_session_id or "", workspace=session.workspace, prompt=_agent_prompt(text), thread_id=session.codex_thread_id, model=session.codex_model)
        return
    if message_type == "run_project":
        mode = "live" if message.get("mode") == "live" else "batch"
        await _run_project(session.project_id, mode)
        return
    if message_type == "stop_run":
        if state.bridge:
            await state.bridge.stop(session.project_id)
        await state.broadcast({"type": "agent_step", "project_id": session.project_id, "role": "progress", "summary": "Stopped the active agent/simulator request."})
        return
    if message_type == "continue_autonomously":
        if state.bridge:
            await state.bridge.submit(project_id=session.project_id, chat_session_id=session.active_chat_session_id or "", workspace=session.workspace, prompt=_agent_prompt("Continue the current task. Inspect existing evidence, make the next safe improvement, and validate it.", autonomous=True), thread_id=session.codex_thread_id, model=session.codex_model)
        return
    if message_type == "load_run":
        run_id = message.get("run_id")
        if isinstance(run_id, str):
            await _replay_recorded_run(session, run_id)
        return
    if message_type == "save_file":
        path, content = message.get("path"), message.get("content")
        if isinstance(path, str) and isinstance(content, str):
            try:
                state.store.save_file(session, path, content)
            except ValueError as exc:
                await state.broadcast({"type": "agent_step", "project_id": session.project_id, "role": "progress", "summary": f"Could not save file: {exc}"})
                return
            await state.broadcast({"type": "files", "project_id": session.project_id, "tree": state.store.file_tree(session), "contents": {path: content}, "changed": [path], "artifact_revision": session.revision_id})


def _agent_prompt(user_text: str, *, autonomous: bool = False) -> str:
    """Give Codex configuration and evidence-loop guidance."""
    autonomy = ""
    if autonomous:
        autonomy = (
            "\n\nAutonomous recovery budget: take up to six evidence-driven attempts in this "
            "turn, stopping earlier when the task is complete or a platform limitation is "
            "proven. After a failed command or tool call, inspect its exact error or recorded "
            "artifact, form a different hypothesis, and change the relevant input, implementation, "
            "or validation method before trying again. Never repeat an identical failed tool call "
            "without new evidence."
        )
    return (
        "RoboPilot workspace rule: the platform can run differential-drive, articulated-hand/arm, "
        "and rail_train projects through validated built-in adapters. For ordinary differential-drive motion requests, prefer "
        "editing mission.json before rewriting controller.py. Use controller.py when a new "
        "algorithm is genuinely needed. You may edit any project files required by the user, "
        "and must only claim a simulation was run after a RoboPilot tool returns a run record. "
        "Use the session-scoped RoboPilot MCP tools for simulation evidence.\n\n"
        f"User request:\n{user_text}{autonomy}"
    )


def _project_preview(session: Any) -> dict[str, Any]:
    """Describe the project's active simulator capability without guessing a robot."""
    try:
        spec = json.loads((session.workspace / "spec.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        spec = {}
    morphology = spec.get("morphology") if isinstance(spec, dict) else None
    kind = morphology.get("type") if isinstance(morphology, dict) else morphology
    kind = kind if isinstance(kind, str) else "unknown"
    title = spec.get("name") if isinstance(spec, dict) and isinstance(spec.get("name"), str) else session.project_id
    adapter = {
        "differential_drive": "PyBullet differential-drive",
        "robotic_hand": "PyBullet articulated position-control",
        "robotic_arm_hand": "PyBullet articulated position-control",
        "articulated_arm": "PyBullet articulated position-control",
        "mobile_manipulator": "PyBullet articulated position-control",
        "quadruped": "PyBullet articulated position-control",
        "rail_train": "PyBullet kinematic rail-train",
    }.get(kind)
    can_run = adapter is not None
    return {
        "type": "project_preview",
        "project_id": session.project_id,
        "title": title,
        "morphology": kind,
        "can_run": can_run,
        "message": f"{adapter} adapter ready." if adapter else f"No simulator adapter is installed for '{kind}'. This project remains editable and previewable.",
    }


async def _send_project_selected(project_id: str, *, created: bool = False, recipient: WebSocket | None = None) -> None:
    """Load a project workspace and its persisted Codex thread into the client."""
    session = state.store.get_or_create(project_id)
    deliver = state.broadcast if recipient is None else lambda event: state.send_to(recipient, event)
    await deliver(
        {
            "type": "project_selected",
            "project_id": session.project_id,
            "projects": state.store.list_projects(),
            "model": session.codex_model,
            "created": created,
            "chat_session_id": session.active_chat_session_id,
            "chat_sessions": state.store.list_chat_sessions(session),
            "chat_history": state.store.chat_history(session),
        }
    )
    await deliver(_project_preview(session))
    runs = state.store.list_runs(session)
    await deliver({"type": "recorded_runs", "project_id": session.project_id, "runs": runs})
    await deliver(
        {
            "type": "files",
            "project_id": session.project_id,
            "tree": state.store.file_tree(session),
            "contents": state.store.read_visible_files(session),
            "changed": [],
            "artifact_revision": session.revision_id,
        }
    )
    # A project should reopen in its last known physical state rather than a
    # blank simulator.  The newest durable run is the authoritative snapshot.
    if runs and _project_preview(session)["can_run"]:
        await _replay_recorded_run(session, str(runs[0]["run_id"]))


async def _run_project(project_id: str, mode: str, duration_s: float | None = None) -> None:
    session = state.store.get_or_create(project_id)
    preview = _project_preview(session)
    if not preview["can_run"]:
        await state.broadcast(
            {
                "type": "run_unsupported",
                "project_id": project_id,
                "message": preview["message"],
                "morphology": preview["morphology"],
            }
        )
        return
    # A typical train route is materially longer than the rover target task.
    # Batch mode remains fast; live mode intentionally mirrors this duration.
    actual_duration_s = duration_s if duration_s is not None else (30.0 if preview["morphology"] == "rail_train" else 10.0)
    await state.broadcast({"type": "stage", "project_id": project_id, "stage": "observe", "status": "start", "note": f"Running the project in {mode} simulator mode for {actual_duration_s:g} simulated seconds."})
    live_events_already_streamed = mode == "live"
    if live_events_already_streamed:
        loop = asyncio.get_running_loop()
        live_run_id = f"run_{uuid.uuid4().hex[:12]}"

        def publish_live_state(sample: dict[str, Any], signal: dict[str, Any]) -> None:
            future = asyncio.run_coroutine_threadsafe(
                _broadcast_live_state(project_id, live_run_id, sample, signal), loop
            )
            future.result(timeout=5)

        summary = await asyncio.to_thread(
            run_simulation,
            session.workspace,
            duration_s=actual_duration_s,
            mode="live",
            scenario="default",
            run_id=live_run_id,
            on_state=publish_live_state,
        )
    else:
        summary = await asyncio.to_thread(session.tools.run_project, actual_duration_s, mode, "default")
    run_id = summary["run_id"]
    if summary.get("status") == "crashed":
        await state.broadcast({"type": "run_summary", "project_id": project_id, **summary, "artifact_revision": session.revision_id})
        await state.broadcast({"type": "recorded_runs", "project_id": project_id, "runs": state.store.list_runs(session)})
        await state.broadcast({"type": "stage", "project_id": project_id, "run_id": run_id, "stage": "observe", "status": "error", "note": "The simulator crashed; Codex can inspect the recorded error and repair the workspace."})
        return
    if not live_events_already_streamed:
        telemetry = await asyncio.to_thread(
            session.tools.inspect_telemetry, run_id, ["dist_to_target_m", "yaw_rad"]
        )
        state_samples = _read_state_samples(session.workspace, run_id)
        for index, sample in enumerate(telemetry["samples"]):
            raw_series = sample["signals"]
            series = {
                "dist_to_target": raw_series.get("dist_to_target_m"),
                "yaw_drift": raw_series.get("yaw_rad"),
            }
            pose = state_samples[min(index, len(state_samples) - 1)] if state_samples else {}
            await state.broadcast({"type": "sim_state", "project_id": project_id, "run_id": run_id, "t": sample["t"], "base_pos": pose.get("base_pos", [0.0, 0.0, 0.0]), "base_quat": pose.get("base_quat", [0.0, 0.0, 0.0, 1.0]), "joints": pose.get("joints", {})})
            await state.broadcast({"type": "telemetry", "project_id": project_id, "run_id": run_id, "t": sample["t"], "series": series})
    await _broadcast_recorded_frame(session, run_id)
    await state.broadcast({"type": "run_summary", "project_id": project_id, **summary, "artifact_revision": session.revision_id})
    await state.broadcast({"type": "recorded_runs", "project_id": project_id, "runs": state.store.list_runs(session)})
    await state.broadcast({"type": "stage", "project_id": project_id, "run_id": run_id, "stage": "observe", "status": "done", "note": "Recorded numerical and visual evidence for the project agent."})


def _read_state_samples(workspace: Path, run_id: str) -> list[dict[str, Any]]:
    """Read only the fixed run-local state record for browser replay."""
    if not run_id.startswith("run_") or any(part in {"", ".", ".."} for part in Path(run_id).parts):
        return []
    path = workspace / "runs" / run_id / "states.jsonl"
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, json.JSONDecodeError):
        return []


async def _broadcast_live_state(
    project_id: str, run_id: str, sample: dict[str, Any], signal: dict[str, Any]
) -> None:
    """Relay a real-time simulator sample without exposing simulator ownership."""
    await state.broadcast(
        {
            "type": "sim_state",
            "project_id": project_id,
            "run_id": run_id,
            "t": sample["t"],
            "base_pos": sample["base_pos"],
            "base_quat": sample["base_quat"],
            "joints": sample["joints"],
        }
    )
    await state.broadcast(
        {
            "type": "telemetry",
            "project_id": project_id,
            "run_id": run_id,
            "t": sample["t"],
            "series": {
                "dist_to_target": signal["dist_to_target_m"],
                "yaw_drift": signal["yaw_rad"],
            },
        }
    )


async def _broadcast_recorded_frame(session: Any, run_id: str) -> None:
    """Tell the browser where to fetch one real camera artifact for a run."""
    try:
        frame = await asyncio.to_thread(session.tools.get_visual_frame, run_id, "robot_rgb")
        frame_name = Path(str(frame["path"])).name
    except (KeyError, OSError, ProjectToolError, ValueError):
        return
    await state.broadcast(
        {
            "type": "sim_frame",
            "project_id": session.project_id,
            "run_id": run_id,
            "t": frame["t"],
            "url": f"/api/projects/{session.project_id}/runs/{run_id}/frames/{frame_name}",
        }
    )


async def _replay_recorded_run(session: Any, run_id: str) -> None:
    """Load a durable run record into the UI without invoking the simulator."""
    try:
        summary = await asyncio.to_thread(session.tools.get_run_summary, run_id)
        telemetry = await asyncio.to_thread(
            session.tools.inspect_telemetry, run_id, ["dist_to_target_m", "yaw_rad"]
        )
    except (ProjectToolError, ValueError) as exc:
        await state.broadcast({"type": "agent_step", "project_id": session.project_id, "role": "progress", "summary": f"Could not load recorded run: {exc}"})
        return
    await state.broadcast({"type": "run_loaded", "project_id": session.project_id, "run_id": run_id})
    state_samples = _read_state_samples(session.workspace, run_id)
    for index, sample in enumerate(telemetry["samples"]):
        raw_series = sample["signals"]
        pose = state_samples[min(index, len(state_samples) - 1)] if state_samples else {}
        await state.broadcast({"type": "sim_state", "project_id": session.project_id, "run_id": run_id, "t": sample["t"], "base_pos": pose.get("base_pos", [0.0, 0.0, 0.0]), "base_quat": pose.get("base_quat", [0.0, 0.0, 0.0, 1.0]), "joints": pose.get("joints", {})})
        await state.broadcast({"type": "telemetry", "project_id": session.project_id, "run_id": run_id, "t": sample["t"], "series": {"dist_to_target": raw_series.get("dist_to_target_m"), "yaw_drift": raw_series.get("yaw_rad")}})
    await _broadcast_recorded_frame(session, run_id)
    await state.broadcast({"type": "run_summary", "project_id": session.project_id, **summary, "artifact_revision": session.revision_id})
