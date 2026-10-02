import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { dataDir } from "./config.ts";
import type { ReplyKind } from "./protocol.ts";

export type SessionState = {
  id: string | null;
  rounds: number;
  startedAt: number | null;
  lastActiveAt: number | null;
};

export type HistoryEntry = {
  id: string;
  ts: number;
  source: "speaker" | "web";
  text: string;
  kind: ReplyKind;
  reply: string;
  durationMs: number;
  sessionId: string | null;
  error?: string;
};

export function emptySession(): SessionState {
  return { id: null, rounds: 0, startedAt: null, lastActiveAt: null };
}

/** session 落盘，NAS 重启后还能接着聊（只要没过空闲超时）。 */
export class SessionStore {
  constructor(private readonly path = join(dataDir(), "butler-session.json")) {}

  load(): SessionState {
    if (!existsSync(this.path)) return emptySession();
    try {
      const raw = JSON.parse(readFileSync(this.path, "utf8"));
      return {
        id: typeof raw.id === "string" ? raw.id : null,
        rounds: Number(raw.rounds) || 0,
        startedAt: Number(raw.startedAt) || null,
        lastActiveAt: Number(raw.lastActiveAt) || null,
      };
    } catch {
      return emptySession();
    }
  }

  save(s: SessionState): void {
    writeAtomic(this.path, JSON.stringify(s, null, 2) + "\n");
  }
}

const HISTORY_KEEP = 200;

/** 最近对话，JSONL，超过一倍再截断。 */
export class HistoryStore {
  private items: HistoryEntry[];

  constructor(private readonly path = join(dataDir(), "butler-history.jsonl")) {
    this.items = this.read();
  }

  list(limit = 50): HistoryEntry[] {
    return this.items.slice(-limit).reverse();
  }

  add(e: HistoryEntry): void {
    this.items.push(e);
    mkdirSync(dirname(this.path), { recursive: true });
    if (this.items.length > HISTORY_KEEP * 2) {
      this.items = this.items.slice(-HISTORY_KEEP);
      writeAtomic(this.path, this.items.map((x) => JSON.stringify(x)).join("\n") + "\n");
    } else {
      appendFileSync(this.path, JSON.stringify(e) + "\n", "utf8");
    }
  }

  private read(): HistoryEntry[] {
    if (!existsSync(this.path)) return [];
    const out: HistoryEntry[] = [];
    for (const line of readFileSync(this.path, "utf8").split("\n")) {
      if (!line.trim()) continue;
      try {
        out.push(JSON.parse(line));
      } catch {
        // 坏行跳过
      }
    }
    return out.slice(-HISTORY_KEEP);
  }
}

function writeAtomic(path: string, content: string): void {
  mkdirSync(dirname(path), { recursive: true });
  const tmp = `${path}.tmp`;
  writeFileSync(tmp, content, "utf8");
  renameSync(tmp, path);
}
