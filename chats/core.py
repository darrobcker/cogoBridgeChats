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

from .store import Store

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
VERSION = "0.2"

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
    store.exec("INSERT INTO audit VALUES (?,?,?,?)", pid, store.now(), link, what)


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
    with store.transaction():
        pid = _new_id(store, "people", "p", 10)
        store.exec("INSERT INTO people(id, name, created_t) VALUES (?,?,?)", pid, _text(name, NAME_MAX, "the name",
                                                                                          True), store.now())
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


def _token(store: Store, kind: str, *, grant_id: str = "", data: dict | None = None,
           expires_t: float | None = None) -> str:
    token = secrets.token_urlsafe(32)
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
        grant = _new_id(store, "grants", "k", 16)
        store.exec("INSERT INTO grants(id, client_id, invite, name, created_t) VALUES (?,?,?,?,?)", grant,
                   data["client_id"], found["hash"] if found else "",
                   _text(name, NAME_MAX, "the name", True)[:NAME_MAX] if found else "", store.now())
        return data, _token(store, "code", grant_id=grant, data=data, expires_t=store.now() + AUTH_CODE_S)


def deny(store: Store, request: str) -> dict | None:
    with store.transaction():
        row = _live_token(store, request, "request")
        if row:
            store.exec("DELETE FROM tokens WHERE hash=?", row["hash"])
        return json.loads(row["data"]) if row else None


def authorization_code(store: Store, code: str) -> dict | None:
    row = _live_token(store, code, "code")
    return {**json.loads(row["data"]), "grant": row["grant_id"], "expires_t": row["expires_t"]} if row else None


def _access_token(store: Store, grant_id: str, scopes: list[str]) -> str:
    return _token(store, "access", grant_id=grant_id, data={"scopes": scopes}, expires_t=store.now() + ACCESS_S)


def exchange_code(store: Store, code: str) -> tuple[str, str] | None:
    """An authorization code for an access token and the renewal token that replaces it, once."""
    with store.transaction():
        row = _live_token(store, code, "code")
        if not row:
            return None
        store.exec("DELETE FROM tokens WHERE hash=?", row["hash"])
        scopes = json.loads(row["data"]).get("scopes") or []
        return (_access_token(store, row["grant_id"], scopes),
                _token(store, "refresh", grant_id=row["grant_id"], data={"scopes": scopes}))


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
        return _access_token(store, row["grant_id"], json.loads(row["data"]).get("scopes") or []), token


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


def person_for_grant(store: Store, grant_id: str) -> str:
    """Whose connection this is. Its first call makes the person, under the name the invite page was given, and
    uses the invite the connection was allowed with if it still works: the contact is made then (rule 2). Without
    one, the person reaches nobody until they make or use an invite (rule 1)."""
    with store.transaction():
        # LEFT JOIN, so a connection whose person row is gone refuses rather than resolving to nobody.
        g = store.one("SELECT g.*, p.id pid, p.erased_t FROM grants g LEFT JOIN people p ON p.id=g.person_id "
                      "WHERE g.id=? AND g.revoked_t IS NULL", grant_id)
        if not g:
            raise NotYours()
        if g["person_id"]:
            if g["pid"] is None or g["erased_t"] is not None:
                raise Refused(_ERASED)
            return g["person_id"]
        inv = _usable_invite(store, hashed=g["invite"])
        kind, cap = ("invited", INVITED_PER_DAY) if inv else ("uninvited", UNINVITED_PER_DAY)
        if _today(store, kind) >= cap:
            raise Refused("this server is taking no more new people today. Try again tomorrow")
        _count(store, kind)
        pid = new_person(store, g["name"])
        store.exec("UPDATE grants SET person_id=? WHERE id=?", pid, grant_id)
        if inv:
            _use(store, inv, pid)
        return pid


def _run_link(store: Store, pid: str) -> str:
    secret = secrets.token_urlsafe(24)
    store.exec("INSERT INTO run_links VALUES (?,?,?,NULL)", _hash(secret), pid, store.now())
    return secret


def person_for_run_link(store: Store, secret: str) -> str | None:
    row = store.one("SELECT l.person_id FROM run_links l JOIN people p ON p.id=l.person_id WHERE l.hash=? AND "
                    "l.ended_t IS NULL AND p.erased_t IS NULL", _hash(secret or ""))
    return row["person_id"] if row else None


