# portable-claude-rag

A drop-in research assistant for Claude Code. Put it in a folder of documents, and Claude
Code answers questions about them with the file, the page and the exact line behind every
number. When the folder does not contain the answer, it says so instead of guessing.

It was built for a folder of a few thousand government statistical PDFs, where the same
tables are republished every year and a vague question matches hundreds of near-identical
editions. Windows only for now.

## What makes it different

It is not chunk-and-embed retrieval. Nothing is stuffed into the prompt. Claude Code drives a
small command-line tool the way a research assistant uses a library:

1. **Find the publication.** One catalogue card per document; yearly editions of the same
   publication are grouped. Claude sees forty candidates and picks.
2. **Find the page** inside that one document, with table captions ranked first.
3. **Open the page.** The tool records that it was opened.
4. **Copy the line** into a note. The tool refuses a note whose page was not opened or whose
   words are not on that page.
5. **Answer only from the notes**, ending in a Sources block.

Two hooks then read every finished answer. If it cites a page that was never opened, or
states a number that no copied line contains, the answer is sent back once before the person
sees it. A third hook refuses any file change outside `engine/` while the assistant is on, so
your documents are read-only to it.

## Requirements

- Windows 10 or 11
- Python 3.11 or newer, with "Add python.exe to PATH" ticked
- Git for Windows (the tool uses the `pdftotext` that ships with it)
- [Claude Code](https://claude.com/claude-code), which needs a paid Claude plan
- About 1 GB of free disk per 2,000 documents, and internet for the first setup

## Install

1. Download this repository as a zip (Code, then Download ZIP) and extract its contents
   **into your documents folder**, so that `engine`, `.claude`, `CLAUDE.md`,
   `README_FIRST.md` and `Start research assistant.cmd` sit next to your documents. You can
   delete this `README.md` from the folder; otherwise it is indexed as one more document.
2. Double-click `Start research assistant.cmd`. Answer yes to the trust prompt and to the
   few setup commands Claude Code asks to run.
3. Wait. It reads every document once, about five minutes per thousand files, then says it
   is ready. It lists what it could not read.

Prefer a terminal? Open one in the folder, run `claude --model claude-opus-5`, and type
`initialize`. `engine/INSTALL.md` has the by-hand procedure.

Everything the tool creates stays inside `engine/`, including the embedding model it
downloads. To remove it, delete the five items from step 1.

## Use

Ask in plain English:

> How has Punjab's development spending changed since 2015?

An answer takes two to four minutes, because real pages are opened first. It looks like this:

```
(research assistant on)

Car and light commercial vehicle production was 208.4 thousand units in 2015-16 and
226.9 thousand units in 2016-17, a rise of roughly nine percent.

Sources
- 208.4 thousand units (2015-16) and 226.9 thousand units (2016-17) | Sources/Federal/Economic Survey/2016-17/Pakistan Economic Survey 2016-17.pdf | p41 | "Automobile Production (Cars and LCVs) shifted from 208.4 thousand units in 2015-16 to 226.9 thousand"
```

The page number is the one your PDF viewer shows.

| Command | What it does |
|---|---|
| `/rag-off` | Plain Claude Code. No index, and files can be changed if you approve. |
| `/rag-on` | Back on. Nothing in the folder can be changed by Claude. |
| `/rag-status` | State, index date, how many files are readable, how many were added since. |
| `/rag-refresh` | Index documents added since the last setup. |
| `/ask <question>` | One question through the assistant, even while it is off. |

## What is in the box

| Path | Role |
|---|---|
| `CLAUDE.md` | Router Claude Code reads at start: not set up, on, or off. |
| `.claude/settings.json` | Four hooks, all via `engine/bin/hooks.cmd`, plus a short allow list. |
| `.claude/skills/` | The five slash commands. |
| `engine/INITIALIZE.md` | The setup procedure, written for Claude Code. |
| `engine/ANSWER.md` | The answering procedure. |
| `engine/bin/index_build.py` | Page-level SQLite FTS5 index. Every file gets a status; nothing is dropped silently. |
| `engine/bin/c_shelf_build.py` | The shelf: one card per document, grouped into publications and editions, embedded with `bge-small-en-v1.5` on CPU. |
| `engine/bin/c_shelf.py` | The tool Claude drives: `find have inside tables table series open note notes verify exact copies`. |
| `engine/bin/c_mode.py` | The on/off switch, the per-turn state line, the file guard. |
| `engine/bin/c_stop_guard.py`, `c_verify_guard.py` | The two checks on finished answers. |
| `engine/PACKAGE_MANIFEST.json` | Source commit and a sha256 for every file. |

## How well it works

Tested on a synthetic 2,002-file corpus shaped like the real target, with traps planted in
it (drafts marked do-not-cite, wrong-year headers, tables that start later than the series).
A separate scorer opened every cited page with `pdftotext`.

| Run | Result |
|---|---|
| 22 questions and follow-ups, asked by an agent that never saw the answer key | 13 fully correct, 6 correct with a minor slip, 3 failed. 0 invented numbers. 163 of 163 citations found on their pages. |
| The failed items, retested after fixes | 6 fully correct, 1 minor slip, 0 failed. |
| Smoke test of this build: fresh install, empty model cache, 3 questions | All correct. |

Typical cost on Claude Opus is two to five US dollars per answer at API prices.

## Limits

- Scanned PDFs with no text layer, encrypted PDFs and Stata files are reported, not read.
  OCR is off by default.
- It knows only the documents present at the last setup or `/rag-refresh`.
- It can still say something is not in the folder when it is. A procedure rule reduces this,
  and no automatic check can catch it. Spot-check anything that matters.
- Tested with Claude Opus on one Windows machine, on generated documents. A second machine
  and a real-world folder are untested.
- Non-PDF files cite `p0`, since they have no pages.

## Background

This package is the outcome of a longer measured experiment on whether Claude Code can
answer vague questions from a large research folder. The full record, including everything
that did not work, is in
[cli-largeFolder-index](https://github.com/nessed/cli-largeFolder-index).
