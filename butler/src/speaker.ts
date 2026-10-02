import { EventEmitter } from "node:events";
import type { IncomingMessage } from "node:http";
import { WebSocketServer, type WebSocket } from "ws";
import type { ButlerConfig } from "./config.ts";
import { log } from "./log.ts";
import {
  decode,
  encodeEvent,
  encodeRequest,
  encodeResponse,
  EV_CONFIG,
  EV_REPLY,
  EV_SAY,
  EV_UTTERANCE,
  utteranceText,
  type ReplyData,
  type Response,
} from "./protocol.ts";

export type SpeakerInfo = {
  connected: boolean;
  addr: string | null;
  since: number | null;
  version: string | null;
};

export type ShellResult = { stdout: string; stderr: string; exit_code: number };

const PING_MS = 15_000;

/** 音箱 Client 连进来的那一条 WS。同一时刻只认一台，新连接顶掉旧的。 */
export class SpeakerLink extends EventEmitter {
  private wss: WebSocketServer | null = null;
  private ws: WebSocket | null = null;
  private state: SpeakerInfo = { connected: false, addr: null, since: null, version: null };
  private pending = new Map<string, { resolve: (r: Response) => void; reject: (e: Error) => void }>();
  private pinger: NodeJS.Timeout | null = null;

  constructor(private readonly getConfig: () => ButlerConfig) {
    super();
  }

  listen(port: number, host = "0.0.0.0"): Promise<number> {
    return new Promise((resolve, reject) => {
      const wss = new WebSocketServer({ port, host });
      wss.once("error", reject);
      wss.once("listening", () => {
        wss.off("error", reject);
        wss.on("error", (err) => log("error", `WS：${err.message}`));
        const addr = wss.address();
        const bound = typeof addr === "object" && addr ? addr.port : port;
        log("info", `等音箱连接 ws://0.0.0.0:${bound}`);
        resolve(bound);
      });
      wss.on("connection", (ws, req) => this.attach(ws, req));
      this.wss = wss;
    });
  }

  async close(): Promise<void> {
    if (this.pinger) clearInterval(this.pinger);
    this.ws?.terminate();
    await new Promise<void>((r) => (this.wss ? this.wss.close(() => r()) : r()));
  }

  info(): SpeakerInfo {
    return { ...this.state };
  }

  /** 音箱不在线返回 false */
  sendEvent(event: string, data?: unknown): boolean {
    const ws = this.ws;
    if (!ws || ws.readyState !== ws.OPEN) return false;
    ws.send(encodeEvent(event, data));
    return true;
  }

  reply(data: ReplyData): boolean {
    return this.sendEvent(EV_REPLY, data);
  }

  say(text: string): boolean {
    return this.sendEvent(EV_SAY, { text });
  }

  pushConfig(): boolean {
    return this.sendEvent(EV_CONFIG, speakerConfigPayload(this.getConfig()));
  }

