from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

from bittensor_cli.src import COLORS
from bittensor_cli.src.bittensor.balances import Balance
from bittensor_cli.src.bittensor.chain_data import ColdkeySubnetLock, StakeInfo
from bittensor_cli.src.bittensor.locks import (
    BLOCKS_PER_DAY,
    LockState,
    available_to_unstake,
    roll_forward_lock,
)
from bittensor_cli.src.bittensor.utils import console, create_table, millify_tao

if TYPE_CHECKING:
    from bittensor_cli.src.bittensor.subtensor_interface import SubtensorInterface


PROJECTION_BUCKETS_DAYS = (30, 90, 365)


def _perpetual_conviction_pct_of_cap(conviction: Decimal, locked_mass: int) -> float:
    """Display-only maturity hint for perpetual locks."""
    if locked_mass == 0:
        return 0.0
    return min(float(conviction) / float(locked_mass) * 100.0, 100.0)


@dataclass(frozen=True, slots=True)
class _LockRow:
    netuid: int
    hotkey: str
    rolled_lock: LockState
    is_perpetual: bool
    is_owner_hotkey_lock: bool
    available_alpha_rao: int


async def stake_locks(
    subtensor: "SubtensorInterface",
    coldkey_ss58: Optional[str],
    netuid: Optional[int],
    json_output: bool,
    verbose: bool = False,
) -> None:
    """Display active stake locks for a coldkey."""
    if coldkey_ss58 is None:
        raise ValueError("coldkey_ss58 is required")

    block_hash = await subtensor.substrate.get_chain_head()
    (
        locks_by_netuid,
        lock_rates,
        current_block_number,
        all_stakes,
    ) = await asyncio.gather(
        subtensor.get_coldkey_locks(
            coldkey_ss58=coldkey_ss58, block_hash=block_hash
        ),
        subtensor.get_lock_rates(block_hash=block_hash),
        subtensor.substrate.get_block_number(block_hash=block_hash),
        subtensor.get_stake_for_coldkey(
            coldkey_ss58=coldkey_ss58, block_hash=block_hash
        ),
    )
    unlock_rate, maturity_rate = lock_rates

    if netuid is not None:
        locks_by_netuid = {
            lock_netuid: lock
            for lock_netuid, lock in locks_by_netuid.items()
            if lock_netuid == netuid
        }

    owner_hotkeys_by_netuid = await _get_owner_hotkeys_by_netuid(
        subtensor=subtensor,
        netuids=locks_by_netuid.keys(),
        block_hash=block_hash,
    )
    total_alpha_by_netuid = _sum_staked_alpha_by_netuid(all_stakes or [])
    rows = _build_lock_rows(
        locks_by_netuid=locks_by_netuid,
        owner_hotkeys_by_netuid=owner_hotkeys_by_netuid,
        total_alpha_by_netuid=total_alpha_by_netuid,
        current_block=current_block_number,
        unlock_rate=unlock_rate,
        maturity_rate=maturity_rate,
    )

    if json_output:
        _print_json(rows, current_block_number, unlock_rate, maturity_rate)
        return

    if not rows:
        console.print("[dim]No active stake locks for this coldkey.[/dim]")
        return

    _print_table(
        rows,
        coldkey_ss58,
        subtensor.network,
        current_block_number,
        unlock_rate,
        maturity_rate,
        verbose,
    )


def _sum_staked_alpha_by_netuid(all_stakes: Iterable[StakeInfo]) -> dict[int, int]:
    total_alpha_by_netuid: dict[int, int] = {}
    for stake in all_stakes:
        total_alpha_by_netuid[stake.netuid] = (
            total_alpha_by_netuid.get(stake.netuid, 0) + stake.stake.rao
        )
    return total_alpha_by_netuid


async def _get_owner_hotkeys_by_netuid(
    subtensor: "SubtensorInterface",
    netuids: Iterable[int],
    block_hash: str,
) -> dict[int, str]:
    sorted_netuids = sorted(netuids)
    if not sorted_netuids:
        return {}

    owner_hotkeys = await asyncio.gather(
        *[
            subtensor.query(
                module="SubtensorModule",
                storage_function="SubnetOwnerHotkey",
                params=[netuid],
                block_hash=block_hash,
            )
            for netuid in sorted_netuids
        ]
    )
    return dict(zip(sorted_netuids, owner_hotkeys))


def _build_lock_rows(
    locks_by_netuid: dict[int, ColdkeySubnetLock],
    owner_hotkeys_by_netuid: dict[int, str],
    total_alpha_by_netuid: dict[int, int],
    current_block: int,
    unlock_rate: int,
    maturity_rate: int,
) -> list[_LockRow]:
    """Roll raw lock storage into display rows."""
    rows: list[_LockRow] = []

    for netuid, coldkey_lock in sorted(locks_by_netuid.items()):
        is_owner_hotkey_lock = coldkey_lock.hotkey == owner_hotkeys_by_netuid.get(
            netuid
        )

        rolled_lock = roll_forward_lock(
            lock=coldkey_lock.lock,
            now=current_block,
            unlock_rate=unlock_rate,
            maturity_rate=maturity_rate,
            owner_lock=is_owner_hotkey_lock,
            perpetual_lock=coldkey_lock.is_perpetual,
        )

        available_alpha_rao = available_to_unstake(
            total_alpha_rao=total_alpha_by_netuid.get(netuid, 0),
            rolled_locked_mass=rolled_lock.locked_mass,
        )

        rows.append(
            _LockRow(
                netuid=netuid,
                hotkey=coldkey_lock.hotkey,
                rolled_lock=rolled_lock,
                is_perpetual=coldkey_lock.is_perpetual,
                is_owner_hotkey_lock=is_owner_hotkey_lock,
                available_alpha_rao=available_alpha_rao,
            )
        )

    return rows


