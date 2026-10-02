import asyncio

import aiohttp

from mihome.errors import format_network_error


def test_timeout_includes_seconds():
    msg = format_network_error(TimeoutError())
    assert "网络超时" in msg
    assert "30s" in msg
    assert "TimeoutError" in msg


def test_asyncio_timeout():
    msg = format_network_error(asyncio.TimeoutError())
    assert "网络超时" in msg
    assert "30s" in msg


def test_client_error_with_message():
    msg = format_network_error(aiohttp.ClientError("connection reset"))
    assert "网络错误" in msg
    assert "ClientError" in msg
    assert "connection reset" in msg


def test_client_error_empty_message():
    msg = format_network_error(aiohttp.ClientError())
    assert msg == "网络错误: ClientError"
    assert not msg.endswith("：") and not msg.endswith(": ")


# ---- 凭据脱敏 ----

from mihome.errors import redact

FAKE_RT = "R3_fakeRefreshTokenValue0123456789abcdef"


def test_redacts_refresh_token_from_oauth_failure():
    # 形状照抄 2026-10-01 实际泄露的报错（令牌换成假的）
    msg = (
        "token 刷新失败，请重新 mihome account bind: invalid http response, "
        '{"code":-6,"message":"{\\"error\\":96009,\\"error_description\\":\\"invalid refresh token\\"}",'
        '"result":{"error":96009}}, '
        '{"client_id": "2882303761520431603", "redirect_uri": "https://mico.api.mijia.tech/login_redirect", '
        f'"refresh_token": "{FAKE_RT}"}}'
    )
    out = redact(msg)
    assert FAKE_RT not in out
    assert '"refresh_token": "R3_f…(已隐去)' in out
    assert '"code":-6' in out, "数字错误码不能被遮"
    assert "invalid refresh token" in out
    assert "2882303761520431603" in out, "client_id 是公开的应用 id，保留"


def test_redacts_auth_code_and_access_token():
    out = redact('{"code": "C3_authcodeABCDEFGH", "access_token": "V3_accessTOKENxyz123"}')
    assert "authcodeABCDEFGH" not in out
    assert "accessTOKENxyz123" not in out


def test_redacts_plain_and_bearer_forms():
    out = redact("GET /x?clientId=1&token=abcdef0123456789 Authorization: Bearer abcdefgh12345678")
    assert "abcdef0123456789" not in out
    assert "abcdefgh12345678" not in out
    assert "clientId=1" in out


def test_leaves_ordinary_errors_alone():
    for msg in ['{"code": -704042011, "message": "设备离线"}', "网络超时（HTTP 30s）: TimeoutError", "code=-704042011"]:
        assert redact(msg) == msg
