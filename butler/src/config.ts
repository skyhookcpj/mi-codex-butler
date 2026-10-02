import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";

export type AbortOn = "final" | "stop";

export type Phrases = {
  /** 唤醒应答，音箱本地念 */
  wake: string;
  /** 受理语：Codex 开跑前先念一句；空 = 不念 */
  ack: string;
  newSession: string;
  busy: string;
  error: string;
  timeout: string;
  /** 唤醒后没听到字；空 = 静默回待机 */
  notHeard: string;
};

export type SpeakerParams = {
  /** 唤醒后等说话的超时 */
  listenTimeoutSec: number;
  /** 打断小爱后等多久再 TTS */
  ttsDelayAfterAbortMs: number;
  /** final = 拿到最终识别再打断；stop = 拿到 is_stop 就打断 */
  abortOn: AbortOn;
};

export type CodexConfig = {
  workspace: string;
  bin: string;
  /** 空 = 用 ~/.codex/config.toml 的模型 */
  model: string;
  timeoutSec: number;
};

export type ButlerConfig = {
  /** 端口只认配置文件 / 环境变量，页面不改 */
  wsPort: number;
  webPort: number;
  newSessionKeywords: string[];
  sessionIdleMinutes: number;
  phrases: Phrases;
  speaker: SpeakerParams;
  codex: CodexConfig;
};

/** 页面可改的部分 */
export type ConfigPatch = {
  newSessionKeywords?: unknown;
  sessionIdleMinutes?: unknown;
  phrases?: Partial<Record<keyof Phrases, unknown>>;
  speaker?: Partial<Record<keyof SpeakerParams, unknown>>;
  codex?: Partial<Record<keyof CodexConfig, unknown>>;
};

const HOME = homedir();

export function dataDir(): string {
  return process.env.MIHOME_HOME || join(HOME, ".mihome");
}

export function configPath(): string {
  return process.env.BUTLER_CONFIG || join(dataDir(), "butler.json");
}

export function defaultConfig(): ButlerConfig {
  return {
    wsPort: 4399,
    webPort: 8787,
    newSessionKeywords: ["新对话", "新会话", "重新开始"],
    sessionIdleMinutes: 30,
    phrases: {
      wake: "在呢",
      ack: "",
      newSession: "好，新开一轮",
      busy: "上一条还在办，稍等一下。",
      error: "这一次没办成，稍后再试。",
      timeout: "办得有点久，先不等了。",
      notHeard: "",
    },
    speaker: {
      listenTimeoutSec: 15,
      ttsDelayAfterAbortMs: 2000,
      abortOn: "final",
    },
    codex: {
      workspace: join(HOME, "mihome-workspace"),
      bin: "codex",
      model: "",
      timeoutSec: 180,
    },
  };
}

/** 读配置文件，再叠环境变量里的端口。旧版字段（小米账号等）自然丢掉。 */
export function loadConfig(): ButlerConfig {
  const base = defaultConfig();
  let cfg = base;
  const path = configPath();
  if (existsSync(path)) {
    try {
      const raw = JSON.parse(readFileSync(path, "utf8"));
      cfg = mergeConfig(base, raw);
      cfg.wsPort = port(raw?.wsPort, base.wsPort);
      cfg.webPort = port(raw?.webPort, base.webPort);
    } catch {
      cfg = base;
    }
  }
  cfg.wsPort = port(process.env.BUTLER_WS_PORT, cfg.wsPort);
  cfg.webPort = port(process.env.BUTLER_WEB_PORT, cfg.webPort);
  return cfg;
}

/** 端口沿用文件里原有的值：运行时的端口可能来自环境变量，不能写回文件。 */
export function saveConfig(cfg: ButlerConfig): void {
  const path = configPath();
  let filePorts: { wsPort?: number; webPort?: number } = {};
  if (existsSync(path)) {
    try {
      const raw = JSON.parse(readFileSync(path, "utf8"));
      filePorts = { wsPort: raw?.wsPort, webPort: raw?.webPort };
    } catch {
      // 坏文件直接覆盖
    }
  }
  const { wsPort: _ws, webPort: _web, ...rest } = cfg;
  const out = { wsPort: filePorts.wsPort, webPort: filePorts.webPort, ...rest };
  mkdirSync(dirname(path), { recursive: true });
  const tmp = `${path}.tmp`;
  writeFileSync(tmp, JSON.stringify(out, null, 2) + "\n", "utf8");
  renameSync(tmp, path);
}

/** 合并页面提交的改动。端口不在这里改。 */
export function mergeConfig(base: ButlerConfig, patch: ConfigPatch | null | undefined): ButlerConfig {
  const p = patch ?? {};
  const ph = p.phrases ?? {};
  const sp = p.speaker ?? {};
  const cx = p.codex ?? {};
  const keywords = Array.isArray(p.newSessionKeywords)
    ? [...new Set(p.newSessionKeywords.map((k) => String(k).trim()).filter(Boolean))]
    : base.newSessionKeywords;
  return {
    wsPort: base.wsPort,
    webPort: base.webPort,
    newSessionKeywords: keywords.length ? keywords : base.newSessionKeywords,
    sessionIdleMinutes: int(p.sessionIdleMinutes, base.sessionIdleMinutes, 1, 24 * 60),
    phrases: {
      wake: text(ph.wake, base.phrases.wake),
      ack: text(ph.ack, base.phrases.ack),
      newSession: text(ph.newSession, base.phrases.newSession),
      busy: text(ph.busy, base.phrases.busy),
      error: text(ph.error, base.phrases.error),
      timeout: text(ph.timeout, base.phrases.timeout),
      notHeard: text(ph.notHeard, base.phrases.notHeard),
    },
    speaker: {
      listenTimeoutSec: int(sp.listenTimeoutSec, base.speaker.listenTimeoutSec, 3, 60),
      ttsDelayAfterAbortMs: int(sp.ttsDelayAfterAbortMs, base.speaker.ttsDelayAfterAbortMs, 0, 10_000),
      abortOn: sp.abortOn === "final" || sp.abortOn === "stop" ? sp.abortOn : base.speaker.abortOn,
    },
    codex: {
      workspace: nonEmpty(cx.workspace, base.codex.workspace),
      bin: nonEmpty(cx.bin, base.codex.bin),
      model: text(cx.model, base.codex.model),
      timeoutSec: int(cx.timeoutSec, base.codex.timeoutSec, 10, 1800),
    },
  };
}

function text(v: unknown, fallback: string): string {
  return typeof v === "string" ? v.trim() : fallback;
}

function nonEmpty(v: unknown, fallback: string): string {
  const s = text(v, fallback);
  return s || fallback;
}

function int(v: unknown, fallback: number, min: number, max: number): number {
  if (v === "" || v === null || v === undefined) return fallback;
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, Math.round(n)));
}

function port(v: unknown, fallback: number): number {
  return int(v, fallback, 1, 65535);
}
