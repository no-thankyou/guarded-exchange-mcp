"""Command-line entrypoint. Commands receive their behaviour from the composition root."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import typer

from guarded_exchange.application.admin import IssuedToken, RevokedToken
from guarded_exchange.application.credentials import StoredCredential
from guarded_exchange.application.policy import OverridesReset, PolicyView
from guarded_exchange.domain.actions import ALL_ACTION_KINDS
from guarded_exchange.domain.audit import canonical_decimal, canonical_time
from guarded_exchange.domain.credentials import WARNING_NO_IP_WHITELIST, ApiKeyPair, Credential
from guarded_exchange.domain.errors import InvalidParams
from guarded_exchange.domain.policy import (
    UNSET,
    InstrumentRef,
    PolicyPatch,
    Profile,
    Unset,
    parse_action_kinds,
    parse_decimal,
    parse_instrument_ref,
    parse_policy_fields,
    patch_to_payload,
    settings_to_payload,
)
from guarded_exchange.domain.users import User

Transport = Literal["stdio", "http"]
TRANSPORTS: tuple[Transport, ...] = ("stdio", "http")
Serve = Callable[[Transport, Path | None], None]


@dataclass(frozen=True, slots=True)
class InitReport:
    """What ``init`` did; the composition root fills it, this layer only renders it."""

    db_path: Path
    schema_revision: str
    master_key_file: Path
    master_key_created: bool
    user_name: str
    user_id: str
    user_created: bool


Init = Callable[[Path | None], InitReport]


@dataclass(frozen=True, slots=True)
class PolicyCommands:
    """Policy behaviour from the composition root. ``user=None`` means the local user."""

    show: Callable[[Path | None, str | None], PolicyView]
    tighten: Callable[[Path | None, PolicyPatch], PolicyView]
    set_ceiling: Callable[[Path | None, str | None, PolicyPatch], PolicyView]
    reset_overrides: Callable[[Path | None, str, frozenset[str] | None], OverridesReset]


@dataclass(frozen=True, slots=True)
class AdminCommands:
    """User and access-token behaviour from the composition root (remote mode, ADR-004)."""

    create_user: Callable[[Path | None, str], User]
    list_users: Callable[[Path | None], Sequence[User]]
    issue_token: Callable[[Path | None, str], IssuedToken]
    revoke_token: Callable[[Path | None, str], RevokedToken]


@dataclass(frozen=True, slots=True)
class KeyCommands:
    """Exchange key behaviour from the composition root (FR-30). ``user=None``: the local user.

    ``add``/``replace`` take ``(env_file, user, keys, admin_attest)``.
    """

    add: Callable[[Path | None, str | None, ApiKeyPair, bool], StoredCredential]
    replace: Callable[[Path | None, str | None, ApiKeyPair, bool], StoredCredential]
    delete: Callable[[Path | None, str | None], Credential]
    show: Callable[[Path | None, str | None], tuple[User, Sequence[Credential]]]


_ENV_FILE_HELP = "dotenv file with GX_* settings (else $GX_ENV_FILE; no implicit ./.env)."
EnvFileOpt = Annotated[Path | None, typer.Option(help=_ENV_FILE_HELP, dir_okay=False)]
UserOpt = Annotated[str | None, typer.Option(help="User name (default: the local user).")]


def render_init_report(report: InitReport) -> str:
    key_state = "created - back it up" if report.master_key_created else "kept existing"
    user_state = "created" if report.user_created else "already existed"
    return "\n".join(
        [
            f"database:   {report.db_path} (schema {report.schema_revision})",
            f"master key: {report.master_key_file} ({key_state})",
            f"local user: {report.user_name} [{report.user_id}] ({user_state})",
        ]
    )


def _user_line(user: User) -> str:
    return f"{user.name} [{user.id}] ({user.status.value}, {user.environment.value})"


def render_users(users: Sequence[User]) -> str:
    if not users:
        return "no users"
    rows = [("name", "id", "status", "environment")]
    rows += [(u.name, u.id, u.status.value, u.environment.value) for u in users]
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row[:3], widths, strict=True)) + "  " + row[3]
        for row in rows
    )


def render_issued_token(issued: IssuedToken) -> str:
    return "\n".join(
        [
            f"user:     {_user_line(issued.user)}",
            f"token id: {issued.token_id}",
            f"token:    {issued.secret}",
            "",
            "Shown once: only its SHA-256 hash is stored, it cannot be displayed again.",
            "The client sends it as the header 'Authorization: Bearer <token>'.",
            f"Revoke it with: guarded-exchange admin token revoke {issued.token_id}",
        ]
    )


def render_revoked_token(revoked: RevokedToken) -> str:
    state = "was already revoked" if revoked.already_revoked else "revoked"
    return f"token {revoked.token.id} (user id {revoked.token.user_id}) {state}"


# --- exchange keys ------------------------------------------------------------------------

_WARNINGS = {
    WARNING_NO_IP_WHITELIST: "the key is not bound to an IP address: anyone who obtains it can use it from "
    "anywhere, and Bybit expires such keys after 90 days. Bind it to this server's IP if you can.",
    "permissions_not_verified": "the exchange could not confirm the key's permissions; it was accepted on "
    "the admin's attestation and will be re-checked when possible.",
}


def _permissions_line(credential: Credential) -> str:
    if credential.permissions is None:
        return "permissions: not verified (accepted on admin attestation)"
    names = ", ".join(sorted(credential.permissions.permissions)) or "(none)"
    mode = "read-only" if credential.permissions.read_only else "read-write"
    checked = canonical_time(credential.permissions_checked_at) if credential.permissions_checked_at else "-"
    expires = (
        canonical_time(credential.permissions.expires_at) if credential.permissions.expires_at else "never"
    )
    return f"permissions: {mode}; {names} (checked {checked}; key expires {expires})"


def render_stored_credential(stored: StoredCredential, action: str) -> str:
    credential = stored.credential
    lines = [
        f"key {credential.id} {action} for {stored.user.name} "
        f"({credential.exchange}, {credential.environment.value}): profile {stored.assessment.profile.value}",
        _permissions_line(credential),
    ]
    if stored.replaced_id is not None:
        lines.append(f"previous key {stored.replaced_id} is deleted and will not be used again")
    lines += [f"warning: {_WARNINGS.get(w, w)}" for w in stored.assessment.warnings]
    lines.append("The key and secret are stored encrypted and are never shown again.")
    return "\n".join(lines)


def render_credentials(user: User, credentials: Sequence[Credential]) -> str:
    if not credentials:
        return f"user {user.name} has no exchange keys"
    blocks = [
        f"key {c.id} ({c.exchange}, {c.environment.value}): {c.status.value}, created "
        f"{canonical_time(c.created_at)}\n  {_permissions_line(c)}"
        for c in credentials
    ]
    return "\n".join([f"user: {_user_line(user)}", *blocks])


def _read_keys() -> ApiKeyPair:
    """Prompt for the key and the secret without echo (FR-30: never via the AI client)."""
    api_key = typer.prompt("Bybit API key", hide_input=True).strip()
    api_secret = typer.prompt("Bybit API secret", hide_input=True).strip()
    try:
        return ApiKeyPair(api_key=api_key, api_secret=api_secret)
    except InvalidParams as exc:  # the message names the field, never the value
        raise typer.BadParameter(exc.message, param_hint=exc.details.get("field")) from None


_ATTEST_QUESTION = (
    "Admin attestation (test environment only, used only if the exchange cannot report the key's "
    "permissions): do you confirm this key has NO withdrawal and NO transfer permission?"
)
AttestOpt = Annotated[
    bool,
    typer.Option(
        "--admin-attest",
        help="Admin: accept a test-environment key whose permissions the exchange cannot report, after "
        "confirming it has no withdrawal or transfer permission (journaled). Never accepted on mainnet.",
    ),
]
YesOpt = Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation.")]


def _keys_cli(commands: KeyCommands) -> typer.Typer:
    app = typer.Typer(
        no_args_is_help=True,
        help="Exchange API keys (typed here, never through the AI client; stored encrypted).",
    )

    def _store(action: str, user: str | None, admin_attest: bool, yes: bool, env_file: Path | None) -> None:
        keys = _read_keys()
        if admin_attest and not yes:
            typer.confirm(_ATTEST_QUESTION, abort=True)
        store = commands.replace if action == "replaced" else commands.add
        typer.echo(render_stored_credential(store(env_file, user, keys, admin_attest), action))

    @app.command("add")
    def add_command(
        user: UserOpt = None,
        admin_attest: AttestOpt = False,
        yes: YesOpt = False,
        env_file: EnvFileOpt = None,
    ) -> None:
        """Add the user's key: checks its permissions on the exchange, refuses withdrawal/transfer."""
        _store("added", user, admin_attest, yes, env_file)

    @app.command("replace")
    def replace_command(
        user: UserOpt = None,
        admin_attest: AttestOpt = False,
        yes: YesOpt = False,
        env_file: EnvFileOpt = None,
    ) -> None:
        """Replace the user's key with a new one (same checks); the old one is deleted."""
        _store("replaced", user, admin_attest, yes, env_file)

    @app.command("delete")
    def delete_command(user: UserOpt = None, yes: YesOpt = False, env_file: EnvFileOpt = None) -> None:
        """Delete the user's active key: it is never used for new actions again."""
        if not yes:
            typer.confirm("Delete the active exchange key?", abort=True)
        deleted = commands.delete(env_file, user)
        typer.echo(f"key {deleted.id} ({deleted.exchange}, {deleted.environment.value}) deleted")

    @app.command("show")
    def show_command(user: UserOpt = None, env_file: EnvFileOpt = None) -> None:
        """List the user's keys and their last checked permissions (never the key itself)."""
        typer.echo(render_credentials(*commands.show(env_file, user)))

    return app


