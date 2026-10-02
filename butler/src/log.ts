import { EventEmitter } from "node:events";

export type LogLevel = "info" | "warn" | "error";

export type LogEntry = {
  ts: string;
  level: LogLevel;
  message: string;
};

const MAX = 300;
const buf: LogEntry[] = [];
const bus = new EventEmitter();
bus.setMaxListeners(50);

export function log(level: LogLevel, message: string): void {
  const entry = push(level, message);
  const line = `[${entry.ts}] ${level} ${message}`;
  if (level === "error") console.error(line);
  else console.log(line);
}

/** 把第三方 console 收进页面日志，不再打印一遍。 */
export function ingest(level: LogLevel, message: string): void {
  push(level, message);
}

function push(level: LogLevel, message: string): LogEntry {
  const entry: LogEntry = {
    ts: new Date().toISOString(),
    level,
    message,
  };
  buf.push(entry);
  if (buf.length > MAX) buf.shift();
  bus.emit("log", entry);
  return entry;
}

export function logs(): LogEntry[] {
  return [...buf];
}

export function onLog(fn: (e: LogEntry) => void): () => void {
  bus.on("log", fn);
  return () => bus.off("log", fn);
}
