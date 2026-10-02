//! NAS 下发的 `butler_config`。音箱本地存一份，NAS 不在时用上一次的。

use serde::{Deserialize, Serialize};
use std::path::Path;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AbortOn {
    /// 拿到最终识别（is_final）再打断：稳
    Final,
    /// 拿到 is_stop 就打断：快一拍
    Stop,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Phrases {
    pub wake: String,
    pub busy: String,
    pub error: String,
    pub timeout: String,
    pub not_heard: String,
}

impl Default for Phrases {
    fn default() -> Self {
        Self {
            wake: "在呢".into(),
            busy: "上一条还在办，稍等一下。".into(),
            error: "这一次没办成，稍后再试。".into(),
            timeout: "办得有点久，先不等了。".into(),
            not_heard: String::new(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    pub phrases: Phrases,
    pub listen_timeout_sec: u64,
    pub tts_delay_after_abort_ms: u64,
    pub abort_on: AbortOn,
    pub reply_timeout_sec: u64,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            phrases: Phrases::default(),
            listen_timeout_sec: 15,
            tts_delay_after_abort_ms: 2000,
            abort_on: AbortOn::Final,
            reply_timeout_sec: 195,
        }
    }
}

impl Settings {
    pub fn load(path: &Path) -> Self {
        std::fs::read_to_string(path).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default()
    }

    pub fn save(&self, path: &Path) -> std::io::Result<()> {
        let tmp = path.with_extension("tmp");
        std::fs::write(&tmp, serde_json::to_vec_pretty(self).expect("settings serialize"))?;
        std::fs::rename(tmp, path)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_server_payload_and_fills_gaps() {
        let s: Settings = serde_json::from_str(
            r#"{"phrases":{"wake":"来了","not_heard":"没听清"},"listen_timeout_sec":10,"abort_on":"stop","extra":1}"#,
        )
        .unwrap();
        assert_eq!(s.phrases.wake, "来了");
        assert_eq!(s.phrases.not_heard, "没听清");
        assert_eq!(s.phrases.busy, Phrases::default().busy);
        assert_eq!(s.listen_timeout_sec, 10);
        assert_eq!(s.abort_on, AbortOn::Stop);
        assert_eq!(s.reply_timeout_sec, 195);
    }

    #[test]
    fn save_and_load_round_trip() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("butler-config.json");
        assert_eq!(Settings::load(&p), Settings::default());
        let mut s = Settings::default();
        s.phrases.wake = "我在".into();
        s.save(&p).unwrap();
        assert_eq!(Settings::load(&p), s);
    }
}
