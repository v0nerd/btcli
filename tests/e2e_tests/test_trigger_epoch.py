import asyncio
import json

import pytest

from .utils import turn_off_hyperparam_freeze_window

"""
Verify commands:

* btcli subnets create
* btcli sudo trigger-epoch
"""


def test_trigger_epoch(local_chain, wallet_setup):
    netuid = 2
    wallet_path_alice = "//Alice"
    wallet_path_bob = "//Bob"

    keypair_alice, wallet_alice, wallet_path_alice, exec_command_alice = wallet_setup(
        wallet_path_alice
    )
    keypair_bob, wallet_bob, wallet_path_bob, exec_command_bob = wallet_setup(
        wallet_path_bob
    )

    # The owner-side trigger_epoch extrinsic only exists on dynamic-tempo runtimes.
    try:
        asyncio.run(
            local_chain.compose_call(
                call_module="SubtensorModule",
                call_function="trigger_epoch",
                call_params={"netuid": netuid},
            )
        )
    except ValueError:
        pytest.skip(
            "Chain does not support SubtensorModule.trigger_epoch "
            "(pre-dynamic-tempo runtime)."
        )

    # With the freeze window on, a fresh subnet's next auto epoch can be close
    # enough that trigger_epoch fails with AutoEpochAlreadyImminent.
    try:
        asyncio.run(turn_off_hyperparam_freeze_window(local_chain, wallet_alice))
    except ValueError:
        print(
            "Skipping turning off hyperparams freeze window. This indicates the call does not exist on the chain you are testing."
        )

    # Register a subnet with sudo as Alice
    result = exec_command_alice(
        command="subnets",
        sub_command="create",
        extra_args=[
            "--wallet-path",
            wallet_path_alice,
            "--network",
            "ws://127.0.0.1:9945",
            "--wallet-name",
            wallet_alice.name,
            "--wallet-hotkey",
            wallet_alice.hotkey_str,
            "--subnet-name",
            "Test Subnet",
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
    result_output = json.loads(result.stdout)
    assert result_output["success"] is True
    assert result_output["netuid"] == netuid

    # A non-owner cannot trigger an epoch
    cmd = exec_command_bob(
        command="sudo",
        sub_command="trigger-epoch",
        extra_args=[
            "--wallet-path",
            wallet_path_bob,
            "--network",
            "ws://127.0.0.1:9945",
            "--wallet-name",
            wallet_bob.name,
            "--wallet-hotkey",
            wallet_bob.hotkey_str,
            "--netuid",
            netuid,
            "--json-out",
            "--no-prompt",
        ],
    )
    cmd_json = json.loads(cmd.stdout)
    assert cmd_json["success"] is False, (cmd.stdout, cmd_json)
    assert "doesn't own" in cmd_json["message"]

    # The subnet owner triggers an epoch
    cmd = exec_command_alice(
        command="sudo",
        sub_command="trigger-epoch",
        extra_args=[
            "--wallet-path",
            wallet_path_alice,
            "--network",
            "ws://127.0.0.1:9945",
            "--wallet-name",
            wallet_alice.name,
            "--wallet-hotkey",
            wallet_alice.hotkey_str,
            "--netuid",
            netuid,
            "--json-out",
            "--no-prompt",
        ],
    )
    cmd_json = json.loads(cmd.stdout)
    assert cmd_json["success"] is True, (cmd.stdout, cmd_json)
    assert isinstance(cmd_json["extrinsic_identifier"], str)
    # fires_at is read from the EpochTriggered event; it should decode on a
    # dynamic-tempo chain.
    assert isinstance(cmd_json["fires_at"], int), cmd_json
    print(f"Successfully triggered epoch on SN{netuid}")
