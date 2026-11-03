"""``kill-switch on|off`` and ``admin kill-switch on|off`` (ticket 015; SPEC FR-16, FR-44): the CLI
only parses, calls the composition root with who acts, and renders."""

import pytest
import typer
from typer.testing import CliRunner

from guarded_exchange.domain.kill_switch import KillSwitch, KillSwitchActor
from guarded_exchange.interface.cli.main import build_cli, render_policy
from tests.unit.test_cli import AdminSpy, InitSpy, KeySpy, PolicySpy, ServeSpy, make_view

pytestmark = pytest.mark.req("FR-16", "FR-44")

runner = CliRunner()


def app(spy: PolicySpy) -> typer.Typer:
    return build_cli(ServeSpy(), InitSpy(), spy.commands(), AdminSpy().commands(), KeySpy().commands())


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["kill-switch", "on"], (None, True, KillSwitchActor.USER)),
        (["kill-switch", "off", "--yes"], (None, False, KillSwitchActor.USER)),
        (["admin", "kill-switch", "on", "--user", "bob"], ("bob", True, KillSwitchActor.ADMIN)),
        (["admin", "kill-switch", "off", "--user", "bob", "--yes"], ("bob", False, KillSwitchActor.ADMIN)),
    ],
)
def test_commands_pass_who_acts(args: list[str], expected: tuple[str | None, bool, KillSwitchActor]) -> None:
    spy = PolicySpy()

    result = runner.invoke(app(spy), args)

    assert result.exit_code == 0, result.output
    assert spy.switches == [expected]


def test_turning_it_on_says_that_nothing_was_cancelled() -> None:
    result = runner.invoke(app(PolicySpy()), ["kill-switch", "on"])

    assert "kill switch ON, turned on by the user" in result.stdout
    assert "Nothing was cancelled on the exchange" in result.stdout


def test_turning_it_off_without_yes_and_without_a_terminal_changes_nothing() -> None:
    spy = PolicySpy(switch=KillSwitch.USER)

    result = runner.invoke(app(spy), ["kill-switch", "off"])

    assert result.exit_code == 1
    assert "pass --yes" in result.stderr
    assert spy.switches == []


def test_turning_it_on_again_reports_no_change() -> None:
    result = runner.invoke(app(PolicySpy(switch=KillSwitch.ADMIN)), ["kill-switch", "on"])

    assert "kill switch unchanged, ON, turned on by the admin" in result.stdout


def test_policy_show_states_the_kill_switch() -> None:
    assert "kill switch: off" in render_policy(make_view()).splitlines()[1]