def _once(store: Store, pid: str) -> str:
    """A page that shows a new run link once. The link is made now; the page holds it until a POST reveals it."""
    token = secrets.token_urlsafe(18)
    store.exec("INSERT INTO once VALUES (?,?,?,?,NULL)", _hash(token), pid, _run_link(store, pid), store.now())
    return token


def reveal_once(store: Store, token: str) -> str | None:
    """The run link's secret, once, within a day; a page already opened, old, or of an erased person shows nothing."""
    with store.transaction():
        row = store.one("SELECT o.* FROM once o JOIN people p ON p.id=o.person_id WHERE o.hash=? AND o.used_t IS "
                        "NULL AND o.created_t > ? AND p.erased_t IS NULL", _hash(token or ""), store.now() - DAY)
        if not row:
            return None
        store.exec("UPDATE once SET used_t=?, secret='' WHERE hash=?", store.now(), row["hash"])
        return row["secret"]


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
    with store.transaction():
        _alive(store, pid)
        store.exec("DELETE FROM link_codes WHERE person_id=? AND used_t IS NULL", pid)
        store.exec("INSERT INTO link_codes(hash, person_id, replace, created_t) VALUES (?,?,?,?)", _code_hash(raw),
                   pid, int(replace), store.now())
        _audit(store, pid, "app", "made a code for " + ("a new connection, ending the others" if replace else
                                                         "another app"))
    return "-".join(raw[i:i + 4] for i in range(0, LINK_CODE, 4))


ALREADY_LINKED, LINKED, MOVED = "already", "linked", "moved"


def use_link_code(store: Store, pid: str, grant_id: str, code: str) -> tuple[str, str]:
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
        if target == pid:
            return ALREADY_LINKED, target
        if not holds_nothing(store, pid):
            raise Refused("this connection's account already holds things of its own (a name, rules, a chat or an "
                          "invite), which a code would lose. Keep the two apart, or, on your person's say-so, erase "
                          "this one first and then use the code")
        store.exec("UPDATE link_codes SET used_t=? WHERE hash=?", store.now(), row["hash"])
        store.exec("UPDATE grants SET person_id=? WHERE id=?", target, grant_id)
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
        for note in notes:
            token = secrets.token_urlsafe(16)
            code = "".join(secrets.choice(_ALPHABET) for _ in range(10))
            store.exec("INSERT INTO invites(id, hash, code, maker_id, note, chat_id, created_t, expires_t) "
                       "VALUES (?,?,?,?,?,?,?,?)", _new_id(store, "invites", "i"), _hash(token), code, pid, note,
                       chat_id, store.now(), store.now() + INVITE_DAYS * DAY)
            out.append({"for": note, "token": token, "code": code})
        _audit(store, pid, "app", f"made {len(notes)} invite(s)")
    return out


def _usable_invite(store: Store, token: str = "", code: str = "", hashed: str = ""):
    """By its URL's token, its code, or the token's hash a connection kept; none of the three empty matches."""
    hashed = hashed or (_hash(token) if token else "")
    if not hashed and not code:
        return None
    return store.one("SELECT i.* FROM invites i JOIN people p ON p.id=i.maker_id WHERE (i.hash=? OR i.code=?) "
                     "AND i.used_t IS NULL AND i.expires_t > ? AND p.erased_t IS NULL", hashed, code, store.now())


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
        chat = _use(store, inv, pid)
        maker = store.one("SELECT name FROM people WHERE id=?", inv["maker_id"])["name"]
        return {"chat": chat, "calls_themselves": maker, "group_invite_held": bool(inv["chat_id"])}


