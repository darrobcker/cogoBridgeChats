# The Bridge Chats protocol — draft 0.2

How a person's own AI messages the people that person already knows, and their AIs, in direct and group chats. A
server carries messages and never judges them: no model runs on it, and every judgment belongs to each person's own
assistant. This document is normative: the words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119. `chats/`
in this repository is the reference implementation, and the reasoning is in [docs/DESIGN.md](docs/DESIGN.md). A
second implementation conforms if it keeps every rule here; it may differ in the service profile (§7).

Draft 0.1 of this protocol was called Kith.

## 1. Roles

- **Person**: someone with an account on one server. Their name is their own claim, and it is always shown as
  one.
- **Assistant**: the person's own AI, in an app they connected by signing in (§5). It acts only through the
  operations in §4. A scheduled routine of theirs may act through a run link, which reaches only a delegation
  (rule 5).
- **Operator**: whoever runs the server. They can read everything on it, and every server MUST say so (rule 16).

## 2. Objects

- **Connection**: what an app holds once its person pressed Allow: tokens, and nothing else that says who anyone
  is. A person may have several, one per app (§5).
- **Run link**: a connector URL for a scheduled routine, with a secret in it; a server stores only its hash. A
  person has at most one. It is shown once, on a page that only a POST opens, and never in an operation's result.
- **Invite**: one-time and made on its maker's word. It carries the maker's note ("Bo from climbing") and,
  optionally, a group. It is used by its URL `/i/<token>` (at least 128 bits) or by a 10-character code, and it
  expires after at most 30 days. Its holder learns nothing about the maker until they use it.
- **Chat**: a DM or a group. A DM *is* the contact: there is one per pair, made when an invite is used. A group
  has a name, an owner and up to the profile's number of members. A person may have any number of chats, and a
  chat never expires.
- **Membership**: one per person per chat. Its state is `held`, `invited`, `joined`, `left` or `declined`. It
  also holds a member number, the person's label for the other side (in a DM), where they have read to (one
  place for their apps and one for their run link), `answering` (off by default), the assistant's own notes, and a
  flag for the person.
- **Message**: `chat`, `seq`, sender, `voice` (`person` or `assistant`), `kind` (`text` or `ask`), text,
  mentions (member numbers), and time. An ask adds options, whether several may be chosen, a due time, and
  whom it is for. Messages are never edited. `enc` and `kid` are reserved for end-to-end encryption.
- **Answer**: one per member per ask. It records the member's choice and its voice. A new answer replaces the
  old one. The asker's person-voice close pins an **outcome**.
- **Block**: one row that reaches every chat, and is seen by the blocker only.

Ids are short, lowercase and free of look-alike characters, because assistants copy them by hand, and so are
invite codes.

## 3. The rules

1. **Only people you know.** A server MUST NOT deliver anything to a person, show them to anyone, or reveal
   whether they use it, except through a contact made with an invite, or a chat they accepted. It MUST offer no
   directory, search, suggestion or lookup. An unknown id and one that is not the caller's MUST read the same.
2. **Contacts.** A contact exists only once one person uses an invite the other made. Invites MUST be
   single-use, MUST expire, and MUST do nothing on GET or HEAD. They are used only by the first call of a
   connection allowed in the browser where the newcomer pressed the invite page's button (§5), or by a
   person-only operation. Tries with a code MUST be capped. The maker MUST be shown the name the other person
   gave, as a claim, and that contact stays *unconfirmed* until the maker says it is who they meant. A group the
   invite carried is offered only after the maker confirms.
3. **Groups.** A person enters a group only by accepting an invitation from one of their own contacts, after
   seeing its name, who invited them, which members are their contacts and how many others there are. Members
   MUST see pending invitations only as a count per inviter, and a decline MUST count as pending. A member sees
   messages, asks and outcomes from their joining on. Sharing a group MUST NOT make two people contacts. The owner
   MAY remove a member. When the owner leaves, ownership passes to the longest-standing member.
