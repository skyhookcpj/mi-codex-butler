import { spawn, type ChildProcess } from "node:child_process";
import { dirname, join } from "node:path";
import type { CodexConfig } from "./config.ts";
import { dataDir } from "./config.ts";
import { log } from "./log.ts";

/** Codex 这一轮正在做的事：跑命令、说中间的话。演示页实时显示。 */
export type CodexStep =
  | { type: "command"; id: string; command: string; status: "running" | "ok" | "fail" }
  | { type: "note"; text: string };

export type CodexRun = {
  /** 已有 session 就 resume，否则新建 */
  sessionId: string | null;
  prompt: string;
  cfg: CodexConfig;
  onStep?: (step: CodexStep) => void;
};

export type CodexResult = { sessionId: string; text: string };

export type CodexErrorKind = "timeout" | "cancelled" | "failed";

export class CodexError extends Error {
  /** turn.started 之前就失败，说明 Codex 还没动手，重试是安全的 */
  constructor(
    readonly kind: CodexErrorKind,
    message: string,
    readonly beforeTurn = false,
  ) {
    super(message);
  }
}

/** Butler 只依赖这个接口，测试里换假的。 */
export interface CodexRunner {
  run(job: CodexRun): Promise<CodexResult>;
  cancel(): boolean;
}

/** turn.completed 之后 codex 还要拖约 10s 才退出，给这么久收尾再结束它。 */
const EXIT_GRACE_MS = 1500;

export function codexArgs(job: CodexRun): string[] {
  const { cfg, sessionId, prompt } = job;
  const mihome = dataDir();
  const model = cfg.model ? ["-m", cfg.model] : [];
  if (sessionId) {
    // resume 不认 -C / -s / --add-dir：cwd 由 spawn 决定，sandbox 用 -c 给
    return [
      "exec",
      "resume",
      "--skip-git-repo-check",
      "--json",
      "-c",
      'sandbox_mode="workspace-write"',
      "-c",
      "sandbox_workspace_write.network_access=true",
      "-c",
      `sandbox_workspace_write.writable_roots=${JSON.stringify([mihome])}`,
      ...model,
      sessionId,
      prompt,
    ];
  }
  return [
    "exec",
    "--skip-git-repo-check",
    "--json",
    "-C",
    cfg.workspace,
    "-s",
    "workspace-write",
    "-c",
    "sandbox_workspace_write.network_access=true",
    "--add-dir",
    mihome,
    ...model,
    prompt,
  ];
}

/** 逐行吃 `codex exec --json` 的 JSONL。 */
export class CodexStream {
  sessionId: string | null = null;
  lastMessage = "";
  turnStarted = false;
  done = false;
  failure: string | null = null;
  /** 最近一条 agent_message 后面又有动作，它就是中间的话；一直到结束都没有，它就是回复 */
  private pendingNote: string | null = null;

  constructor(private readonly onStep?: (step: CodexStep) => void) {}

  /** 返回值：这一行之后整轮是否已经结束（成功或失败） */
  feed(line: string): boolean {
    const s = line.trim();
    if (!s.startsWith("{")) return false;
    let ev: any;
    try {
      ev = JSON.parse(s);
    } catch {
      return false;
    }
    switch (ev?.type) {
      case "thread.started":
        if (typeof ev.thread_id === "string") this.sessionId = ev.thread_id;
        break;
      case "turn.started":
        this.turnStarted = true;
        break;
      case "item.started": {
        const item = ev.item ?? {};
        if (item.type === "command_execution" && typeof item.command === "string") {
          this.flushNote();
          this.step({ type: "command", id: String(item.id), command: item.command, status: "running" });
        }
        break;
      }
      case "item.completed": {
        const item = ev.item ?? {};
        if (item.type === "agent_message" && typeof item.text === "string") {
          this.flushNote();
          this.lastMessage = item.text;
          this.pendingNote = item.text;
        } else if (item.type === "error" && typeof item.message === "string") {
          log("warn", `codex：${firstLine(item.message)}`);
        } else if (item.type === "command_execution" && typeof item.command === "string") {
          log("info", `codex $ ${item.command.slice(0, 300)}`);
          this.flushNote();
          const ok = item.exit_code === 0 || (item.exit_code == null && item.status === "completed");
          this.step({ type: "command", id: String(item.id), command: item.command, status: ok ? "ok" : "fail" });
        }
        break;
      }
      case "turn.completed":
        this.done = true;
        return true;
      case "turn.failed":
        this.failure = ev.error?.message ?? "turn.failed";
        return true;
      case "error":
        this.failure = ev.message ?? "error";
        return true;
    }
    return false;
  }

  private flushNote(): void {
    if (this.pendingNote?.trim()) this.step({ type: "note", text: this.pendingNote.trim() });
    this.pendingNote = null;
  }

