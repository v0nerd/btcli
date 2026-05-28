from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

import plotille
from bittensor_wallet import Wallet
from rich.prompt import Prompt
from rich.text import Text

from bittensor_cli.src import COLORS
from bittensor_cli.src.bittensor.balances import Balance
from bittensor_cli.src.bittensor.chain_data import ColdkeySubnetLock
from bittensor_cli.src.bittensor.locks import (
    BLOCKS_PER_DAY,
    LockState,
    available_to_unstake,
    roll_forward_lock,
)
from bittensor_cli.src.bittensor.utils import (
    confirm_action,
    console,
    create_table,
    is_valid_ss58_address,
    print_error,
    unlock_key,
)
from bittensor_cli.src.commands.lock.common import (
    format_alpha,
    get_subnet_owner_hotkey,
    get_subnet_owner_hotkeys,
    is_subnet_owner_hotkey_lock,
    normalize_mode,
    rolled_existing_lock,
    submit_lock_actions,
)

if TYPE_CHECKING:
    from bittensor_cli.src.bittensor.subtensor_interface import SubtensorInterface


PROJECTION_BUCKETS_DAYS = (30, 90, 365)
GRAPH_BUCKETS_DAYS = tuple(
    sorted(set(range(0, 366, 7)).union(PROJECTION_BUCKETS_DAYS))
)


@dataclass(frozen=True)
class _StakePosition:
    hotkey: str
    netuid: int
    stake_rao: int


@dataclass(frozen=True)
class _LockAddPreview:
    netuid: int
    hotkey: str
    is_perpetual: bool
    targets_owner_hotkey: bool
    current_locked_rao: int
    adding_rao: int
    new_locked_rao: int
    available_after_rao: int
    projected_freed_rao: dict[int, int]
    projected_locked_rao: dict[int, int]
    projected_conviction: dict[int, Decimal]
    existing_lock: bool
    mode_change_needed: bool


