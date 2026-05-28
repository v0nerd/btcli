import asyncio
import json
from typing import TYPE_CHECKING, Optional, TypedDict

from bittensor_wallet import Wallet
from rich import box
from rich.table import Column

from bittensor_cli.src import COLOR_PALETTE, COLORS
from bittensor_cli.src.bittensor.balances import Balance, fixed_to_float
from bittensor_cli.src.bittensor.extrinsics.mev_shield import (
    wait_for_extrinsic_by_hash,
)
from bittensor_cli.src.bittensor.utils import (
    confirm_action,
    console,
    create_table,
    get_subnet_name,
    json_console,
    print_error,
    print_extrinsic_id,
    print_success,
    unlock_key,
)

if TYPE_CHECKING:
    from bittensor_cli.src.bittensor.subtensor_interface import SubtensorInterface


class LockState(TypedDict):
    """
    Exponential lock state for a coldkey on a subnet.

    Attributes:
        locked_mass: Exponentially decaying locked amount (Balance in subnet Alpha).
        conviction: Matured decaying score (integral of locked_mass over time).
        last_update: Block number of last roll-forward.
    """

    locked_mass: Balance
    conviction: float
    last_update: int


def _lock_state_from_raw(raw: dict, netuid: int) -> LockState:
    return LockState(
        locked_mass=Balance.from_rao(raw["locked_mass"]).set_unit(netuid),
        conviction=fixed_to_float(raw["conviction"]),
        last_update=int(raw["last_update"]),
    )


async def get_stake_locks(
    subtensor: "SubtensorInterface",
    coldkey_ss58: str,
    netuid: int,
    block_hash: Optional[str] = None,
) -> list[tuple[str, LockState]]:
    """All lock entries for a coldkey on a subnet (one per hotkey)."""
    query_map = await subtensor.substrate.query_map(
        module="SubtensorModule",
        storage_function="Lock",
        params=[coldkey_ss58, netuid],
        block_hash=block_hash,
        fully_exhaust=True,
        page_size=1_000,
    )
    locks: list[tuple[str, LockState]] = []
    for hotkey, lock_state in query_map.records:
        if lock_state is None:
            continue
        locks.append((hotkey, _lock_state_from_raw(lock_state, netuid)))
    return locks


async def get_coldkey_lock(
    subtensor: "SubtensorInterface",
    coldkey_ss58: str,
    netuid: int,
    block_hash: Optional[str] = None,
) -> Optional[LockState]:
    """Runtime-API view of a coldkey's lock with decay rolled forward to the current block."""
    result = await subtensor.query_runtime_api(
        runtime_api="StakeInfoRuntimeApi",
        method="get_coldkey_lock",
        params=[coldkey_ss58, netuid],
        block_hash=block_hash,
    )
    if result is None:
        return None
    return _lock_state_from_raw(result, netuid)


async def is_perpetual_lock(
    subtensor: "SubtensorInterface",
    coldkey_ss58: str,
    netuid: int,
    block_hash: Optional[str] = None,
) -> bool:
    """True when the coldkey's lock on the subnet has opted into perpetual (non-decaying) mode."""
    value = await subtensor.query(
        module="SubtensorModule",
        storage_function="DecayingLock",
        params=[coldkey_ss58, netuid],
        block_hash=block_hash,
    )
    return value is not None


async def get_hotkey_conviction(
    subtensor: "SubtensorInterface",
    hotkey_ss58: str,
    netuid: int,
    block_hash: Optional[str] = None,
) -> float:
    """Total conviction accrued for a hotkey on a subnet."""
    result = await subtensor.query_runtime_api(
        runtime_api="StakeInfoRuntimeApi",
        method="get_hotkey_conviction",
        params=[hotkey_ss58, netuid],
        block_hash=block_hash,
    )
    if result is None:
        return 0.0
    return fixed_to_float(result)


async def get_most_convicted_hotkey_on_subnet(
    subtensor: "SubtensorInterface",
    netuid: int,
    block_hash: Optional[str] = None,
) -> Optional[str]:
    """Hotkey with the highest conviction on a subnet ("subnet king")."""
    return await subtensor.query_runtime_api(
        runtime_api="StakeInfoRuntimeApi",
        method="get_most_convicted_hotkey_on_subnet",
        params=[netuid],
        block_hash=block_hash,
    )


# ---------------------------------------------------------------------------
# Extrinsics
# ---------------------------------------------------------------------------


