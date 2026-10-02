"""Every rule of PROTOCOL.md, in one place. The MCP tools, sign-in and the pages only call these.

A person reaches another only through a contact made with a one-time invite, or a chat they accepted (rule 1).
The voice of a message comes from the operation that sent it, never from an argument (rule 4). Nothing here reads
a message body beyond its length (rule 14).
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from pathlib import Path

from . import vault
from .store import MIGRATIONS, Store

# -- the service profile: every number a server states (rules 7 and 17) --------------------------------------------
NAME_MAX, ABOUT_MAX, NOTE_MAX, FLAG_MAX, WHY_MAX = 80, 2000, 2000, 300, 500
SEND_MAX, REPLY_MAX, OUTCOME_MAX, OPTION_MAX, OPTIONS_MAX = 4000, 800, 500, 100, 10
GROUP_MAX = 50
INVITE_DAYS = 7
INVITES_PER_DAY = 30
CONNECTS_PER_DAY = 20            # tries with a link or a code: a code is short enough to guess at scale
INVITED_PER_DAY = 20             # people made a day by a connection that came with an invite, server-wide
UNINVITED_PER_DAY = 20           # and without one: anyone can sign in, and reaches nobody until an invite is used
APPS_PER_DAY = 1000              # apps registering for sign-in a day, each a row until the sweep
BRAKE_IN_A_ROW = 3               # assistant-voice messages in one chat since its last person-voice message
BRAKE_PER_DAY = 20               # assistant-voice messages per sender per chat per day
BRAKE_CHATS_PER_HOUR = 10        # distinct chats one sender's assistant writes in per hour
DUPLICATE_WINDOW = 600           # an identical person-voice send within this is the same send
NUDGE_EVERY = 600
PAGE, PAGE_CHARS = 20, 40_000
AUDIT_DAYS = 7
REPORT_DAYS = 90
BACKUP_DAYS = 15                 # what erasing promises: scripts/backup keeps 14, a day's margin for a machine off
DAY = 86400
ACCESS_S = 7 * DAY               # an access token; the renewal token that replaces it never expires (PROTOCOL.md §5)
REQUEST_S = 900                  # a sign-in waiting on the Allow page
AUTH_CODE_S = 300                # an authorization code, between Allow and the app's token request
LINK_CODE_S = 3600               # a code from the chat that joins another app's connection to its person
LINK_CODE = 12                   # characters (59 bits) of one, typed by hand from one app into another
VERSION = "0.3"
ESCROW_S = 14 * DAY              # a key from before locks waits this long for its person's connections to copy it

_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"      # ids and codes are copied by hand: nothing that looks alike


class NotYours(Exception):
    """An id that does not exist and one that is not the caller's read the same (rule 1)."""


class Refused(Exception):
    """A reason the caller's assistant can act on."""


def open_store(path: str | Path = ":memory:") -> Store:
    return Store(path)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _new_id(store: Store, table: str, prefix: str, length: int = 8) -> str:
    while True:
        ref = f"{prefix}-" + "".join(secrets.choice(_ALPHABET) for _ in range(length))
        if not store.one(f"SELECT 1 FROM {table} WHERE id=?", ref):
            return ref


def _text(text: str | None, limit: int, what: str, empty_ok: bool = False) -> str:
    text = (text or "").strip()
    if not text and not empty_ok:
        raise Refused(f"{what} is empty")
    if len(text) > limit:
        raise Refused(f"{what} is {len(text)} characters; the limit is {limit}, and nothing is ever cut")
    return text


def _audit(store: Store, pid: str, link: str, what: str) -> None:
    """What was done, never what anyone wrote: this is kept as it is (rule 10)."""
    store.exec("INSERT INTO audit VALUES (?,?,?,?)", pid, store.now(), link, what)


# -- locks: what anyone writes is kept so that only their own connections open it (chats/vault.py) ---------------

def _self(pid: str) -> bytes:
    """The key a person's own name, rules, labels and notes are locked under: theirs, held only in their own call."""
    return vault.self_key(vault.key(pid))


def _mine(pid: str, blob: str, what: str) -> str:
    return vault.unlock_text(_self(pid), blob, what)


def _seal_to(store: Store, pid: str, data: bytes, purpose: str) -> str:
    row = store.one("SELECT public FROM people WHERE id=?", pid)
    return vault.seal(row["public"], data, purpose) if row and row["public"] else ""


def _chat_key(store: Store, pid: str, chat_id: str) -> bytes:
    """A chat's key, as this member holds it."""
    row = store.one("SELECT key FROM members WHERE chat_id=? AND person_id=?", chat_id, pid)
    if not row or not row["key"]:
        raise vault.Locked()
    return vault.opened(("chat", row["key"]), lambda: vault.open_sealed(vault.key(pid), row["key"], "chat"))


def _in_chat(store: Store, pid: str, chat_id: str, blob: str | None, what: str) -> str:
    """Something written in a chat, as this member reads it; empty where nothing they hold opens it."""
    try:
        return vault.unlock_text(_chat_key(store, pid, chat_id), blob or "", what)
    except vault.Locked:
        return ""


def _escrowed(store: Store, pid: str) -> bytes | None:
    row = store.one("SELECT private FROM escrow WHERE person_id=? AND until_t>?", pid, store.now())
    return vault.decode(row["private"]) if row else None


def _release(store: Store, pid: str) -> None:
    """Every connection and run link of theirs now holds its own copy: the open one goes."""
    if not (store.one("SELECT 1 FROM grants WHERE person_id=? AND revoked_t IS NULL AND person_key=''", pid)
            or store.one("SELECT 1 FROM run_links WHERE person_id=? AND ended_t IS NULL AND person_key=''", pid)):
        store.exec("DELETE FROM escrow WHERE person_id=?", pid)


def _start_over(store: Store, pid: str) -> bytes:
    """A new key for someone nothing they still hold opens: what was sealed to the old one is gone for good, for them
    too. Their chats stay theirs in name, and what was in them stays closed to them."""
    private, public = vault.new_pair()
    store.exec("UPDATE people SET public=?, name='', about='', about_prev='' WHERE id=?", public, pid)
    store.exec("UPDATE members SET key='', label='', notes='', flag='' WHERE person_id=?", pid)
    vault.hold(pid, private)
    return private


def _unlock(store: Store, pid: str, credential: bytes | None, locked: str, table: str, row: str) -> bool:
    """This call holds the person's key from now on, opened by the credential it came with: its own copy, or the one
    kept from before locks, of which it then takes a copy. Returns whether it holds it. A connection nothing opens
    starts them over only when no other connection or run link of theirs holds the key."""
    column = "id" if table == "grants" else "hash"
    try:
        private = vault.unlock(credential, locked, "person key") if credential and locked else None
    except vault.Locked:
        private = None
    if private is None:
        private = _escrowed(store, pid)
        if private is not None and credential:
            store.exec(f"UPDATE {table} SET person_key=? WHERE {column}=?", vault.lock(credential, private,
                                                                                      "person key"), row)
            _release(store, pid)
    if private is None:
        private = vault.key_or_none(pid)                # in hand already: a test, or the operator's own tools
    if private is None and table == "grants" and not (
            store.one("SELECT 1 FROM grants WHERE person_id=? AND revoked_t IS NULL AND person_key<>'' AND id<>?",
                      pid, row)
            or store.one("SELECT 1 FROM run_links WHERE person_id=? AND ended_t IS NULL AND person_key<>''", pid)):
        private = _start_over(store, pid)
        if credential:
            store.exec("UPDATE grants SET person_key=? WHERE id=?", vault.lock(credential, private, "person key"), row)
    if private is not None:
        vault.hold(pid, private)
    return private is not None


LOCKED = ("this connection cannot open your person's chats: they are locked under the keys their other connections "
          "hold. In the app where Bridge Chats still works for them, ask for a code for another app (chats_settings "
          "another_app) and use it here (chats_settings code); this connection then holds the key too.")


def report_key(store: Store) -> str:
    """The operator's public key for reports, made the first time it is needed. Its private half goes beside the
    database, never into it, so that a copy of the database opens no report either; the CLI reads them with it."""
    row = store.one("SELECT v FROM kv WHERE k='report_public'")
    if row:
        return row["v"]
    private, public = vault.new_pair()
    if store.path == ":memory:":
        store.report_private = private
    else:
        path = Path(store.path).with_name("report.key")
        path.touch(mode=0o600)
        path.write_text(vault.encode(private))
    store.exec("INSERT INTO kv(k, v) VALUES ('report_public', ?)", public)
    return public