async def lock_add(
    wallet: Wallet,
    subtensor: "SubtensorInterface",
    netuid: Optional[int] = None,
    hotkey_ss58: Optional[str] = None,
    amount: Optional[float] = None,
    mode: Optional[str] = None,
    prompt: bool = True,
    decline: bool = False,
    quiet: bool = False,
    era: int = 64,
    proxy: Optional[str] = None,
    json_output: bool = False,
    show_graph: bool = True,
    amount_rao: Optional[int] = None,
) -> bool:
    """Create or top up a stake lock."""
    coldkey_ss58 = proxy or wallet.coldkeypub.ss58_address
    signer_ss58 = wallet.coldkeypub.ss58_address
    lock_amount_rao = (
        int(amount_rao)
        if amount_rao is not None
        else int(Balance.from_tao(amount).rao)
        if amount is not None
        else None
    )
    prompt_for_missing = prompt and not json_output

    block_hash = await subtensor.substrate.get_chain_head()
    (
        locks_by_netuid,
        all_stakes,
        lock_rates,
        current_block_,
    ) = await asyncio.gather(
        subtensor.get_coldkey_locks(coldkey_ss58=coldkey_ss58, block_hash=block_hash),
        subtensor.get_stake_for_coldkey(
            coldkey_ss58=coldkey_ss58, block_hash=block_hash
        ),
        subtensor.get_lock_rates(block_hash=block_hash),
        subtensor.substrate.get_block_number(block_hash=block_hash),
    )

    unlock_rate, maturity_rate = lock_rates
    current_block = int(current_block_)

    stake_positions = _stake_positions(all_stakes or [])
    total_alpha_by_netuid = _total_alpha_by_netuid(stake_positions)

    owner_hotkeys_by_netuid = await get_subnet_owner_hotkeys(
        subtensor=subtensor,
        netuids=locks_by_netuid.keys(),
        block_hash=block_hash,
    )

    if netuid is None:
        if not prompt_for_missing:
            print_error("Missing --netuid. Aborted, nothing was submitted.")
            return False

        netuid = _prompt_netuid(
            total_alpha_by_netuid=total_alpha_by_netuid,
            locks_by_netuid=locks_by_netuid,
            owner_hotkeys_by_netuid=owner_hotkeys_by_netuid,
            current_block=current_block,
            unlock_rate=unlock_rate,
            maturity_rate=maturity_rate,
        )
        if netuid is None:
            return False

    total_alpha_rao = total_alpha_by_netuid.get(netuid, 0)
    if total_alpha_rao <= 0:
        print_error(
            f"No stake found on netuid {netuid} for this coldkey. "
            "You need alpha stake on the subnet before you can lock it."
        )
        return False

    owner_hotkey = owner_hotkeys_by_netuid.get(netuid)
    if owner_hotkey is None:
        owner_hotkey = await get_subnet_owner_hotkey(
            subtensor=subtensor,
            netuid=netuid,
            block_hash=block_hash,
        )

    existing_lock = locks_by_netuid.get(netuid)
    existing_targets_owner_hotkey = (
        existing_lock is not None
        and is_subnet_owner_hotkey_lock(existing_lock.hotkey, owner_hotkey)
    )

    rolled_existing_lock_state = rolled_existing_lock(
        existing=existing_lock,
        current_block=current_block,
        unlock_rate=unlock_rate,
        maturity_rate=maturity_rate,
        owner_lock=existing_targets_owner_hotkey,
    )

    current_locked_rao = (
        rolled_existing_lock_state.locked_mass
        if rolled_existing_lock_state
        else 0
    )
    available_to_lock_rao = available_to_unstake(
        total_alpha_rao=total_alpha_rao,
        rolled_locked_mass=current_locked_rao,
    )

    if existing_lock is not None and hotkey_ss58 is None:
        hotkey_ss58 = existing_lock.hotkey

    if existing_lock is not None and existing_lock.hotkey != hotkey_ss58:
        print_error(
            f"Can't add to lock on netuid {netuid}: you already have a lock "
            f"to hotkey {existing_lock.hotkey[:6]}…{existing_lock.hotkey[-4:]}. The "
            "chain enforces one lock per coldkey per subnet, and top-ups "
            "must target the same hotkey.\n"
            f"To change the locked hotkey, use `btcli lock move "
            f"--netuid {netuid} --destination-hotkey {hotkey_ss58}` first.\n"
            "Aborted, nothing was submitted."
        )
        return False

    if hotkey_ss58 is None:
        if not prompt_for_missing:
            print_error("Missing --hotkey. Aborted, nothing was submitted.")
            return False
        hotkey_ss58 = _prompt_hotkey(stake_positions, netuid)
        if hotkey_ss58 is None:
            return False

    if not is_valid_ss58_address(hotkey_ss58):
        print_error(f"Invalid hotkey SS58 address: {hotkey_ss58}")
        return False

    normalized_mode = normalize_mode(mode)
    if normalized_mode is None and mode is not None:
        print_error("Invalid --mode. Use 'decaying' or 'perpetual'.")
        return False

    mode_change_needed = False
    if existing_lock is not None:
        selected_is_perpetual = existing_lock.is_perpetual
        if normalized_mode is not None and (
            normalized_mode == "perpetual"
        ) != selected_is_perpetual:
            print_error(
                "This subnet already has an active lock. `btcli lock add` "
                "will not change its mode during a top-up.\n"
                "Use `btcli lock mode` first if you want to change "
                "between decaying and perpetual."
            )
            return False
    else:
        stored_is_perpetual = await subtensor.get_coldkey_lock_is_perpetual(
            coldkey_ss58=coldkey_ss58,
            netuid=netuid,
            block_hash=block_hash,
        )
        if normalized_mode is None:
            if prompt_for_missing:
                normalized_mode = _prompt_lock_mode(stored_is_perpetual)
            else:
                normalized_mode = "perpetual" if stored_is_perpetual else "decaying"
        selected_is_perpetual = normalized_mode == "perpetual"
        mode_change_needed = selected_is_perpetual != stored_is_perpetual

    if lock_amount_rao is None or lock_amount_rao <= 0:
        if not prompt_for_missing:
            print_error("Missing --amount. Aborted, nothing was submitted.")
            return False
        lock_amount_rao = _prompt_lock_amount(
            max_amount_rao=available_to_lock_rao,
            netuid=netuid,
        )
        if lock_amount_rao is None:
            return False

    if lock_amount_rao <= 0:
        print_error("Amount must be greater than 0. Aborted, nothing was submitted.")
        return False

    if lock_amount_rao > available_to_lock_rao:
        amount_b = Balance.from_rao(lock_amount_rao).set_unit(netuid)
        available_b = Balance.from_rao(available_to_lock_rao).set_unit(netuid)
        total_b = Balance.from_rao(total_alpha_rao).set_unit(netuid)
        locked_b = Balance.from_rao(current_locked_rao).set_unit(netuid)
        print_error(
            f"Can't lock {amount_b}: only {available_b} is available to lock "
            f"on netuid {netuid} ({locked_b} of {total_b} is already locked).\n"
            "Aborted, nothing was submitted."
        )
        return False

    selected_targets_owner_hotkey = is_subnet_owner_hotkey_lock(
        hotkey_ss58,
        owner_hotkey,
    )
    preview = _build_lock_add_preview(
        netuid=netuid,
        hotkey=hotkey_ss58,
        amount_rao=lock_amount_rao,
        total_alpha_rao=total_alpha_rao,
        current_locked_rao=current_locked_rao,
        current_conviction=(
            rolled_existing_lock_state.conviction
            if rolled_existing_lock_state
            else Decimal(0)
        ),
        current_block=current_block,
        unlock_rate=unlock_rate,
        maturity_rate=maturity_rate,
        is_perpetual=selected_is_perpetual,
        targets_owner_hotkey=selected_targets_owner_hotkey,
        existing_lock=existing_lock is not None,
        mode_change_needed=mode_change_needed,
    )

    if json_output:
        _print_lock_add_json(preview)
    else:
        _print_lock_add_preview(preview, show_graph=show_graph)

    confirm_message = (
        "Submit lock top-up?" if preview.existing_lock else "Create stake lock?"
    )
    if prompt and not confirm_action(
        confirm_message, default=False, decline=decline, quiet=quiet
    ):
        console.print("[dim]Aborted.[/dim]")
        return False

    if not unlock_key(wallet).success:
        return False

    calls = []

    if mode_change_needed:
        calls.append(
            {
                "call_function": "set_perpetual_lock",
                "call_params": {"netuid": netuid, "enabled": selected_is_perpetual},
            }
        )

    calls.append(
        {
            "call_function": "lock_stake",
            "call_params": {
                "netuid": netuid,
                "hotkey": hotkey_ss58,
                "amount": lock_amount_rao,
            },
        }
    )

    return await submit_lock_actions(
        subtensor=subtensor,
        wallet=wallet,
        signer_ss58=signer_ss58,
        calls=calls,
        era=era,
        proxy=proxy,
        verb="Lock add",
    )


