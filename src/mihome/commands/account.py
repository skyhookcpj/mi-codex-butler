"""account：bind / authorize / status / unbind。"""

from __future__ import annotations

import base64
import binascii
import json

import click

from mihome.config import load_config, save_config
from mihome.errors import run_async
from mihome.output import print_result
from mihome.session import CloudSession


def _parse_auth_payload(payload: str) -> tuple[str, str]:
    payload = payload.strip()
    if not payload:
        raise click.ClickException("授权信息为空。")
    try:
        decoded = base64.b64decode(payload).decode("utf-8")
        data = json.loads(decoded)
        code = data["code"].strip()
        state = data["state"].strip()
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, KeyError, AttributeError):
        raise click.ClickException("授权信息格式错误，请从回调页面直接复制。")
    if not code or not state:
        raise click.ClickException("授权信息中 code 或 state 为空。")
    return code, state


@click.group("account")
def account_group():
    """小米账号：绑定 / 解绑 / 状态。"""


@account_group.command("bind")
@click.option("--no-wait", is_flag=True, help="只打印授权 URL 后退出。")
@click.option("--pretty", is_flag=True)
@run_async
async def account_bind(no_wait: bool, pretty: bool) -> None:
    """绑定小米账号：打开链接授权，把回调页上的码贴回来。"""
    async with CloudSession(need_auth=False) as session:
        url = session.auth_url()
        click.echo(f"\n请在浏览器中打开以下链接完成小米账号授权：\n\n  {url}\n")
        if no_wait:
            print_result({"oauth_url": url, "state": session.oauth_state()}, pretty)
            click.echo("授权完成后运行：")
            click.echo("  mihome account authorize <授权码>")
            return
        payload = click.prompt("授权完成后，从页面复制授权码并粘贴到此处", type=str)
        code, state = _parse_auth_payload(payload)
        await _finish_authorize(session, code, state, pretty)


@account_group.command("authorize")
@click.argument("payload")
@click.option("--pretty", is_flag=True)
@run_async
async def account_authorize(payload: str, pretty: bool) -> None:
    """提交回调页复制的授权码，完成绑定。"""
    code, state = _parse_auth_payload(payload)
    async with CloudSession(need_auth=False) as session:
        await _finish_authorize(session, code, state, pretty)


async def _finish_authorize(session: CloudSession, code: str, state: str, pretty: bool) -> None:
    info = await session.exchange_code(code, state)
    user = info.user_info
    result = {
        "code": 0,
        "message": "账号绑定成功",
        "user": {
            "uid": user.uid if user else None,
            "nickname": user.nickname if user else None,
        },
        "home_id": session.cfg.get("home_id"),
    }
    print_result(result, pretty)
    home_id = session.cfg.get("home_id")
    if home_id:
        homes = session._homes or {}
        name = homes[home_id].home_name if home_id in homes else home_id
        click.echo(f"\n已启用家庭：{name}")
    else:
        click.echo("\n如有多个家庭，请运行 mihome home list 后 mihome home use <home_id>")


@account_group.command("status")
@click.option("--pretty", is_flag=True)
@click.option("--check", is_flag=True, help="向云端确认 token 仍然有效。")
@run_async
async def account_status(pretty: bool, check: bool) -> None:
    """查看绑定状态。"""
    cfg = load_config()
    oauth = cfg.get("oauth") or {}
    bound = bool(oauth.get("access_token"))
    data = {
        "bound": bound,
        "cloud_server": cfg.get("cloud_server") or "cn",
        "user": cfg.get("user"),
        "home_id": cfg.get("home_id"),
        "expires_ts": oauth.get("expires_ts") if bound else None,
    }
    if bound and check:
        async with CloudSession(need_auth=True) as session:
            user = await session.user_info()
            data["user"] = {
                "uid": user.uid,
                "nickname": user.nickname,
                "union_id": user.union_id,
            }
            data["token_ok"] = True
    print_result(data, pretty)


@account_group.command("unbind")
@click.option("--pretty", is_flag=True)
def account_unbind(pretty: bool) -> None:
    """清除本机 token，不再代表这个小米账号发指令。"""
    cfg = load_config()
    cfg["oauth"] = None
    cfg["user"] = None
    cfg["home_id"] = None
    save_config(cfg)
    print_result({"code": 0, "message": "已解绑"}, pretty)