def read_reports(store: Store, private: bytes) -> list[dict]:
    """What members reported, opened with the operator's private report key."""
    out = []
    for r in store.all("SELECT * FROM reports ORDER BY t"):
        try:
            sealed = json.loads(vault.open_sealed(private, r["copy"], "report"))
        except (vault.Locked, ValueError):
            sealed = {"why": "", "copy": []}
        out.append({"id": r["id"], "reporter": r["reporter_id"], "chat": r["chat_id"], "sender": r["sender_id"],
                    "t": r["t"], **sealed})
    return out


def _nudge(store: Store, pid: str) -> None:
    """Queued, and sent by `flush_nudges` after the change is committed; rate-limited per person (rule 13)."""
    store.pending.append(pid)


def flush_nudges(store: Store) -> None:
    queued, store.pending = list(dict.fromkeys(store.pending)), []
    for pid in queued:
        with store.transaction():
            p = store.one("SELECT topic, nudged_t FROM people WHERE id=? AND erased_t IS NULL", pid)
            if not p or not p["topic"] or (p["nudged_t"] and store.now() - p["nudged_t"] < NUDGE_EVERY):
                continue
            store.exec("UPDATE people SET nudged_t=? WHERE id=?", store.now(), pid)
        if store.on_nudge:
            store.on_nudge(p["topic"])


# -- people, connections and run links (PROTOCOL.md §5) ---------------------------------------------------------

def new_person(store: Store, name: str = "") -> str:
    """Someone new, with a key pair whose private half this call holds, and nothing stores but locked."""
    private, public = vault.new_pair()
    with store.transaction():
        pid = _new_id(store, "people", "p", 10)
        store.exec("INSERT INTO people(id, name, created_t, public) VALUES (?,?,?,?)", pid,
                   vault.lock_text(vault.self_key(private), _text(name, NAME_MAX, "the name", True), "name"),
                   store.now(), public)
    vault.hold(pid, private)
    return pid


def _alive(store: Store, pid: str) -> None:
    """Checked inside each writer's transaction: erasing is final, for a call already in flight too (rule 15)."""
    if not store.one("SELECT 1 FROM people WHERE id=? AND erased_t IS NULL", pid):
        raise NotYours()


