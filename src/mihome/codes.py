"""米家设备侧执行码 → 中文。外层成功只表示 RPC 送达。"""

from __future__ import annotations

MIOT_SPEC_CODES = {
    -704042011: "设备离线",
    -704042001: "未找到设备",
    -704090001: "未找到设备",
    -704040003: "属性不存在",
    -704040004: "事件不存在",
    -704040005: "方法不存在",
    -704040999: "功能未上线",
    -704044006: "未找到功能定义",
    -704030013: "属性不可读",
    -704030023: "属性不可写",
    -704030033: "属性不可上报",
    -704030992: "请求过于频繁，本次被拒绝",
    -704220043: "属性值不正确",
    -704220035: "方法输入参数错误",
    -704220025: "方法输入参数数量不匹配",
    -704222035: "方法输出参数数量不匹配或参数错误",
    -704222034: "事件参数数量不匹配",
    -704220008: "非法的 ID（SIID/PIID/EIID/AIID）",
    -704053100: "无法执行此操作",
    -704053101: "摄像机休眠中",
    -704013101: "红外设备不支持此操作",
    -704083036: "操作超时",
    -704012904: "设备未授权控制能力给小爱",
    -704012905: "设备未绑定",
    -704012906: "认证失败",
    -702022036: "操作正在处理中",
    -705201013: "读属性失败",
    -706012013: "读属性失败",
    -706012014: "读属性失败",
    -705201023: "写属性失败",
    -706012023: "写属性失败",
    -705201015: "方法执行失败",
    -706012015: "方法执行失败",
    -704002000: "设备错误（通用）",
}

MIOT_OK_CODES = frozenset({0, -702000000, -702010000})


def annotate_result_codes(data: dict) -> None:
    """失败项补 code_msg，并把外层 code/message 改成真实结果。"""
    inner = data.get("data")
    if not isinstance(inner, dict):
        return

    items: list[dict] = []
    for arr_key in ("results", "properties"):
        arr = inner.get(arr_key)
        if isinstance(arr, list):
            items.extend(it for it in arr if isinstance(it, dict))
    single = inner.get("result")
    if isinstance(single, dict):
        items.append(single)

    total = 0
    failures: list[tuple[int, str]] = []
    for it in items:
        total += 1
        code = it.get("code")
        if isinstance(code, int) and code < 0 and code not in MIOT_OK_CODES:
            msg = MIOT_SPEC_CODES.get(
                code, "设备侧执行失败（未知错误码，详见米家 spec 错误码表）"
            )
            it["code_msg"] = msg
            failures.append((code, msg))

    if not failures:
        return

    reasons = "；".join(dict.fromkeys(m for _, m in failures))
    data["code"] = failures[0][0]
    if len(failures) == total:
        data["message"] = f"失败：{reasons}"
    else:
        data["message"] = f"部分失败（{len(failures)}/{total}）：{reasons}"
