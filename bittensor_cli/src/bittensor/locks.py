from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterator, Optional

from bittensor_cli.src.bittensor.chain_data import (
    LockState,
    SubnetLockAggregates,
)

# Mirrors subtensor estimates
BLOCKS_PER_DAY: int = 7200
ONE_YEAR_BLOCKS: int = BLOCKS_PER_DAY * 365 + 1800  # 2_629_800
MIN_EXP_RATIO: Decimal = Decimal(-40)


def exp_decay(elapsed_blocks: int, time_constant: int) -> Decimal:
    """
    Exponential decay multiplier `exp(-elapsed_blocks / time_constant)`,
    clamped at `exp(-40)`.

    Boundary cases:
        * elapsed_blocks == 0 → 1 (no decay).
        * time_constant == 0 with positive elapsed → 0 (instant decay).
    """
    if elapsed_blocks == 0:
        return Decimal(1)

    if time_constant == 0:
        return Decimal(0)

    neg_ratio = Decimal(-elapsed_blocks) / Decimal(time_constant)
    if neg_ratio < MIN_EXP_RATIO:
        neg_ratio = MIN_EXP_RATIO

    return Decimal(repr(math.exp(float(neg_ratio))))


def calculate_decayed_mass_and_conviction(
    locked_mass: int,
    conviction: Decimal,
    elapsed_blocks: int,
    unlock_rate: int,
    maturity_rate: int,
    perpetual_lock: bool,
) -> tuple[int, Decimal]:
    """
    Core roll-forward math: `advance (locked_mass, conviction)` by
    `elapsed_blocks` and return the new pair.

    Mass update:
        * Perpetual locks → unchanged.
        * Decaying locks  → decayed by `exp(-elapsed_blocks / unlock_rate)`.

    Conviction update:
        * Existing conviction decays by `exp(-elapsed_blocks / maturity_rate)`.
        * New conviction is generated from the current mass via closed-form
          ODE math, branched by mode and rate values.

    Owner-pin `conviction = locked_mass` for owner aggregates is applied
    by roll_forward_lock.
    """
    unlock_decay = exp_decay(elapsed_blocks, unlock_rate)
    maturity_decay = exp_decay(elapsed_blocks, maturity_rate)
    mass = Decimal(locked_mass)

    if perpetual_lock:
        # Perpetual: mass never decays.
        new_locked_mass = locked_mass
    else:
        # Decaying: mass shrinks by exp(-elapsed_blocks / unlock_rate).
        new_locked_mass = int(unlock_decay * mass)

    # Old conviction always decays at the maturity rate, regardless of mode.
    conviction_from_existing = maturity_decay * conviction

    if perpetual_lock:
        # Perpetual: standard maturation — conviction asymptotes to locked_mass.
        conviction_from_mass = mass * (Decimal(1) - maturity_decay)

    elif unlock_rate == maturity_rate:
        # Equal-rate fallback.
        if maturity_rate == 0:
            conviction_from_mass = Decimal(0)

        else:
            conviction_from_mass = (
                mass
                * (Decimal(elapsed_blocks) / Decimal(maturity_rate))
                * maturity_decay
            )

    elif unlock_rate == 0 or maturity_rate == 0:
        # Zero-rate: no new conviction.
        conviction_from_mass = Decimal(0)

    else:
        # Default chain rates: exact closed-form formula from ODE math.
        rate_delta = Decimal(unlock_rate - maturity_rate)
        decay_delta = unlock_decay - maturity_decay
        gamma = Decimal(unlock_rate) * decay_delta / rate_delta
        conviction_from_mass = mass * gamma if gamma > 0 else Decimal(0)

    new_conviction = conviction_from_existing + conviction_from_mass
    return new_locked_mass, new_conviction


