"""Tests for `btcli wallet encrypt` / `btcli wallet decrypt`."""

import json

import pytest
from unittest.mock import MagicMock, patch
from bittensor_wallet import CRYPTO_ED25519, CRYPTO_SR25519, Keypair


MODULE = "bittensor_cli.src.commands.wallets"

VALID_ED25519_SS58 = "5FjpRbCZbsasWaaXweGhLM7R5F6kYxWcXdVF8wYPvQhxXL8w"


# ---------------------------------------------------------------------------
# encrypt_message
# ---------------------------------------------------------------------------


class TestEncryptMessage:
    @pytest.mark.asyncio
    async def test_rejects_invalid_ss58(self):
        from bittensor_cli.src.commands.wallets import encrypt_message

        with patch(f"{MODULE}.print_error") as mock_err:
            result = await encrypt_message("not-an-ss58", "hi", json_output=False)
        assert result is False
        mock_err.assert_called_once()

    @pytest.mark.asyncio
    async def test_calls_encrypt_for_with_ed25519(self):
        from bittensor_cli.src.commands.wallets import encrypt_message

        with patch.object(
            Keypair, "encrypt_for", return_value=b"\x01\x02\x03"
        ) as mock_ef:
            result = await encrypt_message(VALID_ED25519_SS58, "hi", json_output=True)
        assert result is True
        mock_ef.assert_called_once_with(VALID_ED25519_SS58, b"hi", CRYPTO_ED25519)

    @pytest.mark.asyncio
    async def test_json_output_includes_hex(self):
        from bittensor_cli.src.commands.wallets import encrypt_message

        with (
            patch.object(Keypair, "encrypt_for", return_value=b"\xaa\xbb"),
            patch(f"{MODULE}.json_console") as mock_json,
        ):
            await encrypt_message(VALID_ED25519_SS58, "hi", json_output=True)
        output = json.loads(mock_json.print.call_args[0][0])
        assert output["success"] is True
        assert output["data"]["ciphertext_hex"] == "aabb"
        assert output["data"]["recipient"] == VALID_ED25519_SS58


# ---------------------------------------------------------------------------
# decrypt_message
# ---------------------------------------------------------------------------


class TestDecryptMessage:
    @pytest.mark.asyncio
    async def test_rejects_invalid_hex(self, mock_wallet):
        from bittensor_cli.src.commands.wallets import decrypt_message

        with patch(f"{MODULE}.print_error") as mock_err:
            result = await decrypt_message(
                mock_wallet, "not-hex!!", use_hotkey=False, json_output=False
            )
        assert result is False
        mock_err.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_unlock_fails(self, mock_wallet):
        from bittensor_cli.src.commands.wallets import decrypt_message

        unlock_result = MagicMock()
        unlock_result.success = False
        with patch(f"{MODULE}.unlock_key", return_value=unlock_result):
            result = await decrypt_message(
                mock_wallet, "deadbeef", use_hotkey=False, json_output=False
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_rejects_sr25519_coldkey(self, mock_wallet):
        from bittensor_cli.src.commands.wallets import decrypt_message

        unlock_result = MagicMock()
        unlock_result.success = True
        mock_wallet.coldkey.crypto_type = CRYPTO_SR25519
        with (
            patch(f"{MODULE}.unlock_key", return_value=unlock_result),
            patch(f"{MODULE}.print_error") as mock_err,
        ):
            result = await decrypt_message(
                mock_wallet, "deadbeef", use_hotkey=False, json_output=False
            )
        assert result is False
        assert "ED25519" in mock_err.call_args[0][0]

    @pytest.mark.asyncio
    async def test_decrypts_with_ed25519_coldkey(self, mock_wallet):
        from bittensor_cli.src.commands.wallets import decrypt_message

        unlock_result = MagicMock()
        unlock_result.success = True
        mock_wallet.coldkey.crypto_type = CRYPTO_ED25519
        mock_wallet.coldkey.decrypt = MagicMock(return_value=b"hello")
        with (
            patch(f"{MODULE}.unlock_key", return_value=unlock_result),
            patch(f"{MODULE}.json_console") as mock_json,
        ):
            result = await decrypt_message(
                mock_wallet, "0xdeadbeef", use_hotkey=False, json_output=True
            )
        assert result is True
        mock_wallet.coldkey.decrypt.assert_called_once_with(b"\xde\xad\xbe\xef")
        output = json.loads(mock_json.print.call_args[0][0])
        assert output["success"] is True
        assert output["data"]["plaintext"] == "hello"

    @pytest.mark.asyncio
    async def test_uses_hotkey_when_requested(self, mock_wallet):
        from bittensor_cli.src.commands.wallets import decrypt_message

        unlock_result = MagicMock()
        unlock_result.success = True
        mock_wallet.hotkey.crypto_type = CRYPTO_ED25519
        mock_wallet.hotkey.decrypt = MagicMock(return_value=b"world")
        with (
            patch(f"{MODULE}.unlock_key", return_value=unlock_result) as mock_unlock,
            patch(f"{MODULE}.json_console"),
        ):
            await decrypt_message(mock_wallet, "00", use_hotkey=True, json_output=True)
        # unlock_key called with "hot" not "cold"
        assert mock_unlock.call_args[0][1] == "hot"
        mock_wallet.hotkey.decrypt.assert_called_once()
