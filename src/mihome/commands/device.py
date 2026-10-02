"""device：list / spec / control / props / action。"""

from __future__ import annotations

import asyncio
import json
import sys

import click

from mihome._miot.types import MIoTActionParam, MIoTGetPropertyParam, MIoTSetPropertyParam
from mihome.codes import annotate_result_codes
from mihome.errors import ValidationError, run_async
from mihome.output import escape_field, print_result
from mihome.session import CloudSession
from mihome.specutil import (
    infer_value,
    parse_control_groups,
    parse_iid,
    parse_prop_groups,
    render_device_spec,
    value_matches,
)


@click.group("device")
def device_group():
    """设备：列表 / 规格 / 控制 / 查询 / 动作。"""


@device_group.command("list")
@click.option("--room", default=None, help="按房间过滤")
@click.option("--category", default=None, help="按设备类型过滤")
@click.option("--online", is_flag=True, default=False, help="只返回在线设备")
@run_async
async def device_list(room: str | None, category: str | None, online: bool) -> None:
    """列出当前家庭的设备。

    顶部 # home= 声明家庭名，后跟 TSV：did|device_name|room|category|online
    """
    async with CloudSession() as session:
        home = await session.current_home()
        devices = await session.list_devices()
        rows = []
        for dev in devices:
            row = session.device_row(dev)
            if room and row.get("room") != room:
                continue
            if category and row.get("category") != category:
                continue
            if online and not row.get("online"):
                continue
            rows.append(row)
        click.echo(f"# home={home.home_name}")
        click.echo("# did|device_name|room|category|online")
        for row in rows:
            click.echo(
                "|".join(
                    [
                        escape_field(row["did"]),
                        escape_field(row["name"]),
                        escape_field(row["room"]),
                        escape_field(row["category"]),
                        "online" if row["online"] else "offline",
                    ]
                )
            )


@device_group.command("spec")
@click.argument("dids", nargs=-1, required=True)
@run_async
async def device_spec(dids: tuple[str, ...]) -> None:
    """查看属性和动作规格。缺 iid 时用这个，把输出里的 prop.s.p / action.s.a 抄给 control / props / action。"""
    async with CloudSession() as session:
        blocks: list[str] = []
        for did in dids:
            try:
                device = await session.get_device(did)
            except ValidationError as exc:
                print(json.dumps({"error": str(exc)}), file=sys.stderr)
                continue
            spec = await session.spec_for(device)
            row = session.device_row(device)
            row["spec"] = spec
            blocks.append(render_device_spec(row))
        if blocks:
            click.echo("\n\n\n".join(blocks))
        else:
            sys.exit(1)


VERIFY_DELAY_S = 1.0


@device_group.command("control")
@click.argument("args", nargs=-1, required=True)
@click.option(
    "--set",
    "sets",
    nargs=2,
    multiple=True,
    metavar="IID VALUE",
    help="同一台设备设多个属性，可重复。例：--set 2.4 26 --set 2.1 true",
)
@click.option(
    "--verify",
    is_flag=True,
    help="设完约 1s 回读，没到位的补发一次；仍不到位则退出码 3。适合灯、开关这类可回读的属性",
)
@click.option("--pretty", is_flag=True)
@run_async
async def device_control(args, sets, verify, pretty) -> None:
    """控制设备属性。一条命令可控多台，一次云端请求发出。

    \b
    单属性：mihome device control <did> <iid> <value>
    多台：  mihome device control <did> <iid> <value> [<iid> <value>] [<did> <iid> <value> ...]
    单台多属性也可：mihome device control <did> --set 2.4 26 --set 2.1 true
    iid 为 siid.piid，如 2.1。
    """
    try:
        if sets:
            if len(args) != 1:
                raise ValidationError("--set 只能配一个 did：mihome device control <did> --set IID VALUE ...")
            groups = [(args[0], list(sets))]
        else:
            groups = parse_control_groups(args)
        params = _set_params(groups)
    except ValidationError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

    async with CloudSession() as session:
        results = await session.set_props(params)
        inner: dict = {"results": results}
        if len(groups) == 1:
            inner = {"did": groups[0][0], **inner}
        data = {"code": 0, "message": "ok", "data": inner}
        annotate_result_codes(data)
        if verify and data.get("code", 0) == 0:
            checks = await _verify(session, params)
            inner["verify"] = checks
            missed = [f"{c['did']} {c['iid']}" for c in checks if not c["ok"]]
            if missed:
                data["code"] = 3
                data["message"] = f"没到位（{len(missed)}/{len(checks)}）：{'、'.join(missed)}"
        print_result(data, pretty)
        if data.get("code", 0) != 0:
            sys.exit(3)


