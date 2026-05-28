from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

from bittensor_wallet import Wallet
from rich.prompt import Prompt

from bittensor_cli.src import COLORS
from bittensor_cli.src.bittensor.locks import LockState, roll_forward_lock
from bittensor_cli.src.bittensor.utils import (
    confirm_action,
    console,
    create_table,
    is_valid_ss58_address,
    json_console,
    print_error,
    unlock_key,
)
from bittensor_cli.src.commands.lock.common import (
    format_alpha,
    get_subnet_owner_hotkey,
    is_subnet_owner_hotkey_lock,
    mode_markup,
    mode_name,
    rolled_existing_lock,
    short_ss58,
    submit_lock_action,
)

if TYPE_CHECKING:
    from bittensor_cli.src.bittensor.subtensor_interface import SubtensorInterface


@dataclass(frozen=True)
class _LockMovePreview:
    network: str
    coldkey: str
    netuid: int
    is_perpetual: bool
    origin_targets_owner_hotkey: bool
    destination_targets_owner_hotkey: bool
    locked_rao: int
    conviction: Decimal
    conviction_after: Decimal
    origin_hotkey: str
    destination_hotkey: str
    origin_owner: Optional[str]
    destination_owner: Optional[str]
    conviction_resets: bool


async def lock_move(
    wallet: Wallet,
    subtensor: "SubtensorInterface",
    netuid: int,
    destination_hotkey_ss58: Optional[str],
    prompt: bool,
    decline: bool,
    quiet: bool,
    era: int,
    proxy: Optional[str],
    json_output: bool = False,
) -> bool:
    """Move an existing lock's conviction target to another hotkey."""
    coldkey_ss58 = proxy or wallet.coldkeypub.ss58_address
    signer_ss58 = wallet.coldkeypub.ss58_address

    block_hash = await subtensor.substrate.get_chain_head()
    (
        locks_by_netuid,
        lock_rates,
        current_block_,
        owner_hotkey,
    ) = await asyncio.gather(
        subtensor.get_coldkey_locks(coldkey_ss58=coldkey_ss58, block_hash=block_hash),
        subtensor.get_lock_rates(block_hash=block_hash),
        subtensor.substrate.get_block_number(block_hash=block_hash),
        get_subnet_owner_hotkey(
            subtensor=subtensor,
            netuid=netuid,
            block_hash=block_hash,
        ),
    )
    unlock_rate, maturity_rate = lock_rates

    existing = locks_by_netuid.get(netuid)
    if existing is None:
        print_error(
            f"No lock to move on netuid {netuid}. Use `btcli lock add` "
            "to create one first.\nAborted, nothing was submitted."
        )
        return False

    origin_hotkey = existing.hotkey

    if destination_hotkey_ss58 is None:
        if not prompt:
            print_error("Missing --destination-hotkey. Aborted, nothing was submitted.")
            return False
        destination_hotkey_ss58 = Prompt.ask(
            "Enter destination hotkey SS58 [dim](or Press Enter to view conviction table)[/dim]",
            default="",
            show_default=False,
        ).strip()
        if destination_hotkey_ss58 == "":
            from bittensor_cli.src.commands.subnets import subnets

            destination_hotkey_ss58 = await subnets.subnet_conviction(
                subtensor=subtensor,
                netuid=netuid,
                limit=12,
                json_output=False,
                verbose=True,
                hotkey_selection=True,
                show_summary=False,
            )
            if destination_hotkey_ss58 is None:
                console.print("[dim]No hotkey selected. Aborted.[/dim]")
                return False

    if not is_valid_ss58_address(destination_hotkey_ss58):
        print_error(
            f"Invalid destination hotkey SS58 address: {destination_hotkey_ss58}"
        )
        return False

    if origin_hotkey == destination_hotkey_ss58:
        print_error(
            f"Destination hotkey is the same as the current lock hotkey on "
            f"netuid {netuid}. Nothing to do.\nAborted, nothing was submitted."
        )
        return False

    origin_owner, dest_owner = await asyncio.gather(
        subtensor.get_hotkey_owner(origin_hotkey, block_hash=block_hash),
        subtensor.get_hotkey_owner(destination_hotkey_ss58, block_hash=block_hash),
    )

    conviction_resets = origin_owner != dest_owner
    origin_targets_owner_hotkey = is_subnet_owner_hotkey_lock(
        origin_hotkey,
        owner_hotkey,
    )
    destination_targets_owner_hotkey = is_subnet_owner_hotkey_lock(
        destination_hotkey_ss58,
        owner_hotkey,
    )

    rolled = rolled_existing_lock(
        existing=existing,
        current_block=int(current_block_),
        unlock_rate=unlock_rate,
        maturity_rate=maturity_rate,
        owner_lock=origin_targets_owner_hotkey,
    )
    if rolled is None:
        print_error(
            "No lock to move after roll-forward. Aborted, nothing was submitted."
        )
        return False

    moved_lock = LockState(
        locked_mass=rolled.locked_mass,
        conviction=Decimal(0) if conviction_resets else rolled.conviction,
        last_update=int(current_block_),
    )
    rolled_destination = roll_forward_lock(
        lock=moved_lock,
        now=int(current_block_),
        unlock_rate=unlock_rate,
        maturity_rate=maturity_rate,
        owner_lock=destination_targets_owner_hotkey,
        perpetual_lock=existing.is_perpetual,
    )

    preview = _LockMovePreview(
        network=subtensor.network,
        coldkey=coldkey_ss58,
        netuid=netuid,
        is_perpetual=existing.is_perpetual,
        origin_targets_owner_hotkey=origin_targets_owner_hotkey,
        destination_targets_owner_hotkey=destination_targets_owner_hotkey,
        locked_rao=rolled.locked_mass,
        conviction=rolled.conviction,
        conviction_after=rolled_destination.conviction,
        origin_hotkey=origin_hotkey,
        destination_hotkey=destination_hotkey_ss58,
        origin_owner=origin_owner,
        destination_owner=dest_owner,
        conviction_resets=conviction_resets,
    )

    if json_output:
        _print_lock_move_json(preview)
    else:
        _print_lock_move_preview(preview)

    if preview.destination_targets_owner_hotkey:
        confirm_message = "Move lock to owner hotkey target?"
    elif preview.conviction_resets:
        confirm_message = "Move lock and reset conviction?"
    else:
        confirm_message = "Move lock target?"

    if prompt and not confirm_action(
        confirm_message, default=False, decline=decline, quiet=quiet
    ):
        console.print("[dim]Aborted.[/dim]")
        return False

    if not unlock_key(wallet).success:
        return False

    return await submit_lock_action(
        subtensor=subtensor,
        wallet=wallet,
        signer_ss58=signer_ss58,
        call_function="move_lock",
        call_params={
            "destination_hotkey": destination_hotkey_ss58,
            "netuid": netuid,
        },
        era=era,
        proxy=proxy,
        verb="Move lock",
    )