async def lock_stake(
    wallet: Wallet,
    subtensor: "SubtensorInterface",
    netuid: int,
    hotkey_ss58: str,
    amount_rao: int,
    prompt: bool,
    decline: bool,
    quiet: bool,
    era: int,
    proxy: Optional[str],
) -> bool:
    """Submit lock_stake through the lock add path."""
    return await lock_add(
        wallet=wallet,
        subtensor=subtensor,
        netuid=netuid,
        hotkey_ss58=hotkey_ss58,
        amount_rao=amount_rao,
        mode=None,
        prompt=prompt,
        decline=decline,
        quiet=quiet,
        era=era,
        proxy=proxy,
        json_output=False,
        show_graph=False,
    )


def _stake_positions(all_stakes: list) -> list[_StakePosition]:
    return [
        _StakePosition(
            hotkey=str(stake.hotkey_ss58),
            netuid=int(stake.netuid),
            stake_rao=int(stake.stake.rao),
        )
        for stake in all_stakes
    ]


def _total_alpha_by_netuid(positions: list[_StakePosition]) -> dict[int, int]:
    totals: dict[int, int] = {}
    for position in positions:
        totals[position.netuid] = totals.get(position.netuid, 0) + position.stake_rao
    return totals


def _prompt_netuid(
    total_alpha_by_netuid: dict[int, int],
    locks_by_netuid: dict[int, ColdkeySubnetLock],
    owner_hotkeys_by_netuid: dict[int, Optional[str]],
    current_block: int,
    unlock_rate: int,
    maturity_rate: int,
) -> Optional[int]:
    candidates = [
        netuid
        for netuid, total in sorted(total_alpha_by_netuid.items())
        if total > 0
    ]
    if not candidates:
        print_error("No subnet stake found for this coldkey.")
        return None

    table = create_table(
        title=f"\n[{COLORS.G.HEADER}]Choose Subnet to Lock\n",
        show_footer=False,
    )
    table.add_column("Netuid", justify="center", style=COLORS.G.NETUID)
    table.add_column("Stake", justify="right", style=COLORS.S.STAKE_ALPHA)
    table.add_column("Locked", justify="right", style=COLORS.P.ALPHA_IN)
    table.add_column("Available", justify="right", style=COLORS.S.STAKE_AMOUNT)
    table.add_column("Mode", justify="center")

    for candidate_netuid in candidates:
        existing_lock = locks_by_netuid.get(candidate_netuid)
        targets_owner_hotkey = (
            existing_lock is not None
            and is_subnet_owner_hotkey_lock(
                existing_lock.hotkey,
                owner_hotkeys_by_netuid.get(candidate_netuid),
            )
        )

        rolled = rolled_existing_lock(
            existing=existing_lock,
            current_block=current_block,
            unlock_rate=unlock_rate,
            maturity_rate=maturity_rate,
            owner_lock=targets_owner_hotkey,
        )

        locked_rao = rolled.locked_mass if rolled is not None else 0
        available_rao = available_to_unstake(
            total_alpha_by_netuid[candidate_netuid], locked_rao
        )

        mode = (
            "perpetual"
            if existing_lock is not None and existing_lock.is_perpetual
            else "decaying"
            if existing_lock is not None
            else "—"
        )

        table.add_row(
            str(candidate_netuid),
            format_alpha(total_alpha_by_netuid[candidate_netuid], candidate_netuid),
            format_alpha(locked_rao, candidate_netuid) if locked_rao else "—",
            format_alpha(available_rao, candidate_netuid),
            mode,
        )

    console.print(table)
    console.print()
    selected = Prompt.ask(
        "Enter netuid to lock",
        choices=[str(candidate) for candidate in candidates],
        show_choices=True,
    )
    console.print()

    return int(selected)


