import { createInterface } from "node:readline";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Codex, type Input, type Thread, type ThreadEvent } from "@openai/codex-sdk";

type TurnCommand = {
  type: "turn";
  projectId: string;
  chatSessionId: string;
  workingDirectory: string;
  prompt: string;
  imagePaths?: string[];
  threadId?: string;
  model?: string;
};

type StopCommand = { type: "stop"; projectId: string };
type Command = TurnCommand | StopCommand;

type ActiveProject = {
  codex: Codex;
  thread: Thread;
  workingDirectory: string;
  model?: string;
  controller?: AbortController;
};

type ModelSelection = {
  model: string;
  reasoningEffort?: "minimal" | "low" | "medium" | "high" | "xhigh";
};

const projects = new Map<string, ActiveProject>();
const activeTurns = new Map<string, Promise<void>>();
const stoppedProjects = new Set<string>();
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const pythonExecutable = process.env.ROBOPILOT_PYTHON ?? "python3";

function emit(payload: Record<string, unknown>): void {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function safePath(candidate: string, root: string): string {
  const resolved = path.resolve(candidate);
  const allowedRoots = [path.resolve(root), path.resolve(repoRoot, "sessions")];
  if (!allowedRoots.some((allowed) => resolved === allowed || resolved.startsWith(`${allowed}${path.sep}`))) {
    throw new Error("Image path must stay inside the project session.");
  }
  return resolved;
}

function parseModelSelection(selection?: string): ModelSelection {
  const [model, reasoningEffort] = (selection ?? "").split(":", 2);
  return {
    model: model || undefined,
    reasoningEffort: reasoningEffort as ModelSelection["reasoningEffort"],
  } as ModelSelection;
}

function stageForFileChanges(changes: { path: string }[]): "morphology" | "build" | "export" {
  const paths = changes.map((change) => change.path.toLowerCase());
  if (paths.some((path) => path.endsWith("spec.json") || path.endsWith("robot.urdf") || path.endsWith(".urdf"))) return "morphology";
  if (paths.some((path) => path.includes("ros2_ws/") || path.includes("export"))) return "export";
  return "build";
}

function stageForCommand(command: string): "spec" | "heal" {
  const inspection = /(^|\s)(rg|ls|find|sed|cat|head|tail|pwd|tree)\b/.test(command.trim().toLowerCase());
  return inspection ? "spec" : "heal";
}

function createProject(projectId: string, workingDirectory: string, threadId?: string, model?: string): ActiveProject {
  const workspace = path.resolve(workingDirectory);
  const codex = new Codex({
    apiKey: process.env.CODEX_API_KEY,
    config: {
      mcp_servers: {
        robopilot: {
          command: pythonExecutable,
          args: ["-m", "server.mcp_server", "--session-root", workspace],
          env: { PYTHONPATH: repoRoot },
          cwd: repoRoot,
          required: true,
          startup_timeout_sec: 20,
          // This MCP server is bounded to one project workspace. Explicitly
          // approving it prevents an unanswerable approval prompt when the
          // Codex thread is configured with approvalPolicy: "never".
          default_tools_approval_mode: "approve",
          enabled_tools: [
            "run_project",
            "get_run_summary",
            "inspect_telemetry",
            "get_visual_frame",
            "compare_runs",
            "get_adapter_capabilities",
            "create_adapter_draft",
            "validate_adapter_draft",
          ],
          tool_timeout_sec: 180,
        },
      },
    },
  });
  const selection = parseModelSelection(model);
  const options = {
    workingDirectory: workspace,
    skipGitRepoCheck: true,
    sandboxMode: "workspace-write" as const,
    networkAccessEnabled: false,
    webSearchMode: "disabled" as const,
    approvalPolicy: "never" as const,
    model: selection.model,
    modelReasoningEffort: selection.reasoningEffort,
  };
  const thread = threadId ? codex.resumeThread(threadId, options) : codex.startThread(options);
  const project = { codex, thread, workingDirectory: workspace, model };
  projects.set(projectId, project);
  return project;
}

function publicEvent(projectId: string, chatSessionId: string, event: ThreadEvent): Record<string, unknown> | undefined {
  if (event.type === "thread.started") {
    return { type: "thread_started", project_id: projectId, chat_session_id: chatSessionId, thread_id: event.thread_id };
  }
  if (event.type === "turn.completed") {
    return { type: "turn_completed", project_id: projectId, usage: event.usage };
  }
  if (event.type === "turn.failed") {
    return { type: "turn_error", project_id: projectId, message: event.error.message };
  }
  if (event.type === "error") {
    return { type: "turn_error", project_id: projectId, message: event.message };
  }
  if (event.type === "item.completed" || event.type === "item.updated") {
    const item = event.item;
    if (item.type === "agent_message") return { type: "chat", project_id: projectId, chat_session_id: chatSessionId, role: "assistant", text: item.text };
    if (item.type === "file_change") return { type: "agent_step", project_id: projectId, role: "edit", summary: "Updated project files.", changes: item.changes, status: item.status, stage: stageForFileChanges(item.changes) };
    if (item.type === "mcp_tool_call") {
      const detail = item.status === "failed" && item.error?.message ? `: ${item.error.message}` : "";
      return { type: "agent_step", project_id: projectId, role: "tool_result", summary: `RoboPilot tool ${item.tool} ${item.status}${detail}.`, tool: item.tool, status: item.status, stage: "observe" };
    }
    if (item.type === "command_execution") return { type: "agent_step", project_id: projectId, role: "tool_result", summary: "Ran a project command.", status: item.status, exit_code: item.exit_code, stage: stageForCommand(item.command) };
  }
  return undefined;
}

async function handleTurn(command: TurnCommand): Promise<void> {
  const projectKey = `${command.projectId}:${command.chatSessionId}`;
  const current = projects.get(projectKey);
  const project = current && current.workingDirectory === path.resolve(command.workingDirectory) && current.model === command.model
    ? current
    : createProject(projectKey, command.workingDirectory, command.threadId, command.model);
  const input: Input = command.imagePaths?.length
    ? [{ type: "text", text: command.prompt }, ...command.imagePaths.map((image) => ({ type: "local_image" as const, path: safePath(image, project.workingDirectory) }))]
    : command.prompt;
  project.controller = new AbortController();
  stoppedProjects.delete(projectKey);
  emit({ type: "turn_started", project_id: command.projectId, model: command.model });
  try {
    const streamed = await project.thread.runStreamed(input, { signal: project.controller.signal });
    for await (const event of streamed.events) {
      const visible = publicEvent(command.projectId, command.chatSessionId, event);
      if (visible) emit(visible);
    }
  } finally {
    project.controller = undefined;
  }
}

function startTurn(command: TurnCommand): void {
  const projectKey = `${command.projectId}:${command.chatSessionId}`;
  if (activeTurns.has(projectKey)) {
    emit({ type: "runner_error", project_id: command.projectId, message: "Codex is already working in this conversation." });
    return;
  }
  const task = handleTurn(command)
    .catch((error) => {
      if (!stoppedProjects.has(projectKey)) {
        emit({ type: "runner_error", project_id: command.projectId, message: error instanceof Error ? error.message : "Unknown runner error" });
      }
    })
    .finally(() => {
      activeTurns.delete(projectKey);
      stoppedProjects.delete(projectKey);
    });
  activeTurns.set(projectKey, task);
}

async function handle(command: Command): Promise<void> {
  if (command.type === "stop") {
    for (const [key, project] of projects) {
      if (key.startsWith(`${command.projectId}:`)) {
        stoppedProjects.add(key);
        project.controller?.abort();
      }
    }
    emit({ type: "turn_stopped", project_id: command.projectId });
    return;
  }
  startTurn(command);
}

const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of lines) {
  let command: Command | undefined;
  try {
    command = JSON.parse(line) as Command;
    await handle(command);
  } catch (error) {
    emit({ type: "runner_error", project_id: command?.projectId, message: error instanceof Error ? error.message : "Unknown runner error" });
  }
}
