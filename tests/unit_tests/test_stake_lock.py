"""
Unit tests for bittensor_cli/src/commands/stake/lock.py.

Covers the conviction-lock command surface added to mirror the bittensor SDK's
feat/roman/conviction-lock-support branch: lock_stake, move_lock, set_perpetual_lock,
list_locks, show_conviction, plus the small read-only helpers (get_stake_locks,
get_coldkey_lock, is_perpetual_lock, get_hotkey_conviction).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bittensor_cli.src.bittensor.balances import Balance
from bittensor_cli.src.commands.stake import lock as lock_mod
from .conftest import COLDKEY_SS58, HOTKEY_SS58, ALT_HOTKEY_SS58


MODULE = "bittensor_cli.src.commands.stake.lock"


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _raw_lock_state(
    rao: int = 1_000_000_000, conviction: int = 0, last_update: int = 100
):
    """Storage-shaped Lock entry — values match what substrate.query_map decodes to."""
    return {
        "locked_mass": rao,
        "conviction": conviction,
        "last_update": last_update,
    }


def _query_map_with(records):
    qm = MagicMock()
    qm.records = records
    return qm


def _make_receipt(identifier: str = "0x123"):
    async def _is_success():
        return True

    r = MagicMock()
    r.is_success = _is_success()
    r.get_extrinsic_identifier = AsyncMock(return_value=identifier)
    r.block_hash = "0xblock"
    return r


# ---------------------------------------------------------------------------
# Helper read functions
# ---------------------------------------------------------------------------


class TestGetStakeLocks:
    @pytest.mark.asyncio
    async def test_filters_none_records(self, mock_subtensor):
        mock_subtensor.substrate.query_map = AsyncMock(
            return_value=_query_map_with(
                [(HOTKEY_SS58, _raw_lock_state(1_000)), (ALT_HOTKEY_SS58, None)]
            )
        )
        locks = await lock_mod.get_stake_locks(mock_subtensor, COLDKEY_SS58, netuid=1)
        assert len(locks) == 1
        assert locks[0][0] == HOTKEY_SS58
        assert locks[0][1]["locked_mass"].rao == 1_000

    @pytest.mark.asyncio
    async def test_passes_params_to_storage(self, mock_subtensor):
        mock_subtensor.substrate.query_map = AsyncMock(return_value=_query_map_with([]))
        await lock_mod.get_stake_locks(mock_subtensor, COLDKEY_SS58, netuid=7)
        mock_subtensor.substrate.query_map.assert_awaited_once()
        kwargs = mock_subtensor.substrate.query_map.call_args.kwargs
        assert kwargs["module"] == "SubtensorModule"
        assert kwargs["storage_function"] == "Lock"
        assert kwargs["params"] == [COLDKEY_SS58, 7]


class TestGetColdkeyLock:
    @pytest.mark.asyncio
    async def test_returns_none_when_missing(self, mock_subtensor):
        mock_subtensor.query_runtime_api = AsyncMock(return_value=None)
        assert await lock_mod.get_coldkey_lock(mock_subtensor, COLDKEY_SS58, 1) is None

    @pytest.mark.asyncio
    async def test_parses_lock_state(self, mock_subtensor):
        mock_subtensor.query_runtime_api = AsyncMock(
            return_value=_raw_lock_state(rao=5_000, last_update=42)
        )
        state = await lock_mod.get_coldkey_lock(mock_subtensor, COLDKEY_SS58, 1)
        assert state["locked_mass"].rao == 5_000
        assert state["last_update"] == 42


class TestIsPerpetualLock:
    @pytest.mark.asyncio
    async def test_decaying_when_entry_present(self, mock_subtensor):
        """DecayingLock entry present → currently perpetual (locked, won't decay)."""
        mock_subtensor.query = AsyncMock(return_value={"some": "value"})
        assert await lock_mod.is_perpetual_lock(mock_subtensor, COLDKEY_SS58, 1) is True

    @pytest.mark.asyncio
    async def test_not_perpetual_when_missing(self, mock_subtensor):
        mock_subtensor.query = AsyncMock(return_value=None)
        assert (
            await lock_mod.is_perpetual_lock(mock_subtensor, COLDKEY_SS58, 1) is False
        )


class TestHotkeyConviction:
    @pytest.mark.asyncio
    async def test_returns_zero_when_missing(self, mock_subtensor):
        mock_subtensor.query_runtime_api = AsyncMock(return_value=None)
        assert (
            await lock_mod.get_hotkey_conviction(mock_subtensor, HOTKEY_SS58, 1) == 0.0
        )

    @pytest.mark.asyncio
    async def test_returns_king(self, mock_subtensor):
        mock_subtensor.query_runtime_api = AsyncMock(return_value=HOTKEY_SS58)
        assert (
            await lock_mod.get_most_convicted_hotkey_on_subnet(mock_subtensor, 1)
            == HOTKEY_SS58
        )


# ---------------------------------------------------------------------------
# lock_stake extrinsic
# ---------------------------------------------------------------------------


class TestLockStake:
    @pytest.mark.asyncio
    async def test_happy_path_composes_correct_call(self, mock_wallet, mock_subtensor):
        mock_subtensor.subnet_exists = AsyncMock(return_value=True)
        mock_subtensor.does_hotkey_exist = AsyncMock(return_value=True)
        mock_subtensor.get_stake = AsyncMock(return_value=Balance.from_tao(10))
        mock_subtensor.substrate.query_map = AsyncMock(return_value=_query_map_with([]))

        receipt = _make_receipt("0xabc")
        mock_subtensor.sign_and_send_extrinsic = AsyncMock(
            return_value=(True, "", receipt)
        )

        with (
            patch(f"{MODULE}.unlock_key", return_value=MagicMock(success=True)),
            patch(f"{MODULE}.print_extrinsic_id", new_callable=AsyncMock),
        ):
            ok, ext_id = await lock_mod.lock_stake(
                wallet=mock_wallet,
                subtensor=mock_subtensor,
                hotkey_ss58=HOTKEY_SS58,
                netuid=1,
                amount=5.0,
                era=16,
                prompt=False,
                mev_protection=False,
            )

        assert ok is True
        assert ext_id == "0xabc"
        mock_subtensor.substrate.compose_call.assert_awaited_once()
        kwargs = mock_subtensor.substrate.compose_call.call_args.kwargs
        assert kwargs["call_module"] == "SubtensorModule"
        assert kwargs["call_function"] == "lock_stake"
        assert kwargs["call_params"] == {
            "hotkey": HOTKEY_SS58,
            "netuid": 1,
            "amount": Balance.from_tao(5).rao,
        }

    @pytest.mark.asyncio
    async def test_rejects_when_amount_exceeds_stake(self, mock_wallet, mock_subtensor):
        mock_subtensor.subnet_exists = AsyncMock(return_value=True)
        mock_subtensor.does_hotkey_exist = AsyncMock(return_value=True)
        mock_subtensor.get_stake = AsyncMock(return_value=Balance.from_tao(1))
        mock_subtensor.substrate.query_map = AsyncMock(return_value=_query_map_with([]))

        ok, ext_id = await lock_mod.lock_stake(
            wallet=mock_wallet,
            subtensor=mock_subtensor,
            hotkey_ss58=HOTKEY_SS58,
            netuid=1,
            amount=10.0,
            era=16,
            prompt=False,
            mev_protection=False,
        )
        assert ok is False
        mock_subtensor.sign_and_send_extrinsic.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_when_existing_lock_on_different_hotkey(
        self, mock_wallet, mock_subtensor
    ):
        mock_subtensor.subnet_exists = AsyncMock(return_value=True)
        mock_subtensor.does_hotkey_exist = AsyncMock(return_value=True)
        mock_subtensor.get_stake = AsyncMock(return_value=Balance.from_tao(10))
        mock_subtensor.substrate.query_map = AsyncMock(
            return_value=_query_map_with([(ALT_HOTKEY_SS58, _raw_lock_state())])
        )

        ok, _ = await lock_mod.lock_stake(
            wallet=mock_wallet,
            subtensor=mock_subtensor,
            hotkey_ss58=HOTKEY_SS58,
            netuid=1,
            amount=1.0,
            era=16,
            prompt=False,
            mev_protection=False,
        )
        assert ok is False
        mock_subtensor.sign_and_send_extrinsic.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_subnet_missing_short_circuits(self, mock_wallet, mock_subtensor):
        mock_subtensor.subnet_exists = AsyncMock(return_value=False)
        mock_subtensor.does_hotkey_exist = AsyncMock(return_value=True)
        mock_subtensor.get_stake = AsyncMock(return_value=Balance.from_tao(10))
        mock_subtensor.substrate.query_map = AsyncMock(return_value=_query_map_with([]))

        ok, _ = await lock_mod.lock_stake(
            wallet=mock_wallet,
            subtensor=mock_subtensor,
            hotkey_ss58=HOTKEY_SS58,
            netuid=99,
            amount=1.0,
            era=16,
            prompt=False,
            mev_protection=False,
        )
        assert ok is False
        mock_subtensor.sign_and_send_extrinsic.assert_not_awaited()


# ---------------------------------------------------------------------------
# move_lock extrinsic
# ---------------------------------------------------------------------------


class TestMoveLock:
    @pytest.mark.asyncio
    async def test_happy_path_composes_correct_call(self, mock_wallet, mock_subtensor):
        mock_subtensor.subnet_exists = AsyncMock(return_value=True)
        mock_subtensor.does_hotkey_exist = AsyncMock(return_value=True)
        mock_subtensor.substrate.query_map = AsyncMock(
            return_value=_query_map_with([(HOTKEY_SS58, _raw_lock_state())])
        )
        receipt = _make_receipt("0xdef")
        mock_subtensor.sign_and_send_extrinsic = AsyncMock(
            return_value=(True, "", receipt)
        )

        with (
            patch(f"{MODULE}.unlock_key", return_value=MagicMock(success=True)),
            patch(f"{MODULE}.print_extrinsic_id", new_callable=AsyncMock),
        ):
            ok, ext_id = await lock_mod.move_lock(
                wallet=mock_wallet,
                subtensor=mock_subtensor,
                destination_hotkey_ss58=ALT_HOTKEY_SS58,
                netuid=1,
                era=16,
                prompt=False,
                mev_protection=False,
            )

        assert ok is True
        assert ext_id == "0xdef"
        kwargs = mock_subtensor.substrate.compose_call.call_args.kwargs
        assert kwargs["call_function"] == "move_lock"
        assert kwargs["call_params"] == {
            "destination_hotkey": ALT_HOTKEY_SS58,
            "netuid": 1,
        }

    @pytest.mark.asyncio
    async def test_rejects_when_no_existing_lock(self, mock_wallet, mock_subtensor):
        mock_subtensor.subnet_exists = AsyncMock(return_value=True)
        mock_subtensor.does_hotkey_exist = AsyncMock(return_value=True)
        mock_subtensor.substrate.query_map = AsyncMock(return_value=_query_map_with([]))

        ok, _ = await lock_mod.move_lock(
            wallet=mock_wallet,
            subtensor=mock_subtensor,
            destination_hotkey_ss58=ALT_HOTKEY_SS58,
            netuid=1,
            era=16,
            prompt=False,
            mev_protection=False,
        )
        assert ok is False
        mock_subtensor.sign_and_send_extrinsic.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_when_destination_equals_origin(
        self, mock_wallet, mock_subtensor
    ):
        mock_subtensor.subnet_exists = AsyncMock(return_value=True)
        mock_subtensor.does_hotkey_exist = AsyncMock(return_value=True)
        mock_subtensor.substrate.query_map = AsyncMock(
            return_value=_query_map_with([(HOTKEY_SS58, _raw_lock_state())])
        )

        ok, _ = await lock_mod.move_lock(
            wallet=mock_wallet,
            subtensor=mock_subtensor,
            destination_hotkey_ss58=HOTKEY_SS58,
            netuid=1,
            era=16,
            prompt=False,
            mev_protection=False,
        )
        assert ok is False
        mock_subtensor.sign_and_send_extrinsic.assert_not_awaited()


# ---------------------------------------------------------------------------
# set_perpetual_lock extrinsic
# ---------------------------------------------------------------------------


class TestSetPerpetualLock:
    @pytest.mark.asyncio
    async def test_happy_path_enable(self, mock_wallet, mock_subtensor):
        mock_subtensor.subnet_exists = AsyncMock(return_value=True)
        # currently decaying (no DecayingLock entry); existing lock present
        mock_subtensor.query = AsyncMock(return_value=None)
        mock_subtensor.substrate.query_map = AsyncMock(
            return_value=_query_map_with([(HOTKEY_SS58, _raw_lock_state())])
        )
        receipt = _make_receipt("0x999")
        mock_subtensor.sign_and_send_extrinsic = AsyncMock(
            return_value=(True, "", receipt)
        )

        with (
            patch(f"{MODULE}.unlock_key", return_value=MagicMock(success=True)),
            patch(f"{MODULE}.print_extrinsic_id", new_callable=AsyncMock),
        ):
            ok, ext_id = await lock_mod.set_perpetual_lock(
                wallet=mock_wallet,
                subtensor=mock_subtensor,
                netuid=1,
                enabled=True,
                era=16,
                prompt=False,
            )

        assert ok is True
        assert ext_id == "0x999"
        kwargs = mock_subtensor.substrate.compose_call.call_args.kwargs
        assert kwargs["call_function"] == "set_perpetual_lock"
        assert kwargs["call_params"] == {"netuid": 1, "enabled": True}

    @pytest.mark.asyncio
    async def test_noop_when_already_in_target_state(self, mock_wallet, mock_subtensor):
        mock_subtensor.subnet_exists = AsyncMock(return_value=True)
        # currently perpetual; user requests enable → no extrinsic
        mock_subtensor.query = AsyncMock(return_value={"already": "perpetual"})
        mock_subtensor.substrate.query_map = AsyncMock(
            return_value=_query_map_with([(HOTKEY_SS58, _raw_lock_state())])
        )

        ok, _ = await lock_mod.set_perpetual_lock(
            wallet=mock_wallet,
            subtensor=mock_subtensor,
            netuid=1,
            enabled=True,
            era=16,
            prompt=False,
        )
        assert ok is True
        mock_subtensor.sign_and_send_extrinsic.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_when_no_existing_lock(self, mock_wallet, mock_subtensor):
        mock_subtensor.subnet_exists = AsyncMock(return_value=True)
        mock_subtensor.query = AsyncMock(return_value=None)
        mock_subtensor.substrate.query_map = AsyncMock(return_value=_query_map_with([]))

        ok, _ = await lock_mod.set_perpetual_lock(
            wallet=mock_wallet,
            subtensor=mock_subtensor,
            netuid=1,
            enabled=True,
            era=16,
            prompt=False,
        )
        assert ok is False


# ---------------------------------------------------------------------------
# list_locks read view
# ---------------------------------------------------------------------------


class TestListLocks:
    @pytest.mark.asyncio
    async def test_returns_data_structure(self, mock_subtensor):
        mock_subtensor.substrate.query_map = AsyncMock(
            return_value=_query_map_with([(HOTKEY_SS58, _raw_lock_state(rao=2_500))])
        )
        mock_subtensor.query_runtime_api = AsyncMock(
            return_value=_raw_lock_state(rao=2_400, last_update=200)
        )
        mock_subtensor.query = AsyncMock(return_value=None)
        mock_subtensor.all_subnets = AsyncMock(return_value=[])

        data = await lock_mod.list_locks(
            subtensor=mock_subtensor,
            coldkey_ss58=COLDKEY_SS58,
            netuid=1,
            json_output=True,
        )

        assert data["coldkey"] == COLDKEY_SS58
        assert data["netuid"] == 1
        assert data["perpetual"] is False
        assert len(data["locks"]) == 1
        assert data["locks"][0]["hotkey"] == HOTKEY_SS58
        assert data["locks"][0]["locked_mass_rao"] == 2_500
        assert data["rolled_forward"]["locked_mass_rao"] == 2_400

    @pytest.mark.asyncio
    async def test_handles_no_rolled_view(self, mock_subtensor):
        mock_subtensor.substrate.query_map = AsyncMock(return_value=_query_map_with([]))
        mock_subtensor.query_runtime_api = AsyncMock(return_value=None)
        mock_subtensor.query = AsyncMock(return_value=None)
        mock_subtensor.all_subnets = AsyncMock(return_value=[])

        data = await lock_mod.list_locks(
            subtensor=mock_subtensor,
            coldkey_ss58=COLDKEY_SS58,
            netuid=1,
            json_output=True,
        )
        assert data["locks"] == []
        assert data["rolled_forward"] is None


# ---------------------------------------------------------------------------
# show_conviction read view
# ---------------------------------------------------------------------------


class TestShowConviction:
    @pytest.mark.asyncio
    async def test_with_hotkey(self, mock_subtensor):
        async def _runtime(runtime_api, method, params, block_hash=None):
            if method == "get_hotkey_conviction":
                return 12345  # raw fixed-point value
            if method == "get_most_convicted_hotkey_on_subnet":
                return HOTKEY_SS58
            return None

        mock_subtensor.query_runtime_api = AsyncMock(side_effect=_runtime)

        data = await lock_mod.show_conviction(
            subtensor=mock_subtensor,
            netuid=1,
            hotkey_ss58=HOTKEY_SS58,
            json_output=True,
        )
        assert data["king"] == HOTKEY_SS58
        assert data["conviction"] is not None

    @pytest.mark.asyncio
    async def test_without_hotkey_only_returns_king(self, mock_subtensor):
        mock_subtensor.query_runtime_api = AsyncMock(return_value=ALT_HOTKEY_SS58)

        data = await lock_mod.show_conviction(
            subtensor=mock_subtensor,
            netuid=1,
            hotkey_ss58=None,
            json_output=True,
        )
        assert data["king"] == ALT_HOTKEY_SS58
        assert data["conviction"] is None
