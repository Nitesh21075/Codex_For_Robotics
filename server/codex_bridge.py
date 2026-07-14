"""Async process bridge to the local TypeScript Codex SDK runner."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any


EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class CodexBridge:
    """One local runner process, preserving Codex threads by project id.

    The bridge deliberately stays server-side. If `CODEX_API_KEY` is absent,
    callers receive a clear event and the rest of the UI/simulator remains
    usable through its deterministic local path.
    """

    def __init__(self, root: str | Path, on_event: EventHandler) -> None:
        self.root = Path(root).resolve()
        self._load_local_environment()
        self.runner_root = self.root / "codex_runner"
        self.on_event = on_event
        self.process: asyncio.subprocess.Process | None = None
        self.reader_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def _load_local_environment(self) -> None:
        """Load only simple unset KEY=value entries from the project .env file.

        The browser never receives these values. This lets `uvicorn server.main:app`
        use the documented local setup without requiring an external dotenv plugin.
        """
        path = self.root / ".env"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key in {"CODEX_API_KEY", "ROBOPILOT_PYTHON"} and key not in os.environ:
                os.environ[key] = value.strip().strip('"').strip("'")

    @property
    def configured(self) -> bool:
        return bool(os.environ.get("CODEX_API_KEY"))

    async def submit(
        self,
        *,
        project_id: str,
        chat_session_id: str,
        workspace: Path,
        prompt: str,
        thread_id: str | None,
        model: str,
    ) -> bool:
        if not self.configured:
            await self.on_event(
                {
                    "type": "agent_step",
                    "project_id": project_id,
                    "role": "progress",
                    "summary": "Codex is not configured yet. Set CODEX_API_KEY in the server environment to activate the persistent agent.",
                }
            )
            return False
        await self._ensure_started()
        assert self.process and self.process.stdin
        payload = {
            "type": "turn",
            "projectId": project_id,
            "chatSessionId": chat_session_id,
            "workingDirectory": str(workspace),
            "prompt": prompt,
            "threadId": thread_id,
            "model": model,
        }
        async with self._lock:
            self.process.stdin.write((json.dumps(payload) + "\n").encode())
            await self.process.stdin.drain()
        return True

    async def stop(self, project_id: str) -> None:
        if self.process and self.process.stdin:
            async with self._lock:
                self.process.stdin.write((json.dumps({"type": "stop", "projectId": project_id}) + "\n").encode())
                await self.process.stdin.drain()

    async def close(self) -> None:
        if self.reader_task:
            self.reader_task.cancel()
        if self.process and self.process.returncode is None:
            self.process.terminate()
            await self.process.wait()

    async def _ensure_started(self) -> None:
        if self.process and self.process.returncode is None:
            return
        environment = os.environ.copy()
        environment.setdefault("ROBOPILOT_PYTHON", str(self.root / ".venv" / "bin" / "python"))
        self.process = await asyncio.create_subprocess_exec(
            "node",
            "--import",
            "tsx",
            "src/index.ts",
            cwd=self.runner_root,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.reader_task = asyncio.create_task(self._read_events())

    async def _read_events(self) -> None:
        assert self.process and self.process.stdout
        while line := await self.process.stdout.readline():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                await self.on_event(event)
