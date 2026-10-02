import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { CodexError, CodexStream, SpawnCodexRunner, codexArgs } from "./codex.ts";
import { defaultConfig } from "./config.ts";

const cfg = { ...defaultConfig().codex, workspace: "/ws" };

test("new session args use -C/-s/--add-dir", () => {
  const args = codexArgs({ sessionId: null, prompt: "P", cfg });
  assert.deepEqual(args.slice(0, 5), ["exec", "--skip-git-repo-check", "--json", "-C", "/ws"]);
  assert.ok(args.includes("--add-dir"));
  assert.equal(args.at(-1), "P");
  assert.ok(!args.includes("resume"));
});

test("resume args avoid flags resume does not accept", () => {
  const args = codexArgs({ sessionId: "abc", prompt: "P", cfg: { ...cfg, model: "glm" } });
  assert.deepEqual(args.slice(0, 2), ["exec", "resume"]);
  for (const flag of ["-C", "-s", "--add-dir"]) assert.ok(!args.includes(flag), flag);
  assert.ok(args.includes('sandbox_mode="workspace-write"'));
  assert.deepEqual(args.slice(-4), ["-m", "glm", "abc", "P"]);
});

test("stream parses real codex 0.159 output", () => {
  const s = new CodexStream();
  const lines = [
    '{"type":"thread.started","thread_id":"01a0f6a0-6649-70c2-b898-67a477f906d2"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"error","message":"Codex is ignoring 1 unrecognized configuration setting."}}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"先查一下"}}',
    '{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"收到"}}',
  ];
  for (const l of lines) assert.equal(s.feed(l), false);
  assert.equal(s.feed('{"type":"turn.completed","usage":{}}'), true);
  assert.equal(s.sessionId, "01a0f6a0-6649-70c2-b898-67a477f906d2");
  assert.equal(s.lastMessage, "收到");
  assert.equal(s.turnStarted, true);
  assert.equal(s.failure, null);
});

test("stream reports turn.failed and ignores junk", () => {
  const s = new CodexStream();
  assert.equal(s.feed("Reading prompt from stdin..."), false);
  assert.equal(s.feed("{not json"), false);
  assert.equal(s.feed('{"type":"turn.failed","error":{"message":"boom"}}'), true);
  assert.equal(s.failure, "boom");
});

/** 假 codex：吐 JSONL 后赖着不退，模拟真 codex 的 10s 拖尾。 */
function fakeCodex(body: string): string {
  const dir = mkdtempSync(join(tmpdir(), "fake-codex-"));
  const bin = join(dir, "codex");
  writeFileSync(bin, `#!/bin/sh\n${body}\n`);
  chmodSync(bin, 0o755);
  return bin;
}

test("runner resolves on turn.completed without waiting for exit", async () => {
  const bin = fakeCodex(
    [
      `echo '{"type":"thread.started","thread_id":"t-1"}'`,
      `echo '{"type":"turn.started"}'`,
      `echo '{"type":"item.completed","item":{"type":"agent_message","text":"灯开了"}}'`,
      `echo '{"type":"turn.completed"}'`,
      "sleep 10",
    ].join("\n"),
  );
  const t0 = Date.now();
  const res = await new SpawnCodexRunner().run({ sessionId: null, prompt: "x", cfg: { ...cfg, bin, workspace: tmpdir() } });
  assert.deepEqual(res, { sessionId: "t-1", text: "灯开了" });
  assert.ok(Date.now() - t0 < 3000, "should not wait for the slow exit");
});

test("runner times out", async () => {
  const bin = fakeCodex("sleep 30");
  await assert.rejects(
    new SpawnCodexRunner().run({ sessionId: null, prompt: "x", cfg: { ...cfg, bin, workspace: tmpdir(), timeoutSec: 1 } }),
    (e: unknown) => e instanceof CodexError && e.kind === "timeout",
  );
});

test("runner cancel", async () => {
  const bin = fakeCodex(`echo '{"type":"turn.started"}'\nsleep 30`);
  const runner = new SpawnCodexRunner();
  const p = runner.run({ sessionId: null, prompt: "x", cfg: { ...cfg, bin, workspace: tmpdir() } });
  setTimeout(() => runner.cancel(), 200);
  await assert.rejects(p, (e: unknown) => e instanceof CodexError && e.kind === "cancelled");
});

test("runner failure before turn is marked retry-safe", async () => {
  const bin = fakeCodex(`echo 'no rollout found for thread id abc' >&2\nexit 1`);
  await assert.rejects(
    new SpawnCodexRunner().run({ sessionId: "abc", prompt: "x", cfg: { ...cfg, bin, workspace: tmpdir() } }),
    (e: unknown) => e instanceof CodexError && e.kind === "failed" && e.beforeTurn && /no rollout/.test(e.message),
  );
});