def _day(store: Store) -> int:
    return int(store.now() // DAY)


def _today(store: Store, k: str) -> int:
    row = store.one("SELECT n FROM daily WHERE k=? AND day=?", k, _day(store))
    return row["n"] if row else 0


def _count(store: Store, k: str) -> None:
    store.exec("INSERT INTO daily(k, day, n) VALUES (?,?,1) ON CONFLICT(k, day) DO UPDATE SET n=n+1", k, _day(store))


def register_client(store: Store, client_id: str, info: str) -> None:
    """An app registering for sign-in (RFC 7591). Anyone may, so a day's registrations are capped: each is a row
    until the sweep takes the ones that never connected."""
    with store.transaction():
        if _today(store, "apps") >= APPS_PER_DAY:
            raise Refused("too many apps registered today; try again tomorrow")
        _count(store, "apps")
        store.exec("INSERT OR REPLACE INTO clients(id, info, created_t) VALUES (?,?,?)", client_id, info, store.now())


def client(store: Store, client_id: str) -> str | None:
    row = store.one("SELECT info FROM clients WHERE id=?", client_id)
    return row["info"] if row else None


def _token(store: Store, kind: str, *, grant_id: str = "", data: dict | None = None, token: str = "",
           expires_t: float | None = None) -> str:
    token = token or secrets.token_urlsafe(32)
    store.exec("INSERT INTO tokens(hash, kind, grant_id, data, created_t, expires_t) VALUES (?,?,?,?,?,?)",
               _hash(token), kind, grant_id, json.dumps(data or {}), store.now(), expires_t)
    return token


def _live_token(store: Store, token: str, kind: str):
    """The row, while it lasts and its connection has not ended."""
    return store.one("SELECT t.*, g.client_id FROM tokens t LEFT JOIN grants g ON g.id=t.grant_id WHERE t.hash=? AND "
                     "t.kind=? AND (t.expires_t IS NULL OR t.expires_t>?) AND (t.grant_id='' OR (g.id IS NOT NULL "
                     "AND g.revoked_t IS NULL))", _hash(token or ""), kind, store.now())


def sign_in_request(store: Store, data: dict) -> str:
    """A sign-in an app started, waiting on its person's Allow; `data` is what the authorization code will need."""
    with store.transaction():
        return _token(store, "request", data=data, expires_t=store.now() + REQUEST_S)


def sign_in_waiting(store: Store, request: str) -> dict | None:
    row = _live_token(store, request, "request")
    return json.loads(row["data"]) if row else None


def allow(store: Store, request: str, invite: str = "", name: str = "") -> tuple[dict, str] | None:
    """Allow pressed: a new connection, and an authorization code for its app. `invite` and `name` are what the
    invite page left in this browser; only an invite that still works is kept. It makes nobody: the connection's
    first tool call does."""
    with store.transaction():
        row = _live_token(store, request, "request")
        if not row:
            return None
        store.exec("DELETE FROM tokens WHERE hash=?", row["hash"])
        data = json.loads(row["data"])
        found = _usable_invite(store, token=invite) if invite else None
        grant, code = _new_id(store, "grants", "k", 16), secrets.token_urlsafe(32)
        store.exec("INSERT INTO grants(id, client_id, invite, created_t) VALUES (?,?,?,?)", grant,
                   data["client_id"], found["hash"] if found else "", store.now())
        kept = data
        if found:
            # What the first call needs from the invite, locked under the code until the app trades it for tokens.
            card = {"name": _text(name, NAME_MAX, "the name", True)[:NAME_MAX],
                    "maker": _maker_name(found, token=invite)}
            kept = {**data, "invite_key": vault.lock(vault.from_secret(code, "code"), json.dumps(card).encode(),
                                                     "invite key")}
        return data, _token(store, "code", grant_id=grant, data=kept, expires_t=store.now() + AUTH_CODE_S, token=code)


def deny(store: Store, request: str) -> dict | None:
    with store.transaction():
        row = _live_token(store, request, "request")
        if row:
            store.exec("DELETE FROM tokens WHERE hash=?", row["hash"])
        return json.loads(row["data"]) if row else None


def authorization_code(store: Store, code: str) -> dict | None:
    row = _live_token(store, code, "code")
    return {**json.loads(row["data"]), "grant": row["grant_id"], "expires_t": row["expires_t"]} if row else None


def _connection_key(refresh: str) -> bytes:
    """What a connection's keys are locked under: its renewal token, which the server keeps only as a hash."""
    return vault.from_secret(refresh, "connection")


def _access_token(store: Store, grant_id: str, scopes: list[str], connection: bytes) -> str:
    """Carries the connection's key locked under itself, so a call with it opens what the connection holds."""
    token = secrets.token_urlsafe(32)
    return _token(store, "access", grant_id=grant_id, token=token, expires_t=store.now() + ACCESS_S, data={
        "scopes": scopes, "key": vault.lock(vault.from_secret(token, "access"), connection, "connection")})


def exchange_code(store: Store, code: str) -> tuple[str, str] | None:
    """An authorization code for an access token and the renewal token that replaces it, once. What came with the
    code from the invite page is locked again under the connection."""
    with store.transaction():
        row = _live_token(store, code, "code")
        if not row:
            return None
        store.exec("DELETE FROM tokens WHERE hash=?", row["hash"])
        data = json.loads(row["data"])
        scopes, refresh = data.get("scopes") or [], secrets.token_urlsafe(32)
        connection = _connection_key(refresh)
        if data.get("invite_key"):
            card = vault.unlock(vault.from_secret(code, "code"), data["invite_key"], "invite key")
            store.exec("UPDATE grants SET invite_key=? WHERE id=?", vault.lock(connection, card, "invite key"),
                       row["grant_id"])
        return (_access_token(store, row["grant_id"], scopes, connection),
                _token(store, "refresh", grant_id=row["grant_id"], data={"scopes": scopes}, token=refresh))


def renewal(store: Store, token: str) -> dict | None:
    row = _live_token(store, token, "refresh")
    return {"grant": row["grant_id"], "client_id": row["client_id"], **json.loads(row["data"])} if row else None


def renew(store: Store, token: str) -> tuple[str, str] | None:
    """A fresh access token, and the same renewal token back. It is not replaced: an app that renews twice at once,
    or loses the answer, would then hold a dead one and sign in again, which makes someone new. Ending the
    connection ends it."""
    with store.transaction():
        row = _live_token(store, token, "refresh")
        if not row:
            return None
        connection = _connection_key(token)
        # A connection from before locks takes its copy of its person's key at its first renewal.
        g = store.one("SELECT person_id, person_key FROM grants WHERE id=?", row["grant_id"])
        if g and g["person_id"] and not g["person_key"]:
            private = _escrowed(store, g["person_id"])
            if private is not None:
                store.exec("UPDATE grants SET person_key=? WHERE id=?", vault.lock(connection, private, "person key"),
                           row["grant_id"])
                _release(store, g["person_id"])
        return _access_token(store, row["grant_id"], json.loads(row["data"]).get("scopes") or [], connection), token


def access(store: Store, token: str) -> dict | None:
    """The connection behind an access token."""
    row = _live_token(store, token, "access")
    if not row:
        return None
    return {"grant": row["grant_id"], "client_id": row["client_id"], "expires_t": row["expires_t"],
            **json.loads(row["data"])}


def revoke(store: Store, token: str) -> None:
    """An app giving back a token ends its connection."""
    with store.transaction():
        row = store.one("SELECT grant_id FROM tokens WHERE hash=?", _hash(token or ""))
        if row and row["grant_id"]:
            _end_grants(store, "id=?", row["grant_id"])


def _end_grants(store: Store, where: str, *args) -> None:
    ids = [r["id"] for r in store.all(f"SELECT id FROM grants WHERE revoked_t IS NULL AND {where}", *args)]
    for grant in ids:
        store.exec("UPDATE grants SET revoked_t=? WHERE id=?", store.now(), grant)
        store.exec("DELETE FROM tokens WHERE grant_id=?", grant)


_ERASED = ("your person erased everything about themselves here, so this connection no longer works. To start over "
           "as someone new, remove Bridge Chats from the AI's settings and add it again")


def _connection_from(store: Store, token: str) -> bytes | None:
    """The connection key an access token carries; none for one from before locks."""
    row = _live_token(store, token, "access") if token else None
    locked = json.loads(row["data"]).get("key") if row else None
    return vault.unlock(vault.from_secret(token, "access"), locked, "connection") if locked else None


def person_for_grant(store: Store, grant_id: str, token: str = "") -> str:
    """Whose connection this is. Its first call makes the person, under the name the invite page was given, and
    uses the invite the connection was allowed with if it still works: the contact is made then (rule 2). Without
    one, the person reaches nobody until they make or use an invite (rule 1)."""
    with store.transaction():
        # LEFT JOIN, so a connection whose person row is gone refuses rather than resolving to nobody.
        g = store.one("SELECT g.*, p.id pid, p.erased_t FROM grants g LEFT JOIN people p ON p.id=g.person_id "
                      "WHERE g.id=? AND g.revoked_t IS NULL", grant_id)
        if not g:
            raise NotYours()
        connection = _connection_from(store, token)
        if g["person_id"]:
            if g["pid"] is None or g["erased_t"] is not None:
                raise Refused(_ERASED)
            _unlock(store, g["person_id"], connection, g["person_key"], "grants", grant_id)
            return g["person_id"]
        card = json.loads(vault.unlock(connection, g["invite_key"], "invite key")) \
            if connection and g["invite_key"] else {}
        inv = _usable_invite(store, hashed=g["invite"]) if card else None
        kind, cap = ("invited", INVITED_PER_DAY) if inv else ("uninvited", UNINVITED_PER_DAY)
        if _today(store, kind) >= cap:
            raise Refused("this server is taking no more new people today. Try again tomorrow")
        _count(store, kind)
        pid = new_person(store, card.get("name", ""))
        store.exec("UPDATE grants SET person_id=?, person_key=? WHERE id=?", pid,
                   vault.lock(connection, vault.key(pid), "person key") if connection else "", grant_id)
        if not connection:
            # A token from before locks, for a connection that had not called: its key waits, as everyone's from
            # then did, for the connection's next renewal to take a copy.
            store.exec("INSERT INTO escrow VALUES (?,?,?)", pid, vault.encode(vault.key(pid)), store.now() + ESCROW_S)
        if inv:
            _use(store, inv, pid, card.get("maker", ""))
        return pid


def _run_link(store: Store, pid: str) -> str:
    """A routine's link, holding its person's key locked under its secret, as a connection does."""
    secret = secrets.token_urlsafe(24)
    store.exec("INSERT INTO run_links VALUES (?,?,?,NULL,?)", _hash(secret), pid, store.now(),
               vault.lock(vault.from_secret(secret, "run link"), vault.key(pid), "person key"))
    return secret


def person_for_run_link(store: Store, secret: str) -> str | None:
    """Whose run link this is, with their key held for the call; none for a link that opens nothing, which is told
    to make a new one rather than starting anyone over."""
    with store.transaction():
        row = store.one("SELECT l.person_id, l.hash, l.person_key FROM run_links l JOIN people p ON p.id=l.person_id "
                        "WHERE l.hash=? AND l.ended_t IS NULL AND p.erased_t IS NULL", _hash(secret or ""))
        if not row:
            return None
        held = _unlock(store, row["person_id"], vault.from_secret(secret, "run link"), row["person_key"], "run_links",
                       row["hash"])
    return row["person_id"] if held else None


def _once(store: Store, pid: str) -> str:
    """A page that shows a new run link once. The link is made now; the page holds it until a POST reveals it."""
    token = secrets.token_urlsafe(18)
    store.exec("INSERT INTO once VALUES (?,?,?,?,NULL)", _hash(token), pid,
               vault.lock_text(vault.from_secret(token, "once"), _run_link(store, pid), "run link"), store.now())
    return token


def _once_row(store: Store, token: str):
    return store.one("SELECT o.* FROM once o JOIN people p ON p.id=o.person_id WHERE o.hash=? AND o.used_t IS NULL AND "
                     "o.created_t > ? AND p.erased_t IS NULL", _hash(token or ""), store.now() - DAY)


def once_waiting(store: Store, token: str) -> bool:
    """Whether a one-time page still holds its link: read only, so a preview of it uses nothing up."""
    return bool(_once_row(store, token))


def reveal_once(store: Store, token: str) -> str | None:
    """The run link's secret, once, within a day; a page already opened, old, or of an erased person shows nothing."""
    with store.transaction():
        row = _once_row(store, token)
        if not row:
            return None
        store.exec("UPDATE once SET used_t=?, secret='' WHERE hash=?", store.now(), row["hash"])
        return vault.unlock_text(vault.from_secret(token, "once"), row["secret"], "run link")


def holds_nothing(store: Store, pid: str) -> bool:
    """A brand-new account, as far as anyone could lose anything by giving it up: no name, rules or nudges, no chat,
    no invite of its own."""
    me = store.one("SELECT name, about, topic FROM people WHERE id=?", pid)
    return not (me["name"] or me["about"] or me["topic"]
                or store.one("SELECT 1 FROM members WHERE person_id=? LIMIT 1", pid)
                or store.one("SELECT 1 FROM invites WHERE maker_id=? LIMIT 1", pid))


def _code_hash(code: str) -> str:
    return _hash("".join(ch for ch in (code or "").lower() if ch in _ALPHABET))


def link_code(store: Store, pid: str, *, replace: bool) -> str:
    """A code for the chat of another app, where it makes that app's connection this person's. With `replace`, its
    use ends every other connection and run link of theirs. A new code ends any earlier one not yet used."""
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(LINK_CODE))
    # Their key goes with the code, locked under it; the operator's code, made with no key in hand, carries none,
    # and whoever uses it starts over (`_start_over`).
    private = vault.key_or_none(pid)
    with store.transaction():
        _alive(store, pid)
        store.exec("DELETE FROM link_codes WHERE person_id=? AND used_t IS NULL", pid)
        store.exec("INSERT INTO link_codes(hash, person_id, replace, created_t, person_key) VALUES (?,?,?,?,?)",
                   _code_hash(raw), pid, int(replace), store.now(),
                   vault.lock(vault.from_code(raw, "link code"), private, "person key") if private else "")
        _audit(store, pid, "app", "made a code for " + ("a new connection, ending the others" if replace else
                                                         "another app"))
    return "-".join(raw[i:i + 4] for i in range(0, LINK_CODE, 4))


ALREADY_LINKED, LINKED, MOVED = "already", "linked", "moved"


