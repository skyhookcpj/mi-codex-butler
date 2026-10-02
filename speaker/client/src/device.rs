//! 音箱上的原厂命令：念字、叫醒、打断。念字和叫醒串行执行（只有一个喇叭）；
//! 重启类（打断、轻量重置）立刻开始，叫醒会等它们做完。
//! `BUTLER_DRY_RUN=1` 时只打印不执行，用来在 Mac 上联调。

use crate::clock::Clock;
use crate::log;
use serde_json::{json, Value};
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::Arc;
use tokio::process::Command;
use tokio::sync::{Mutex, Notify};

const TTS_PLAY: &str = "/usr/sbin/tts_play.sh";
const WAKE_UP: &str = r#"ubus call pnshelper event_notify '{"src":1,"event":0}'"#;
/// 打断 = 重启接待 → 重启 pns（拾音）→ 替原厂跑一遍对话结束的收尾 `wakeup.sh ready`。
/// - 只重启接待的话，接待来不及告诉 mipns「对话结束」，mipns 卡在收音态，之后的静默唤醒全被丢掉
///   （「小爱同学」走 pre-wakeup 路径不受影响，所以 Open-XiaoAI 的 migpt 示例不会踩到）。
/// - 识别出字时原厂已经调了 `wakeup.sh think`（顶灯跑马灯），正常结束才会调 `ready` 关灯，
///   打断后没人调，灯会一直转；`ready` 顺带让 mediaplayer 退出唤醒态。
///
/// 2026-10-01 实测：从打断起约 1.5s 静默唤醒恢复、1.7s 原厂 TTS 恢复，和打断后本来就要等的 2s 重叠。
const ABORT: &str = "/etc/init.d/mico_aivs_lab restart >/dev/null 2>&1; \
                     /etc/init.d/pns restart >/dev/null 2>&1; \
                     /bin/wakeup.sh ready >/dev/null 2>&1";
const ABORT_SETTLE_MS: u64 = 1300;
/// 同一个接待进程只有第一次静默唤醒能正常上传音频（之后不再发首包，云端识别为空），
/// 所以每次唤醒前空闲重启一次。2026-10-01 实测重启后 0.3s 唤醒即可正常收音。
const REFRESH_AIVS: &str = "/etc/init.d/mico_aivs_lab restart >/dev/null 2>&1";
const REFRESH_SETTLE_MS: u64 = 300;
const MAX_SPEECH_CHARS: usize = 400;

#[derive(Clone)]
pub struct Device {
    dry_run: bool,
    clock: Clock,
    cache_dir: PathBuf,
    /// 缓存好的唤醒应答：(文字, mp3 路径)
    wake: Arc<Mutex<Option<(String, PathBuf)>>>,
    maint: Arc<Maintenance>,
}

/// 重启类操作的协调：进行中的个数 + 最早可以叫醒的时刻（进程内毫秒）。
#[derive(Default)]
struct Maintenance {
    state: std::sync::Mutex<(usize, u64)>,
    changed: Notify,
    /// 重启命令一个接一个跑，避免交错
    serial: Mutex<()>,
}

impl Device {
    pub fn new(dry_run: bool, clock: Clock, cache_dir: PathBuf) -> Self {
        Self { dry_run, clock, cache_dir, wake: Arc::new(Mutex::new(None)), maint: Default::default() }
    }

    /// 打断 / 整套重置。立刻开始，不排在念字后面：越快打断，小爱越来不及开口。
    pub fn abort(&self) {
        self.maintain(ABORT, ABORT_SETTLE_MS);
    }

    /// 轻量重置：空闲时重启接待，和念「在呢」同时进行。
    pub fn refresh_aivs(&self) {
        self.maintain(REFRESH_AIVS, REFRESH_SETTLE_MS);
    }

    /// 等所有重启做完、缓够，再静默叫醒。
    pub async fn wake_up(&self) {
        loop {
            let changed = self.maint.changed.notified();
            tokio::pin!(changed);
            changed.as_mut().enable();
            let (inflight, ready_at) = *self.maint.state.lock().expect("maint poisoned");
            if inflight > 0 {
                changed.await;
            } else if self.clock.now() < ready_at {
                self.sleep_until(ready_at).await;
            } else {
                break;
            }
        }
        self.sh(WAKE_UP).await;
    }

