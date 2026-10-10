---
name: rag-status
description: Show whether the research assistant is on, when the index was built, how many files it covers, and how many were added since
---

Run exactly this command:

    "engine/bin/hooks.cmd" status

Report its output in plain words: the state, the index date, how many files are readable and how many are not (and why), and how many files have been added to the folder since the index was built. If files were added, say that /rag-refresh makes them searchable. Report the command's output and nothing from memory.
