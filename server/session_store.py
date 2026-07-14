"""Persistent, session-rooted project workspaces for RoboPilot."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from .project_tools import ProjectTools
from .sim_tool import DEFAULT_CONTROLLER


_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
DEFAULT_CODEX_MODEL = "gpt-5.6-luna:medium"
SUPPORTED_CODEX_MODELS = (
    "gpt-5.6-luna:low",
    "gpt-5.6-luna:medium",
    "gpt-5.6-luna:high",
    "gpt-5.6-luna:xhigh",
    "gpt-5.6-terra:low",
    "gpt-5.6-terra:medium",
    "gpt-5.6-terra:high",
    "gpt-5.6-terra:xhigh",
    "gpt-5.5",
)

_DEFAULT_FILES = {
    "spec.json": json.dumps(
        {
            "name": "red_ball_rover",
            "morphology": {"type": "differential_drive"},
            "sensors": ["robot_rgb"],
            "task": {"type": "drive_to_target", "target": "red_ball"},
        },
        indent=2,
    )
    + "\n",
    "controller.py": DEFAULT_CONTROLLER,
    "mission.json": json.dumps(
        {
            "mode": "target_seek",
            "parameters": {"max_speed_rad_s": 7.0, "turn_gain": 5.0},
        },
        indent=2,
    )
    + "\n",
    "AGENTS.md": """# RoboPilot project guidance\n\nThis is a session-local robotics project. Use the RoboPilot MCP tools to run the project and inspect bounded numerical or visual evidence. Keep morphology changes in `spec.json`; do not rewrite the URDF template from scratch. For routine motion requests, prefer editing `mission.json` (for example target seeking, circle motion, or explicit wheel velocities) rather than rewriting `controller.py`. Edit the controller when the user actually needs a new control algorithm. Explain concrete edits and validate them with a run when appropriate.\n""",
}

_FRESH_PROJECT_FILES = {
    "PROJECT.md": "# New robotics project\n\nDescribe the robot and task in chat to have Codex scaffold this workspace.\n",
    "AGENTS.md": """# RoboPilot fresh-project guidance

This workspace starts empty on purpose. For the user's first request, create a concise `spec.json`, a README, and the code/assets needed to express the requested robot and task. Explain what was created. The currently installed PyBullet adapter can execute the differential-drive rover template only; for another morphology such as an arm, scaffold the project honestly and state that a matching simulator adapter is still needed before running physics. Do not claim a simulation was run unless the RoboPilot tool returned a run record.
""",
}


@dataclass
class ProjectSession:
    project_id: str
    root: Path
    workspace: Path
    tools: ProjectTools
    revision: int = 1
    chat_sessions: list[dict[str, object]] | None = None
    active_chat_session_id: str | None = None
    codex_model: str = DEFAULT_CODEX_MODEL

    @property
    def revision_id(self) -> str:
        return f"rev_{self.revision}"

    @property
    def active_chat_session(self) -> dict[str, object]:
        assert self.chat_sessions
        return next(item for item in self.chat_sessions if item["session_id"] == self.active_chat_session_id)

    @property
    def codex_thread_id(self) -> str | None:
        value = self.active_chat_session.get("codex_thread_id")
        return value if isinstance(value, str) else None


