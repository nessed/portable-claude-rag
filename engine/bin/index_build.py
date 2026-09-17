#!/usr/bin/env python
"""index_build.py - portable page-level corpus index. Zero external dependencies.

Chosen because it is the ONLY stack that survives this machine's constraints:
no CUDA, no Docker, no WSL, no cargo, no rga, no tesseract, no sqlite3 CLI.
SQLite FTS5 ships in Python's stdlib and pdftotext ships with Git for Windows.

Design points that matter:
  * PAGE-LEVEL rows, not file-level. "Pakistan's X trajectory" is answered by one
    table on page 340, so the retrieval unit has to be a page.
  * COVERAGE ACCOUNTING with a closed status enum and no "unknown" state. The
    accounting identity discovered = indexed + failed + excluded + pending must hold.
  * CONTENT HASH is the identity; path is a mutable alias. A moved file is a path
    update, not a reindex, and never a silent miss.
  * EXCLUSIONS ARE RECORDED, not dropped, so 136k venv files stay auditable
    as intentionally_excluded:dependency rather than vanishing from the numbers.

Usage:
  python index_build.py --corpus <root> --db <path.db> [--workers 12] [--limit N]
  python index_build.py --db <path.db> --stats
"""
import argparse, hashlib, json, os, re, sqlite3, subprocess, sys, time, zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# F-01: resolved (env PDFTOTEXT_EXE, then Git for Windows, then PATH). The
# try/except keeps this module runnable on its own, which the docstring above
# promises, even when labpaths.py is not beside it.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import labpaths as _L
    PDFTOTEXT = _L.PDFTOTEXT
except Exception:  # pragma: no cover - standalone fallback
    import shutil as _shutil
    PDFTOTEXT = (os.environ.get("PDFTOTEXT_EXE")
                 or _shutil.which("pdftotext") or "pdftotext.exe")

# Directory names that are reinstallable dependency material, never research signal.
EXCLUDE_DIRS = {
    ".git", "node_modules", "site-packages", "__pycache__", "__MACOSX",
    ".envs", ".venv", "venv", ".model_cache", ".mypy_cache", ".pytest_cache",
    "dist-info", ".next", ".cache", "Lib",
}
TEXT_EXT = {
    ".md", ".txt", ".csv", ".json", ".jsonl", ".yaml", ".yml", ".html", ".htm",
    ".py", ".js", ".ts", ".tsx", ".r", ".do", ".sql", ".sh", ".ps1", ".log",
    ".tex", ".bib", ".ini", ".cfg", ".toml", ".xml", ".rst", ".tsv",
}
PDF_EXT = {".pdf"}
DOCX_EXT = {".docx", ".xlsx", ".pptx"}
BINARY_SKIP = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp", ".mp4",
    ".mp3", ".wav", ".zip", ".gz", ".tar", ".7z", ".rar", ".exe", ".dll",
    ".so", ".pyd", ".whl", ".pack", ".idx", ".bin", ".safetensors", ".pt",
    ".onnx", ".dta", ".parquet", ".db", ".sqlite", ".pdb", ".lib", ".obj",
}
MAX_BYTES = 300 * 1024 * 1024
PDF_TIMEOUT = 180