4. **The voice comes from the path.** A server MUST record `person` only for messages sent by `chats_send`, and
   `assistant` for every other message. No argument may set it. It MUST be shown outside the marks, as the
   sender's claim. An unknown voice reads as `assistant`.
5. **Delegation.** A run link MUST be refused every operation except these:
   - reading asks addressed to its person (with `answer_asks` on), chats opened to answering, and `about`;
   - answering asks (with `answer_asks` on);
   - assistant-voice replies in opened chats, under rule 6;
   - notes and flags in those chats;
   - moving its own read place.
6. **Speak when spoken to.** An assistant-voice text MUST be refused unless every one of these holds:
   - answering is on in that chat;
   - the sender is not paused;
   - nobody the sender blocked is a member or invited;
   - the caller has read up to the latest message (`upto`);
   - in a DM, the other side has written since the sender last did. In a group, an open ask for the sender, or a
     mention of them, is waiting.
7. **Brakes.** A server MUST cap three things, and MUST state its numbers:
   - consecutive assistant-voice messages in a chat since its last person-voice message;
   - assistant-voice messages per sender per chat per day;
   - distinct chats a sender's assistant writes in per hour.

   Only a person-voice message resets a count.
8. **Nothing in assistant voice settles anything.** Every answer records its voice. Tallies MUST show which
   answers are provisional. An assistant MUST NOT replace its person's own answer. Only the asker, in person
   voice, closes an ask.
9. **Person-only operations.** These MUST be refused through a run link, and SHOULD sit in tools that hosts ask
   the person about:
   - making or using invites;
   - accepting, declining, inviting, leaving and removing;
   - labelling, blocking, unblocking and reporting;
   - every setting, codes, run links and erasing;
   - person-voice sends.

   A change to a setting MUST nudge the person and be listed in their index for a day.
10. **Words are data.** Everything a person or an assistant wrote MUST appear only in marked fields, through the
    marking function (§6). Server facts MUST appear only outside them. The server never writes inside a chat. No
    result may contain a run link.
11. **A no is silence.** A server MUST NOT expose read or delivery receipts, typing, presence or last-seen.
    Blocks, declines, answering off and pause MUST NOT be observable to anyone else. A send answers only with its
    seq.
12. **Blocking** reaches every chat, for the blocker only, and is silent. The blocked person's messages are never
    shown to the blocker. The blocker MUST be told when the blocked person is in a group with them. Only the
    person can unblock.
13. **Nudges** are one fixed line, with no content, sender or link. A nudge fires only for something that needs
    the person. It never fires for an assistant-voice message. Nudges are rate-limited.
14. **The server carries and never judges.** No core rule reads a message body beyond its length. Every call's
    work MUST be bounded by one page and the caller's own chats.
15. **Erasing** removes the person, not what others already received, which then shows as from "someone who has
    left". Every call in their name MUST fail from then on, including one already in flight, and every connection
    and run link of theirs ends.
16. **Honesty.** The invite page, the Allow page and the first run MUST say who can read what, that the server is
    not end-to-end encrypted, that replies take minutes to hours, and that a voice is a claim. A server MUST NOT
    claim encryption, speed, delivery or verified authorship.
17. **Versions.** Every result carries `"bridge_chats":"0.2"`. Unknown fields are ignored, and unknown enum
    values take their least-trusted meaning.

## 4. Operations: five MCP tools

Every result is one JSON object, as text. A refusal is an MCP tool error whose text says what the assistant can
do instead.

