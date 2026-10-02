# Working in this repository

For coding agents and new contributors. Read `PROTOCOL.md` first: it is short, and it is the contract.

## Map

| Path | What it is |
|---|---|
| `PROTOCOL.md` | The protocol: roles, objects, rules, operations. Normative. Change it before changing behaviour |
| `chats/vault.py` | Locks: the keys what anyone writes is kept under, and the keys one call holds (rule 18) |
| `chats/core.py` | Every rule of PROTOCOL.md, and sign-in's. If a rule is enforced anywhere else, that is a bug |
| `chats/views.py` | What an assistant is shown, and `mark()` |
| `chats/mcp_server.py` | The five tools, and the instructions every assistant receives |
| `chats/oauth.py` | Sign-in: the MCP SDK's OAuth server, answered from `core`, where its rules are |
| `chats/web.py`, `templates/`, `static/` | Home, the invite and connect pages, the Allow page, the one-time run-link page, the not-found page; `CHATS_THEME` gives a server its own look. There is no other web surface, on purpose |
| `chats/store.py` | SQLite schema, one connection, one lock |
| `chats/notify.py`, `app.py`, `cli.py` | The nudge; ASGI wiring; operator commands |
| `tests/test_core.py` | One section per rule, and sign-in's |
| `tests/test_surface.py` | The pages, sign-in, and MCP over a real port as a real client would |
| `tests/test_ops.py` | The operator's scripts, against a scratch home |
| `docs/DESIGN.md` | What holds, what does not, and why it is shaped this way |
| `docs/OPERATIONS.md`, `scripts/` | Running a server: address, backups, alerts |

## Commands

```bash
uv sync
uv run pytest -q          # must pass, and must pass five times in a row: there are threads in it
uv run ruff check .       # must be clean
CHATS_HOME=/tmp/chats-dev uv run chats serve --port 8771
```

## Invariants: a change that breaks one of these is wrong, whatever else it fixes

1. A stranger is never shown: nothing reaches a person, or shows them, except through a contact or a chat they
   accepted (rule 1).
2. The voice comes from the tool, never from an argument (rule 4).
3. A run link reaches only its delegation (rule 5), and no result contains one (rule 10).
4. What anyone wrote reaches an assistant only through `mark()` (rule 10).
5. No model runs on the server and no API key is read.
6. Every writer checks its person is alive inside its transaction (`tests/test_core.py` finds them all).
7. Bridge Chats imports nothing from Bridge.
8. What anyone writes is stored only locked, and a call holds the key of no one but the person whose connection or
   run link it came through (rule 18). `tests/test_core.py::test_nothing_anyone_wrote_is_kept_readable` scans the
   database for it.

## Conventions

- The product is Bridge Chats; the package, command and settings are `chats` (`CHATS_*`).
- When a review or a real user finds a bug: reproduce it as a test first, say in the test's docstring what went
  wrong, then fix it.
- Comments say why, not what. Docs state nothing that a code change would silently make false: no test counts,
  and the list of operations lives only in `PROTOCOL.md`.
- The schema has a version (`store.SCHEMA_VERSION`). A schema change bumps it and ships a migration.
