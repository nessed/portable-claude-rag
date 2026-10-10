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
import argparse, glob, hashlib, json, os, re, sqlite3, subprocess, sys, tempfile, time, zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labpaths as L  # noqa: E402  (FIRST_PAGE_INDEX: the index's page numbering)


def _page_index(position):
    """The stored page_index for a document's Nth page (0-based `position`).

    The index's numbering lives in ONE constant (labpaths.FIRST_PAGE_INDEX) because it is
    stored -- in `pages`, in the shelf's `captions`, and inside every written note -- so a
    citation's `pN` and the PDF page a reader opens have to agree. Everything downstream
    (the shelf builders' title read, the OCR renderer's page number) derives from it.
    """
    return position + L.FIRST_PAGE_INDEX

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
# What the installer writes into the documents folder. The tool's own files, not
# corpus material: a root-level CLAUDE.md and the .claude/ directory are skipped
# by the walker. A CLAUDE.md in a subfolder is treated as real content, so a
# document folder that legitimately holds one still indexes it.
TOOL_OWNED_MD = "CLAUDE.md"
TOOL_OWNED_DIR = ".claude"
PDF_EXT = {".pdf"}
DOCX_EXT = {".docx", ".xlsx", ".pptx"}
BINARY_SKIP = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp", ".mp4",
    ".mp3", ".wav", ".zip", ".gz", ".tar", ".7z", ".rar", ".exe", ".dll",
    ".so", ".pyd", ".whl", ".pack", ".idx", ".bin", ".safetensors", ".pt",
    ".onnx", ".dta", ".parquet", ".db", ".sqlite", ".pdb", ".lib", ".obj",
}
MAX_BYTES = 300 * 1024 * 1024
PDF_TIMEOUT = 180            # floor budget, kept as the documented default
PDF_TIMEOUT_MAX = 1800       # ceiling: 30 min, so a genuinely stuck file still ends

# --- OCR of pages that have no text layer ------------------------------------
# Off unless PCR_OCR is set, so the package behaves exactly as before for anyone
# who does not ask for it. The original design note above says "no tesseract";
# that was true when it was written. On this host tesseract and pdftoppm are both
# installed, so the choice is now a flag rather than a limitation.
#
# Why it exists: index_build marks a file `indexed` as soon as ONE page yields
# text, so a mostly-scanned document looks fully indexed and its unreadable pages
# are invisible in COVERAGE. Measured on the Punjab corpus: one file extracts
# 6,073 characters from 47 pages and is reported as indexed.
OCR = os.environ.get("PCR_OCR", "").strip().lower() not in ("", "0", "off", "false")
OCR_DPI = 300
OCR_TIMEOUT = 300            # per page, one page per tesseract process
OCR_DOC_MIN_CHARS = 200      # chars/page below which the WHOLE doc is re-read
OCR_PAGE_MIN_CHARS = 200     # chars below which one page is re-read
OCR_MAX_PAGES = 5000         # stop discovery even if a document claims more


def _find_exe(env_var, name):
    """env var, then PATH, then the bare name (same order as labpaths.PDFTOTEXT)."""
    v = os.environ.get(env_var)
    if v:
        return v
    import shutil as _shutil
    return _shutil.which(name) or name


PDFTOPPM = _find_exe("PDFTOPPM_EXE", "pdftoppm")
PDFINFO = _find_exe("PDFINFO_EXE", "pdfinfo")
TESSERACT = _find_exe("TESSERACT_EXE", "tesseract")


def pdf_timeout(path):
    """pdftotext slows down under worker contention, not because the file is bad:
    at 6 workers on an 8-core box the largest volumes blew a flat 180s budget and
    were dropped as failed_timeout while COVERAGE still printed failed=0. Scale
    the floor with file size so the budget describes the work, not the weather."""
    try:
        mb = os.path.getsize(path) / 1e6
    except OSError:
        return PDF_TIMEOUT
    return int(min(PDF_TIMEOUT_MAX, max(PDF_TIMEOUT, 60 + 12 * mb)))


