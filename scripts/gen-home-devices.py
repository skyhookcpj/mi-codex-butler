#!/usr/bin/env python3
"""生成 home-devices skill 的初稿：读 mihome 的设备列表和规格，按房间列出设备和常用属性。

    python3 scripts/gen-home-devices.py > ~/mihome-workspace/.codex/skills/home-devices/SKILL.md
    python3 scripts/gen-home-devices.py --category light,switch   # 只要灯和开关

生成的是初稿：改成家里人平时的叫法，删掉不想让管家碰的设备，补上「没点名时怎么做」这类约定。
门锁、摄像头、燃气阀这类设备默认不收录。
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys

DEFAULT_CATEGORIES = ["light", "switch", "curtain", "air-conditioner"]
# 每类设备收录哪些属性（spec_name 去掉 @ 后面的部分）
KEEP = {
    "light": ["on", "brightness", "color-temperature"],
    "switch": ["on"],
    "curtain": ["motor-control", "current-position", "target-position"],
    "air-conditioner": ["on", "mode", "target-temperature", "fan-level"],
}
PROP = re.compile(r"^prop\.(\d+\.\d+)\s+([^|]+)\|([^|]*)\|([^|#]*)(?:\|([^|#]*))?(?:\|[^#]*)?(?:#\s*(.*))?$")


def mihome(*args: str) -> str:
    p = subprocess.run(["mihome", *args], capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        sys.exit(f"mihome {' '.join(args[:2])} 失败：{p.stderr.strip()[-300:]}")
    return p.stdout


def list_devices(categories: list[str]) -> tuple[str, list[dict]]:
    home, rows = "", []
    for line in mihome("device", "list").splitlines():
        if line.startswith("# home="):
            home = line[len("# home=") :]
        if not line or line.startswith("#"):
            continue
        did, name, room, category, online = re.split(r"(?<!\\)\|", line)[:5]
        if category in categories:
            rows.append({"did": did, "name": name.replace("\\|", "|"), "room": room or "未分配房间", "category": category})
    return home, rows


def spec_props(dids: list[str]) -> dict[str, list[tuple[str, str, str, str]]]:
    """did → [(iid, spec_name, constraint, 说明)]"""
    out: dict[str, list] = {}
    did = None
    for line in mihome("device", "spec", *dids).splitlines():
        if line.startswith("did="):
            did = line.split()[0][len("did=") :]
            out[did] = []
        elif did and (m := PROP.match(line.strip())):
            iid, spec_name, access, _fmt, constraint, note = m.groups()
            if "w" in access or "r" in access:
                out[did].append((iid, spec_name.strip(), (constraint or "").strip(), (note or "").strip()))
    return out


def describe(category: str, props: list[tuple[str, str, str, str]]) -> str:
    keep = KEEP.get(category, ["on"])
    cells = []
    for iid, spec_name, constraint, note in props:
        base, _, where = spec_name.partition("@")
        if base not in keep:
            continue
        label = f"{base}（{where}）" if where else base
        extra = constraint if constraint and not constraint.startswith("[") else ""
        if constraint.startswith("["):
            extra = constraint.split(";")[0].lstrip("[").replace(",", "–")
        cells.append(f"`{iid}` {label}" + (f"：{extra}" if extra else "") + (f" {note}" if note and not where else ""))
    return "<br>".join(cells) or "（没找到常用属性，用 `mihome device spec` 查）"


def render(home: str, rows: list[dict], props: dict) -> str:
    today = dt.date.today().isoformat()
    lines = [
        "---",
        "name: home-devices",
        "description: 控制家里的灯、开关、窗帘、空调。设备表在本 skill 内，按表拼 mihome 命令，不要 list。缺 iid 才 spec。",
        "---",
        "",
        "# home-devices",
        "",
        f"<!-- {today} 由 scripts/gen-home-devices.py 从「{home}」生成的初稿。"
        "把名称改成家里人平时的叫法，删掉不想让管家碰的设备，补上「没点名时怎么做」这类约定，再删掉这行。 -->",
        "",
        "只控下表设备。表里有 did/iid 就直接拼命令，不要 `device list`，不要编造。对不上就说找不到。",
        "表里缺 iid 或取值对不上时，再 `mihome device spec <did>`，从输出抄 `prop.s.p`（写成 `s.p`）。",
        "",
        "`mihome` 要访问小米云：等命令返回，不要当后台任务。用返回 JSON 的 `code` 判断成败。",
        "",
        "## 命令",
        "",
        "- 改属性：`mihome device control <did> <iid> <value> [<iid> <value>] [<did> <iid> <value> ...]`。"
        "多台设备写进**一条**命令，一次下发；灯和开关加 `--verify` 回读，没到位自动补发",
        "- 查状态：`mihome device props <did> <iid> [<did> <iid> ...]`",
        "- 开关类 `on`：true=开 false=关。带取值表的属性（如 `Close=0,Open=2`）照表写数字",
        "",
    ]
    rooms: dict[str, list[dict]] = {}
    for r in rows:
        rooms.setdefault(r["room"], []).append(r)
    for room, devs in rooms.items():
        lines += [f"## {room}", "", "| 名称 | 类型 | did | 常用属性 |", "| --- | --- | --- | --- |"]
        for d in sorted(devs, key=lambda d: (d["category"], d["name"])):
            lines.append(f"| {d['name']} | {d['category']} | {d['did']} | {describe(d['category'], props.get(d['did'], []))} |")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--category", default=",".join(DEFAULT_CATEGORIES), help="逗号分隔，默认 " + ",".join(DEFAULT_CATEGORIES))
    args = ap.parse_args()
    home, rows = list_devices([c.strip() for c in args.category.split(",") if c.strip()])
    if not rows:
        sys.exit("没找到符合类型的设备")
    print(render(home, rows, spec_props([r["did"] for r in rows])))


if __name__ == "__main__":
    main()