def use_link_code(store: Store, pid: str, grant_id: str, code: str, token: str = "") -> tuple[str, str]:
    """This connection becomes the code's person, and the empty account its first call made is erased. Only while
    that account holds nothing. Returns what happened, and whose the connection now is."""
    with store.transaction():
        _alive(store, pid)
        row = store.one("SELECT l.*, p.erased_t FROM link_codes l JOIN people p ON p.id=l.person_id WHERE l.hash=? "
                        "AND l.used_t IS NULL AND l.created_t>?", _code_hash(code), store.now() - LINK_CODE_S)
        if not row or row["erased_t"] is not None:
            raise Refused("that code does not work: it may be mistyped, used already, more than an hour old, or "
                          "replaced by a newer one. Ask for a fresh one in the app where Bridge Chats already works")
        target = row["person_id"]
        typed = "".join(ch for ch in (code or "").lower() if ch in _ALPHABET)
        theirs = vault.unlock(vault.from_code(typed, "link code"), row["person_key"], "person key") \
            if row["person_key"] else None
        connection = _connection_from(store, token)
        if target == pid:
            # Their own code, in a connection of theirs that could not open their key: now it holds it too.
            if theirs is not None and connection:
                store.exec("UPDATE grants SET person_key=? WHERE id=?", vault.lock(connection, theirs, "person key"),
                           grant_id)
                vault.hold(target, theirs)
            return ALREADY_LINKED, target
        if not holds_nothing(store, pid):
            raise Refused("this connection's account already holds things of its own (a name, rules, a chat or an "
                          "invite), which a code would lose. Keep the two apart, or, on your person's say-so, erase "
                          "this one first and then use the code")
        store.exec("UPDATE link_codes SET used_t=? WHERE hash=?", store.now(), row["hash"])
        if theirs is None:
            theirs = _start_over(store, target)
        vault.hold(target, theirs)
        store.exec("UPDATE grants SET person_id=?, person_key=? WHERE id=?", target,
                   vault.lock(connection, theirs, "person key") if connection else "", grant_id)
        store.exec("UPDATE people SET erased_t=? WHERE id=?", store.now(), pid)
        store.exec("DELETE FROM audit WHERE person_id=?", pid)
        if row["replace"]:
            _end_grants(store, "person_id=? AND id<>?", target, grant_id)
            store.exec("UPDATE run_links SET ended_t=? WHERE person_id=? AND ended_t IS NULL", store.now(), target)
        _audit(store, target, "app", "used a code: another app is now this account" +
               (", and every other connection ended" if row["replace"] else ""))
        _nudge(store, target)
        return (MOVED if row["replace"] else LINKED), target


def sweep(store: Store) -> None:
    """Delete what is spent: expired sign-in steps, connections whose app never called within a day (they made
    nobody), apps that never connected, old codes, counts and one-time pages, and the audit and reports past what is
    shown. Cheap, and done at most hourly."""
    now = store.now()
    if now - getattr(store, "swept_t", 0) < 3600:
        return
    store.swept_t = now
    with store.transaction():
        store.exec("DELETE FROM tokens WHERE expires_t < ?", now)
        store.exec("DELETE FROM tokens WHERE grant_id IN (SELECT id FROM grants WHERE revoked_t IS NOT NULL OR "
                   "(person_id='' AND created_t < ?))", now - DAY)
        store.exec("DELETE FROM grants WHERE revoked_t < ? OR (person_id='' AND created_t < ?)", now - DAY, now - DAY)
        store.exec("DELETE FROM clients WHERE created_t < ? AND id NOT IN (SELECT client_id FROM grants)", now - DAY)
        store.exec("DELETE FROM link_codes WHERE created_t < ?", now - LINK_CODE_S)
        store.exec("DELETE FROM daily WHERE day < ?", _day(store) - 1)
        store.exec("DELETE FROM once WHERE created_t < ?", now - DAY)
        store.exec("DELETE FROM audit WHERE t < ?", now - AUDIT_DAYS * DAY)
        store.exec("DELETE FROM reports WHERE t < ?", now - REPORT_DAYS * DAY)
        store.exec("DELETE FROM escrow WHERE until_t < ?", now)


# -- invites and contacts (rule 2) -------------------------------------------------------------------------------

def invite(store: Store, pid: str, names: list[str], chat_id: str | None = None) -> list[dict]:
    """One single-use invite per name, each carrying its maker's note. With `chat_id`, whoever uses one is also
    invited to that group once the maker confirms who they are."""
    notes = [_text(n, NAME_MAX, "a name to invite") for n in names]
    if not notes:
        raise Refused("say whom each invite is for: `for` takes one name or several")
    with store.transaction():
        _alive(store, pid)
        if chat_id:
            _group_member(store, pid, chat_id)
        made = store.one("SELECT COUNT(*) n FROM invites WHERE maker_id=? AND created_t > ?", pid,
                         store.now() - DAY)["n"]
        if made + len(notes) > INVITES_PER_DAY:
            raise Refused(f"that is over {INVITES_PER_DAY} invites in a day; the rest can wait until tomorrow")
        out = []
        # The maker's name goes with each invite, locked under its token and its code, for whoever uses it; their
        # note is theirs alone.
        maker = _mine(pid, store.one("SELECT name FROM people WHERE id=?", pid)["name"], "name").encode()
        for note in notes:
            token = secrets.token_urlsafe(16)
            code = "".join(secrets.choice(_ALPHABET) for _ in range(10))
            store.exec("INSERT INTO invites(id, hash, code, maker_id, note, chat_id, created_t, expires_t, maker_t, "
                       "maker_c) VALUES (?,?,?,?,?,?,?,?,?,?)", _new_id(store, "invites", "i"), _hash(token),
                       _code_hash(code), pid, vault.lock_text(_self(pid), note, "label"), chat_id, store.now(),
                       store.now() + INVITE_DAYS * DAY, vault.lock(vault.from_secret(token, "invite"), maker, "maker"),
                       vault.lock(vault.from_code(code, "invite"), maker, "maker"))
            out.append({"for": note, "token": token, "code": code})
        _audit(store, pid, "app", f"made {len(notes)} invite(s)")
    return out


def _usable_invite(store: Store, token: str = "", code: str = "", hashed: str = ""):
    """By its URL's token, its code, or the token's hash a connection kept; none of the three empty matches."""
    hashed = hashed or (_hash(token) if token else "")
    if not hashed and not code:
        return None
    return store.one("SELECT i.* FROM invites i JOIN people p ON p.id=i.maker_id WHERE (i.hash=? OR i.code=?) "
                     "AND i.used_t IS NULL AND i.expires_t > ? AND p.erased_t IS NULL", hashed,
                     _code_hash(code) if code else "", store.now())


def _maker_name(inv, *, token: str = "", code: str = "") -> str:
    """The name an invite's maker goes by, opened with the token or code it was used with."""
    try:
        if token and inv["maker_t"]:
            return vault.unlock_text(vault.from_secret(token, "invite"), inv["maker_t"], "maker")
        if code and inv["maker_c"]:
            return vault.unlock_text(vault.from_code(code, "invite"), inv["maker_c"], "maker")
    except vault.Locked:
        pass
    return ""


def invite_page(store: Store, token: str) -> bool:
    """Whether the invite behind a page can still be used. Its holder learns nothing about its maker (rule 2)."""
    return bool(_usable_invite(store, token=token))


def connect(store: Store, pid: str, link_or_code: str) -> dict:
    """Someone already on this server uses an invite from their chat. Person-only (rule 9)."""
    text = (link_or_code or "").strip()
    match = re.search(r"/i/([A-Za-z0-9_-]{16,})", text)
    token, code = (match.group(1), "") if match else ("", re.sub(r"[^a-z0-9]", "", text.lower()))
    # Counted in a transaction of its own: inside the one below, a refused try rolled its own count back, and
    # guessing codes was never capped (test).
    with store.transaction():
        _alive(store, pid)
        tries = store.one("SELECT COUNT(*) n FROM audit WHERE person_id=? AND what LIKE 'tried an invite%' AND t > ?",
                          pid, store.now() - DAY)["n"]
        if tries >= CONNECTS_PER_DAY:
            raise Refused(f"that is {CONNECTS_PER_DAY} invites tried today; try again tomorrow")
        _audit(store, pid, "app", "tried an invite")
    with store.transaction():
        _alive(store, pid)
        inv = _usable_invite(store, token=token, code=code) if (token or len(code) == 10) else None
        if not inv:
            raise Refused("already used or doesn't work: tell whoever sent it")
        if inv["maker_id"] == pid:
            raise Refused("that invite is your person's own: they send it to the person it is for")
        maker = _maker_name(inv, token=token, code=code)
        chat = _use(store, inv, pid, maker)
        return {"chat": chat, "calls_themselves": maker, "group_invite_held": bool(inv["chat_id"])}


