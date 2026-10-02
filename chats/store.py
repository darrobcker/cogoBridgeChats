"""SQLite: the schema, one connection, one lock.

One connection is shared by the web event loop and the MCP SDK's worker threads. Two threads inside `BEGIN` on
one connection raise, and interleaved reads return rows that are not there, so every statement takes the lock,
and every command that reads before it writes runs inside `transaction()`. At this scale serialising is free.
"""
from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

SCHEMA_VERSION = 2
# From each older version to the next, applied in order when a database is opened: `{version: script}`, each script
# ending with the next version's `PRAGMA user_version`, or a function of the connection that does the same: 1 → 2,
# which locks everything, is `core._lock_everything`, registered by core. This repository starts at version 1.
MIGRATIONS: dict[int, str | Callable[[sqlite3.Connection], None]] = {}

SCHEMA = """
-- A person exists once their own AI first calls through a connection (PROTOCOL.md §5). `name` is their own claim,
-- shown as one. `about` is their standing rules for their own AI: read only by them. Both are locked under their
-- own key (chats/vault.py); `public` is the half anyone may seal to them with. `topic` is kept as it is: the server
-- nudges it with nobody's connection behind the call.
CREATE TABLE people (
  id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', about TEXT NOT NULL DEFAULT '',
  about_prev TEXT NOT NULL DEFAULT '', answer_asks INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0,
  topic TEXT NOT NULL DEFAULT '', nudged_t REAL, tested_t REAL, created_t REAL NOT NULL, erased_t REAL,
  public TEXT NOT NULL DEFAULT '');
-- A person's key from before locks, for the connections and run links they already had: each takes its own copy at
-- its next use, and this goes when they all have, or at `until_t`. The only key kept open.
CREATE TABLE escrow (person_id TEXT PRIMARY KEY, private TEXT NOT NULL, until_t REAL NOT NULL);
-- A connection an app made by signing in: the person, once its first call made them. `invite` (hashed) is the invite
-- the invite page left in that browser; `invite_key`, what that first call needs from it (the name they gave, and
-- the name its maker goes by), and `person_key` the person's private key, both locked under its renewal token.
CREATE TABLE grants (
  id TEXT PRIMARY KEY, client_id TEXT NOT NULL, person_id TEXT NOT NULL DEFAULT '', invite TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '', created_t REAL NOT NULL, revoked_t REAL, invite_key TEXT NOT NULL DEFAULT '',
  person_key TEXT NOT NULL DEFAULT '');
CREATE INDEX grants_person ON grants(person_id, revoked_t);
-- Hashed, each of: a sign-in waiting on the Allow page (`request`), an authorization code (`code`), an access or a
-- renewal token (`access`, `refresh`). `data`: what the next step needs.
CREATE TABLE tokens (
  hash TEXT PRIMARY KEY, kind TEXT NOT NULL, grant_id TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT '',
  created_t REAL NOT NULL, expires_t REAL);
CREATE INDEX tokens_grant ON tokens(grant_id);
-- Apps registered for sign-in (RFC 7591), as the MCP SDK describes them.
CREATE TABLE clients (id TEXT PRIMARY KEY, info TEXT NOT NULL, created_t REAL NOT NULL);
-- A code from the chat, hashed, that makes another app's connection this person's. `replace`: its use ends every other.
-- `person_key`: their private key, locked under the code; empty in one the operator made, who holds no key.
CREATE TABLE link_codes (
  hash TEXT PRIMARY KEY, person_id TEXT NOT NULL, replace INTEGER NOT NULL, created_t REAL NOT NULL, used_t REAL,
  person_key TEXT NOT NULL DEFAULT '');
-- Counts per day, server-wide: apps registered, people made with an invite and without one.
CREATE TABLE daily (k TEXT NOT NULL, day INTEGER NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (k, day));
-- A routine's connector URL, hashed: it can do only its delegation (PROTOCOL.md rule 5).
CREATE TABLE run_links (hash TEXT PRIMARY KEY, person_id TEXT NOT NULL, created_t REAL NOT NULL, ended_t REAL,
  person_key TEXT NOT NULL DEFAULT '');
CREATE INDEX run_links_person ON run_links(person_id);
-- A page that shows a new run link once, on a POST, so a link preview cannot use it up; the link is locked under
-- the page's token until then.
CREATE TABLE once (
  hash TEXT PRIMARY KEY, person_id TEXT NOT NULL, secret TEXT NOT NULL, created_t REAL NOT NULL, used_t REAL);
-- One-time invites. `chat_id` is a group the one who uses it is invited to, once the maker confirms them. `hash` and
-- `code` are the token's and the code's hashes; `note` is locked under the maker's own key, and the name the maker
-- goes by under the token (`maker_t`) and the code (`maker_c`), for whoever uses it.
CREATE TABLE invites (
  id TEXT PRIMARY KEY, hash TEXT NOT NULL UNIQUE, code TEXT NOT NULL UNIQUE, maker_id TEXT NOT NULL,
  note TEXT NOT NULL, chat_id TEXT, created_t REAL NOT NULL, expires_t REAL NOT NULL, used_t REAL, used_by TEXT,
  maker_t TEXT NOT NULL DEFAULT '', maker_c TEXT NOT NULL DEFAULT '');
CREATE INDEX invites_maker ON invites(maker_id);
-- A DM is the contact itself: one per pair (`pair` is the two ids in order). A group has a name and an owner; the
-- name is locked under the chat's key.
CREATE TABLE chats (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', owner_id TEXT, pair TEXT UNIQUE,
  seq INTEGER NOT NULL DEFAULT 0, created_t REAL NOT NULL);
-- state: held (used an invite to it; waits for the maker to confirm them), invited, joined, left, declined.
-- `label` (DMs): how the other person appears to this one; `confirmed` = 0 until this side has said who it is.
-- `label`, `notes` and `flag` are this member's own, locked under their key. `key`: the chat's, sealed to them;
-- `shown`: the name they go by there, locked under it, for the others in it.
CREATE TABLE members (
  chat_id TEXT NOT NULL, person_id TEXT NOT NULL, state TEXT NOT NULL, invited_by TEXT, number INTEGER NOT NULL,
  label TEXT NOT NULL DEFAULT '', confirmed INTEGER NOT NULL DEFAULT 1, from_seq INTEGER NOT NULL DEFAULT 0,
  read_seq INTEGER NOT NULL DEFAULT 0, run_seq INTEGER NOT NULL DEFAULT 0, answering INTEGER NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '', flag TEXT NOT NULL DEFAULT '', flag_t REAL, t REAL NOT NULL,
  key TEXT NOT NULL DEFAULT '', shown TEXT NOT NULL DEFAULT '', PRIMARY KEY (chat_id, person_id));
CREATE INDEX members_person ON members(person_id, state);
-- voice: 'person' only through chats_send, 'assistant' through chats_reply (rule 4). An ask carries options. `text`,
-- `options` and `outcome` are locked under the chat's key; `mark` tells a repeated send without them.
CREATE TABLE messages (
  chat_id TEXT NOT NULL, seq INTEGER NOT NULL, sender_id TEXT NOT NULL, voice TEXT NOT NULL, kind TEXT NOT NULL,
  text TEXT NOT NULL, options TEXT, multi INTEGER NOT NULL DEFAULT 0, due_t REAL, for_numbers TEXT,
  mention TEXT, outcome TEXT, closed_t REAL, t REAL NOT NULL, mark TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (chat_id, seq));
CREATE INDEX messages_sender ON messages(sender_id, t);
CREATE TABLE answers (
  chat_id TEXT NOT NULL, ask_seq INTEGER NOT NULL, person_id TEXT NOT NULL, choice TEXT NOT NULL,
  voice TEXT NOT NULL, t REAL NOT NULL, PRIMARY KEY (chat_id, ask_seq, person_id));
CREATE TABLE blocks (blocker_id TEXT NOT NULL, blocked_id TEXT NOT NULL, t REAL NOT NULL,
  PRIMARY KEY (blocker_id, blocked_id));
-- A member's copy of what one sender sent them, for the operator only: sealed to the operator's report key, whose
-- private half is kept beside the database, never in it (the CLI's `reports`).
CREATE TABLE reports (id TEXT PRIMARY KEY, reporter_id TEXT NOT NULL, chat_id TEXT NOT NULL,
  sender_id TEXT NOT NULL, why TEXT NOT NULL, copy TEXT NOT NULL, t REAL NOT NULL);
-- What was done in a person's name, and how: through an app (`app`) or a routine's run link (`run`). `mine` reads it.
CREATE TABLE audit (person_id TEXT NOT NULL, t REAL NOT NULL, link TEXT NOT NULL, what TEXT NOT NULL);
CREATE INDEX audit_person ON audit(person_id, t);
CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        # Deleted rows otherwise stay as bytes in free pages, which every backup copies whole.
        self.db.execute("PRAGMA secure_delete=ON")
        self._lock = threading.RLock()
        self._depth = 0
        self._clock: Callable[[], float] = time.time
        # Called with a person's ntfy topic after something that needs them is committed.
        self.on_nudge: Callable[[str], None] | None = None
        self.pending: list[str] = []          # people to nudge once the change that needs them is committed
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
            self.db.executescript(SCHEMA)
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        else:
            while version in MIGRATIONS:
                step = MIGRATIONS[version]
                step(self.db) if callable(step) else self.db.executescript(step)
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise RuntimeError(f"{self.path} has schema version {version}; this code needs {SCHEMA_VERSION}. "
                                   "Move the file aside, or migrate it.")

    def now(self) -> float:
        return self._clock()

    def set_clock(self, fn: Callable[[], float]) -> None:
        self._clock = fn

    @contextlib.contextmanager
    def transaction(self):
        """One writer at a time, and read-then-write is atomic. Re-entrant: an inner one joins the outer."""
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self.db.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            finally:
                self._depth = 0

    def empty_log(self) -> None:
        """Copy the write-ahead log into the file and empty it, so what an erase replaced is not left there. Never
        waits: behind a backup's read a checkpoint would hold the lock every call takes."""
        with self._lock:
            if self._depth:
                return
            wait = self.db.execute("PRAGMA busy_timeout").fetchone()[0]
            self.db.execute("PRAGMA busy_timeout=0")
            try:
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            finally:
                self.db.execute(f"PRAGMA busy_timeout={int(wait)}")

    def one(self, sql: str, *args) -> sqlite3.Row | None:
        with self._lock:
            return self.db.execute(sql, args).fetchone()

    def all(self, sql: str, *args) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    def exec(self, sql: str, *args) -> sqlite3.Cursor:
        with self._lock:
            return self.db.execute(sql, args)
