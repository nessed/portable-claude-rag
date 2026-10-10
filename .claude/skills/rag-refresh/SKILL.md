---
name: rag-refresh
description: Index documents added to the folder since the last initialize or refresh, so they become searchable
---

Tell the user this reads only the new files and takes a few seconds per file, then run exactly:

    "engine/.venv/Scripts/python.exe" engine/bin/setup_folder.py refresh --folder . --artefacts engine/artefacts --seed-env

Red NativeCommandError blocks are progress output, not errors; judge by the exit code. When it finishes, report how many files were read and how many could not be, quoting the `failed_by={...}` field if it is not empty. Then run `"engine/bin/hooks.cmd" status` and report the new counts.
