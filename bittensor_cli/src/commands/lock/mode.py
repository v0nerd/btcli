from __future__ import annotations

import asyncio
import json
from typing import Optional, TYPE_CHECKING

from bittensor_wallet import Wallet

from bittensor_cli.src import COLORS
from bittensor_cli.src.bittensor.chain_data import ColdkeySubnetLock
from bittensor_cli.src.bittensor.locks import LockState
from bittensor_cli.src.bittensor.utils import (
    confirm_action,
    console,
    create_table,
    print_error,
    unlock_key,
)
from bittensor_cli.src.commands.lock.common import (
    format_alpha,
    get_subnet_owner_hotkey,
    is_subnet_owner_hotkey_lock,
    mode_markup,
    mode_name,
    normalize_mode,
    rolled_existing_lock,
    submit_lock_action,
)

if TYPE_CHECKING:
    from bittensor_cli.src.bittensor.subtensor_interface import SubtensorInterface


async def lock_mode(
    wallet: Wallet,
    subtensor: "SubtensorInterface",
    netuid: int,
    mode: Optional[str],
    prompt: bool,
    decline: bool,
    quiet: bool,
    era: int,
    proxy: Optional[str],
    json_output: bool = False,
) -> bool:
    """View or change a coldkey's lock mode on one subnet."""
    normalized_mode = normalize_mode(mode)
    if normalized_mode is None and mode is not None:
        print_error("Invalid --mode. Use 'decaying' or 'perpetual'.")
        return False

    coldkey_ss58 = proxy or wallet.coldkeypub.ss58_address
    signer_ss58 = wallet.coldkeypub.ss58_address
    block_hash = await subtensor.substrate.get_chain_head()
    (
        locks_by_netuid,
        stored_is_perpetual,
        lock_rates,
        current_block_,
        owner_hotkey,
    ) = await asyncio.gather(
        subtensor.get_coldkey_locks(coldkey_ss58=coldkey_ss58, block_hash=block_hash),
        subtensor.get_coldkey_lock_is_perpetual(
            coldkey_ss58=coldkey_ss58,
            netuid=netuid,
            block_hash=block_hash,
        ),
        subtensor.get_lock_rates(block_hash=block_hash),
        subtensor.substrate.get_block_number(block_hash=block_hash),
        get_subnet_owner_hotkey(
            subtensor=subtensor,
            netuid=netuid,
            block_hash=block_hash,
        ),
    )
    unlock_rate, maturity_rate = lock_rates

    active_lock = locks_by_netuid.get(netuid)
    current_is_perpetual = (
        active_lock.is_perpetual if active_lock is not None else stored_is_perpetual
    )
    target_is_perpetual = (
        normalized_mode == "perpetual" if normalized_mode is not None else None
    )
    active_owner_lock = active_lock is not None and is_subnet_owner_hotkey_lock(
        active_lock.hotkey, owner_hotkey
    )

    rolled_lock = rolled_existing_lock(
        existing=active_lock,
        current_block=int(current_block_),
        unlock_rate=unlock_rate,
        maturity_rate=maturity_rate,
        owner_lock=active_owner_lock,
    )

    if json_output:
        _print_lock_mode_json(
            netuid=netuid,
            current_is_perpetual=current_is_perpetual,
            target_is_perpetual=target_is_perpetual,
            active_lock=active_lock,
            rolled_lock=rolled_lock,
        )
    else:
        _print_lock_mode_status(
            network=subtensor.network,
            coldkey_ss58=coldkey_ss58,
            netuid=netuid,
            current_is_perpetual=current_is_perpetual,
            target_is_perpetual=target_is_perpetual,
            active_lock=active_lock,
            rolled_lock=rolled_lock,
        )

    already_confirmed = False

    if target_is_perpetual is None:
        if json_output or not prompt:
            return True

        target_is_perpetual = not current_is_perpetual
        target_mode = mode_name(target_is_perpetual)
        if not confirm_action(
            f"Change lock mode to {target_mode}?",
            default=False,
            decline=decline,
            quiet=quiet,
        ):
            console.print("[dim]No change submitted.[/dim]")
            return True
        already_confirmed = True

    if target_is_perpetual == current_is_perpetual:
        if not json_output:
            console.print(
                f"[dim]Already {mode_name(current_is_perpetual)}; nothing to submit.[/dim]"
            )
        return True

    if (
        prompt
        and not already_confirmed
        and not confirm_action(
            "Submit lock mode change?", default=False, decline=decline, quiet=quiet
        )
    ):
        console.print("[dim]Aborted.[/dim]")
        return False

    if not unlock_key(wallet).success:
        return False

    return await submit_lock_action(
        subtensor=subtensor,
        wallet=wallet,
        signer_ss58=signer_ss58,
        call_function="set_perpetual_lock",
        call_params={"netuid": netuid, "enabled": target_is_perpetual},
        era=era,
        proxy=proxy,
        verb="Set perpetual" if target_is_perpetual else "Set decaying",
    )


