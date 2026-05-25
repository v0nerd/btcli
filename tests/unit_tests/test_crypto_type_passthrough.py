"""Tests that --crypto-type is plumbed through to bittensor_wallet calls."""

import json

import pytest
from unittest.mock import MagicMock, patch
from bittensor_wallet import CRYPTO_ED25519, CRYPTO_SR25519, Keypair

from bittensor_cli.src.bittensor.utils import CryptoType, crypto_type_to_int


MODULE = "bittensor_cli.src.commands.wallets"


class TestCryptoTypeEnum:
    def test_str_values(self):
        assert CryptoType.SR25519.value == "sr25519"
        assert CryptoType.ED25519.value == "ed25519"

    def test_mapping_to_int(self):
        assert crypto_type_to_int(CryptoType.SR25519) == CRYPTO_SR25519
        assert crypto_type_to_int(CryptoType.ED25519) == CRYPTO_ED25519


class TestNewColdkeyCryptoType:
    @pytest.mark.asyncio
    async def test_passes_crypto_type_to_create_new_coldkey(self, mock_wallet):
        from bittensor_cli.src.commands.wallets import new_coldkey

        await new_coldkey(
            wallet=mock_wallet,
            n_words=12,
            use_password=False,
            uri=None,
            json_output=False,
            crypto_type=CRYPTO_ED25519,
        )
        mock_wallet.create_new_coldkey.assert_called_once()
        kwargs = mock_wallet.create_new_coldkey.call_args.kwargs
        assert kwargs["crypto_type"] == CRYPTO_ED25519

    @pytest.mark.asyncio
    async def test_passes_crypto_type_to_create_from_uri(self, mock_wallet):
        from bittensor_cli.src.commands.wallets import new_coldkey

        fake_kp = MagicMock(spec=Keypair)
        with patch.object(
            Keypair, "create_from_uri", return_value=fake_kp
        ) as mock_from_uri:
            await new_coldkey(
                wallet=mock_wallet,
                n_words=12,
                use_password=False,
                uri="//Alice",
                json_output=False,
                crypto_type=CRYPTO_ED25519,
            )
        mock_from_uri.assert_called_once_with("//Alice", crypto_type=CRYPTO_ED25519)


class TestNewHotkeyCryptoType:
    @pytest.mark.asyncio
    async def test_passes_crypto_type_to_create_new_hotkey(self, mock_wallet):
        from bittensor_cli.src.commands.wallets import new_hotkey

        await new_hotkey(
            wallet=mock_wallet,
            n_words=12,
            use_password=False,
            uri=None,
            json_output=False,
            crypto_type=CRYPTO_ED25519,
        )
        kwargs = mock_wallet.create_new_hotkey.call_args.kwargs
        assert kwargs["crypto_type"] == CRYPTO_ED25519


class TestRegenColdkeyCryptoType:
    @pytest.mark.asyncio
    async def test_passes_crypto_type_to_regenerate_coldkey(self, mock_wallet):
        from bittensor_cli.src.commands.wallets import regen_coldkey

        mock_wallet.regenerate_coldkey.return_value = mock_wallet
        await regen_coldkey(
            wallet=mock_wallet,
            mnemonic="m1 m2 m3 m4 m5 m6 m7 m8 m9 m10 m11 m12",
            json_output=False,
            crypto_type=CRYPTO_ED25519,
        )
        kwargs = mock_wallet.regenerate_coldkey.call_args.kwargs
        assert kwargs["crypto_type"] == CRYPTO_ED25519


class TestRegenColdkeyPubCryptoType:
    @pytest.mark.asyncio
    async def test_passes_crypto_type_to_regenerate_coldkeypub(self, mock_wallet):
        from bittensor_cli.src.commands.wallets import regen_coldkey_pub

        mock_wallet.regenerate_coldkeypub.return_value = mock_wallet
        await regen_coldkey_pub(
            wallet=mock_wallet,
            ss58_address="5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
            public_key_hex=None,
            crypto_type=CRYPTO_ED25519,
        )
        kwargs = mock_wallet.regenerate_coldkeypub.call_args.kwargs
        assert kwargs["crypto_type"] == CRYPTO_ED25519


class TestWalletCreateSplitCryptoTypes:
    @pytest.mark.asyncio
    async def test_passes_split_crypto_types(self, mock_wallet):
        from bittensor_cli.src.commands.wallets import wallet_create

        await wallet_create(
            wallet=mock_wallet,
            json_output=False,
            coldkey_crypto_type=CRYPTO_ED25519,
            hotkey_crypto_type=CRYPTO_SR25519,
        )
        cold_kwargs = mock_wallet.create_new_coldkey.call_args.kwargs
        hot_kwargs = mock_wallet.create_new_hotkey.call_args.kwargs
        assert cold_kwargs["crypto_type"] == CRYPTO_ED25519
        assert hot_kwargs["crypto_type"] == CRYPTO_SR25519
