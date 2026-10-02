//! 和 NAS 之间的 WS 文本帧，形状沿用 Open-XiaoAI（serde 外部标签枚举）：
//! `{"Request":{...}}` / `{"Response":{...}}` / `{"Event":{...}}`。

use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

pub const EV_UTTERANCE: &str = "butler_utterance";
pub const EV_REPLY: &str = "butler_reply";
pub const EV_SAY: &str = "butler_say";
pub const EV_CONFIG: &str = "butler_config";

#[derive(Debug, Serialize, Deserialize)]
pub enum AppMessage {
    Request(Request),
    Response(Response),
    Event(Event),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Request {
    pub id: String,
    pub command: String,
    #[serde(default)]
    pub payload: Option<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Response {
    pub id: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub code: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub msg: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub data: Option<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Event {
    pub id: String,
    pub event: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub data: Option<Value>,
}

impl Event {
    pub fn new(event: &str, data: Value) -> Self {
        Self { id: Uuid::new_v4().to_string(), event: event.to_string(), data: Some(data) }
    }
}

impl Response {
    pub fn ok(id: &str, data: Value) -> Self {
        Self { id: id.to_string(), code: Some(0), msg: Some("success".into()), data: Some(data) }
    }

    pub fn err(id: &str, msg: impl ToString) -> Self {
        Self { id: id.to_string(), code: Some(-1), msg: Some(msg.to_string()), data: None }
    }
}

pub fn encode(msg: &AppMessage) -> String {
    serde_json::to_string(msg).expect("AppMessage always serializes")
}

pub fn decode(frame: &str) -> Option<AppMessage> {
    serde_json::from_str(frame).ok()
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct ReplyData {
    pub reply_to: String,
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub text: String,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn decodes_server_frames() {
        let f =
            r#"{"Event":{"id":"e1","event":"butler_reply","data":{"reply_to":"u1","kind":"reply","text":"灯开了"}}}"#;
        let Some(AppMessage::Event(ev)) = decode(f) else { panic!("not an event") };
        assert_eq!(ev.event, EV_REPLY);
        let r: ReplyData = serde_json::from_value(ev.data.unwrap()).unwrap();
        assert_eq!(r, ReplyData { reply_to: "u1".into(), kind: "reply".into(), text: "灯开了".into() });

        let f = r#"{"Request":{"id":"r1","command":"get_version"}}"#;
        let Some(AppMessage::Request(req)) = decode(f) else { panic!("not a request") };
        assert_eq!(req.command, "get_version");
        assert!(req.payload.is_none());
    }

    #[test]
    fn encodes_like_upstream() {
        let s = encode(&AppMessage::Event(Event {
            id: "x".into(),
            event: EV_UTTERANCE.into(),
            data: Some(json!({"text":"开灯"})),
        }));
        assert_eq!(s, r#"{"Event":{"id":"x","event":"butler_utterance","data":{"text":"开灯"}}}"#);
        let s = encode(&AppMessage::Response(Response::ok("r1", json!("0.1.0"))));
        assert_eq!(s, r#"{"Response":{"id":"r1","code":0,"msg":"success","data":"0.1.0"}}"#);
    }

    #[test]
    fn junk_is_none() {
        assert!(decode("hello").is_none());
        assert!(decode(r#"{"Stream":{"id":"1"}}"#).is_none());
    }
}