async def lock_stake(
    wallet: Wallet,
    subtensor: "SubtensorInterface",
    hotkey_ss58: str,
    netuid: int,
    amount: float,
    era: int,
    prompt: bool = True,
    decline: bool = False,
    quiet: bool = False,
    proxy: Optional[str] = None,
    mev_protection: bool = True,
    json_output: bool = False,
) -> tuple[bool, str]:
    """Lock alpha stake on a hotkey within a subnet to start accruing conviction."""
    coldkey_ss58 = proxy or wallet.coldkeypub.ss58_address

    block_hash = await subtensor.substrate.get_chain_head()
    subnet_exists, hotkey_exists, current_stake, existing_lock = await asyncio.gather(
        subtensor.subnet_exists(netuid=netuid, block_hash=block_hash),
        subtensor.does_hotkey_exist(hotkey_ss58, block_hash=block_hash),
        subtensor.get_stake(
            coldkey_ss58=coldkey_ss58,
            hotkey_ss58=hotkey_ss58,
            netuid=netuid,
            block_hash=block_hash,
        ),
        get_stake_locks(subtensor, coldkey_ss58, netuid, block_hash=block_hash),
    )

    if not subnet_exists:
        print_error(f"Subnet {netuid} does not exist")
        return False, ""

    if not hotkey_exists:
        print_error(f"Hotkey not registered on chain: {hotkey_ss58}")
        if not prompt:
            return False, ""
        if not confirm_action(
            "Continue with the lock anyway? The extrinsic may fail on-chain.",
            default=False,
            decline=decline,
            quiet=quiet,
        ):
            return False, ""

    amount_balance = Balance.from_tao(amount).set_unit(netuid)
    if amount_balance.rao <= 0:
        print_error(f"Lock amount must be positive (got {amount_balance})")
        return False, ""
    if amount_balance > current_stake:
        print_error(
            f"Not enough stake to lock:\n"
            f"  Stake balance: [{COLOR_PALETTE.S.AMOUNT}]{current_stake}[/{COLOR_PALETTE.S.AMOUNT}]"
            f" < Lock amount: [{COLOR_PALETTE.S.AMOUNT}]{amount_balance}[/{COLOR_PALETTE.S.AMOUNT}]"
        )
        return False, ""

    # An existing lock on a different hotkey will be rejected on-chain (LockHotkeyMismatch).
    # Surface that early so users don't pay the extrinsic round-trip to discover it.
    if existing_lock:
        existing_hotkey = existing_lock[0][0]
        if existing_hotkey != hotkey_ss58:
            print_error(
                f"Coldkey already has a lock on hotkey [blue]{existing_hotkey}[/blue] for "
                f"subnet [yellow]{netuid}[/yellow]. Locks are bound to a single hotkey per subnet; "
                f"use `btcli stake lock move` to move it before locking on a different hotkey."
            )
            return False, ""

    console.print(
        f"\nLocking [{COLOR_PALETTE.POOLS.TAO}]{amount_balance}[/{COLOR_PALETTE.POOLS.TAO}] "
        f"on hotkey [{COLOR_PALETTE.G.SUBHEAD}]{hotkey_ss58}[/{COLOR_PALETTE.G.SUBHEAD}] "
        f"in subnet [{COLOR_PALETTE.G.SUBHEAD}]{netuid}[/{COLOR_PALETTE.G.SUBHEAD}]"
    )
    if prompt and not confirm_action("Continue?", decline=decline, quiet=quiet):
        return False, ""

    if not unlock_key(wallet).success:
        return False, ""

    call = await subtensor.substrate.compose_call(
        call_module="SubtensorModule",
        call_function="lock_stake",
        call_params={
            "hotkey": hotkey_ss58,
            "netuid": netuid,
            "amount": amount_balance.rao,
        },
    )

    with console.status(
        f":satellite: Locking [blue]{amount_balance}[/blue] on "
        f"[blue]{hotkey_ss58}[/blue] (netuid [blue]{netuid}[/blue])..."
    ) as status:
        success, err_msg, response = await subtensor.sign_and_send_extrinsic(
            call=call,
            wallet=wallet,
            era={"period": era},
            proxy=proxy,
            mev_protection=mev_protection,
        )
        ext_id = await response.get_extrinsic_identifier() if response else ""
        if success and mev_protection:
            inner_hash = err_msg
            mev_success, mev_error, response = await wait_for_extrinsic_by_hash(
                subtensor=subtensor,
                extrinsic_hash=inner_hash,
                submit_block_hash=response.block_hash,
                status=status,
            )
            if not mev_success:
                status.stop()
                print_error(f"\nFailed: {mev_error}")
                return False, ""
            ext_id = await response.get_extrinsic_identifier() if response else ""

    if not success:
        print_error(f"\nFailed to lock stake: {err_msg}")
        return False, ""

    await print_extrinsic_id(response)
    print_success("[dark_sea_green3]Stake locked.[/dark_sea_green3]")
    if json_output:
        json_console.print(
            json.dumps({"success": True, "extrinsic_identifier": ext_id or None})
        )
    return True, ext_id


