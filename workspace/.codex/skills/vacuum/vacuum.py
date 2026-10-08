#!/usr/bin/env python3
"""扫地机（追觅 Dreamehome 云）：查状态、开始 / 暂停 / 停止 / 回充、按房间清扫。

要用 install.sh 装好的 Python 跑（协议包和依赖都在那里）：

    ~/.mihome/dreame-venv/bin/python .codex/skills/vacuum/vacuum.py <命令>

    setup                   在终端里交互登录一次：输入账号密码，只保存 refresh token，密码不落盘
    status                  状态、电量、充电、本次清扫、故障
    start [--mode M] [--confirm]        全屋清扫（或继续暂停的清扫）
    clean 房间… [--mode M] [--confirm]  按房间清扫，房间名查 rooms 缓存
    goto 房间 [--confirm]   开到房间中心停下，不清扫（定点巡航，要机型支持）
    mode                    当前清洁模式
    pause | stop | dock     暂停 / 停止 / 回充
    rooms [--refresh]       房间表；--refresh 读一次地图更新缓存（约 5 秒，只读）

清洁模式 M：扫地 / 拖地 / 扫拖 / 先扫后拖。不写就是扫地；每次开扫前都按这次的说法设一遍，不沿用上次。

保护规则写在这里，不靠提示词：
- 夜间（22:00–8:00）start / clean / goto 不带 --confirm 就不下发，返回 need_confirm
- 故障状态下不开扫，直接返回故障原因；正在扫时不重复开始、也不去别的房间
- goto 电量低于 15% 不去（同 Tasshack）
- 房间名对不上就列出可用的房间，不猜

配置 ~/.mihome/dreame.json（setup 生成，600）：账号、区域、refresh token、设备 did。
房间缓存 ~/.mihome/dreame-rooms.json。协议来自 Tasshack/dreame-vacuum（MIT），install.sh 装在 ~/.mihome/vendor/dreame。
输出一行 JSON 给 Codex 组织口播；token 只在本进程里用，不打印。
"""

from __future__ import annotations

import datetime as dt
import getpass
import json
import logging
import os
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("MIHOME_HOME") or Path.home() / ".mihome")
CFG_PATH = HOME / "dreame.json"
ROOMS_PATH = HOME / "dreame-rooms.json"
VENDOR = HOME / "vendor"
TZ = dt.timezone(dt.timedelta(hours=8))
NIGHT = (22, 8)  # 22:00 到次日 8:00 开扫要确认

# 协议包里有个 types.py，当前目录在它旁边时会盖住标准库；只从 vendor 目录导入
sys.path.insert(0, str(VENDOR))
logging.disable(logging.CRITICAL)  # 协议包的日志会混进输出，必要时用 VACUUM_DEBUG=1 打开
if os.environ.get("VACUUM_DEBUG"):
    logging.disable(logging.NOTSET)
    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)

from dreame.protocol import DreameVacuumDreameHomeCloudProtocol as Cloud  # noqa: E402
from dreame.types import (  # noqa: E402
    DreameVacuumChargingStatus,
    DreameVacuumErrorCode,
    DreameVacuumState,
    DreameVacuumStatus,
    DreameVacuumSuctionLevel,
    DreameVacuumTaskStatus,
)

# 属性 siid.piid，见 Tasshack types.py 的 DreameVacuumPropertyMapping
PROPS = {
    "state": (2, 1),
    "error": (2, 2),
    "battery": (3, 1),
    "charging": (3, 2),
    "status": (4, 1),
    "cleaning_time": (4, 2),
    "cleaned_area": (4, 3),
    "suction": (4, 4),
    "water": (4, 5),
    "task": (4, 7),
    "mode": (4, 23),
}
# 动作 siid.aiid，见 DreameVacuumActionMapping
ACTIONS = {"start": (2, 1), "pause": (2, 2), "dock": (3, 1), "custom": (4, 1), "stop": (4, 2)}
# 有自清洁基站、能抬拖布的机型（如 S50 Pro）：属性 4.23 是组合值，最低两位是模式，其余位是拖布自清洁设置。
# 编码见 Tasshack device.py 的 _cleaning_mode_changed / _update_cleaning_mode
MODES = {"扫地": 2, "拖地": 1, "扫拖": 0, "先扫后拖": 3}
DEFAULT_MODE = "扫地"
CLEANING_STATES = {
    DreameVacuumState.SWEEPING,
    DreameVacuumState.MOPPING,
    DreameVacuumState.SWEEPING_AND_MOPPING,
    DreameVacuumState.SECOND_CLEANING,
    DreameVacuumState.SPOT_CLEANING,
}


