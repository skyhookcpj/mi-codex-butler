import assert from "node:assert/strict";
import { test } from "node:test";
import WebSocket from "ws";
import { defaultConfig } from "./config.ts";
import { decode, encodeEvent, encodeResponse, EV_UTTERANCE, type AppMessage } from "./protocol.ts";
import { SpeakerLink } from "./speaker.ts";

/** 假音箱，按 Open-XiaoAI Client 的帧格式说话，自动回 RPC。 */
class FakeClient {
  readonly ws: WebSocket;
  readonly inbox: AppMessage[] = [];
  private waiters: Array<() => void> = [];

  constructor(url: string) {
    this.ws = new WebSocket(url);
    this.ws.on("message", (raw) => {
      const msg = decode(raw.toString());
      if (!msg) return;
      if ("Request" in msg) {
        const { id, command } = msg.Request;
        const data = command === "get_version" ? "1.0.0-test" : { stdout: "ok\n", stderr: "", exit_code: 0 };
        this.ws.send(encodeResponse({ id, code: 0, msg: "success", data }));
      }
      this.inbox.push(msg);
      for (const w of this.waiters.splice(0)) w();
    });
  }

  open(): Promise<void> {
    return new Promise((r) => this.ws.once("open", () => r()));
  }

  async event(name: string): Promise<{ id: string; data: any }> {
    for (;;) {
      const i = this.inbox.findIndex((m) => "Event" in m && m.Event.event === name);
      if (i >= 0) {
        const m = this.inbox.splice(i, 1)[0] as { Event: { id: string; data: any } };
        return m.Event;
      }
      await new Promise<void>((r) => this.waiters.push(r));
    }
  }
}

async function start() {
  const cfg = defaultConfig();
  const link = new SpeakerLink(() => cfg);
  const port = await link.listen(0, "127.0.0.1");
  return { link, url: `ws://127.0.0.1:${port}` };
}

test("on connect: config is pushed and version fetched", async () => {
  const { link, url } = await start();
  const states: boolean[] = [];
  link.on("state", (s) => states.push(s.connected));
  const c = new FakeClient(url);
  await c.open();
  const cfg = await c.event("butler_config");
  assert.equal(cfg.data.phrases.wake, "在呢");
  assert.equal(cfg.data.reply_timeout_sec, 195);
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(link.info().version, "1.0.0-test");
  assert.equal(link.info().addr, "127.0.0.1");
  c.ws.close();
  await link.close();
});

test("utterance → reply with reply_to", async () => {
  const { link, url } = await start();
  link.on("utterance", (id: string, text: string) => {
    link.reply({ reply_to: id, kind: "reply", text: `听到了：${text}` });
  });
  const c = new FakeClient(url);
  await c.open();
  const frame = encodeEvent(EV_UTTERANCE, { text: " 客厅现在怎样 " });
  const sentId = (JSON.parse(frame) as { Event: { id: string } }).Event.id;
  c.ws.send(frame);
  const reply = await c.event("butler_reply");
  assert.deepEqual(reply.data, { reply_to: sentId, kind: "reply", text: "听到了：客厅现在怎样" });
  c.ws.close();
  await link.close();
});

test("empty utterance is closed out silently", async () => {
  const { link, url } = await start();
  let emitted = false;
  link.on("utterance", () => (emitted = true));
  const c = new FakeClient(url);
  await c.open();
  c.ws.send(encodeEvent(EV_UTTERANCE, { text: "  " }));
  const reply = await c.event("butler_reply");
  assert.equal(reply.data.kind, "cancelled");
  assert.equal(reply.data.text, "");
  assert.equal(emitted, false);
  c.ws.close();
  await link.close();
});

test("run_shell RPC round trip", async () => {
  const { link, url } = await start();
  const c = new FakeClient(url);
  await c.open();
  await c.event("butler_config");
  const res = await link.runShell("echo ok");
  assert.equal(res.stdout, "ok\n");
  c.ws.close();
  await link.close();
});

test("a new connection replaces the old one", async () => {
  const { link, url } = await start();
  const a = new FakeClient(url);
  await a.open();
  const closed = new Promise<number>((r) => a.ws.once("close", (code) => r(code)));
  const b = new FakeClient(url);
  await b.open();
  assert.equal(await closed, 4000);
  assert.equal(link.info().connected, true);
  assert.ok(link.say("测试"));
  assert.equal((await b.event("butler_say")).data.text, "测试");
  b.ws.close();
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(link.info().connected, false);
  assert.equal(link.say("没人听"), false);
  await link.close();
});