    /// 登记是同步的：调用返回时，之后的 wake_up 一定会等它。
    fn maintain(&self, script: &'static str, settle_ms: u64) {
        self.maint.state.lock().expect("maint poisoned").0 += 1;
        let me = self.clone();
        tokio::spawn(async move {
            let serial = me.maint.serial.lock().await;
            me.sh(script).await;
            drop(serial);
            let mut st = me.maint.state.lock().expect("maint poisoned");
            st.0 -= 1;
            st.1 = st.1.max(me.clock.now() + settle_ms);
            drop(st);
            me.maint.changed.notify_waiters();
        });
    }

    async fn sleep_until(&self, at: u64) {
        let wait = at.saturating_sub(self.clock.now());
        if wait > 0 {
            tokio::time::sleep(std::time::Duration::from_millis(wait)).await;
        }
    }

    /// 等到 `not_before`（进程内毫秒）再念。
    pub async fn speak(&self, text: &str, not_before: u64) {
        self.sleep_until(not_before).await;
        let text = tts_safe(text);
        if text.is_empty() {
            return;
        }
        log!("念：{text}");
        if self.dry_run {
            return;
        }
        // 文字作为 argv 传，不经过 shell；tts_play.sh 里再拼 JSON，所以 tts_safe 去掉了引号和反斜杠
        let res = Command::new(TTS_PLAY).arg(&text).stdout(Stdio::null()).stderr(Stdio::null()).status().await;
        if let Err(e) = res {
            log!("念失败：{e}");
        }
    }

    /// 唤醒应答：有缓存直接播 mp3（省掉约 1s 的云端合成），否则现合成，顺手补缓存。
    pub async fn play_wake(&self, text: &str) {
        let want = tts_safe(text);
        let cached = self.wake.lock().await.as_ref().filter(|(t, p)| *t == want && p.exists()).map(|(_, p)| p.clone());
        match cached {
            Some(path) => {
                log!("念（缓存）：{text}");
                if !self.dry_run {
                    let _ = Command::new("miplayer")
                        .arg("-f")
                        .arg(&path)
                        .stdout(Stdio::null())
                        .stderr(Stdio::null())
                        .status()
                        .await;
                }
            }
            None => {
                self.speak(text, 0).await;
                let me = self.clone();
                let text = text.to_string();
                tokio::spawn(async move { me.prepare_wake(&text).await });
            }
        }
    }

    /// 预先合成唤醒应答。只合成不播放。整个过程持锁：启动和收到配置会同时触发，只合成一次。
    pub async fn prepare_wake(&self, text: &str) {
        let text = tts_safe(text);
        if text.is_empty() || self.dry_run {
            return;
        }
        let mut wake = self.wake.lock().await;
        if wake.as_ref().is_some_and(|(t, p)| *t == text && p.exists()) {
            return;
        }
        let arg = json!({ "text": text, "save": 1 }).to_string();
        let out = Command::new("ubus").args(["call", "mibrain", "text_to_speech", &arg]).output().await;
        let path = out.ok().and_then(|o| tts_path(&String::from_utf8_lossy(&o.stdout)));
        let Some(src) = path else {
            log!("唤醒应答预合成失败，先用现合成");
            return;
        };
        let _ = std::fs::create_dir_all(&self.cache_dir);
        let dst = self.cache_dir.join("wake.mp3");
        let ok = std::fs::copy(&src, &dst).is_ok();
        let _ = std::fs::remove_file(&src);
        if ok {
            log!("唤醒应答已缓存：{text}");
            *wake = Some((text, dst));
        }
    }

    async fn sh(&self, script: &str) {
        log!("$ {script}");
        if self.dry_run {
            return;
        }
        let res =
            Command::new("/bin/sh").arg("-c").arg(script).stdout(Stdio::null()).stderr(Stdio::null()).status().await;
        if let Err(e) = res {
            log!("执行失败：{e}");
        }
    }
}

