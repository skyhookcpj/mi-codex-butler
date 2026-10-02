import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { defaultConfig, loadConfig, mergeConfig, saveConfig } from "./config.ts";

test("keywords trim, dedupe, drop empty; empty list keeps previous", () => {
  const next = mergeConfig(defaultConfig(), { newSessionKeywords: [" 新对话 ", "", "新对话", "从头来"] });
  assert.deepEqual(next.newSessionKeywords, ["新对话", "从头来"]);
  const kept = mergeConfig(next, { newSessionKeywords: [" "] });
  assert.deepEqual(kept.newSessionKeywords, ["新对话", "从头来"]);
});

test("page cannot change ports", () => {
  const next = mergeConfig(defaultConfig(), { wsPort: 1, webPort: 2 } as any);
  assert.equal(next.wsPort, 4399);
  assert.equal(next.webPort, 8787);
});

test("numbers clamp and bad values fall back", () => {
  const next = mergeConfig(defaultConfig(), {
    sessionIdleMinutes: "abc",
    speaker: { listenTimeoutSec: 999, ttsDelayAfterAbortMs: -5, abortOn: "whenever" },
    codex: { timeoutSec: "90" },
  });
  assert.equal(next.sessionIdleMinutes, 30);
  assert.equal(next.speaker.listenTimeoutSec, 60);
  assert.equal(next.speaker.ttsDelayAfterAbortMs, 0);
  assert.equal(next.speaker.abortOn, "final");
  assert.equal(next.codex.timeoutSec, 90);
});

test("phrases may be cleared, workspace may not", () => {
  const next = mergeConfig(defaultConfig(), { phrases: { ack: "好嘞", notHeard: "" }, codex: { workspace: " " } });
  assert.equal(next.phrases.ack, "好嘞");
  assert.equal(next.phrases.notHeard, "");
  assert.equal(next.codex.workspace, defaultConfig().codex.workspace);
});

test("legacy migpt config loads with secrets dropped, ports from file then env", () => {
  const dir = mkdtempSync(join(tmpdir(), "butler-cfg-"));
  const path = join(dir, "butler.json");
  writeFileSync(
    path,
    JSON.stringify({ webPort: 8788, keywords: ["管家"], speaker: { did: "x", passToken: "secret" } }),
  );
  const prev = { cfg: process.env.BUTLER_CONFIG, ws: process.env.BUTLER_WS_PORT };
  process.env.BUTLER_CONFIG = path;
  process.env.BUTLER_WS_PORT = "5000";
  try {
    const cfg = loadConfig();
    assert.equal(cfg.webPort, 8788);
    assert.equal(cfg.wsPort, 5000);
    assert.equal(JSON.stringify(cfg).includes("secret"), false);
    assert.deepEqual(cfg.newSessionKeywords, defaultConfig().newSessionKeywords);
  } finally {
    restore("BUTLER_CONFIG", prev.cfg);
    restore("BUTLER_WS_PORT", prev.ws);
  }
});

test("saving never writes env-provided ports into the file", () => {
  const dir = mkdtempSync(join(tmpdir(), "butler-cfg-"));
  const path = join(dir, "butler.json");
  writeFileSync(path, JSON.stringify({ webPort: 8788 }));
  const prev = { cfg: process.env.BUTLER_CONFIG, ws: process.env.BUTLER_WS_PORT };
  process.env.BUTLER_CONFIG = path;
  process.env.BUTLER_WS_PORT = "5000";
  try {
    const cfg = mergeConfig(loadConfig(), { phrases: { ack: "好嘞" } });
    assert.equal(cfg.wsPort, 5000);
    saveConfig(cfg);
    const raw = JSON.parse(readFileSync(path, "utf8"));
    assert.equal(raw.webPort, 8788);
    assert.equal("wsPort" in raw, false);
    assert.equal(raw.phrases.ack, "好嘞");
  } finally {
    restore("BUTLER_CONFIG", prev.cfg);
    restore("BUTLER_WS_PORT", prev.ws);
  }
});

function restore(key: string, value: string | undefined) {
  if (value === undefined) delete process.env[key];
  else process.env[key] = value;
}