def roll_forward_lock(
    lock: LockState,
    now: int,
    unlock_rate: int,
    maturity_rate: int,
    *,
    owner_lock: bool,
    perpetual_lock: bool,
) -> LockState:
    """
     Advance a lock to now via `calculate_decayed_mass_and_conviction`;
     returned unchanged when `now <= last_update`.

    `owner_lock=True` pins `conviction = locked_mass` after the
     roll-forward
    """

    if now > lock.last_update:
        elapsed_blocks = now - lock.last_update
        new_mass, new_conv = calculate_decayed_mass_and_conviction(
            locked_mass=lock.locked_mass,
            conviction=lock.conviction,
            elapsed_blocks=elapsed_blocks,
            unlock_rate=unlock_rate,
            maturity_rate=maturity_rate,
            perpetual_lock=perpetual_lock,
        )
        rolled = LockState(locked_mass=new_mass, conviction=new_conv, last_update=now)
    else:
        rolled = lock

    if owner_lock:
        rolled = LockState(
            locked_mass=rolled.locked_mass,
            conviction=Decimal(rolled.locked_mass),
            last_update=rolled.last_update,
        )
    return rolled


def available_to_unstake(total_alpha_rao: int, rolled_locked_mass: int) -> int:
    if total_alpha_rao > rolled_locked_mass:
        return total_alpha_rao - rolled_locked_mass
    return 0


@dataclass(frozen=True)
class _RolledAggregate:
    """
    One rolled-forward aggregate entry.

    attributed_hotkey is the hotkey this contribution counts toward for
    grouping/filtering, or None when attribution doesn't matter, such as subnet totals.
    """

    attributed_hotkey: Optional[str]
    is_perpetual: bool
    state: LockState


def _iter_rolled_aggregates(
    aggregates: SubnetLockAggregates,
    owner_hotkey: Optional[str],
    now: int,
    unlock_rate: int,
    maturity_rate: int,
) -> Iterator[_RolledAggregate]:
    """
    Yield every populated subnet aggregate bucket after rolling it to now.

    Covers HotkeyLock, DecayingHotkeyLock, OwnerLock, and DecayingOwnerLock.
    Hotkey buckets keep their stored hotkey key; owner buckets are attributed
    to owner_hotkey.

    Pass owner_hotkey=None only when the caller does not need attribution, such
    as subnet-wide totals.
    """
    for hotkey, lock in aggregates.hotkey_perpetual.items():
        yield _RolledAggregate(
            attributed_hotkey=hotkey,
            is_perpetual=True,
            state=roll_forward_lock(
                lock=lock,
                now=now,
                unlock_rate=unlock_rate,
                maturity_rate=maturity_rate,
                owner_lock=False,
                perpetual_lock=True,
            ),
        )

    for hotkey, lock in aggregates.hotkey_decaying.items():
        yield _RolledAggregate(
            attributed_hotkey=hotkey,
            is_perpetual=False,
            state=roll_forward_lock(
                lock=lock,
                now=now,
                unlock_rate=unlock_rate,
                maturity_rate=maturity_rate,
                owner_lock=False,
                perpetual_lock=False,
            ),
        )

    if aggregates.owner_perp_lock is not None:
        yield _RolledAggregate(
            attributed_hotkey=owner_hotkey,
            is_perpetual=True,
            state=roll_forward_lock(
                lock=aggregates.owner_perp_lock,
                now=now,
                unlock_rate=unlock_rate,
                maturity_rate=maturity_rate,
                owner_lock=True,
                perpetual_lock=True,
            ),
        )

    if aggregates.owner_decay_lock is not None:
        yield _RolledAggregate(
            attributed_hotkey=owner_hotkey,
            is_perpetual=False,
            state=roll_forward_lock(
                lock=aggregates.owner_decay_lock,
                now=now,
                unlock_rate=unlock_rate,
                maturity_rate=maturity_rate,
                owner_lock=True,
                perpetual_lock=False,
            ),
        )


