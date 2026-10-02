//! 管家路径的状态机，纯逻辑：吃输入，吐动作，不碰 IO。时间是进程内毫秒。
//!
//! ```text
//! IDLE ──唤醒词──► 重启接待 + 念「在呢」+ 静默叫醒 ──► LISTENING
//! LISTENING ──识别出字──► 打断小爱 + 发给 NAS ──► WAIT_REPLY
//!           ──空识别──► （没听清）──► IDLE
//!           ──超时──► （没听清；一个字都没出过就整套重置）──► IDLE
//! WAIT_REPLY ──回复──► 念 ──► IDLE
//!            ──超时/发送失败──► 念超时语/出错语 ──► IDLE
//!            ──唤醒词──► 念忙碌语，继续等
//! ```

use crate::logs::Recognize;
use crate::protocol::ReplyData;
use crate::settings::{AbortOn, Settings};
use uuid::Uuid;

#[derive(Debug, Clone, PartialEq)]
pub enum Input {
    Kws(String),
    Recognize(Recognize),
    Reply(ReplyData),
    /// NAS 让念的随口一句（受理语、页面「让音箱念」）
    Say(String),
    /// utterance 没发出去（NAS 没连上）
    SendFailed(String),
    Config(Settings),
    Tick,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Action {
    /// 唤醒应答（优先用缓存好的音频）
    PlayWake(String),
    /// 轻量重置：空闲时重启接待。同一个接待进程只有第一次静默唤醒能正常上传音频，所以每次唤醒前都重启
    RefreshAivs,
    /// 静默叫醒原厂接待（执行器会等前面的重置做完）
    WakeUp,
    /// 打断 / 整套重置：重启接待 + pns + 补跑对话收尾
    Abort,
    /// 念一句；`not_before` 之前原厂 TTS 还没从打断里缓过来
    Speak {
        text: String,
        not_before: u64,
    },
    SendUtterance {
        id: String,
        text: String,
        dialog_id: String,
    },
}

#[derive(Debug, Clone, PartialEq)]
pub enum State {
    Idle,
    /// `heard`：这一轮出过任何识别结果
    Listening {
        deadline: u64,
        heard: bool,
    },
    WaitReply {
        id: String,
        deadline: u64,
    },
}

pub struct Machine {
    state: State,
    settings: Settings,
    last_abort: Option<u64>,
    /// 已经接管过的 dialog：打断后它的残余行不能再算成下一句
    handled_dialog: Option<String>,
}

impl Machine {
    pub fn new(settings: Settings) -> Self {
        Self { state: State::Idle, settings, last_abort: None, handled_dialog: None }
    }

    pub fn state(&self) -> &State {
        &self.state
    }

    pub fn step(&mut self, input: Input, now: u64) -> Vec<Action> {
        let mut out = Vec::new();
        match input {
            Input::Config(s) => self.settings = s,

            Input::Kws(_) => match &self.state {
                State::WaitReply { .. } => self.speak(&mut out, &self.settings.phrases.busy.clone(), now),
                State::Idle | State::Listening { .. } => {
                    if matches!(self.state, State::Listening { .. }) {
                        // 接待正在收音，轻量重启会把 mipns 卡住，只能整套重置
                        self.last_abort = Some(now);
                        out.push(Action::Abort);
                    } else {
                        out.push(Action::RefreshAivs);
                    }
                    out.push(Action::PlayWake(self.settings.phrases.wake.clone()));
                    out.push(Action::WakeUp);
                    self.state =
                        State::Listening { deadline: now + self.settings.listen_timeout_sec * 1000, heard: false };
                }
            },

            Input::Recognize(r) => {
                if !matches!(self.state, State::Listening { .. }) {
                    return out; // 小爱自己的对话，不管
                }
                if self.handled_dialog.as_deref() == Some(r.dialog_id.as_str()) {
                    return out;
                }
                if let State::Listening { heard, .. } = &mut self.state {
                    *heard = true;
                }
                let ready = match self.settings.abort_on {
                    AbortOn::Final => r.is_final,
                    AbortOn::Stop => r.is_final || r.is_stop,
                };
                if !ready {
                    return out;
                }
                if r.text.is_empty() {
                    if r.is_final {
                        self.state = State::Idle;
                        self.speak(&mut out, &self.settings.phrases.not_heard.clone(), now);
                    }
                    return out;
                }
                self.handled_dialog = Some(r.dialog_id.clone());
                self.last_abort = Some(now);
                out.push(Action::Abort);
                let id = Uuid::new_v4().to_string();
                out.push(Action::SendUtterance { id: id.clone(), text: r.text, dialog_id: r.dialog_id });
                self.state = State::WaitReply { id, deadline: now + self.settings.reply_timeout_sec * 1000 };
            }

            Input::Reply(r) => match &self.state {
                State::WaitReply { id, .. } if *id == r.reply_to => {
                    self.state = State::Idle;
                    self.speak(&mut out, &r.text, now);
                }
                // 迟到的回复：闲着就念，正在听写就别插嘴
                State::Idle => self.speak(&mut out, &r.text, now),
                _ => {}
            },

            Input::Say(text) => {
                if !matches!(self.state, State::Listening { .. }) {
                    self.speak(&mut out, &text, now);
                }
            }

            Input::SendFailed(failed) => {
                if matches!(&self.state, State::WaitReply { id, .. } if *id == failed) {
                    self.state = State::Idle;
                    self.speak(&mut out, &self.settings.phrases.error.clone(), now);
                }
            }

            Input::Tick => match &self.state {
                State::Listening { deadline, heard } if now >= *deadline => {
                    if !*heard {
                        // 叫醒了却一个字都没出：多半原厂又卡住了，整套重置，下一轮能自己恢复
                        self.last_abort = Some(now);
                        out.push(Action::Abort);
                    }
                    self.state = State::Idle;
                    self.speak(&mut out, &self.settings.phrases.not_heard.clone(), now);
                }
                State::WaitReply { deadline, .. } if now >= *deadline => {
                    self.state = State::Idle;
                    self.speak(&mut out, &self.settings.phrases.timeout.clone(), now);
                }
                _ => {}
            },
        }
        out
    }

