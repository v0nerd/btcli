from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Optional, TYPE_CHECKING

from bittensor_wallet import Wallet

from bittensor_cli.src import COLORS
from bittensor_cli.src.bittensor.balances import Balance
from bittensor_cli.src.bittensor.chain_data import ColdkeySubnetLock
from bittensor_cli.src.bittensor.locks import LockState, roll_forward_lock
from bittensor_cli.src.bittensor.utils import (
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
