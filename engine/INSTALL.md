# Install

This package indexes a folder of documents and then lets you ask questions about
it in plain English, with every answer citing the page it came from.

## The short way

Copy `CLAUDE_ROOT.md` to `CLAUDE.md` in the folder that holds both this package
and your documents, open Claude Code there, and say **initialize**. It reads
`INITIALIZE.md` -- this same procedure, addressed to it -- and does everything
below for you, including telling you what to install if something is missing.

The rest of this document is for doing it by hand.

## What you need

- Python 3.11
- Claude Code on the PATH
- about 1 GB of free disk per 2,000 documents
- an internet connection for the **first** run only, to fetch the embedding model

## Install

From this directory:

```
python -m venv .venv
.venv\Scripts\pip install -r requirements-portable.txt
.venv\Scripts\python.exe bin\setup_folder.py doctor
```

`doctor` prints one line per prerequisite and exits non-zero if anything is
missing. Fix what it names before going on.

## Index a folder

```
.venv\Scripts\python.exe bin\setup_folder.py install --folder <your folder> --artefacts .\artefacts --seed-env
```

The folder itself is left read-only apart from two files the tool owns,
`CLAUDE.md` and `.claude/settings.json`; `uninstall` removes exactly those. Every
index artefact goes under `--artefacts`.

This builds the **v1 shelf** -- the one every measured number in the report rests
on. `--builder v2` selects an experimental builder that failed its acceptance gate
on 2026-09-15; it prints a warning and stamps `builder: v2 (experimental)` into
the manifest, so it cannot be used by accident.

An interrupted install is resumed by re-running the identical command.

## Ask it something

```
cd <your folder>
claude --model claude-opus-5
```

or, without leaving this directory:

```
.venv\Scripts\python.exe bin\setup_folder.py ask --folder <your folder> "<question>"
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
.venv\Scripts\python.exe bin\c_selftest.py --db .\artefacts\pages.db --shelf .\artefacts\shelf\shelf.db --expect-pages 0
```

## Remove it

```
.venv\Scripts\python.exe bin\setup_folder.py uninstall --folder <your folder>
```

Add `--purge --artefacts .\artefacts` to delete the index as well.
