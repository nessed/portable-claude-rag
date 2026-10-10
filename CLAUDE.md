# This folder

Your research documents, plus a tool in `engine/` that answers questions about them with
the page each answer came from. A line at the top of every turn says which of three
states applies. Read it, and read this file, before anything else.

## Not set up yet (`engine/artefacts/READY` does not exist)

When the user says **initialize**, read `engine/INITIALIZE.md` and do exactly what it
says, start to finish. It is written for you and it is the only instruction you need. A
person doing it by hand uses `engine/INSTALL.md`. Do not answer questions about the
documents before the setup has finished; say that `initialize` comes first.

## Set up and ON (`engine/artefacts/READY` exists, `engine/OFF` does not)

The folder is ready: do not run the setup again. For every question about the documents,
read `engine/ANSWER.md` and follow it exactly. Never use Grep, Glob or Read on the
documents instead of it; they miss PDFs and spreadsheets. Begin each reply with the line
`(research assistant on)` so the person can see the tool is in use.

While ON you may not create, change, move or delete any file in this folder outside
`engine/`; the hooks refuse it. If the user wants a file changed, tell them to say
`/rag-off` first and ask again.

## OFF (`engine/OFF` exists)

Plain Claude Code. The index is not consulted and files may be changed if the user asks
and approves. `/rag-on` switches the tool back on; `/ask <question>` sends one question
through it anyway.

## Skills

`/rag-on`, `/rag-off`, `/rag-status`, `/rag-refresh` (index documents added since),
`/ask <question>`.
