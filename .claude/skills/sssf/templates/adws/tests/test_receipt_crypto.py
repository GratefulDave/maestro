"""Executable proof of the receipt signature primitive (§6.6).

§6.6 requires a detached Ed25519 signature over a receipt's stored bytes,
public verification keys that are committed, a finalizer-held signing key,
and the property that losing the signing key still leaves every existing
receipt verifiable. That is a signature scheme, not a hash, so the
primitive has to be correct rather than plausible.

Correctness here is settled against RFC 8032's own published test vectors
rather than against a round-trip with ourselves: a broken implementation
that signs and verifies consistently would pass a round-trip test and
produce signatures no other Ed25519 verifier accepts, which is exactly the
failure that makes a committed public key worthless.

The suite's interpreter has no `cryptography` and no `nacl` (measured, not
assumed -- see the module docstring of `receipt_crypto`), so the primitive
is stdlib `hashlib` arithmetic and these vectors are the only thing
standing between it and a private, incompatible signature format.

Run with:  uv run adws/adw_test.py -k crypto
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import receipt_crypto as rc  # noqa: E402

# RFC 8032 §7.1 -- Ed25519 test vectors 1, 2 and 3, verbatim.
RFC_8032_VECTORS = (
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8"
        "821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085a"
        "c1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff"
        "9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
)


class Ed25519AgainstRFC8032(unittest.TestCase):
    """Interoperability, not self-consistency."""

    def test_public_key_derivation_matches_every_vector(self):
        for seed_hex, public_hex, _msg, _sig in RFC_8032_VECTORS:
            with self.subTest(seed=seed_hex[:8]):
                derived = rc.seed_to_public_key(bytes.fromhex(seed_hex))
                self.assertEqual(derived.hex(), public_hex)

    def test_signatures_match_every_vector_byte_for_byte(self):
        for seed_hex, _public_hex, msg_hex, sig_hex in RFC_8032_VECTORS:
            with self.subTest(seed=seed_hex[:8]):
                produced = rc.sign(bytes.fromhex(seed_hex),
                                   bytes.fromhex(msg_hex))
                self.assertEqual(produced.hex(), sig_hex)

    def test_every_vector_signature_verifies(self):
        for _seed_hex, public_hex, msg_hex, sig_hex in RFC_8032_VECTORS:
            with self.subTest(public=public_hex[:8]):
                self.assertTrue(rc.verify(bytes.fromhex(public_hex),
                                          bytes.fromhex(msg_hex),
                                          bytes.fromhex(sig_hex)))

    def test_signing_is_deterministic(self):
        """Ed25519 signatures are deterministic, which is what lets a
        receipt's signature be compared and re-derived rather than only
        verified."""
        seed = bytes.fromhex(RFC_8032_VECTORS[2][0])
        message = b"receipt bytes"
        self.assertEqual(rc.sign(seed, message), rc.sign(seed, message))


class ForgeryIsRejected(unittest.TestCase):
    """§6.6's whole point: an invalid signature is a hard error, never
    treated as absent."""

    def setUp(self):
        self.seed = rc.generate_seed()
        self.public = rc.seed_to_public_key(self.seed)
        self.message = b'{"digest": "abc", "verdict": "PASS"}'
        self.signature = rc.sign(self.seed, self.message)

    def test_untampered_pair_verifies(self):
        self.assertTrue(rc.verify(self.public, self.message, self.signature))

    def test_a_changed_message_does_not_verify(self):
        forged = self.message.replace(b"PASS", b"FAIL")
        self.assertNotEqual(forged, self.message)
        self.assertFalse(rc.verify(self.public, forged, self.signature))

    def test_a_changed_signature_does_not_verify(self):
        flipped = bytearray(self.signature)
        flipped[0] ^= 0x01
        self.assertFalse(rc.verify(self.public, self.message, bytes(flipped)))

    def test_another_signers_key_does_not_verify(self):
        other_public = rc.seed_to_public_key(rc.generate_seed())
        self.assertFalse(rc.verify(other_public, self.message, self.signature))

    def test_a_non_canonical_scalar_is_rejected(self):
        """s >= L is rejected rather than reduced -- otherwise one valid
        signature admits a family of malleable variants."""
        group_order = (1 << 252) + 27742317777372353535851937790883648493
        mangled = self.signature[:32] + (group_order + 1).to_bytes(32, "little")
        self.assertFalse(rc.verify(self.public, self.message, mangled))


class KeyMaterialIsChecked(unittest.TestCase):

    def test_a_short_seed_is_a_typed_error(self):
        with self.assertRaises(rc.KeyMaterialError):
            rc.sign(b"too short", b"message")

    def test_a_short_public_key_is_a_typed_error(self):
        with self.assertRaises(rc.KeyMaterialError):
            rc.verify(b"too short", b"message", b"\x00" * 64)

    def test_a_wrong_length_signature_is_false_not_an_error(self):
        """A malformed signature is a failed verification, not a crash:
        the caller's job is to refuse the receipt, and it refuses on the
        same branch for every invalid signature."""
        seed = rc.generate_seed()
        public = rc.seed_to_public_key(seed)
        self.assertFalse(rc.verify(public, b"message", b"\x00" * 63))

    def test_generated_seeds_are_32_bytes_and_distinct(self):
        first, second = rc.generate_seed(), rc.generate_seed()
        self.assertEqual(len(first), 32)
        self.assertEqual(len(second), 32)
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