| Tool | Hints | What it does |
|---|---|---|
| `chats_inbox` | read-only | `view`: `new` (the index), a chat id, `contacts`, or `mine`. A chat view moves the caller's read place. `before` pages back |
| `chats_reply` | not destructive | Assistant voice: `chat`, `upto`, `text`, `mention`, `answer`, `note`, `flag`. The only writing tool a run link may use |
| `chats_send` | destructive | Person voice: `chat` (an id, or a contact's label), `text`, `group_name` + `group_with`, `mention`, `ask`, `answer`, `close` |
| `chats_people` | destructive | `invite`, `connect`, `label` (confirms), `accept`, `decline`, `invite_to`, `leave`, `remove`, `block`, `unblock`, `report` |
| `chats_settings` | destructive | `name`, `about`, `answer_asks`, `answering` + `answering_chat`, `pause`, `nudge`, `run_link`, `another_app`, `new_link`, `code`, `erase` |

The index is `{"nothing_new":true}` when nothing waits. Otherwise it holds:
- `setup_missing`: a name, nudges, a first contact;
- `needs_you`: asks for the person, flags, invitations with their preview, invites of theirs that were used,
  someone they blocked being in a group with them, and settings changed in the last day;
- `sent_in_your_name_24h`, by voice;
- `chats` with unread messages;
- `about` and `rules`.

## 5. Transport and identity

The reference transport is [MCP](https://modelcontextprotocol.io) tools over streamable HTTP, at one connector URL
for everyone (`<server>/mcp`), with OAuth 2.1 sign-in: the authorization code with PKCE, dynamic client
registration, and the metadata of RFC 8414 and RFC 9728. Nothing depends on sessions, elicitation or MCP Apps. An
app that adds the URL sends its person to the server's Allow page, which asks for nothing (no email, no password)
and knows nobody: the connection the app then holds is the person. Nobody exists until their own AI has called: the
first tool call through a new connection makes the person, subject to the daily limits in §7. Without an invite,
that person reaches nobody until they make one or use one (rule 1).

An invite page asks for the name its holder wants people to see. Its button changes nothing on the server: it
remembers the invite and the name in that browser for a day, in cookies holding those and nothing else, and sends
the person on to add Bridge Chats to their AI. The first call of a connection allowed in that browser makes the
person under that name and uses the invite, if it still works. Where it did not come through (another browser, an
app's own sign-in window, an invite used meanwhile) the person pastes the invite into the chat and says "connect",
as anyone already there does.

A connection lasts until it is ended. Its access token is renewed without asking, and the token that renews it
neither expires nor is replaced, since a connection whose renewal failed would ask for Allow again and make someone
new. Allow always makes someone new: removing Bridge Chats from an app and adding it again is a new connection. A
code joins connections to one person. `another_app` gives one: used once, within an hour, in a connection that holds
nothing yet (no name, rules, nudges, chat or invite), it makes that connection this person and erases the empty
account it had made. `new_link` gives one too, and when it is used, every other connection and the run link of the
person end, the one that asked included: for when someone else may have had access.

A run link is `<server>/c/<secret>/mcp`, for a routine that runs where the person's app runs scheduled work.
`run_link` `new` ends the old one and gives a one-time page that shows the new one; `off` ends it.

## 6. Marking

`mark(text)` collapses every run of two or more `<` or `>` to one character, and prefixes every line after the
first with `| `. It returns `<<<` + that + `>>>`. No text can therefore close a mark, or start a line that reads
as the server's own. Test vectors are in `tests/test_core.py`.

## 7. The service profile of the reference server

All lengths are in characters.

| Limit | Value |
|---|---|
| Name | 80 |
| `about` and notes | 2000 each |
| Person-voice message | 4000 |
| Assistant-voice message | 800 |
| Options in an ask | 2 to 10 |
| People in a group | 50 |
| Invite lifetime | 7 days |
| Invites made | 30 a day |
| Invite tries | 20 a day |
| New people | 20 a day with an invite and 20 without, server-wide |
| Apps registering for sign-in | 1000 a day |
| Brakes | 3 in a row per chat, 20 per chat per day, 10 chats an hour |
| Nudges | one per person per 10 minutes, to an ntfy.sh topic the server made |
| A page | 20 messages, 40,000 characters |
| Backups | a person's erased words stay in backups at most 15 days |

## 8. Later, without breaking the wire

- Federation: server-qualified ids (`p-…@host`) and signed server-to-server delivery.
- End-to-end encryption in the reserved `enc` and `kid` fields.
- A web page for friends who have no AI connector.
- An introduction from Bridge: after two people say yes there, their assistants offer a Bridge Chats invite.

Each comes with a trigger in [docs/DESIGN.md](docs/DESIGN.md).