# --- policy rendering and parsing ---------------------------------------------------------


_ALL_ACTIONS = sorted(kind.value for kind in ALL_ACTION_KINDS)


def _cell(value: object) -> str:
    if value == _ALL_ACTIONS:
        return "all"
    match value:
        case None:
            return "off"
        case bool():
            return "yes" if value else "no"
        case Decimal():
            return canonical_decimal(value)
        case list():
            return ", ".join(map(str, value)) if value else "(none)"
    return str(value)


def render_policy(view: PolicyView) -> str:
    """The admin ceiling, the user's own settings and what applies: one row per setting."""
    ceiling = settings_to_payload(view.ceiling)
    own: Mapping[str, object] = patch_to_payload(view.overrides)
    effective = settings_to_payload(view.effective)
    source = "default" if view.ceiling_is_default else "set by admin"
    rows = [("setting", f"ceiling ({source})", "user setting", "effective")]
    rows += [
        (name, _cell(value), _cell(own[name]) if name in own else "-", _cell(effective[name]))
        for name, value in ceiling.items()
    ]
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    table = [
        "  ".join(cell.ljust(width) for cell, width in zip(row[:3], widths, strict=True)) + "  " + row[3]
        for row in rows
    ]
    user = view.user
    header = f"user: {user.name} [{user.id}] ({user.status.value}, {user.environment.value})"
    note = ["", "note: the exchange key is read-only, so the effective profile is read_only"]
    return "\n".join([header, "", *table, *(note if view.key_read_only else [])])


