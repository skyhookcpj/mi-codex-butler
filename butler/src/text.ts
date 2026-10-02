const PUNCT = /[\s,，、。.!！?？;；:：~～…"'“”‘’「」『』()（）\[\]【】]+/g;

export function normalize(s: string): string {
  return (s || "").replace(PUNCT, "");
}

export type NewSessionHit =
  | { kind: "none" }
  /** 只是要新开一轮 */
  | { kind: "reset" }
  /** 「新对话，打开客厅灯」：新开一轮，再把后半句当第一句 */
  | { kind: "reset_then"; rest: string };

/** 一句话里带的语气词不算内容，「开个新对话吧」仍只是新开一轮。 */
const FILLER_SLACK = 3;

export function matchNewSession(text: string, keywords: string[]): NewSessionHit {
  const t = (text || "").trim();
  const n = normalize(t);
  if (!n) return { kind: "none" };
  const sorted = keywords
    .map((k) => normalize(k))
    .filter(Boolean)
    .sort((a, b) => b.length - a.length);
  for (const kw of sorted) {
    if (n === kw) return { kind: "reset" };
    if (n.startsWith(kw)) {
      const rest = stripLeadingKeyword(t, kw);
      if (normalize(rest).length > FILLER_SLACK) return { kind: "reset_then", rest };
      return { kind: "reset" };
    }
    if (n.includes(kw) && n.length - kw.length <= FILLER_SLACK) return { kind: "reset" };
  }
  return { kind: "none" };
}

/** 在原文里去掉开头的关键词（关键词中间可能夹着标点）和其后的标点。 */
function stripLeadingKeyword(text: string, kw: string): string {
  let matched = 0;
  let i = 0;
  while (i < text.length && matched < kw.length) {
    const ch = text[i];
    if (ch === kw[matched]) matched++;
    else if (normalize(ch) !== "") break;
    i++;
  }
  return text.slice(i).replace(/^[\s,，、。.!！?？;；:：]+/, "").trim();
}

const MAX_SPEECH = 300;

/** Codex 的最后一条消息要被念出来：去掉 markdown，换行改成停顿，太长截到句末。 */
export function toSpeech(s: string): string {
  let t = (s || "")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/https?:\/\/\S+/g, "")
    .replace(/^\s{0,3}#{1,6}\s*/gm, "")
    .replace(/^\s*(?:[-*+]|\d+[.)、])\s+/gm, "")
    .replace(/[*_~]{1,3}([^*_~\n]+)[*_~]{1,3}/g, "$1")
    .replace(/[*#>|]/g, "");
  t = t
    .split(/\n+/)
    .map((x) => x.trim())
    .filter(Boolean)
    .map((x) => (/[。！？!?，,；;：:]$/.test(x) ? x : `${x}。`))
    .join("")
    .replace(/\s{2,}/g, " ")
    .trim();
  if (t.length > MAX_SPEECH) {
    const cut = t.slice(0, MAX_SPEECH);
    const end = Math.max(cut.lastIndexOf("。"), cut.lastIndexOf("！"), cut.lastIndexOf("？"));
    t = end > MAX_SPEECH / 2 ? cut.slice(0, end + 1) : `${cut}…`;
  }
  return t;
}
