"""Ed25519 over stdlib `hashlib` — the receipt signature primitive (§6.6).

§6.6 requires a detached Ed25519 signature beside every receipt, public
verification keys that are committed, a finalizer-held signing key, and
rotation by appending a public key so old receipts verify forever. None of
that works with a hash or an HMAC: a committed verification key is only
meaningful if the signature it checks was produced by a key nobody else
holds.

**Why this is arithmetic here rather than a library call.** The suite runs
under `uv run adws/adw_test.py`, whose PEP 723 header declares exactly
four dependencies (pydantic, python-dotenv, pyyaml, rich). Measured in
that interpreter, `cryptography` and `nacl` both raise
`ModuleNotFoundError`; only `hashlib` and `secrets` are present. Adding a
dependency was not permitted for this step, and Ed25519's definition
(RFC 8032) needs nothing but SHA-512 and integer arithmetic, both of which
the standard library supplies. So the scheme is implemented here, directly
from RFC 8032 §5.1 and §6, and pinned to that document's own published
test vectors — interoperability with every other Ed25519 verifier is a
tested property, not an assumption, because a self-consistent but
non-conforming implementation would round-trip happily and make the
committed public key worthless.

**Stated limitation.** This is a straightforward bignum implementation and
is *not* constant-time; a local attacker able to time `sign` precisely and
repeatedly could in principle learn something about the scalar. The
signing key is finalizer-held and local, signing happens once per receipt
on the finalizer's own machine, and verification — the operation that runs
everywhere — touches only public data. If a vetted Ed25519 dependency is
ever admitted to the suite's environment, `sign`/`verify`/
`seed_to_public_key` are the whole surface to swap, and these vectors keep
the swap honest.

The 32-byte value this module calls a *seed* is what RFC 8032 calls the
private key: the public key is derived from it, never stored beside it, so
private-key loss cannot invalidate a receipt already signed (§6.6).
"""

from __future__ import annotations

import hashlib
import secrets

NO_BLOB = "NO_BLOB"
NO_PRIOR_REF = "NO_PRIOR_REF"

__all__ = [
    "KeyMaterialError",
    "NO_BLOB",
    "NO_PRIOR_REF",
    "SEED_SIZE",
    "PUBLIC_KEY_SIZE",
    "SIGNATURE_SIZE",
    "generate_seed",
    "seed_to_public_key",
    "sign",
    "verify",
]

SEED_SIZE = 32
PUBLIC_KEY_SIZE = 32
SIGNATURE_SIZE = 64


class KeyMaterialError(ValueError):
    """Key material of the wrong size.

    Distinguished from a failed verification on purpose: a wrong-sized key
    is a configuration defect the operator must fix, while a wrong
    signature is a verdict about a receipt. §6.6 forbids only the second
    from being treated as absence.
    """


# ── the curve (RFC 8032 §5.1) ───────────────────────────────────────────────

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493


def _modp_inv(x: int) -> int:
    return pow(x, _P - 2, _P)


_D = -121665 * _modp_inv(121666) % _P
_MODP_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _sha512_modq(data: bytes) -> int:
    return int.from_bytes(_sha512(data), "little") % _L


# Points are extended homogeneous coordinates (X, Y, Z, T) with x = X/Z,
# y = Y/Z and x*y = T/Z.


def _point_add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % _P
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % _P
    C = 2 * P[3] * Q[3] * _D % _P
    D = 2 * P[2] * Q[2] % _P
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F, G * H, F * G, E * H)


def _point_mul(s: int, P):
    Q = (0, 1, 1, 0)  # the neutral element
    while s > 0:
        if s & 1:
            Q = _point_add(Q, P)
        P = _point_add(P, P)
        s >>= 1
    return Q


def _point_equal(P, Q) -> bool:
    if (P[0] * Q[2] - Q[0] * P[2]) % _P != 0:
        return False
    if (P[1] * Q[2] - Q[1] * P[2]) % _P != 0:
        return False
    return True


def _recover_x(y: int, sign_bit: int):
    if y >= _P:
        return None
    x2 = (y * y - 1) * _modp_inv(_D * y * y + 1) % _P
    if x2 == 0:
        return None if sign_bit else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _MODP_SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign_bit:
        x = _P - x
    return x


_G_Y = 4 * _modp_inv(5) % _P
_G_X = _recover_x(_G_Y, 0)
_G = (_G_X, _G_Y, 1, _G_X * _G_Y % _P)


def _point_compress(P) -> bytes:
    zinv = _modp_inv(P[2])
    x = P[0] * zinv % _P
    y = P[1] * zinv % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _point_decompress(data: bytes):
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign_bit = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign_bit)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _secret_expand(seed: bytes):
    if len(seed) != SEED_SIZE:
        raise KeyMaterialError(
            f"an Ed25519 seed is exactly {SEED_SIZE} bytes; got {len(seed)}"
        )
    h = _sha512(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


# ── the public surface ───────────────────────────────────────────────────────


def generate_seed() -> bytes:
    """A fresh finalizer signing key, from the OS CSPRNG."""
    return secrets.token_bytes(SEED_SIZE)


def seed_to_public_key(seed: bytes) -> bytes:
    """The committed verification key for a signing seed (§6.6).

    Derived rather than stored, which is why losing the seed leaves every
    already-signed receipt verifiable: the public key was published, and
    nothing about verification needs the seed back.
    """
    a, _prefix = _secret_expand(seed)
    return _point_compress(_point_mul(a, _G))


def sign(seed: bytes, message: bytes) -> bytes:
    """A detached 64-byte signature over `message` (RFC 8032 §5.1.6).

    Deterministic: the nonce comes from the key's own hash prefix and the
    message, never from a random source, so the same receipt bytes always
    carry the same signature.
    """
    a, prefix = _secret_expand(seed)
    public = _point_compress(_point_mul(a, _G))
    r = _sha512_modq(prefix + message)
    encoded_r = _point_compress(_point_mul(r, _G))
    h = _sha512_modq(encoded_r + public + message)
    s = (r + h * a) % _L
    return encoded_r + int.to_bytes(s, 32, "little")


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Whether `signature` is `public_key`'s signature over `message`.

    Returns False for every invalid signature — wrong key, altered
    message, altered signature, malformed encoding, or a non-canonical
    scalar — so the caller has exactly one branch to refuse on. It never
    returns True for material it could not fully check, which is the
    property §6.6 leans on when it forbids treating an invalid signature
    as an absent one.
    """
    if len(public_key) != PUBLIC_KEY_SIZE:
        raise KeyMaterialError(
            f"an Ed25519 public key is exactly {PUBLIC_KEY_SIZE} bytes; "
            f"got {len(public_key)}"
        )
    if len(signature) != SIGNATURE_SIZE:
        return False
    A = _point_decompress(public_key)
    if A is None:
        return False
    encoded_r = signature[:32]
    R = _point_decompress(encoded_r)
    if R is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        # Non-canonical: reducing here would admit a family of malleable
        # variants of one valid signature.
        return False
    h = _sha512_modq(encoded_r + public_key + message)
    return _point_equal(_point_mul(s, _G), _point_add(R, _point_mul(h, A)))
