# Operations: running a server

A server is one process and one SQLite file. What matters is that the file survives, that its address stays the
same, and that its operator knows when it is down.

## How it runs

- The server: `uv run chats serve --port 8771`, with `CHATS_BASE_URL` (its public https address) and
  `CHATS_OPERATOR` (who runs it, named on every page and to every assistant, which say what they cannot read) and
  `CHATS_SOURCE` (where the code it runs is published, linked from the pages).
  It listens on 127.0.0.1 only; a tunnel or a reverse proxy carries the public address to it.
- `scripts/tick` every ten minutes: backups, trimming logs, alerts (below).

Data is in `~/.chats` (`chats.db` with its `-wal` and `-shm`), backups in `~/.chats-backups`; `CHATS_HOME` and
`CHATS_BACKUPS` move them. `/health` reads the database and answers `{"ok": true, "version": "<commit>"}`, or 503.

The address must not change: sign-in is issued for it, and every connection an app holds is tied to it. A
throwaway `--tunnel` address is for trying it out.

With an https address the server keeps to it: `www.` and visits over plain http (as a tunnel reports them, in
Cloudflare's `CF-Visitor` or a proxy's `X-Forwarded-Proto`) are sent to the address itself, and every answer tells
browsers to stay on https. Pages run only the server's own files and cannot be framed. The MCP door keeps no
sessions and answers in plain JSON: who is calling is read from each request, so a restart ends nothing an app
holds.

On Linux, two systemd units: a service for `chats serve` (`Restart=always`) and a timer for `scripts/tick`. On a
Mac, two LaunchAgents doing the same.

## What tick does

- Runs `scripts/backup` when the newest backup is over a day old, and deletes its backups older than 14 days
  (erasing tells a person 15: a day's margin for a machine that was off).
- Trims `server.log`, `tunnel.log` and `tick.log` in `CHATS_HOME` to their last megabyte once one passes 5 MB.
- With `CHATS_BACKUP_COPY` set: copies the newest backup there once, and tries again on the next run if that
  failed.
- With `CHATS_OPERATOR_NOTIFY` (an ntfy.sh topic URL) and `CHATS_BASE_URL` set: after two failed checks of the
  public `/health` in a row, posts one line there, at most hourly, and one when it answers again; also when a
  backup or its copy fails.

A machine that is off sends nothing, so something elsewhere has to check the public `/health` too: any uptime
checker, or a scheduled job on another machine.

## Backups somewhere else

The backups sit on the same disk as the database, so a lost machine takes them too. `CHATS_BACKUP_COPY` (tick's
environment) sends each new one to an Azure Blob container, as its address with a signature that may only create
(a stored access policy with permission `c`): the server can add a copy and never read, replace or delete one. The
container must delete them itself within the 15 days erasing promises: a lifecycle rule deleting blobs 12 days
after they were written (the rule runs once a day), with blob soft delete and versioning off, since either would
keep what the rule deletes.

## Restoring from a backup

Only when the live file is lost or damaged. Anything written after the backup is lost, including what people
erased since: erase again anyone you learn erased themselves since the backup. Stop the server, move
`~/.chats/chats.db*` aside (a WAL left beside a restored file is replayed into it), copy the backup to
`~/.chats/chats.db`, `chmod 600` it, and start the server. Now and then, check the newest backup opens:

```bash
d=$(mktemp -d) && cp "$(ls -t ~/.chats-backups/chats-*.db | head -1)" "$d/chats.db" &&
  CHATS_HOME=$d uv run chats stats; rm -rf "$d"
```

## Someone who lost every connection

A connection is whoever holds the app it is in. Someone who removed Bridge Chats from their only app, or lost
that app account, has a new, empty account the next time they add it. If it still works in another app of theirs,
that app gives a code (`chats_settings another_app`). If not, nobody can bring their chats back: everything in
them is locked under keys only their connections held (PROTOCOL.md rule 18). `uv run chats code <person id>` still
makes a code for a person id the operator somehow has, sent only to them through a channel they already trust; its
use ends every other connection of theirs, and they start over: their chats stay theirs, and open again only as
someone in each lets them back in.

## Locks and reports

What anyone writes is stored locked (`chats/vault.py`): the database and its backups open nothing without a
member's connection, and the operator holds no key. `chats stats` still gives counts. The move to locks (schema 2)
kept each person's key open in `escrow` until each connection and run link they already had took its own copy at
its next use, for at most two weeks; backups from before it hold what was written then in the clear until they age
out, and the one the deploy takes just before it is the way back, since code from before refuses schema 2.

A member's report is sealed to the operator's report key, made at the first `serve`: its public half is in the
database, its private half in `report.key` beside it, 0600, never in a backup. `chats reports` opens them with it.
Keep that file: without it no report opens.

## A look of its own

`CHATS_THEME` names a directory whose `templates/` are found before the server's own and whose `static/` is served
at `/static`. A theme replaces `base.html` (the frame) and may replace `home.html`; the words that say who can read
what before anyone presses a button stay the server's own.
