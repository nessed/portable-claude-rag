# This folder

A folder of research documents, and a tool that can answer questions about them
with the page it got each answer from.

**Nothing is set up yet.** To set it up, tell Claude Code:

> initialize

and follow along. The procedure is `engine/INITIALIZE.md` -- read that file and
do exactly what it says, start to finish, before anything else. It is written for
you, it expects the person watching to be new to all of this, and it is the only
instruction you need.

If you are a person reading this and would rather do it by hand, `engine/INSTALL.md`
is the same procedure written for you.

## If it is already set up

`engine/artefacts` exists once the setup has run. When it does, do not run the
setup again -- the folder is ready, and questions are asked from the documents
folder, which has its own `CLAUDE.md` explaining how to answer from it:

    cd <the documents folder>
    claude --model claude-opus-5
