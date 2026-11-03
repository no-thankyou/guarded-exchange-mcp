from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from guarded_exchange.application.admin import IssuedToken, RevokedToken
from guarded_exchange.application.credentials import StoredCredential
from guarded_exchange.application.kill_switch import KillSwitchChange
from guarded_exchange.application.policy import OverridesReset, PolicyView
from guarded_exchange.domain.access_tokens import AccessToken
from guarded_exchange.domain.actions import ActionKind
from guarded_exchange.domain.credentials import (
    EXCHANGE_BYBIT,
    WARNING_NO_IP_WHITELIST,
    ApiKeyPair,
    Credential,
    CredentialStatus,
    KeyAssessment,
    KeyPermissions,
)
from guarded_exchange.domain.errors import NotFound, PermissionDenied
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.kill_switch import KillSwitch, KillSwitchActor, turned_off, turned_on
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
    AdminCommands,
    InitReport,
    KeyCommands,
    PolicyCommands,
    PolicyOptions,
    Transport,
    build_cli,
    render_overrides_reset,
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
    resets: list[tuple[str, frozenset[str] | None]] = field(default_factory=list)
    switches: list[tuple[str | None, bool, KillSwitchActor]] = field(default_factory=list)
    own: PolicyPatch = NO_OVERRIDES  # the user's settings ``show`` reports
    switch: KillSwitch = KillSwitch.NONE  # the kill switch ``kill_switch`` starts from
    fail_with: Exception | None = None

    def commands(self) -> PolicyCommands:
        return PolicyCommands(
            show=self.show,
            tighten=self.tighten,
            set_ceiling=self.set_ceiling,
            reset_overrides=self.reset_overrides,
            kill_switch=self.kill_switch,
        )

    def kill_switch(
        self, env_file: Path | None, user: str | None, on: bool, by: KillSwitchActor
    ) -> KillSwitchChange:
        self.switches.append((user, on, by))
        after = turned_on(self.switch, by) if on else turned_off(self.switch, by)
        return KillSwitchChange(user=replace(USER, kill_switch=after), before=self.switch, after=after)

    def show(self, env_file: Path | None, user: str | None) -> PolicyView:
        if self.fail_with is not None:
            raise self.fail_with
        self.shown.append((env_file, user))
        return make_view(self.own)

    def reset_overrides(
        self, env_file: Path | None, user: str, fields: frozenset[str] | None
    ) -> OverridesReset:
        self.resets.append((user, fields))
        reset = tuple(name for name in self.own.changes() if fields is None or name in fields)
        return OverridesReset(view=make_view(), reset_fields=reset)

    def tighten(self, env_file: Path | None, change: PolicyPatch) -> PolicyView:
        if self.fail_with is not None:
            raise self.fail_with
        self.tightened.append(change)
        return make_view(change)

    def set_ceiling(self, env_file: Path | None, user: str | None, change: PolicyPatch) -> PolicyView:
        self.ceilings.append((user, change))
        return make_view()


SECRET = "gxt_" + "s" * 43


@dataclass
class AdminSpy:
    created: list[str] = field(default_factory=list)
    issued_for: list[str] = field(default_factory=list)
    revoked: list[str] = field(default_factory=list)
    users: list[User] = field(default_factory=lambda: [USER])
    already_revoked: bool = False
    fail_with: Exception | None = None

    def commands(self) -> AdminCommands:
        return AdminCommands(
            create_user=self.create_user,
            list_users=lambda env_file: self.users,
            issue_token=self.issue_token,
            revoke_token=self.revoke_token,
        )

    def create_user(self, env_file: Path | None, name: str) -> User:
        self.created.append(name)
        return User("u-2", name, UserStatus.ACTIVE, Environment.TESTNET, datetime(2026, 10, 28, tzinfo=UTC))

    def issue_token(self, env_file: Path | None, user: str) -> IssuedToken:
        if self.fail_with is not None:
            raise self.fail_with
        self.issued_for.append(user)
        return IssuedToken(
            token_id="t-1",  # noqa: S106 - an id, not a secret
            user=USER,
            secret=SECRET,
            created_at=USER.created_at,
        )

    def revoke_token(self, env_file: Path | None, token_id: str) -> RevokedToken:
        self.revoked.append(token_id)
        token = AccessToken(token_id, "u-1", "0" * 64, USER.created_at, USER.created_at)
        return RevokedToken(token=token, already_revoked=self.already_revoked)