def _set[T](value: T | None) -> T | Unset:
    return UNSET if value is None else value


def _decimal(field: str, raw: str | None) -> Decimal | Unset:
    return UNSET if raw is None else parse_decimal(field, raw)


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyOptions:
    """Raw command-line values shared by ``policy tighten`` and ``admin policy set-ceiling``."""

    profile: Profile | None = None
    allow: list[str] | None = None
    allow_none: bool = False
    max_order_notional_usdt: str | None = None
    max_leverage: str | None = None
    slippage_pct: str | None = None
    confirm: list[str] | None = None
    confirm_above_notional_usdt: str | None = None
    confirmation_ttl_s: int | None = None
    trust_client_ui: bool | None = None
    prefer_external_channel: bool | None = None
    unconfirmed_max_actions: int | None = None
    unconfirmed_window_s: int | None = None
    max_pending: int | None = None

    def to_patch(self) -> PolicyPatch:
        """Text -> domain values; a bad value raises ``InvalidParams`` naming the field."""
        if self.allow and self.allow_none:
            raise typer.BadParameter("use either --allow or --allow-none", param_hint="--allow")
        allowlist: frozenset[InstrumentRef] | None = None
        if self.allow_none:
            allowlist = frozenset()
        elif self.allow:
            allowlist = frozenset(parse_instrument_ref(item) for item in self.allow)
        threshold: Decimal | Unset | None = UNSET
        if self.confirm_above_notional_usdt is not None:
            raw = self.confirm_above_notional_usdt.strip().lower()
            threshold = None if raw == "none" else parse_decimal("confirm_above_notional_usdt", raw)
        return PolicyPatch(
            profile=_set(self.profile),
            allowlist=_set(allowlist),
            max_order_notional_usdt=_decimal("max_order_notional_usdt", self.max_order_notional_usdt),
            max_leverage=_decimal("max_leverage", self.max_leverage),
            slippage_pct=_decimal("slippage_pct", self.slippage_pct),
            confirm_actions=parse_action_kinds(self.confirm) if self.confirm else UNSET,
            confirm_above_notional_usdt=threshold,
            confirmation_ttl_s=_set(self.confirmation_ttl_s),
            trust_client_ui=_set(self.trust_client_ui),
            prefer_external_channel=_set(self.prefer_external_channel),
            unconfirmed_max_actions=_set(self.unconfirmed_max_actions),
            unconfirmed_window_s=_set(self.unconfirmed_window_s),
            max_pending=_set(self.max_pending),
        )