def _prompt_hotkey(
    positions: list[_StakePosition],
    netuid: int,
    *,
    title: str = "Choose Lock Hotkey",
    prompt_text: str = "Enter hotkey index or hotkey SS58",
    raw_prompt: str = "Enter hotkey SS58 to lock to",
    empty_message: Optional[str] = None,
) -> Optional[str]:
    subnet_positions = [p for p in positions if p.netuid == netuid and p.stake_rao > 0]
    if not subnet_positions:
        if empty_message:
            console.print(f"[dim]{empty_message}[/dim]")
        while True:
            hotkey = Prompt.ask(raw_prompt)
            if is_valid_ss58_address(hotkey):
                return hotkey
            console.print("[red]Enter a valid hotkey SS58 address.[/red]")

    table = create_table(
        title=f"\n[{COLORS.G.HEADER}]{title}\n",
        show_footer=False,
    )
    table.add_column("#", justify="center")
    table.add_column("Stake", justify="right", style=COLORS.S.STAKE_ALPHA)
    table.add_column("Hotkey", style=COLORS.G.HOTKEY)

    for idx, position in enumerate(subnet_positions):
        table.add_row(
            str(idx),
            format_alpha(position.stake_rao, netuid),
            position.hotkey,
        )
    console.print(table)

    while True:
        prompt_kwargs = {"default": "0"} if len(subnet_positions) == 1 else {}
        answer = Prompt.ask(prompt_text, **prompt_kwargs)
        if answer.isdigit():
            idx = int(answer)
            if 0 <= idx < len(subnet_positions):
                return subnet_positions[idx].hotkey
            console.print("[red]Invalid hotkey index.[/red]")
            continue
        if is_valid_ss58_address(answer):
            return answer
        console.print("[red]Enter a valid index or hotkey SS58 address.[/red]")


