# Read me first

This folder now contains your documents plus a small tool (`engine\`) that lets Claude Code
answer questions about them with the page every answer came from.

## Start

Double-click **Start research assistant.cmd**. The first time, it sets itself up: it reads
every document once (about 3 to 6 minutes per 1,000 files) and then says it is ready.
Every later time it just opens Claude Code here, ready for questions.

If you prefer a terminal: open one in this folder, run `claude --model claude-opus-5`, and
type `initialize` the first time.

You need Python 3.11 or newer (python.org, tick "Add python.exe to PATH"), Git for Windows
(git-scm.com, defaults), and Claude Code. The first setup also downloads a 130 MB model once.

## Ask

Ask in plain English: "How has spending on X changed over the last ten years?" An answer
takes one to three minutes because it opens real pages before replying. Every number ends
with a `Sources` line: the file, the page and the exact line. Open the file at that page
to check; do that a few times early on.

Every reply starts with `(research assistant on)` so you can see the tool is in use.

## On and off

- `/rag-off` switches the tool off. Claude Code then behaves as it normally does and can
  change files here if you ask it to.
- `/rag-on` switches it back on. While on, nothing in this folder can be changed by Claude.
- `/rag-status` shows when the index was built and how many files it covers.
- `/rag-refresh` makes documents you added since searchable.
- `/ask <question>` sends one question through the tool even when it is off.

## What it does not do

It cannot read scanned PDFs that have no text layer; it tells you which files it could not
read. It only knows documents present at the last initialize or refresh. It can say
"not in the folder" when the figure is there, so spot checks still matter.

## Remove it

Delete `engine\`, `.claude\`, `CLAUDE.md`, `README_FIRST.md` and `Start research
assistant.cmd`. Your documents are untouched; the tool never writes outside `engine\`.