def _patch(options: PolicyOptions) -> PolicyPatch:
    """A bad value is a usage error (exit code 2, names the setting), not a traceback."""
    try:
        return options.to_patch()
    except InvalidParams as exc:
        raise typer.BadParameter(exc.message, param_hint=exc.details.get("field")) from exc


ProfileOpt = Annotated[Profile | None, typer.Option(help="read_only or trade.")]
AllowOpt = Annotated[
    list[str] | None,
    typer.Option("--allow", help="Allowed instrument 'category:SYMBOL' (repeat); replaces the list."),
]
AllowNoneOpt = Annotated[bool, typer.Option("--allow-none", help="Allow no instrument at all.")]
NotionalOpt = Annotated[str | None, typer.Option(help="Max risk-increasing order size, USDT.")]
LeverageOpt = Annotated[str | None, typer.Option(help="Max leverage, x.")]
SlippageOpt = Annotated[str | None, typer.Option(help="Market order price tolerance, percent (<= 3).")]
ConfirmOpt = Annotated[
    list[str] | None,
    typer.Option("--confirm", help="Action that always needs confirmation (repeat), or 'all' / 'none'."),
]
ConfirmAboveOpt = Annotated[
    str | None, typer.Option(help="Also confirm any order at least this large, USDT; 'none' = off.")
]
TtlOpt = Annotated[int | None, typer.Option(help="Confirmation request lifetime, seconds (<= 600).")]
TrustUiOpt = Annotated[
    bool | None,
    typer.Option("--trust-client-ui/--no-trust-client-ui", help="Accept confirmation in the AI client UI."),
]
PreferExternalOpt = Annotated[
    bool | None,
    typer.Option(
        "--prefer-external-channel/--no-prefer-external-channel",
        help="Confirm in the external channel even when the client UI is trusted.",
    ),
]
UnconfirmedOpt = Annotated[int | None, typer.Option(help="Unconfirmed risk-increasing actions per window.")]
WindowOpt = Annotated[int | None, typer.Option(help="Window of --unconfirmed-max-actions, seconds.")]
PendingOpt = Annotated[int | None, typer.Option(help="Confirmations waiting at the same time.")]


# --- commands -----------------------------------------------------------------------------


def build_cli(
    serve: Serve, init: Init, policy: PolicyCommands, admin_commands: AdminCommands, keys: KeyCommands
) -> typer.Typer:
    app = typer.Typer(name="guarded-exchange", no_args_is_help=True, add_completion=False)

    @app.callback()
    def main() -> None:
        """Guarded MCP server for a crypto exchange."""

    @app.command("serve")
    def serve_command(
        transport: Annotated[
            str,
            typer.Option(
                help="stdio (local mode, the local user) or http (remote mode: Streamable HTTP at "
                "GX_HTTP_HOST:GX_HTTP_PORT/mcp, bearer tokens from `admin token issue`)."
            ),
        ] = "stdio",
        env_file: EnvFileOpt = None,
    ) -> None:
        """Run the MCP server (JSON logs on stderr; on stdio, stdout = protocol)."""
        if transport not in TRANSPORTS:
            raise typer.BadParameter("must be 'stdio' or 'http'", param_hint="--transport")
        serve("http" if transport == "http" else "stdio", env_file)

    @app.command("init")
    def init_command(env_file: EnvFileOpt = None) -> None:
        """Create or migrate the database, the master key and the local user. Safe to re-run."""
        typer.echo(render_init_report(init(env_file)))

    app.add_typer(_policy_cli(policy), name="policy")
    app.add_typer(_keys_cli(keys), name="keys")
    admin = typer.Typer(no_args_is_help=True, help="Admin commands (service channel only, never MCP).")
    admin.add_typer(_admin_policy_cli(policy), name="policy")
    admin.add_typer(_admin_user_cli(admin_commands), name="user")
    admin.add_typer(_admin_token_cli(admin_commands), name="token")
    app.add_typer(admin, name="admin")
    return app