async def move_lock(
    wallet: Wallet,
    subtensor: "SubtensorInterface",
    destination_hotkey_ss58: str,
    netuid: int,
    era: int,
    prompt: bool = True,
    decline: bool = False,
    quiet: bool = False,
    proxy: Optional[str] = None,
    mev_protection: bool = True,
    json_output: bool = False,
) -> tuple[bool, str]:
    """Move an existing lock to a different hotkey on the same subnet."""
    coldkey_ss58 = proxy or wallet.coldkeypub.ss58_address

    block_hash = await subtensor.substrate.get_chain_head()
    subnet_exists, dest_exists, existing_lock = await asyncio.gather(
        subtensor.subnet_exists(netuid=netuid, block_hash=block_hash),
        subtensor.does_hotkey_exist(destination_hotkey_ss58, block_hash=block_hash),
        get_stake_locks(subtensor, coldkey_ss58, netuid, block_hash=block_hash),
    )

    if not subnet_exists:
        print_error(f"Subnet {netuid} does not exist")
        return False, ""

    if not dest_exists:
        print_error(
            f"Destination hotkey not registered on chain: {destination_hotkey_ss58}"
        )
        if not prompt:
            return False, ""
        if not confirm_action(
            "Continue with the move anyway? The extrinsic may fail on-chain.",
            default=False,
            decline=decline,
            quiet=quiet,
        ):
            return False, ""

    if not existing_lock:
        print_error(
            f"No existing lock found for coldkey on subnet [yellow]{netuid}[/yellow]."
        )
        return False, ""

    origin_hotkey, origin_state = existing_lock[0]
    if origin_hotkey == destination_hotkey_ss58:
        print_error(
            f"Destination hotkey is the same as the current lock holder "
            f"([blue]{origin_hotkey}[/blue]). Nothing to move."
        )
        return False, ""

    console.print(
        f"\nMoving lock of [{COLOR_PALETTE.POOLS.TAO}]{origin_state['locked_mass']}"
        f"[/{COLOR_PALETTE.POOLS.TAO}] "
        f"from [{COLOR_PALETTE.G.SUBHEAD}]{origin_hotkey}[/{COLOR_PALETTE.G.SUBHEAD}] "
        f"to [{COLOR_PALETTE.G.SUBHEAD}]{destination_hotkey_ss58}[/{COLOR_PALETTE.G.SUBHEAD}] "
        f"on subnet [{COLOR_PALETTE.G.SUBHEAD}]{netuid}[/{COLOR_PALETTE.G.SUBHEAD}]"
    )
    if prompt and not confirm_action("Continue?", decline=decline, quiet=quiet):
        return False, ""

    if not unlock_key(wallet).success:
        return False, ""

    call = await subtensor.substrate.compose_call(
        call_module="SubtensorModule",
        call_function="move_lock",
        call_params={
            "destination_hotkey": destination_hotkey_ss58,
            "netuid": netuid,
        },
    )

    with console.status(
        f":satellite: Moving lock to [blue]{destination_hotkey_ss58}[/blue] "
        f"(netuid [blue]{netuid}[/blue])..."
    ) as status:
        success, err_msg, response = await subtensor.sign_and_send_extrinsic(
            call=call,
            wallet=wallet,
            era={"period": era},
            proxy=proxy,
            mev_protection=mev_protection,
        )
        ext_id = await response.get_extrinsic_identifier() if response else ""
        if success and mev_protection:
            inner_hash = err_msg
            mev_success, mev_error, response = await wait_for_extrinsic_by_hash(
                subtensor=subtensor,
                extrinsic_hash=inner_hash,
                submit_block_hash=response.block_hash,
                status=status,
            )
            if not mev_success:
                status.stop()
                print_error(f"\nFailed: {mev_error}")
                return False, ""
            ext_id = await response.get_extrinsic_identifier() if response else ""

    if not success:
        print_error(f"\nFailed to move lock: {err_msg}")
        return False, ""

    await print_extrinsic_id(response)
    print_success("[dark_sea_green3]Lock moved.[/dark_sea_green3]")
    if json_output:
        json_console.print(
            json.dumps({"success": True, "extrinsic_identifier": ext_id or None})
        )
    return True, ext_id


