# Initialize

You are Claude Code. Someone opened a terminal in this folder (or double-clicked
`Start research assistant.cmd`), started you, and typed **initialize**. They may never
have used a terminal before. Do the whole setup yourself, explain it in plain English as
you go, and stop and ask when something is genuinely missing from their machine.

This document is the whole procedure. You do not need anything outside this
folder, and you must not go looking for any.

## What this folder is

```
<this folder>          the person's research documents, in whatever subfolders they keep
  engine\             the tool. Never edit anything under engine\bin.
  CLAUDE.md            what you read at startup; it routes by whether the tool is set up
  .claude\            the hooks and the /rag-on, /rag-off, /ask skills (already in place)
```

The documents folder is **this folder**. Call it `<docs>` below; in every command it is
written `.` (the current directory). Do not ask which folder to index, and do not index
anything else. `engine\` is left out of the index automatically.

## Step 0 - tell them what is about to happen

Before you run anything, say roughly this, in your own words:

> I'm going to set up a Python environment inside `engine`, then read every
> document in this folder once and build a searchable index of it. The reading pass
> is the slow part: about five minutes per thousand documents, and
> it uses most of the CPU while it runs. Nothing is written to this folder outside
> `engine`, and none of your documents is modified. That includes the 130 MB language
> model the first run downloads: it is kept in `engine\models`, not in your user profile. Say
> stop at any point and nothing is lost -- rerunning resumes.

Then go, without waiting for permission. They already said initialize.

## Red text is not an error

PowerShell paints everything a program writes to stderr red, and prints a block
headed `NativeCommandError` around it. Progress messages, pip's "a new release of
pip is available" notice and the index pass's *building page-range cache,
one-time* line all arrive that way, and so does the first build's model download:
a `Fetching 5 files` progress bar, a warning about unauthenticated requests to the
HF Hub (no `HF_TOKEN` is needed), and a `huggingface_hub` warning that the machine
does not support symlinks. It looks exactly like a crash and it is not one.

Judge every command in this document by its exit code, never by the colour. When
a red block appears, say what it actually is before the person watching decides
the tool is broken.

## Step 1 - check Python

```
python --version
```

Needs **3.11 or newer**. If the command is not found, or the version is older,
stop and tell them: install Python 3.11+ from python.org, tick "Add python.exe to
PATH" in the installer, close the terminal, open a new one, and run you again.
Do not try to install Python for them.

## Step 2 - build the environment

```
python -m venv engine\.venv
engine\.venv\Scripts\python.exe -m pip install --no-cache-dir -r engine\requirements-portable.txt
```

The pip step downloads roughly 200 MB and takes a few minutes on a normal
connection. It needs internet. If it fails with a network or proxy error, show
them the last few lines and ask whether they are behind a corporate proxy.

## Step 3 - check the prerequisites

```
engine\.venv\Scripts\python.exe engine\bin\setup_folder.py doctor
```

One line per prerequisite. Read them all before reacting.

- **pdftotext FAIL** -- this is the one that actually stops people. The tool
  needs `pdftotext.exe`, which ships inside Git for Windows. Tell them to install
  Git for Windows from git-scm.com, accepting every default, then close the
  terminal, open a new one in this folder and run you again. If they already have
  poppler somewhere, you can instead set `PDFTOTEXT_EXE` to its `pdftotext.exe`
  and rerun `doctor`.
- **claude_exe FAIL** -- only matters for the `ask` shortcut in step 6. If they
  are talking to you right now, Claude Code is installed; set `CLAUDE_CMD` to the
  `claude.exe` you can find, or just skip it and use the interactive route.
- **embedding_model ... NOT found** -- not a failure. It says the first index
  build will download a 130 MB model into `engine\models`, so that build needs internet.
- **free_disk FAIL** -- they need about 1 GB per 2,000 documents. Say the number.
- **ocr ... not found** -- not a failure. It means a scanned page with no text
  layer is reported as `image_only_no_text` rather than read. OCR is off unless
  `PCR_OCR=1` is set and both `tesseract` and `pdftoppm` are on PATH. If the folder
  turns out to hold scans, say which files were unreadable; do not install OCR for them.

Do not go on to step 4 while `pdftotext` is failing. Everything will index as
`failed_parser` and the result will look like the tool is broken.

## Step 4 - read the documents

Pick the worker count from their machine rather than taking the default:

```
python -c "import os; print(max(2, min(12, (os.cpu_count() or 4) - 2)))"
```

Then, with `<N>` filled in:

```
engine\.venv\Scripts\python.exe engine\bin\setup_folder.py install --folder . --artefacts engine\artefacts --seed-env --workers <N>
```

This is the long one. Tell them the estimate before you start it and let it run.

**Things you will see that are not errors.** Say so plainly if they appear:

- Red `NativeCommandError` blocks, as above. The index pass prints several.
- Files counted as `failed_parser` or `excluded`. A handful in a large folder is
  normal -- scanned images with no text layer, zero-byte files, archives. The
  final report gives the counts. Only worry if most of the folder failed, which
  almost always means `pdftotext` is missing.
- An install that stops halfway. Rerun the identical command; it resumes.
- `REFUSED <folder> is already set up`, exit code 3. That is not a failed
  install, it is a finished one: the last thing install does is write the `READY`
  marker under `engine\artefacts`. The folder is ready. Go to step 5 and check it,
  and do not run `uninstall` to "clean up" first -- that throws the index away.
- `ADOPTING index <old> -> <new>`. The folder was moved or renamed since the index
  was built. Nothing is broken; the index is repointed and the install continues.

When it finishes, tell them how many documents were read, how many pages that
came to, how long it took, and how many files were skipped and why. Two fields in
that report are to be quoted rather than summarised: `failed_by={...}` names each
failure class with its count, and `pages_with_no_readable_text=N` counts pages that
carry no text layer at all. If either is non-zero, say the number out loud. A skipped
file the person never hears about is a hole in every answer they get afterwards.

## Step 5 - check the build

```
engine\.venv\Scripts\python.exe engine\bin\c_selftest.py --db engine\artefacts\pages.db --shelf engine\artefacts\shelf\shelf.db --expect-pages 0
```

`--expect-pages 0` means "do not compare this folder against a fixed reference
count" and must stay. Report the pass count as one line. If any check fails, show
the failing lines verbatim and stop -- do not try to repair the index.

## Step 6 - hand it over, and switch yourself over

From this point the research assistant is **ON** in this very chat: `engine\artefacts\READY`
exists, so for every question about the documents you read `engine/ANSWER.md` and follow
it exactly, you begin each reply with `(research assistant on)`, and you may not create
or change any file in this folder outside `engine\` (the hooks refuse it). No new
terminal and no restart is needed.

Say, in your own words:

> It's ready, and it's on. Ask me anything about these documents right here, in plain
> English -- "how has spending on X changed over the last ten years?" An answer usually
> takes two to four minutes, because I open the real pages before I reply. Every number
> comes with a `Sources` line naming the file and page it came from, and when the folder
> does not contain the answer, I say so instead of guessing.
>
> While it is on I will not change any file in this folder. `/rag-off` switches it off
> and I behave like ordinary Claude Code again; `/rag-on` switches it back; `/rag-status`
> shows what the index covers; `/rag-refresh` picks up documents you add later; `/ask`
> sends one question through it even when it is off. Next time, just double-click
> `Start research assistant.cmd` or open Claude Code in this folder.
>
> One thing to know: the answers are only as good as this folder. A `Sources`
> line is a claim you can check in seconds -- open the file at that page. Do
> check a few early on. I also check myself: before handing over an answer I re-open
> every page I cited and confirm the quoted line is really there. Treat that as a floor,
> not a guarantee -- it cannot catch a figure reported as missing when the folder does
> have it, so your own spot checks still matter.

If they would rather not use the chat, the same question can be asked with:

```
engine\.venv\Scripts\python.exe engine\bin\setup_folder.py ask --folder . "<their question>"
```

## Rules for you while doing this

- **Do not edit anything under `engine\bin`.** If a step fails, report it. A
  patched engine is worse than a broken one, because nobody knows what it is any
  more.
- **Do not index anything outside this folder**, and do not read documents to
  answer questions during setup. Setup is setup.
- **Do not claim a step worked because it printed something.** Check exit codes.
- **Do not hide a failure in a summary.** If step 4 skipped 300 files, say 300.
- If the same command fails twice for the same reason, stop and ask them. Do not
  try a third variation.

## To remove it

```
engine\.venv\Scripts\python.exe engine\bin\setup_folder.py uninstall --folder . --purge --artefacts engine\artefacts
```

That deletes the index. Deleting `engine\`, `.claude\`, `CLAUDE.md`,
`README_FIRST.md` and `Start research assistant.cmd` removes everything else;
their documents are untouched.