def _admin_user_cli(commands: AdminCommands) -> typer.Typer:
    app = typer.Typer(no_args_is_help=True, help="Users of the server.")

    @app.command("create")
    def create_command(
        name: Annotated[str, typer.Argument(help="Lowercase name, e.g. alice.")], env_file: EnvFileOpt = None
    ) -> None:
        """Create a user: active, test environment, read-only until you raise their ceiling."""
        typer.echo(f"user created: {_user_line(commands.create_user(env_file, name))}")

    @app.command("list")
    def list_command(env_file: EnvFileOpt = None) -> None:
        """List all users."""
        typer.echo(render_users(commands.list_users(env_file)))

    return app


def _admin_token_cli(commands: AdminCommands) -> typer.Typer:
    app = typer.Typer(no_args_is_help=True, help="Bearer access tokens of the remote (HTTP) mode.")

    @app.command("issue")
    def issue_command(
        user: Annotated[str, typer.Option(help="User the token acts as.")], env_file: EnvFileOpt = None
    ) -> None:
        """Issue a new token for an active user. The token is printed once and never stored."""
        typer.echo(render_issued_token(commands.issue_token(env_file, user)))

    @app.command("revoke")
    def revoke_command(
        token_id: Annotated[str, typer.Argument(help="Token id printed by `admin token issue`.")],
        env_file: EnvFileOpt = None,
    ) -> None:
        """Revoke a token: the next request with it gets HTTP 401."""
        typer.echo(render_revoked_token(commands.revoke_token(env_file, token_id)))

    return app


def _policy_cli(policy: PolicyCommands) -> typer.Typer:
    app = typer.Typer(no_args_is_help=True, help="Your policy: show it or tighten it.")

    @app.command("show")
    def show_command(user: UserOpt = None, env_file: EnvFileOpt = None) -> None:
        """Show the admin ceiling, the user's own settings and the effective policy."""
        typer.echo(render_policy(policy.show(env_file, user)))

    @app.command("tighten")
    def tighten_command(
        *,
        profile: ProfileOpt = None,
        allow: AllowOpt = None,
        allow_none: AllowNoneOpt = False,
        max_order_notional_usdt: NotionalOpt = None,
        max_leverage: LeverageOpt = None,
        slippage_pct: SlippageOpt = None,
        confirm: ConfirmOpt = None,
        confirm_above_notional_usdt: ConfirmAboveOpt = None,
        confirmation_ttl_s: TtlOpt = None,
        trust_client_ui: TrustUiOpt = None,
        prefer_external_channel: PreferExternalOpt = None,
        unconfirmed_max_actions: UnconfirmedOpt = None,
        unconfirmed_window_s: WindowOpt = None,
        max_pending: PendingOpt = None,
        env_file: EnvFileOpt = None,
    ) -> None:
        """Make the local user's policy stricter. Anything that would loosen it is refused."""
        options = PolicyOptions(
            profile=profile,
            allow=allow,
            allow_none=allow_none,
            max_order_notional_usdt=max_order_notional_usdt,
            max_leverage=max_leverage,
            slippage_pct=slippage_pct,
            confirm=confirm,
            confirm_above_notional_usdt=confirm_above_notional_usdt,
            confirmation_ttl_s=confirmation_ttl_s,
            trust_client_ui=trust_client_ui,
            prefer_external_channel=prefer_external_channel,
            unconfirmed_max_actions=unconfirmed_max_actions,
            unconfirmed_window_s=unconfirmed_window_s,
            max_pending=max_pending,
        )
        typer.echo(render_policy(policy.tighten(env_file, _patch(options))))

    return app