    fn speak(&self, out: &mut Vec<Action>, text: &str, now: u64) {
        let text = text.trim();
        if text.is_empty() {
            return;
        }
        let ready_at = self.last_abort.map_or(0, |t| t + self.settings.tts_delay_after_abort_ms);
        out.push(Action::Speak { text: text.to_string(), not_before: ready_at.max(now) });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rec(dialog: &str, text: &str, is_final: bool, is_stop: bool) -> Input {
        Input::Recognize(Recognize { dialog_id: dialog.into(), text: text.into(), is_final, is_stop })
    }

    fn reply(to: &str, text: &str) -> Input {
        Input::Reply(ReplyData { reply_to: to.into(), kind: "reply".into(), text: text.into() })
    }

    fn sent_id(actions: &[Action]) -> String {
        actions
            .iter()
            .find_map(|a| match a {
                Action::SendUtterance { id, .. } => Some(id.clone()),
                _ => None,
            })
            .expect("no SendUtterance")
    }

    fn speak(text: &str, not_before: u64) -> Action {
        Action::Speak { text: text.into(), not_before }
    }

    #[test]
    fn happy_path() {
        let mut m = Machine::new(Settings::default());
        assert_eq!(
            m.step(Input::Kws("你好管家".into()), 0),
            vec![Action::RefreshAivs, Action::PlayWake("在呢".into()), Action::WakeUp]
        );
        assert!(m.step(rec("d1", "客厅现在", false, false), 1000).is_empty());
        assert!(m.step(rec("d1", "客厅现在怎样", false, true), 1500).is_empty(), "final mode waits for is_final");
        let acts = m.step(rec("d1", "客厅现在怎样", true, false), 1800);
        assert_eq!(acts[0], Action::Abort);
        assert!(
            matches!(&acts[1], Action::SendUtterance { text, dialog_id, .. } if text == "客厅现在怎样" && dialog_id == "d1")
        );
        let id = sent_id(&acts);
        assert!(matches!(m.state(), State::WaitReply { .. }));
        // Codex 很快就回了：念要等到打断后 2s
        assert_eq!(m.step(reply(&id, "客厅主灯开着。"), 2500), vec![speak("客厅主灯开着。", 3800)]);
        assert_eq!(m.state(), &State::Idle);
        // 慢回复不用再等
        assert_eq!(m.step(Input::Say("x".into()), 10_000), vec![speak("x", 10_000)]);
    }

    #[test]
    fn stop_mode_aborts_on_is_stop_and_ignores_the_later_final() {
        let mut m = Machine::new(Settings { abort_on: AbortOn::Stop, ..Settings::default() });
        m.step(Input::Kws("k".into()), 0);
        let acts = m.step(rec("d1", "开灯", false, true), 1000);
        assert_eq!(acts[0], Action::Abort);
        let id = sent_id(&acts);
        assert!(m.step(rec("d1", "开灯", true, false), 1100).is_empty());
        m.step(reply(&id, "好"), 5000);
        // 回到待机后再唤醒，旧 dialog 的残余行不能被当成新的一句
        m.step(Input::Kws("k".into()), 6000);
        assert!(m.step(rec("d1", "开灯", true, false), 6100).is_empty());
        assert!(matches!(m.state(), State::Listening { .. }));
        assert_eq!(m.step(rec("d2", "关灯", true, false), 7000)[0], Action::Abort);
    }

    #[test]
    fn recognition_outside_butler_path_is_ignored() {
        let mut m = Machine::new(Settings::default());
        assert!(m.step(rec("d1", "打开客厅灯", true, false), 0).is_empty());
        assert_eq!(m.state(), &State::Idle);
    }

    #[test]
    fn listening_timeout_and_empty_final() {
        let mut m = Machine::new(Settings::default());
        m.step(Input::Kws("k".into()), 0);
        assert!(m.step(Input::Tick, 14_999).is_empty());
        assert_eq!(m.step(Input::Tick, 15_000), vec![Action::Abort], "一个字都没出：整套重置，not_heard 默认为空");
        assert_eq!(m.state(), &State::Idle);
        // 出过字（哪怕最后是空）再超时，就不重置
        m.step(Input::Kws("k".into()), 20_000);
        m.step(rec("d9", "客", false, false), 21_000);
        assert!(m.step(Input::Tick, 35_000).is_empty());

        let mut s = Settings::default();
        s.phrases.not_heard = "我没听清".into();
        let mut m = Machine::new(s);
        m.step(Input::Kws("k".into()), 0);
        assert_eq!(m.step(rec("d1", "", true, false), 3000), vec![speak("我没听清", 3000)]);
        assert_eq!(m.state(), &State::Idle);
    }

    #[test]
    fn wake_while_waiting_says_busy_and_keeps_waiting() {
        let mut m = Machine::new(Settings::default());
        m.step(Input::Kws("k".into()), 0);
        let id = sent_id(&m.step(rec("d1", "跑个长任务", true, false), 1000));
        assert_eq!(m.step(Input::Kws("k".into()), 20_000), vec![speak("上一条还在办，稍等一下。", 20_000)]);
        assert!(matches!(m.state(), State::WaitReply { .. }));
        assert_eq!(m.step(reply(&id, "办完了"), 60_000), vec![speak("办完了", 60_000)]);
    }

    #[test]
    fn rewake_while_listening_restarts_listening() {
        let mut m = Machine::new(Settings::default());
        m.step(Input::Kws("k".into()), 0);
        assert_eq!(
            m.step(Input::Kws("k".into()), 10_000),
            vec![Action::Abort, Action::PlayWake("在呢".into()), Action::WakeUp],
            "接待正在收音时只能整套重置"
        );
        assert!(m.step(Input::Tick, 20_000).is_empty());
        assert!(matches!(m.state(), State::Listening { .. }));
    }

    #[test]
    fn reply_timeout_and_send_failure() {
        let mut m = Machine::new(Settings::default());
        m.step(Input::Kws("k".into()), 0);
        m.step(rec("d1", "a", true, false), 1000);
        assert_eq!(m.step(Input::Tick, 196_000), vec![speak("办得有点久，先不等了。", 196_000)]);
        assert_eq!(m.state(), &State::Idle);

        m.step(Input::Kws("k".into()), 200_000);
        let id = sent_id(&m.step(rec("d2", "b", true, false), 201_000));
        assert!(m.step(Input::SendFailed("other".into()), 201_001).is_empty());
        assert_eq!(m.step(Input::SendFailed(id), 201_002), vec![speak("这一次没办成，稍后再试。", 203_000)]);
    }

    #[test]
    fn late_or_foreign_replies() {
        let mut m = Machine::new(Settings::default());
        // 闲着时迟到的回复照念
        assert_eq!(m.step(reply("old", "迟到的结果"), 0), vec![speak("迟到的结果", 0)]);
        // 正在听写时不插嘴
        m.step(Input::Kws("k".into()), 1000);
        assert!(m.step(reply("old", "迟到的结果"), 1500).is_empty());
        assert!(m.step(Input::Say("好嘞".into()), 1600).is_empty());
        // 等回复时：受理语照念，别人的回复不收
        let id = sent_id(&m.step(rec("d1", "开灯", true, false), 2000));
        assert_eq!(m.step(Input::Say("好嘞".into()), 2100), vec![speak("好嘞", 4000)]);
        assert!(m.step(reply("old", "x"), 2200).is_empty());
        assert!(matches!(m.state(), State::WaitReply { .. }));
        // 空文本回复（页面点了停止）：不念，回待机
        assert!(m.step(reply(&id, ""), 3000).is_empty());
        assert_eq!(m.state(), &State::Idle);
    }

    #[test]
    fn config_update_applies_to_next_round() {
        let mut m = Machine::new(Settings::default());
        let mut s = Settings { listen_timeout_sec: 5, ..Settings::default() };
        s.phrases.not_heard = "嗯？".into();
        m.step(Input::Config(s), 0);
        m.step(Input::Kws("k".into()), 0);
        m.step(rec("d1", "", false, false), 1000);
        assert_eq!(m.step(Input::Tick, 5000), vec![speak("嗯？", 5000)]);
    }
}
