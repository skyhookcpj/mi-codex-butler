import assert from "node:assert/strict";
import { test } from "node:test";
import { matchNewSession, toSpeech } from "./text.ts";

const KW = ["新对话", "新会话", "重新开始"];

test("exact keyword resets", () => {
  assert.deepEqual(matchNewSession("新对话", KW), { kind: "reset" });
  assert.deepEqual(matchNewSession("新对话。", KW), { kind: "reset" });
  assert.deepEqual(matchNewSession(" 重新开始！", KW), { kind: "reset" });
});

test("keyword with a little filler still just resets", () => {
  assert.deepEqual(matchNewSession("开个新对话吧", KW), { kind: "reset" });
  assert.deepEqual(matchNewSession("新对话吧", KW), { kind: "reset" });
});

test("keyword prefix then a real request", () => {
  assert.deepEqual(matchNewSession("新对话，打开客厅主灯", KW), { kind: "reset_then", rest: "打开客厅主灯" });
  assert.deepEqual(matchNewSession("重新开始 客厅窗帘开了没", KW), { kind: "reset_then", rest: "客厅窗帘开了没" });
});

test("keyword buried in a normal sentence does not reset", () => {
  assert.deepEqual(matchNewSession("帮我把新对话功能的说明念一下", KW), { kind: "none" });
  assert.deepEqual(matchNewSession("客厅现在怎样", KW), { kind: "none" });
  assert.deepEqual(matchNewSession("", KW), { kind: "none" });
});

test("speech strips markdown and joins lines", () => {
  assert.equal(toSpeech("**客厅主灯**已经打开了"), "客厅主灯已经打开了。");
  assert.equal(toSpeech("- 主灯：开\n- 筒灯：关"), "主灯：开。筒灯：关。");
  assert.equal(toSpeech("已执行 `mihome device control 1 2.1 true`"), "已执行 mihome device control 1 2.1 true。");
  assert.equal(toSpeech("详见 https://example.com 好了"), "详见 好了。");
});

test("speech keeps existing punctuation and caps length at a sentence end", () => {
  assert.equal(toSpeech("好的！"), "好的！");
  const long = "这是一句话。".repeat(80);
  const out = toSpeech(long);
  assert.ok(out.length <= 300);
  assert.ok(out.endsWith("。"));
});
