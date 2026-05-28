"""
Parity tests for bittensor_cli/src/bittensor/locks.py against subtensor's
rust implementation in pallets/subtensor/src/staking/lock.rs.

Fixture values are pinned from rust test bodies in
pallets/subtensor/src/tests/locks.rs (commit bb40e0df5).
"""

import math
from decimal import Decimal

import pytest

from bittensor_cli.src.bittensor.locks import (
    LockState,
    calculate_decayed_mass_and_conviction,
    exp_decay,
    roll_forward_lock,
)


# Subtensor defaults (lock.rs::DefaultUnlockRate / DefaultMaturityRate)
DEFAULT_UNLOCK_RATE = 934_866
DEFAULT_MATURITY_RATE = 934_866


# ---------------------------------------------------------------------------
# exp_decay — mirrors subtensor lock.rs::exp_decay
# ---------------------------------------------------------------------------


def test_exp_decay_zero_dt_returns_one():
    assert exp_decay(0, 1000) == Decimal(1)


def test_exp_decay_zero_tau_with_nonzero_dt_returns_zero():
    assert exp_decay(1000, 0) == Decimal(0)


def test_exp_decay_both_zero_returns_one():
    assert exp_decay(0, 0) == Decimal(1)


def test_exp_decay_one_full_tau_matches_e_inverse():
    # exp_decay(tau, tau) == exp(-1) ≈ 0.36788
    result = exp_decay(DEFAULT_UNLOCK_RATE, DEFAULT_UNLOCK_RATE)
    assert abs(float(result) - math.exp(-1)) < 1e-9


def test_exp_decay_clamped_at_negative_40():
    # Very large dt / tiny tau ratio should clamp to exp(-40), not underflow further.
    result = exp_decay(10**18, 1)
    assert result == Decimal(repr(math.exp(-40)))


# ---------------------------------------------------------------------------
# LockState — dataclass sanity
# ---------------------------------------------------------------------------


def test_lock_state_zero_constructor():
    lock = LockState.zero(now=500)
    assert lock.locked_mass == 0
    assert lock.conviction == Decimal(0)
    assert lock.last_update == 500


def test_lock_state_is_immutable():
    lock = LockState(locked_mass=100, conviction=Decimal("0.5"), last_update=10)
    with pytest.raises(Exception):
        lock.locked_mass = 200  # frozen dataclass


# ---------------------------------------------------------------------------
# calculate_decayed_mass_and_conviction — mirrors lock.rs:105
# ---------------------------------------------------------------------------


def test_decayed_perpetual_mass_does_not_change():
    # Perpetual lock: locked_mass is preserved, conviction matures upward.
    new_mass, new_conv = calculate_decayed_mass_and_conviction(
        locked_mass=10_000,
        conviction=Decimal(0),
        elapsed_blocks=DEFAULT_MATURITY_RATE,  # one full maturity tau
        unlock_rate=DEFAULT_UNLOCK_RATE,
        maturity_rate=DEFAULT_MATURITY_RATE,
        perpetual_lock=True,
    )
    assert new_mass == 10_000
    # conviction = mass * (1 - exp(-1)) ≈ 6321
    expected_conv = 10_000 * (1 - math.exp(-1))
    assert abs(float(new_conv) - expected_conv) < 0.001


def test_decayed_non_perpetual_mass_decays():
    # Decaying lock: locked_mass = mass * exp(-dt/unlock_rate).
    new_mass, _new_conv = calculate_decayed_mass_and_conviction(
        locked_mass=10_000,
        conviction=Decimal(0),
        elapsed_blocks=DEFAULT_UNLOCK_RATE,  # one full unlock tau
        unlock_rate=DEFAULT_UNLOCK_RATE,
        maturity_rate=DEFAULT_MATURITY_RATE,
        perpetual_lock=False,
    )
    # 10000 * exp(-1) ≈ 3679
    expected = int(10_000 * math.exp(-1))
    # Tolerance: ≤2 rao for integer truncation drift
    assert abs(new_mass - expected) <= 2


def test_decayed_unequal_rate_closed_form_parity():
    """
    Mirrors subtensor test_roll_forward_conviction_uses_unequal_rate_closed_form.

    With unlock_rate U, maturity_rate M = U * 12/10, locked_mass = 10_000,
    dt = 10_000, conviction_0 = 0, perpetual = false:

        gamma = U * (decay_x - decay_z) / (U - M)
        expected_conviction = locked_mass * gamma
    """
    U = DEFAULT_UNLOCK_RATE
    M = U * 12 // 10
    locked_mass = 10_000
    dt = 10_000

    _new_mass, new_conv = calculate_decayed_mass_and_conviction(
        locked_mass=locked_mass,
        conviction=Decimal(0),
        elapsed_blocks=dt,
        unlock_rate=U,
        maturity_rate=M,
        perpetual_lock=False,
    )

    decay_x = math.exp(-dt / U)
    decay_z = math.exp(-dt / M)
    gamma = U * (decay_x - decay_z) / (U - M)
    expected = locked_mass * gamma

    # epsilon 1e-4 — generous because conviction values are small here
    assert abs(float(new_conv) - expected) < 1e-4