def _use(store: Store, inv, pid: str, maker_name: str) -> str:
    """The contact, one DM per pair: connecting twice returns the one there is. The maker's side stays
    unconfirmed, labelled with their own note, until they say it is who they meant: whoever used the link may not
    be the one it was for (challenge round). The DM's key is made here and sealed to both, and each one's name goes
    in it, locked, for the other: the maker's as the invite carried it."""
    store.exec("UPDATE invites SET used_t=?, used_by=? WHERE id=?", store.now(), pid, inv["id"])
    maker = inv["maker_id"]
    pair = "|".join(sorted((maker, pid)))
    row = store.one("SELECT id FROM chats WHERE pair=?", pair)
    if row:
        chat = row["id"]
    else:
        chat, key = _new_id(store, "chats", "c"), vault.new_key()
        mine = _mine(pid, store.one("SELECT name FROM people WHERE id=?", pid)["name"], "name")
        store.exec("INSERT INTO chats(id, kind, pair, created_t) VALUES (?,?,?,?)", chat, "dm", pair, store.now())
        # The maker's label is their own note, locked already under their own key: carried over as it is.
        store.exec("INSERT INTO members(chat_id, person_id, state, number, label, confirmed, t, key, shown) VALUES "
                   "(?,?,'joined',1,?,0,?,?,?)", chat, maker, inv["note"], store.now(),
                   _seal_to(store, maker, key, "chat"), vault.lock_text(key, maker_name, "name"))
        store.exec("INSERT INTO members(chat_id, person_id, state, number, label, confirmed, t, key, shown) VALUES "
                   "(?,?,'joined',2,?,1,?,?,?)", chat, pid,
                   vault.lock_text(_self(pid), maker_name[:NAME_MAX] or "whoever sent the invite", "label"),
                   store.now(), _seal_to(store, pid, key, "chat"), vault.lock_text(key, mine, "name"))
    if inv["chat_id"]:
        _add_member(store, inv["chat_id"], pid, maker, "held")
    _nudge(store, maker)
    return chat


def confirm(store: Store, pid: str, contact: str, label: str = "") -> str:
    """The maker says who used their invite: it then shows under `label` (or their note), and any group it
    invited them to is offered to them."""
    with store.transaction():
        _alive(store, pid)
        chat, other = _contact(store, pid, contact)
        label = _text(label, NAME_MAX, "the label", True) or _mine(pid, chat["label"], "label")
        store.exec("UPDATE members SET label=?, confirmed=1 WHERE chat_id=? AND person_id=?",
                   vault.lock_text(_self(pid), label, "label"), chat["id"], pid)
        held = store.all("SELECT chat_id FROM members WHERE person_id=? AND state='held' AND invited_by=?", other, pid)
        for h in held:
            # The group's key comes from the maker, who holds it, now that they have said who this is.
            store.exec("UPDATE members SET state='invited', t=?, key=? WHERE chat_id=? AND person_id=?", store.now(),
                       _seal_to(store, other, _chat_key(store, pid, h["chat_id"]), "chat"), h["chat_id"], other)
        if held:
            _nudge(store, other)
        _audit(store, pid, "app", "labelled a contact")
        return label


def _contact(store: Store, pid: str, ref: str):
    """A DM of the caller's, by its id or by the caller's label for the other person. Returns (my row, other id)."""
    ref = (ref or "").strip()
    # Labels are locked, so they are opened here and compared: one person's own DMs, never anyone else's (rule 14).
    rows = [r for r in store.all("SELECT m.*, c.id FROM members m JOIN chats c ON c.id=m.chat_id WHERE c.kind='dm' "
                                 "AND m.person_id=?", pid)
            if r["id"] == ref or _mine(pid, r["label"], "label").lower() == ref.lower()]
    if not rows:
        raise NotYours()
    if len(rows) > 1:
        raise Refused(f"{len(rows)} contacts go by that label: use the chat id from chats_inbox view='contacts'")
    other = store.one("SELECT person_id FROM members WHERE chat_id=? AND person_id<>?", rows[0]["id"], pid)
    return rows[0], other["person_id"]


# -- chats (rule 3) ----------------------------------------------------------------------------------------------

def _member(store: Store, pid: str, chat_id: str, states=("joined",)):
    row = store.one("SELECT m.*, c.kind, c.name, c.owner_id, c.seq FROM members m JOIN chats c ON c.id=m.chat_id "
                    f"WHERE m.chat_id=? AND m.person_id=? AND m.state IN ({','.join('?' * len(states))})",
                    (chat_id or "").strip(), pid, *states)
    if not row:
        raise NotYours()
    return row


def _group_member(store: Store, pid: str, chat_id: str):
    row = _member(store, pid, chat_id)
    if row["kind"] != "group":
        raise Refused("that is a DM: only a group takes more people")
    return row


def _add_member(store: Store, chat_id: str, pid: str, by: str, state: str, key: bytes | None = None) -> None:
    """`key`: the chat's, sealed to them now, if they may read it yet; one held until its maker confirms them is
    given it then."""
    row = store.one("SELECT state FROM members WHERE chat_id=? AND person_id=?", chat_id, pid)
    if row and row["state"] in ("joined", "invited", "held"):
        return
    count = store.one("SELECT COUNT(*) n FROM members WHERE chat_id=? AND state IN ('joined','invited','held')",
                      chat_id)["n"]
    if count >= GROUP_MAX:
        raise Refused(f"a group holds {GROUP_MAX} people, invitations included")
    number = store.one("SELECT COALESCE(MAX(number), 0) + 1 n FROM members WHERE chat_id=?", chat_id)["n"]
    store.exec("INSERT OR REPLACE INTO members(chat_id, person_id, state, invited_by, number, t, key) VALUES "
               "(?,?,?,?,?,?,?)", chat_id, pid, state, by, number, store.now(),
               _seal_to(store, pid, key, "chat") if key else "")
    if state == "invited":
        _nudge(store, pid)


def start_group(store: Store, pid: str, name: str, contacts: list[str]) -> tuple[str, list[str]]:
    name = _text(name, NAME_MAX, "the group's name")
    with store.transaction():
        _alive(store, pid)
        others = [_contact(store, pid, c) for c in contacts]
        chat, key = _new_id(store, "chats", "c"), vault.new_key()
        mine = _mine(pid, store.one("SELECT name FROM people WHERE id=?", pid)["name"], "name")
        store.exec("INSERT INTO chats(id, kind, name, owner_id, created_t) VALUES (?,?,?,?,?)", chat, "group",
                   vault.lock_text(key, name, "group name"), pid, store.now())
        store.exec("INSERT INTO members(chat_id, person_id, state, number, t, key, shown) VALUES "
                   "(?,?,'joined',1,?,?,?)", chat, pid, store.now(), _seal_to(store, pid, key, "chat"),
                   vault.lock_text(key, mine, "name"))
        for _, other in others:
            _add_member(store, chat, other, pid, "invited", key)
        return chat, [_mine(pid, row["label"], "label") for row, _ in others]


def invite_to(store: Store, pid: str, chat_id: str, contacts: list[str]) -> list[str]:
    """Any member invites their own contacts, and nobody else (rule 3)."""
    with store.transaction():
        _alive(store, pid)
        _group_member(store, pid, chat_id)
        others = [_contact(store, pid, c) for c in contacts]
        key = _chat_key(store, pid, chat_id)
        for _, other in others:
            _add_member(store, chat_id, other, pid, "invited", key)
        _audit(store, pid, "app", f"invited {len(others)} to a group")
        return [_mine(pid, row["label"], "label") for row, _ in others]


def accept(store: Store, pid: str, chat_id: str) -> None:
    """They see messages from their joining on: an ex invited by a friend saw plans pinned before (challenge)."""
    with store.transaction():
        _alive(store, pid)
        row = _member(store, pid, chat_id, ("invited",))
        mine = _mine(pid, store.one("SELECT name FROM people WHERE id=?", pid)["name"], "name")
        store.exec("UPDATE members SET state='joined', from_seq=?, read_seq=?, run_seq=?, t=?, shown=? WHERE "
                   "chat_id=? AND person_id=?", row["seq"], row["seq"], row["seq"], store.now(),
                   vault.lock_text(_chat_key(store, pid, chat_id), mine, "name"), chat_id, pid)
        _audit(store, pid, "app", "joined a group")


def decline(store: Store, pid: str, chat_id: str) -> None:
    """Silent: to the others a decline looks like an invitation never answered (rule 11)."""
    with store.transaction():
        _alive(store, pid)
        _member(store, pid, chat_id, ("invited",))
        store.exec("UPDATE members SET state='declined', key='' WHERE chat_id=? AND person_id=?", chat_id, pid)


