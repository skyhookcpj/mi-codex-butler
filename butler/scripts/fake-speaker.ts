/**
 * 假音箱：连上 Server，敲一行字 = 音箱识别到一句话。用来在没有真音箱时调 Server。
 *
 *   npm run fake-speaker -- ws://<NAS_IP>:4399
 */
import { createInterface } from "node:readline";
import WebSocket from "ws";
import { decode, encodeEvent, encodeResponse, EV_UTTERANCE } from "../src/protocol.ts";

const url = process.argv[2] || "ws://127.0.0.1:4399";
const ws = new WebSocket(url);
const rl = createInterface({ input: process.stdin, output: process.stdout, prompt: "你说> " });

ws.on("open", () => {
  console.log(`已连接 ${url}`);
  rl.prompt();
});
ws.on("close", (code) => {
  console.log(`断开 ${code}`);
  process.exit(0);
});
ws.on("error", (err) => {
  console.error(err.message);
  process.exit(1);
});
ws.on("message", (raw) => {
  const msg = decode(raw.toString());
  if (!msg) return;
  if ("Request" in msg) {
    const { id, command, payload } = msg.Request;
    console.log(`\n[RPC] ${command} ${JSON.stringify(payload ?? null)}`);
    const data = command === "get_version" ? "fake-speaker" : { stdout: "", stderr: "", exit_code: 0 };
    ws.send(encodeResponse({ id, code: 0, msg: "success", data }));
  } else if ("Event" in msg) {
    const { event, data } = msg.Event;
    console.log(`\n[${event}] ${JSON.stringify(data)}`);
  }
  rl.prompt();
});

rl.on("line", (line) => {
  const text = line.trim();
  if (text) ws.send(encodeEvent(EV_UTTERANCE, { text }));
  else rl.prompt();
});
rl.on("close", () => ws.close());