/// tts_play.sh 把文字原样拼进 `{"text":"..."}`：去掉会破坏 JSON 的字符，换行变停顿，太长截断。
pub fn tts_safe(text: &str) -> String {
    let mut out = String::new();
    for ch in text.trim().chars() {
        match ch {
            '"' => out.push('”'),
            '\\' => {}
            '\n' | '\r' | '\t' => {
                if !out.ends_with(['，', '。', '！', '？']) && !out.is_empty() {
                    out.push('，');
                }
            }
            c if c.is_control() => {}
            c => out.push(c),
        }
        if out.chars().count() >= MAX_SPEECH_CHARS {
            break;
        }
    }
    out.trim_end_matches('，').trim().to_string()
}

/// `ubus call mibrain text_to_speech` 的输出：`{"code":0,"info":"{ \"path\": \"\\/tmp\\/tts\\/x.mp3\" }"}`
pub fn tts_path(stdout: &str) -> Option<PathBuf> {
    let outer: Value = serde_json::from_str(stdout.trim()).ok()?;
    if outer.get("code").and_then(Value::as_i64) != Some(0) {
        return None;
    }
    let info: Value = serde_json::from_str(outer.get("info")?.as_str()?).ok()?;
    Some(PathBuf::from(info.get("path")?.as_str()?))
}

/// run_shell RPC：给 NAS 调试用。
pub async fn run_shell(script: &str) -> Value {
    match Command::new("/bin/sh").arg("-c").arg(script).output().await {
        Ok(o) => json!({
            "stdout": String::from_utf8_lossy(&o.stdout),
            "stderr": String::from_utf8_lossy(&o.stderr),
            "exit_code": o.status.code().unwrap_or(-1),
        }),
        Err(e) => json!({ "stdout": "", "stderr": e.to_string(), "exit_code": -1 }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tts_safe_neutralizes_json_breakers() {
        assert_eq!(tts_safe(r#"他说"开灯"\n"#), "他说”开灯”n");
        assert_eq!(tts_safe("第一行\n第二行\n"), "第一行，第二行");
        assert_eq!(tts_safe("好了。\n明天见"), "好了。明天见");
        assert_eq!(tts_safe("it's $(reboot) `x`"), "it's $(reboot) `x`");
        assert_eq!(tts_safe("  "), "");
        assert_eq!(tts_safe(&"字".repeat(1000)).chars().count(), MAX_SPEECH_CHARS);
    }

    #[test]
    fn parses_real_tts_output() {
        // 2026-10-01 客厅 OH2P 上的真实输出
        let out = "{\n\t\"code\": 0,\n\t\"info\": \"{ \\\"path\\\": \\\"\\\\\\/tmp\\\\\\/tts\\\\\\/tts_6b32_1790862083_63156.mp3\\\" }\"\n}";
        assert_eq!(tts_path(out), Some(PathBuf::from("/tmp/tts/tts_6b32_1790862083_63156.mp3")));
        assert_eq!(tts_path(r#"{"code":-1}"#), None);
        assert_eq!(tts_path(""), None);
    }

    #[tokio::test]
    async fn wake_waits_for_pending_restarts() {
        let dir = tempfile::tempdir().unwrap();
        let clock = Clock::start();
        let d = Device::new(true, clock, dir.path().to_path_buf());
        // 没有待完成的重启：立刻叫醒
        d.wake_up().await;
        assert!(clock.now() < 100);

        d.refresh_aivs();
        let t0 = clock.now();
        d.wake_up().await;
        assert!(clock.now() - t0 >= REFRESH_SETTLE_MS);

        // 打断后紧跟轻量重置：取两者里更晚的就绪时刻
        d.abort();
        d.refresh_aivs();
        let t0 = clock.now();
        d.wake_up().await;
        assert!(clock.now() - t0 >= ABORT_SETTLE_MS);

        // 叫醒已经在等了，又来一次重启：叫醒得等新的那次也做完
        d.refresh_aivs();
        let t0 = clock.now();
        let (d2, waiter) = (d.clone(), d.clone());
        let wake = tokio::spawn(async move { waiter.wake_up().await });
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        d2.abort();
        wake.await.unwrap();
        assert!(clock.now() - t0 >= 100 + ABORT_SETTLE_MS);
    }

    #[tokio::test]
    async fn run_shell_reports_output() {
        let v = run_shell("echo hi; echo err >&2; exit 3").await;
        assert_eq!(v["stdout"], "hi\n");
        assert_eq!(v["stderr"], "err\n");
        assert_eq!(v["exit_code"], 3);
    }
}
