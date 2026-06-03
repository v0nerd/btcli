from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

from rich.prompt import Prompt

from bittensor_cli.src import COLORS
from bittensor_cli.src.bittensor.balances import Balance
from bittensor_cli.src.bittensor.chain_data import ColdkeySubnetLock, StakeInfo
from bittensor_cli.src.bittensor.locks import (
    BLOCKS_PER_DAY,
    LockState,
    available_to_unstake,
    roll_forward_lock,
)
from bittensor_cli.src.bittensor.utils import (
    console,
    create_table,
    group_subnets,
    json_console,
    millify_tao,
    print_error,
)
from bittensor_cli.src.commands.lock.common import print_lock_projection_graph

if TYPE_CHECKING:
    from bittensor_cli.src.bittensor.subtensor_interface import SubtensorInterface


PROJECTION_BUCKETS_DAYS = (30, 90, 365)
GRAPH_BUCKETS_DAYS = tuple(sorted(set(range(0, 366, 7)).union(PROJECTION_BUCKETS_DAYS)))


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

    rows, current_block, unlock_rate, maturity_rate = await _load_lock_rows(
        subtensor=subtensor,
        coldkey_ss58=coldkey_ss58,
        netuid=netuid,
    )

    if json_output:
        _print_json(rows, current_block, unlock_rate, maturity_rate)
        return

    if not rows:
        console.print("[dim]No active stake locks for this coldkey.[/dim]")
        return

    _print_table(
        rows,
        coldkey_ss58,
        subtensor.network,
        current_block,
        unlock_rate,
        maturity_rate,
        verbose,
    )


async def stake_lock_show(
    subtensor: "SubtensorInterface",
    coldkey_ss58: Optional[str],
    netuid: Optional[int],
    json_output: bool,
    verbose: bool = False,
    show_graph: bool = True,
) -> None:
    """Display one active stake lock and its lock projection."""
    if coldkey_ss58 is None:
        raise ValueError("coldkey_ss58 is required")

    rows, current_block, unlock_rate, maturity_rate = await _load_lock_rows(
        subtensor=subtensor,
        coldkey_ss58=coldkey_ss58,
        netuid=netuid,
    )

    if not rows:
        if netuid is not None:
            console.print(f"[dim]No active stake lock on netuid {netuid}.[/dim]")
            return
        console.print("[dim]No active stake locks for this coldkey.[/dim]")
        return

    if netuid is None:
        if json_output:
            print_error("Missing --netuid for JSON output.")
            return
        netuid = _prompt_locked_netuid(rows)

    rows = [row for row in rows if row.netuid == netuid]
    if not rows:
        console.print(f"[dim]No active stake lock on netuid {netuid}.[/dim]")
        return

    if json_output:
        _print_json(rows, current_block, unlock_rate, maturity_rate)
        return

    _print_table(
        rows,
        coldkey_ss58,
        subtensor.network,
        current_block,
        unlock_rate,
        maturity_rate,
        verbose,
    )

    if show_graph:
        _print_lock_row_graph(rows[0], current_block, unlock_rate, maturity_rate)


async def _load_lock_rows(
    subtensor: "SubtensorInterface",
    coldkey_ss58: str,
    netuid: Optional[int],
) -> tuple[list[_LockRow], int, int, int]:
    block_hash = await subtensor.substrate.get_chain_head()
    (
        locks_by_netuid,
        lock_rates,
        current_block,
        all_stakes,
    ) = await asyncio.gather(
        subtensor.get_coldkey_locks(coldkey_ss58=coldkey_ss58, block_hash=block_hash),
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
    rows = _build_lock_rows(
        locks_by_netuid=locks_by_netuid,
        owner_hotkeys_by_netuid=owner_hotkeys_by_netuid,
        total_alpha_by_netuid=_sum_staked_alpha_by_netuid(all_stakes or []),
        current_block=int(current_block),
        unlock_rate=unlock_rate,
        maturity_rate=maturity_rate,
    )
    return rows, int(current_block), unlock_rate, maturity_rate


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


def _prompt_locked_netuid(rows: list[_LockRow]) -> int:
    locked_netuids = sorted(row.netuid for row in rows)
    locked_range = group_subnets(locked_netuids)
    locked_set = set(locked_netuids)

    while True:
        selected = Prompt.ask(
            f"Enter netuid to show [dim](locked: {locked_range})[/dim]"
        ).strip()

        try:
            selected_netuid = int(selected)
        except ValueError:
            print_error("Please enter a valid netuid.")
            continue

        if selected_netuid in locked_set:
            console.print()
            return selected_netuid

        print_error(f"Please select a locked netuid: {locked_range}")


def _print_lock_row_graph(
    row: _LockRow,
    current_block: int,
    unlock_rate: int,
    maturity_rate: int,
) -> None:
    projected_locked: dict[int, int] = {}
    projected_conviction: dict[int, Decimal] = {}

    for days in GRAPH_BUCKETS_DAYS:
        future = roll_forward_lock(
            lock=row.rolled_lock,
            now=current_block + days * BLOCKS_PER_DAY,
            unlock_rate=unlock_rate,
            maturity_rate=maturity_rate,
            owner_lock=row.is_owner_hotkey_lock,
            perpetual_lock=row.is_perpetual,
        )
        projected_locked[days] = future.locked_mass
        projected_conviction[days] = future.conviction

    print_lock_projection_graph(
        netuid=row.netuid,
        projected_locked_rao=projected_locked,
        projected_conviction=projected_conviction,
        targets_owner_hotkey=row.is_owner_hotkey_lock,
        hint=None,
    )


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
        coldkey_ss58 if verbose else f"{coldkey_ss58[:6]}...{coldkey_ss58[-6:]}"
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

        hotkey_cell = row.hotkey if verbose else f"{row.hotkey[:6]}...{row.hotkey[-6:]}"
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

    json_console.print_json(data=out)
