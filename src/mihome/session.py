"""云端会话：OAuth token、HTTP 客户端、spec 解析。短进程，用完关掉。"""

from __future__ import annotations

import time
from typing import Any

from mihome._miot.cloud import MIoTHttpClient, MIoTOAuth2Client
from mihome._miot.const import OAUTH2_REDIRECT_URI_LIST
from mihome._miot.error import MIoTHttpError
from mihome._miot.spec import MIoTSpecParser
from mihome._miot.storage import MIoTStorage
from mihome._miot.types import (
    MIoTActionParam,
    MIoTDeviceInfo,
    MIoTGetPropertyParam,
    MIoTHomeInfo,
    MIoTManualSceneInfo,
    MIoTOauthInfo,
    MIoTSetPropertyParam,
    MIoTUserInfo,
)
from mihome.config import cache_dir, ensure_uuid, save_config
from mihome.errors import CliError, ValidationError
from mihome.specutil import PARSE_LITE_LEVELS, category_of, lite_to_spec_dict

REDIRECT_URI = OAUTH2_REDIRECT_URI_LIST[-1]


class CloudSession:
    def __init__(self, *, need_auth: bool = True) -> None:
        self.need_auth = need_auth
        self.cfg = ensure_uuid()
        self.oauth: MIoTOAuth2Client | None = None
        self.http: MIoTHttpClient | None = None
        self.spec_parser: MIoTSpecParser | None = None
        self._homes: dict[str, MIoTHomeInfo] | None = None
        self._devices: dict[str, MIoTDeviceInfo] | None = None
        self._spec_cache: dict[str, dict] = {}

    @property
    def cloud_server(self) -> str:
        return str(self.cfg.get("cloud_server") or "cn")

    @property
    def uuid(self) -> str:
        return str(self.cfg["uuid"])

    def _oauth_client(self) -> MIoTOAuth2Client:
        if self.oauth is None:
            self.oauth = MIoTOAuth2Client(
                redirect_uri=REDIRECT_URI,
                cloud_server=self.cloud_server,
                uuid=self.uuid,
            )
        return self.oauth

    def _parser(self) -> MIoTSpecParser:
        if self.spec_parser is None:
            storage = MIoTStorage(str(cache_dir()))
            self.spec_parser = MIoTSpecParser(storage=storage, lang="zh-Hans")
        return self.spec_parser

    async def __aenter__(self) -> CloudSession:
        if not self.need_auth:
            self._oauth_client()
            return self
        try:
            await self._ensure_token()
        except BaseException:
            # 刷新失败时 __aexit__ 不会被调用，自己关掉 OAuth 客户端的连接
            await self.__aexit__(None, None, None)
            raise
        oauth = self.cfg.get("oauth") or {}
        self.http = MIoTHttpClient(
            cloud_server=self.cloud_server,
            access_token=oauth["access_token"],
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self.http:
            await self.http.deinit_async()
        if self.oauth:
            await self.oauth.deinit_async()
        if self.spec_parser:
            await self.spec_parser.deinit_async()

    def auth_url(self) -> str:
        return self._oauth_client().gen_auth_url()

    def oauth_state(self) -> str:
        return self._oauth_client().state

    async def exchange_code(self, code: str, state: str) -> MIoTOauthInfo:
        assert self.oauth is not None
        if not await self.oauth.check_state_async(state):
            raise CliError("授权 state 不匹配，请重新运行 account bind", code=1)
        info = await self.oauth.get_access_token_async(code)
        self.http = MIoTHttpClient(
            cloud_server=self.cloud_server,
            access_token=info.access_token,
        )
        user = await self.http.get_user_info_async()
        info.user_info = user
        self._store_oauth(info, user)
        await self._maybe_select_single_home()
        return info

    async def list_homes(self) -> dict[str, MIoTHomeInfo]:
        http = self._http()
        if self._homes is None:
            self._homes = await http.get_homes_async(fetch_share_home=True)
        return self._homes

    async def current_home(self) -> MIoTHomeInfo:
        homes = await self.list_homes()
        if not homes:
            raise CliError("这个账号下没有家庭", code=3)
        home_id = self.cfg.get("home_id")
        if home_id and str(home_id) in homes:
            return homes[str(home_id)]
        if len(homes) == 1:
            home = next(iter(homes.values()))
            self.cfg["home_id"] = home.home_id
            save_config(self.cfg)
            return home
        names = ", ".join(f"{h.home_id} {h.home_name}" for h in homes.values())
        raise CliError(
            f"有多个家庭，请先运行 mihome home use <home_id>。可选：{names}",
            code=1,
        )

    async def set_home(self, home_id: str) -> MIoTHomeInfo:
        homes = await self.list_homes()
        if home_id not in homes:
            raise ValidationError(f"home_id '{home_id}' 不存在，先运行 mihome home list")
        self.cfg["home_id"] = home_id
        save_config(self.cfg)
        return homes[home_id]

    async def list_devices(self) -> list[MIoTDeviceInfo]:
        home = await self.current_home()
        http = self._http()
        if self._devices is None:
            self._devices = await http.get_devices_async(home_infos=[home])
        return list(self._devices.values())

    async def get_device(self, did: str) -> MIoTDeviceInfo:
        if self._devices:
            for dev in self._devices.values():
                if dev.did == did:
                    return dev
        found = await self._http().get_devices_with_dids_async([did])
        if found and did in found:
            return found[did]
        raise ValidationError(f"did '{did}' not found. Run `device list` to see available dids.")

    async def spec_for(self, device: MIoTDeviceInfo) -> dict:
        urn = device.urn
        if urn in self._spec_cache:
            return self._spec_cache[urn]
        parser = self._parser()
        lite = await parser.parse_lite_async(urn=urn, **PARSE_LITE_LEVELS)
        spec = lite_to_spec_dict(lite or {})
        self._spec_cache[urn] = spec
        return spec

    def device_row(self, device: MIoTDeviceInfo) -> dict:
        return {
            "did": device.did,
            "name": device.name,
            "home": device.home_name,
            "room": device.room_name,
            "category": category_of(device.urn),
            "online": device.online,
            "model": device.model,
        }

    async def list_scenes(self) -> list[MIoTManualSceneInfo]:
        home = await self.current_home()
        http = self._http()
        scenes = await http.get_manual_scenes_async(home_infos=[home])
        return list(scenes.values())

    async def trigger_scene(self, scene_id: str) -> bool:
        scenes = await self.list_scenes()
        target = next((s for s in scenes if str(s.scene_id) == str(scene_id)), None)
        if target is None:
            raise ValidationError(
                f"scene_id '{scene_id}' not found. Run `scene list` to see available scenes."
            )
        return await self._http().run_manual_scene_async(target)

    async def set_props(self, params: list[MIoTSetPropertyParam]) -> list:
        return await self._http().set_props_async(params)

    async def get_props(self, params: list[MIoTGetPropertyParam]) -> list:
        return await self._http().get_props_async(params)

    async def call_action(self, param: MIoTActionParam) -> dict:
        return await self._http().action_async(param)

    async def notify_push(self, text: str) -> str:
        http = self._http()
        notify_id = await http.create_app_notify_async(text)
        if not notify_id:
            raise CliError("米家拒绝了这条推送文案，请换一句话", code=3)
        ok = await http.send_app_notify_async(notify_id)
        if not ok:
            raise CliError("推送发送失败", code=3)
        return str(notify_id)

    async def user_info(self) -> MIoTUserInfo:
        return await self._http().get_user_info_async()

    def _http(self) -> MIoTHttpClient:
        if self.http is None:
            raise CliError("未绑定小米账号，请先运行 mihome account bind", code=1)
        return self.http

    async def _ensure_token(self) -> None:
        oauth = self.cfg.get("oauth")
        if not oauth or not oauth.get("access_token") or not oauth.get("refresh_token"):
            raise CliError("未绑定小米账号，请先运行 mihome account bind", code=1)
        expires_ts = int(oauth.get("expires_ts") or 0)
        if time.time() < expires_ts:
            return
        try:
            info = await self._oauth_client().refresh_access_token_async(
                oauth["refresh_token"]
            )
        except MIoTHttpError as exc:
            raise CliError(
                f"token 刷新失败，请重新 mihome account bind: {exc}",
                code=3,
            ) from exc
        except Exception as exc:
            raise CliError(
                f"token 刷新失败，请重新 mihome account bind: {exc}",
                code=3,
            ) from exc
        user = self.cfg.get("user")
        if user:
            info.user_info = MIoTUserInfo(**user) if isinstance(user, dict) else user
        self._store_oauth(info, info.user_info)

    def _store_oauth(self, info: MIoTOauthInfo, user: MIoTUserInfo | None) -> None:
        self.cfg["oauth"] = {
            "access_token": info.access_token,
            "refresh_token": info.refresh_token,
            "expires_ts": info.expires_ts,
        }
        if user:
            self.cfg["user"] = {
                "uid": user.uid,
                "nickname": user.nickname,
                "icon": user.icon,
                "union_id": user.union_id,
            }
        save_config(self.cfg)

    async def _maybe_select_single_home(self) -> None:
        try:
            homes = await self.list_homes()
        except Exception:
            return
        if len(homes) == 1:
            home = next(iter(homes.values()))
            self.cfg["home_id"] = home.home_id
            save_config(self.cfg)
