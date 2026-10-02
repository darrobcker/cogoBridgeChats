-- Schema version 1, as the live database had it before 2 (locks).

-- A person exists once their own AI first calls through a connection (PROTOCOL.md §5). `name` is their own claim,
-- shown as one. `about` is their standing rules for their own AI: read only by them.
CREATE TABLE people (
  id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', about TEXT NOT NULL DEFAULT '',
  about_prev TEXT NOT NULL DEFAULT '', answer_asks INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0,
  topic TEXT NOT NULL DEFAULT '', nudged_t REAL, tested_t REAL, created_t REAL NOT NULL, erased_t REAL);
-- A connection an app made by signing in: the person, once its first call made them; `invite` (hashed) and `name`,
-- what the invite page left in that browser, used at that first call.
CREATE TABLE grants (
  id TEXT PRIMARY KEY, client_id TEXT NOT NULL, person_id TEXT NOT NULL DEFAULT '', invite TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '', created_t REAL NOT NULL, revoked_t REAL);
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
CREATE TABLE link_codes (
  hash TEXT PRIMARY KEY, person_id TEXT NOT NULL, replace INTEGER NOT NULL, created_t REAL NOT NULL, used_t REAL);
-- Counts per day, server-wide: apps registered, people made with an invite and without one.
CREATE TABLE daily (k TEXT NOT NULL, day INTEGER NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (k, day));
-- A routine's connector URL, hashed: it can do only its delegation (PROTOCOL.md rule 5).
CREATE TABLE run_links (hash TEXT PRIMARY KEY, person_id TEXT NOT NULL, created_t REAL NOT NULL, ended_t REAL);
CREATE INDEX run_links_person ON run_links(person_id);
-- A page that shows a new run link once, on a POST, so a link preview cannot use it up.
CREATE TABLE once (
  hash TEXT PRIMARY KEY, person_id TEXT NOT NULL, secret TEXT NOT NULL, created_t REAL NOT NULL, used_t REAL);
-- One-time invites. `chat_id` is a group the one who uses it is invited to, once the maker confirms them.
CREATE TABLE invites (
  id TEXT PRIMARY KEY, hash TEXT NOT NULL UNIQUE, code TEXT NOT NULL UNIQUE, maker_id TEXT NOT NULL,
  note TEXT NOT NULL, chat_id TEXT, created_t REAL NOT NULL, expires_t REAL NOT NULL, used_t REAL, used_by TEXT);
CREATE INDEX invites_maker ON invites(maker_id);
-- A DM is the contact itself: one per pair (`pair` is the two ids in order). A group has a name and an owner.
CREATE TABLE chats (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', owner_id TEXT, pair TEXT UNIQUE,
  seq INTEGER NOT NULL DEFAULT 0, created_t REAL NOT NULL);
-- state: held (used an invite to it; waits for the maker to confirm them), invited, joined, left, declined.
-- `label` (DMs): how the other person appears to this one; `confirmed` = 0 until this side has said who it is.
CREATE TABLE members (
  chat_id TEXT NOT NULL, person_id TEXT NOT NULL, state TEXT NOT NULL, invited_by TEXT, number INTEGER NOT NULL,
  label TEXT NOT NULL DEFAULT '', confirmed INTEGER NOT NULL DEFAULT 1, from_seq INTEGER NOT NULL DEFAULT 0,
  read_seq INTEGER NOT NULL DEFAULT 0, run_seq INTEGER NOT NULL DEFAULT 0, answering INTEGER NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '', flag TEXT NOT NULL DEFAULT '', flag_t REAL, t REAL NOT NULL,
  PRIMARY KEY (chat_id, person_id));
CREATE INDEX members_person ON members(person_id, state);
-- voice: 'person' only through chats_send, 'assistant' through chats_reply (rule 4). An ask carries options.
CREATE TABLE messages (
  chat_id TEXT NOT NULL, seq INTEGER NOT NULL, sender_id TEXT NOT NULL, voice TEXT NOT NULL, kind TEXT NOT NULL,
  text TEXT NOT NULL, options TEXT, multi INTEGER NOT NULL DEFAULT 0, due_t REAL, for_numbers TEXT,
  mention TEXT, outcome TEXT, closed_t REAL, t REAL NOT NULL, PRIMARY KEY (chat_id, seq));
CREATE INDEX messages_sender ON messages(sender_id, t);
CREATE TABLE answers (
  chat_id TEXT NOT NULL, ask_seq INTEGER NOT NULL, person_id TEXT NOT NULL, choice TEXT NOT NULL,
  voice TEXT NOT NULL, t REAL NOT NULL, PRIMARY KEY (chat_id, ask_seq, person_id));
CREATE TABLE blocks (blocker_id TEXT NOT NULL, blocked_id TEXT NOT NULL, t REAL NOT NULL,
  PRIMARY KEY (blocker_id, blocked_id));
-- A member's copy of what one sender sent them, for the operator only (the CLI).
CREATE TABLE reports (id TEXT PRIMARY KEY, reporter_id TEXT NOT NULL, chat_id TEXT NOT NULL,
  sender_id TEXT NOT NULL, why TEXT NOT NULL, copy TEXT NOT NULL, t REAL NOT NULL);
-- What was done in a person's name, and how: through an app (`app`) or a routine's run link (`run`). `mine` reads it.
CREATE TABLE audit (person_id TEXT NOT NULL, t REAL NOT NULL, link TEXT NOT NULL, what TEXT NOT NULL);
CREATE INDEX audit_person ON audit(person_id, t);
PRAGMA user_version=1;
