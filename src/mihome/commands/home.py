"""home：list / use。"""

from __future__ import annotations

import click

from mihome.errors import run_async
from mihome.output import escape_field, print_result
from mihome.session import CloudSession


@click.group("home")
def home_group():
    """家庭：列表 / 选定当前家庭。"""


@home_group.command("list")
@run_async
async def home_list() -> None:
    """列出账号下的家庭。"""
    async with CloudSession() as session:
        homes = await session.list_homes()
        current = session.cfg.get("home_id")
        click.echo("# home_id|home_name|share|current")
        for home in homes.values():
            flag = "yes" if home.home_id == current else ""
            share = "share" if home.share_home else "own"
            click.echo(
                "|".join(
                    [
                        escape_field(home.home_id),
                        escape_field(home.home_name),
                        share,
                        flag,
                    ]
                )
            )


@home_group.command("use")
@click.argument("home_id")
@click.option("--pretty", is_flag=True)
@run_async
async def home_use(home_id: str, pretty: bool) -> None:
    """选定当前家庭。之后的设备、场景、控制都只打这一家。"""
    async with CloudSession() as session:
        home = await session.set_home(home_id)
        print_result(
            {
                "code": 0,
                "message": f"已切换到 {home.home_name}",
                "home_id": home.home_id,
                "home_name": home.home_name,
            },
            pretty,
        )
