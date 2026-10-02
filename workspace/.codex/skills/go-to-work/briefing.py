#!/usr/bin/env python3
"""上班前：并行执行配置好的设备动作（比如开窗帘）、查天气、查今天的飞书日程，输出一段 JSON 交给 Codex 组织播报。
除了配置里写的设备，家里别的设备都不动。

配置 ~/.mihome/go-to-work.json（样例见同目录 go-to-work.example.json）：
- city：{name, lat, lon}，查天气用
- devices：可选，{what: 做了什么（比如「拉开客厅窗帘」）, control: [[did, iid, value], ...]}，一条 mihome device control 下发
- agenda：可选，false = 不查飞书日程

只用标准库。任何一项失败都不影响其他项，失败原因写进对应字段。
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import os
import re
import subprocess
import urllib.request

CFG_PATH = os.path.expanduser("~/.mihome/go-to-work.json")
TZ = dt.timezone(dt.timedelta(hours=8))

WMO = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "有雾", 48: "有雾",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨", 56: "冻毛毛雨", 57: "冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "阵雨", 82: "强阵雨", 85: "阵雪", 86: "阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "雷阵雨伴冰雹",
}
RAINY = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}


def run(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def load_cfg() -> dict:
    with open(CFG_PATH) as f:
        return json.load(f)


def run_devices(devices: dict) -> dict:
    cmd = ["mihome", "device", "control"]
    for did, iid, value in devices["control"]:
        cmd += [str(did), str(iid), json.dumps(value) if isinstance(value, bool) else str(value)]
    try:
        code, out, err = run(cmd, timeout=20)
        data = json.loads(out) if out.strip().startswith("{") else {}
        if code == 0 and data.get("code", 0) == 0:
            return {"ok": True, "what": devices.get("what", "")}
        return {"ok": False, "error": data.get("message") or err.strip()[-200:] or f"退出码 {code}"}
    except Exception as e:  # noqa: BLE001 —— 一项失败不影响其他
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def weather(city: dict) -> dict:
    if not (city.get("lat") and city.get("lon")):
        return {"ok": False, "error": "go-to-work.json 里没配置 city 的 lat / lon"}
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={city['lat']}&longitude={city['lon']}&timezone=Asia/Shanghai&forecast_days=1"
        "&current=temperature_2m,weather_code"
        "&hourly=precipitation_probability,weather_code,temperature_2m"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            d = json.load(r)
        cur, day, hourly = d["current"], d["daily"], d.get("hourly", {})
        code_now, code_day = int(cur["weather_code"]), int(day["weather_code"][0])
        rain_prob = day.get("precipitation_probability_max", [None])[0]
        # 接下来 6 小时：最大降雨概率、最早可能下雨的钟点（概率 ≥ 50 或天气码是雨）
        now_hour = dt.datetime.now(TZ).strftime("%Y-%m-%dT%H:00")
        hours = [
            (t[11:16], p, int(c), temp)
            for t, p, c, temp in zip(
                hourly.get("time", []),
                hourly.get("precipitation_probability", []),
                hourly.get("weather_code", []),
                hourly.get("temperature_2m", []),
            )
            if t >= now_hour
        ][:6]
        rain_hours = [h for h, p, c, _ in hours if (p or 0) >= 50 or c in RAINY]
        return {
            "ok": True,
            "city": city.get("name", ""),
            "now": {"temp": round(cur["temperature_2m"]), "desc": WMO.get(code_now, "未知")},
            "today": {
                "desc": WMO.get(code_day, "未知"),
                "max": round(day["temperature_2m_max"][0]),
                "min": round(day["temperature_2m_min"][0]),
                "rain_prob": rain_prob,
            },
            "next_6h": {
                "rain_prob_max": max((p or 0 for _, p, _, _ in hours), default=None),
                "rain_from": rain_hours[0] if rain_hours else None,
                "temp_range": [round(min(t for *_, t in hours)), round(max(t for *_, t in hours))] if hours else None,
            },
            "umbrella": code_day in RAINY or (rain_prob or 0) >= 40,
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _parse_time(t: dict | None) -> dt.datetime | None:
    """飞书日程时间：{datetime: ISO8601} / {timestamp: 秒} / {date: YYYY-MM-DD}。"""
    if not t:
        return None
    if t.get("datetime"):
        return dt.datetime.fromisoformat(t["datetime"]).astimezone(TZ)
    if t.get("timestamp"):
        return dt.datetime.fromtimestamp(int(t["timestamp"]), TZ)
    if t.get("date"):
        return dt.datetime.fromisoformat(t["date"]).replace(tzinfo=TZ)
    return None


def _brief(text: str, limit: int = 100) -> str:
    """会议说明摘要：去掉链接和 markdown 符号，压成一行，截到 limit 字。"""
    t = re.sub(r"https?://\S+", "", text or "")
    t = re.sub(r"[*#>`_\[\]|]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= limit else t[:limit] + "…"


RSVP = {"accept": "已接受", "needs_action": "未回复", "tentative": "待定", "decline": "已拒绝"}


def _simplify(ev: dict, now: dt.datetime) -> dict | None:
    if ev.get("status") == "cancelled" or ev.get("self_rsvp_status") == "decline":
        return None
    start, end = _parse_time(ev.get("start_time")), _parse_time(ev.get("end_time"))
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + dt.timedelta(days=1)
    # 盖住今天一整天（含跨多天的，比如假期）就当全天日程说
    all_day = bool(start and end and start <= day_start and end >= day_end - dt.timedelta(minutes=1))
    out = {
        "title": ev.get("summary") or "（无标题）",
        "all_day": all_day,
        "start": None if all_day or not start else start.strftime("%H:%M"),
        "end": None if all_day or not end else end.strftime("%H:%M"),
        "ended": bool(end and end <= now and not all_day),
        "rsvp": RSVP.get(ev.get("self_rsvp_status") or "", ev.get("self_rsvp_status") or "未知"),
    }
    if start and end and not all_day:
        out["minutes"] = int((end - start).total_seconds() // 60)
    vchat = ev.get("vchat") or {}
    out["online"] = vchat.get("vc_type") == "vc" or bool(vchat.get("meeting_url"))
    if ev.get("description"):
        out["about"] = _brief(ev["description"])
    if isinstance(ev.get("location"), dict) and ev["location"].get("name"):
        out["location"] = ev["location"]["name"]
    return out


def _lark_error(out: str, err: str, code: int) -> str:
    """lark-cli 出错时输出 {"ok":false,"error":{type,subtype,message}}（有时带前缀），转成一句人话。"""
    text = f"{out or ''}\n{err or ''}"
    info: dict = {}
    start = text.find("{")
    if start >= 0:
        try:
            info = json.loads(text[start:]).get("error") or {}
        except (json.JSONDecodeError, AttributeError):
            info = {}
    kind = f"{info.get('type', '')} {info.get('subtype', '')} {info.get('message', '')} {text}".lower()
    if "not_configured" in kind or "not configured" in kind:
        return "飞书还没配置好（NAS 上需要先 lark-cli config init）"
    if any(k in kind for k in ("refresh token", "expired", "not logged", "auth login", "unauthorized", "missing")):
        return "飞书授权过期或未登录（NAS 上需要重新 lark-cli auth login）"
    return (info.get("message") or text.strip())[-200:] or f"退出码 {code}"


def agenda() -> dict:
    try:
        code, out, err = run(["lark-cli", "calendar", "+agenda", "--as", "user", "--format", "json"], timeout=30)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if code != 0:
        return {"ok": False, "error": _lark_error(out, err, code)}
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "error": "日程返回不是 JSON：" + out.strip()[:200]}
    items = d.get("data", d) if isinstance(d, dict) else d
    if isinstance(items, dict):
        items = items.get("items") or items.get("events") or []
    now = dt.datetime.now(TZ)
    events = [e for e in (_simplify(ev, now) for ev in items or [] if isinstance(ev, dict)) if e]
    events.sort(key=lambda e: (not e["all_day"], e["start"] or ""))
    upcoming = [e for e in events if not e["ended"]]
    return {
        "ok": True,
        "count": len(events),
        "upcoming": len(upcoming),
        "pending_rsvp": sum(1 for e in events if e["rsvp"] == "未回复"),
        "events": events,
    }


def main() -> None:
    now = dt.datetime.now(TZ)
    try:
        cfg = load_cfg()
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"ok": False, "error": f"读不了 {CFG_PATH}：{type(e).__name__}"}, ensure_ascii=False))
        return
    jobs = {"weather": lambda: weather(cfg.get("city") or {})}
    if cfg.get("devices", {}).get("control"):
        jobs["devices"] = lambda: run_devices(cfg["devices"])
    if cfg.get("agenda", True):
        jobs["agenda"] = agenda
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futs = {k: pool.submit(fn) for k, fn in jobs.items()}
        result = {k: f.result() for k, f in futs.items()}
    print(
        json.dumps(
            {
                "date": now.strftime("%Y-%m-%d"),
                "weekday": "一二三四五六日"[now.weekday()],
                "time": now.strftime("%H:%M"),
                **result,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
