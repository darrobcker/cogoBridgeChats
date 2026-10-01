from __future__ import annotations

import pytest

from chats import core

T0 = 1_790_000_000.0


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock):
    s = core.open_store()
    s.set_clock(clock)
    s.nudged = []
    s.on_nudge = s.nudged.append
    return s


class World:
    """People, and contacts made the only way there is: one invites, the other connects, the first confirms."""

    def __init__(self, store):
        self.store = store
        self.p: dict[str, str] = {}

    def person(self, name: str) -> str:
        self.p[name] = core.new_person(self.store, name)
        return self.p[name]

    def contact(self, a: str, b: str) -> str:
        made = core.invite(self.store, self.p[a], [b])[0]
        chat = core.connect(self.store, self.p[b], made["code"])["chat"]
        core.confirm(self.store, self.p[a], chat, b)
        return chat

    def group(self, owner: str, name: str, *others: str) -> str:
        chat, _ = core.start_group(self.store, self.p[owner], name, list(others))
        for o in others:
            core.accept(self.store, self.p[o], chat)
        return chat

    def seq(self, chat: str) -> int:
        return self.store.one("SELECT seq FROM chats WHERE id=?", chat)["seq"]


@pytest.fixture
def world(store) -> World:
    return World(store)


def connection(store, *, invite: str = "", name: str = "") -> tuple[str, str]:
    """(the grant, its access token): what an app holds once its person pressed Allow, made without the pages."""
    request = core.sign_in_request(store, {"client_id": "app", "redirect_uri": "https://app.test/cb", "state": "s",
                                            "scopes": []})
    _, code = core.allow(store, request, invite, name)
    access, _ = core.exchange_code(store, code)
    return core.access(store, access)["grant"], access
