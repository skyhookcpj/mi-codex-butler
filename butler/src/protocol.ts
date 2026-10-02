/**
 * Open-XiaoAI 的 WS 文本帧（packages/client-rust/src/services/connect/data.rs）。
 * serde 外部标签枚举：{ "Request": {...} } / { "Response": {...} } / { "Event": {...} }。
 * 二进制帧是音频 Stream，管家用不到，直接忽略。
 */
import { randomUUID } from "node:crypto";

export type Request = { id: string; command: string; payload?: unknown };
export type Response = { id: string; code?: number; msg?: string; data?: unknown };
export type Event = { id: string; event: string; data?: unknown };

export type AppMessage =
  | { Request: Request }
  | { Response: Response }
  | { Event: Event };

/** 音箱 → NAS */
export const EV_UTTERANCE = "butler_utterance";
/** NAS → 音箱：回复某一句（reply_to = 那句 utterance 的 Event.id），text 为空表示不念、只结束等待 */
export const EV_REPLY = "butler_reply";
/** NAS → 音箱：随时念一句，不影响状态机 */
export const EV_SAY = "butler_say";
/** NAS → 音箱：话术和音箱端参数，连上时和保存配置后下发 */
export const EV_CONFIG = "butler_config";

export type ReplyKind = "reply" | "new_session" | "busy" | "error" | "timeout" | "cancelled";

export type UtteranceData = { text: string; dialog_id?: string };
export type ReplyData = { reply_to: string; kind: ReplyKind; text: string };
export type SayData = { text: string };

export function encodeEvent(event: string, data?: unknown): string {
  const msg: AppMessage = { Event: { id: randomUUID(), event, data } };
  return JSON.stringify(msg);
}

export function encodeRequest(command: string, payload?: unknown): { id: string; frame: string } {
  const id = randomUUID();
  const msg: AppMessage = { Request: { id, command, payload } };
  return { id, frame: JSON.stringify(msg) };
}

export function encodeResponse(res: Response): string {
  const msg: AppMessage = { Response: res };
  return JSON.stringify(msg);
}

export function decode(frame: string): AppMessage | null {
  let raw: unknown;
  try {
    raw = JSON.parse(frame);
  } catch {
    return null;
  }
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  for (const key of ["Request", "Response", "Event"] as const) {
    const body = obj[key];
    if (body && typeof body === "object" && typeof (body as { id?: unknown }).id === "string") {
      return { [key]: body } as AppMessage;
    }
  }
  return null;
}

export function utteranceText(data: unknown): string {
  if (!data || typeof data !== "object") return "";
  const t = (data as { text?: unknown }).text;
  return typeof t === "string" ? t.trim() : "";
}