def leave(store: Store, pid: str, chat_id: str) -> None:
    with store.transaction():
        _alive(store, pid)
        row = _group_member(store, pid, chat_id)
        store.exec("UPDATE members SET state='left', t=?, key='' WHERE chat_id=? AND person_id=?", store.now(), chat_id,
                   pid)
        if row["owner_id"] == pid:
            heir = store.one("SELECT person_id FROM members WHERE chat_id=? AND state='joined' ORDER BY t, number "
                             "LIMIT 1", chat_id)
            store.exec("UPDATE chats SET owner_id=? WHERE id=?", heir["person_id"] if heir else None, chat_id)
        _audit(store, pid, "app", "left a group")


def remove(store: Store, pid: str, chat_id: str, number: int) -> None:
    with store.transaction():
        _alive(store, pid)
        row = _group_member(store, pid, chat_id)
        if row["owner_id"] != pid:
            raise Refused("only the group's owner removes people")
        target = store.one("SELECT person_id FROM members WHERE chat_id=? AND number=? AND state IN "
                           "('joined','invited','held')", chat_id, int(number))
        if not target or target["person_id"] == pid:
            raise NotYours()
        store.exec("UPDATE members SET state='left', t=?, key='' WHERE chat_id=? AND person_id=?", store.now(),
                   chat_id, target["person_id"])
        _audit(store, pid, "app", "removed someone from a group")


# -- blocks and reports (rule 12) --------------------------------------------------------------------------------

def _blocked_by(store: Store, pid: str) -> set[str]:
    return {r["blocked_id"] for r in store.all("SELECT blocked_id FROM blocks WHERE blocker_id=?", pid)}


def _dm_others(store: Store, chat_id: str, pid: str) -> list[str]:
    return [r["person_id"] for r in store.all("SELECT person_id FROM members WHERE chat_id=? AND person_id<>?",
                                              chat_id, pid)]


def _person_in(store: Store, pid: str, chat_id: str, contact: str = "", number: int | None = None) -> str:
    if contact:
        return _contact(store, pid, contact)[1]
    _member(store, pid, chat_id)
    row = store.one("SELECT person_id FROM members WHERE chat_id=? AND number=?", chat_id, int(number or 0))
    if not row or row["person_id"] == pid:
        raise NotYours()
    return row["person_id"]


def block(store: Store, pid: str, contact: str = "", chat_id: str = "", number: int | None = None) -> None:
    with store.transaction():
        _alive(store, pid)
        other = _person_in(store, pid, chat_id, contact, number)
        store.exec("INSERT OR IGNORE INTO blocks VALUES (?,?,?)", pid, other, store.now())
        _audit(store, pid, "app", "blocked someone")


def unblock(store: Store, pid: str, contact: str) -> None:
    with store.transaction():
        _alive(store, pid)
        other = _contact(store, pid, contact)[1]
        store.exec("DELETE FROM blocks WHERE blocker_id=? AND blocked_id=?", pid, other)
        _audit(store, pid, "app", "unblocked someone")


def report(store: Store, pid: str, chat_id: str, number: int, why: str) -> None:
    """A copy of what that member sent this person in that chat, for the operator (the CLI). Nobody else sees it."""
    why = _text(why, WHY_MAX, "the reason")
    with store.transaction():
        _alive(store, pid)
        me = _member(store, pid, chat_id)
        other = _person_in(store, pid, chat_id, number=number)
        key = _chat_key(store, pid, chat_id)
        sent = [{**dict(r), "text": vault.unlock_text(key, r["text"], "message")} for r in store.all(
            "SELECT seq, text, voice, t FROM messages WHERE chat_id=? AND sender_id=? AND seq>? ORDER BY seq DESC "
            "LIMIT 50", chat_id, other, me["from_seq"])]
        # Sealed to the operator, whose report key's private half is kept beside the database, not in it.
        store.exec("INSERT INTO reports VALUES (?,?,?,?,?,?,?)", _new_id(store, "reports", "r"), pid, chat_id, other,
                   "", vault.seal(report_key(store), json.dumps({"why": why, "copy": sent}).encode(), "report"),
                   store.now())
        _audit(store, pid, "app", "reported someone")


# -- messages (rules 4 to 8) ------------------------------------------------------------------------------------

def _visible(store: Store, pid: str, chat_id: str, after: int, until: int | None = None, limit: int = PAGE):
    """Messages this person may see: from their joining on, and none from anyone they blocked."""
    me = _member(store, pid, chat_id)
    blocked = _blocked_by(store, pid)
    rows = store.all("SELECT * FROM messages WHERE chat_id=? AND seq>? AND seq>? AND seq<=? ORDER BY seq LIMIT ?",
                     chat_id, after, me["from_seq"], until if until is not None else 1 << 62, limit * 4)
    return [r for r in rows if r["sender_id"] not in blocked][:limit]


def _append(store: Store, pid: str, chat_id: str, voice: str, kind: str, text: str, **extra) -> int:
    seq = store.one("SELECT seq FROM chats WHERE id=?", chat_id)["seq"] + 1
    key = _chat_key(store, pid, chat_id)
    store.exec("UPDATE chats SET seq=? WHERE id=?", seq, chat_id)
    store.exec("INSERT INTO messages(chat_id, seq, sender_id, voice, kind, text, options, multi, due_t, for_numbers, "
               "mention, t, mark) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", chat_id, seq, pid, voice, kind,
               vault.lock_text(key, text, "message"),
               vault.lock_text(key, json.dumps(extra["options"]), "options") if extra.get("options") is not None
               else None,
               int(bool(extra.get("multi"))), extra.get("due_t"),
               json.dumps(extra.get("for_numbers")) if extra.get("for_numbers") else None,
               json.dumps(extra.get("mention")) if extra.get("mention") else None, store.now(),
               vault.mark(key, f"{voice} {text}"))
    column = "read_seq" if voice == "person" else "run_seq"
    store.exec(f"UPDATE members SET {column}=MAX({column}, ?) WHERE chat_id=? AND person_id=?", seq, chat_id, pid)
    return seq


def _addressed(ask, number: int) -> bool:
    numbers = json.loads(ask["for_numbers"]) if ask["for_numbers"] else None
    return numbers is None or number in numbers


def _ask(store: Store, chat_id: str, seq: int):
    ask = store.one("SELECT * FROM messages WHERE chat_id=? AND seq=? AND kind='ask'", chat_id, int(seq))
    if not ask:
        raise NotYours()
    return ask


def _answer(store: Store, pid: str, me, answer: dict, voice: str) -> int:
    ask = _ask(store, me["chat_id"], answer.get("ask", 0))
    if ask["seq"] <= me["from_seq"] or not _addressed(ask, me["number"]):
        raise NotYours()
    if ask["closed_t"]:
        raise Refused("that ask is closed: its outcome is pinned in the chat")
    options = json.loads(_in_chat(store, pid, ask["chat_id"], ask["options"], "options") or "[]")
    choice = sorted({int(c) for c in answer.get("choice", [])})
    if any(c < 0 or c >= len(options) for c in choice) or (len(choice) > 1 and not ask["multi"]):
        raise Refused(f"choose {'any' if ask['multi'] else 'one'} of options 0 to {len(options) - 1}, or [] for none")
    old = store.one("SELECT voice FROM answers WHERE chat_id=? AND ask_seq=? AND person_id=?", ask["chat_id"],
                    ask["seq"], pid)
    if voice == "assistant" and old and old["voice"] == "person":
        raise Refused("your person already answered this ask themselves; only they change it")
    store.exec("INSERT OR REPLACE INTO answers VALUES (?,?,?,?,?,?)", ask["chat_id"], ask["seq"], pid,
               json.dumps(choice), voice, store.now())
    return ask["seq"]