class VacuumError(Exception):
    pass


# ---------- 配置 ----------


def load_cfg() -> dict:
    try:
        cfg = json.loads(CFG_PATH.read_text())
    except FileNotFoundError:
        raise VacuumError("还没登录追觅账号：在终端里运行一次 vacuum.py setup") from None
    for k in ("username", "auth_key", "did"):
        if not cfg.get(k):
            raise VacuumError(f"dreame.json 缺少 {k}，在终端里重新运行 vacuum.py setup")
    return cfg


def save_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def zh() -> dict:
    """Tasshack 自带的简体中文：状态、故障码、充电状态（sensor）和吸力（select）。"""
    try:
        entity = json.loads((VENDOR / "dreame" / "zh-Hans.json").read_text())["entity"]
    except (OSError, KeyError, json.JSONDecodeError):
        return {}
    names: dict = {}
    for domain in ("select", "sensor"):
        for k, v in entity.get(domain, {}).items():
            names.setdefault(k, {}).update(v.get("state", {}))
    return names


def label(names: dict, group: str, enum, value) -> str | None:
    try:
        key = enum(value).name.lower()
    except ValueError:
        return None
    return names.get(group, {}).get(key, key)


# ---------- 云端 ----------


def connect(cfg: dict) -> Cloud:
    """用 refresh token 续期登录（不带密码：token 失效时不会拿错误密码反复去试）。"""
    cloud = Cloud(cfg["username"], "", "dreame", cfg.get("country", "cn"), cfg["auth_key"], str(cfg["did"]))
    if not cloud.login():
        if cloud.auth_failed:
            raise VacuumError("追觅登录已失效，需要在终端里重新运行 vacuum.py setup")
        raise VacuumError("暂时连不上追觅云，稍后再试")
    if cloud.auth_key and cloud.auth_key != cfg["auth_key"]:
        cfg["auth_key"] = cloud.auth_key  # refresh token 可能轮换，存下最新的
        save_json(CFG_PATH, cfg)
    if not cloud.get_device_info():  # 拿到设备所在的服务器，send 要用
        raise VacuumError("追觅云里找不到这台扫地机，可能被解绑了")
    return cloud


def read_props(cloud: Cloud, did: str) -> dict:
    params = [{"did": did, "siid": s, "piid": p} for s, p in PROPS.values()]
    res = cloud.send("get_properties", params)
    if not isinstance(res, list):
        raise VacuumError("读不到扫地机状态")
    by_iid = {(r.get("siid"), r.get("piid")): r.get("value") for r in res if r.get("code", 0) == 0}
    return {name: by_iid.get(iid) for name, iid in PROPS.items()}


def act(cloud: Cloud, did: str, name: str, params=None) -> None:
    siid, aiid = ACTIONS[name]
    res = cloud.send("action", {"did": did, "siid": siid, "aiid": aiid, "in": params or []})
    code = res.get("code") if isinstance(res, dict) else None
    if code != 0:
        raise VacuumError(f"指令没被接受（{code if code is not None else '无响应'}）")


def describe(raw: dict) -> dict:
    names = zh()
    out = {
        "state": label(names, "state", DreameVacuumState, raw["state"]),
        "battery": raw["battery"],
        "charging": label(names, "charging_status", DreameVacuumChargingStatus, raw["charging"]),
        "task": label(names, "task_status", DreameVacuumTaskStatus, raw["task"]),
        "suction": label(names, "suction_level", DreameVacuumSuctionLevel, raw["suction"]),
        "mode": mode_name(raw["mode"]),
    }
    if raw["task"] not in (None, DreameVacuumTaskStatus.COMPLETED):
        out["cleaning_minutes"] = raw["cleaning_time"]
        out["cleaned_area_m2"] = raw["cleaned_area"]
    if raw["error"]:
        out["error"] = label(names, "error", DreameVacuumErrorCode, raw["error"]) or f"故障码 {raw['error']}"
    return out


