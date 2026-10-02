"""The locks themselves (bridge/vault.py): what is locked opens only with its key, for its purpose."""
from __future__ import annotations

import pytest

from chats import vault


def test_locked_text_opens_only_with_its_key_and_for_its_purpose():
    key = vault.new_key()
    blob = vault.lock_text(key, "a climbing partner", "need")
    assert blob.startswith(vault.V) and "climbing" not in blob
    assert vault.unlock_text(key, blob, "need") == "a climbing partner"
    for wrong_key, purpose in ((vault.new_key(), "need"), (key, "message")):
        with pytest.raises(vault.Locked):
            vault.unlock_text(wrong_key, blob, purpose)
    assert vault.lock_text(key, "", "need") == "" and vault.unlock_text(key, "", "need") == ""


def test_sealed_to_a_person_opens_only_with_their_private_key():
    private, public = vault.new_pair()
    other, _ = vault.new_pair()
    blob = vault.seal(public, b"community key", "community")
    assert vault.open_sealed(private, blob, "community") == b"community key"
    with pytest.raises(vault.Locked):
        vault.open_sealed(other, blob, "community")
    assert vault.seal(public, b"x", "a") != vault.seal(public, b"x", "a")      # a fresh key each time


def test_a_code_s_key_takes_real_work_at_the_real_factor(monkeypatch):
    monkeypatch.setattr(vault, "SCRYPT_N", 2 ** 14)
    vault.from_code.cache_clear()
    try:
        key = vault.from_code("abcdefghjkmnpq", "invite")
        assert key == vault.from_code("abcdefghjkmnpq", "invite") != vault.from_code("abcdefghjkmnpq", "link code")
    finally:
        vault.from_code.cache_clear()


def test_a_call_holds_only_the_keys_it_opened():
    with vault.keys():
        vault.hold("p-a", b"a" * 32)
        with vault.keys(), pytest.raises(vault.Locked):
            vault.key("p-a")
        assert vault.key("p-a") == b"a" * 32
