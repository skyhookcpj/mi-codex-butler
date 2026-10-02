from mihome.specutil import (
    infer_value,
    lookup_iid_by_key,
    normalize_bool,
    parse_iid,
    parse_prop_groups,
    render_device_spec,
    resolve_keys_for_device,
    validate_value,
)
from mihome.errors import ValidationError
import pytest


SPEC = {
    "prop.2.1": {
        "type_name": "on",
        "service_description": "开关",
        "format": "bool",
        "writeable": True,
        "readable": True,
        "notify": True,
    },
    "prop.3.1": {
        "type_name": "on",
        "service_description": "空调",
        "format": "bool",
        "writeable": True,
        "readable": True,
    },
    "prop.2.2": {
        "type_name": "brightness",
        "service_description": "开关",
        "format": "uint8",
        "writeable": True,
        "readable": True,
        "value_range": [1, 100, 1],
        "unit": "percentage",
    },
    "action.5.1": {
        "type_name": "play-text",
        "service_description": "音箱",
        "writeable": True,
        "readable": False,
        "in_params": [{"name": "text-content", "format": "string"}],
    },
}


def test_lookup_unique_and_disambiguated():
    assert lookup_iid_by_key("brightness", SPEC) == "prop.2.2"
    assert lookup_iid_by_key("play-text", SPEC) == "action.5.1"
    assert lookup_iid_by_key("prop.2.2", SPEC) == "prop.2.2"
    with pytest.raises(ValidationError, match="matches 2"):
        lookup_iid_by_key("on", SPEC)
    assert lookup_iid_by_key("on@空调", SPEC) == "prop.3.1"


def test_resolve_keys_adds_at_suffix():
    keys = resolve_keys_for_device(SPEC)
    assert keys["prop.2.2"] == "brightness"
    assert keys["prop.2.1"] == "on@开关"
    assert keys["prop.3.1"] == "on@空调"
    assert keys["action.5.1"] == "play-text"


def test_infer_and_range():
    assert infer_value("true") is True
    assert infer_value("26") == 26
    assert normalize_bool("on") is True
    validate_value(SPEC["prop.2.2"], 30)
    with pytest.raises(ValidationError, match="out of range"):
        validate_value(SPEC["prop.2.2"], 0)


def test_parse_prop_groups():
    assert parse_prop_groups(("100000006", "2.1", "3.1")) == [
        ("100000006", ["2.1", "3.1"])
    ]
    assert parse_prop_groups(("100000006", "2.1", "100000007", "2.1")) == [
        ("100000006", ["2.1"]),
        ("100000007", ["2.1"]),
    ]
    with pytest.raises(ValidationError):
        parse_prop_groups(("2.1",))


def test_parse_iid():
    assert parse_iid("prop.2.1") == ("prop", 2, 1)
    assert parse_iid("2.1") == ("prop", 2, 1)
    assert parse_iid("10.1") == ("prop", 10, 1)
    assert parse_iid("action.5.1") == ("action", 5, 1)
    with pytest.raises(ValidationError, match="invalid iid"):
        parse_iid("on@开关左键")


def test_render_contains_play_text():
    text = render_device_spec(
        {
            "did": "1",
            "name": "客厅音箱",
            "room": "客厅",
            "category": "speaker",
            "online": True,
            "spec": SPEC,
        }
    )
    assert "play-text|x|text-content:string" in text
    assert "did=1" in text


# ---- device control 多组参数 ----

from mihome.specutil import parse_control_groups


def test_control_single_group_is_backward_compatible():
    assert parse_control_groups(("100000001", "2.1", "true")) == [("100000001", [("2.1", "true")])]


def test_control_multiple_devices_and_props():
    args = ("100000001", "2.1", "true", "3.1", "true", "100000002", "2.1", "false", "proxy.100000005.0103", "2.4", "26")
    assert parse_control_groups(args) == [
        ("100000001", [("2.1", "true"), ("3.1", "true")]),
        ("100000002", [("2.1", "false")]),
        ("proxy.100000005.0103", [("2.4", "26")]),
    ]


def test_control_numeric_values_are_not_mistaken_for_dids():
    # 窗帘开度 50、风速 4 都满足 did 的格式，按位置不会被切错
    args = ("100000008", "2.7", "50", "100000009", "2.7", "50", "proxy.100000005.0103", "3.2", "4", "2.1", "true")
    assert parse_control_groups(args) == [
        ("100000008", [("2.7", "50")]),
        ("100000009", [("2.7", "50")]),
        ("proxy.100000005.0103", [("3.2", "4"), ("2.1", "true")]),
    ]


@pytest.mark.parametrize(
    "args, hint",
    [
        ((), "provide"),
        (("100000001",), "缺少"),
        (("100000001", "2.1"), "缺少值"),
        (("100000001", "on", "true"), "应该是 iid"),
        (("客厅灯", "2.1", "true"), "不是 did"),
        (("100000001", "2.1", "true", "3.1"), "缺少值"),
    ],
)
def test_control_bad_args(args, hint):
    with pytest.raises(ValidationError, match=hint):
        parse_control_groups(args)


from mihome.specutil import value_matches


@pytest.mark.parametrize(
    "want, got, ok",
    [
        (True, True, True),
        (True, 1, True),
        (True, "on", True),
        (False, False, True),
        (True, False, False),
        (True, None, False),
        (26, 26, True),
        (26, 26.0, True),
        (26, "26", True),
        (50, 49, False),
        (2, True, False),
        ("auto", "auto", True),
    ],
)
def test_value_matches(want, got, ok):
    assert value_matches(want, got) is ok