def test_decayed_equal_rate_branch():
    # When unlock_rate == maturity_rate, the math falls back to:
    # conviction_from_mass = mass * (dt / rate) * decay_z
    rate = 1_000_000
    locked_mass = 10_000
    dt = 100_000

    _new_mass, new_conv = calculate_decayed_mass_and_conviction(
        locked_mass=locked_mass,
        conviction=Decimal(0),
        elapsed_blocks=dt,
        unlock_rate=rate,
        maturity_rate=rate,
        perpetual_lock=False,
    )

    expected = locked_mass * (dt / rate) * math.exp(-dt / rate)
    assert abs(float(new_conv) - expected) < 1e-6


def test_decayed_existing_conviction_decays_with_maturity_rate():
    # conviction_from_existing = maturity_decay * conviction_0
    _new_mass, new_conv = calculate_decayed_mass_and_conviction(
        locked_mass=0,
        conviction=Decimal(1000),
        elapsed_blocks=DEFAULT_MATURITY_RATE,
        unlock_rate=DEFAULT_UNLOCK_RATE,
        maturity_rate=DEFAULT_MATURITY_RATE,
        perpetual_lock=False,
    )
    # mass is 0 so no mass-derived term; only existing conviction decays:
    # new_conv = 1000 * exp(-1) ≈ 367.88
    expected = 1000 * math.exp(-1)
    assert abs(float(new_conv) - expected) < 0.01


# ---------------------------------------------------------------------------
# roll_forward_lock + variants — mirrors lock.rs:164–226
# ---------------------------------------------------------------------------


def test_roll_forward_returns_unchanged_when_now_not_after_last_update():
    lock = LockState(locked_mass=500, conviction=Decimal(100), last_update=1000)
    rolled = roll_forward_lock(
        lock,
        now=1000,  # same block
        unlock_rate=DEFAULT_UNLOCK_RATE,
        maturity_rate=DEFAULT_MATURITY_RATE,
        owner_lock=False,
        perpetual_lock=True,
    )
    assert rolled == lock


def test_roll_forward_perpetual_individual_lock_matures_conviction():
    # Perpetual: mass unchanged, conviction asymptotes to mass.
    lock = LockState(locked_mass=10_000, conviction=Decimal(0), last_update=0)
    rolled = roll_forward_lock(
        lock,
        now=DEFAULT_MATURITY_RATE,
        unlock_rate=DEFAULT_UNLOCK_RATE,
        maturity_rate=DEFAULT_MATURITY_RATE,
        owner_lock=False,
        perpetual_lock=True,
    )
    assert rolled.locked_mass == 10_000
    expected_conv = 10_000 * (1 - math.exp(-1))
    assert abs(float(rolled.conviction) - expected_conv) < 0.001
    assert rolled.last_update == DEFAULT_MATURITY_RATE


def test_roll_forward_owner_lock_pins_conviction_to_mass():
    # Mirrors lock.rs:188-190: owner_lock forces conviction = locked_mass after
    # the decay-and-mature pass.
    lock = LockState(locked_mass=10_000, conviction=Decimal(0), last_update=0)
    rolled = roll_forward_lock(
        lock,
        now=DEFAULT_MATURITY_RATE,
        unlock_rate=DEFAULT_UNLOCK_RATE,
        maturity_rate=DEFAULT_MATURITY_RATE,
        owner_lock=True,
        perpetual_lock=True,
    )
    assert rolled.conviction == Decimal(rolled.locked_mass)


def test_roll_forward_decaying_individual_lock_decays_mass():
    # Mirrors test_roll_forward_locked_mass_decays.
    lock = LockState(locked_mass=10_000, conviction=Decimal(0), last_update=0)
    rolled = roll_forward_lock(
        lock,
        now=DEFAULT_UNLOCK_RATE,
        unlock_rate=DEFAULT_UNLOCK_RATE,
        maturity_rate=DEFAULT_MATURITY_RATE,
        owner_lock=False,
        perpetual_lock=False,
    )
    assert 0 < rolled.locked_mass < 10_000
    expected = int(10_000 * math.exp(-1))
    assert abs(rolled.locked_mass - expected) <= 2