def mode_name(raw) -> str | None:
    if raw is None or not mode_supported():
        return None
    code = raw & 0x03
    return next((k for k, v in MODES.items() if v == code), None)


def mode_supported() -> bool:
    caps = load_rooms_file().get("capability", {})
    return bool(caps.get("self_wash_base") and caps.get("mop_pad_lifting"))


def set_mode(cloud: Cloud, did: str, raw: dict, mode: str) -> None:
    """改属性 4.23 的模式位，拖布自清洁那几位原样保留；改完读回来确认。"""
    from dreame.device import DreameVacuumDevice

    if not mode_supported():
        raise VacuumError("这款扫地机切换模式的写法还没适配（或者还没跑 rooms --refresh）")
    if mode == "先扫后拖" and not load_rooms_file().get("capability", {}).get("mopping_after_sweeping"):
        raise VacuumError("这款扫地机不支持先扫后拖")
    if raw["mode"] is None:
        raise VacuumError("读不到当前的清洁模式")
    values = DreameVacuumDevice.split_group_value(raw["mode"], True)
    values[0] = MODES[mode]
    new = DreameVacuumDevice.combine_group_value(values)
    if new == raw["mode"]:
        return
    res = cloud.send("set_properties", [{"did": did, "siid": 4, "piid": 23, "value": new}])
    if not (isinstance(res, list) and res and res[0].get("code", 0) == 0):
        raise VacuumError(f"切换到{mode}没成功")
    for _ in range(3):
        time.sleep(1)
        if mode_name(read_props(cloud, did)["mode"]) == mode:
            return
    raise VacuumError(f"切换到{mode}后读回来不对，可能拖布没装好")


def pick_mode(args) -> str:
    if "--mode" not in args:
        return DEFAULT_MODE
    i = args.index("--mode")
    mode = args[i + 1] if i + 1 < len(args) else ""
    if mode not in MODES:
        raise VacuumError(f"清洁模式只能是：{'、'.join(MODES)}")
    return mode


def is_night(now: dt.datetime) -> bool:
    start, end = NIGHT
    return now.hour >= start or now.hour < end


# ---------- 命令 ----------


def cmd_setup(_args) -> dict:
    if not sys.stdin.isatty():
        raise VacuumError("setup 要在终端里交互运行（会提示输入密码）")
    username = input("Dreamehome 账号（手机号或邮箱）：").strip()
    password = getpass.getpass("密码（不会保存）：")
    country = input("区域 [cn]：").strip() or "cn"
    cloud = Cloud(username, password, "dreame", country)
    if not cloud.login():
        raise VacuumError("登录失败：账号密码不对，或者需要先在追觅 App 里完成验证")
    found = []

    def walk(o):
        if isinstance(o, dict):
            if "did" in o and "model" in o:
                found.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(cloud.get_devices())
    vacuums = [d for d in found if ".vacuum." in str(d.get("model"))]
    if not vacuums:
        raise VacuumError("账号下没找到扫地机")
    for i, d in enumerate(vacuums):
        print(f"  [{i}] {d.get('customName') or d.get('name') or ''} {d['model']}")
    pick = vacuums[0] if len(vacuums) == 1 else vacuums[int(input("选哪台：") or 0)]
    cfg = {
        "username": username,
        "country": country,
        "auth_key": cloud.auth_key,
        "did": str(pick["did"]),
        "model": pick["model"],
        "name": pick.get("customName") or pick.get("name"),
    }
    HOME.mkdir(parents=True, exist_ok=True)
    save_json(CFG_PATH, cfg)
    return {"ok": True, "step": "setup", "model": cfg["model"], "name": cfg["name"], "saved": str(CFG_PATH)}


def cmd_status(_args) -> dict:
    cfg = load_cfg()
    cloud = connect(cfg)
    return {"ok": True, "step": "status", **describe(read_props(cloud, cfg["did"]))}


