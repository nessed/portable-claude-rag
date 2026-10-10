---
name: rag-on
description: Switch the research assistant on: questions are answered from the document index with page citations, and files in this folder are read-only
---

Run exactly this command and show its output:

    "engine/bin/hooks.cmd" on

Then say in one line that the research assistant is ON: every question about the documents is answered by opening the real pages, every figure comes with its page, and nothing in this folder can be changed until /rag-off. From now on follow engine/ANSWER.md for document questions and begin each reply with `(research assistant on)`. Say only what the command printed about the folder and the index; do not add anything you remember from earlier in the chat about files being added or removed.
