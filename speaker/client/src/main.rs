//! 管家音箱端。
//!
//! 用法：`butler-client [ws://NAS:4399]`，不给地址就读 `$BUTLER_HOME/server.txt`。
//!
//! 环境变量（都有音箱上的默认值，Mac 上联调时覆盖）：
//! - `BUTLER_HOME`            配置目录，默认 `/data/open-xiaoai`
//! - `BUTLER_KWS_LOG`         默认 `/tmp/open-xiaoai/kws.log`
//! - `BUTLER_INSTRUCTION_LOG` 默认 `/tmp/mico_aivs_lab/instruction.log`
//! - `BUTLER_CACHE`           唤醒应答缓存，默认 `/tmp/butler-client`
//! - `BUTLER_DRY_RUN=1`       只打印原厂命令，不执行

mod clock;
mod device;
mod logs;
mod machine;
mod protocol;
mod settings;
mod tail;

use clock::Clock;
use device::Device;
use futures_util::{SinkExt, StreamExt};
use logs::{parse_dialog_finish, parse_instruction, parse_kws, KwsLine};
use machine::{Action, Input, Machine, State};
use protocol::{AppMessage, Event, ReplyData, Request, Response, EV_CONFIG, EV_REPLY, EV_SAY, EV_UTTERANCE};
use serde_json::{json, Value};
use settings::Settings;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tail::Tail;
use tokio::sync::mpsc::{unbounded_channel, UnboundedSender};
use tokio_tungstenite::tungstenite::Message;

const VERSION: &str = env!("CARGO_PKG_VERSION");
const POLL: Duration = Duration::from_millis(50);
const TICK: Duration = Duration::from_millis(250);
/// NAS 每 15s ping 一次，这么久没动静就当断了
const SILENCE_LIMIT: Duration = Duration::from_secs(45);

type WsOut = Arc<Mutex<Option<UnboundedSender<Message>>>>;

fn env_path(key: &str, default: &str) -> PathBuf {
    std::env::var_os(key).map(PathBuf::from).unwrap_or_else(|| PathBuf::from(default))
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let home = env_path("BUTLER_HOME", "/data/open-xiaoai");
    let server = std::env::args()
        .nth(1)
        .or_else(|| std::fs::read_to_string(home.join("server.txt")).ok())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());
    let Some(server) = server else {
        eprintln!("用法：butler-client ws://NAS:4399（或写到 {}）", home.join("server.txt").display());
        std::process::exit(2);
    };
    let dry_run = std::env::var("BUTLER_DRY_RUN").is_ok_and(|v| v == "1");
    let clock = Clock::start();
    let settings_path = home.join("butler-config.json");
    let settings = Settings::load(&settings_path);
    let device = Device::new(dry_run, clock, env_path("BUTLER_CACHE", "/tmp/butler-client"));
    log!("butler-client {VERSION} 启动，NAS={server}{}", if dry_run { "（dry run）" } else { "" });
    // 上次可能停在只识别模式中途退出：先回正常模式，保证「小爱同学」可用
    device.init_asr_only();

    {
        let device = device.clone();
        let wake = settings.phrases.wake.clone();
        tokio::spawn(async move { device.prepare_wake(&wake).await });
    }

    let (in_tx, mut in_rx) = unbounded_channel::<Input>();
    let ws_out: WsOut = Arc::new(Mutex::new(None));

    // 念字、叫醒都走这条队列，一个接一个
    let (speech_tx, mut speech_rx) = unbounded_channel::<Action>();
    {
        let device = device.clone();
        tokio::spawn(async move {
            while let Some(action) = speech_rx.recv().await {
                match action {
                    Action::PlayWake(text) => device.play_wake(&text).await,
                    Action::WakeUp => device.wake_up().await,
                    Action::Speak { text, not_before } => device.speak(&text, not_before).await,
                    _ => {}
                }
            }
        });
    }

    spawn_kws_watch(env_path("BUTLER_KWS_LOG", "/tmp/open-xiaoai/kws.log"), in_tx.clone(), ws_out.clone());
    spawn_instruction_watch(env_path("BUTLER_INSTRUCTION_LOG", "/tmp/mico_aivs_lab/instruction.log"), in_tx.clone());
    {
        let in_tx = in_tx.clone();
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(TICK).await;
                if in_tx.send(Input::Tick).is_err() {
                    break;
                }
            }
        });
    }
    tokio::spawn(ws_loop(server, in_tx.clone(), ws_out.clone()));

    let mut machine = Machine::new(settings);
    while let Some(input) = in_rx.recv().await {
        if let Input::Config(s) = &input {
            if let Err(e) = s.save(&settings_path) {
                log!("配置没存下来：{e}");
            }
            let device = device.clone();
            let wake = s.phrases.wake.clone();
            tokio::spawn(async move { device.prepare_wake(&wake).await });
        }
        let before = state_name(machine.state());
        for action in machine.step(input, clock.now()) {
            match action {
                Action::Abort => device.abort(),
                Action::RefreshAivs { asr_only } => device.refresh_aivs(asr_only),
                Action::SendUtterance { id, text, dialog_id } => {
                    log!("听到：{text}");
                    let ev = Event {
                        id: id.clone(),
                        event: EV_UTTERANCE.into(),
                        data: Some(json!({ "text": text, "dialog_id": dialog_id })),
                    };
                    if !send(&ws_out, AppMessage::Event(ev)) {
                        log!("NAS 没连上，这句发不出去");
                        let _ = in_tx.send(Input::SendFailed(id));
                    }
                }
                other => {
                    let _ = speech_tx.send(other);
                }
            }
        }
        let after = state_name(machine.state());
        if before != after {
            log!("{before} → {after}");
        }
    }
}