def _guard_start(cloud: Cloud, cfg: dict, args, mode: str | None) -> tuple[dict | None, dict]:
    """开扫 / 出发前的检查和模式设置（mode 为 None 不设）；第一个值不是 None 表示不下发、直接回这个结果。"""
    raw = read_props(cloud, cfg["did"])
    if raw["state"] == DreameVacuumState.ERROR:
        return {"ok": False, "step": args[0], "reason": "error", **describe(raw)}, raw
    if raw["state"] in CLEANING_STATES:
        return {"ok": False, "step": args[0], "reason": "already_cleaning", **describe(raw)}, raw
    if is_night(dt.datetime.now(TZ)) and "--confirm" not in args:
        out = {"ok": False, "step": args[0], "need_confirm": True, "reason": "night"}
        return ({**out, "mode": mode} if mode else out), raw
    if mode:
        set_mode(cloud, cfg["did"], raw, mode)
    return None, raw


def cmd_start(args) -> dict:
    mode = pick_mode(args)
    cfg = load_cfg()
    cloud = connect(cfg)
    stop, _raw = _guard_start(cloud, cfg, args, mode)
    if stop is not None:
        return stop
    act(cloud, cfg["did"], "start")
    return {"ok": True, "step": "start", "mode": mode}


def cmd_clean(args) -> dict:
    mode = pick_mode(args)
    names = [a for a in args[1:] if not a.startswith("--") and a not in MODES]
    if not names:
        raise VacuumError("要说扫哪个房间")
    rooms = load_rooms()
    missing = [n for n in names if n not in rooms]
    if missing:
        return {"ok": False, "step": "clean", "reason": "unknown_room", "unknown": missing, "rooms": list(rooms)}
    cfg = load_cfg()
    cloud = connect(cfg)
    stop, raw = _guard_start(cloud, cfg, args, mode)
    if stop is not None:
        return stop
    # 每个房间：[编号, 遍数, 吸力, 水量, 顺序]，吸力和水量用扫地机当前的设置（同 Tasshack clean_segment）。
    # 支持「按房间定制」的新机型顺序位必须是 1，其他机型按 1、2、3 排
    suction = raw["suction"] if raw["suction"] is not None else 1
    water = raw["water"] if raw["water"] is not None else 2
    custom = load_rooms_file().get("customized_cleaning", True)
    selects = [[rooms[n], 1, suction, water, 1 if custom else i + 1] for i, n in enumerate(names)]
    payload = [
        {"piid": PROPS["status"][1], "value": DreameVacuumStatus.SEGMENT_CLEANING.value},
        {"piid": 10, "value": json.dumps({"selects": selects}, separators=(",", ":"))},
    ]
    act(cloud, cfg["did"], "custom", payload)
    return {"ok": True, "step": "clean", "rooms": names, "mode": mode}


def cmd_goto(args) -> dict:
    names = [a for a in args[1:] if not a.startswith("--")]
    if len(names) != 1:
        raise VacuumError("一次只能去一个房间")
    name = names[0]
    data = load_rooms_file()
    if not data.get("capability", {}).get("cruising"):
        raise VacuumError("这款扫地机不支持定点巡航（或者还没跑 rooms --refresh）")
    centers = data.get("centers") or {}
    if name not in centers:
        return {"ok": False, "step": "goto", "reason": "unknown_room", "unknown": [name], "rooms": list(centers)}
    cfg = load_cfg()
    cloud = connect(cfg)
    stop, raw = _guard_start(cloud, cfg, args, None)
    if stop is not None:
        return stop
    if raw["battery"] is not None and raw["battery"] < 15:
        return {"ok": False, "step": "goto", "reason": "low_battery", "battery": raw["battery"]}
    x, y = centers[name]
    payload = [
        {"piid": PROPS["status"][1], "value": DreameVacuumStatus.CRUISING_POINT.value},
        {"piid": 10, "value": json.dumps({"tpoint": [[x, y, 0, 0]]}, separators=(",", ":"))},
    ]
    act(cloud, cfg["did"], "custom", payload)
    return {"ok": True, "step": "goto", "room": name}


def cmd_mode(_args) -> dict:
    cfg = load_cfg()
    raw = read_props(connect(cfg), cfg["did"])
    return {"ok": True, "step": "mode", "mode": mode_name(raw["mode"]), "modes": list(MODES)}


def simple(name: str):
    def run(_args) -> dict:
        cfg = load_cfg()
        act(connect(cfg), cfg["did"], name)
        return {"ok": True, "step": name}

    return run


