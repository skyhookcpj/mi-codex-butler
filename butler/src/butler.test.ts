import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { Butler } from "./butler.ts";
import { CodexError, type CodexResult, type CodexRun, type CodexRunner } from "./codex.ts";
import { defaultConfig, type ButlerConfig } from "./config.ts";
import { HistoryStore, SessionStore } from "./store.ts";

type Step = CodexResult | CodexError | ((job: CodexRun) => Promise<CodexResult>);

class FakeRunner implements CodexRunner {
  calls: CodexRun[] = [];
  cancelled = 0;
  constructor(private steps: Step[]) {}
  async run(job: CodexRun): Promise<CodexResult> {
    this.calls.push(job);
    const step = this.steps.shift();
    if (!step) throw new Error("no more steps");
    if (step instanceof CodexError) throw step;
    if (typeof step === "function") return step(job);
    return step;
  }
  cancel(): boolean {
    this.cancelled++;
    return true;
  }
}

function setup(steps: Step[], opts: { cfg?: Partial<ButlerConfig>; dir?: string } = {}) {
  const dir = opts.dir ?? mkdtempSync(join(tmpdir(), "butler-"));
  const cfg = { ...defaultConfig(), ...opts.cfg };
  const clock = { t: 1_000_000 };
  const said: string[] = [];
  const runner = new FakeRunner(steps);
  const butler = new Butler({
    config: () => cfg,
    runner,
    sessions: new SessionStore(join(dir, "session.json")),
    history: new HistoryStore(join(dir, "history.jsonl")),
    say: (t) => said.push(t),
    now: () => clock.t,
  });
  return { butler, runner, clock, said, cfg, dir };
}

test("first utterance creates a session, second resumes it", async () => {
  const { butler, runner } = setup([
    { sessionId: "s1", text: "客厅主灯开着。" },
    { sessionId: "s1", text: "窗帘也开着。" },
  ]);
  assert.deepEqual(await butler.handle("客厅现在怎样", "speaker"), { kind: "reply", text: "客厅主灯开着。" });
  assert.deepEqual(await butler.handle("那窗帘开了没", "speaker"), { kind: "reply", text: "窗帘也开着。" });
  assert.equal(runner.calls[0].sessionId, null);
  assert.equal(runner.calls[1].sessionId, "s1");
  assert.match(runner.calls[1].prompt, /那窗帘开了没/);
  assert.equal(butler.state().session.rounds, 2);
});

test("new-session keyword drops the session without running codex", async () => {
  const { butler, runner } = setup([
    { sessionId: "s1", text: "好" },
    { sessionId: "s2", text: "好" },
  ]);
  await butler.handle("打开客厅灯", "speaker");
  assert.deepEqual(await butler.handle("新对话", "speaker"), { kind: "new_session", text: "好，新开一轮" });
  assert.equal(runner.calls.length, 1);
  assert.equal(butler.state().session.id, null);
  await butler.handle("关灯", "speaker");
  assert.equal(runner.calls[1].sessionId, null);
});

test("keyword followed by a request runs it in a fresh session", async () => {
  const { butler, runner } = setup([
    { sessionId: "s1", text: "好" },
    { sessionId: "s2", text: "开了" },
  ]);
  await butler.handle("你好", "speaker");
  const out = await butler.handle("新对话，打开客厅主灯", "speaker");
  assert.equal(out.kind, "reply");
  assert.equal(runner.calls[1].sessionId, null);
  assert.match(runner.calls[1].prompt, /打开客厅主灯/);
  assert.doesNotMatch(runner.calls[1].prompt, /新对话/);
});

test("busy while a task runs; the running task still finishes", async () => {
  let release!: () => void;
  const { butler, runner } = setup([
    () => new Promise<CodexResult>((r) => (release = () => r({ sessionId: "s1", text: "办完了" }))),
  ]);
  const first = butler.handle("跑个长任务", "speaker");
  await new Promise((r) => setImmediate(r));
  assert.ok(butler.state().task);
  assert.deepEqual(await butler.handle("再来一个", "speaker"), { kind: "busy", text: "上一条还在办，稍等一下。" });
  assert.deepEqual(await butler.handle("新对话", "speaker"), { kind: "busy", text: "上一条还在办，稍等一下。" });
  release();
  assert.deepEqual(await first, { kind: "reply", text: "办完了。" });
  assert.equal(runner.calls.length, 1);
  assert.equal(butler.state().task, null);
});

