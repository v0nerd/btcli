"""Tests for `wallets.verify` crypto-type detection.

Covers the case where the signature was produced with ED25519 but the verifier
must still pick the right scheme — and the explicit-override and both-fail
paths.
"""

import pytest

from bittensor_wallet import CRYPTO_ED25519, CRYPTO_SR25519, Keypair

from bittensor_cli.src.bittensor.utils import CryptoType
from bittensor_cli.src.commands.wallets import verify


MESSAGE = "hello world"


@pytest.fixture(scope="module")
def sr_keypair() -> Keypair:
    return Keypair.create_from_uri("//Alice", crypto_type=CRYPTO_SR25519)


@pytest.fixture(scope="module")
def ed_keypair() -> Keypair:
    return Keypair.create_from_uri("//Alice", crypto_type=CRYPTO_ED25519)


@pytest.fixture(scope="module")
def sr_sig_hex(sr_keypair: Keypair) -> str:
    return sr_keypair.sign(MESSAGE.encode("utf-8")).hex()


@pytest.fixture(scope="module")
def ed_sig_hex(ed_keypair: Keypair) -> str:
    return ed_keypair.sign(MESSAGE.encode("utf-8")).hex()


class TestVerifyAutoDetect:
    @pytest.mark.asyncio
    async def test_sr_signature_auto_detected(self, sr_keypair, sr_sig_hex):
        assert await verify(MESSAGE, sr_sig_hex, sr_keypair.ss58_address) is True

    @pytest.mark.asyncio
    async def test_ed_signature_auto_detected(self, ed_keypair, ed_sig_hex):
        assert await verify(MESSAGE, ed_sig_hex, ed_keypair.ss58_address) is True

    @pytest.mark.asyncio
    async def test_garbage_signature_fails(self, sr_keypair):
        bogus = "00" * 64
        assert await verify(MESSAGE, bogus, sr_keypair.ss58_address) is False

    @pytest.mark.asyncio
    async def test_wrong_message_fails(self, sr_keypair, sr_sig_hex):
        assert (
            await verify("different message", sr_sig_hex, sr_keypair.ss58_address)
            is False
        )


class TestVerifyExplicitCryptoType:
    @pytest.mark.asyncio
    async def test_explicit_sr_accepts_sr_signature(self, sr_keypair, sr_sig_hex):
        assert (
            await verify(
                MESSAGE,
                sr_sig_hex,
                sr_keypair.ss58_address,
                crypto_type=CryptoType.SR25519,
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_explicit_ed_accepts_ed_signature(self, ed_keypair, ed_sig_hex):
        assert (
            await verify(
                MESSAGE,
                ed_sig_hex,
                ed_keypair.ss58_address,
                crypto_type=CryptoType.ED25519,
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_explicit_sr_rejects_ed_signature(self, ed_keypair, ed_sig_hex):
        # ED signature against the ED-derived address but verifier forced to SR
        # — must NOT auto-fall-through to ED25519.
        assert (
            await verify(
                MESSAGE,
                ed_sig_hex,
                ed_keypair.ss58_address,
                crypto_type=CryptoType.SR25519,
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_explicit_ed_rejects_sr_signature(self, sr_keypair, sr_sig_hex):
        assert (
            await verify(
                MESSAGE,
                sr_sig_hex,
                sr_keypair.ss58_address,
                crypto_type=CryptoType.ED25519,
            )
            is False
        )


class TestVerifyInvalidInputs:
    @pytest.mark.asyncio
    async def test_invalid_ss58_returns_false(self, sr_sig_hex):
        assert await verify(MESSAGE, sr_sig_hex, "not-an-address") is False

    @pytest.mark.asyncio
    async def test_invalid_signature_hex_returns_false(self, sr_keypair):
        assert await verify(MESSAGE, "nothex", sr_keypair.ss58_address) is False


class TestVerifyWithPublicKeyHex:
    @pytest.mark.asyncio
    async def test_ed_pubkey_hex_auto_detected(self, ed_keypair, ed_sig_hex):
        pubkey_hex = ed_keypair.public_key.hex()
        assert await verify(MESSAGE, ed_sig_hex, pubkey_hex) is True

    @pytest.mark.asyncio
    async def test_sr_pubkey_hex_with_0x_prefix(self, sr_keypair, sr_sig_hex):
        pubkey_hex = "0x" + sr_keypair.public_key.hex()
        assert await verify(MESSAGE, sr_sig_hex, pubkey_hex) is True