def load_rooms_file() -> dict:
    try:
        return json.loads(ROOMS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def load_rooms() -> dict:
    rooms = load_rooms_file().get("rooms")
    if not rooms:
        raise VacuumError("还没有房间表：先跑 vacuum.py rooms --refresh")
    return rooms


def refresh_rooms() -> dict:
    """用 Tasshack 的设备类加载一次地图，取「房间名 → 编号」。只读，不下发控制指令。"""
    from dreame.device import DreameVacuumDevice
    from dreame.types import SEGMENT_TYPE_CODE_TO_NAME

    cfg = load_cfg()
    # 设备类要求传密码才会建云端连接；给个占位，再清空，refresh token 失效时就不会拿它去试登录
    dev = DreameVacuumDevice(
        cfg.get("name") or "vacuum", None, None, None, cfg["username"], "-", cfg.get("country", "cn"),
        True, "dreame", str(cfg["did"]), cfg["auth_key"],
    )
    dev._protocol.cloud._password = ""
    try:
        dev.update()
        for _ in range(40):
            if dev.status.segments:
                break
            time.sleep(0.5)
        segments = dev.status.segments or {}
        if not segments:
            if dev.auth_failed:
                raise VacuumError("追觅登录已失效，需要在终端里重新运行 vacuum.py setup")
            raise VacuumError("没读到地图里的房间，可能还没建图")
        type_zh = zh().get("segment_name", {})
        rooms: dict[str, int] = {}
        centers: dict[str, list[int]] = {}
        for sid, seg in sorted(segments.items()):
            name = seg.custom_name
            if not name:
                en = SEGMENT_TYPE_CODE_TO_NAME.get(seg.type) or seg.name or "Room"
                name = type_zh.get(en.lower().replace(" ", "_"), seg.name)
            if name in rooms:
                name = f"{name}{sid}"
            rooms[name] = sid
            if seg.x is not None and seg.y is not None:
                centers[name] = [int(seg.x), int(seg.y)]  # goto 用的房间中心点
        data = {
            "rooms": rooms,
            "centers": centers,
            "customized_cleaning": bool(dev.capability.customized_cleaning),
            "capability": {
                k: bool(getattr(dev.capability, k, False))
                for k in ("self_wash_base", "mop_pad_lifting", "mopping_after_sweeping", "cleangenius", "cruising")
            },
            "model": dev.info.model if dev.info else cfg.get("model"),
            "at": dt.datetime.now(TZ).isoformat(timespec="seconds"),
        }
        save_json(ROOMS_PATH, data)
        return data
    finally:
        key = dev._protocol.cloud.auth_key if dev._protocol.cloud else None
        if key and key != cfg["auth_key"]:
            cfg["auth_key"] = key  # 续期会换新 token，旧的随即失效，必须存下
            save_json(CFG_PATH, cfg)
        dev.disconnect()


def cmd_rooms(args) -> dict:
    if "--refresh" in args:
        data = refresh_rooms()
        return {"ok": True, "step": "rooms", "refreshed": True, "rooms": list(data["rooms"])}
    return {"ok": True, "step": "rooms", "rooms": list(load_rooms())}


COMMANDS = {
    "setup": cmd_setup,
    "status": cmd_status,
    "start": cmd_start,
    "clean": cmd_clean,
    "pause": simple("pause"),
    "stop": simple("stop"),
    "dock": simple("dock"),
    "rooms": cmd_rooms,
    "mode": cmd_mode,
    "goto": cmd_goto,
}


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] not in COMMANDS:
        print(json.dumps({"ok": False, "error": "用法：vacuum.py " + "|".join(COMMANDS)}, ensure_ascii=False))
        sys.exit(2)
    try:
        out = COMMANDS[args[0]](args)
    except VacuumError as e:
        out = {"ok": False, "step": args[0], "error": str(e)}
    except Exception as e:  # noqa: BLE001 —— 协议包的异常统一转成一句话
        out = {"ok": False, "step": args[0], "error": f"{type(e).__name__}: {str(e)[:200]}"}
    print(json.dumps(out, ensure_ascii=False), flush=True)
    os._exit(0)  # 协议包可能留着后台线程，打印完直接退出


if __name__ == "__main__":
    main()