def _print_lock_move_preview(preview: _LockMovePreview) -> None:
    """Render the lock move confirmation preview."""
    title = (
        f"\n[{COLORS.G.HEADER}]Lock Move Preview[/{COLORS.G.HEADER}]\n"
        f"[{COLORS.G.SUBHEAD}]Network: {preview.network} • Coldkey: "
        f"[{COLORS.G.CK}]{preview.coldkey}[/{COLORS.G.CK}]"
        f"[/{COLORS.G.SUBHEAD}]\n"
    )
    table = create_table(title=title, show_footer=False)
    table.add_column("Netuid", justify="center", style=COLORS.G.NETUID)
    table.add_column("Mode", justify="center")
    table.add_column("Locked", justify="right", style=COLORS.P.ALPHA_IN)
    table.add_column("Conviction", justify="right", style=COLORS.P.ALPHA_IN)
    table.add_column("From Hotkey", style=COLORS.G.HK)
    table.add_column("To Hotkey", style=COLORS.G.HK)
    table.add_column("Conviction After", justify="right")
    table.add_column("Effect", justify="center")

    conviction_after = format_alpha(int(preview.conviction_after), preview.netuid)
    if preview.destination_targets_owner_hotkey:
        effect = "[yellow]pins[/yellow]"
    elif preview.conviction_resets:
        effect = "[yellow]resets[/yellow]"
    else:
        effect = "[dark_sea_green3]preserved[/dark_sea_green3]"

    table.add_row(
        str(preview.netuid),
        mode_markup(preview.is_perpetual),
        format_alpha(preview.locked_rao, preview.netuid),
        format_alpha(int(preview.conviction), preview.netuid),
        short_ss58(preview.origin_hotkey),
        short_ss58(preview.destination_hotkey),
        conviction_after,
        effect,
    )
    console.print(table)
    console.print()

    console.print(
        "[dim]Locked alpha stays on the original stake position. Only the "
        "conviction target changes.[/dim]"
    )

    if preview.origin_targets_owner_hotkey:
        console.print(
            "[dim]Origin targets the subnet owner hotkey, so conviction is "
            "pinned to locked alpha.[/dim]"
        )

    if preview.destination_targets_owner_hotkey:
        console.print(
            "[dim]Destination targets the subnet owner hotkey, so conviction "
            "after the move is pinned to locked alpha.[/dim]"
        )

    console.print(
        f"[dim]From owner: [{COLORS.G.CK}]{short_ss58(preview.origin_owner)}"
        f"[/{COLORS.G.CK}] • To owner: [{COLORS.G.CK}]"
        f"{short_ss58(preview.destination_owner)}[/{COLORS.G.CK}][/dim]"
    )

    if preview.conviction_resets and preview.destination_targets_owner_hotkey:
        console.print(
            "[yellow]These hotkeys are owned by different coldkeys, so earned "
            "conviction is cleared first. The owner hotkey target then pins "
            "conviction to locked alpha.[/yellow]"
        )
    elif preview.conviction_resets:
        console.print(
            "[yellow]These hotkeys are owned by different coldkeys, so current "
            "conviction resets to 0. Locked alpha survives and starts maturing "
            "again from zero.[/yellow]"
        )
    console.print()


def _print_lock_move_json(preview: _LockMovePreview) -> None:
    json_console.print_json(
        data={
            "network": preview.network,
            "coldkey": preview.coldkey,
            "netuid": preview.netuid,
            "mode": mode_name(preview.is_perpetual),
            "origin_targets_owner_hotkey": preview.origin_targets_owner_hotkey,
            "destination_targets_owner_hotkey": preview.destination_targets_owner_hotkey,
            "locked_rao": preview.locked_rao,
            "conviction_rao": str(preview.conviction),
            "conviction_after_rao": str(preview.conviction_after),
            "origin_hotkey": preview.origin_hotkey,
            "destination_hotkey": preview.destination_hotkey,
            "origin_owner": preview.origin_owner,
            "destination_owner": preview.destination_owner,
            "conviction_resets": preview.conviction_resets,
        }
    )
