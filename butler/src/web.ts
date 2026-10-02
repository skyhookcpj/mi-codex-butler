import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Butler } from "./butler.ts";
import { mergeConfig, saveConfig, type ButlerConfig, type ConfigPatch } from "./config.ts";
import { log, logs, onLog } from "./log.ts";
import type { SpeakerLink } from "./speaker.ts";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "public");
const MAX_BODY = 64 * 1024;

export type WebDeps = {
  butler: Butler;
  speaker: SpeakerLink;
  getConfig: () => ButlerConfig;
  setConfig: (cfg: ButlerConfig) => void;
};

export function startWeb(port: number, deps: WebDeps, host = "0.0.0.0"): Promise<Server> {
  const server = createServer(async (req, res) => {
    try {
      await route(req, res, deps);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      log("error", message);
      if (!res.headersSent) json(res, 500, { error: message });
      else res.end();
    }
  });
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, host, () => {
      server.off("error", reject);
      log("info", `配置页 http://${host === "0.0.0.0" ? "127.0.0.1" : host}:${port}`);
      resolve(server);
    });
  });
}

function snapshot(deps: WebDeps) {
  return { now: Date.now(), speaker: deps.speaker.info(), ...deps.butler.state() };
}

async function route(req: IncomingMessage, res: ServerResponse, deps: WebDeps): Promise<void> {
  const { butler, speaker } = deps;
  const path = new URL(req.url || "/", "http://local").pathname;
  const method = req.method || "GET";

  if (method === "GET" && (path === "/" || path === "/index.html")) {
    return file(res, join(ROOT, "index.html"), "text/html; charset=utf-8");
  }
  if (method === "GET" && path === "/api/config") {
    return json(res, 200, deps.getConfig());
  }
  if (method === "PUT" && path === "/api/config") {
    const next = mergeConfig(deps.getConfig(), (await readBody(req)) as ConfigPatch);
    saveConfig(next);
    deps.setConfig(next);
    const pushed = speaker.pushConfig();
    log("info", `配置已保存${pushed ? "，已下发音箱" : ""}`);
    return json(res, 200, { ok: true, pushed, config: next });
  }
  if (method === "GET" && path === "/api/state") {
    return json(res, 200, snapshot(deps));
  }
  if (method === "GET" && path === "/api/history") {
    return json(res, 200, butler.history(50));
  }
  if (method === "POST" && path === "/api/session/reset") {
    butler.resetSession("页面");
    return json(res, 200, { ok: true });
  }
  if (method === "POST" && path === "/api/task/cancel") {
    return json(res, 200, { ok: butler.cancel() });
  }
  if (method === "POST" && path === "/api/simulate") {
    const body = (await readBody(req)) as { text?: unknown; speak?: unknown };
    const text = typeof body.text === "string" ? body.text.trim() : "";
    if (!text) return json(res, 400, { error: "说点什么" });
    const outcome = await butler.handle(text, "web");
    const spoken = body.speak === true && !!outcome.text && speaker.say(outcome.text);
    return json(res, 200, { ...outcome, spoken });
  }
  if (method === "POST" && path === "/api/say") {
    const body = (await readBody(req)) as { text?: unknown };
    const text = typeof body.text === "string" ? body.text.trim() : "";
    if (!text) return json(res, 400, { error: "说点什么" });
    if (!speaker.say(text)) return json(res, 409, { error: "音箱没连上" });
    return json(res, 200, { ok: true });
  }
  if (method === "GET" && path === "/api/stream") {
    return stream(req, res, deps);
  }
  json(res, 404, { error: "not found" });
}

/** 一条 SSE 推三种东西：log / state / history。 */
function stream(req: IncomingMessage, res: ServerResponse, deps: WebDeps): void {
  res.writeHead(200, {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
  });
  const send = (event: string, data: unknown) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  send("state", snapshot(deps));
  for (const e of logs()) send("log", e);

  const pushState = () => send("state", snapshot(deps));
  const pushHistory = (e: unknown) => send("history", e);
  const offLog = onLog((e) => send("log", e));
  deps.butler.on("state", pushState);
  deps.butler.on("history", pushHistory);
  deps.speaker.on("state", pushState);
  const keepalive = setInterval(() => res.write(": ping\n\n"), 25_000);

  req.on("close", () => {
    clearInterval(keepalive);
    offLog();
    deps.butler.off("state", pushState);
    deps.butler.off("history", pushHistory);
    deps.speaker.off("state", pushState);
  });
}

async function readBody(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const c of req) {
    size += (c as Buffer).length;
    if (size > MAX_BODY) throw new Error("请求太大");
    chunks.push(c as Buffer);
  }
  const raw = Buffer.concat(chunks).toString("utf8");
  if (!raw) return {};
  try {
    return JSON.parse(raw);
  } catch {
    throw new Error("请求不是 JSON");
  }
}

function json(res: ServerResponse, code: number, data: unknown): void {
  res.writeHead(code, { "Content-Type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(data));
}

function file(res: ServerResponse, path: string, type: string): void {
  if (!existsSync(path)) return json(res, 404, { error: "missing ui" });
  res.writeHead(200, { "Content-Type": type, "Cache-Control": "no-cache" });
  res.end(readFileSync(path));
}
