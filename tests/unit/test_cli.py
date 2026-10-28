from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from guarded_exchange.application.policy import PolicyView
from guarded_exchange.domain.actions import ActionKind
from guarded_exchange.domain.errors import PermissionDenied
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.policy import (
    NO_OVERRIDES,
    InstrumentRef,
    PolicyPatch,
    Profile,
    default_ceiling,
    strictest,
)
from guarded_exchange.domain.users import Environment, User, UserStatus
from guarded_exchange.interface.cli.main import (
    InitReport,
    PolicyCommands,
    PolicyOptions,
    Transport,
    build_cli,
    render_policy,
)

runner = CliRunner()
REPORT = InitReport(
    db_path=Path("/data/gx.db"),
    schema_revision="0002",
    master_key_file=Path("/conf/master.key"),
    master_key_created=True,
    user_name="local",
    user_id="u-1",
    user_created=True,
)


class ServeSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[Transport, Path | None]] = []

    def __call__(self, transport: Transport, env_file: Path | None) -> None:
        self.calls.append((transport, env_file))


class InitSpy:
    def __init__(self, report: InitReport = REPORT) -> None:
        self.calls: list[Path | None] = []
        self.report = report

    def __call__(self, env_file: Path | None) -> InitReport:
        self.calls.append(env_file)
        return self.report


USER = User("u-1", "local", UserStatus.ACTIVE, Environment.TESTNET, datetime(2026, 10, 27, tzinfo=UTC))


def make_view(overrides: PolicyPatch = NO_OVERRIDES) -> PolicyView:
    ceiling = default_ceiling(Environment.TESTNET)
    return PolicyView(
        user=USER,
        ceiling=ceiling,
        ceiling_is_default=True,
        overrides=overrides,
        effective=strictest(ceiling, overrides, Environment.TESTNET),
    )


@dataclass
class PolicySpy:
    shown: list[tuple[Path | None, str | None]] = field(default_factory=list)
    tightened: list[PolicyPatch] = field(default_factory=list)
    ceilings: list[tuple[str | None, PolicyPatch]] = field(default_factory=list)
    fail_with: Exception | None = None

    def commands(self) -> PolicyCommands:
        return PolicyCommands(show=self.show, tighten=self.tighten, set_ceiling=self.set_ceiling)

    def show(self, env_file: Path | None, user: str | None) -> PolicyView:
        self.shown.append((env_file, user))
        return make_view()

    def tighten(self, env_file: Path | None, change: PolicyPatch) -> PolicyView:
        if self.fail_with is not None:
            raise self.fail_with
        self.tightened.append(change)
        return make_view(change)

    def set_ceiling(self, env_file: Path | None, user: str | None, change: PolicyPatch) -> PolicyView:
        self.ceilings.append((user, change))
        return make_view()


def test_serve_defaults_to_stdio_without_env_file() -> None:
    spy = ServeSpy()

    result = runner.invoke(build_cli(spy, InitSpy(), PolicySpy().commands()), ["serve"])

    assert result.exit_code == 0, result.output
    assert spy.calls == [("stdio", None)]


def test_serve_passes_explicit_env_file() -> None:
    spy = ServeSpy()

    result = runner.invoke(
        build_cli(spy, InitSpy(), PolicySpy().commands()), ["serve", "--env-file", "conf/gx.env"]
    )

    assert result.exit_code == 0, result.output
    assert spy.calls == [("stdio", Path("conf/gx.env"))]


def test_serve_rejects_unsupported_transport() -> None:
    spy = ServeSpy()

    result = runner.invoke(
        build_cli(spy, InitSpy(), PolicySpy().commands()), ["serve", "--transport", "streamable-http"]
    )

    assert result.exit_code != 0
    assert spy.calls == []


def test_init_passes_env_file_and_prints_report() -> None:
    init = InitSpy()

    result = runner.invoke(
        build_cli(ServeSpy(), init, PolicySpy().commands()), ["init", "--env-file", "conf/gx.env"]
    )

    assert result.exit_code == 0, result.output
    assert init.calls == [Path("conf/gx.env")]
    assert "/data/gx.db (schema 0002)" in result.output
    assert "/conf/master.key (created - back it up)" in result.output
    assert "local [u-1] (created)" in result.output


def test_init_report_for_a_rerun_says_nothing_was_created() -> None:
    rerun = InitReport(
        db_path=Path("/data/gx.db"),
        schema_revision="0002",
        master_key_file=Path("/conf/master.key"),
        master_key_created=False,
        user_name="local",
        user_id="u-1",
        user_created=False,
    )

    result = runner.invoke(build_cli(ServeSpy(), InitSpy(rerun), PolicySpy().commands()), ["init"])

    assert result.exit_code == 0, result.output
    assert "(kept existing)" in result.output
    assert "(already existed)" in result.output


# --- policy commands ------------------------------------------------------------------------