def _use(store: Store, inv, pid: str) -> str:
    """The contact, one DM per pair: connecting twice returns the one there is. The maker's side stays
    unconfirmed, labelled with their own note, until they say it is who they meant: whoever used the link may not
    be the one it was for (challenge round)."""
    store.exec("UPDATE invites SET used_t=?, used_by=? WHERE id=?", store.now(), pid, inv["id"])
    maker = inv["maker_id"]
    pair = "|".join(sorted((maker, pid)))
    row = store.one("SELECT id FROM chats WHERE pair=?", pair)
    if row:
        chat = row["id"]
    else:
        chat = _new_id(store, "chats", "c")
        store.exec("INSERT INTO chats(id, kind, pair, created_t) VALUES (?,?,?,?)", chat, "dm", pair, store.now())
        name = store.one("SELECT name FROM people WHERE id=?", maker)["name"]
        store.exec("INSERT INTO members(chat_id, person_id, state, number, label, confirmed, t) VALUES "
                   "(?,?,'joined',1,?,0,?)", chat, maker, inv["note"], store.now())
        store.exec("INSERT INTO members(chat_id, person_id, state, number, label, confirmed, t) VALUES "
                   "(?,?,'joined',2,?,1,?)", chat, pid, name[:NAME_MAX] or "whoever sent the invite", store.now())
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
        label = _text(label, NAME_MAX, "the label", True) or chat["label"]
        store.exec("UPDATE members SET label=?, confirmed=1 WHERE chat_id=? AND person_id=?", label, chat["id"], pid)
        held = store.all("SELECT chat_id FROM members WHERE person_id=? AND state='held' AND invited_by=?", other, pid)
        for h in held:
            store.exec("UPDATE members SET state='invited', t=? WHERE chat_id=? AND person_id=?", store.now(),
                       h["chat_id"], other)
        if held:
            _nudge(store, other)
        _audit(store, pid, "app", f"labelled a contact {label!r}")
        return label


def _contact(store: Store, pid: str, ref: str):
    """A DM of the caller's, by its id or by the caller's label for the other person. Returns (my row, other id)."""
    ref = (ref or "").strip()
    rows = store.all("SELECT m.*, c.id FROM members m JOIN chats c ON c.id=m.chat_id WHERE c.kind='dm' AND "
                     "m.person_id=? AND (c.id=? OR lower(m.label)=lower(?))", pid, ref, ref)
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


def _add_member(store: Store, chat_id: str, pid: str, by: str, state: str) -> None:
    row = store.one("SELECT state FROM members WHERE chat_id=? AND person_id=?", chat_id, pid)
    if row and row["state"] in ("joined", "invited", "held"):
        return
    count = store.one("SELECT COUNT(*) n FROM members WHERE chat_id=? AND state IN ('joined','invited','held')",
                      chat_id)["n"]
    if count >= GROUP_MAX:
        raise Refused(f"a group holds {GROUP_MAX} people, invitations included")
    number = store.one("SELECT COALESCE(MAX(number), 0) + 1 n FROM members WHERE chat_id=?", chat_id)["n"]
    store.exec("INSERT OR REPLACE INTO members(chat_id, person_id, state, invited_by, number, t) VALUES "
               "(?,?,?,?,?,?)", chat_id, pid, state, by, number, store.now())
    if state == "invited":
        _nudge(store, pid)


def start_group(store: Store, pid: str, name: str, contacts: list[str]) -> tuple[str, list[str]]:
    name = _text(name, NAME_MAX, "the group's name")
    with store.transaction():
        _alive(store, pid)
        others = [_contact(store, pid, c) for c in contacts]
        chat = _new_id(store, "chats", "c")
        store.exec("INSERT INTO chats(id, kind, name, owner_id, created_t) VALUES (?,?,?,?,?)", chat, "group", name,
                   pid, store.now())
        store.exec("INSERT INTO members(chat_id, person_id, state, number, t) VALUES (?,?,'joined',1,?)", chat, pid,
                   store.now())
        for _, other in others:
            _add_member(store, chat, other, pid, "invited")
        return chat, [row["label"] for row, _ in others]


def invite_to(store: Store, pid: str, chat_id: str, contacts: list[str]) -> list[str]:
    """Any member invites their own contacts, and nobody else (rule 3)."""
    with store.transaction():
        _alive(store, pid)
        _group_member(store, pid, chat_id)
        others = [_contact(store, pid, c) for c in contacts]
        for _, other in others:
            _add_member(store, chat_id, other, pid, "invited")
        _audit(store, pid, "app", f"invited {len(others)} to a group")
        return [row["label"] for row, _ in others]


def accept(store: Store, pid: str, chat_id: str) -> None:
    """They see messages from their joining on: an ex invited by a friend saw plans pinned before (challenge)."""
    with store.transaction():
        _alive(store, pid)
        row = _member(store, pid, chat_id, ("invited",))
        store.exec("UPDATE members SET state='joined', from_seq=?, read_seq=?, run_seq=?, t=? WHERE chat_id=? AND "
                   "person_id=?", row["seq"], row["seq"], row["seq"], store.now(), chat_id, pid)
        _audit(store, pid, "app", "joined a group")


