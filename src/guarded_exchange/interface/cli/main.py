"""Command-line entrypoint. Commands receive their behaviour from the composition root."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import typer

from guarded_exchange.application.policy import PolicyView
from guarded_exchange.domain.actions import ALL_ACTION_KINDS
from guarded_exchange.domain.audit import canonical_decimal
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
    patch_to_payload,
    settings_to_payload,
)

Transport = Literal["stdio"]
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
    return "\n".join([header, "", *table])


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


def build_cli(serve: Serve, init: Init, policy: PolicyCommands) -> typer.Typer:
    app = typer.Typer(name="guarded-exchange", no_args_is_help=True, add_completion=False)

    @app.callback()
    def main() -> None:
        """Guarded MCP server for a crypto exchange."""

    @app.command("serve")
    def serve_command(
        transport: Annotated[
            str, typer.Option(help="MCP transport. Only stdio is available in this version.")
        ] = "stdio",
        env_file: EnvFileOpt = None,
    ) -> None:
        """Run the MCP server (stdout = protocol, stderr = JSON logs)."""
        if transport != "stdio":
            raise typer.BadParameter("only 'stdio' is supported yet", param_hint="--transport")
        serve("stdio", env_file)

    @app.command("init")
    def init_command(env_file: EnvFileOpt = None) -> None:
        """Create or migrate the database, the master key and the local user. Safe to re-run."""
        typer.echo(render_init_report(init(env_file)))

    app.add_typer(_policy_cli(policy), name="policy")
    admin = typer.Typer(no_args_is_help=True, help="Admin commands (service channel only, never MCP).")
    admin.add_typer(_admin_policy_cli(policy), name="policy")
    app.add_typer(admin, name="admin")
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

    return app