def test_policy_show_renders_ceiling_user_setting_and_effective_per_row() -> None:
    spy = PolicySpy()

    result = runner.invoke(
        build_cli(ServeSpy(), InitSpy(), spy.commands()), ["policy", "show", "--user", "bob"]
    )

    assert result.exit_code == 0, result.output
    assert spy.shown == [(None, "bob")]
    assert "user: local [u-1] (active, testnet)" in result.output
    rows = {line.split()[0]: line.split()[1:] for line in result.output.splitlines()[3:]}
    assert rows["profile"] == ["read_only", "-", "read_only"]
    assert rows["allowlist"] == ["(none)", "-", "(none)"]
    assert rows["confirm_above_notional_usdt"] == ["off", "-", "off"]
    assert rows["trust_client_ui"] == ["yes", "-", "yes"]


def test_rendered_user_setting_shows_where_the_value_comes_from() -> None:
    text = render_policy(make_view(PolicyPatch(max_leverage=Decimal(3))))

    row = next(line for line in text.splitlines() if line.startswith("max_leverage"))
    assert row.split() == ["max_leverage", "10", "3", "3"]


@pytest.mark.req("FR-12")
def test_policy_tighten_turns_options_into_a_domain_patch() -> None:
    spy = PolicySpy()
    args = [
        "policy", "tighten", "--profile", "read_only", "--allow", "linear:BTCUSDT", "--allow", "spot:ETHUSDT",
        "--max-order-notional-usdt", "250.50", "--max-leverage", "3", "--slippage-pct", "0.5",
        "--confirm", "place_order", "--confirm", "cancel_order", "--confirm-above-notional-usdt", "100",
        "--confirmation-ttl-s", "60", "--no-trust-client-ui", "--prefer-external-channel",
        "--unconfirmed-max-actions", "2", "--unconfirmed-window-s", "3600", "--max-pending", "1",
    ]  # fmt: skip

    result = runner.invoke(build_cli(ServeSpy(), InitSpy(), spy.commands()), args)

    assert result.exit_code == 0, result.output
    assert spy.tightened == [
        PolicyPatch(
            profile=Profile.READ_ONLY,
            allowlist=frozenset(
                {InstrumentRef(Category.LINEAR, "BTCUSDT"), InstrumentRef(Category.SPOT, "ETHUSDT")}
            ),
            max_order_notional_usdt=Decimal("250.50"),
            max_leverage=Decimal(3),
            slippage_pct=Decimal("0.5"),
            confirm_actions=frozenset({ActionKind.PLACE_ORDER, ActionKind.CANCEL_ORDER}),
            confirm_above_notional_usdt=Decimal(100),
            confirmation_ttl_s=60,
            trust_client_ui=False,
            prefer_external_channel=True,
            unconfirmed_max_actions=2,
            unconfirmed_window_s=3600,
            max_pending=1,
        )
    ]


def test_set_ceiling_passes_the_user_and_only_the_given_options() -> None:
    spy = PolicySpy()
    args = ["admin", "policy", "set-ceiling", "--user", "alice", "--profile", "trade", "--allow-none",
            "--confirm", "all", "--confirm-above-notional-usdt", "none"]  # fmt: skip

    result = runner.invoke(build_cli(ServeSpy(), InitSpy(), spy.commands()), args)

    assert result.exit_code == 0, result.output
    [(user, change)] = spy.ceilings
    assert user == "alice"
    assert change.changes() == {
        "profile": Profile.TRADE,
        "allowlist": frozenset(),
        "confirm_actions": frozenset(ActionKind),
        "confirm_above_notional_usdt": None,
    }


@pytest.mark.parametrize(
    ("args", "hint"),
    [
        (["--max-leverage", "lots"], "max_leverage"),
        (["--allow", "BTCUSDT"], "allowlist"),
        (["--confirm", "withdraw"], "confirm_actions"),
        (["--slippage-pct", "7"], "slippage_pct"),
        (["--allow", "linear:BTCUSDT", "--allow-none"], "--allow"),
    ],
)
def test_bad_option_values_are_usage_errors_naming_the_setting(args: list[str], hint: str) -> None:
    spy = PolicySpy()

    result = runner.invoke(build_cli(ServeSpy(), InitSpy(), spy.commands()), ["policy", "tighten", *args])

    assert result.exit_code == 2
    assert hint in result.stderr
    assert spy.tightened == []


def test_options_without_values_make_an_empty_patch() -> None:
    assert PolicyOptions().to_patch().is_empty


def test_refusal_from_the_use_case_propagates_to_the_composition_root() -> None:
    spy = PolicySpy(fail_with=PermissionDenied("weaker", details={"field": "max_leverage"}))

    result = runner.invoke(
        build_cli(ServeSpy(), InitSpy(), spy.commands()), ["policy", "tighten", "--max-leverage", "50"]
    )

    assert isinstance(result.exception, PermissionDenied)  # bootstrap prints it and exits 1
