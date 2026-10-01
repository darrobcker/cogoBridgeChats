# Why Bridge Chats is shaped this way

Bridge Chats is the half of Bridge for the people you already know: Bridge finds someone you would have loved to
know, anonymous until you both say yes; Bridge Chats is how your AI keeps up with the people you know, and theirs.
The two share no code and no database. Their privacy rules differ (anonymous strangers in one, invite-only friends in
the other), so one set of instructions covering both would blur them.

## What holds even if an assistant is fully prompt-injected (each pinned by a test)

- It cannot reach anyone new. Invites, connecting, accepting and every setting are person-only, and refused
  through a run link.
- It cannot speak as its person. The voice comes from the tool, and `chats_send` is the only person-voice path.
- It cannot loop with another assistant, or spread a worm. The brakes allow 3 in a row per chat and 10 chats an
  hour. Only a person's own message resets them.
- It cannot read a chat its person did not open to it, through a run link.
- It cannot settle a plan. Its answers are provisional, and only the asker closes an ask.
- A stranger's name never appears in anything it is shown, and what anyone wrote reaches it only inside marks.

## What does not hold, said plainly

- **The operator can read everything.** There is no end-to-end encryption.
- **Whoever uses a person's AI account is that person here.** A connection is the app that holds it. `mine` shows
  what was done in their name, and through which door; `new_link` gives a code whose use ends every other
  connection and the run link.
- **A copied run link reaches the delegation.** It can read the chats opened to answering, answer asks
  provisionally, and reply where answering is on, under the brakes. It cannot reach anyone new, speak as the
  person, or change anything. `run_link off` ends it, and a new one ends the old.
- **Names are claims.** A contact made by an invite shows as "not yet confirmed" until its maker says it is who
  they meant. Only then is a group the invite carried offered.
- **Where a person turns answering on, their assistant can paraphrase what it knows.** It is off by default, one
  chat at a time, and the instructions say never to write alone about money, addresses, health, children, work
  or landlords.
- **Nudges go through the public ntfy.sh.** The topic is random and holds one fixed line, but whoever learns the
  topic sees when something is waiting.
- **Background work runs only where the person's app runs scheduled routines,** through a run link. Everywhere
  else, things happen when the person next opens their AI.
- **ChatGPT works only in a computer's browser.** A phone-only ChatGPT user cannot take part.

## Decisions, and why

- **Sign-in, not links.** Draft 0.1 gave each person a connector URL with a secret in it, and a copied URL was the
  whole account. Sign-in (OAuth, as Bridge has it) leaves nothing to copy. Only the run link stays a URL, because a
  routine has no browser to press Allow in, and it reaches only the delegation.
- **Anyone may sign in; nobody is reachable without an invite.** Someone has to be first, and the server offers no
  directory, so an account with no contact can reach nobody. New people are capped a day, with an invite and
  without, separately, so the uninvited cannot use up a day's room for invited friends.
- **The invite page makes nobody.** It remembers the invite in the browser, and the connection's first call uses
  it. A page preview, a reload or an abandoned sign-in leaves no account behind.
- **Codes join apps.** The same person in Claude and ChatGPT is one account through a one-use code from one chat
  to the other, which lands only on an account that holds nothing, so nothing is lost.
- **The person-only tools are marked destructive,** so hosts ask before each (rule 9), even though most are not
  destructive in the everyday sense.

## The open objections from the design's challenge round

| Objection | Now |
|---|---|
| An ex invited by a mutual friend sees earlier plans | Fixed: a joiner sees nothing from before they joined, and a blocker is told |
| A batch paste lets the wrong person claim a link, and inherit your label | Fixed: unconfirmed until the maker confirms; the group waits |
| A copied link gives silent access | Fixed for apps by sign-in; a copied run link reaches only the delegation |
| "While you're away" overpromises | The pages and instructions say minutes to hours, and routines only where the app runs them |
| Phone-only ChatGPT users cannot join | Said on the invite page; not fixable on today's hosts |
| A plan closed on a provisional answer | The tally marks provisional answers; only a person closes |

## Later, each with its trigger

- **Federation**: when a second operator wants their people to reach this server's.
- **End-to-end encryption**: when a member needs the operator unable to read.
- **A guest page for friends with no AI connector**: when over a third of the invites go unused.
- **An introduction from Bridge**: when both run on one host, as one paragraph in each server's instructions, so
  the servers never link to each other.
