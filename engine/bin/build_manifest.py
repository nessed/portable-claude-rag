#!/usr/bin/env python
"""build_manifest.py - what an install produced, three ways.

Plan E Phase 3.2. An install writes three files into its artefacts directory:

  build_manifest.json   machine-readable: versions, counts, hashes, timings
  BUILD_REPORT.md       the same facts in one page of plain English, naming the
                        fields that are NOT deterministic and why
  canonical_export.json the logical content of the databases as SORTED ROWS with
                        every wall-clock field removed

The third one exists because Gate R1 asks whether two clean-room builds of the
same corpus agree, and raw SQLite files never match: they carry creation
timestamps, they depend on directory walk order, and float embeddings differ in
their last bits between runs. Comparing raw hashes would fail every time and
prove nothing. Comparing a canonical export fails only when the BUILD differs,
which is the question actually being asked.

  python build_manifest.py --artefacts <dir> --folder <corpus> [--label L]
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labpaths as L  # noqa: E402

# Columns that record WHEN something happened rather than WHAT was built. They
# are removed from the canonical export; the manifest keeps them.
WALLCLOCK_FIELDS = {"built_at", "indexed_at", "created_at", "updated_at",
                    "timing_s", "wall_s", "elapsed_s", "ts", "mtime",
                    "generated_utc", "build_time"}

# Tables exported logically, in this order. A table absent from a given builder's
# schema is skipped and named in `tables_absent`, so the two sides of an R1
# comparison can still be compared and the difference is visible.
SHELF_TABLES = ["docs", "families", "editions", "dupes", "duplicate_groups",
                "captions", "cards"]
DB_TABLES = ["pages_status", "status", "files"]

PKGS = ("fastembed", "onnxruntime", "numpy", "pdfplumber")
EMBED_MODEL = "BAAI/bge-small-en-v1.5"


def sha256_file(p):
    p = Path(p)
    if not p.exists():
        return None
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_obj(o):
    return hashlib.sha256(
        json.dumps(o, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")).hexdigest()


def pkg_versions():
    import importlib.metadata as md
    out = {}
    for name in PKGS:
        try:
            out[name] = md.version(name)
        except Exception:
            out[name] = "<absent>"
    return out


def embed_model_sha():
    cache = os.environ.get("FASTEMBED_CACHE_PATH") or str(
        Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Temp" / "fastembed_cache")
    root = Path(cache)
    if not root.exists():
        return {"<cache>": "<absent>"}
    return {p.parts[len(root.parts)] + "/" + p.name: sha256_file(p)
            for p in sorted(root.rglob("*.onnx"))}


def _tables(con):
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _export_table(con, table):
    """Every row, as dicts, with wall-clock columns dropped, sorted."""
    cur = con.execute("SELECT * FROM %s" % table)
    cols = [d[0] for d in cur.description]
    keep = [c for c in cols if c.lower() not in WALLCLOCK_FIELDS]
    rows = []
    for r in cur.fetchall():
        d = {c: v for c, v in zip(cols, r) if c in keep}
        rows.append(d)
    rows.sort(key=lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False))
    return rows, [c for c in cols if c not in keep]


def canonical_export(art):
    out = {"tables": {}, "tables_absent": [], "dropped_columns": {}}
    for label, path, wanted in (("shelf", art["shelf"], SHELF_TABLES),
                                ("pages", art["db"], DB_TABLES)):
        if not Path(path).exists():
            out["tables_absent"].append("%s (whole database)" % label)
            continue
        con = sqlite3.connect("file:%s?mode=ro" % Path(path).as_posix(), uri=True)
        try:
            present = _tables(con)
            for t in wanted:
                if t not in present:
                    out["tables_absent"].append("%s.%s" % (label, t))
                    continue
                rows, dropped = _export_table(con, t)
                out["tables"]["%s.%s" % (label, t)] = rows
                if dropped:
                    out["dropped_columns"]["%s.%s" % (label, t)] = dropped
        finally:
            con.close()
    out["row_counts"] = {k: len(v) for k, v in out["tables"].items()}
    return out


def vector_sha(path, decimals=3):
    """sha256 of a vector store ROUNDED, so two builds are compared on the values
    rather than on float noise in the last bits."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        import numpy as np
        a = np.load(str(p))
        return {"sha256_rounded_%ddp" % decimals:
                hashlib.sha256(np.round(a.astype("float64"), decimals).tobytes()).hexdigest(),
                "shape": list(a.shape), "dtype": str(a.dtype)}
    except Exception as e:
        return {"error": str(e)}


