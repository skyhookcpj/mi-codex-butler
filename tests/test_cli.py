from click.testing import CliRunner

from mihome.cli import cli


def test_help_lists_v1_commands():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for name in ("account", "home", "device", "scene", "notify"):
        assert name in result.output


def test_device_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["device", "--help"])
    assert result.exit_code == 0
    for name in ("list", "spec", "control", "props", "action"):
        assert name in result.output
