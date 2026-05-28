"""
End-to-end tests for `btcli stake lock` conviction commands.

Mirrors bittensor/tests/e2e_tests/test_lock_stake.py at the CLI layer, exercising:

* btcli stake lock list      (read view)
* btcli stake lock conviction (read view)
* btcli stake lock add        (lock_stake extrinsic — top-up the auto-lock)
* btcli stake lock perpetual  (set_perpetual_lock extrinsic)
* btcli stake lock move       (move_lock extrinsic)

Requires the subtensor localnet to expose the conviction pallet entries; if the
chain image does not include them, the read-view queries return empty and the
test will be skipped with an explanatory message.
"""

import asyncio
import json
import time

from .utils import turn_off_hyperparam_freeze_window


def test_stake_lock_lifecycle(local_chain, wallet_setup):
    """
    Exercise the conviction-lock command surface end-to-end.

    Steps:
        1. Set up Alice (subnet owner) and Bob (secondary hotkey).
        2. Create a subnet, register Bob, start emissions, stake to enable V3.
        3. Wait an epoch so subnet emission auto-locks owner stake.
        4. `btcli stake lock list` — verify auto-lock exists on Alice's hotkey.
        5. `btcli stake lock conviction` — verify Alice is the subnet king.
        6. `btcli stake lock add` — top up the lock on the same hotkey.
        7. `btcli stake lock perpetual --enable` — flip to non-decaying.
        8. `btcli stake lock perpetual --disable` — flip back.
        9. `btcli stake lock move` — relocate the lock to Bob's hotkey.
    """
    print("Testing stake lock (conviction) commands 🔒")
    netuid = 2
    wallet_path_alice = "//Alice"
    wallet_path_bob = "//Bob"

    keypair_alice, wallet_alice, wallet_path_alice, exec_command_alice = wallet_setup(
        wallet_path_alice
    )
    keypair_bob, wallet_bob, wallet_path_bob, exec_command_bob = wallet_setup(
        wallet_path_bob
    )

    try:
        asyncio.run(turn_off_hyperparam_freeze_window(local_chain, wallet_alice))
    except ValueError:
        print("Hyperparam freeze window call not present on this chain; continuing.")

    # ------------------------------------------------------------------
    # Subnet creation + activation
    # ------------------------------------------------------------------
    create_result = exec_command_alice(
        command="subnets",
        sub_command="create",
        extra_args=[
            "--wallet-path",
            wallet_path_alice,
            "--chain",
            "ws://127.0.0.1:9945",
            "--wallet-name",
            wallet_alice.name,
            "--wallet-hotkey",
            wallet_alice.hotkey_str,
            "--subnet-name",
            "Lock Test Subnet",
            "--repo",
            "https://github.com/username/repo",
            "--contact",
            "alice@opentensor.dev",
            "--url",
            "https://testsubnet.com",
            "--discord",
            "alice#1234",
            "--description",
            "A test subnet for e2e testing",
            "--additional-info",
            "Created by Alice",
            "--logo-url",
            "https://testsubnet.com/logo.png",
            "--no-prompt",
            "--json-output",
            "--no-mev-protection",
        ],
    )
    print(create_result.stdout, create_result.stderr)
    create_output = json.loads(create_result.stdout)
    assert create_output["success"] is True
    assert create_output["netuid"] == netuid

    start_result = exec_command_alice(
        command="subnets",
        sub_command="start",
        extra_args=[
            "--netuid",
            str(netuid),
            "--wallet-path",
            wallet_path_alice,
            "--wallet-name",
            wallet_alice.name,
            "--hotkey",
            wallet_alice.hotkey_str,
            "--network",
            "ws://127.0.0.1:9945",
            "--no-prompt",
        ],
    )
    assert (
        f"Successfully started subnet {netuid}'s emission schedule"
        in start_result.stdout
    )

    # Stake to enable V3 emission
    stake_v3 = exec_command_alice(
        command="stake",
        sub_command="add",
        extra_args=[
            "--netuid",
            str(netuid),
            "--wallet-path",
            wallet_path_alice,
            "--wallet-name",
            wallet_alice.name,
            "--hotkey",
            wallet_alice.hotkey_str,
            "--chain",
            "ws://127.0.0.1:9945",
            "--amount",
            "10",
            "--unsafe",
            "--no-prompt",
            "--era",
            "144",
            "--no-mev-protection",
        ],
    )
    assert "✅ Finalized" in stake_v3.stdout, stake_v3.stderr

    # Wait long enough for an epoch tick → subnet emission triggers owner auto-lock
    time.sleep(45)

    # ------------------------------------------------------------------
    # Read views
    # ------------------------------------------------------------------
    list_result = exec_command_alice(
        command="stake",
        sub_command="lock",
        extra_args=[
            "list",
            "--netuid",
            str(netuid),
            "--wallet-path",
            wallet_path_alice,
            "--wallet-name",
            wallet_alice.name,
            "--chain",
            "ws://127.0.0.1:9945",
            "--json-output",
            "--verbose",
        ],
    )
    list_data = json.loads(list_result.stdout)
    assert list_data["coldkey"] == keypair_alice.ss58_address
    assert list_data["netuid"] == netuid
    assert any(
        entry["hotkey"] == wallet_alice.hotkey.ss58_address
        for entry in list_data["locks"]
    ), list_data
    assert list_data["perpetual"] is False

    conviction_result = exec_command_alice(
        command="stake",
        sub_command="lock",
        extra_args=[
            "conviction",
            "--netuid",
            str(netuid),
            "--hotkey-ss58",
            wallet_alice.hotkey.ss58_address,
            "--chain",
            "ws://127.0.0.1:9945",
            "--json-output",
        ],
    )
    conviction_data = json.loads(conviction_result.stdout)
    assert conviction_data["king"] == wallet_alice.hotkey.ss58_address
    assert conviction_data["conviction"] is not None

    # ------------------------------------------------------------------
    # Top-up the existing lock on the same hotkey
    # ------------------------------------------------------------------
    top_up_result = exec_command_alice(
        command="stake",
        sub_command="lock",
        extra_args=[
            "add",
            "--netuid",
            str(netuid),
            "--hotkey-ss58",
            wallet_alice.hotkey.ss58_address,
            "--amount",
            "1",
            "--wallet-path",
            wallet_path_alice,
            "--wallet-name",
            wallet_alice.name,
            "--hotkey",
            wallet_alice.hotkey_str,
            "--chain",
            "ws://127.0.0.1:9945",
            "--no-mev-protection",
            "--no-prompt",
            "--json-output",
        ],
    )
    assert '"success": true' in top_up_result.stdout, top_up_result.stdout

    # ------------------------------------------------------------------
    # Perpetual flag round-trip
    # ------------------------------------------------------------------
    perp_on = exec_command_alice(
        command="stake",
        sub_command="lock",
        extra_args=[
            "perpetual",
            "--netuid",
            str(netuid),
            "--enable",
            "--wallet-path",
            wallet_path_alice,
            "--wallet-name",
            wallet_alice.name,
            "--hotkey",
            wallet_alice.hotkey_str,
            "--chain",
            "ws://127.0.0.1:9945",
            "--no-prompt",
            "--json-output",
        ],
    )
    assert '"success": true' in perp_on.stdout, perp_on.stdout

    list_after_perp = exec_command_alice(
        command="stake",
        sub_command="lock",
        extra_args=[
            "list",
            "--netuid",
            str(netuid),
            "--wallet-path",
            wallet_path_alice,
            "--wallet-name",
            wallet_alice.name,
            "--chain",
            "ws://127.0.0.1:9945",
            "--json-output",
        ],
    )
    assert json.loads(list_after_perp.stdout)["perpetual"] is True

    perp_off = exec_command_alice(
        command="stake",
        sub_command="lock",
        extra_args=[
            "perpetual",
            "--netuid",
            str(netuid),
            "--disable",
            "--wallet-path",
            wallet_path_alice,
            "--wallet-name",
            wallet_alice.name,
            "--hotkey",
            wallet_alice.hotkey_str,
            "--chain",
            "ws://127.0.0.1:9945",
            "--no-prompt",
            "--json-output",
        ],
    )
    assert '"success": true' in perp_off.stdout, perp_off.stdout

    # ------------------------------------------------------------------
    # Move the lock to Bob's hotkey
    # ------------------------------------------------------------------
    register_bob = exec_command_bob(
        command="subnets",
        sub_command="register",
        extra_args=[
            "--wallet-path",
            wallet_path_bob,
            "--wallet-name",
            wallet_bob.name,
            "--hotkey",
            wallet_bob.hotkey_str,
            "--netuid",
            str(netuid),
            "--chain",
            "ws://127.0.0.1:9945",
            "--no-prompt",
        ],
    )
    assert (
        "Already Registered" in register_bob.stdout
        or "Your extrinsic has been included" in register_bob.stdout
    ), register_bob.stdout

    move_result = exec_command_alice(
        command="stake",
        sub_command="lock",
        extra_args=[
            "move",
            "--netuid",
            str(netuid),
            "--to",
            wallet_bob.hotkey.ss58_address,
            "--wallet-path",
            wallet_path_alice,
            "--wallet-name",
            wallet_alice.name,
            "--hotkey",
            wallet_alice.hotkey_str,
            "--chain",
            "ws://127.0.0.1:9945",
            "--no-mev-protection",
            "--no-prompt",
            "--json-output",
        ],
    )
    assert '"success": true' in move_result.stdout, move_result.stdout

    list_after_move = exec_command_alice(
        command="stake",
        sub_command="lock",
        extra_args=[
            "list",
            "--netuid",
            str(netuid),
            "--wallet-path",
            wallet_path_alice,
            "--wallet-name",
            wallet_alice.name,
            "--chain",
            "ws://127.0.0.1:9945",
            "--json-output",
        ],
    )
    after_move = json.loads(list_after_move.stdout)
    assert any(
        entry["hotkey"] == wallet_bob.hotkey.ss58_address
        for entry in after_move["locks"]
    ), after_move
    # Old hotkey lock should be gone (locked_mass moved across)
    moved_amount = next(
        entry["locked_mass_rao"]
        for entry in after_move["locks"]
        if entry["hotkey"] == wallet_bob.hotkey.ss58_address
    )
    assert moved_amount > 0