def _iter_rolled_aggregates_for_hotkey(
    hotkey: str,
    aggregates: SubnetLockAggregates,
    owner_hotkey: str,
    now: int,
    unlock_rate: int,
    maturity_rate: int,
) -> Iterator[_RolledAggregate]:
    """
    Per-hotkey variant of `_iter_rolled_aggregates` with targeted lookup.
    """
    perpetual = aggregates.hotkey_perpetual.get(hotkey)
    if perpetual is not None:
        yield _RolledAggregate(
            attributed_hotkey=hotkey,
            is_perpetual=True,
            state=roll_forward_lock(
                lock=perpetual,
                now=now,
                unlock_rate=unlock_rate,
                maturity_rate=maturity_rate,
                owner_lock=False,
                perpetual_lock=True,
            ),
        )

    decaying = aggregates.hotkey_decaying.get(hotkey)
    if decaying is not None:
        yield _RolledAggregate(
            attributed_hotkey=hotkey,
            is_perpetual=False,
            state=roll_forward_lock(
                lock=decaying,
                now=now,
                unlock_rate=unlock_rate,
                maturity_rate=maturity_rate,
                owner_lock=False,
                perpetual_lock=False,
            ),
        )

    if hotkey == owner_hotkey:
        if aggregates.owner_perp_lock is not None:
            yield _RolledAggregate(
                attributed_hotkey=hotkey,
                is_perpetual=True,
                state=roll_forward_lock(
                    lock=aggregates.owner_perp_lock,
                    now=now,
                    unlock_rate=unlock_rate,
                    maturity_rate=maturity_rate,
                    owner_lock=True,
                    perpetual_lock=True,
                ),
            )

        if aggregates.owner_decay_lock is not None:
            yield _RolledAggregate(
                attributed_hotkey=hotkey,
                is_perpetual=False,
                state=roll_forward_lock(
                    lock=aggregates.owner_decay_lock,
                    now=now,
                    unlock_rate=unlock_rate,
                    maturity_rate=maturity_rate,
                    owner_lock=True,
                    perpetual_lock=False,
                ),
            )


def hotkey_aggregate_locked_split(
    hotkey: str,
    aggregates: SubnetLockAggregates,
    owner_hotkey: str,
    now: int,
    unlock_rate: int,
    maturity_rate: int,
) -> tuple[int, int]:
    """
    Return rolled locked mass for hotkey as (perpetual_rao, decaying_rao).

    Owner aggregate buckets are included when hotkey is the subnet owner hotkey.
    """
    perpetual_total = 0
    decaying_total = 0

    for entry in _iter_rolled_aggregates_for_hotkey(
        hotkey=hotkey,
        aggregates=aggregates,
        owner_hotkey=owner_hotkey,
        now=now,
        unlock_rate=unlock_rate,
        maturity_rate=maturity_rate,
    ):
        if entry.is_perpetual:
            perpetual_total += entry.state.locked_mass

        else:
            decaying_total += entry.state.locked_mass

    return perpetual_total, decaying_total


def hotkey_aggregate_conviction(
    hotkey: str,
    aggregates: SubnetLockAggregates,
    owner_hotkey: str,
    now: int,
    unlock_rate: int,
    maturity_rate: int,
) -> Decimal:
    """
    Return rolled aggregate conviction attributable to the hotkey.

    Sums the hotkey perpetual and hotkey decaying buckets. If hotkey is the subnet
    owner hotkey, also includes the owner perpetual and owner decaying buckets.
    """
    return sum(
        (
            entry.state.conviction
            for entry in _iter_rolled_aggregates_for_hotkey(
                hotkey=hotkey,
                aggregates=aggregates,
                owner_hotkey=owner_hotkey,
                now=now,
                unlock_rate=unlock_rate,
                maturity_rate=maturity_rate,
            )
        ),
        start=Decimal(0),
    )


def subnet_total_conviction(
    aggregates: SubnetLockAggregates,
    now: int,
    unlock_rate: int,
    maturity_rate: int,
) -> Decimal:
    """
    Return total rolled aggregate conviction for the subnet.

    Sums conviction of all hotkeys and owner buckets.
    """
    return sum(
        (
            entry.state.conviction
            for entry in _iter_rolled_aggregates(
                aggregates=aggregates,
                owner_hotkey=None,
                now=now,
                unlock_rate=unlock_rate,
                maturity_rate=maturity_rate,
            )
        ),
        start=Decimal(0),
    )


def subnet_total_locked(
    aggregates: SubnetLockAggregates,
    now: int,
    unlock_rate: int,
    maturity_rate: int,
) -> int:
    """
    Return total rolled locked mass across all subnet aggregate buckets, in rao.

    Sums locked mass of all hotkeys and owner buckets.
    """
    return sum(
        entry.state.locked_mass
        for entry in _iter_rolled_aggregates(
            aggregates=aggregates,
            owner_hotkey=None,
            now=now,
            unlock_rate=unlock_rate,
            maturity_rate=maturity_rate,
        )
    )