def _format_alpha_rao(rao: int, netuid: int, verbose: bool) -> str:
    alpha = Balance.from_rao(rao).set_unit(netuid)
    if verbose:
        return f"{alpha}"
    return f"{millify_tao(alpha.tao)} {Balance.get_unit(netuid)}"


def _format_projected_freed(
    row: _LockRow,
    days: int,
    current_block: int,
    unlock_rate: int,
    maturity_rate: int,
    verbose: bool,
) -> str:
    if row.is_perpetual:
        return "—"

    future = roll_forward_lock(
        lock=row.rolled_lock,
        now=current_block + days * BLOCKS_PER_DAY,
        unlock_rate=unlock_rate,
        maturity_rate=maturity_rate,
        owner_lock=row.is_owner_hotkey_lock,
        perpetual_lock=False,
    )
    freed = max(0, row.rolled_lock.locked_mass - future.locked_mass)
    return _format_alpha_rao(freed, row.netuid, verbose) if freed > 0 else "—"


def _print_table(
    rows: list[_LockRow],
    coldkey_ss58: str,
    network: str,
    current_block: int,
    unlock_rate: int,
    maturity_rate: int,
    verbose: bool,
) -> None:
    coldkey_cell = (
        coldkey_ss58
        if verbose
        else f"{coldkey_ss58[:6]}...{coldkey_ss58[-6:]}"
    )

    table = create_table(
        title=(
            f"\n[{COLORS.G.HEADER}]Stake Locks"
            f"\nNetwork: [{COLORS.G.SUBHEAD}]{network}[/{COLORS.G.SUBHEAD}]"
            f" • Coldkey: [{COLORS.G.CK}]{coldkey_cell}[/{COLORS.G.CK}]\n"
        ),
        show_footer=False,
    )
    table.add_column("Netuid", style="grey89", justify="center")
    table.add_column("Mode", justify="center")
    table.add_column(
        "Locked",
        style=COLORS.S.STAKE_ALPHA,
        justify="center",
    )
    table.add_column(
        "Conviction",
        style=COLORS.P.EXTRA_2,
        justify="center",
    )
    table.add_column("Maturity", justify="center")
    table.add_column(
        "Available",
        style=COLORS.S.STAKE_ALPHA,
        justify="center",
    )
    for days in PROJECTION_BUCKETS_DAYS:
        table.add_column(
            f"+{days}d Free",
            style=COLORS.P.ALPHA_IN,
            justify="center",
        )
    table.add_column(
        "Hotkey",
        style=COLORS.G.HK,
        justify="center",
    )
    table.add_column("Note", style="dim", justify="center")

    for row in rows:
        mode = "perpetual" if row.is_perpetual else "decaying"
        maturity = "—"

        if row.is_perpetual:
            pct = _perpetual_conviction_pct_of_cap(
                row.rolled_lock.conviction, row.rolled_lock.locked_mass
            )
            maturity = f"{pct:.0f}%"

        hotkey_cell = (
            row.hotkey if verbose else f"{row.hotkey[:6]}...{row.hotkey[-6:]}"
        )
        note = "owner hotkey" if row.is_owner_hotkey_lock else ""

        table.add_row(
            str(row.netuid),
            mode,
            _format_alpha_rao(row.rolled_lock.locked_mass, row.netuid, verbose),
            _format_alpha_rao(int(row.rolled_lock.conviction), row.netuid, verbose),
            maturity,
            _format_alpha_rao(row.available_alpha_rao, row.netuid, verbose),
            *[
                _format_projected_freed(
                    row,
                    days,
                    current_block,
                    unlock_rate,
                    maturity_rate,
                    verbose,
                )
                for days in PROJECTION_BUCKETS_DAYS
            ],
            hotkey_cell,
            note,
        )

    console.print(table)


def _print_json(
    rows: list[_LockRow],
    current_block: int,
    unlock_rate: int,
    maturity_rate: int,
) -> None:
    out: dict = {
        "current_block": current_block,
        "unlock_rate": unlock_rate,
        "maturity_rate": maturity_rate,
        "locks": [],
    }
    for row in rows:
        projections: dict = {}

        for days in PROJECTION_BUCKETS_DAYS:
            future_block = current_block + days * BLOCKS_PER_DAY
            future = roll_forward_lock(
                lock=row.rolled_lock,
                now=future_block,
                unlock_rate=unlock_rate,
                maturity_rate=maturity_rate,
                owner_lock=row.is_owner_hotkey_lock,
                perpetual_lock=row.is_perpetual,
            )
            freed = max(0, row.rolled_lock.locked_mass - future.locked_mass)
            projections[f"+{days}d"] = {
                "locked_mass_rao": future.locked_mass,
                "freed_rao": freed,
                "conviction_raw": str(future.conviction),
            }

        out["locks"].append(
            {
                "netuid": row.netuid,
                "hotkey": row.hotkey,
                "locked_alpha_rao": row.rolled_lock.locked_mass,
                "mode": "perpetual" if row.is_perpetual else "decaying",
                "owner_hotkey_lock": row.is_owner_hotkey_lock,
                "conviction_raw": str(row.rolled_lock.conviction),
                "conviction_alpha_eq_rao": int(row.rolled_lock.conviction),
                "conviction_pct_of_cap": (
                    _perpetual_conviction_pct_of_cap(
                        row.rolled_lock.conviction, row.rolled_lock.locked_mass
                    )
                    if row.is_perpetual
                    else None
                ),
                "available_to_unstake_rao": row.available_alpha_rao,
                "projections": projections,
            }
        )

    print(json.dumps(out, indent=2))