def _admin_policy_cli(policy: PolicyCommands) -> typer.Typer:
    app = typer.Typer(no_args_is_help=True, help="Users' policy ceilings.")

    @app.command("set-ceiling")
    def set_ceiling_command(
        *,
        user: UserOpt = None,
        profile: ProfileOpt = None,
        allow: AllowOpt = None,
        allow_none: AllowNoneOpt = False,
        max_order_notional_usdt: NotionalOpt = None,
        max_leverage: LeverageOpt = None,
        slippage_pct: SlippageOpt = None,
        confirm: ConfirmOpt = None,
        confirm_above_notional_usdt: ConfirmAboveOpt = None,
        confirmation_ttl_s: TtlOpt = None,
        trust_client_ui: TrustUiOpt = None,
        prefer_external_channel: PreferExternalOpt = None,
        unconfirmed_max_actions: UnconfirmedOpt = None,
        unconfirmed_window_s: WindowOpt = None,
        max_pending: PendingOpt = None,
        env_file: EnvFileOpt = None,
    ) -> None:
        """Change a user's ceiling (raise or lower). Given settings change, the rest stay."""
        options = PolicyOptions(
            profile=profile,
            allow=allow,
            allow_none=allow_none,
            max_order_notional_usdt=max_order_notional_usdt,
            max_leverage=max_leverage,
            slippage_pct=slippage_pct,
            confirm=confirm,
            confirm_above_notional_usdt=confirm_above_notional_usdt,
            confirmation_ttl_s=confirmation_ttl_s,
            trust_client_ui=trust_client_ui,
            prefer_external_channel=prefer_external_channel,
            unconfirmed_max_actions=unconfirmed_max_actions,
            unconfirmed_window_s=unconfirmed_window_s,
            max_pending=max_pending,
        )
        typer.echo(render_policy(policy.set_ceiling(env_file, user, _patch(options))))

    @app.command("reset-overrides")
    def reset_overrides_command(
        *,
        user: Annotated[str, typer.Option(help="User whose own settings to remove.")],
        field: Annotated[
            list[str] | None,
            typer.Option("--field", help="Only this setting (repeat), e.g. profile; default: all of them."),
        ] = None,
        yes: YesOpt = False,
        env_file: EnvFileOpt = None,
    ) -> None:
        """Remove a user's own settings (from `policy tighten`): those fields follow the ceiling again."""
        try:
            fields = parse_policy_fields(field) if field else None
        except InvalidParams as exc:
            raise typer.BadParameter(exc.message, param_hint="--field") from exc
        own = patch_to_payload(policy.show(env_file, user).overrides)
        to_reset = [name for name in own if fields is None or name in fields]
        if not to_reset:
            typer.echo(f"user {user} has no own settings to reset; nothing changed")
            return
        if not yes:
            typer.echo(render_settings_to_reset(user, own, to_reset))
            _confirm_or_exit(f"Reset these settings of {user} to the admin ceiling?")
            # Reset only what the admin saw: a setting tightened while the question was open stays.
            fields = frozenset(to_reset)
        typer.echo(render_overrides_reset(policy.reset_overrides(env_file, user, fields)))

    return app


def render_settings_to_reset(user: str, own: Mapping[str, object], names: Sequence[str]) -> str:
    lines = [f"user setting of {user} to remove: {name} = {_cell(own[name])}" for name in names]
    return "\n".join(lines)


def render_overrides_reset(result: OverridesReset) -> str:
    if not result.reset_fields:
        return f"user {result.view.user.name} has no own settings to reset; nothing changed"
    names = ", ".join(result.reset_fields)
    return "\n".join([f"reset to the ceiling: {names}", "", render_policy(result.view)])


def _confirm_or_exit(question: str) -> None:
    """Ask; no answer (no terminal, end of input) or 'no' changes nothing and exits with code 1."""
    try:
        confirmed = typer.confirm(question, default=False)
    except typer.Abort:
        confirmed = False
    if not confirmed:
        typer.echo("not confirmed: nothing changed (pass --yes to skip the question)", err=True)
        raise typer.Exit(1)