def decline(store: Store, pid: str, chat_id: str) -> None:
    """Silent: to the others a decline looks like an invitation never answered (rule 11)."""
    with store.transaction():
        _alive(store, pid)
        _member(store, pid, chat_id, ("invited",))
        store.exec("UPDATE members SET state='declined' WHERE chat_id=? AND person_id=?", chat_id, pid)


def leave(store: Store, pid: str, chat_id: str) -> None:
    with store.transaction():
        _alive(store, pid)
        row = _group_member(store, pid, chat_id)
        store.exec("UPDATE members SET state='left', t=? WHERE chat_id=? AND person_id=?", store.now(), chat_id, pid)
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
        store.exec("UPDATE members SET state='left', t=? WHERE chat_id=? AND person_id=?", store.now(), chat_id,
                   target["person_id"])
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
        sent = store.all("SELECT seq, text, voice, t FROM messages WHERE chat_id=? AND sender_id=? AND seq>? ORDER BY "
                         "seq DESC LIMIT 50", chat_id, other, me["from_seq"])
        store.exec("INSERT INTO reports VALUES (?,?,?,?,?,?,?)", _new_id(store, "reports", "r"), pid, chat_id, other,
                   why, json.dumps([dict(r) for r in sent]), store.now())
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
    store.exec("UPDATE chats SET seq=? WHERE id=?", seq, chat_id)
    store.exec("INSERT INTO messages(chat_id, seq, sender_id, voice, kind, text, options, multi, due_t, for_numbers, "
               "mention, t) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", chat_id, seq, pid, voice, kind, text,
               json.dumps(extra.get("options")) if extra.get("options") is not None else None,
               int(bool(extra.get("multi"))), extra.get("due_t"),
               json.dumps(extra.get("for_numbers")) if extra.get("for_numbers") else None,
               json.dumps(extra.get("mention")) if extra.get("mention") else None, store.now())
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
    options = json.loads(ask["options"])
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
            store.exec("UPDATE messages SET outcome=?, closed_t=? WHERE chat_id=? AND seq=?", outcome, store.now(),
                       chat_id, asked["seq"])
            out["closed"] = asked["seq"]
            _nudge_members(store, pid, chat_id)
        if text:
            dup = store.one("SELECT seq FROM messages WHERE chat_id=? AND sender_id=? AND voice='person' AND text=? "
                            "AND t > ? ORDER BY seq DESC LIMIT 1", chat_id, pid, text,
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
            store.exec("UPDATE members SET notes=? WHERE chat_id=? AND person_id=?", _text(note, NOTE_MAX, "the notes",
                       True), chat_id, pid)
            out["noted"] = True
        if flag is not None:
            store.exec("UPDATE members SET flag=?, flag_t=? WHERE chat_id=? AND person_id=?",
                       _text(flag, FLAG_MAX, "the flag", True), store.now(), chat_id, pid)
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
            store.exec("UPDATE people SET name=? WHERE id=?", _text(change["name"], NAME_MAX, "the name"), pid)
        if change.get("about") is not None:
            store.exec("UPDATE people SET about_prev=about, about=? WHERE id=?",
                       _text(change["about"], ABOUT_MAX, "about", True), pid)
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
        store.exec("UPDATE people SET name='', about='', about_prev='', topic='', erased_t=? WHERE id=?", now, pid)
        _end_grants(store, "person_id=?", pid)
        store.exec("UPDATE run_links SET ended_t=COALESCE(ended_t, ?) WHERE person_id=?", now, pid)
        store.exec("DELETE FROM once WHERE person_id=?", pid)
        store.exec("DELETE FROM link_codes WHERE person_id=?", pid)
        store.exec("UPDATE invites SET note='', expires_t=MIN(expires_t, ?) WHERE maker_id=?", now, pid)
        store.exec("DELETE FROM blocks WHERE blocker_id=?", pid)
        store.exec("UPDATE members SET state='left', notes='', flag='', label='', t=? WHERE person_id=? AND "
                   "state<>'left'", now, pid)
        store.exec("DELETE FROM answers WHERE person_id=?", pid)
        store.exec("DELETE FROM audit WHERE person_id=?", pid)
    store.empty_log()
