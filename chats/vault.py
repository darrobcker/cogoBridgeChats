"""Locks (PROTOCOL.md §5): what people and their assistants write is stored sealed, under keys that only their own
connections hold.

Each person has a key pair. Its private half is stored only locked: under a sign-in's renewal token, a run link's
secret, or a code, which the server keeps only as hashes. So the server opens a person's things only in a call that
came through one of their connections, and holds the key for that call alone; nothing it stores, and nothing in a
backup, opens anything by itself. Each chat has a key of its own, sealed to each member. What passes through the
server while a call runs is still in its memory: this protects what is kept, not what is being delivered.

`cryptography`'s primitives, nothing of our own: X25519 to seal to a person, HKDF to derive keys from tokens,
scrypt from the short codes people copy by hand, and ChaCha20-Poly1305 for everything locked. The same as Bridge's
(github.com/darrobcker/cogoBridge, bridge/vault.py), kept apart so that each server stands alone.
"""
from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

V = "v1."           # every locked value starts so; a later scheme takes another


class Locked(Exception):
    """Nothing in this call opens that: its key is held by a connection this call did not come through."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def encode(raw: bytes) -> str:
    return _b64(raw)


def decode(text: str) -> bytes:
    return _unb64(text)


def new_key() -> bytes:
    return os.urandom(32)


def lock(key: bytes, data: bytes, purpose: str) -> str:
    """`data` locked under `key`, bound to what it is for, so one value cannot be passed off as another."""
    nonce = os.urandom(12)
    return V + _b64(nonce + ChaCha20Poly1305(key).encrypt(nonce, data, purpose.encode()))


def unlock(key: bytes, blob: str, purpose: str) -> bytes:
    if not blob.startswith(V):
        raise Locked()
    raw = _unb64(blob[len(V):])
    try:
        return ChaCha20Poly1305(key).decrypt(raw[:12], raw[12:], purpose.encode())
    except (InvalidTag, ValueError):
        raise Locked() from None


def lock_text(key: bytes, text: str, purpose: str) -> str:
    """Empty stays empty: that a field is unset is not something anyone wrote."""
    return lock(key, text.encode(), purpose) if text else ""


def unlock_text(key: bytes, blob: str, purpose: str) -> str:
    return unlock(key, blob, purpose).decode() if blob else ""


def from_secret(secret: str, purpose: str) -> bytes:
    """A key from a token or a link's secret: 256 bits from `secrets`, so one fast step is enough."""
    return HKDF(hashes.SHA256(), 32, salt=None, info=purpose.encode()).derive(secret.encode())


SCRYPT_N = 2 ** 14      # about 16 MB and a few hundredths of a second for each code


@functools.lru_cache(maxsize=512)
def from_code(code: str, purpose: str, salt: str = "") -> bytes:
    """A key from a code people copy by hand, 50 to 59 bits: scrypt, so a copy of the database cannot try them all,
    salted with the row it belongs to where the row is known, so one guess cannot be tried against every row at once
    (review). Kept, so a code tried again does not cost the work again."""
    return Scrypt(salt=f"{purpose}:{salt}".encode(), length=32, n=SCRYPT_N, r=8, p=1).derive(code.encode())


def new_pair() -> tuple[bytes, str]:
    """A person's key pair: the private half, which is only ever stored locked, and the public half, stored as is."""
    private = X25519PrivateKey.generate()
    return private.private_bytes_raw(), _b64(private.public_key().public_bytes_raw())


def _sealing_key(shared: bytes, ephemeral: bytes, public: bytes, purpose: str) -> bytes:
    return HKDF(hashes.SHA256(), 32, salt=ephemeral + public, info=purpose.encode()).derive(shared)


def seal(public: str, data: bytes, purpose: str) -> str:
    """`data` for the holder of `public`'s private half alone: anyone may seal, only they open."""
    ephemeral, to = X25519PrivateKey.generate(), _unb64(public)
    mine = ephemeral.public_key().public_bytes_raw()
    key = _sealing_key(ephemeral.exchange(X25519PublicKey.from_public_bytes(to)), mine, to, purpose)
    nonce = os.urandom(12)
    return V + _b64(mine + nonce + ChaCha20Poly1305(key).encrypt(nonce, data, purpose.encode()))


def open_sealed(private: bytes, blob: str, purpose: str) -> bytes:
    if not blob.startswith(V):
        raise Locked()
    raw = _unb64(blob[len(V):])
    me = X25519PrivateKey.from_private_bytes(private)
    try:
        key = _sealing_key(me.exchange(X25519PublicKey.from_public_bytes(raw[:32])), raw[:32],
                           me.public_key().public_bytes_raw(), purpose)
        return ChaCha20Poly1305(key).decrypt(raw[32:44], raw[44:], purpose.encode())
    except (InvalidTag, ValueError):
        raise Locked() from None


def self_key(private: bytes) -> bytes:
    """What a person keeps for themselves alone — name, contact, notes — is locked under this."""
    return HKDF(hashes.SHA256(), 32, salt=None, info=b"chats self").derive(private)


def mark(key: bytes, text: str) -> str:
    """The same text gives the same mark under the same key, and the mark gives nothing back."""
    return hmac.new(key, text.encode(), hashlib.sha256).hexdigest()


def code_hash(code: str) -> str:
    """How an invite code is looked up. The key it opens takes scrypt; this only finds the row."""
    return hashlib.sha256(("invite " + code).encode()).hexdigest()


# -- the keys a call holds ---------------------------------------------------------------------------------------

_RING: ContextVar[dict | None] = ContextVar("chats_keys", default=None)
# Outside any call's own keys: the operator's tools, and tests, whose threads share what each made. A call through a
# connection always has keys of its own (`keys()`), so it never reads this.
_SHARED: dict | None = None


def _ring() -> dict | None:
    ring = _RING.get()
    return ring if ring is not None else _SHARED


def key(person_id: str) -> bytes:
    """The private key of a person whose connection this call came through."""
    ring = _ring()
    if ring is None or person_id not in ring:
        raise Locked()
    return ring[person_id]


def key_or_none(person_id: str) -> bytes | None:
    ring = _ring()
    return ring.get(person_id) if ring else None


def held() -> dict:
    return dict(_ring() or {})


def hold(person_id: str, private: bytes) -> None:
    ring = _ring()
    if ring is None:
        ring = {}
        _RING.set(ring)
    ring[person_id] = private


# What a call has already opened, kept for that call: a `check` opens the same community's key for every need.
_OPENED: ContextVar[dict | None] = ContextVar("chats_opened", default=None)


def opened(what: tuple, open_it):
    """`open_it()`, once per call for the same `what`."""
    seen = _OPENED.get()
    if seen is None:
        return open_it()
    if what not in seen:
        seen[what] = open_it()
    return seen[what]


@contextmanager
def keys() -> Iterator[None]:
    """One call's keys, and what it opened with them, gone when it returns, whatever thread runs the next."""
    token, kept = _RING.set({}), _OPENED.set({})
    try:
        yield
    finally:
        _OPENED.reset(kept)
        _RING.reset(token)
