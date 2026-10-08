import { EventEmitter } from "node:events";
import { randomUUID } from "node:crypto";
import type { ButlerConfig } from "./config.ts";
import { CodexError, type CodexRunner, type CodexStep } from "./codex.ts";
import { log } from "./log.ts";
import type { ReplyKind } from "./protocol.ts";
import { emptySession, type HistoryEntry, type HistoryStore, type SessionState, type SessionStore } from "./store.ts";
import { matchNewSession, toSpeech } from "./text.ts";

export type Source = "speaker" | "web";

export type Outcome = { kind: ReplyKind; text: string };

export type TaskInfo = { text: string; source: Source; startedAt: number };

/** 演示页上的一轮：听到什么、Codex 每一步、最后的回复 */
export type LiveStep = {
  id: string;
  type: "command" | "note";
  text: string;
  status?: "running" | "ok" | "fail";
  at: number;
  endedAt?: number;
};

export type LiveRound = {
  id: string;
  source: Source;
  text: string;
  startedAt: number;
  steps: LiveStep[];
  reply: { kind: ReplyKind; text: string; at: number } | null;
};

const LIVE_ROUNDS = 5;

export type ButlerState = {
  session: SessionState & { expiresAt: number | null };
  task: TaskInfo | null;
};

export type ButlerDeps = {
  config: () => ButlerConfig;
  runner: CodexRunner;
  sessions: SessionStore;
  history: HistoryStore;
  /** 受理语等「随口一句」，音箱不在线就算了 */
  say: (text: string) => void;
  now?: () => number;
};

/** 只转交原话。音箱在哪、怎么回话（长短、口吻）写在工作区 AGENTS.md 里，改文档即生效。 */
export function buildPrompt(text: string): string {
  return ["用户对着音箱说：", text].join("\n");
}

/**
 * 收字 → 新会话关键词 / 忙碌 / 空闲过期 → Codex 新建或续写 → 一句口语。
 * 同一时间只跑一个 Codex。
 */
export class Butler extends EventEmitter {
  private session: SessionState;
  private task: TaskInfo | null = null;
  private rounds: LiveRound[] = [];
  private readonly now: () => number;

  constructor(private readonly deps: ButlerDeps) {
    super();
    this.now = deps.now ?? Date.now;
    this.session = deps.sessions.load();
  }

  state(): ButlerState {
    return {
      session: { ...this.session, expiresAt: this.expiresAt() },
      task: this.task ? { ...this.task } : null,
    };
  }

  history(limit?: number): HistoryEntry[] {
    return this.deps.history.list(limit);
  }

  /** 最近几轮的实时过程，新的在后 */
  live(): LiveRound[] {
    return structuredClone(this.rounds);
  }

  resetSession(reason: string): void {
    log(
      "info",
      this.session.id
        ? `丢掉 session ${this.session.id.slice(0, 8)}（${reason}）`
        : `新开一轮（${reason}；本来就没有 session）`,
    );
    this.session = emptySession();
    this.deps.sessions.save(this.session);
    this.changed();
  }

  cancel(): boolean {
    if (!this.task) return false;
    log("info", "手动停止当前任务");
    return this.deps.runner.cancel();
  }