def _prompt_lock_mode(stored_is_perpetual: bool) -> str:
    current_default = "perpetual" if stored_is_perpetual else "decaying"
    console.print(
        "\n[bold]Lock mode[/bold]\n"
        "[dim]decaying[/dim]  Locked alpha gradually unlocks over time.\n"
        "[dim]perpetual[/dim] Locked alpha stays locked until you switch it to decaying."
    )
    return Prompt.ask(
        "Choose lock mode",
        choices=["decaying", "perpetual"],
        default=current_default,
        show_choices=True,
    )


def _prompt_lock_amount(max_amount_rao: int, netuid: int) -> Optional[int]:
    if max_amount_rao <= 0:
        print_error("No alpha is available to lock on this subnet.")
        return None

    max_amount = Balance.from_rao(max_amount_rao).set_unit(netuid)
    while True:
        answer = Prompt.ask(
            f"Enter amount to lock in [{COLORS.S.STAKE_AMOUNT}]"
            f"{Balance.get_unit(netuid)}[/{COLORS.S.STAKE_AMOUNT}] "
            f"([{COLORS.S.STAKE_AMOUNT}]max: {max_amount}"
            f"[/{COLORS.S.STAKE_AMOUNT}], or 'all')"
        )
        if answer.lower() == "all":
            return max_amount_rao
        try:
            amount = Balance.from_tao(float(answer)).set_unit(netuid)
        except ValueError:
            console.print("[red]Please enter a valid number or 'all'.[/red]")
            continue
        if amount.rao <= 0:
            console.print("[red]Amount must be greater than 0.[/red]")
            continue
        if amount.rao > max_amount_rao:
            console.print(f"[red]Amount exceeds available lock amount of {max_amount}.[/red]")
            continue
        return int(amount.rao)


def _build_lock_add_preview(
    netuid: int,
    hotkey: str,
    amount_rao: int,
    total_alpha_rao: int,
    current_locked_rao: int,
    current_conviction: Decimal,
    current_block: int,
    unlock_rate: int,
    maturity_rate: int,
    is_perpetual: bool,
    targets_owner_hotkey: bool,
    existing_lock: bool,
    mode_change_needed: bool,
) -> _LockAddPreview:
    """Build the projected lock state shown before submission."""
    new_locked_rao = current_locked_rao + amount_rao
    post_lock = LockState(
        locked_mass=new_locked_rao,
        conviction=current_conviction if existing_lock else Decimal(0),
        last_update=current_block,
    )

    projected_freed: dict[int, int] = {}
    projected_locked: dict[int, int] = {}
    projected_conviction: dict[int, Decimal] = {}

    for days in GRAPH_BUCKETS_DAYS:
        future = roll_forward_lock(
            lock=post_lock,
            now=current_block + days * BLOCKS_PER_DAY,
            unlock_rate=unlock_rate,
            maturity_rate=maturity_rate,
            owner_lock=targets_owner_hotkey,
            perpetual_lock=is_perpetual,
        )
        projected_locked[days] = future.locked_mass
        projected_conviction[days] = future.conviction

    for days in PROJECTION_BUCKETS_DAYS:
        projected_freed[days] = max(0, post_lock.locked_mass - projected_locked[days])

    return _LockAddPreview(
        netuid=netuid,
        hotkey=hotkey,
        is_perpetual=is_perpetual,
        targets_owner_hotkey=targets_owner_hotkey,
        current_locked_rao=current_locked_rao,
        adding_rao=amount_rao,
        new_locked_rao=new_locked_rao,
        available_after_rao=available_to_unstake(total_alpha_rao, new_locked_rao),
        projected_freed_rao=projected_freed,
        projected_locked_rao=projected_locked,
        projected_conviction=projected_conviction,
        existing_lock=existing_lock,
        mode_change_needed=mode_change_needed,
    )