def _print_lock_mode_status(
    network: str,
    coldkey_ss58: str,
    netuid: int,
    current_is_perpetual: bool,
    target_is_perpetual: Optional[bool],
    active_lock: Optional[ColdkeySubnetLock],
    rolled_lock: Optional[LockState],
) -> None:
    """Render the current and target lock mode."""
    title = (
        f"\n[{COLORS.G.HEADER}]Lock Mode[/{COLORS.G.HEADER}]\n"
        f"[{COLORS.G.SUBHEAD}]Network: {network} • Coldkey: "
        f"[{COLORS.G.CK}]{coldkey_ss58}[/{COLORS.G.CK}]"
        f"[/{COLORS.G.SUBHEAD}]\n"
    )
    table = create_table(title=title, show_footer=False)
    table.add_column("Netuid", justify="center", style=COLORS.G.NETUID)
    table.add_column("Current Mode", justify="center")
    if target_is_perpetual is not None:
        table.add_column("New Mode", justify="center")
    table.add_column("Active Lock", justify="center")
    table.add_column("Locked", justify="right", style=COLORS.P.ALPHA_IN)
    table.add_column("Hotkey", style=COLORS.G.HK)

    row = [str(netuid), mode_markup(current_is_perpetual)]

    if target_is_perpetual is not None:
        row.append(mode_markup(target_is_perpetual))

    row.extend(
        [
            "yes" if active_lock is not None else "no",
            format_alpha(rolled_lock.locked_mass, netuid) if rolled_lock else "—",
            active_lock.hotkey if active_lock else "—",
        ]
    )

    table.add_row(*row)
    console.print(table)
    console.print()

    if active_lock is None:
        console.print(
            "[dim]No active lock exists on this subnet. The stored mode applies "
            "when you create a lock.[/dim]"
        )

    if target_is_perpetual is None:
        console.print(
            "[dim]Decaying locks free locked alpha over time. Perpetual locks keep "
            "alpha locked until switched back to decaying.[/dim]"
        )
    elif target_is_perpetual:
        console.print(
            "[dim]Changing to perpetual stops locked alpha from decaying.[/dim]"
        )
    else:
        console.print(
            "[dim]Changing to decaying lets locked alpha decay over time.[/dim]"
        )
    console.print()


def _print_lock_mode_json(
    netuid: int,
    current_is_perpetual: bool,
    target_is_perpetual: Optional[bool],
    active_lock: Optional[ColdkeySubnetLock],
    rolled_lock: Optional[LockState],
) -> None:
    console.print(
        json.dumps(
            {
                "netuid": netuid,
                "current_mode": mode_name(current_is_perpetual),
                "target_mode": (
                    mode_name(target_is_perpetual)
                    if target_is_perpetual is not None
                    else None
                ),
                "active_lock": active_lock is not None,
                "locked_rao": rolled_lock.locked_mass if rolled_lock else 0,
                "hotkey": active_lock.hotkey if active_lock else None,
            }
        )
    )
