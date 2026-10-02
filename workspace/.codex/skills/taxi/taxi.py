#!/usr/bin/env python3
"""叫车（滴滴 MCP）：固定路线 家 → 公司。

    taxi.py estimate   询价（不花钱），记下这次询价
    taxi.py order      用最近一次询价下单（真金白银）
    taxi.py status     查当前订单：司机、车牌、多久到
    taxi.py cancel     取消当前订单

保护规则写在这里，不靠提示词：
- 只有一条路线：配置里的 home → office，不接受别的起终点
- 下单必须用 estimate_ttl_sec 内的询价；一次询价只能下一单
- 正式环境每天最多 daily_limit 单（沙箱不限）；有进行中的订单时不能再叫

配置 ~/.mihome/didi.json（MCP Key、env=sandbox|production、home/office 坐标、product、daily_limit、estimate_ttl_sec），
状态 ~/.mihome/didi-state.json。Key 只在本进程里用，不打印。输出一行 JSON 给 Codex 组织口播。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

CFG_PATH = os.path.expanduser("~/.mihome/didi.json")
STATE_PATH = os.path.expanduser("~/.mihome/didi-state.json")
ENDPOINT = {
    "sandbox": "https://mcp.didichuxing.com/mcp-servers-sandbox",
    "production": "https://mcp.didichuxing.com/mcp-servers",
}
TZ = dt.timezone(dt.timedelta(hours=8))
ACTIVE_MAX_SEC = 3 * 3600  # 下单 3 小时后不再视为进行中


class TaxiError(Exception):
    pass


def load_cfg() -> dict:
    cfg = json.load(open(CFG_PATH))
    if cfg.get("env") not in ENDPOINT:
        raise TaxiError("didi.json 里的 env 只能是 sandbox 或 production")
    for k in ("mcp_key", "home", "office", "product"):
        if not cfg.get(k):
            raise TaxiError(f"didi.json 缺少 {k}")
    return cfg


def load_state() -> dict:
    try:
        return json.load(open(STATE_PATH))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_PATH)


def call(cfg: dict, tool: str, args: dict) -> dict:
    url = f"{ENDPOINT[cfg['env']]}?key={cfg['mcp_key'].strip()}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json, text/event-stream"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode()
    except Exception as e:  # noqa: BLE001
        raise TaxiError(f"连不上滴滴：{type(e).__name__}") from None
    datas = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
    res = json.loads(datas[-1] if datas else raw)
    if res.get("error"):
        raise TaxiError(f"滴滴返回错误：{res['error'].get('message') or res['error']}")
    r = res.get("result") or {}
    text = "".join(c.get("text", "") for c in r.get("content", []) if isinstance(c, dict))
    if r.get("isError"):
        raise TaxiError(text.strip()[:200] or "滴滴返回错误")
    return {"structured": r.get("structuredContent"), "text": text}


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def today_orders(state: dict, env: str) -> list[dict]:
    day = now().strftime("%Y-%m-%d")
    return [o for o in state.get("orders", []) if o.get("env") == env and o.get("at", "").startswith(day)]


def active_order(state: dict, env: str) -> dict | None:
    o = state.get("active")
    if not o or o.get("env") != env:
        return None
    age = (now() - dt.datetime.fromisoformat(o["at"])).total_seconds()
    return o if age < ACTIVE_MAX_SEC else None


def parse_order_text(text: str) -> dict:
    """查订单只返回文字：抽出状态、车型、车牌、距离和到达时间。"""
    t = text.strip()
    info: dict = {"status_text": t.splitlines()[0].strip() if t else ""}
    if m := re.search(r"车型[：:]\s*(.+)", t):
        info["car"] = m.group(1).strip().replace(" · ", "")
    if m := re.search(r"车牌[：:]\s*(\S+)", t):
        info["plate"] = m.group(1).strip()
    if m := re.search(r"([\d.]+)\s*公里", t):
        info["distance_km"] = float(m.group(1))
    if m := re.search(r"约需\s*(\d+)\s*分钟", t):
        info["eta_min"] = int(m.group(1))
    if "取消" in info["status_text"]:
        info["state"] = "cancelled"
    elif "完成" in t or "已结束" in t:
        info["state"] = "finished"
    elif "到达起点" in t or "已到达" in t:
        info["state"] = "arrived"
    elif "plate" in info:
        info["state"] = "assigned"
    elif "匹配" in t:
        info["state"] = "matching"
    else:
        info["state"] = "unknown"
    return info


def push(cfg: dict, text: str) -> bool:
    prefix = "【测试】" if cfg["env"] == "sandbox" else ""
    try:
        r = subprocess.run(["mihome", "notify", "push", "--text", prefix + text], capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def cmd_estimate(cfg: dict, state: dict) -> dict:
    h, o = cfg["home"], cfg["office"]
    res = call(
        cfg,
        "taxi_estimate",
        {"from_lng": h["lng"], "from_lat": h["lat"], "from_name": h["name"],
         "to_lng": o["lng"], "to_lat": o["lat"], "to_name": o["name"]},
    )
    sc = res["structured"] or {}
    items = sc.get("items") or []
    item = next((i for i in items if i.get("productName") == cfg["product"]), None)
    if not item:
        names = "、".join(i.get("productName", "?") for i in items) or "无"
        raise TaxiError(f"现在叫不到{cfg['product']}，可用车型：{names}")
    state["estimate"] = {
        "env": cfg["env"],
        "trace_id": sc["traceId"],
        "product_category": item["productCategory"],
        "product": item["productName"],
        "price": item.get("priceText"),
        "price_discounted": item.get("priceDiscounted"),
        "at": now().isoformat(),
    }
    save_state(state)
    return {
        "ok": True,
        "step": "estimate",
        "env": cfg["env"],
        "route": f"{h['name']} → {o['name']}",
        "product": item["productName"],
        "price": item.get("priceText"),
        "price_discounted": item.get("priceDiscounted"),
        "confirm_within_sec": cfg.get("estimate_ttl_sec", 180),
    }


def cmd_order(cfg: dict, state: dict) -> dict:
    est = state.get("estimate")
    ttl = cfg.get("estimate_ttl_sec", 180)
    if not est or est.get("env") != cfg["env"]:
        raise TaxiError("还没询过价，要先问价格再确认")
    age = (now() - dt.datetime.fromisoformat(est["at"])).total_seconds()
    if age > ttl:
        raise TaxiError(f"上次询价已经过了 {int(age // 60)} 分钟，价格可能变了，需要重新询价")
    if active_order(state, cfg["env"]):
        raise TaxiError("已经有一单在进行中，不能再叫")
    limit = cfg.get("daily_limit", 2)
    # 沙箱订单是假的，不占次数，免得测几次就测不下去
    if cfg["env"] == "production" and len(today_orders(state, cfg["env"])) >= limit:
        raise TaxiError(f"今天已经叫了 {limit} 次，到上限了")

    # 一次询价只能下一单：先作废，再下单
    state.pop("estimate", None)
    save_state(state)
    res = call(cfg, "taxi_create_order", {"product_category": est["product_category"], "estimate_trace_id": est["trace_id"]})
    sc = res["structured"] or {}
    order_id = sc.get("orderId") or sc.get("order_id")
    if not order_id:
        raise TaxiError("下单没拿到订单号：" + res["text"].strip()[:150])
    rec = {"id": order_id, "env": cfg["env"], "at": now().isoformat(), "product": est["product"], "price": est.get("price")}
    state["active"] = rec
    state.setdefault("orders", []).append(rec)
    state["orders"] = state["orders"][-50:]
    save_state(state)

    # 等司机接单，最多 30 秒
    info: dict = {"state": "matching"}
    for _ in range(6):
        time.sleep(5)
        try:
            info = parse_order_text(call(cfg, "taxi_query_order", {"order_id": order_id})["text"])
        except TaxiError:
            continue
        if info.get("plate"):
            break
    pushed = False
    if info.get("plate"):
        eta = f"，约 {info['eta_min']} 分钟到" if info.get("eta_min") else ""
        pushed = push(cfg, f"已叫车去{cfg['office']['name']}：{info.get('car', '')} {info['plate']}{eta}")
    return {"ok": True, "step": "order", "env": cfg["env"], "order_id_tail": order_id[-4:], "product": est["product"],
            "price": est.get("price"), "driver": info, "pushed_to_phone": pushed}


def cmd_status(cfg: dict, state: dict) -> dict:
    o = active_order(state, cfg["env"])
    if not o:
        return {"ok": True, "step": "status", "env": cfg["env"], "active": False}
    info = parse_order_text(call(cfg, "taxi_query_order", {"order_id": o["id"]})["text"])
    if info["state"] in ("cancelled", "finished"):
        state.pop("active", None)
        save_state(state)
    return {"ok": True, "step": "status", "env": cfg["env"], "active": True, "driver": info}


def cmd_cancel(cfg: dict, state: dict) -> dict:
    o = active_order(state, cfg["env"])
    if not o:
        return {"ok": True, "step": "cancel", "env": cfg["env"], "cancelled": False, "reason": "没有进行中的订单"}
    res = call(cfg, "taxi_cancel_order", {"order_id": o["id"], "reason": "不需要了"})
    ok = bool((res["structured"] or {}).get("success")) or "取消成功" in res["text"]
    if ok:
        state.pop("active", None)
        save_state(state)
    return {"ok": ok, "step": "cancel", "env": cfg["env"], "cancelled": ok, "note": res["text"].strip().splitlines()[0][:80] if res["text"] else ""}


def main() -> None:
    cmds = {"estimate": cmd_estimate, "order": cmd_order, "status": cmd_status, "cancel": cmd_cancel}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        print(json.dumps({"ok": False, "error": "用法：taxi.py estimate|order|status|cancel"}, ensure_ascii=False))
        sys.exit(2)
    try:
        cfg = load_cfg()
        out = cmds[sys.argv[1]](cfg, load_state())
    except TaxiError as e:
        out = {"ok": False, "step": sys.argv[1], "error": str(e)}
    except (OSError, json.JSONDecodeError) as e:
        out = {"ok": False, "step": sys.argv[1], "error": f"读配置失败：{type(e).__name__}"}
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