def counts(art):
    out = {}
    if Path(art["shelf"]).exists():
        con = sqlite3.connect("file:%s?mode=ro" % Path(art["shelf"]).as_posix(), uri=True)
        try:
            present = _tables(con)
            for t in SHELF_TABLES:
                if t in present:
                    out["n_%s" % t] = con.execute(
                        "SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        finally:
            con.close()
    if Path(art["db"]).exists():
        con = sqlite3.connect("file:%s?mode=ro" % Path(art["db"]).as_posix(), uri=True)
        try:
            present = _tables(con)
            for t in DB_TABLES:
                if t in present:
                    out["n_%s" % t] = con.execute(
                        "SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                    try:
                        rows = con.execute(
                            "SELECT status, COUNT(*) FROM %s GROUP BY status" % t
                        ).fetchall()
                        if rows:
                            out["status_counts"] = {str(k): v for k, v in rows}
                    except sqlite3.Error:
                        pass
        finally:
            con.close()
    return out


def write_all(art, folder, label, builder, timings, workers=None, seed_env=None):
    folder = Path(folder)
    d = art["dir"]
    d.mkdir(parents=True, exist_ok=True)

    n_files = sum(1 for x in folder.rglob("*") if x.is_file())
    canon = canonical_export(art)
    canon_path = d / "canonical_export.json"
    canon_path.write_text(json.dumps(canon, indent=1, ensure_ascii=False),
                          encoding="utf-8")
    canon_sha = sha256_obj(canon)

    import subprocess
    def git(*a):
        try:
            return subprocess.run(["git", "-C", str(L.RETRIEVAL_LAB)] + list(a),
                                  capture_output=True, text=True,
                                  timeout=60).stdout.strip()
        except Exception:
            return None

    man = {
        "schema": "build_manifest/1",
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "label": label,
        "builder": ("v2 (experimental)" if builder == "v2" else "v1"),
        "builder_script": ("c_shelf_build_v2.py" if builder == "v2"
                           else "c_shelf_build.py"),
        "builder_warning": ("EXPERIMENTAL SHELF V2 -- failed Gate S 2026-09-15; "
                            "not production" if builder == "v2" else None),
        "package_commit": git("rev-parse", "HEAD"),
        "package_branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "corpus": {"root": str(folder), "name": folder.name, "n_files": n_files},
        "runtime": {
            "python": sys.version.split()[0],
            "packages": pkg_versions(),
            "workers": workers,
            "seed_env": seed_env or {},
            "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS"),
            "PYTHONHASHSEED": os.environ.get("PYTHONHASHSEED"),
        },
        "embedding_model": EMBED_MODEL,
        "embedding_model_files_sha256": embed_model_sha(),
        "counts": counts(art),
        "artefact_sha256": {k: sha256_file(v) for k, v in art.items()
                            if k not in ("dir", "shelf_dir")},
        "vectors": {"cards": vector_sha(art["cards"]),
                    "captions": vector_sha(art["cap_vec"])},
        "canonical_export_sha256": canon_sha,
        "canonical_row_counts": canon["row_counts"],
        "canonical_tables_absent": canon["tables_absent"],
        "timings_s": timings,
        "total_wall_s": round(sum(v for v in timings.values()
                                  if isinstance(v, (int, float))), 1),
        "warnings": [],
    }
    if builder == "v2":
        man["warnings"].append(
            "Built with the EXPERIMENTAL v2 shelf builder, which failed Gate S on "
            "2026-09-15. Not the configuration any reported number was measured on.")
    if not seed_env:
        man["warnings"].append(
            "Built without --seed-env, so PYTHONHASHSEED and OMP_NUM_THREADS were "
            "whatever the environment had. Two such builds are not strictly "
            "comparable.")

    (d / "build_manifest.json").write_text(
        json.dumps(man, indent=1, ensure_ascii=False), encoding="utf-8")
    (d / "BUILD_REPORT.md").write_text(render_report(man), encoding="utf-8")
    return man


def render_report(m):
    c = m["counts"]
    rows = "\n".join("| %s | %s |" % (k.replace("n_", "").replace("_", " "), v)
                     for k, v in sorted(c.items()) if k != "status_counts")
    pk = "\n".join("| %s | %s |" % (k, v)
                   for k, v in sorted(m["runtime"]["packages"].items()))
    art = "\n".join("| %s | `%s` |" % (k, (v or "absent")[:16])
                    for k, v in sorted(m["artefact_sha256"].items()))
    warn = ("\n".join("- %s" % w for w in m["warnings"])) or "- none"
    return """# Build report — {label}

Built {built} from package commit `{commit}` on branch `{branch}`.

## What was built

The folder `{root}` holds **{nfiles} files**. Indexing reads every page of every
readable document into a page index, then the shelf builder groups those documents
into families and editions and embeds one card per document; a caption index and
caption vectors are built on top. After that the folder gets a `CLAUDE.md` and a
`.claude/settings.json` and is ready to be asked questions.

**Shelf builder: {builder}** ({builder_script}).
{builder_note}

## Counts

| thing | count |
|---|---|
{rows}

## Versions

python {py}

| package | version |
|---|---|
{pk}

Embedding model: `{model}`. Its cached weights are hashed in
`build_manifest.json` under `embedding_model_files_sha256`; a different hash
there means a different model, whatever the name says.

Workers: {workers}. Seed environment: {seed}.

## Artefacts

| file | sha256 (first 16) |
|---|---|
{art}

## What is NOT deterministic, and what to compare instead

Three things differ between two builds of the same folder, none of which mean the
build differs:

1. **Timestamps.** Every artefact records when it was made. `canonical_export.json`
   drops every wall-clock column ({dropped}).
2. **Directory walk order.** The order files are discovered depends on the
   filesystem, so row order and rowids differ. The canonical export sorts every
   table by its full content before hashing.
3. **Float embeddings.** Vector values can differ in their last bits between runs
   of the same ONNX model. The manifest therefore hashes the vector stores
   **rounded to 3 decimal places**, and a comparison that disagrees below that is
   reported as float noise rather than treated as a difference.

So two builds are the same build when `canonical_export_sha256` matches, the counts
match, the rounded vector hashes match (or the per-row cosine similarity is at
least 0.9999 mean / 0.999 min), and the same fixed list of retrieval queries
returns the same ranked results. Raw file hashes are expected to differ and are
recorded for completeness only.

## Warnings

{warn}

## Timings

{timings} — total {total}s.
""".format(
        label=m["label"], built=m["built_utc"], commit=(m["package_commit"] or "?")[:10],
        branch=m["package_branch"], root=m["corpus"]["root"],
        nfiles=m["corpus"]["n_files"], builder=m["builder"],
        builder_script=m["builder_script"],
        builder_note=(("**%s**" % m["builder_warning"]) if m["builder_warning"]
                      else "This is the builder every measured number in the "
                           "report rests on, and the one the demo runs."),
        rows=rows or "| (none) | |", py=m["runtime"]["python"], pk=pk,
        model=m["embedding_model"], workers=m["runtime"]["workers"],
        seed=(json.dumps(m["runtime"]["seed_env"]) if m["runtime"]["seed_env"]
              else "not set (see warnings)"),
        art=art or "| (none) | |",
        dropped=", ".join(sorted(WALLCLOCK_FIELDS)),
        warn=warn, timings=json.dumps(m["timings_s"]), total=m["total_wall_s"])


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--artefacts", required=True)
    ap.add_argument("--folder", required=True)
    ap.add_argument("--label", default=None)
    ap.add_argument("--builder", default="v1")
    a = ap.parse_args(argv[1:])
    import setup_folder as SF
    art = SF.artefacts(a.label or Path(a.folder).name, a.artefacts)
    m = write_all(art, a.folder, a.label or Path(a.folder).name, a.builder, {})
    print(json.dumps({k: v for k, v in m.items()
                      if k in ("label", "builder", "counts",
                               "canonical_export_sha256")}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
