# Initialize

You are Claude Code. Someone opened a terminal in this folder, started you, and
typed **initialize**. They may never have used a terminal before. Do the whole
setup yourself, explain it in plain English as you go, and stop and ask when
something is genuinely missing from their machine.

This document is the whole procedure. You do not need anything outside this
folder, and you must not go looking for any.

## What this folder is

```
<this folder>
  engine\          the tool. Never edit anything under engine\bin.
  <documents>\     the folder of research documents. It is the one that is not `engine`.
  INSTALL.md          the same procedure written for a person doing it by hand.
```

Find the documents folder first: list this directory, and take the one directory
that is not `engine` and not a dot-folder. If there is more than one candidate,
ask which one. Call it `<docs>` below and use its real name in every command.

## Step 0 - tell them what is about to happen

Before you run anything, say roughly this, in your own words:

> I'm going to set up a Python environment inside `engine`, then read every
> document in `<docs>` once and build a searchable index of it. The reading pass
> is the slow part: roughly three to six minutes per thousand documents, and
> it uses most of the CPU while it runs. Nothing outside this folder is
> touched, and nothing in `<docs>` is modified except two small files the
> tool owns. Say stop at any point and nothing is lost -- rerunning resumes.

Then go, without waiting for permission. They already said initialize.

## Red text is not an error

PowerShell paints everything a program writes to stderr red, and prints a block
headed `NativeCommandError` around it. Progress messages, pip's "a new release of
pip is available" notice and the index pass's *building page-range cache,
one-time* line all arrive that way. It looks exactly like a crash and it is not
one.

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
engine\.venv\Scripts\python.exe -m pip install -r engine\requirements-portable.txt
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
  build will download a 130 MB model, so that build needs internet.
- **free_disk FAIL** -- they need about 1 GB per 2,000 documents. Say the number.

Do not go on to step 4 while `pdftotext` is failing. Everything will index as
`failed_parser` and the result will look like the tool is broken.

## Step 4 - read the documents

Pick the worker count from their machine rather than taking the default:

```
python -c "import os; print(max(2, min(12, (os.cpu_count() or 4) - 2)))"
```

Then, with `<docs>` and `<N>` filled in:

```
engine\.venv\Scripts\python.exe engine\bin\setup_folder.py install --folder <docs> --artefacts engine\artefacts --seed-env --workers <N>
```

This is the long one. Tell them the estimate before you start it and let it run.

**Things you will see that are not errors.** Say so plainly if they appear:

- Red `NativeCommandError` blocks, as above. The index pass prints several.
- Files counted as `failed_parser` or `excluded`. A handful in a large folder is
  normal -- scanned images with no text layer, zero-byte files, archives. The
  final report gives the counts. Only worry if most of the folder failed, which
  almost always means `pdftotext` is missing.
- An install that stops halfway. Rerun the identical command; it resumes.
- `REFUSED <folder> already has a CLAUDE.md`, exit code 3. That is not a
  failed install, it is a finished one: the last thing install does is write
  that file. The folder is already set up. Go to step 5 and check it, and do
  not run `uninstall` to "clean up" first -- that throws the index away.

When it finishes, tell them how many documents were read, how many pages that
came to, how long it took, and how many files were skipped and why.

## Step 5 - check the build

```
engine\.venv\Scripts\python.exe engine\bin\c_selftest.py --db engine\artefacts\pages.db --shelf engine\artefacts\shelf\shelf.db --expect-pages 0
```

`--expect-pages 0` means "do not compare this folder against a fixed reference
count" and must stay. Report the pass count as one line. If any check fails, show
the failing lines verbatim and stop -- do not try to repair the index.

## Step 6 - hand it over

Say, in your own words:

> It's ready. To ask it something, open a terminal in the `<docs>` folder and
> start Claude Code there:
>
>     cd <docs>
>     claude --model claude-opus-5
>
> Then ask in plain English -- "how has spending on X changed over the last ten
> years?" An answer usually takes one to three minutes, because it is opening
> real pages before it replies. Every number comes with a `Sources` line naming
> the file and page it came from, and when the folder does not contain the
> answer, it says so instead of guessing. The first few commands will ask your
> permission to run; approving them is expected.
>
> One thing to know: the answers are only as good as this folder. A `Sources`
> line is a claim you can check in seconds -- open the file at that page. Do
> check a few early on.

If they would rather not leave this terminal, the same question can be asked with:

```
engine\.venv\Scripts\python.exe engine\bin\setup_folder.py ask --folder <docs> "<their question>"
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
engine\.venv\Scripts\python.exe engine\bin\setup_folder.py uninstall --folder <docs> --purge --artefacts engine\artefacts
```

That deletes the two files the tool wrote into `<docs>` and the whole index.
Their documents are untouched.