def _print_lock_add_preview(preview: _LockAddPreview, show_graph: bool) -> None:
    """Render the lock add confirmation preview."""
    mode = "perpetual" if preview.is_perpetual else "decaying"

    table = create_table(
        title=f"\n[{COLORS.G.HEADER}]Lock Preview\n",
        show_footer=False,
    )
    table.add_column("Netuid", justify="center", style=COLORS.G.NETUID)
    table.add_column("Mode", justify="center")
    table.add_column("Current Locked", justify="right", style=COLORS.P.ALPHA_IN)
    table.add_column("Adding", justify="right", style=COLORS.S.STAKE_AMOUNT)
    table.add_column("New Locked", justify="right", style=COLORS.P.ALPHA_IN)
    table.add_column("Available After", justify="right", style=COLORS.S.STAKE_AMOUNT)

    for days in PROJECTION_BUCKETS_DAYS:
        table.add_column(f"+{days}d Free", justify="right", style=COLORS.P.ALPHA_IN)

    table.add_column("Hotkey", style=COLORS.G.HOTKEY)

    projected = [
        format_alpha(preview.projected_freed_rao[days], preview.netuid)
        if preview.projected_freed_rao[days] > 0
        else "—"
        for days in PROJECTION_BUCKETS_DAYS
    ]
    table.add_row(
        str(preview.netuid),
        mode,
        format_alpha(preview.current_locked_rao, preview.netuid)
        if preview.current_locked_rao
        else "—",
        format_alpha(preview.adding_rao, preview.netuid),
        format_alpha(preview.new_locked_rao, preview.netuid),
        format_alpha(preview.available_after_rao, preview.netuid),
        *projected,
        preview.hotkey,
    )

    console.print(table)
    console.print()

    if preview.mode_change_needed:
        console.print(
            f"[dim]This will first set lock mode to {mode}, then submit the lock add.[/dim]"
        )

    if preview.targets_owner_hotkey:
        console.print(
            "[dim]Owner hotkey target: conviction is pinned to locked alpha "
            "by chain rules.[/dim]"
        )

    console.print(
        "[dim]Decaying locks free locked alpha over time. Perpetual locks keep "
        "alpha locked until you switch them to decaying.[/dim]\n"
    )

    if show_graph:
        _print_lock_add_graph(preview)


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


def _print_lock_add_graph(preview: _LockAddPreview) -> None:
    """Render the local lock and conviction projection graph."""
    days = list(GRAPH_BUCKETS_DAYS)
    locked = [_alpha_float(preview.projected_locked_rao[day]) for day in days]
    conviction = [_alpha_float(preview.projected_conviction[day]) for day in days]

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
        f"Alpha ({Balance.get_unit(preview.netuid)})",
        fg=(186, 233, 143),
        mode="rgb",
    )
    fig.x_ticks_fkt = lambda value, _next_value: f"{value:.0f}"
    fig.y_ticks_fkt = _graph_tick
    fig.set_x_limits(min_=0, max_=365)
    fig.set_y_limits(min_=0, max_=max_y * 1.05)

    if preview.targets_owner_hotkey:
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

    console.print(
        f"[{COLORS.G.HEADER}]Lock projection"
        f"[/{COLORS.G.HEADER}] [dim](use --no-graph to hide)[/dim]"
    )
    console.print(Text.from_ansi(fig.show(legend=True)))

    if preview.targets_owner_hotkey:
        console.print(
            "[dim]Owner hotkey target: one line represents both locked alpha "
            "and conviction.[/dim]"
        )

    console.print()


def _print_lock_add_json(preview: _LockAddPreview) -> None:
    console.print(
        json.dumps(
            {
                "netuid": preview.netuid,
                "hotkey": preview.hotkey,
                "mode": "perpetual" if preview.is_perpetual else "decaying",
                "targets_owner_hotkey": preview.targets_owner_hotkey,
                "current_locked_rao": preview.current_locked_rao,
                "adding_rao": preview.adding_rao,
                "new_locked_rao": preview.new_locked_rao,
                "available_after_rao": preview.available_after_rao,
                "projected_freed_rao": preview.projected_freed_rao,
                "projected_locked_rao": preview.projected_locked_rao,
                "projected_conviction_rao": {
                    day: str(value) for day, value in preview.projected_conviction.items()
                },
                "mode_change_needed": preview.mode_change_needed,
            }
        )
    )