fn state_name(s: &State) -> &'static str {
    match s {
        State::Idle => "待机",
        State::Listening { .. } => "听写",
        State::WaitReply { .. } => "等回复",
    }
}

fn send(ws_out: &WsOut, msg: AppMessage) -> bool {
    let guard = ws_out.lock().expect("ws_out poisoned");
    guard.as_ref().is_some_and(|tx| tx.send(Message::Text(protocol::encode(&msg).into())).is_ok())
}

fn spawn_kws_watch(path: PathBuf, in_tx: UnboundedSender<Input>, ws_out: WsOut) {
    tokio::spawn(async move {
        let mut tail = Tail::new(&path, true);
        let mut last_ts = 0;
        loop {
            for line in tail.poll() {
                match parse_kws(&line) {
                    Some(KwsLine::Keyword { ts, keyword }) if ts != last_ts => {
                        last_ts = ts;
                        log!("唤醒词：{keyword}");
                        send(&ws_out, AppMessage::Event(Event::new("kws", json!({ "keyword": keyword }))));
                        let _ = in_tx.send(Input::Kws(keyword));
                    }
                    Some(KwsLine::Started) => log!("KWS 已就绪"),
                    Some(_) => {}
                    None => log!("kws.log 坏行：{line}"),
                }
            }
            tokio::time::sleep(POLL).await;
        }
    });
}

fn spawn_instruction_watch(path: PathBuf, in_tx: UnboundedSender<Input>) {
    tokio::spawn(async move {
        let mut tail = Tail::new(&path, true);
        loop {
            for line in tail.poll() {
                if let Some(r) = parse_instruction(&line) {
                    let _ = in_tx.send(Input::Recognize(r));
                } else if let Some(dialog) = parse_dialog_finish(&line) {
                    let _ = in_tx.send(Input::DialogFinish(dialog));
                }
            }
            tokio::time::sleep(POLL).await;
        }
    });
}

async fn ws_loop(url: String, in_tx: UnboundedSender<Input>, ws_out: WsOut) {
    let mut backoff = Duration::from_secs(1);
    let mut reported = false;
    loop {
        let ws = match tokio_tungstenite::connect_async(url.as_str()).await {
            Ok((ws, _)) => ws,
            Err(e) => {
                if !reported {
                    log!("连不上 NAS：{e}，每隔几秒重试");
                    reported = true;
                }
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(Duration::from_secs(5));
                continue;
            }
        };
        log!("已连接 NAS");
        reported = false;
        backoff = Duration::from_secs(1);

        let (mut sink, mut stream) = ws.split();
        let (tx, mut rx) = unbounded_channel::<Message>();
        *ws_out.lock().expect("ws_out poisoned") = Some(tx.clone());
        let writer = tokio::spawn(async move {
            while let Some(m) = rx.recv().await {
                if sink.send(m).await.is_err() {
                    break;
                }
            }
        });

        loop {
            match tokio::time::timeout(SILENCE_LIMIT, stream.next()).await {
                Err(_) => {
                    log!("NAS 太久没动静，重连");
                    break;
                }
                Ok(None) | Ok(Some(Err(_))) | Ok(Some(Ok(Message::Close(_)))) => break,
                Ok(Some(Ok(Message::Text(t)))) => on_frame(t.as_str(), &in_tx, &tx),
                Ok(Some(Ok(_))) => {} // ping/pong 由 tungstenite 自己应答
            }
        }
        *ws_out.lock().expect("ws_out poisoned") = None;
        writer.abort();
        log!("和 NAS 断开");
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}

fn on_frame(frame: &str, in_tx: &UnboundedSender<Input>, tx: &UnboundedSender<Message>) {
    match protocol::decode(frame) {
        Some(AppMessage::Event(ev)) => {
            let data = ev.data.unwrap_or(Value::Null);
            let input = match ev.event.as_str() {
                EV_REPLY => serde_json::from_value::<ReplyData>(data).ok().map(|r| {
                    log!("回复（{}）：{}", r.kind, r.text);
                    Input::Reply(r)
                }),
                EV_SAY => data.get("text").and_then(Value::as_str).map(|t| Input::Say(t.to_string())),
                EV_CONFIG => serde_json::from_value::<Settings>(data).ok().map(|s| {
                    log!("收到配置");
                    Input::Config(s)
                }),
                _ => None,
            };
            if let Some(input) = input {
                let _ = in_tx.send(input);
            }
        }
        Some(AppMessage::Request(req)) => {
            let tx = tx.clone();
            tokio::spawn(async move {
                let res = handle_rpc(req).await;
                let _ = tx.send(Message::Text(protocol::encode(&AppMessage::Response(res)).into()));
            });
        }
        Some(AppMessage::Response(_)) => {}
        None => log!("看不懂的帧：{}", frame.chars().take(200).collect::<String>()),
    }
}

async fn handle_rpc(req: Request) -> Response {
    match req.command.as_str() {
        "get_version" => Response::ok(&req.id, json!(VERSION)),
        "run_shell" => match req.payload.as_ref().and_then(Value::as_str) {
            Some(script) => Response::ok(&req.id, device::run_shell(script).await),
            None => Response::err(&req.id, "empty command"),
        },
        _ => Response::err(&req.id, "command not found"),
    }
}