def send(store: Store, pid: str, chat_id: str, text: str = "", *, mention: list[int] | None = None,
         ask: dict | None = None, answer: dict | None = None, close: dict | None = None) -> dict:
    """Person voice: words the person wrote or approved in this turn (rule 4). Only through a full link."""
    text = _text(text, SEND_MAX, "the message", empty_ok=bool(answer or close))
    with store.transaction():
        _alive(store, pid)
        me = _member(store, pid, chat_id)
        out: dict = {}
        if answer:
            out["answered"] = _answer(store, pid, me, answer, "person")
        if close:
            asked = _ask(store, chat_id, close.get("ask", 0))
            if asked["sender_id"] != pid:
                raise Refused("only whoever made an ask closes it")
            outcome = _text(close.get("outcome"), OUTCOME_MAX, "the outcome")
            store.exec("UPDATE messages SET outcome=?, closed_t=? WHERE chat_id=? AND seq=?",
                       vault.lock_text(_chat_key(store, pid, chat_id), outcome, "outcome"), store.now(), chat_id,
                       asked["seq"])
            out["closed"] = asked["seq"]
            _nudge_members(store, pid, chat_id)
        if text:
            dup = store.one("SELECT seq FROM messages WHERE chat_id=? AND sender_id=? AND voice='person' AND mark=? "
                            "AND t > ? ORDER BY seq DESC LIMIT 1", chat_id, pid,
                            vault.mark(_chat_key(store, pid, chat_id), f"person {text}"),
                            store.now() - DUPLICATE_WINDOW)
            if dup and not ask:
                return {**out, "sent": dup["seq"]}
            extra: dict = {"mention": [int(n) for n in mention or []]}
            if ask:
                options = [_text(o, OPTION_MAX, "an option") for o in ask.get("options", [])]
                if not 2 <= len(options) <= OPTIONS_MAX:
                    raise Refused(f"an ask takes 2 to {OPTIONS_MAX} options")
                extra.update(options=options, multi=ask.get("multi"), due_t=ask.get("due"),
                             for_numbers=[int(n) for n in ask.get("for") or []] or None)
            out["sent"] = _append(store, pid, chat_id, "person", "ask" if ask else "text", text, **extra)
            _nudge_members(store, pid, chat_id)
        _audit(store, pid, "app", "sent a message in your own words")
        return out


def _nudge_members(store: Store, pid: str, chat_id: str) -> None:
    """A person-voice message needs its readers; nobody who blocked its sender hears of it (rules 12 and 13)."""
    for r in store.all("SELECT person_id FROM members WHERE chat_id=? AND state='joined' AND person_id<>?", chat_id,
                       pid):
        if pid not in _blocked_by(store, r["person_id"]):
            _nudge(store, r["person_id"])


def reply(store: Store, pid: str, link: str, chat_id: str, upto: int, text: str = "", *,
          mention: list[int] | None = None, answer: dict | None = None, note: str | None = None,
          flag: str | None = None) -> dict:
    """Assistant voice, labelled as the assistant's and never a commitment. The one writing operation a run link
    may use: text only where answering is on and it was spoken to, answers only with answer_asks on (rules 5 and 6)."""
    text = _text(text, REPLY_MAX, "the reply", empty_ok=True)
    with store.transaction():
        _alive(store, pid)
        me = _member(store, pid, chat_id)
        person = store.one("SELECT * FROM people WHERE id=?", pid)
        # In a chat not opened to answering, a run link may only answer an ask: nothing else of it is its (rule 5).
        if link == "run" and not me["answering"] and (not answer or text or note is not None or flag is not None):
            raise NotYours()
        out: dict = {}
        if note is not None:
            store.exec("UPDATE members SET notes=? WHERE chat_id=? AND person_id=?",
                       vault.lock_text(_self(pid), _text(note, NOTE_MAX, "the notes", True), "notes"), chat_id, pid)
            out["noted"] = True
        if flag is not None:
            store.exec("UPDATE members SET flag=?, flag_t=? WHERE chat_id=? AND person_id=?",
                       vault.lock_text(_self(pid), _text(flag, FLAG_MAX, "the flag", True), "flag"), store.now(),
                       chat_id, pid)
            if flag.strip():
                _nudge(store, pid)
            out["flagged"] = bool(flag.strip())
        if (text or answer) and person["paused"]:
            raise Refused("your person paused their assistant: nothing goes out in its voice until they unpause")
        if answer:
            if not person["answer_asks"]:
                raise Refused("answer_asks is off: your person answers asks themselves (chats_send), or turns it on")
            out["answered"], out["provisional"] = _answer(store, pid, me, answer, "assistant"), True
        if text:
            # A claimed `upto` past the caller's own read place let it skip what it never read (review).
            _may_speak(store, pid, me, min(int(upto), me["run_seq"] if link == "run" else me["read_seq"]))
            out["sent"] = _append(store, pid, chat_id, "assistant", "text", text,
                                  mention=[int(n) for n in mention or []])
        if not out:
            raise Refused("nothing to do: give text, answer, note or flag")
        _audit(store, pid, link, "replied as your assistant" if text else "answered or noted as your assistant")
        return out


def _may_speak(store: Store, pid: str, me, upto: int) -> None:
    chat_id = me["chat_id"]
    if not me["answering"]:
        raise Refused("answering is off in this chat: send your person's own words with chats_send, or ask them to "
                      "turn answering on here")
    unread = [m for m in _visible(store, pid, chat_id, upto) if m["sender_id"] != pid]
    if unread:
        raise Refused(f"read these first: {len(unread)} new since {upto}; chats_inbox with this chat shows them")
    others = {r["person_id"] for r in store.all("SELECT person_id FROM members WHERE chat_id=? AND person_id<>? AND "
                                                "state IN ('joined','invited','held')", chat_id, pid)}
    if others & _blocked_by(store, pid):
        raise Refused("someone your person blocked is in this chat: only your person writes here")
    mine = store.one("SELECT COALESCE(MAX(seq), 0) s FROM messages WHERE chat_id=? AND sender_id=?", chat_id,
                     pid)["s"]
    since = [m for m in _visible(store, pid, chat_id, max(mine, me["from_seq"]), limit=10_000)
             if m["sender_id"] != pid]
    if me["kind"] == "dm":
        spoken_to = bool(since)
    else:
        spoken_to = any((m["kind"] == "ask" and not m["closed_t"] and _addressed(m, me["number"]))
                        or me["number"] in json.loads(m["mention"] or "[]") for m in since)
    if not spoken_to:
        raise Refused("not spoken to: in a DM the other side writes first, in a group an ask or a mention for your "
                      "person must be waiting")
    # rule 7: counted from metadata only, and only a person-voice message resets a run.
    last_person = store.one("SELECT COALESCE(MAX(seq), 0) s FROM messages WHERE chat_id=? AND voice='person'",
                            chat_id)["s"]
    in_a_row = store.one("SELECT COUNT(*) n FROM messages WHERE chat_id=? AND seq>? AND voice='assistant'", chat_id,
                         last_person)["n"]
    today = store.one("SELECT COUNT(*) n FROM messages WHERE chat_id=? AND sender_id=? AND voice='assistant' AND t>?",
                      chat_id, pid, store.now() - DAY)["n"]
    chats = {r["chat_id"] for r in store.all("SELECT DISTINCT chat_id FROM messages WHERE sender_id=? AND "
                                             "voice='assistant' AND t>?", pid, store.now() - 3600)}
    if in_a_row >= BRAKE_IN_A_ROW or today >= BRAKE_PER_DAY or (chat_id not in chats and
                                                                len(chats) >= BRAKE_CHATS_PER_HOUR):
        raise Refused("a brake was reached: assistants have written enough here for now, and a person must write "
                      "before they write again")


# -- settings, erasing (rules 9 and 15) ----------------------------------------------------------------------------

def settings(store: Store, pid: str, **change) -> dict:
    """Person-only. Every change nudges the person and shows in their index for a day (rule 9)."""
    with store.transaction():
        _alive(store, pid)
        out: dict = {}
        if change.get("name") is not None:
            name = _text(change["name"], NAME_MAX, "the name")
            store.exec("UPDATE people SET name=? WHERE id=?", vault.lock_text(_self(pid), name, "name"), pid)
            # The name they go by in each chat they are in, as the others there read it.
            for m in store.all("SELECT chat_id FROM members WHERE person_id=? AND state='joined' AND key<>''", pid):
                store.exec("UPDATE members SET shown=? WHERE chat_id=? AND person_id=?",
                           vault.lock_text(_chat_key(store, pid, m["chat_id"]), name, "name"), m["chat_id"], pid)
        if change.get("about") is not None:
            store.exec("UPDATE people SET about_prev=about, about=? WHERE id=?",
                       vault.lock_text(_self(pid), _text(change["about"], ABOUT_MAX, "about", True), "about"), pid)
        for key in ("answer_asks", "pause"):
            if change.get(key) is not None:
                store.exec(f"UPDATE people SET {'paused' if key == 'pause' else key}=? WHERE id=?",
                           int(change[key] == "on"), pid)
        if change.get("answering"):
            chat, on = change["answering"].get("chat", ""), change["answering"].get("on") == "on"
            _member(store, pid, chat)
            store.exec("UPDATE members SET answering=? WHERE chat_id=? AND person_id=?", int(on), chat, pid)
        nudge = change.get("nudge")
        if nudge == "on":
            topic = "chats-" + "".join(secrets.choice(_ALPHABET) for _ in range(26))
            store.exec("UPDATE people SET topic=?, nudged_t=NULL WHERE id=?", topic, pid)
            out["nudge_topic"] = topic
        elif nudge == "off":
            store.exec("UPDATE people SET topic='' WHERE id=?", pid)
        elif nudge == "test":
            p = store.one("SELECT topic, tested_t FROM people WHERE id=?", pid)
            if not p["topic"]:
                raise Refused('nudges are off: nudge "on" first')
            if p["tested_t"] and store.now() - p["tested_t"] < DAY:
                raise Refused("a test goes at most once a day")
            store.exec("UPDATE people SET tested_t=?, nudged_t=NULL WHERE id=?", store.now(), pid)
        if change.get("run_link") in ("new", "off"):     # one run link at a time: a new one ends the old
            store.exec("UPDATE run_links SET ended_t=? WHERE person_id=? AND ended_t IS NULL", store.now(), pid)
            if change["run_link"] == "new":
                out["once"] = _once(store, pid)
        done = [k for k, v in change.items() if v is not None and v != ""]
        if done:
            _audit(store, pid, "app", "changed settings: " + ", ".join(sorted(done)))
            _nudge(store, pid)
        return out