  async handle(rawText: string, source: Source): Promise<Outcome> {
    const cfg = this.deps.config();
    const started = this.now();
    let text = rawText.trim();
    log("info", `${source === "web" ? "模拟" : "音箱"}：${text}`);
    const round = this.openRound(text, source, started);

    const hit = matchNewSession(text, cfg.newSessionKeywords);
    if (hit.kind !== "none") {
      if (this.task) return this.record(round, text, source, started, { kind: "busy", text: cfg.phrases.busy });
      this.resetSession("新会话关键词");
      if (hit.kind === "reset") {
        return this.record(round, text, source, started, { kind: "new_session", text: cfg.phrases.newSession });
      }
      text = hit.rest;
    }

    if (this.task) {
      return this.record(round, text, source, started, { kind: "busy", text: cfg.phrases.busy });
    }

    const exp = this.expiresAt();
    if (exp !== null && this.now() > exp) this.resetSession(`空闲超过 ${cfg.sessionIdleMinutes} 分钟`);

    this.task = { text, source, startedAt: started };
    this.changed();
    if (cfg.phrases.ack) this.deps.say(cfg.phrases.ack);

    try {
      const res = await this.runWithFallback(text, cfg, (s) => this.onStep(round, s));
      const isNew = res.sessionId !== this.session.id;
      this.session = {
        id: res.sessionId,
        rounds: isNew ? 1 : this.session.rounds + 1,
        startedAt: isNew ? this.now() : this.session.startedAt,
        lastActiveAt: this.now(),
      };
      this.deps.sessions.save(this.session);
      const speech = toSpeech(res.text) || cfg.phrases.error;
      return this.record(round, text, source, started, { kind: "reply", text: speech });
    } catch (err) {
      const e = err instanceof CodexError ? err : new CodexError("failed", String((err as Error)?.message ?? err));
      log(e.kind === "cancelled" ? "info" : "error", e.message);
      const outcome: Outcome =
        e.kind === "timeout"
          ? { kind: "timeout", text: cfg.phrases.timeout }
          : e.kind === "cancelled"
            ? { kind: "cancelled", text: "" }
            : { kind: "error", text: cfg.phrases.error };
      return this.record(round, text, source, started, outcome, e.message);
    } finally {
      this.task = null;
      this.changed();
    }
  }

  /** resume 在 Codex 动手前就挂了（比如 session 文件没了），换新 session 再来一次。 */
  private async runWithFallback(text: string, cfg: ButlerConfig, onStep: (s: CodexStep) => void) {
    const prompt = buildPrompt(text);
    const sessionId = this.session.id;
    try {
      return await this.deps.runner.run({ sessionId, prompt, cfg: cfg.codex, onStep });
    } catch (err) {
      if (sessionId && err instanceof CodexError && err.kind === "failed" && err.beforeTurn) {
        log("warn", `resume 失败，换新 session：${err.message}`);
        this.resetSession("resume 失败");
        return await this.deps.runner.run({ sessionId: null, prompt, cfg: cfg.codex, onStep });
      }
      throw err;
    }
  }

  private expiresAt(): number | null {
    if (!this.session.id || !this.session.lastActiveAt) return null;
    return this.session.lastActiveAt + this.deps.config().sessionIdleMinutes * 60_000;
  }

  private openRound(text: string, source: Source, started: number): LiveRound {
    const round: LiveRound = { id: randomUUID(), source, text, startedAt: started, steps: [], reply: null };
    this.rounds.push(round);
    if (this.rounds.length > LIVE_ROUNDS) this.rounds.shift();
    this.emit("live", structuredClone(round));
    return round;
  }

  private onStep(round: LiveRound, s: CodexStep): void {
    const at = this.now();
    if (s.type === "note") {
      round.steps.push({ id: `note-${round.steps.length}`, type: "note", text: s.text, at });
    } else {
      const prev = round.steps.find((x) => x.type === "command" && x.id === s.id);
      if (prev) {
        prev.status = s.status;
        if (s.status !== "running") prev.endedAt = at;
      } else {
        const step: LiveStep = { id: s.id, type: "command", text: s.command, status: s.status, at };
        if (s.status !== "running") step.endedAt = at;
        round.steps.push(step);
      }
    }
    this.emit("live", structuredClone(round));
  }

  private record(round: LiveRound, text: string, source: Source, started: number, outcome: Outcome, error?: string): Outcome {
    round.reply = { kind: outcome.kind, text: outcome.text, at: this.now() };
    this.emit("live", structuredClone(round));
    const label = { reply: "回复", new_session: "新开一轮", busy: "忙碌", error: "出错", timeout: "超时", cancelled: "已停止" }[
      outcome.kind
    ];
    log("info", `${label}：${outcome.text || "（不念）"}`);
    const entry: HistoryEntry = {
      id: randomUUID(),
      ts: started,
      source,
      text,
      kind: outcome.kind,
      reply: outcome.text,
      durationMs: this.now() - started,
      sessionId: this.session.id,
      ...(error ? { error } : {}),
    };
    this.deps.history.add(entry);
    this.emit("history", entry);
    return outcome;
  }

  private changed(): void {
    this.emit("state", this.state());
  }
}