  private step(s: CodexStep): void {
    try {
      this.onStep?.(s);
    } catch (err) {
      log("warn", `演示页回调出错：${(err as Error)?.message ?? err}`);
    }
  }
}

export class SpawnCodexRunner implements CodexRunner {
  private child: ChildProcess | null = null;
  private cancelled = false;
  /** 上一个进程彻底退出前不起下一个，免得两个进程同时写一个 session */
  private lastExit: Promise<void> = Promise.resolve();

  cancel(): boolean {
    if (!this.child) return false;
    this.cancelled = true;
    killTree(this.child, "SIGKILL");
    return true;
  }

  async run(job: CodexRun): Promise<CodexResult> {
    // 正常 1.5s 内就退了；万一卡住也别把下一句堵死
    await Promise.race([this.lastExit, new Promise((r) => setTimeout(r, 5000).unref())]);
    this.cancelled = false;
    const args = codexArgs(job);
    const timeoutMs = job.cfg.timeoutSec * 1000;
    log("info", `codex ${job.sessionId ? `resume ${job.sessionId.slice(0, 8)}` : "new"}`);

    const child = spawn(job.cfg.bin, args, {
      cwd: job.cfg.workspace,
      env: { ...process.env, PATH: searchPath() },
      stdio: ["ignore", "pipe", "pipe"],
      // 自成进程组：npm 装的 codex 是 node 包装脚本再拉原生二进制，只杀外层会留下孤儿
      detached: true,
    });
    this.child = child;
    let exited!: () => void;
    this.lastExit = new Promise((r) => (exited = r));

    const stream = new CodexStream(job.onStep);
    let stderr = "";

    return new Promise<CodexResult>((resolve, reject) => {
      let settled = false;
      const settle = (fn: () => void) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        fn();
      };
      const finishTurn = () => {
        if (stream.failure) {
          settle(() => reject(new CodexError("failed", stream.failure!, !stream.turnStarted)));
        } else if (!stream.sessionId) {
          settle(() => reject(new CodexError("failed", "没拿到 session id")));
        } else if (!stream.lastMessage.trim()) {
          settle(() => reject(new CodexError("failed", "Codex 没有回复")));
        } else {
          settle(() => resolve({ sessionId: stream.sessionId!, text: stream.lastMessage.trim() }));
        }
        // 回复已经拿到，不等它自己慢慢退
        setTimeout(() => {
          if (child.exitCode === null && child.signalCode === null) killTree(child, "SIGTERM");
        }, EXIT_GRACE_MS).unref();
      };

      const timer = setTimeout(() => {
        killTree(child, "SIGKILL");
        settle(() => reject(new CodexError("timeout", `Codex 超时（${job.cfg.timeoutSec}s）`)));
      }, timeoutMs);

      let buf = "";
      child.stdout!.setEncoding("utf8");
      child.stdout!.on("data", (chunk: string) => {
        buf += chunk;
        let i: number;
        while ((i = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, i);
          buf = buf.slice(i + 1);
          if (!settled && stream.feed(line)) finishTurn();
        }
      });
      child.stderr!.setEncoding("utf8");
      child.stderr!.on("data", (chunk: string) => {
        stderr = (stderr + chunk).slice(-4000);
      });
      child.on("error", (err) => {
        exited();
        settle(() => reject(new CodexError("failed", `起不来 ${job.cfg.bin}：${err.message}`, true)));
      });
      child.on("close", (code, signal) => {
        if (this.child === child) this.child = null;
        exited();
        if (buf && !settled && stream.feed(buf)) finishTurn();
        if (settled) return;
        if (this.cancelled) {
          settle(() => reject(new CodexError("cancelled", "已停止")));
          return;
        }
        const tail = stderr.trim().split("\n").slice(-3).join(" ");
        settle(() =>
          reject(
            new CodexError(
              "failed",
              `Codex 退出 ${code ?? signal}${tail ? `：${tail.slice(-300)}` : ""}`,
              !stream.turnStarted,
            ),
          ),
        );
      });
    });
  }
}

function killTree(child: ChildProcess, signal: NodeJS.Signals): void {
  try {
    if (child.pid) process.kill(-child.pid, signal);
  } catch {
    child.kill(signal);
  }
}

/** codex 一般和 node 装在同一个 nvm bin 下；mihome 在 ~/.local/bin。 */
function searchPath(): string {
  const home = process.env.HOME || "";
  const parts = [dirname(process.execPath), join(home, ".local", "bin"), process.env.PATH || ""];
  return [...new Set(parts.filter(Boolean))].join(":");
}

function firstLine(s: string): string {
  return s.split("\n")[0].slice(0, 300);
}
