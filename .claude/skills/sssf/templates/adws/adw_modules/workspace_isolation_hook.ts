import { Buffer } from "node:buffer";
import { spawnSync } from "node:child_process";
import type { HookAPI } from "@oh-my-pi/pi-coding-agent/extensibility/hooks";


function shellQuote(value: string): string {
  return `'${value.replaceAll("'", `'"'"'`)}'`;
}

function validateToolInput(
  python: string,
  runner: string,
  root: string,
  tool: string,
  input: Record<string, unknown>,
): void {
  const encoded = Buffer.from(JSON.stringify(input), "utf8").toString("base64");
  const result = spawnSync(python, [runner, "check-tool", root, tool, encoded], {
    encoding: "utf8",
    env: process.env,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    const detail = result.stderr.trim() || `policy checker exited ${result.status}`;
    throw new Error(detail);
  }
}

export default function workspaceIsolation(pi: HookAPI): void {
  pi.on("tool_call", async (event) => {
    const root = process.env.MAESTRO_ROLE_ROOT;
    const python = process.env.MAESTRO_ISOLATION_PYTHON;
    const runner = process.env.MAESTRO_ISOLATION_RUNNER;
    if (!root || !python || !runner) {
      return { block: true, reason: "MAESTRO_WORKTREE_BOUNDARY:isolation configuration missing" };
    }
    const tool = event.toolName.toLowerCase();
    const input = { ...(event.input as Record<string, unknown>) };
    try {
      if (tool === "bash") {
        const rawCwd = input.cwd;
        if (typeof rawCwd !== "string" || !rawCwd.trim()) {
          input.cwd = root;
        }
      }
      validateToolInput(python, runner, root, tool, input);
      if (tool !== "bash") {
        return {};
      }
      const cwd = input.cwd ?? root;
      const command = input.command;
      if (typeof cwd !== "string") throw new Error("bash cwd is not a string");
      if (typeof command !== "string") throw new Error("bash command missing");
      const encoded = Buffer.from(command, "utf8").toString("base64");
      return {
        input: {
          ...input,
          cwd: root,
          command: `${shellQuote(python)} ${shellQuote(runner)} run-bash ${shellQuote(root)} ${shellQuote(cwd)} ${shellQuote(encoded)}`,
        },
      };

    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      return { block: true, reason: `MAESTRO_WORKTREE_BOUNDARY:${detail}` };
    }
  });
}
