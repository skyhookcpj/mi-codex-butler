//! 解析音箱上的两份日志行：
//! - `/tmp/open-xiaoai/kws.log`：`{timestamp}@{keyword}`，KWS 每次命中追加一行
//! - `/tmp/mico_aivs_lab/instruction.log`：原厂接待写的 JSON，一行一条指令

use serde_json::Value;

#[derive(Debug, Clone, PartialEq)]
pub enum KwsLine {
    Started,
    Keyword { ts: u64, keyword: String },
}

/// 坏行返回 None（上游这里 unwrap，一行坏数据就能把 client 搞崩）。
pub fn parse_kws(line: &str) -> Option<KwsLine> {
    let (ts, keyword) = line.trim().split_once('@')?;
    let ts: u64 = ts.trim().parse().ok()?;
    let keyword = keyword.trim();
    if keyword.is_empty() {
        return None;
    }
    if keyword == "__STARTED__" {
        return Some(KwsLine::Started);
    }
    Some(KwsLine::Keyword { ts, keyword: keyword.to_string() })
}

#[derive(Debug, Clone, PartialEq)]
pub struct Recognize {
    pub dialog_id: String,
    pub text: String,
    pub is_final: bool,
    /// 原厂判断用户说完了（比 is_final 早一拍，文字通常已经定了）
    pub is_stop: bool,
}

/// 只关心 SpeechRecognizer.RecognizeResult，其它指令返回 None。
pub fn parse_instruction(line: &str) -> Option<Recognize> {
    let v: Value = serde_json::from_str(line.trim()).ok()?;
    let header = v.get("header")?;
    if header.get("namespace")?.as_str()? != "SpeechRecognizer" || header.get("name")?.as_str()? != "RecognizeResult" {
        return None;
    }
    let payload = v.get("payload")?;
    let first = payload.get("results").and_then(|r| r.get(0));
    let text = first.and_then(|r| r.get("text")).and_then(Value::as_str).unwrap_or("").trim().to_string();
    Some(Recognize {
        dialog_id: header.get("dialog_id").and_then(Value::as_str).unwrap_or("").to_string(),
        text,
        is_final: payload.get("is_final").and_then(Value::as_bool).unwrap_or(false),
        is_stop: first.and_then(|r| r.get("is_stop")).and_then(Value::as_bool).unwrap_or(false),
    })
}

/// Dialog.Finish：接待把这一轮对话收尾完了，返回 dialog_id。
pub fn parse_dialog_finish(line: &str) -> Option<String> {
    let v: Value = serde_json::from_str(line.trim()).ok()?;
    let header = v.get("header")?;
    if header.get("namespace")?.as_str()? != "Dialog" || header.get("name")?.as_str()? != "Finish" {
        return None;
    }
    Some(header.get("dialog_id")?.as_str()?.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kws_lines() {
        assert_eq!(
            parse_kws("1727771234@你好管家"),
            Some(KwsLine::Keyword { ts: 1727771234, keyword: "你好管家".into() })
        );
        assert_eq!(parse_kws("1@__STARTED__\n"), Some(KwsLine::Started));
        assert_eq!(parse_kws("garbage"), None);
        assert_eq!(parse_kws("abc@你好管家"), None);
        assert_eq!(parse_kws("12@"), None);
    }

    // 2026-10-01 从客厅 OH2P 抓的真实行
    const PARTIAL: &str = r#"{"header":{"dialog_id":"685931ec9d1df74dbbbae30d49e0567f","id":"c1618da8e0e94f03ad99a2ea4a587387","name":"RecognizeResult","namespace":"SpeechRecognizer"},"payload":{"is_final":false,"is_vad_begin":true,"results":[{"asr_binary_offset":2120,"begin_offset":1500,"confidence":0.0,"end_offset":2120,"is_key_word_free_request":false,"text":"现在几点了"}]}}"#;
    const STOP: &str = r#"{"header":{"dialog_id":"685931ec9d1df74dbbbae30d49e0567f","id":"d5b384de1f1e444881e8f54b908d1ada","name":"RecognizeResult","namespace":"SpeechRecognizer"},"payload":{"is_final":false,"is_vad_begin":true,"results":[{"asr_binary_offset":2080,"begin_offset":1260,"confidence":0.0,"end_offset":2080,"is_key_word_free_request":false,"is_nlp_request":true,"is_stop":true,"text":"现在几点了"}]}}"#;
    const FINAL: &str = r#"{"header":{"dialog_id":"685931ec9d1df74dbbbae30d49e0567f","id":"ed5e9f20cf39485cb70a54849e6fc342","name":"RecognizeResult","namespace":"SpeechRecognizer"},"payload":{"is_final":true,"is_vad_begin":true,"results":[{"asr_binary_offset":2080,"begin_offset":1260,"confidence":0.0,"end_offset":2080,"is_key_word_free_request":false,"is_nlp_request":false,"origin_text":"现在几点了","text":"现在几点了"}]}}"#;
    const SPEAK: &str = r#"{"header":{"dialog_id":"685931ec9d1df74dbbbae30d49e0567f","id":"1b0c30e5b28b4072aaf9443d63d45da7","name":"Speak","namespace":"SpeechSynthesizer"},"payload":{"emotion":{"category":"DEFAULT","level":"LOW"},"text":"现在是下午4点34分"}}"#;

    #[test]
    fn real_instruction_lines() {
        let p = parse_instruction(PARTIAL).unwrap();
        assert!(!p.is_final && !p.is_stop);
        let s = parse_instruction(STOP).unwrap();
        assert!(!s.is_final && s.is_stop);
        assert_eq!(s.text, "现在几点了");
        let f = parse_instruction(FINAL).unwrap();
        assert!(f.is_final);
        assert_eq!(f.dialog_id, "685931ec9d1df74dbbbae30d49e0567f");
        assert_eq!(parse_instruction(SPEAK), None);
        assert_eq!(parse_instruction("{not json"), None);
    }

    #[test]
    fn final_without_results_is_empty_text() {
        let l = r#"{"header":{"dialog_id":"d","id":"i","name":"RecognizeResult","namespace":"SpeechRecognizer"},"payload":{"is_final":true,"is_vad_begin":false,"results":[]}}"#;
        let r = parse_instruction(l).unwrap();
        assert!(r.is_final);
        assert_eq!(r.text, "");
    }

    #[test]
    fn dialog_finish_lines() {
        // 格式同 2026-10-08 只识别模式下 OH2P 的真实行（编号是假的）
        let line = r#"{"header":{"dialog_id":"0123456789abcdef0123456789abcdef","id":"fedcba9876543210fedcba98765432100000000000000000","name":"Finish","namespace":"Dialog"},"payload":{}}"#;
        assert_eq!(parse_dialog_finish(line), Some("0123456789abcdef0123456789abcdef".into()));
        assert_eq!(parse_dialog_finish(FINAL), None);
        assert_eq!(parse_instruction(line), None);
        assert_eq!(parse_dialog_finish("{not json"), None);
    }
}