def _ocr_page(path, page_no, tmpdir):
    """Text of one page via pdftoppm + tesseract, or None when the page does not
    render (which happens when pdftotext's page numbering runs past the real end
    of a malformed document -- the Compendium case)."""
    stem = os.path.join(tmpdir, "pg")
    try:
        r = subprocess.run([PDFTOPPM, "-r", str(OCR_DPI), "-png", "-f", str(page_no),
                            "-l", str(page_no), str(path), stem],
                           capture_output=True, timeout=OCR_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    pngs = sorted(glob.glob(stem + "*.png"))
    if not pngs:
        return None
    try:
        t = subprocess.run([TESSERACT, pngs[0], "stdout"],
                           capture_output=True, timeout=OCR_TIMEOUT)
        return (t.stdout or b"").decode("utf-8", "replace") if t.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""
    finally:
        for f in pngs:
            try:
                os.remove(f)
            except OSError:
                pass


def _looks_like_junk(txt):
    """A CID-obfuscated page passes a character-count test but is not text.

    pdftotext emits the raw glyph codes instead of the glyphs, so a "readable"
    591-character page can be almost entirely control bytes. Measured on the
    Compendium: its junk pages run ~40% control characters, its real pages 0%.
    A character count alone therefore cannot decide whether a page needs OCR.
    """
    t = txt or ""
    if not t:
        return False
    bad = sum(1 for ch in t
              if (ord(ch) < 32 and ch not in "\n\r\t") or 0xE000 <= ord(ch) <= 0xF8FF)
    return bad > max(4, len(t) * 0.02)


def _pdf_page_count(path):
    """The PDF's own page count, or None when pdfinfo is not installed. Used to
    bound OCR so a document cannot be truncated silently by a render failure, and
    so phantom pages can be reported. Optional: the pack runs without it."""
    try:
        r = subprocess.run([PDFINFO, str(path)], capture_output=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    for line in (r.stdout or b"").decode("utf-8", "replace").splitlines():
        if line.startswith("Pages:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _ocr_whole_document(path, numbered=None):
    """OCR a document whose text layer is junk or absent.

    numbered is the pdftotext page list when there was some text: good pages keep
    their text and only the thin ones are re-read. Without it, pages are rendered
    from 1 upwards until one does not render, which is how the real page count is
    discovered without asking pdfinfo.

    Status stays `indexed` with error_class `ocr`, so the closed status enum and
    the accounting identity are untouched and the file is still findable by
    `select rel from files where error_class='ocr'`.
    """
    known = _pdf_page_count(path)
    pages, n_ocr, consecutive_missing = [], 0, 0
    with tempfile.TemporaryDirectory() as td:
        if numbered is None:
            # A page that will not render does not exist. With pdfinfo present the
            # loop is bounded by the real count; without it, stop after three
            # consecutive misses. Truncating a document silently is the failure
            # this pass exists to remove, so the bound is stated either way.
            i = 0
            while i < (known or OCR_MAX_PAGES):
                idx = _page_index(i)
                t = _ocr_page(path, L.pdf_page_number(idx), td)
                if t is None:
                    consecutive_missing += 1
                    if known is None and consecutive_missing >= 3:
                        break
                else:
                    consecutive_missing = 0
                    if t.strip():
                        pages.append((idx, t))
                        n_ocr += 1
                i += 1
        else:
            for i, txt in numbered:
                if len(txt.strip()) >= OCR_PAGE_MIN_CHARS and not _looks_like_junk(txt):
                    pages.append((i, txt))
                    continue
                t = _ocr_page(path, L.pdf_page_number(i), td)
                if t is None:
                    # pdftoppm cannot render it, so the PDF has no such page:
                    # pdftotext's own numbering ran past the end of a malformed
                    # document (81 rows stored for a 47-page file). Dropped rather
                    # than storing junk under a page number that does not exist.
                    continue
                if t.strip():
                    pages.append((i, t))
                    n_ocr += 1
                elif txt.strip():
                    pages.append((i, txt))   # tesseract found nothing: keep what there was
    if not pages:
        return "image_only_no_text", "ocr_no_text", []
    print(f"  OCR {os.path.basename(str(path))}: {n_ocr} pages read, {len(pages)} stored"
          + (f", PDF has {known}" if known else ""), flush=True)
    return "indexed", "ocr", pages


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
        budget = pdf_timeout(path)
        for attempt in (1, 2):
            try:
                p = subprocess.run([PDFTOTEXT, "-layout", "-enc", "UTF-8", str(path), "-"],
                                   capture_output=True, timeout=budget)
                break
            except subprocess.TimeoutExpired:
                if attempt == 2:
                    return "failed_timeout", "pdftotext_timeout", []
                budget *= 3        # contention is not a property of the file: retry, do not drop
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
        numbered = [(_page_index(i), t) for i, t in enumerate(pages)]
        readable = [(i, t) for i, t in numbered if t.strip()]
        if not readable:
            if OCR:
                return _ocr_whole_document(path)
            return "image_only_no_text", "", []
        if OCR and (sum(len(t) for _, t in readable) / len(readable)) < OCR_DOC_MIN_CHARS:
            return _ocr_whole_document(path, numbered)
        return "indexed", "", readable

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


def walk(root, exclude_top=()):
    """Candidate files under root, plus how many were excluded.

    exclude_top: names of top-level directories to leave out entirely. The paste-in layout
    puts the engine itself (venv, index, scripts) inside the documents folder, so the
    installer passes ("engine",). Their files are counted as excluded, not dropped, so the
    accounting identity discovered = indexed + failed + excluded still closes.
    """
    keep, excluded = [], 0
    root_abs = os.path.abspath(root)
    exclude_top = set(exclude_top or ())
    for dirpath, dirnames, filenames in os.walk(root):
        if exclude_top and os.path.abspath(dirpath) == root_abs:
            for d in list(dirnames):
                if d in exclude_top:
                    excluded += sum(len(fs) for _, _, fs in os.walk(os.path.join(dirpath, d)))
            dirnames[:] = [d for d in dirnames if d not in exclude_top]
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        parts = set(Path(dirpath).parts)
        if parts & EXCLUDE_DIRS:
            excluded += len(filenames)
            continue
        at_root = os.path.abspath(dirpath) == root_abs
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root).replace("\\", "/")
            # The two files this pack installs INTO the documents folder. Without
            # this the next walk counts them as corpus documents and they can be
            # retrieved and cited as sources (measured: discovered 28 in a folder
            # holding 26 documents). Excluded rather than dropped, so the coverage
            # identity discovered = indexed + failed + excluded still closes.
            if rel.split("/")[0] == TOOL_OWNED_DIR or (at_root and fn == TOOL_OWNED_MD):
                excluded += 1
                continue
            keep.append((p, rel))
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
    ap.add_argument("--exclude-top", action="append", default=[],
                    help="top-level directory name to leave out (repeatable); the "
                         "paste-in install passes `engine`")
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
    items, n_excluded = walk(root, exclude_top=a.exclude_top)
    if a.limit:
        items = items[:a.limit]
    print(f"discovered={total_on_disk}  candidates={len(items)}  "
          f"excluded_dependency={n_excluded}  walk={time.time()-t0:.1f}s", flush=True)

    # A file that failed is retried on the next build instead of being permanent.
    # Until now this was "rel not present in files", so a file dropped once (a
    # timeout under load, a truncated copy) could only be retried by --purge-ing
    # the whole index. Safe because a failed file contributed no page rows, so
    # INSERT OR REPLACE leaves nothing stale behind.
    RETRY_STATUS = ("failed_timeout", "failed_parser", "failed_corrupt",
                    "failed_permission", "zero_byte", "skipped_too_large")
    if OCR:
        # With OCR on, an unreadable scan is worth another attempt: it may now
        # yield text.
        RETRY_STATUS = RETRY_STATUS + ("image_only_no_text",)
    rows = list(db.execute("SELECT rel, status FROM files"))
    seen = {r[0]: r[1] for r in rows}
    todo = [it for it in items
            if it[1] not in seen or str(seen[it[1]]) in RETRY_STATUS]
    n_retry = sum(1 for it in items if str(seen.get(it[1])) in RETRY_STATUS)
    print(f"already indexed={len(seen)}  todo={len(todo)}  "
          f"retrying={n_retry}  ocr={'on' if OCR else 'off'}", flush=True)

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
