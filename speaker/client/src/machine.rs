//! 管家路径的状态机，纯逻辑：吃输入，吐动作，不碰 IO。时间是进程内毫秒。
//!
//! ```text
//! IDLE ──唤醒词──► 以「只识别」模式重启接待 + 念「在呢」+ 静默叫醒 ──► LISTENING
//! LISTENING ──识别出字──► 发给 NAS ──► WAIT_REPLY
//!           ──空识别──► （没听清）──► IDLE
//!           ──超时──► （没听清）整套重置 ──► IDLE
//! WAIT_REPLY ──回复──► 念 ──► IDLE
//!            ──超时/发送失败──► 念超时语/出错语 ──► IDLE
//!            ──唤醒词──► 念忙碌语，继续等
//! 这一轮出了最终识别后，等接待自己发出 Dialog.Finish（最多 2 秒），再以正常模式重启接待。
//! ```
//!
//! 「只识别」：接待启动时看到 `/data/pns.lab` 就进实验室测试模式，云端只做识别，不理解、不执行、不播报
//! （2026-10-08 实测）。所以管家这一轮不用再中途打断小爱，「小爱同学」平时照常。

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
    /// 接待写出 Dialog.Finish：这一轮对话（按 dialog_id）收尾完了
    DialogFinish(String),
    Config(Settings),
    Tick,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Action {
    /// 唤醒应答（优先用缓存好的音频）
    PlayWake(String),
    /// 轻量重置：重启接待。`asr_only` 为 true 时以「只识别」模式启动（管家这一轮），false 回到正常模式。
    /// 同一个接待进程只有第一次静默唤醒能正常上传音频，所以每次唤醒前都要重启
    RefreshAivs {
        asr_only: bool,
    },
    /// 静默叫醒原厂接待（执行器会等前面的重置做完）
    WakeUp,
    /// 整套重置：重启接待（回到正常模式）+ pns + 补跑对话收尾。只在收音中途要放弃这一轮时用
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

/// 出了最终识别后，最多等这么久的 Dialog.Finish，就不等了直接回正常模式
pub const ASR_ONLY_EXIT_WAIT_MS: u64 = 2000;

pub struct Machine {
    state: State,
    settings: Settings,
    last_abort: Option<u64>,
    /// 已经接管过的 dialog：它的残余行不能再算成下一句
    handled_dialog: Option<String>,
    /// 等这一轮（dialog_id）收尾后回正常模式；第二个值是最晚什么时候回
    asr_only_exit: Option<(String, u64)>,
}

impl Machine {
    pub fn new(settings: Settings) -> Self {
        Self { state: State::Idle, settings, last_abort: None, handled_dialog: None, asr_only_exit: None }
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
                        // 接待正在收音，轻量重启会把 mipns 卡住，先整套重置
                        self.last_abort = Some(now);
                        out.push(Action::Abort);
                    }
                    self.asr_only_exit = None;
                    out.push(Action::RefreshAivs { asr_only: true });
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
                        self.asr_only_exit = Some((r.dialog_id.clone(), now + ASR_ONLY_EXIT_WAIT_MS));
                        self.speak(&mut out, &self.settings.phrases.not_heard.clone(), now);
                    }
                    return out;
                }
                // 只识别模式下小爱既不执行也不开口，不用打断；等它自己收尾再回正常模式
                self.handled_dialog = Some(r.dialog_id.clone());
                self.asr_only_exit = Some((r.dialog_id.clone(), now + ASR_ONLY_EXIT_WAIT_MS));
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

            Input::DialogFinish(dialog) => {
                if self.asr_only_exit.as_ref().is_some_and(|(d, _)| *d == dialog) {
                    self.asr_only_exit = None;
                    out.push(Action::RefreshAivs { asr_only: false });
                }
            }

            Input::Tick => {
                // 等不到 Dialog.Finish 也别一直停在只识别模式，否则「小爱同学」会只听不做
                if self.asr_only_exit.as_ref().is_some_and(|(_, at)| now >= *at) {
                    self.asr_only_exit = None;
                    out.push(Action::RefreshAivs { asr_only: false });
                }
                self.tick(&mut out, now);
            }
        }
        out
    }

    fn tick(&mut self, out: &mut Vec<Action>, now: u64) {
        match &self.state {
            State::Listening { deadline, .. } if now >= *deadline => {
                // 超时还没有最终识别：接待可能卡住，也可能还在收音。整套重置，顺带回到正常模式
                self.last_abort = Some(now);
                self.asr_only_exit = None;
                out.push(Action::Abort);
                self.state = State::Idle;
                self.speak(out, &self.settings.phrases.not_heard.clone(), now);
            }
            State::WaitReply { deadline, .. } if now >= *deadline => {
                self.state = State::Idle;
                self.speak(out, &self.settings.phrases.timeout.clone(), now);
            }
            _ => {}
        }
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
            vec![Action::RefreshAivs { asr_only: true }, Action::PlayWake("在呢".into()), Action::WakeUp]
        );
        assert!(m.step(rec("d1", "客厅现在", false, false), 1000).is_empty());
        assert!(m.step(rec("d1", "客厅现在怎样", false, true), 1500).is_empty(), "final mode waits for is_final");
        let acts = m.step(rec("d1", "客厅现在怎样", true, false), 1800);
        assert_eq!(acts.len(), 1, "只识别模式下不打断小爱：{acts:?}");
        assert!(
            matches!(&acts[0], Action::SendUtterance { text, dialog_id, .. } if text == "客厅现在怎样" && dialog_id == "d1")
        );
        let id = sent_id(&acts);
        assert!(matches!(m.state(), State::WaitReply { .. }));
        // 小爱自己收尾后回正常模式
        assert_eq!(m.step(Input::DialogFinish("d1".into()), 1900), vec![Action::RefreshAivs { asr_only: false }]);
        // 回复不用再等打断后的 2s（执行器会等重启做完）
        assert_eq!(m.step(reply(&id, "客厅主灯开着。"), 2500), vec![speak("客厅主灯开着。", 2500)]);
        assert_eq!(m.state(), &State::Idle);
        assert!(m.step(Input::Tick, 10_000).is_empty(), "已经回正常模式，不会再重启");
    }

    #[test]
    fn stop_mode_aborts_on_is_stop_and_ignores_the_later_final() {
        let mut m = Machine::new(Settings { abort_on: AbortOn::Stop, ..Settings::default() });
        m.step(Input::Kws("k".into()), 0);
        let acts = m.step(rec("d1", "开灯", false, true), 1000);
        let id = sent_id(&acts);
        assert!(m.step(rec("d1", "开灯", true, false), 1100).is_empty());
        m.step(reply(&id, "好"), 5000);
        // 回到待机后再唤醒，旧 dialog 的残余行不能被当成新的一句
        m.step(Input::Kws("k".into()), 6000);
        assert!(m.step(rec("d1", "开灯", true, false), 6100).is_empty());
        assert!(matches!(m.state(), State::Listening { .. }));
        assert!(matches!(&m.step(rec("d2", "关灯", true, false), 7000)[0], Action::SendUtterance { .. }));
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
        assert_eq!(m.step(Input::Tick, 15_000), vec![Action::Abort], "超时：整套重置回正常模式，not_heard 默认为空");
        assert_eq!(m.state(), &State::Idle);
        // 出过字但一直没有最终识别：也整套重置，不能停在只识别模式
        m.step(Input::Kws("k".into()), 20_000);
        m.step(rec("d9", "客", false, false), 21_000);
        assert_eq!(m.step(Input::Tick, 35_000), vec![Action::Abort]);

        let mut s = Settings::default();
        s.phrases.not_heard = "我没听清".into();
        let mut m = Machine::new(s);
        m.step(Input::Kws("k".into()), 0);
        assert_eq!(m.step(rec("d1", "", true, false), 3000), vec![speak("我没听清", 3000)]);
        assert_eq!(m.state(), &State::Idle);
        assert_eq!(m.step(Input::DialogFinish("d1".into()), 3100), vec![Action::RefreshAivs { asr_only: false }]);
    }

    #[test]
    fn asr_only_ends_even_without_dialog_finish() {
        let mut m = Machine::new(Settings::default());
        m.step(Input::Kws("k".into()), 0);
        m.step(rec("d1", "开灯", true, false), 1000);
        // 别的对话收尾不算
        assert!(m.step(Input::DialogFinish("other".into()), 1100).is_empty());
        assert!(m.step(Input::Tick, 1000 + ASR_ONLY_EXIT_WAIT_MS - 1).is_empty());
        assert_eq!(m.step(Input::Tick, 1000 + ASR_ONLY_EXIT_WAIT_MS), vec![Action::RefreshAivs { asr_only: false }]);
        // 迟到的 Dialog.Finish 不再重启第二次
        assert!(m.step(Input::DialogFinish("d1".into()), 5000).is_empty());
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
            vec![
                Action::Abort,
                Action::RefreshAivs { asr_only: true },
                Action::PlayWake("在呢".into()),
                Action::WakeUp
            ],
            "接待正在收音时先整套重置，再以只识别模式重启"
        );
        assert!(m.step(Input::Tick, 20_000).is_empty());
        assert!(matches!(m.state(), State::Listening { .. }));
    }

    #[test]
    fn reply_timeout_and_send_failure() {
        let mut m = Machine::new(Settings::default());
        m.step(Input::Kws("k".into()), 0);
        m.step(rec("d1", "a", true, false), 1000);
        // 一直没等到 Dialog.Finish：第一次 tick 就先回正常模式
        assert_eq!(
            m.step(Input::Tick, 196_000),
            vec![Action::RefreshAivs { asr_only: false }, speak("办得有点久，先不等了。", 196_000)]
        );
        assert_eq!(m.state(), &State::Idle);

        m.step(Input::Kws("k".into()), 200_000);
        let id = sent_id(&m.step(rec("d2", "b", true, false), 201_000));
        assert!(m.step(Input::SendFailed("other".into()), 201_001).is_empty());
        assert_eq!(m.step(Input::SendFailed(id), 201_002), vec![speak("这一次没办成，稍后再试。", 201_002)]);
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
        assert_eq!(m.step(Input::Say("好嘞".into()), 2100), vec![speak("好嘞", 2100)]);
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
        // 超时整套重置，提示语等重置缓过来再念
        assert_eq!(m.step(Input::Tick, 5000), vec![Action::Abort, speak("嗯？", 7000)]);
    }
}
