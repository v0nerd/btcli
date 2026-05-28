from __future__ import annotations

import asyncio
from collections.abc import Iterable
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

import plotille
from bittensor_wallet import Wallet
from rich.text import Text

from bittensor_cli.src import COLORS
from bittensor_cli.src.bittensor.balances import Balance
from bittensor_cli.src.bittensor.chain_data import ColdkeySubnetLock
from bittensor_cli.src.bittensor.locks import LockState, roll_forward_lock
from bittensor_cli.src.bittensor.utils import (
    console,
    print_error,
    print_extrinsic_id,
    print_success,
)

if TYPE_CHECKING:
    from bittensor_cli.src.bittensor.subtensor_interface import SubtensorInterface


LOCK_MODES = {"decaying", "perpetual"}


def format_alpha(rao: int, netuid: int) -> str:
    return str(Balance.from_rao(rao).set_unit(netuid))


def short_ss58(ss58: Optional[str]) -> str:
    if not ss58:
        return "—"
    return f"{ss58[:6]}...{ss58[-6:]}"


def mode_name(is_perpetual: bool) -> str:
    return "perpetual" if is_perpetual else "decaying"


def mode_markup(is_perpetual: bool) -> str:
    mode = mode_name(is_perpetual)
    color = COLORS.P.ALPHA_IN if is_perpetual else COLORS.S.STAKE_AMOUNT
    return f"[{color}]{mode}[/{color}]"


def normalize_mode(mode: Optional[str]) -> Optional[str]:
    if mode is None:
        return None
    normalized = mode.strip().lower()
    return normalized if normalized in LOCK_MODES else None


def rolled_existing_lock(
    existing: Optional[ColdkeySubnetLock],
    current_block: int,
    unlock_rate: int,
    maturity_rate: int,
    owner_lock: bool = False,
) -> Optional[LockState]:
    """Roll an existing lock to the current block."""
    if existing is None:
        return None

    return roll_forward_lock(
        lock=existing.lock,
        now=current_block,
        unlock_rate=unlock_rate,
        maturity_rate=maturity_rate,
        owner_lock=owner_lock,
        perpetual_lock=existing.is_perpetual,
    )


def print_lock_projection_graph(
    netuid: int,
    projected_locked_rao: dict[int, int],
    projected_conviction: dict[int, Decimal],
    targets_owner_hotkey: bool,
    hint: Optional[str] = "use --no-graph to hide",
) -> None:
    days = sorted(projected_locked_rao)
    locked = [_alpha_float(projected_locked_rao[day]) for day in days]
    conviction = [_alpha_float(projected_conviction[day]) for day in days]

    max_y = max(locked + conviction)
    if max_y <= 0:
        return

    fig = plotille.Figure()
    fig.width = 60
    fig.height = 9
    fig.color_mode = "rgb"
    fig.background = None
    fig.origin = False
    fig.x_label = plotille.color("Days", fg=(186, 233, 143), mode="rgb")
    fig.y_label = plotille.color(
        f"Alpha ({Balance.get_unit(netuid)})",
        fg=(186, 233, 143),
        mode="rgb",
    )
    fig.x_ticks_fkt = lambda value, _next_value: f"{value:.0f}"
    fig.y_ticks_fkt = _graph_tick
    fig.set_x_limits(min_=0, max_=365)
    fig.set_y_limits(min_=0, max_=max_y * 1.05)

    if targets_owner_hotkey:
        fig.plot(
            days,
            locked,
            label="Locked = Conviction",
            interp="linear",
            lc="ffd166",
        )
    else:
        fig.plot(days, locked, label="Locked", interp="linear", lc="d09fe9")
        fig.plot(days, conviction, label="Conviction", interp="linear", lc="afefff")

    hint_text = f" [dim]({hint})[/dim]" if hint else ""
    console.print(
        f"\n[{COLORS.G.HEADER}]Lock projection[/{COLORS.G.HEADER}]{hint_text}"
    )
    console.print(Text.from_ansi(fig.show(legend=True)))

    if targets_owner_hotkey:
        console.print(
            "[dim]Owner hotkey target: one line represents both locked alpha "
            "and conviction.[/dim]"
        )

    console.print()


def _alpha_float(rao: int | Decimal) -> float:
    return float(Balance.from_rao(int(rao)).tao)


def _graph_tick(value: float, _next_value: float) -> str:
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}k"
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"


async def get_subnet_owner_hotkey(
    subtensor: "SubtensorInterface",
    netuid: int,
    block_hash: Optional[str],
) -> Optional[str]:
    """Read the owner hotkey for a subnet."""
    owner_hotkey = await subtensor.query(
        module="SubtensorModule",
        storage_function="SubnetOwnerHotkey",
        params=[netuid],
        block_hash=block_hash,
    )
    return str(owner_hotkey) if owner_hotkey is not None else None


async def get_subnet_owner_hotkeys(
    subtensor: "SubtensorInterface",
    netuids: Iterable[int],
    block_hash: Optional[str],
) -> dict[int, Optional[str]]:
    """Read owner hotkeys for multiple subnets."""
    sorted_netuids = sorted(set(netuids))
    if not sorted_netuids:
        return {}

    owner_hotkeys = await asyncio.gather(
        *[
            get_subnet_owner_hotkey(
                subtensor=subtensor,
                netuid=netuid,
                block_hash=block_hash,
            )
            for netuid in sorted_netuids
        ]
    )
    return dict(zip(sorted_netuids, owner_hotkeys))


def is_subnet_owner_hotkey_lock(
    hotkey_ss58: str,
    owner_hotkey_ss58: Optional[str],
) -> bool:
    return owner_hotkey_ss58 is not None and hotkey_ss58 == owner_hotkey_ss58


async def submit_lock_action(
    subtensor: "SubtensorInterface",
    wallet: Wallet,
    signer_ss58: str,
    call_function: str,
    call_params: dict,
    era: int,
    proxy: Optional[str],
    verb: str,
) -> bool:
    return await submit_lock_actions(
        subtensor=subtensor,
        wallet=wallet,
        signer_ss58=signer_ss58,
        calls=[{"call_function": call_function, "call_params": call_params}],
        era=era,
        proxy=proxy,
        verb=verb,
    )


async def submit_lock_actions(
    subtensor: "SubtensorInterface",
    wallet: Wallet,
    signer_ss58: str,
    calls: list[dict],
    era: int,
    proxy: Optional[str],
    verb: str,
) -> bool:
    """Submit lock extrinsics with sequential nonces."""
    next_nonce = await subtensor.substrate.get_account_next_index(signer_ss58)

    for offset, call_spec in enumerate(calls):
        call = await subtensor.substrate.compose_call(
            call_module="SubtensorModule",
            call_function=call_spec["call_function"],
            call_params=call_spec["call_params"],
        )

        success, err_msg, response = await subtensor.sign_and_send_extrinsic(
            call=call,
            wallet=wallet,
            era={"period": era},
            proxy=proxy,
            nonce=next_nonce + offset,
        )

        if not success:
            print_error(f"{verb} failed: {err_msg}")
            return False

        await print_extrinsic_id(response)

    print_success(f"[dark_sea_green3]{verb} succeeded.")
    return True
