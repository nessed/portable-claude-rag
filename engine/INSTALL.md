# Install

This package indexes the folder it sits in and then lets you ask questions about
the documents in plain English, with every answer citing the page it came from.

## The short way

Unzip the package into your documents folder (so that `engine\`, `CLAUDE.md` and
`.claude\` sit beside your documents), double-click `Start research assistant.cmd`,
or open Claude Code in that folder and say **initialize**. It reads `INITIALIZE.md`
-- this same procedure, addressed to it -- and does everything below for you,
including telling you what to install if something is missing. `CLAUDE_ROOT.md` is a
copy of the `CLAUDE.md` the package ships.

The rest of this document is for doing it by hand, from the documents folder.

## What you need

- Python 3.11
- Git for Windows (it ships `pdftotext`)
- Claude Code on the PATH
- about 1 GB of free disk per 2,000 documents
- an internet connection for the **first** run only, to fetch the embedding model

## Install

From the documents folder:

```
python -m venv engine\.venv
engine\.venv\Scripts\pip install -r engine\requirements-portable.txt
engine\.venv\Scripts\python.exe engine\bin\setup_folder.py doctor
```

`doctor` prints one line per prerequisite and exits non-zero if anything is
missing. Fix what it names before going on.

## Index the folder

```
engine\.venv\Scripts\python.exe engine\bin\setup_folder.py install --folder . --artefacts engine\artefacts --seed-env
```

Nothing is written into the folder outside `engine\`: the index goes under
`engine\artefacts`, the answering procedure to `engine\ANSWER.md`, and a `READY`
marker says the install finished. `engine\` itself is never indexed.

This builds the **v1 shelf** -- the one every measured number in the report rests
on. `--builder v2` selects an experimental builder that failed its acceptance gate
on 2026-09-15; it prints a warning and stamps `builder: v2 (experimental)` into
the manifest, so it cannot be used by accident.

An interrupted install is resumed by re-running the identical command. Documents
added later are picked up by:

```
engine\.venv\Scripts\python.exe engine\bin\setup_folder.py refresh --folder . --artefacts engine\artefacts --seed-env
```

## Ask it something

```
claude --model claude-opus-5
```

in the documents folder. `/rag-off` and `/rag-on` switch the tool off and on;
while it is on, Claude Code cannot change any file outside `engine\`. Or, without
Claude Code's chat:

```
engine\.venv\Scripts\python.exe engine\bin\setup_folder.py ask --folder . "<question>"
```

## Check the build

The artefacts directory gets three files:

- `build_manifest.json` -- versions, counts, hashes, timings
- `BUILD_REPORT.md` -- the same in one page of plain English
- `canonical_export.json` -- the logical content, sorted, with timestamps removed

Two installs of the same folder produce the same `canonical_export.json` hash.
Raw database files will differ between installs -- they carry timestamps and
filesystem walk order -- and `BUILD_REPORT.md` explains which fields those are.

`--expect-pages 0` says "do not check the page count". Leave it in: without
it the self-test compares your folder against a fixed reference corpus and
reports a failure that is not one.

```
engine\.venv\Scripts\python.exe engine\bin\c_selftest.py --db engine\artefacts\pages.db --shelf engine\artefacts\shelf\shelf.db --expect-pages 0
```

## Remove it

```
engine\.venv\Scripts\python.exe engine\bin\setup_folder.py uninstall --folder . --purge --artefacts engine\artefacts
```

deletes the index. Deleting `engine\`, `.claude\`, `CLAUDE.md`, `README_FIRST.md` and
`Start research assistant.cmd` removes everything; the documents are untouched.
