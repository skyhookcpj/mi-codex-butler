"""scene：list / trigger。"""

from __future__ import annotations

import click

from mihome.errors import run_async
from mihome.output import escape_field, print_result
from mihome.session import CloudSession


@click.group("scene")
def scene_group():
    """米家手动场景：列表 / 触发。"""


@scene_group.command("list")
@run_async
async def scene_list() -> None:
    """列出当前家庭的手动场景。"""
    async with CloudSession() as session:
        scenes = await session.list_scenes()
        click.echo("# scene_id|scene_name")
        for scene in scenes:
            click.echo(
                "|".join(
                    [escape_field(scene.scene_id), escape_field(scene.scene_name)]
                )
            )


@scene_group.command("trigger")
@click.argument("scene_id")
@click.option("--pretty", is_flag=True)
@run_async
async def scene_trigger(scene_id: str, pretty: bool) -> None:
    """触发指定场景。"""
    async with CloudSession() as session:
        ok = await session.trigger_scene(scene_id)
        data = {
            "code": 0 if ok else 3,
            "message": "已触发" if ok else "触发失败",
            "scene_id": scene_id,
        }
        print_result(data, pretty)
        if not ok:
            raise SystemExit(3)