  /** 调音箱上的 RPC，比如 run_shell / get_version。 */
  call(command: string, payload?: unknown, timeoutMs = 10_000): Promise<Response> {
    const ws = this.ws;
    if (!ws || ws.readyState !== ws.OPEN) return Promise.reject(new Error("音箱没连上"));
    const { id, frame } = encodeRequest(command, payload);
    return new Promise<Response>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${command} 超时`));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (r) => {
          clearTimeout(timer);
          resolve(r);
        },
        reject: (e) => {
          clearTimeout(timer);
          reject(e);
        },
      });
      ws.send(frame);
    });
  }

  async runShell(script: string, timeoutMs = 10_000): Promise<ShellResult> {
    const res = await this.call("run_shell", script, timeoutMs);
    if (res.code !== undefined && res.code !== 0) throw new Error(res.msg || `run_shell code ${res.code}`);
    return res.data as ShellResult;
  }

  private attach(ws: WebSocket, req: IncomingMessage): void {
    const addr = (req.socket.remoteAddress || "").replace(/^::ffff:/, "") || null;
    if (this.ws) {
      log("info", `新连接 ${addr} 顶掉旧连接 ${this.state.addr}`);
      this.detach(this.ws);
      this.ws.close(4000, "replaced");
    }
    this.ws = ws;
    this.state = { connected: true, addr, since: Date.now(), version: null };
    log("info", `音箱已连接 ${addr}`);
    this.emit("state", this.info());

    let alive = true;
    ws.on("pong", () => (alive = true));
    if (this.pinger) clearInterval(this.pinger);
    this.pinger = setInterval(() => {
      if (this.ws !== ws) return;
      if (!alive) {
        log("warn", "音箱心跳超时，断开");
        ws.terminate();
        return;
      }
      alive = false;
      ws.ping();
    }, PING_MS);

    ws.on("message", (raw, isBinary) => {
      if (isBinary || this.ws !== ws) return;
      this.onFrame(ws, raw.toString());
    });
    ws.on("close", () => {
      if (this.ws !== ws) return;
      this.detach(ws);
      this.ws = null;
      this.state = { connected: false, addr: this.state.addr, since: Date.now(), version: null };
      log("warn", "音箱断开");
      this.emit("state", this.info());
    });
    ws.on("error", (err) => log("warn", `音箱连接：${err.message}`));

    this.pushConfig();
    this.call("get_version", undefined, 5_000)
      .then((r) => {
        if (this.ws !== ws) return;
        this.state.version = typeof r.data === "string" ? r.data : JSON.stringify(r.data ?? null);
        this.emit("state", this.info());
      })
      .catch(() => undefined);
  }

  private detach(ws: WebSocket): void {
    ws.removeAllListeners("message");
    for (const [, p] of this.pending) p.reject(new Error("音箱断开"));
    this.pending.clear();
  }

  private onFrame(ws: WebSocket, frame: string): void {
    const msg = decode(frame);
    if (!msg) {
      log("warn", `看不懂的帧：${frame.slice(0, 200)}`);
      return;
    }
    if ("Response" in msg) {
      const p = this.pending.get(msg.Response.id);
      if (p) {
        this.pending.delete(msg.Response.id);
        p.resolve(msg.Response);
      }
      return;
    }
    if ("Request" in msg) {
      // NAS 不提供 RPC
      ws.send(encodeResponse({ id: msg.Request.id, code: -1, msg: "command not found" }));
      return;
    }
    const { id, event, data } = msg.Event;
    switch (event) {
      case EV_UTTERANCE: {
        const text = utteranceText(data);
        if (text) this.emit("utterance", id, text);
        else this.reply({ reply_to: id, kind: "cancelled", text: "" });
        return;
      }
      case "kws":
        log("info", `唤醒词：${JSON.stringify(data)}`);
        return;
      case "playing":
      case "instruction":
        // 上游 Client 的原样转发，太吵，不记
        return;
      default:
        log("info", `音箱事件 ${event}：${JSON.stringify(data ?? null).slice(0, 200)}`);
    }
  }
}

/** 下发给音箱的那部分：话术和音箱端参数。 */
export function speakerConfigPayload(cfg: ButlerConfig) {
  return {
    phrases: {
      wake: cfg.phrases.wake,
      busy: cfg.phrases.busy,
      error: cfg.phrases.error,
      timeout: cfg.phrases.timeout,
      not_heard: cfg.phrases.notHeard,
    },
    listen_timeout_sec: cfg.speaker.listenTimeoutSec,
    tts_delay_after_abort_ms: cfg.speaker.ttsDelayAfterAbortMs,
    abort_on: cfg.speaker.abortOn,
    // 音箱等回复的上限：比 Codex 超时多留一点，让 NAS 的超时语先到
    reply_timeout_sec: cfg.codex.timeoutSec + 15,
  };
}