def erase(store: Store, pid: str, confirm: str) -> None:
    """Their name, rules, connections, run links, invites, blocks and memberships go; what others already received
    stays with them, under 'someone who has left'. Every call in their name fails from here on, one in flight
    included."""
    if (confirm or "").strip().lower() != "delete everything":
        raise Refused('pass confirm="delete everything" once your person has asked for this')
    with store.transaction():
        _alive(store, pid)
        now = store.now()
        store.exec("UPDATE people SET name='', about='', about_prev='', topic='', erased_t=?, public='' WHERE id=?",
                   now, pid)
        store.exec("DELETE FROM escrow WHERE person_id=?", pid)
        store.exec("UPDATE grants SET person_key='' WHERE person_id=?", pid)
        store.exec("UPDATE run_links SET person_key='' WHERE person_id=?", pid)
        _end_grants(store, "person_id=?", pid)
        store.exec("UPDATE run_links SET ended_t=COALESCE(ended_t, ?) WHERE person_id=?", now, pid)
        store.exec("DELETE FROM once WHERE person_id=?", pid)
        store.exec("DELETE FROM link_codes WHERE person_id=?", pid)
        store.exec("UPDATE invites SET note='', expires_t=MIN(expires_t, ?) WHERE maker_id=?", now, pid)
        store.exec("DELETE FROM blocks WHERE blocker_id=?", pid)
        store.exec("UPDATE members SET state='left', notes='', flag='', label='', key='', t=? WHERE person_id=? AND "
                   "state<>'left'", now, pid)
        store.exec("UPDATE members SET key='' WHERE person_id=?", pid)
        store.exec("DELETE FROM answers WHERE person_id=?", pid)
        store.exec("DELETE FROM audit WHERE person_id=?", pid)
    store.empty_log()


# -- schema 1 → 2: locking what was there before -------------------------------------------------------------------

def _lock_everything(db) -> None:
    """Everything people and their assistants wrote, locked (chats/vault.py). No connection is behind a migration, so
    each person's new key is kept open in `escrow` until each connection and run link they already had takes its own
    copy, at its next use, or for ESCROW_S at most: the one window in which a key is kept open, and only for what was
    there before. Codes and pages in flight are dropped; an invite from before is used by its code, whose maker's name
    comes with it, or by its link, without it."""
    import time
    now = time.time()
    db.execute("BEGIN")
    try:
        for statement in (
                "ALTER TABLE people ADD COLUMN public TEXT NOT NULL DEFAULT ''",
                "CREATE TABLE escrow (person_id TEXT PRIMARY KEY, private TEXT NOT NULL, until_t REAL NOT NULL)",
                "ALTER TABLE grants ADD COLUMN invite_key TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE grants ADD COLUMN person_key TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE link_codes ADD COLUMN person_key TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE run_links ADD COLUMN person_key TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE invites ADD COLUMN maker_t TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE invites ADD COLUMN maker_c TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE members ADD COLUMN key TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE members ADD COLUMN shown TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE messages ADD COLUMN mark TEXT NOT NULL DEFAULT ''",
                "CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)"):
            db.execute(statement)
        rows = lambda sql, *args: db.execute(sql, args).fetchall()         # noqa: E731
        private, public, name = {}, {}, {}
        for p in rows("SELECT * FROM people WHERE erased_t IS NULL"):
            private[p["id"]], public[p["id"]] = vault.new_pair()
            mine, name[p["id"]] = vault.self_key(private[p["id"]]), p["name"]
            db.execute("UPDATE people SET public=?, name=?, about=?, about_prev=? WHERE id=?", (
                public[p["id"]], vault.lock_text(mine, p["name"], "name"), vault.lock_text(mine, p["about"], "about"),
                vault.lock_text(mine, p["about_prev"], "about"), p["id"]))
            db.execute("INSERT INTO escrow VALUES (?,?,?)", (p["id"], vault.encode(private[p["id"]]), now + ESCROW_S))

        def own(pid: str, text: str, what: str) -> str:
            return vault.lock_text(vault.self_key(private[pid]), text, what) if pid in private else ""
        for i in rows("SELECT * FROM invites"):
            maker = name.get(i["maker_id"], "").encode()
            db.execute("UPDATE invites SET note=?, code=?, maker_c=? WHERE id=?", (
                own(i["maker_id"], i["note"], "label"), _code_hash(i["code"]),
                vault.lock(vault.from_code(i["code"], "invite"), maker, "maker"), i["id"]))
        for c in rows("SELECT * FROM chats"):
            key = vault.new_key()
            db.execute("UPDATE chats SET name=? WHERE id=?", (vault.lock_text(key, c["name"], "group name"), c["id"]))
            for m in rows("SELECT * FROM members WHERE chat_id=?", c["id"]):
                reads = m["state"] in ("joined", "invited") and m["person_id"] in public
                db.execute("UPDATE members SET label=?, notes=?, flag=?, key=?, shown=? WHERE chat_id=? AND "
                           "person_id=?", (
                               own(m["person_id"], m["label"], "label"), own(m["person_id"], m["notes"], "notes"),
                               own(m["person_id"], m["flag"], "flag"),
                               vault.seal(public[m["person_id"]], key, "chat") if reads else "",
                               vault.lock_text(key, name.get(m["person_id"], ""), "name"), c["id"], m["person_id"]))
            for msg in rows("SELECT * FROM messages WHERE chat_id=?", c["id"]):
                db.execute("UPDATE messages SET text=?, options=?, outcome=?, mark=? WHERE chat_id=? AND seq=?", (
                    vault.lock_text(key, msg["text"], "message"),
                    vault.lock_text(key, msg["options"], "options") if msg["options"] else msg["options"],
                    vault.lock_text(key, msg["outcome"], "outcome") if msg["outcome"] else msg["outcome"],
                    vault.mark(key, f"{msg['voice']} {msg['text']}"), c["id"], msg["seq"]))
        if rows("SELECT 1 FROM reports LIMIT 1"):
            report_private, report_public = vault.new_pair()
            path = next(r[2] for r in rows("PRAGMA database_list") if r[1] == "main")
            if path:
                key_file = Path(path).with_name("report.key")
                key_file.touch(mode=0o600)
                key_file.write_text(vault.encode(report_private))
            db.execute("INSERT INTO kv VALUES ('report_public', ?)", (report_public,))
            for r in rows("SELECT * FROM reports"):
                db.execute("UPDATE reports SET why='', copy=? WHERE id=?", (vault.seal(report_public, json.dumps(
                    {"why": r["why"], "copy": json.loads(r["copy"] or "[]")}).encode(), "report"), r["id"]))
        db.execute("UPDATE grants SET invite='', name=''")
        db.execute("DELETE FROM tokens WHERE kind IN ('request', 'code')")
        db.execute("DELETE FROM link_codes WHERE used_t IS NULL")
        db.execute("DELETE FROM once")
        db.execute("UPDATE audit SET what='labelled a contact' WHERE what LIKE 'labelled a contact%'")
        db.execute("PRAGMA user_version=2")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    # Nothing written before may stay in a free page or the log: the file is written afresh, and the log emptied.
    db.execute("VACUUM")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()


MIGRATIONS[1] = _lock_everything