async def set_perpetual_lock(
    wallet: Wallet,
    subtensor: "SubtensorInterface",
    netuid: int,
    enabled: bool,
    era: int,
    prompt: bool = True,
    decline: bool = False,
    quiet: bool = False,
    proxy: Optional[str] = None,
    json_output: bool = False,
) -> tuple[bool, str]:
    """Toggle the perpetual (non-decaying) lock flag for the caller's lock on a subnet."""
    coldkey_ss58 = proxy or wallet.coldkeypub.ss58_address

    block_hash = await subtensor.substrate.get_chain_head()
    subnet_exists, currently_perpetual, existing_lock = await asyncio.gather(
        subtensor.subnet_exists(netuid=netuid, block_hash=block_hash),
        is_perpetual_lock(subtensor, coldkey_ss58, netuid, block_hash=block_hash),
        get_stake_locks(subtensor, coldkey_ss58, netuid, block_hash=block_hash),
    )

    if not subnet_exists:
        print_error(f"Subnet {netuid} does not exist")
        return False, ""

    if not existing_lock:
        print_error(
            f"No existing lock found for coldkey on subnet [yellow]{netuid}[/yellow]. "
            f"Lock stake first with `btcli stake lock add`."
        )
        return False, ""

    if currently_perpetual == enabled:
        state_str = "perpetual" if enabled else "decaying"
        console.print(
            f"[yellow]Lock on subnet {netuid} is already {state_str}; nothing to do.[/yellow]"
        )
        if json_output:
            json_console.print(
                json.dumps(
                    {"success": True, "extrinsic_identifier": None, "noop": True}
                )
            )
        return True, ""

    action = "enable" if enabled else "disable"
    console.print(
        f"\nWill [{COLOR_PALETTE.G.SUBHEAD}]{action}[/{COLOR_PALETTE.G.SUBHEAD}] "
        f"perpetual lock on subnet [{COLOR_PALETTE.G.SUBHEAD}]{netuid}[/{COLOR_PALETTE.G.SUBHEAD}]"
    )
    if prompt and not confirm_action("Continue?", decline=decline, quiet=quiet):
        return False, ""

    if not unlock_key(wallet).success:
        return False, ""

    call = await subtensor.substrate.compose_call(
        call_module="SubtensorModule",
        call_function="set_perpetual_lock",
        call_params={
            "netuid": netuid,
            "enabled": enabled,
        },
    )

    with console.status(
        f":satellite: Setting perpetual lock to [blue]{enabled}[/blue] "
        f"on netuid [blue]{netuid}[/blue]..."
    ):
        success, err_msg, response = await subtensor.sign_and_send_extrinsic(
            call=call,
            wallet=wallet,
            era={"period": era},
            proxy=proxy,
            mev_protection=False,
        )

    if not success:
        print_error(f"\nFailed to set perpetual lock: {err_msg}")
        return False, ""

    ext_id = await response.get_extrinsic_identifier() if response else ""
    await print_extrinsic_id(response)
    print_success(
        f"[dark_sea_green3]Perpetual lock {'enabled' if enabled else 'disabled'}.[/dark_sea_green3]"
    )
    if json_output:
        json_console.print(
            json.dumps({"success": True, "extrinsic_identifier": ext_id or None})
        )
    return True, ext_id


# ---------------------------------------------------------------------------
# Read-only views
# ---------------------------------------------------------------------------