def _set_params(groups: list[tuple[str, list[tuple[str, str]]]]) -> list[MIoTSetPropertyParam]:
    params: list[MIoTSetPropertyParam] = []
    for did, pairs in groups:
        for iid, raw in pairs:
            kind, siid, piid = parse_iid(iid)
            if kind != "prop":
                raise ValidationError(
                    f"'{iid}' is an action, not a property; 请改用：mihome device action {did} {iid} [值...]"
                )
            params.append(MIoTSetPropertyParam(did=did, siid=siid, piid=piid, value=infer_value(raw)))
    return params


async def _verify(session: CloudSession, params: list[MIoTSetPropertyParam]) -> list[dict]:
    """设属性只返回「云端已受理」（code 1），设备异步执行，偶尔会丢。回读一次，没到位的补发一次。"""

    async def read(ps: list[MIoTSetPropertyParam]) -> dict[tuple[str, int, int], object]:
        raw = await session.get_props([MIoTGetPropertyParam(did=p.did, siid=p.siid, piid=p.piid) for p in ps])
        return {
            (str(x.get("did")), int(x.get("siid", -1)), int(x.get("piid", -1))): x.get("value")
            for x in raw
            if isinstance(x, dict)
        }

    def key(p: MIoTSetPropertyParam) -> tuple[str, int, int]:
        return (p.did, p.siid, p.piid)

    await asyncio.sleep(VERIFY_DELAY_S)
    got = await read(params)
    missed = [p for p in params if not value_matches(p.value, got.get(key(p)))]
    retried = {key(p) for p in missed}
    if missed:
        await session.set_props(missed)
        await asyncio.sleep(VERIFY_DELAY_S)
        got.update(await read(missed))
    return [
        {
            "did": p.did,
            "iid": f"{p.siid}.{p.piid}",
            "want": p.value,
            "got": got.get(key(p)),
            "ok": value_matches(p.value, got.get(key(p))),
            **({"retried": True} if key(p) in retried else {}),
        }
        for p in params
    ]


@device_group.command("props")
@click.argument("args", nargs=-1, required=True)
@click.option("--pretty", is_flag=True)
@run_async
async def device_props(args, pretty) -> None:
    """查询属性。`mihome device props <did> <iid> [<did> <iid> ...]`。

    iid 为 siid.piid（如 2.1）。可在一条命令里查多台。
    """
    try:
        groups = parse_prop_groups(args)
        for did, specs in groups:
            if not specs:
                raise ValidationError(
                    f"did {did} 缺少 iid。例：mihome device props {did} 2.1"
                )
    except ValidationError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

    params: list[MIoTGetPropertyParam] = []
    spec_by_did_iid: dict[tuple[str, str], str] = {}
    try:
        for did, specs in groups:
            for key in specs:
                kind, siid, piid = parse_iid(key)
                if kind != "prop":
                    raise ValidationError(f"'{key}' is an action, use device action")
                iid = f"prop.{siid}.{piid}"
                spec_by_did_iid[(did, iid)] = key
                params.append(MIoTGetPropertyParam(did=did, siid=siid, piid=piid))
    except ValidationError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

    async with CloudSession() as session:
        raw = await session.get_props(params)
        properties = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            siid = item.get("siid")
            piid = item.get("piid")
            iid = f"prop.{siid}.{piid}" if siid is not None and piid is not None else ""
            row = dict(item)
            row["iid"] = iid
            row["spec_name"] = spec_by_did_iid.get((str(item.get("did") or ""), iid), iid)
            properties.append(row)
        data = {
            "code": 0,
            "message": "ok",
            "data": {"properties": properties},
        }
        if len(groups) == 1:
            data["data"]["did"] = groups[0][0]
        annotate_result_codes(data)
        print_result(data, pretty)
        if data.get("code", 0) != 0:
            sys.exit(3)


@device_group.command("action")
@click.argument("did")
@click.argument("iid")
@click.argument("params", nargs=-1)
@click.option("--pretty", is_flag=True)
@run_async
async def device_action(did, iid, params, pretty) -> None:
    """调用设备动作。iid 用 action.s.a。"""
    try:
        kind, siid, aiid = parse_iid(iid)
        if kind != "action":
            raise ValidationError(
                f"'{iid}' is a property, not an action; 请改用 mihome device control"
            )
    except ValidationError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

    async with CloudSession() as session:
        result = await session.call_action(
            MIoTActionParam(
                did=did,
                siid=siid,
                aiid=aiid,
                in_=[infer_value(p) for p in params],
            )
        )
        data = {"code": 0, "message": "ok", "data": {"did": did, "result": result}}
        annotate_result_codes(data)
        print_result(data, pretty)
        if data.get("code", 0) != 0:
            sys.exit(3)