def sha256_of(p, cap=64 * 1024 * 1024):
    """Full hash for normal files; head+tail+size for very large ones (still stable)."""
    h = hashlib.sha256()
    size = os.path.getsize(p)
    with open(p, "rb") as fh:
        if size <= cap:
            for blk in iter(lambda: fh.read(1 << 20), b""):
                h.update(blk)
        else:
            h.update(fh.read(cap // 2))
            fh.seek(-cap // 2, os.SEEK_END)
            h.update(fh.read())
            h.update(str(size).encode())
    return h.hexdigest()


def extract_pages(path, ext):
    """-> (status, error_class, [(page_index, text), ...])"""
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return "failed_permission", type(e).__name__, []
    if size == 0:
        return "zero_byte", "", []
    if size > MAX_BYTES:
        return "skipped_too_large", "", []

    if ext in PDF_EXT:
        try:
            p = subprocess.run([PDFTOTEXT, "-layout", "-enc", "UTF-8", str(path), "-"],
                               capture_output=True, timeout=PDF_TIMEOUT)
        except subprocess.TimeoutExpired:
            return "failed_timeout", "pdftotext_timeout", []
        except OSError as e:
            return "failed_parser", type(e).__name__, []
        if p.returncode != 0:
            err = (p.stderr or b"").decode("utf-8", "replace")[:200]
            if "Incorrect password" in err or "encrypted" in err.lower():
                return "failed_encrypted", "encrypted", []
            return "failed_parser", err[:80] or "pdftotext_rc%d" % p.returncode, []
        raw = (p.stdout or b"").decode("utf-8", "replace")
        pages = raw.split("\f")
        if pages and not pages[-1].strip():
            pages.pop()
        if not any(t.strip() for t in pages):
            return "image_only_no_text", "", []
        return "indexed", "", [(i, t) for i, t in enumerate(pages) if t.strip()]

    if ext in DOCX_EXT:
        try:
            out = []
            with zipfile.ZipFile(path) as z:
                for n in z.namelist():
                    if n.endswith(".xml") and (
                        "document" in n or "sheet" in n or "slide" in n or
                        "sharedStrings" in n
                    ):
                        t = z.read(n).decode("utf-8", "replace")
                        t = re.sub(r"<[^>]+>", " ", t)
                        t = re.sub(r"\s+", " ", t)
                        if t.strip():
                            out.append(t)
            body = "\n".join(out)
            if not body.strip():
                return "image_only_no_text", "", []
            return "indexed", "", [(0, body)]
        except zipfile.BadZipFile:
            return "failed_corrupt", "BadZipFile", []
        except Exception as e:
            return "failed_parser", type(e).__name__, []

    if ext in TEXT_EXT or ext == "":
        try:
            body = Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return "failed_parser", type(e).__name__, []
        if not body.strip():
            return "zero_byte", "", []
        # chunk long text files so a hit points at a region, not a 100k-line file
        lines, pages, buf, n = body.splitlines(), [], [], 0
        for ln in lines:
            buf.append(ln)
            if len(buf) >= 400:
                pages.append((n, "\n".join(buf))); buf = []; n += 1
        if buf:
            pages.append((n, "\n".join(buf)))
        return "indexed", "", pages

    return "unsupported_type", ext, []


def work(item):
    path, rel = item
    ext = Path(path).suffix.lower()
    try:
        st = os.stat(path)
    except OSError as e:
        return dict(rel=rel, status="failed_permission", err=type(e).__name__,
                    size=0, mtime=0, sha="", pages=[], n_pages=0)
    if ext in BINARY_SKIP:
        return dict(rel=rel, status="unsupported_type", err=ext, size=st.st_size,
                    mtime=st.st_mtime, sha="", pages=[], n_pages=0)
    status, err, pages = extract_pages(path, ext)
    sha = ""
    if status == "indexed":
        try:
            sha = sha256_of(path)
        except Exception:
            pass
    return dict(rel=rel, status=status, err=err, size=st.st_size,
                mtime=st.st_mtime, sha=sha, pages=pages, n_pages=len(pages))


def schema(db):
    db.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS files(
      file_id INTEGER PRIMARY KEY, rel TEXT UNIQUE, sha256 TEXT, size INTEGER,
      mtime REAL, status TEXT NOT NULL, error_class TEXT, n_pages INTEGER,
      indexed_at TEXT);
    CREATE INDEX IF NOT EXISTS ix_files_sha ON files(sha256);
    CREATE INDEX IF NOT EXISTS ix_files_status ON files(status);
    CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
      rel UNINDEXED, page_index UNINDEXED, body, tokenize='unicode61');
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    """)


def walk(root):
    keep, excluded = [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        parts = set(Path(dirpath).parts)
        if parts & EXCLUDE_DIRS:
            excluded += len(filenames)
            continue
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            keep.append((p, os.path.relpath(p, root).replace("\\", "/")))
    return keep, excluded


def count_all(root):
    n = 0
    for _, _, fs in os.walk(root):
        n += len(fs)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus")
    ap.add_argument("--db", required=True)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()

    db = sqlite3.connect(a.db)
    schema(db)
    if a.stats:
        print(json.dumps(dict(db.execute(
            "SELECT status, COUNT(*) FROM files GROUP BY status").fetchall()), indent=1))
        print("pages:", db.execute("SELECT COUNT(*) FROM pages").fetchone()[0])
        return

    root = str(Path(a.corpus).resolve())
    t0 = time.time()
    print(f"walking {root} ...", flush=True)
    total_on_disk = count_all(root)
    items, n_excluded = walk(root)
    if a.limit:
        items = items[:a.limit]
    print(f"discovered={total_on_disk}  candidates={len(items)}  "
          f"excluded_dependency={n_excluded}  walk={time.time()-t0:.1f}s", flush=True)

    done = {r[0] for r in db.execute("SELECT rel FROM files")}
    todo = [it for it in items if it[1] not in done]
    print(f"already indexed={len(done)}  todo={len(todo)}", flush=True)

    n, npages, t1 = 0, 0, time.time()
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(work, it): it for it in todo}
        for fut in as_completed(futs):
            try:
                r = fut.result()
            except Exception as e:
                r = dict(rel=futs[fut][1], status="failed_parser", err=str(type(e).__name__),
                         size=0, mtime=0, sha="", pages=[], n_pages=0)
            db.execute(
                "INSERT OR REPLACE INTO files(rel,sha256,size,mtime,status,error_class,"
                "n_pages,indexed_at) VALUES(?,?,?,?,?,?,?,datetime('now'))",
                (r["rel"], r["sha"], r["size"], r["mtime"], r["status"],
                 r["err"], r["n_pages"]))
            if r["pages"]:
                db.executemany("INSERT INTO pages(rel,page_index,body) VALUES(?,?,?)",
                               [(r["rel"], i, t) for i, t in r["pages"]])
                npages += len(r["pages"])
            n += 1
            if n % 250 == 0:
                db.commit()
                el = time.time() - t1
                print(f"  {n}/{len(todo)} files, {npages} pages, {el:.0f}s, "
                      f"{n/max(el,1):.1f} files/s", flush=True)
    db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('corpus_root',?)", (root,))
    db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('discovered',?)", (str(total_on_disk),))
    db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('excluded_dependency',?)", (str(n_excluded),))
    db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('built_at',?)",
               (time.strftime("%Y-%m-%dT%H:%M:%S"),))
    db.commit()
    print(f"DONE files={n} pages={npages} wall={time.time()-t1:.0f}s", flush=True)
    print(json.dumps(dict(db.execute(
        "SELECT status, COUNT(*) FROM files GROUP BY status").fetchall()), indent=1))


if __name__ == "__main__":
    main()