async def list_locks(
    subtensor: "SubtensorInterface",
    coldkey_ss58: str,
    netuid: int,
    json_output: bool = False,
) -> dict:
    """Display all locks for a coldkey on a subnet plus the runtime-rolled coldkey view."""
    block_hash = await subtensor.substrate.get_chain_head()
    raw_locks, rolled, perpetual, subnets_info = await asyncio.gather(
        get_stake_locks(subtensor, coldkey_ss58, netuid, block_hash=block_hash),
        get_coldkey_lock(subtensor, coldkey_ss58, netuid, block_hash=block_hash),
        is_perpetual_lock(subtensor, coldkey_ss58, netuid, block_hash=block_hash),
        subtensor.all_subnets(block_hash=block_hash),
    )

    subnet_map = {info.netuid: info for info in subnets_info}
    subnet_label = (
        f"{netuid} ({get_subnet_name(subnet_map[netuid])})"
        if netuid in subnet_map
        else str(netuid)
    )

    data: dict = {
        "coldkey": coldkey_ss58,
        "netuid": netuid,
        "perpetual": perpetual,
        "locks": [
            {
                "hotkey": hotkey,
                "locked_mass_rao": int(state["locked_mass"].rao),
                "locked_mass_tao": float(state["locked_mass"].tao),
                "conviction": float(state["conviction"]),
                "last_update": int(state["last_update"]),
            }
            for hotkey, state in raw_locks
        ],
        "rolled_forward": (
            {
                "locked_mass_rao": int(rolled["locked_mass"].rao),
                "locked_mass_tao": float(rolled["locked_mass"].tao),
                "conviction": float(rolled["conviction"]),
                "last_update": int(rolled["last_update"]),
            }
            if rolled is not None
            else None
        ),
    }

    if json_output:
        json_console.print(json.dumps(data))
        return data

    if not raw_locks:
        console.print(
            f"[yellow]No locks found for coldkey {coldkey_ss58} on subnet {subnet_label}.[/yellow]"
        )
        return data

    table = create_table(
        Column("Hotkey", justify="left", style=COLOR_PALETTE.G.SUBHEAD),
        Column("Locked Mass", justify="right", style=COLOR_PALETTE.POOLS.TAO),
        Column("Conviction", justify="right"),
        Column("Last Update (block)", justify="right"),
        title=(
            f"\n[{COLORS.GENERAL.HEADER}]Locks for coldkey "
            f"[bold]{coldkey_ss58}[/bold]\n"
            f"Subnet: {subnet_label}\n"
            f"Perpetual: {'yes' if perpetual else 'no'}"
            f"[/{COLORS.GENERAL.HEADER}]"
        ),
        box=box.SIMPLE,
    )
    for hotkey, state in raw_locks:
        table.add_row(
            hotkey,
            str(state["locked_mass"]),
            f"{state['conviction']:.6f}",
            str(state["last_update"]),
        )
    console.print(table)

    if rolled is not None:
        console.print(
            f"\nRolled-forward (current block) view for coldkey:\n"
            f"  Locked mass: [{COLOR_PALETTE.POOLS.TAO}]{rolled['locked_mass']}"
            f"[/{COLOR_PALETTE.POOLS.TAO}]\n"
            f"  Conviction:  [{COLOR_PALETTE.G.SUBHEAD}]{rolled['conviction']:.6f}"
            f"[/{COLOR_PALETTE.G.SUBHEAD}]\n"
            f"  Last update: block {rolled['last_update']}"
        )
    return data


async def show_conviction(
    subtensor: "SubtensorInterface",
    netuid: int,
    hotkey_ss58: Optional[str] = None,
    json_output: bool = False,
) -> dict:
    """Show a hotkey's conviction on a subnet and which hotkey is the subnet king."""
    block_hash = await subtensor.substrate.get_chain_head()

    if hotkey_ss58 is not None:
        conviction, king = await asyncio.gather(
            get_hotkey_conviction(
                subtensor, hotkey_ss58, netuid, block_hash=block_hash
            ),
            get_most_convicted_hotkey_on_subnet(
                subtensor, netuid, block_hash=block_hash
            ),
        )
    else:
        conviction = None
        king = await get_most_convicted_hotkey_on_subnet(
            subtensor, netuid, block_hash=block_hash
        )

    data = {
        "netuid": netuid,
        "hotkey": hotkey_ss58,
        "conviction": conviction,
        "king": king,
    }
    if json_output:
        json_console.print(json.dumps(data))
        return data

    if hotkey_ss58 is not None:
        console.print(
            f"\nConviction for hotkey "
            f"[{COLOR_PALETTE.G.SUBHEAD}]{hotkey_ss58}[/{COLOR_PALETTE.G.SUBHEAD}] "
            f"on subnet [{COLOR_PALETTE.G.SUBHEAD}]{netuid}[/{COLOR_PALETTE.G.SUBHEAD}]: "
            f"[{COLOR_PALETTE.POOLS.TAO}]{conviction:.6f}[/{COLOR_PALETTE.POOLS.TAO}]"
        )
    if king:
        console.print(
            f"Subnet king (highest conviction on netuid {netuid}): "
            f"[{COLOR_PALETTE.G.SUBHEAD}]{king}[/{COLOR_PALETTE.G.SUBHEAD}]"
        )
    else:
        console.print(
            f"[yellow]No locked stake on subnet {netuid}; no subnet king.[/yellow]"
        )
    return data
