# Acceptance test — scorecard

Date of the run: ______________    Started: ______    Finished: ______

## The rule

> If you need knowledge that isn't in this folder's own documents to make it work,
> that is a usability bug — write it down.

The folder's own documents are `CLAUDE.md`, `engine\INITIALIZE.md` and
`engine\INSTALL.md`. Nothing else counts: not the repo, not a past session, not
anything you happen to remember.

Fill this in as you go, not afterwards.

---

## Which route

There are two. Do **Route A**, because it is the one a stranger will take.

- **Route A — say "initialize".** Open a terminal in this folder, start Claude
  Code, type `initialize`, and do nothing else except answer its questions and
  approve its commands. Score Part 1A.
- **Route B — by hand.** Follow `engine\INSTALL.md` yourself, command by
  command. Score Part 1B. Only worth doing if Route A fails, or on a second
  machine.

---

## Part 1A — the "initialize" route

| | |
|---|---|
| time from typing `initialize` to "it's ready" | |
| how many times you had to type anything other than approving a command | |
| how many commands it asked permission for | |
| did it tell you what was about to happen, before the long step? | |
| did its time estimate match what actually happened? | |
| did anything look like a crash that wasn't? what? | |
| did it report the skipped-file counts honestly, or bury them? | |
| anything it did that you did not expect | |

**Every time you had to know something the folder did not tell you** — write it
down here *before* you go and find out:

<br><br><br><br><br>

---

## Part 1B — by hand (only if you do Route B)

One row per section of `engine\INSTALL.md`, in the order that document prints them.

### "What you need"

| started | finished | worked as written? | what I had to know that the document did not say | what confused me |
|---|---|---|---|---|
|  |  |  |  |  |

### "Install"

| started | finished | worked as written? | what I had to know that the document did not say | what confused me |
|---|---|---|---|---|
|  |  |  |  |  |

### "Index a folder"

| started | finished | worked as written? | what I had to know that the document did not say | what confused me |
|---|---|---|---|---|
|  |  |  |  |  |

### "Ask it something"

| started | finished | worked as written? | what I had to know that the document did not say | what confused me |
|---|---|---|---|---|
|  |  |  |  |  |

### "Check the build"

| started | finished | worked as written? | what I had to know that the document did not say | what confused me |
|---|---|---|---|---|
|  |  |  |  |  |

---

## Part 2 — the three questions

A fresh Claude Code session per question, started in the `research` folder.

### Question 1 — a trajectory

> How has federal development spending changed over the past ten years?

| | |
|---|---|
| time to first answer | |
| did every number come with a `Sources` line? | |
| you pick one citation and open that file at that page — was it there? | |
| how many permission prompts | |
| what I would tell sir before he sees this | |

### Question 2 — one specific year

> What was the fiscal deficit in 2019-20?

| | |
|---|---|
| time to first answer | |
| did every number come with a `Sources` line? | |
| you pick one citation and open that file at that page — was it there? | |
| how many permission prompts | |
| what I would tell sir before he sees this | |

### Question 3 — something that should not be there

> How much was spent on flood reconstruction after the 2022 floods?

| | |
|---|---|
| time to first answer | |
| did it say "not here", or did it produce a number anyway? | |
| if it produced a number, open the page it cites — does the page say that? | |
| how many permission prompts | |
| what I would tell sir before he sees this | |

---

## Part 3 — remove it

Only when everything above is done. Ask Claude Code to remove it, or run the
uninstall command from `engine\INSTALL.md`.

| worked as written? | what was left behind in `research` that shouldn't be | what confused me |
|---|---|---|
|  |  |  |

---

## The one thing I would fix first

<br><br><br><br><br>