test("idle timeout starts a new session", async () => {
  const { butler, runner, clock } = setup([
    { sessionId: "s1", text: "好" },
    { sessionId: "s2", text: "好" },
  ]);
  await butler.handle("一", "speaker");
  clock.t += 31 * 60_000;
  await butler.handle("二", "speaker");
  assert.equal(runner.calls[1].sessionId, null);
});

test("timeout / error / cancel map to phrases", async () => {
  const { butler } = setup([
    new CodexError("timeout", "Codex 超时（180s）"),
    new CodexError("failed", "Codex 退出 1", false),
    new CodexError("cancelled", "已停止"),
  ]);
  assert.deepEqual(await butler.handle("a", "speaker"), { kind: "timeout", text: "办得有点久，先不等了。" });
  assert.deepEqual(await butler.handle("b", "speaker"), { kind: "error", text: "这一次没办成，稍后再试。" });
  assert.deepEqual(await butler.handle("c", "speaker"), { kind: "cancelled", text: "" });
  const hist = butler.history();
  assert.equal(hist.length, 3);
  assert.equal(hist[0].kind, "cancelled");
  assert.equal(hist[2].error, "Codex 超时（180s）");
});

test("resume failing before the turn retries once as a new session", async () => {
  const { butler, runner } = setup([
    { sessionId: "s1", text: "好" },
    new CodexError("failed", "no rollout found", true),
    { sessionId: "s2", text: "接上了" },
  ]);
  await butler.handle("一", "speaker");
  assert.deepEqual(await butler.handle("二", "speaker"), { kind: "reply", text: "接上了。" });
  assert.deepEqual(
    runner.calls.map((c) => c.sessionId),
    [null, "s1", null],
  );
  assert.equal(butler.state().session.id, "s2");
});

test("resume failing mid-turn is not retried (could double-run actions)", async () => {
  const { butler, runner } = setup([
    { sessionId: "s1", text: "好" },
    new CodexError("failed", "Codex 退出 1", false),
  ]);
  await butler.handle("一", "speaker");
  assert.equal((await butler.handle("开灯", "speaker")).kind, "error");
  assert.equal(runner.calls.length, 2);
  assert.equal(butler.state().session.id, "s1");
});

test("ack phrase is said before codex runs", async () => {
  const { butler, said } = setup([{ sessionId: "s1", text: "好" }], {
    cfg: { phrases: { ...defaultConfig().phrases, ack: "好嘞" } },
  });
  await butler.handle("开灯", "speaker");
  assert.deepEqual(said, ["好嘞"]);
});

test("session and history survive a restart", async () => {
  const first = setup([{ sessionId: "s1", text: "好" }]);
  await first.butler.handle("开灯", "speaker");
  const again = setup([{ sessionId: "s1", text: "关了" }], { dir: first.dir });
  assert.equal(again.butler.state().session.id, "s1");
  assert.equal(again.butler.history().length, 1);
});

test("live rounds track each step and the reply", async () => {
  const { butler, clock } = setup([
    async (job) => {
      job.onStep?.({ type: "command", id: "c1", command: "mihome device control 1 2.1 false", status: "running" });
      clock.t += 1200;
      job.onStep?.({ type: "command", id: "c1", command: "mihome device control 1 2.1 false", status: "ok" });
      job.onStep?.({ type: "note", text: "灯已经关了" });
      return { sessionId: "s1", text: "客厅灯关了。" };
    },
  ]);
  const seen: string[] = [];
  butler.on("live", (r) => seen.push(r.steps.map((s: { status?: string }) => s.status ?? "note").join(",")));
  await butler.handle("把客厅灯关了", "speaker");
  const [round] = butler.live();
  assert.equal(round.text, "把客厅灯关了");
  assert.equal(round.steps.length, 2);
  assert.deepEqual(
    { status: round.steps[0].status, ms: round.steps[0].endedAt! - round.steps[0].at },
    { status: "ok", ms: 1200 },
  );
  assert.equal(round.steps[1].type, "note");
  assert.deepEqual(round.reply && { kind: round.reply.kind, text: round.reply.text }, { kind: "reply", text: "客厅灯关了。" });
  assert.deepEqual(seen, ["", "running", "ok", "ok,note", "ok,note"]);
});

test("live keeps only the last few rounds", async () => {
  const { butler } = setup(Array.from({ length: 7 }, (_, i) => ({ sessionId: "s", text: `第${i}句` })));
  for (let i = 0; i < 7; i++) await butler.handle(`说${i}`, "web");
  const live = butler.live();
  assert.equal(live.length, 5);
  assert.equal(live.at(-1)!.text, "说6");
});
