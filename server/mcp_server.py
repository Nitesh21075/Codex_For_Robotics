"""Stdio MCP server exposing the capability-limited RoboPilot project tools.

One process is launched for one project session.  Its only filesystem authority
is the ``--session-root`` passed at startup; it does not accept a session path
or arbitrary artifact path from an MCP client.

Run with::

    python -m server.mcp_server --session-root ./sessions/project_123
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Literal

from .project_tools import ProjectTools


def create_mcp_server(session_root: str | Path) -> Any:
    """Build the stdio server for exactly one session workspace.

    Importing ``mcp`` here keeps the ordinary FastAPI application importable in
    development environments before the optional MCP runtime dependency has
    been installed.  Starting this module still fails loudly with a useful
    install instruction instead of silently exposing a partial tool surface.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on runtime env
        if exc.name != "mcp":
            raise
        raise RuntimeError(
            "The RoboPilot MCP server requires the Python 'mcp' package. "
            "Install it in the backend environment before launching this module."
        ) from exc

    tools = ProjectTools(session_root)
    server = FastMCP("RoboPilot Project Tools")

    @server.tool(name="run_project", description="Run this session's project in batch or live mode.")
    def run_project(
        duration_s: float, mode: Literal["batch", "live"], scenario: str
    ) -> dict[str, Any]:
        return tools.run_project(duration_s=duration_s, mode=mode, scenario=scenario)

    @server.tool(name="get_run_summary", description="Return the compact outcome for one recorded run.")
    def get_run_summary(run_id: str) -> dict[str, Any]:
        return tools.get_run_summary(run_id)

    @server.tool(
        name="inspect_telemetry",
        description="Return a bounded timestamped numerical signal window from one recorded run.",
    )
    def inspect_telemetry(
        run_id: str,
        signals: list[str],
        start_s: float | None = None,
        end_s: float | None = None,
    ) -> dict[str, Any]:
        return tools.inspect_telemetry(
            run_id=run_id, signals=signals, start_s=start_s, end_s=end_s
        )

    @server.tool(
        name="get_visual_frame",
        description="Return a session-local path to the robot/simulator camera frame nearest a time.",
    )
    def get_visual_frame(
        run_id: str, camera: str, t: float | None = None
    ) -> dict[str, Any]:
        return tools.get_visual_frame(run_id=run_id, camera=camera, t=t)

    @server.tool(
        name="compare_runs",
        description="Compare task metrics between a base run and a candidate run.",
    )
    def compare_runs(base_run_id: str, candidate_run_id: str) -> dict[str, Any]:
        return tools.compare_runs(
            base_run_id=base_run_id, candidate_run_id=candidate_run_id
        )

    @server.tool(
        name="get_adapter_capabilities",
        description="List the approved simulator families and workflow for declaring a project-local adapter.",
    )
    def get_adapter_capabilities() -> dict[str, Any]:
        return tools.get_adapter_capabilities()

    @server.tool(
        name="create_adapter_draft",
        description="Declare this project's new morphology against one approved simulator family. This is not runnable until validation succeeds.",
    )
    def create_adapter_draft(
        morphology: str, family: str, display_name: str | None = None
    ) -> dict[str, Any]:
        return tools.create_adapter_draft(
            morphology=morphology, family=family, display_name=display_name
        )

    @server.tool(
        name="validate_adapter_draft",
        description="Perform a real short simulator run for the declared adapter and promote it only if the adapter launches without crashing.",
    )
    def validate_adapter_draft(duration_s: float = 3.0) -> dict[str, Any]:
        return tools.validate_adapter_draft(duration_s=duration_s)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="RoboPilot session-scoped MCP server")
    parser.add_argument(
        "--session-root",
        required=True,
        type=Path,
        help="Project workspace owned by this one MCP server process.",
    )
    args = parser.parse_args()
    create_mcp_server(args.session_root).run(transport="stdio")


if __name__ == "__main__":
    main()
