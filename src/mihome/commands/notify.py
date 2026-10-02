"""notify：米家 App 推送。"""

from __future__ import annotations

import click

from mihome.errors import run_async
from mihome.output import print_result
from mihome.session import CloudSession


@click.group("notify")
def notify_group():
    """米家 App 通知。"""


@notify_group.command("push")
@click.option("--text", required=True, help="推送文案")
@click.option("--pretty", is_flag=True)
@run_async
async def notify_push(text: str, pretty: bool) -> None:
    """往绑定账号的米家 App 推一条通知。"""
    async with CloudSession() as session:
        notify_id = await session.notify_push(text)
        print_result({"code": 0, "message": "已推送", "notify_id": notify_id}, pretty)