def test_serve_defaults_to_stdio_without_env_file() -> None:
    spy = ServeSpy()

    result = runner.invoke(
        build_cli(spy, InitSpy(), PolicySpy().commands(), AdminSpy().commands(), KeySpy().commands()),
        ["serve"],
    )

    assert result.exit_code == 0, result.output
    assert spy.calls == [("stdio", None)]


def test_serve_passes_explicit_env_file() -> None:
    spy = ServeSpy()

    result = runner.invoke(
        build_cli(spy, InitSpy(), PolicySpy().commands(), AdminSpy().commands(), KeySpy().commands()),
        ["serve", "--env-file", "conf/gx.env"],
    )

    assert result.exit_code == 0, result.output
    assert spy.calls == [("stdio", Path("conf/gx.env"))]


def test_serve_rejects_unsupported_transport() -> None:
    spy = ServeSpy()

    result = runner.invoke(
        build_cli(spy, InitSpy(), PolicySpy().commands(), AdminSpy().commands(), KeySpy().commands()),
        ["serve", "--transport", "streamable-http"],
    )

    assert result.exit_code != 0
    assert spy.calls == []


def test_init_passes_env_file_and_prints_report() -> None:
    init = InitSpy()

    result = runner.invoke(
        build_cli(ServeSpy(), init, PolicySpy().commands(), AdminSpy().commands(), KeySpy().commands()),
        ["init", "--env-file", "conf/gx.env"],
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

    result = runner.invoke(
        build_cli(
            ServeSpy(), InitSpy(rerun), PolicySpy().commands(), AdminSpy().commands(), KeySpy().commands()
        ),
        ["init"],
    )

    assert result.exit_code == 0, result.output
    assert "(kept existing)" in result.output
    assert "(already existed)" in result.output


# --- policy commands ------------------------------------------------------------------------


def test_policy_show_renders_ceiling_user_setting_and_effective_per_row() -> None:
    spy = PolicySpy()

    result = runner.invoke(
        build_cli(ServeSpy(), InitSpy(), spy.commands(), AdminSpy().commands(), KeySpy().commands()),
        ["policy", "show", "--user", "bob"],
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

    result = runner.invoke(
        build_cli(ServeSpy(), InitSpy(), spy.commands(), AdminSpy().commands(), KeySpy().commands()), args
    )

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

    result = runner.invoke(
        build_cli(ServeSpy(), InitSpy(), spy.commands(), AdminSpy().commands(), KeySpy().commands()), args
    )

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

    result = runner.invoke(
        build_cli(ServeSpy(), InitSpy(), spy.commands(), AdminSpy().commands(), KeySpy().commands()),
        ["policy", "tighten", *args],
    )

    assert result.exit_code == 2
    assert hint in result.stderr
    assert spy.tightened == []


def test_options_without_values_make_an_empty_patch() -> None:
    assert PolicyOptions().to_patch().is_empty


def test_refusal_from_the_use_case_propagates_to_the_composition_root() -> None:
    spy = PolicySpy(fail_with=PermissionDenied("weaker", details={"field": "max_leverage"}))

    result = runner.invoke(
        build_cli(ServeSpy(), InitSpy(), spy.commands(), AdminSpy().commands(), KeySpy().commands()),
        ["policy", "tighten", "--max-leverage", "50"],
    )

    assert isinstance(result.exception, PermissionDenied)  # bootstrap prints it and exits 1


# --- admin policy reset-overrides (ticket 034) ------------------------------------------------

OWN = PolicyPatch(profile=Profile.READ_ONLY, max_leverage=Decimal(2))


def _policy_app(spy: PolicySpy) -> typer.Typer:
    return build_cli(ServeSpy(), InitSpy(), spy.commands(), AdminSpy().commands(), KeySpy().commands())


@pytest.mark.req("FR-12")
def test_reset_overrides_shows_what_it_removes_and_asks_first() -> None:
    spy = PolicySpy(own=OWN)

    declined = runner.invoke(
        _policy_app(spy), ["admin", "policy", "reset-overrides", "--user", "bob"], input="n\n"
    )
    confirmed = runner.invoke(
        _policy_app(spy),
        ["admin", "policy", "reset-overrides", "--user", "bob", "--field", "max-leverage"],
        input="y\n",
    )

    assert declined.exit_code == 1
    assert "user setting of bob to remove: profile = read_only" in declined.stdout
    assert "max_leverage = 2" in declined.stdout
    assert "not confirmed: nothing changed" in declined.stderr
    assert confirmed.exit_code == 0, confirmed.output
    assert "profile = read_only" not in confirmed.stdout  # only the named field is shown
    assert "reset to the ceiling: max_leverage" in confirmed.stdout
    assert spy.resets == [("bob", frozenset({"max_leverage"}))]


@pytest.mark.req("FR-12")
def test_reset_overrides_after_the_question_resets_only_the_listed_settings() -> None:
    spy = PolicySpy(own=OWN)

    result = runner.invoke(
        _policy_app(spy), ["admin", "policy", "reset-overrides", "--user", "bob"], input="y\n"
    )

    assert result.exit_code == 0, result.output
    # not None ("all"): a setting the user tightens while the question is open is not removed unseen
    assert spy.resets == [("bob", frozenset({"profile", "max_leverage"}))]


@pytest.mark.req("FR-12")
def test_reset_overrides_without_yes_and_without_a_terminal_refuses() -> None:
    spy = PolicySpy(own=OWN)

    result = runner.invoke(
        _policy_app(spy), ["admin", "policy", "reset-overrides", "--user", "bob"]
    )  # no input

    assert result.exit_code == 1
    assert "pass --yes" in result.stderr
    assert spy.resets == []


def test_reset_overrides_with_yes_does_not_ask() -> None:
    spy = PolicySpy(own=OWN)

    result = runner.invoke(_policy_app(spy), ["admin", "policy", "reset-overrides", "--user", "bob", "--yes"])

    assert result.exit_code == 0, result.output
    assert "?" not in result.stdout
    assert "reset to the ceiling: profile, max_leverage" in result.stdout
    assert spy.resets == [("bob", None)]


def test_reset_overrides_with_nothing_to_reset_neither_asks_nor_resets() -> None:
    spy = PolicySpy(own=PolicyPatch(max_pending=1))

    result = runner.invoke(
        _policy_app(spy), ["admin", "policy", "reset-overrides", "--user", "bob", "--field", "profile"]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "user bob has no own settings to reset; nothing changed"
    assert spy.resets == []


def test_reset_overrides_rejects_unknown_fields_and_needs_a_user() -> None:
    spy = PolicySpy(own=OWN)

    unknown = runner.invoke(
        _policy_app(spy), ["admin", "policy", "reset-overrides", "--user", "bob", "--field", "withdrawals"]
    )
    no_user = runner.invoke(_policy_app(spy), ["admin", "policy", "reset-overrides", "--yes"])

    assert unknown.exit_code == 2
    assert "unknown policy settings: withdrawals" in unknown.stderr
    assert no_user.exit_code == 2
    assert "--user" in no_user.stderr
    assert spy.shown == []
    assert spy.resets == []


def test_reset_overrides_failure_propagates_before_asking() -> None:
    spy = PolicySpy(own=OWN, fail_with=NotFound("user 'ghost' does not exist"))

    result = runner.invoke(_policy_app(spy), ["admin", "policy", "reset-overrides", "--user", "ghost"])

    assert isinstance(result.exception, NotFound)  # bootstrap prints it and exits 1
    assert spy.resets == []


def test_rendered_reset_result_with_nothing_reset() -> None:
    text = render_overrides_reset(OverridesReset(view=make_view(), reset_fields=()))

    assert text == "user local has no own settings to reset; nothing changed"


@pytest.mark.req("FR-44")
def test_reset_overrides_is_an_admin_command_only() -> None:
    result = runner.invoke(_policy_app(PolicySpy(own=OWN)), ["policy", "reset-overrides", "--yes"])

    assert result.exit_code == 2  # the user's `policy` group has no such command


@pytest.mark.req("FR-29")
def test_serve_accepts_the_http_transport() -> None:
    spy = ServeSpy()

    result = runner.invoke(
        build_cli(spy, InitSpy(), PolicySpy().commands(), AdminSpy().commands(), KeySpy().commands()),
        ["serve", "--transport", "http"],
    )

    assert result.exit_code == 0, result.output
    assert spy.calls == [("http", None)]


@pytest.mark.req("FR-29")
def test_admin_user_create_and_list() -> None:
    spy = AdminSpy()
    app = build_cli(ServeSpy(), InitSpy(), PolicySpy().commands(), spy.commands(), KeySpy().commands())

    created = runner.invoke(app, ["admin", "user", "create", "alice"])
    listed = runner.invoke(app, ["admin", "user", "list"])
    empty = runner.invoke(
        build_cli(
            ServeSpy(), InitSpy(), PolicySpy().commands(), AdminSpy(users=[]).commands(), KeySpy().commands()
        ),
        ["admin", "user", "list"],
    )

    assert created.exit_code == 0, created.output
    assert spy.created == ["alice"]
    assert "user created: alice [u-2] (active, testnet)" in created.output
    assert listed.output.splitlines()[0].split() == ["name", "id", "status", "environment"]
    assert listed.output.splitlines()[1].split() == ["local", "u-1", "active", "testnet"]
    assert empty.output.strip() == "no users"


@pytest.mark.req("FR-29")
def test_admin_token_issue_prints_the_secret_once_with_its_id() -> None:
    spy = AdminSpy()

    result = runner.invoke(
        build_cli(ServeSpy(), InitSpy(), PolicySpy().commands(), spy.commands(), KeySpy().commands()),
        ["admin", "token", "issue", "--user", "local"],
    )

    assert result.exit_code == 0, result.output
    assert spy.issued_for == ["local"]
    assert result.output.count(SECRET) == 1
    assert "token id: t-1" in result.output
    assert "Shown once" in result.output
    assert "admin token revoke t-1" in result.output


def test_admin_token_issue_requires_a_user() -> None:
    spy = AdminSpy()

    result = runner.invoke(
        build_cli(ServeSpy(), InitSpy(), PolicySpy().commands(), spy.commands(), KeySpy().commands()),
        ["admin", "token", "issue"],
    )

    assert result.exit_code == 2
    assert spy.issued_for == []


@pytest.mark.req("FR-29")
@pytest.mark.parametrize(("already", "expected"), [(False, "revoked"), (True, "was already revoked")])
def test_admin_token_revoke(already: bool, expected: str) -> None:
    spy = AdminSpy(already_revoked=already)

    result = runner.invoke(
        build_cli(ServeSpy(), InitSpy(), PolicySpy().commands(), spy.commands(), KeySpy().commands()),
        ["admin", "token", "revoke", "t-1"],
    )

    assert result.exit_code == 0, result.output
    assert spy.revoked == ["t-1"]
    assert result.output.strip() == f"token t-1 (user id u-1) {expected}"


def test_admin_command_failure_propagates_from_the_composition_root() -> None:
    spy = AdminSpy(fail_with=NotFound("user 'bob' does not exist"))

    result = runner.invoke(
        build_cli(ServeSpy(), InitSpy(), PolicySpy().commands(), spy.commands(), KeySpy().commands()),
        ["admin", "token", "issue", "--user", "bob"],
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, NotFound)  # bootstrap._with_storage turns it into exit 1


# --- exchange keys (ticket 007) ---------------------------------------------------------------

API_KEY = "CLIKEY0123456789AB"
API_SECRET = "cli-secret-0123456789abcdefghijklmnop"  # noqa: S105 - test value


def _credential(**changes: Any) -> Credential:
    values: dict[str, Any] = {
        "id": "cred-1",
        "user_id": "u-1",
        "exchange": EXCHANGE_BYBIT,
        "environment": Environment.TESTNET,
        "status": CredentialStatus.ACTIVE,
        "permissions": KeyPermissions(
            read_only=False,
            permissions=frozenset({"ContractTrade.Order", "Spot.SpotTrade"}),
            expires_at=None,
            ip_restricted=False,
        ),
        "permissions_checked_at": USER.created_at,
        "admin_attested": False,
        "created_at": USER.created_at,
        **changes,
    }
    return Credential(**values)


@dataclass
class KeySpy:
    stored: list[tuple[str, str | None, ApiKeyPair, bool]] = field(default_factory=list)
    deleted: list[str | None] = field(default_factory=list)
    credential: Credential = field(default_factory=_credential)

    def commands(self) -> KeyCommands:
        return KeyCommands(
            add=lambda env, user, keys, attest: self._store("add", user, keys, attest),
            replace=lambda env, user, keys, attest: self._store("replace", user, keys, attest),
            delete=self.delete,
            show=lambda env, user: (
                USER,
                [self.credential, _credential(id="cred-0", status=CredentialStatus.DELETED)],
            ),
        )

    def _store(self, action: str, user: str | None, keys: ApiKeyPair, attest: bool) -> StoredCredential:
        self.stored.append((action, user, keys, attest))
        return StoredCredential(
            user=USER,
            credential=self.credential,
            assessment=KeyAssessment(profile=Profile.TRADE, warnings=(WARNING_NO_IP_WHITELIST,)),
            replaced_id="cred-0" if action == "replace" else None,
        )

    def delete(self, env_file: Path | None, user: str | None) -> Credential:
        self.deleted.append(user)
        return _credential(status=CredentialStatus.DELETED)


def _keys_app(spy: KeySpy) -> typer.Typer:
    return build_cli(ServeSpy(), InitSpy(), PolicySpy().commands(), AdminSpy().commands(), spy.commands())


@pytest.mark.req("FR-30", "FR-31")
def test_keys_add_prompts_without_echo_and_never_prints_the_key() -> None:
    spy = KeySpy()

    result = runner.invoke(
        _keys_app(spy),
        ["keys", "add", "--user", "local"],
        input=f"{API_KEY}\n{API_SECRET}\n",
    )

    assert result.exit_code == 0, result.output
    assert [(a, u, k.api_key, k.api_secret, at) for a, u, k, at in spy.stored] == [
        ("add", "local", API_KEY, API_SECRET, False)
    ]
    assert API_KEY not in result.output  # hide_input: the prompt does not echo what was typed
    assert API_SECRET not in result.output
    assert "Bybit API key:" in result.output
    assert "key cred-1 added for local (bybit, testnet): profile trade" in result.output
    assert "not bound to an IP address" in result.output
    assert "never shown again" in result.output


@pytest.mark.req("FR-30")
def test_keys_input_is_validated_without_echoing_it() -> None:
    spy = KeySpy()

    result = runner.invoke(_keys_app(spy), ["keys", "add"], input="short\nsecret with spaces inside\n")

    assert result.exit_code == 2
    assert spy.stored == []
    assert "api_key must be 8-256 printable characters" in result.output
    assert "secret with spaces" not in result.output


@pytest.mark.req("FR-49")
def test_keys_admin_attest_asks_for_explicit_confirmation() -> None:
    declined = KeySpy()
    accepted = KeySpy()

    no = runner.invoke(
        _keys_app(declined),
        ["keys", "add", "--admin-attest"],
        input=f"{API_KEY}\n{API_SECRET}\nn\n",
    )
    yes = runner.invoke(
        _keys_app(accepted),
        ["keys", "add", "--admin-attest"],
        input=f"{API_KEY}\n{API_SECRET}\ny\n",
    )

    assert no.exit_code == 1  # aborted
    assert declined.stored == []
    assert "NO withdrawal and NO transfer permission" in no.output
    assert yes.exit_code == 0, yes.output
    assert [attest for *_, attest in accepted.stored] == [True]


@pytest.mark.req("FR-30", "FR-29")
def test_keys_replace_delete_and_show() -> None:
    spy = KeySpy()
    app = _keys_app(spy)

    replaced = runner.invoke(app, ["keys", "replace"], input=f"{API_KEY}\n{API_SECRET}\n")
    refused = runner.invoke(app, ["keys", "delete"], input="n\n")
    deleted = runner.invoke(app, ["keys", "delete", "--yes"])
    shown = runner.invoke(app, ["keys", "show"])

    assert replaced.exit_code == 0, replaced.output
    assert [a for a, *_ in spy.stored] == ["replace"]
    assert "previous key cred-0 is deleted" in replaced.output
    assert refused.exit_code == 1
    assert deleted.exit_code == 0, deleted.output
    assert spy.deleted == [None]  # the local user
    assert "key cred-1 (bybit, testnet) deleted" in deleted.output
    assert shown.exit_code == 0, shown.output
    assert "key cred-1 (bybit, testnet): active" in shown.output
    assert "key cred-0 (bybit, testnet): deleted" in shown.output
    assert "ContractTrade.Order, Spot.SpotTrade" in shown.output
    assert API_KEY not in shown.output
