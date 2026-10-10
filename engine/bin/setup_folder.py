#!/usr/bin/env python
"""setup_folder.py - point this at a folder of documents and start asking questions.

Everything the lab built is reachable today only by knowing which six scripts to
run in which order against which paths. This is the one command. Ali points it at
a folder shaped like the professor's, and after one install he is asking questions
in it.

  python corpus-lab/bin/setup_folder.py doctor
  python corpus-lab/bin/setup_folder.py install --folder <path> [--label <name>] [--workers 12]
  python corpus-lab/bin/setup_folder.py refresh   --folder <path> [--artefacts <dir>]
  python corpus-lab/bin/setup_folder.py status    --folder <path>
  python corpus-lab/bin/setup_folder.py ask       --folder <path> "<question>" [--model claude-opus-5]
  python corpus-lab/bin/setup_folder.py uninstall --folder <path> [--purge]

The folder itself is READ-ONLY except for the two files c_stack.py owns
(CLAUDE.md and .claude/settings.json), and `uninstall` removes exactly those.
Every index artefact lives under corpus-lab/02_stacks/portable/<label>/.

Each install stage checks for its own output first, so an interrupted install is
resumed by re-running the identical command.

Phase 10.3 (Plan E):
  --builder v1|v2   which shelf builder to use. **Default v1** -- the builder
                    that every measured number in the report was produced with,
                    and the one the demo runs. v2 is the experimental builder
                    that FAILED Gate S on 2026-09-15; selecting it prints a
                    warning and stamps `builder: v2 (experimental)` into the
                    manifest, so it can never be used silently. Until this flag
                    existed the installer shipped v2 unconditionally, which meant
                    the portable was not the thing that had been measured.
  --artefacts <dir> where the index and shelf go. Default: the old location, so
                    existing installs are untouched. A portable install points
                    this inside its own directory, which is what makes it
                    portable at all.
  --seed-env        set and record PYTHONHASHSEED=0 and OMP_NUM_THREADS=4, so
                    two installs of the same corpus are comparable.

At the end of a successful install the artefacts directory gets
`build_manifest.json` (machine-readable), `BUILD_REPORT.md` (one page, plain
English) and `canonical_export.json` (sorted logical rows with every wall-clock
field removed) -- the three files Gate R1 compares two clean-room builds on.
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labpaths as L  # noqa: E402

PORTABLE = L.STACKS / "portable"
PDFTOTEXT = Path(L.PDFTOTEXT)  # F-01: env, then PATH, then the usual places
RUNTIME_PACKAGES = ("numpy", "fastembed", "onnxruntime", "pdfplumber")
MODEL_NAME = "BAAI/bge-small-en-v1.5"
# The 15,000 rung's artefacts are the frozen ones every recorded number rests on.
PROTECTED = ("corpus_15000",)

BUILDERS = {
    "v1": ("c_shelf_build.py",
           "the builder every measured number in the report rests on"),
    "v2": ("c_shelf_build_v2.py",
           "EXPERIMENTAL -- family merge, year normalisation, consensus primary"),
}
V2_WARNING = ("EXPERIMENTAL SHELF V2 -- failed Gate S 2026-09-15; not production")

# Deterministic defaults, recorded rather than assumed (Plan E section D).
SEED_ENV = {"PYTHONHASHSEED": "0", "OMP_NUM_THREADS": "4"}

# Paste-in layout (2026-10-10). The package is unzipped INTO the documents folder, so the
# artefacts directory sits inside the folder being indexed. Nothing the engine owns may be
# read as a document, and the two files the old layout wrote into the folder (CLAUDE.md and
# .claude/settings.json) now ship in the package and are never written by install. The
# marker that says "set up" is <artefacts>/READY.
READY_NAME = "READY"
ANSWER_NAME = "ANSWER.md"


def in_place_top(folder, art_dir):
    """The top-level directory of `folder` that holds the artefacts (normally `engine`),
    or None when the artefacts live outside the folder (the old side-by-side layout)."""
    try:
        rel = Path(art_dir).resolve().relative_to(Path(folder).resolve())
    except ValueError:
        return None
    return rel.parts[0] if rel.parts else None


def _py():
    return sys.executable


def _run(cmd, cwd=None, env=None):
    t0 = time.time()
    p = subprocess.run(cmd, cwd=cwd, env=env)
    return p.returncode, time.time() - t0


# --------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------- #
def find_model_cache():
    """fastembed keeps its ONNX weights in a local cache; say where, and whether
    the first run will need the internet."""
    cands = []
    for var in ("FASTEMBED_CACHE_PATH", "HF_HOME", "XDG_CACHE_HOME"):
        v = os.environ.get(var)
        if v:
            cands.append(Path(v))
    cands += [Path.home() / ".cache" / "huggingface",
              Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "temp" / "fastembed_cache",
              Path.home() / ".cache" / "fastembed"]
    stem = MODEL_NAME.split("/")[-1].lower()
    for c in cands:
        if not c.exists():
            continue
        try:
            for p in c.rglob("*"):
                if p.is_dir() and stem in p.name.lower():
                    return p, True
        except OSError:
            continue
    return (cands[0] if cands else None), False


def doctor(a):
    checks = []

    def chk(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    chk("python_is_the_venv", "\\.venv\\" in _py() or "/.venv/" in _py(), _py())
    for pkg in RUNTIME_PACKAGES:
        try:
            __import__(pkg)
            chk("package_%s" % pkg, True, "importable")
        except Exception as e:
            chk("package_%s" % pkg, False, str(e)[:80])
    chk("pdftotext", PDFTOTEXT.is_absolute() and PDFTOTEXT.exists(),
        str(PDFTOTEXT) if PDFTOTEXT.is_absolute() else
        "not found on PATH -- install Git for Windows (it ships pdftotext), "
        "or set PDFTOTEXT_EXE to a poppler pdftotext.exe")
    _cl = Path(L.CLAUDE)
    chk("claude_exe", _cl.is_absolute() and _cl.exists(),
        str(_cl) if _cl.is_absolute() else
        "not found on PATH -- install Claude Code, or set CLAUDE_CMD to its claude.exe")

    mp, found = find_model_cache()
    chk("embedding_model_%s" % MODEL_NAME.split("/")[-1], True,
        ("cached at %s" % mp) if found
        else "NOT found near %s -- the first run will download it, so that run needs internet" % mp)

    # OCR is optional and off by default, so its absence is reported, never failed: a
    # scanned page with no text layer is then counted as image_only_no_text rather than
    # read. Only asking for OCR (PCR_OCR=1) without the two tools it needs is a failure.
    ocr_on = os.environ.get("PCR_OCR", "").strip().lower() not in ("", "0", "off", "false")
    ocr_missing = [n for n, v in (("tesseract", "TESSERACT_EXE"), ("pdftoppm", "PDFTOPPM_EXE"))
                   if not (os.environ.get(v) and Path(os.environ[v]).exists())
                   and not shutil.which(n)]
    if ocr_on:
        chk("ocr", not ocr_missing,
            "on (PCR_OCR=1)" if not ocr_missing else
            "PCR_OCR=1 but %s not found -- put it on PATH or unset PCR_OCR"
            % " and ".join(ocr_missing))
    else:
        chk("ocr", True,
            "off (PCR_OCR not set) -- scanned pages will be reported as image_only_no_text"
            + ("; tesseract/pdftoppm not found" if ocr_missing else ""))

    free_gb = shutil.disk_usage(str(L.RETRIEVAL_LAB)).free / 1e9
    chk("free_disk_ge_1gb_per_2000_files", free_gb >= 1.0, "%.1f GB free" % free_gb)

    n_fail = 0
    for name, ok, detail in checks:
        print("[%s] %-42s %s" % ("PASS" if ok else "FAIL", name, detail))
        if not ok:
            n_fail += 1
    print("\n%d checks, %d failed" % (len(checks), n_fail))
    return 1 if n_fail else 0


# --------------------------------------------------------------------- #
# install
# --------------------------------------------------------------------- #
def artefacts(label, root=None):
    """Where the index and shelf live.

    `root` is Plan E 3.1's --artefacts. With no root the old location is used
    unchanged, so every existing install and every recorded path still resolves.
    """
    d = Path(root).resolve() if root else (PORTABLE / label)
    return {"dir": d, "db": d / "pages.db", "shelf_dir": d / "shelf",
            "shelf": d / "shelf" / "shelf.db",
            "cards": d / "shelf" / "cards.f16.npy",
            "cap_fts": d / "shelf" / "captions_fts.db",
            "cap_vec": d / "shelf" / "captions.f16.npy"}


def index_owner(db_path):
    """The corpus_root recorded inside an existing index, or None when there is no
    index. Answers "whose pages are these?" before a shelf gets built on top of them.

    Why it exists: an install skips [1/5] whenever pages.db is already there, so
    pointing a SECOND folder at a populated artefacts directory builds a shelf over
    the first folder's pages and prints Ready. Measured live: both indexes on this
    machine still claimed the Temp path they were built in, months after the move.
    """
    if not os.path.exists(db_path):
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % Path(db_path).as_posix(), uri=True)
        row = con.execute("SELECT v FROM meta WHERE k='corpus_root'").fetchone()
        con.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _repoint_index(db_path, folder):
    """Record the new owner. Called only after --adopt, so the metadata stops
    naming a folder the index is no longer used for."""
    try:
        con = sqlite3.connect(db_path)
        con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('corpus_root',?)",
                    (str(Path(folder).resolve()),))
        con.commit()
        con.close()
        return True
    except sqlite3.Error as e:
        print("ADOPT_META_FAILED %s" % e, file=sys.stderr)
        return False


def install(a):
    folder = Path(a.folder).resolve()
    if not folder.is_dir():
        print("NO_SUCH_FOLDER %s" % folder, file=sys.stderr)
        return 2
    if folder.name in PROTECTED:
        print("REFUSED %s is a frozen rung; its artefacts are the recorded ones. "
              "Copy the folder if you want to reindex it." % folder.name, file=sys.stderr)
        return 3
    label = a.label or folder.name
    builder = getattr(a, "builder", "v1")
    art = artefacts(label, getattr(a, "artefacts", None))
    top = in_place_top(folder, art["dir"])
    refresh = bool(getattr(a, "refresh", False))

    if top is None and (folder / "CLAUDE.md").exists() and not refresh:
        # Old layout: the last thing install writes into the folder is CLAUDE.md.
        print("REFUSED %s already has a CLAUDE.md. Run `uninstall` first, or "
              "point at a folder this tool owns." % folder, file=sys.stderr)
        return 3
    if (art["dir"] / READY_NAME).exists() and not refresh:
        print("REFUSED %s is already set up (%s exists). Nothing to do; to pick up "
              "documents added since, run `refresh`." % (folder, art["dir"] / READY_NAME),
              file=sys.stderr)
        return 3

    # Whose pages are in that index? [1/5] skips when pages.db exists, so without
    # this the shelf is built over another folder's pages and the install reports
    # Ready. Refuse and name the owner; --adopt repoints it deliberately. In the
    # paste-in layout the index can only belong to the folder that holds it, so a
    # mismatch means the folder was moved: adopt without being asked.
    owner = index_owner(art["db"])
    if owner and Path(owner).resolve() != Path(folder).resolve():
        if top is not None:
            setattr(a, "adopt", True)
        if not getattr(a, "adopt", False):
            print("FOREIGN_INDEX_PRESENT %s belongs to %s" % (art["db"], owner),
                  file=sys.stderr)
            print("  re-run with --adopt to repoint it at %s" % folder, file=sys.stderr)
            return 3
        print("ADOPTING index %s -> %s" % (owner, folder))
        _repoint_index(art["db"], folder)

    art["shelf_dir"].mkdir(parents=True, exist_ok=True)
    timings = {}

    env = dict(os.environ)
    if getattr(a, "seed_env", False):
        env.update(SEED_ENV)
        for k, v in SEED_ENV.items():
            os.environ[k] = v
        print("SEED_ENV %s" % json.dumps(SEED_ENV))

    builder_script, builder_desc = BUILDERS[builder]
    if builder == "v2":
        print("!" * 72)
        print(V2_WARNING)
        print("!" * 72, flush=True)
    print("BUILDER %s (%s) -> %s" % (builder, builder_desc, builder_script))

    # 1. page index. index_build is incremental: with an existing db it reads only the
    # files it has not seen (and retries the ones that failed), so `refresh` runs it again.
    if art["db"].exists() and not refresh:
        print("[1/5] index: already built at %s -- skipping" % art["db"])
        timings["index_s"] = 0
    else:
        print("[1/5] %s %s -> %s (%d workers)" % (
            "refreshing" if art["db"].exists() else "indexing", folder, art["db"], a.workers),
              flush=True)
        cmd = [_py(), "-u", str(L.BIN / "index_build.py"),
               "--corpus", str(folder), "--db", str(art["db"]),
               "--workers", str(a.workers)]
        if top is not None:
            cmd += ["--exclude-top", top]
        rc, s = _run(cmd, env=env)
        timings["index_s"] = round(s, 1)
        print("      %.0fs" % s, flush=True)
        if rc != 0:
            print("INDEX_FAILED rc=%d" % rc, file=sys.stderr)
            return 4

    # 2. shelf -- v1 by default (Plan E decision 4): the portable must build the
    # same shelf that was measured, not the experimental one that failed Gate S.
    needs_full = not (art["cards"].exists() and art["shelf"].exists())
    if not needs_full:
        # The shelf exists, but the index may have grown since. A missing document used
        # to mean a full rebuild of every card; c_shelf_update brings the shelf forward
        # by embedding only what is missing, and prints UP_TO_DATE when there is nothing
        # to do. The caption artefacts are updated in place by it, so their own skip
        # tests below still hold.
        print("[2/5] shelf: exists -- bringing it up to date with the index", flush=True)
        rc, s = _run([_py(), "-u", str(L.BIN / "c_shelf_update.py"),
                      "--db", str(art["db"]), "--out", str(art["shelf_dir"])], env=env)
        timings["shelf_s"] = round(s, 1)
        if rc == 6:
            # More than half the index is newer than the shelf, so this is not "a document
            # was added", it is "this is a different corpus" (or a first build onto an
            # inherited index). An incremental merge would cost the same as a rebuild and
            # leave rows for documents the index no longer has.
            print("      more than half the index is newer than the shelf -- rebuilding it "
                  "in full instead", flush=True)
            needs_full = True
        elif rc != 0:
            print("SHELF_UPDATE_FAILED rc=%d" % rc, file=sys.stderr)
            return 4

    if needs_full:
        print("[2/5] building the shelf with builder %s (card vectors take the "
              "longest)" % builder, flush=True)
        rc, s = _run([_py(), "-u", str(L.BIN / builder_script),
                      "--db", str(art["db"]), "--out", str(art["shelf_dir"])],
                     env=env)
        timings["shelf_s"] = round(s, 1)
        print("      %.0fs" % s, flush=True)
        if rc != 0:
            print("SHELF_FAILED rc=%d" % rc, file=sys.stderr)
            return 4

    # 3. caption index + caption vectors
    if art["cap_fts"].exists():
        print("[3/5] caption index: already built -- skipping")
        timings["caption_index_s"] = 0
    else:
        print("[3/5] caption index", flush=True)
        rc, s = _run([_py(), "-u", str(L.BIN / "c_caption_index.py"),
                      "--out-dir", str(art["shelf_dir"])], env=env)
        timings["caption_index_s"] = round(s, 1)
        if rc != 0:
            print("CAPTION_INDEX_FAILED rc=%d" % rc, file=sys.stderr)
            return 4

    if art["cap_vec"].exists():
        print("[4/5] caption vectors: already built -- skipping")
        timings["caption_embed_s"] = 0
    else:
        print("[4/5] caption vectors", flush=True)
        rc, s = _run([_py(), "-u", str(L.BIN / "c_caption_embed.py"),
                      "--out-dir", str(art["shelf_dir"])], env=env)
        timings["caption_embed_s"] = round(s, 1)
        print("      %.0fs" % s, flush=True)
        if rc != 0:
            print("CAPTION_EMBED_FAILED rc=%d" % rc, file=sys.stderr)
            return 4

    # 5. register the root. Paste-in layout: CLAUDE.md, .claude/settings.json and the
    # skills ship in the package, so nothing is written into the folder; the answering
    # procedure goes to <top>/ANSWER.md with relative paths and READY marks the install.
    # Old layout: c_stack writes the two files into the folder, as before.
    if top is not None:
        print("[5/5] registering the folder (paste-in layout: nothing is written outside %s)"
              % top, flush=True)
        t0 = time.time()
        import c_shelf as CSH
        import c_stack as CST
        CSH.do_register(str(folder), str(art["db"]), str(art["shelf"]))
        answer = folder / top / ANSWER_NAME
        answer.write_text(CST.claude_md_text("%s/.venv/Scripts/python.exe" % top,
                                             "%s/bin/c_shelf.py" % top)
                          + CST.PASTE_IN_ADDENDUM, encoding="utf-8")
        timings["stack_s"] = round(time.time() - t0, 1)
    else:
        print("[5/5] installing CLAUDE.md and .claude/settings.json", flush=True)
        rc, s = _run([_py(), str(L.BIN / "c_stack.py"), "setup",
                      "--corpus", str(folder), "--shelf-dir", str(art["shelf_dir"]),
                      "--db", str(art["db"]), "--log-dir", str(art["dir"] / "hooks")])
        timings["stack_s"] = round(s, 1)
        if rc != 0:
            print("STACK_SETUP_FAILED rc=%d" % rc, file=sys.stderr)
            return 4

    # what the folder turned out to contain
    import c_shelf as CSH
    ctx = CSH.get_ctx(str(art["db"]), str(art["shelf"]), str(folder))
    CSH._print_coverage(ctx.coverage())
    n_fam = ctx.shelf.execute("SELECT COUNT(*) FROM families").fetchone()[0]
    n_docs = ctx.shelf.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    print("SHELF families=%d documents=%d" % (n_fam, n_docs))
    print("TIMINGS %s" % json.dumps(timings))

    try:
        import build_manifest as BM
        BM.write_all(art, folder, label, builder, timings,
                     workers=a.workers,
                     seed_env=SEED_ENV if getattr(a, "seed_env", False) else {})
        print("MANIFEST %s" % (art["dir"] / "build_manifest.json"))
        print("REPORT   %s" % (art["dir"] / "BUILD_REPORT.md"))
        print("CANON    %s" % (art["dir"] / "canonical_export.json"))
    except Exception as e:
        print("MANIFEST_FAILED %s" % e, file=sys.stderr)
        return 5
    if top is not None:
        (art["dir"] / READY_NAME).write_text(
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), encoding="utf-8")
        print("READY    %s" % (art["dir"] / READY_NAME))
        print("\nReady. Ask it something here, in this same chat. /rag-status shows the "
              "state, /rag-off switches it off.")
        return 0
    print("\nReady. Ask it something:")
    print('  cd "%s"' % folder)
    print("  claude --model claude-opus-5")
    return 0


def refresh(a):
    """Pick up documents added since the index was built: the index is incremental, the
    shelf is brought forward by c_shelf_update, and the manifest and READY are rewritten."""
    setattr(a, "refresh", True)
    if not hasattr(a, "builder"):
        setattr(a, "builder", "v1")
    if not hasattr(a, "adopt"):
        setattr(a, "adopt", False)
    return install(a)


# --------------------------------------------------------------------- #
# status / uninstall / ask
# --------------------------------------------------------------------- #
def status(a):
    folder = Path(a.folder).resolve()
    return _run([_py(), str(L.BIN / "c_stack.py"), "status", "--corpus", str(folder)])[0]


def uninstall(a):
    folder = Path(a.folder).resolve()
    label = a.label or folder.name
    art = artefacts(label, getattr(a, "artefacts", None))
    top = in_place_top(folder, art["dir"])
    if top is not None:
        # Paste-in layout: the folder holds nothing the installer wrote except under <top>.
        rc = 0
        p = art["dir"] / READY_NAME
        if p.exists():
            p.unlink()
            print("REMOVED %s" % p)
        off = folder / top / "OFF"
        if off.exists():
            off.unlink()
        print("switched off: %s no longer answers from the index" % folder)
    else:
        rc = _run([_py(), str(L.BIN / "c_stack.py"), "teardown", "--corpus", str(folder)])[0]
    # drop the registry entry so a later `find` in that folder does not resolve
    # to a shelf that is no longer installed
    try:
        import c_shelf as CSH
        reg = CSH._read_registry_file(CSH.REG_PATH)   # the package's own file only
        key = CSH._norm_root_key(folder)
        if key in reg:
            del reg[key]
            CSH._save_registry(reg)
            print("UNREGISTERED %s" % folder)
    except Exception as e:
        print("registry cleanup skipped: %s" % e, file=sys.stderr)
    if a.purge:
        d = art["dir"]
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            print("PURGED %s" % d)
    else:
        print("index artefacts kept (pass --purge to delete them)")
    return rc


def ask(a):
    """The professor's own command, with stdout going to a FILE.

    Session Log A.5.2: an answer was lost to a `tail` once. Nothing that costs a
    model call gets thrown away here -- it is written whole, then echoed."""
    folder = Path(a.folder).resolve()
    ready = (folder / "engine" / "artefacts" / READY_NAME).exists()
    if not ready and not (folder / "CLAUDE.md").exists():
        print("NOT_INSTALLED %s -- run `install --folder` first" % folder, file=sys.stderr)
        return 3
    out_dir = L.SCRATCH / "asks"
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = a.slug or ("ask_%s" % time.strftime("%Y%m%d_%H%M%S"))
    out_p, err_p = out_dir / ("%s.md" % slug), out_dir / ("%s.err" % slug)

    cmd = [str(L.CLAUDE), "-p", a.question, "--model", a.model,
           "--permission-mode", "bypassPermissions"]
    # Phase 10.3.4: Gate R2 has to check every Sources line against the pages the
    # session actually OPENED, and those lines are in tool output, not in the
    # final prose. --stream-json records the transcript so that check is
    # possible. Absent the flag the command is byte-for-byte what it was.
    stream = getattr(a, "stream_json", None)
    if stream:
        cmd += ["--output-format", "stream-json", "--verbose"]
        out_p = Path(stream)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        err_p = out_p.with_suffix(".err")
    print("asking (%s), output -> %s" % (a.model, out_p), flush=True)
    t0 = time.time()
    with open(out_p, "w", encoding="utf-8") as fo, open(err_p, "w", encoding="utf-8") as fe:
        p = subprocess.run(cmd, cwd=str(folder), stdin=subprocess.DEVNULL,
                           stdout=fo, stderr=fe)
    wall = time.time() - t0
    text = out_p.read_text(encoding="utf-8", errors="replace")
    if stream:
        print("\n[stream-json transcript saved to %s]" % out_p)
        print("[rc=%d  %.1fs]" % (p.returncode, wall))
        return p.returncode
    # The answer is already safely on disk; echoing it must never be able to lose
    # it. A Windows console is cp1252, and a real answer contains non-breaking
    # hyphens and en-dashes, which raised UnicodeEncodeError and took the whole
    # command down AFTER the model had been paid for. Found by the Phase 6 cold
    # test on corpus_500.
    _echo(text)
    print("\n[rc=%d  %.1fs  answer saved to %s]" % (p.returncode, wall, out_p))
    return p.returncode


def _echo(text):
    """Print text without ever raising on a console that cannot encode it."""
    try:
        print("\n" + text)
        return
    except UnicodeEncodeError:
        pass
    enc = (getattr(sys.stdout, "encoding", None) or "utf-8")
    sys.stdout.write("\n" + text.encode(enc, errors="replace").decode(enc, errors="replace"))
    sys.stdout.write("\n")


def main():
    ap = argparse.ArgumentParser(prog="setup_folder.py")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("doctor")

    p = sub.add_parser("install")
    p.add_argument("--folder", required=True)
    p.add_argument("--label", default=None)
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--builder", choices=["v1", "v2"], default="v1",
                   help="shelf builder. v1 (default) is the one every measured "
                        "number rests on; v2 is experimental and failed Gate S")
    p.add_argument("--artefacts", default=None,
                   help="where the index and shelf go (default: the old location)")
    p.add_argument("--seed-env", dest="seed_env", action="store_true",
                   help="set and record PYTHONHASHSEED=0 and OMP_NUM_THREADS=4")
    p.add_argument("--adopt", action="store_true",
                   help="repoint an index that belongs to another folder at --folder. "
                        "Without this the install refuses with FOREIGN_INDEX_PRESENT")

    p = sub.add_parser("refresh",
                       help="index documents added since install and bring the shelf forward")
    p.add_argument("--folder", required=True)
    p.add_argument("--label", default=None)
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--artefacts", default=None)
    p.add_argument("--seed-env", dest="seed_env", action="store_true")

    p = sub.add_parser("status")
    p.add_argument("--folder", required=True)

    p = sub.add_parser("uninstall")
    p.add_argument("--folder", required=True)
    p.add_argument("--label", default=None)
    p.add_argument("--artefacts", default=None)
    p.add_argument("--purge", action="store_true")

    p = sub.add_parser("ask")
    p.add_argument("--folder", required=True)
    p.add_argument("question")
    p.add_argument("--model", default="claude-opus-5")
    p.add_argument("--slug", default=None)
    p.add_argument("--stream-json", dest="stream_json", default=None,
                   help="also record the stream-json transcript to this path, so "
                        "citations can be checked against what was opened")

    a = ap.parse_args()
    if not a.cmd:
        ap.print_help()
        return 2
    return {"doctor": doctor, "install": install, "refresh": refresh, "status": status,
            "uninstall": uninstall, "ask": ask}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
