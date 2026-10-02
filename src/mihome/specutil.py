"""spec_name 解析、取值校验、spec 文本渲染。对齐 miloco-cli 的设备命令语义。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mihome._miot.spec import MIoTSpecDeviceLite, MIoTSpecTypeLevel
from mihome.errors import ValidationError

_BOOL_TRUE = {"true", "1", "on", "yes"}
_BOOL_FALSE = {"false", "0", "off", "no"}
_ANNOTATION_RE = re.compile(r"\([^)]*\)\s*$")
_KEY_BARE_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
_KEY_DESC_RE = re.compile(r"^([a-z0-9][a-z0-9.-]*)@([^\s|,:=@]+)$")
_IID_RE = re.compile(r"^(prop|action)\.(\d+)\.(\d+)$")
_SHORT_IID_RE = re.compile(r"^(\d+)\.(\d+)$")
_DID_RE = re.compile(r"^(\d+|proxy\.\d+\.\d+|blt\.[a-z0-9.]+)$", re.I)
_DESC_FORBID_RE = re.compile(r"[\s|,:=@]+")

ACCESS_LEGEND = "w=可写 / r=可读 / n=设备主动推送 / x=动作（可组合，如 wrn；- = 都不是）"


def infer_value(raw: str):
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def value_matches(want, got) -> bool:
    """回读校验用：bool 按真假比，数字按数值比，其它按字符串比。读不到（None）算没到位。"""
    if got is None:
        return False
    if isinstance(want, bool):
        try:
            return normalize_bool(got) == want
        except ValidationError:
            return False
    if isinstance(want, (int, float)) and not isinstance(got, bool):
        try:
            return float(got) == float(want)
        except (TypeError, ValueError):
            return False
    return str(got) == str(want)


def normalize_bool(raw):
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in _BOOL_TRUE:
            return True
        if low in _BOOL_FALSE:
            return False
    return raw


def normalize_desc(desc: str | None) -> str:
    if not desc:
        return ""
    return _DESC_FORBID_RE.sub("_", desc.strip()).strip("_")


def is_did(token: str) -> bool:
    return bool(_DID_RE.match(token))


def parse_prop_groups(args: tuple[str, ...]) -> list[tuple[str, list[str]]]:
    """把 `DID IID [IID...] [DID IID...]` 拆成 [(did, [iid...]), ...]。"""
    if not args:
        raise ValidationError("provide <did> [iid...]")
    groups: list[tuple[str, list[str]]] = []
    current_did: str | None = None
    current_specs: list[str] = []
    for tok in args:
        if is_did(tok):
            if current_did is not None:
                groups.append((current_did, current_specs))
            current_did = tok
            current_specs = []
        else:
            if current_did is None:
                raise ValidationError(f"first argument must be a did, got '{tok}'")
            current_specs.append(tok)
    if current_did is not None:
        groups.append((current_did, current_specs))
    return groups


def _looks_like_iid(tok: str) -> bool:
    return bool(_IID_RE.match(tok) or _SHORT_IID_RE.match(tok))


def parse_control_groups(args: tuple[str, ...]) -> list[tuple[str, list[tuple[str, str]]]]:
    """把 `DID IID VALUE [IID VALUE...] [DID IID VALUE...]` 拆成 [(did, [(iid, value)...]), ...]。

    按位置切分，不靠「像不像 did」：数字值（如开度 50）本身也满足 did 的格式。
    did 后面必是 iid，iid 后面必是值；值后面的词像 iid 就接着算同一台，否则是下一台的 did。
    """
    if not args:
        raise ValidationError("provide <did> <iid> <value> [...]")
    groups: list[tuple[str, list[tuple[str, str]]]] = []
    i = 0
    while i < len(args):
        did = args[i]
        if not is_did(did):
            raise ValidationError(f"'{did}' 不是 did。用法：<did> <iid> <value> [<iid> <value>] [<did> ...]")
        i += 1
        pairs: list[tuple[str, str]] = []
        while i < len(args) and (not pairs or _looks_like_iid(args[i])):
            iid = args[i]
            if not _looks_like_iid(iid):
                raise ValidationError(f"did {did} 后面应该是 iid（如 2.1），拿到 '{iid}'")
            if i + 1 >= len(args):
                raise ValidationError(f"did {did} 的 {iid} 缺少值")
            pairs.append((iid, args[i + 1]))
            i += 2
        if not pairs:
            raise ValidationError(f"did {did} 缺少 <iid> <value>")
        groups.append((did, pairs))
    return groups


def parse_iid(iid: str) -> tuple[str, int, int]:
    """`2.1` → prop.2.1；`prop.2.1` / `action.5.1` 原样。不查设备、不读 spec。"""
    raw = (iid or "").strip()
    m = _IID_RE.match(raw)
    if m:
        return m.group(1), int(m.group(2)), int(m.group(3))
    m = _SHORT_IID_RE.match(raw)
    if m:
        return "prop", int(m.group(1)), int(m.group(2))
    raise ValidationError(
        f"invalid iid '{iid}', expected siid.piid (e.g. 2.1) or prop.s.p / action.s.a"
    )


def lookup_iid_by_key(key: str, spec: dict) -> str:
    """把 control/props/action 第二参解析成 iid。"""
    key = _ANNOTATION_RE.sub("", key)
    if _IID_RE.match(key):
        return key

    m_desc = _KEY_DESC_RE.match(key)
    if m_desc:
        type_name, desc_norm = m_desc.group(1), m_desc.group(2)
    elif _KEY_BARE_RE.match(key):
        type_name, desc_norm = key, None
    else:
        raise ValidationError(
            f"unrecognized spec_name '{key}'. Expected a spec_name, "
            "spec_name@<子设备描述>, or prop.s.p / action.s.p."
        )

    candidates: list[tuple[str, dict]] = []
    for iid, entry in _iter_spec_entries(spec):
        if entry.get("type_name") != type_name:
            continue
        if desc_norm is not None and (
            normalize_desc(entry.get("service_description")) != desc_norm
        ):
            continue
        candidates.append((iid, entry))

    if len(candidates) == 1:
        return candidates[0][0]

    if len(candidates) > 1:
        lines = [
            f"  {iid} ({entry.get('service_description') or entry.get('description') or ''})"
            for iid, entry in candidates
        ]
        raise ValidationError(
            f"spec_name '{key}' matches {len(candidates)} iids, "
            f"use spec_name@<子设备描述> or prop.s.p / action.s.p to disambiguate:\n"
            + "\n".join(lines)
        )

    if desc_norm is not None:
        raise ValidationError(
            f"spec_name '{key}' not matched. Run `device spec` to see available spec_names."
        )
    if not spec:
        raise ValidationError(
            f"spec_name '{key}' cannot be resolved: spec empty."
        )
    if not any(e.get("type_name") for _, e in _iter_spec_entries(spec)):
        raise ValidationError(
            f"spec_name '{key}' cannot be resolved: device spec has no named entries."
        )
    raise ValidationError(
        f"spec_name '{key}' not found. Run `device spec` to see available spec_names."
    )


def validate_value(iid_spec: dict, value) -> None:
    value_list = iid_spec.get("value_list")
    if isinstance(value_list, list) and value_list:
        allowed = [it for it in value_list if isinstance(it, dict)]
        if value not in {it.get("value") for it in allowed}:
            opts = ", ".join(f"{it.get('name')}={it.get('value')}" for it in allowed)
            raise ValidationError(f"value {value!r} is not a valid enum; allowed: {opts}")
        return

    value_range = iid_spec.get("value_range")
    if not value_range or len(value_range) < 2 or not isinstance(value, (int, float)):
        return
    lo, hi = value_range[0], value_range[1]
    if not (lo <= value <= hi):
        step = value_range[2] if len(value_range) >= 3 else None
        rng = f"[{lo},{hi}" + (f";{step}" if step is not None else "") + "]"
        unit = iid_spec.get("unit")
        suffix = f" {unit}" if unit else ""
        raise ValidationError(f"value {value} out of range {rng}{suffix}")


def spec_entry(spec: dict, iid: str) -> dict:
    if iid not in spec:
        raise ValidationError(f"iid '{iid}' not in spec")
    entry = spec[iid]
    return entry if isinstance(entry, dict) else {}


def resolve_keys_for_device(spec: dict) -> dict[str, str]:
    """同设备内同 type_name 冲突时加 @service_description。"""
    if not isinstance(spec, dict):
        return {}
    counts: dict[str, int] = {}
    for _, entry in _iter_spec_entries(spec):
        type_name = entry.get("type_name")
        if type_name:
            counts[type_name] = counts.get(type_name, 0) + 1

    result: dict[str, str] = {}
    desc_used: dict[str, int] = {}
    for iid, entry in _iter_spec_entries(spec):
        type_name = entry.get("type_name")
        if not type_name:
            continue
        if counts.get(type_name, 0) <= 1:
            result[iid] = type_name
            continue
        normalized_desc = normalize_desc(entry.get("service_description"))
        if normalized_desc:
            key = f"{type_name}@{normalized_desc}"
            desc_used[key] = desc_used.get(key, 0) + 1
            result[iid] = key
        else:
            result[iid] = iid

    for iid, key in list(result.items()):
        if "@" in key and desc_used.get(key, 0) > 1:
            result[iid] = iid
    return result


def lite_to_spec_dict(spec_lite: dict[str, MIoTSpecDeviceLite]) -> dict:
    """parse_lite_async 的 prop.0.s.p → CLI 用的 prop.s.p。"""
    spec: dict = {}
    for full_iid, item in spec_lite.items():
        parts = full_iid.split(".")
        if len(parts) != 4:
            continue
        short_iid = f"{parts[0]}.{parts[2]}.{parts[3]}"
        entry: dict = {
            "description": item.description,
            "format": item.format,
            "writeable": item.writeable,
            "readable": item.readable,
            "notify": item.notify,
        }
        if item.unit:
            entry["unit"] = item.unit
        if item.value_range:
            entry["value_range"] = [
                item.value_range.min_,
                item.value_range.max_,
                item.value_range.step,
            ]
        if item.value_list:
            entry["value_list"] = [
                {"name": v.name, "value": v.value, "description": v.description}
                for v in item.value_list
            ]
        if item.type_name:
            entry["type_name"] = item.type_name
        if item.service_type_name:
            entry["service_type_name"] = item.service_type_name
        if item.service_description:
            entry["service_description"] = item.service_description
        if item.in_params:
            entry["in_params"] = [
                {"name": p.name, "format": p.format} for p in item.in_params
            ]
        if item.prop_description:
            entry["prop_description"] = item.prop_description
        spec[short_iid] = entry
    return spec


def category_of(urn: str | None) -> str:
    if not urn:
        return ""
    parts = urn.split(":")
    if len(parts) >= 4 and parts[2] == "device":
        return parts[3]
    return ""


def render_device_spec(dev: dict) -> str:
    spec = dev.get("spec") or {}
    head = [f"did={dev.get('did')}", f"device_name={dev.get('name') or '-'}"]
    if dev.get("home"):
        head.append(f"home={dev['home']}")
    if dev.get("room"):
        head.append(f"room={dev['room']}")
    if dev.get("category"):
        head.append(f"category={dev['category']}")
    head.append("online" if dev.get("online") else "offline")
    out = ["  ".join(head)]
    if not spec:
        out.append("  (spec 为空)")
        return "\n".join(out)

    out.append("")
    out.append("# 按 service 分组")
    out.append("# prop.iid    spec_name|access|format|constraint|unit")
    out.append("# action.iid  spec_name|x|in_params")
    out.append(f"# access：{ACCESS_LEGEND}")

    iid_to_key = resolve_keys_for_device(spec)
    services: dict[str, dict] = {}
    for iid, entry in spec.items():
        if not isinstance(entry, dict):
            continue
        parts = iid.split(".")
        siid = parts[1] if len(parts) >= 3 else "?"
        svc = services.setdefault(
            siid, {"type_name": None, "description": None, "props": [], "actions": []}
        )
        if svc["type_name"] is None and entry.get("service_type_name"):
            svc["type_name"] = entry["service_type_name"]
        if svc["description"] is None and entry.get("service_description"):
            svc["description"] = entry["service_description"]
        key = iid_to_key.get(iid) or iid
        line = _build_spec_line(iid, entry, key)
        bucket = svc["actions"] if line.is_action else svc["props"]
        bucket.append((iid, line.render()))

    for siid, svc in services.items():
        out.append("")
        header = f"[service {siid}] {svc['type_name'] or '-'}"
        if svc["description"]:
            header += f"（{svc['description']}）"
        out.append(header)
        rows = svc["props"] + svc["actions"]
        if not rows:
            continue
        width = max(len(iid) for iid, _ in rows)
        for iid, rendered in rows:
            out.append(f"{iid:<{width}}  {rendered}")
    return "\n".join(out)


PARSE_LITE_LEVELS = dict(
    spec_service_level=MIoTSpecTypeLevel.UNKNOWN,
    spec_property_level=MIoTSpecTypeLevel.UNKNOWN,
    spec_action_level=MIoTSpecTypeLevel.UNKNOWN,
)


def _iter_spec_entries(spec: dict):
    if not isinstance(spec, dict):
        return
    for iid, entry in spec.items():
        if isinstance(entry, dict):
            yield iid, entry


@dataclass
class SpecLine:
    key: str
    fmt: str
    wr: str
    extra: str
    unit: str
    is_action: bool
    annotation: str = ""

    def render(self) -> str:
        if self.is_action:
            line = f"{self.key}|{self.wr}|{self.extra}" if self.extra else f"{self.key}|{self.wr}"
        else:
            parts = [self.key, self.wr, self.fmt]
            if self.extra or self.unit:
                parts.append(self.extra)
            if self.unit:
                parts.append(self.unit)
            line = "|".join(parts)
        if self.annotation:
            line = f"{line}  # {self.annotation}"
        return line


def _access_of(entry: dict) -> str:
    bits = ""
    if entry.get("writeable"):
        bits += "w"
    if entry.get("readable"):
        bits += "r"
    if entry.get("notify"):
        bits += "n"
    return bits or "-"


def _format_extra(entry: dict) -> str:
    value_range = entry.get("value_range")
    value_list = entry.get("value_list")
    in_params = entry.get("in_params")
    if value_range and isinstance(value_range, list) and len(value_range) >= 2:
        lo, hi = value_range[0], value_range[1]
        step = value_range[2] if len(value_range) >= 3 else None
        if step is None:
            return f"[{_num_str(lo)},{_num_str(hi)}]"
        return f"[{_num_str(lo)},{_num_str(hi)};{_num_str(step)}]"
    if value_list and isinstance(value_list, list):
        items = []
        for v in value_list:
            if not isinstance(v, dict):
                continue
            name = str(v.get("name", "")).replace("|", "").replace(",", "").replace(":", "")
            val = v.get("value")
            items.append(f"{name}={_num_str(val) if isinstance(val, (int, float)) else val}")
        if items:
            return ",".join(items)
    if in_params and isinstance(in_params, list):
        items = []
        for p in in_params:
            if not isinstance(p, dict):
                continue
            name = (
                str(p.get("name", ""))
                .replace("|", "")
                .replace(",", "")
                .replace(":", "")
                .replace("=", "")
            )
            fmt = str(p.get("format", "")).replace("|", "").replace(",", "")
            items.append(f"{name}:{fmt}")
        if items:
            return ",".join(items)
    return ""


def _num_str(n) -> str:
    if isinstance(n, bool):
        return "true" if n else "false"
    if isinstance(n, int):
        return str(n)
    if isinstance(n, float):
        if n.is_integer():
            return str(int(n))
        return repr(n).rstrip("0").rstrip(".") or "0"
    return str(n)


def _build_spec_line(iid: str, entry: dict, key: str) -> SpecLine:
    is_action = iid.startswith("action.")
    annotation = _build_annotation(iid, entry, key)
    if is_action:
        return SpecLine(
            key=key, fmt="", wr="x", extra=_format_extra(entry),
            unit="", is_action=True, annotation=annotation,
        )
    unit = str(entry["unit"]).replace("|", "").replace(",", "") if entry.get("unit") else ""
    return SpecLine(
        key=key,
        fmt=str(entry.get("format", "") or ""),
        wr=_access_of(entry),
        extra=_format_extra(entry),
        unit=unit,
        is_action=False,
        annotation=annotation,
    )


def _build_annotation(iid: str, entry: dict, key: str) -> str:
    desc = str(entry.get("description") or "")
    svc_desc = str(entry.get("service_description") or "")
    if key == iid:
        return _clean_annotation(desc) if desc else ""
    type_name = key.split("@")[0] if "@" in key else key
    prop_desc_en = str(entry.get("prop_description") or "")
    normalized_en = prop_desc_en.strip().lower().replace(" ", "-").replace("_", "-")
    if normalized_en == type_name:
        return ""
    if svc_desc and desc.startswith(svc_desc):
        type_desc = desc[len(svc_desc):].strip()
        if type_desc:
            return _clean_annotation(type_desc)
    return ""


def _clean_annotation(s: str) -> str:
    s = s.replace("|", "").replace("(", "").replace(")", "")
    if len(s) > 20:
        s = s[:20] + "…"
    return s