class ProjectStore:
    def __init__(self, sessions_root: str | Path) -> None:
        self.sessions_root = Path(sessions_root).resolve()
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, ProjectSession] = {}

    def get_or_create(self, project_id: str = "rover-demo") -> ProjectSession:
        if not _PROJECT_ID.fullmatch(project_id):
            raise ValueError("project_id must be lowercase letters, numbers, or hyphens")
        existing = self._sessions.get(project_id)
        if existing:
            return existing
        root = self.sessions_root / project_id
        workspace = root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        metadata_path = root / "project.json"
        metadata = self._read_metadata(metadata_path)
        for relative, content in _DEFAULT_FILES.items():
            destination = workspace / relative
            if not destination.exists():
                destination.write_text(content, encoding="utf-8")
        chat_sessions = self._load_chat_sessions(metadata)
        active_chat_session_id = metadata.get("active_chat_session_id")
        if not isinstance(active_chat_session_id, str) or not any(item["session_id"] == active_chat_session_id for item in chat_sessions):
            active_chat_session_id = str(chat_sessions[0]["session_id"])
        session = ProjectSession(
            project_id=project_id,
            root=root,
            workspace=workspace,
            tools=ProjectTools(workspace),
            revision=int(metadata.get("revision", 1)),
            chat_sessions=chat_sessions,
            active_chat_session_id=active_chat_session_id,
            codex_model=self._safe_model(metadata.get("codex_model")),
        )
        self._sessions[project_id] = session
        self._persist(session)
        return session

    def create_fresh_project(self, project_id: str) -> tuple[ProjectSession, bool]:
        """Create a blank, agent-scaffolded project without the rover starter files.

        Reusing an existing name only opens that project; it never erases user or
        agent work.
        """
        if not _PROJECT_ID.fullmatch(project_id):
            raise ValueError("project_id must be lowercase letters, numbers, or hyphens")
        root = self.sessions_root / project_id
        if project_id in self._sessions or (root / "project.json").is_file():
            return self.get_or_create(project_id), False
        session = self.get_or_create(project_id)
        for relative in _DEFAULT_FILES:
            candidate = session.workspace / relative
            if candidate.is_file():
                candidate.unlink()
        for relative, content in _FRESH_PROJECT_FILES.items():
            (session.workspace / relative).write_text(content, encoding="utf-8")
        self._persist(session)
        return session, True

    def save_file(self, session: ProjectSession, relative_path: str, content: str) -> ProjectSession:
        if not isinstance(content, str) or len(content) > 500_000:
            raise ValueError("file content must be text up to 500 KB")
        candidate = Path(relative_path)
        if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
            raise ValueError("path must stay inside the project workspace")
        target = (session.workspace / candidate).resolve()
        if session.workspace not in target.parents:
            raise ValueError("path must stay inside the project workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        session.revision += 1
        self._persist(session)
        return session

    def file_tree(self, session: ProjectSession) -> list[str]:
        visible_suffixes = {".json", ".py", ".urdf", ".md", ".xml", ".yaml", ".yml"}
        return sorted(
            str(path.relative_to(session.workspace))
            for path in session.workspace.rglob("*")
            if path.is_file()
            and "runs" not in path.relative_to(session.workspace).parts
            and "__pycache__" not in path.relative_to(session.workspace).parts
            and path.suffix in visible_suffixes
        )

    def read_visible_files(self, session: ProjectSession) -> dict[str, str]:
        contents: dict[str, str] = {}
        for relative in self.file_tree(session):
            path = session.workspace / relative
            try:
                contents[relative] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
        return contents

    def _chat_session(self, session: ProjectSession, session_id: str | None = None) -> dict[str, object]:
        requested = session_id or session.active_chat_session_id
        assert session.chat_sessions
        match = next((item for item in session.chat_sessions if item["session_id"] == requested), None)
        if match is None:
            raise ValueError("conversation not found in this project")
        return match

    def set_codex_thread(self, session: ProjectSession, thread_id: str, chat_session_id: str | None = None) -> None:
        self._chat_session(session, chat_session_id)["codex_thread_id"] = thread_id
        self._persist(session)

    def list_chat_sessions(self, session: ProjectSession) -> list[dict[str, object]]:
        assert session.chat_sessions
        return [{"session_id": item["session_id"], "title": item["title"]} for item in session.chat_sessions]

    def chat_history(self, session: ProjectSession) -> list[dict[str, str]]:
        messages = session.active_chat_session.get("messages", [])
        return [item for item in messages if isinstance(item, dict) and item.get("role") in {"user", "assistant"} and isinstance(item.get("text"), str)]

    def select_chat_session(self, session: ProjectSession, session_id: str) -> None:
        if not session.chat_sessions or not any(item["session_id"] == session_id for item in session.chat_sessions):
            raise ValueError("conversation not found in this project")
        session.active_chat_session_id = session_id
        self._persist(session)

    def create_chat_session(self, session: ProjectSession) -> ProjectSession:
        assert session.chat_sessions is not None
        entry: dict[str, object] = {"session_id": f"chat_{uuid.uuid4().hex[:12]}", "title": "New conversation", "codex_thread_id": None, "messages": []}
        session.chat_sessions.insert(0, entry)
        session.active_chat_session_id = str(entry["session_id"])
        self._persist(session)
        return session

    def append_chat_message(self, session: ProjectSession, role: str, text: str, chat_session_id: str | None = None) -> None:
        if role not in {"user", "assistant"}:
            return
        chat_session = self._chat_session(session, chat_session_id)
        messages = chat_session.setdefault("messages", [])
        if not isinstance(messages, list):
            messages = chat_session["messages"] = []
        messages.append({"role": role, "text": text})
        if role == "user" and chat_session.get("title") == "New conversation":
            chat_session["title"] = text.strip().replace("\n", " ")[:48] or "New conversation"
        self._persist(session)

    def set_codex_model(self, session: ProjectSession, model: str) -> None:
        if model not in SUPPORTED_CODEX_MODELS:
            raise ValueError("unsupported Codex model")
        session.codex_model = model
        self._persist(session)

    def record_agent_edit(self, session: ProjectSession) -> None:
        """Advance the user-visible revision after Codex writes the workspace."""
        session.revision += 1
        self._persist(session)

    def list_projects(self) -> list[str]:
        """Return project ids already persisted on disk, plus loaded sessions."""
        projects = set(self._sessions)
        for path in self.sessions_root.iterdir():
            if path.is_dir() and _PROJECT_ID.fullmatch(path.name):
                projects.add(path.name)
        return sorted(projects)

    def list_runs(self, session: ProjectSession) -> list[dict[str, object]]:
        """Return compact, browser-safe run records newest first."""
        runs_root = session.workspace / "runs"
        entries: list[dict[str, object]] = []
        if not runs_root.is_dir():
            return entries
        for run_dir in runs_root.iterdir():
            if not run_dir.is_dir() or not run_dir.name.startswith("run_"):
                continue
            metadata = self._read_metadata(run_dir / "run_record.json")
            if not metadata:
                continue
            telemetry = metadata.get("telemetry_summary")
            entries.append(
                {
                    "run_id": run_dir.name,
                    "status": metadata.get("status", "unknown"),
                    "mode": metadata.get("mode", "batch"),
                    "sim_time_s": telemetry.get("sim_time_s") if isinstance(telemetry, dict) else None,
                    "recorded_at": run_dir.stat().st_mtime,
                }
            )
        return sorted(entries, key=lambda entry: float(entry["recorded_at"]), reverse=True)

    def _persist(self, session: ProjectSession) -> None:
        (session.root / "project.json").write_text(
            json.dumps(
                {
                    "project_id": session.project_id,
                    "revision": session.revision,
                    "chat_sessions": session.chat_sessions,
                    "active_chat_session_id": session.active_chat_session_id,
                    "codex_model": session.codex_model,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _read_metadata(path: Path) -> dict[str, object]:
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _safe_model(value: object) -> str:
        return value if isinstance(value, str) and value in SUPPORTED_CODEX_MODELS else DEFAULT_CODEX_MODEL

    @staticmethod
    def _load_chat_sessions(metadata: dict[str, object]) -> list[dict[str, object]]:
        raw = metadata.get("chat_sessions")
        if isinstance(raw, list):
            sessions = [item for item in raw if isinstance(item, dict) and isinstance(item.get("session_id"), str)]
            if sessions:
                for item in sessions:
                    item.setdefault("title", "New conversation")
                    item.setdefault("codex_thread_id", None)
                    item.setdefault("messages", [])
                return sessions
        # Migrate the original single-thread project format without losing its memory.
        return [{"session_id": f"chat_{uuid.uuid4().hex[:12]}", "title": "New conversation", "codex_thread_id": metadata.get("codex_thread_id"), "messages": []}]
