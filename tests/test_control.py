"""device control：多台一次请求、--verify 回读补发。用假的云端会话，不碰真设备。"""

import json

import pytest
from click.testing import CliRunner

from mihome.cli import cli
from mihome.commands import device as device_cmd


class FakeSession:
    """记录 set_props 调用；状态按 apply 规则落地（可模拟设备丢指令）。"""

    def __init__(self, drop=()):
        self.state = {}
        self.set_calls = []
        self.drop = list(drop)  # 依次对每次 set_props 生效：要丢掉的 (did, siid) 集合

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def set_props(self, params):
        self.set_calls.append([(p.did, p.siid, p.piid, p.value) for p in params])
        dropped = self.drop.pop(0) if self.drop else set()
        for p in params:
            if (p.did, p.siid) not in dropped:
                self.state[(p.did, p.siid, p.piid)] = p.value
        return [{"did": p.did, "siid": p.siid, "piid": p.piid, "code": 1} for p in params]

    async def get_props(self, params):
        return [
            {"did": p.did, "siid": p.siid, "piid": p.piid, "value": self.state.get((p.did, p.siid, p.piid)), "code": 0}
            for p in params
        ]


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(device_cmd, "VERIFY_DELAY_S", 0)

    def _run(args, session):
        monkeypatch.setattr(device_cmd, "CloudSession", lambda: session)
        res = CliRunner().invoke(cli, ["device", "control", *args])
        out = json.loads(res.stdout) if res.stdout.strip().startswith("{") else None
        return res, out

    return _run


LIVING = ["100000001", "2.1", "true", "3.1", "true", "100000002", "2.1", "true", "100000003", "2.1", "true"]


def test_multiple_devices_go_out_in_one_request(run):
    s = FakeSession()
    res, out = run(LIVING, s)
    assert res.exit_code == 0, res.output
    assert len(s.set_calls) == 1
    assert s.set_calls[0] == [
        ("100000001", 2, 1, True),
        ("100000001", 3, 1, True),
        ("100000002", 2, 1, True),
        ("100000003", 2, 1, True),
    ]
    assert "did" not in out["data"], "多台时不写顶层 did"
    assert len(out["data"]["results"]) == 4


def test_single_device_output_unchanged(run):
    res, out = run(["100000004", "2.1", "false"], FakeSession())
    assert res.exit_code == 0
    assert out["data"]["did"] == "100000004"


def test_verify_retries_dropped_command(run):
    # 第一次请求里 100000001.3 丢了，补发后到位
    s = FakeSession(drop=[{("100000001", 3)}])
    res, out = run([*LIVING, "--verify"], s)
    assert res.exit_code == 0, res.output
    assert len(s.set_calls) == 2
    assert s.set_calls[1] == [("100000001", 3, 1, True)], "只补发没到位的那一路"
    checks = {(c["did"], c["iid"]): c for c in out["data"]["verify"]}
    assert all(c["ok"] for c in checks.values())
    assert checks[("100000001", "3.1")]["retried"] is True
    assert "retried" not in checks[("100000001", "2.1")]


def test_verify_reports_and_exits_3_when_still_missing(run):
    s = FakeSession(drop=[{("100000003", 2)}, {("100000003", 2)}])
    res, out = run([*LIVING, "--verify"], s)
    assert res.exit_code == 3
    assert out["code"] == 3
    assert "100000003 2.1" in out["message"]


def test_set_option_still_works_and_rejects_extra_dids(run):
    s = FakeSession()
    res, _ = run(["proxy.100000005.0103", "--set", "2.4", "26", "--set", "2.1", "true"], s)
    assert res.exit_code == 0
    assert s.set_calls[0] == [("proxy.100000005.0103", 2, 4, 26), ("proxy.100000005.0103", 2, 1, True)]
    res, _ = run(["100000001", "2.1", "true", "--set", "2.4", "26"], FakeSession())
    assert res.exit_code == 1
