#!/usr/bin/env python
"""c_shelf_update_selftest.py -- an incremental update must equal a full rebuild.

This is the acceptance test for c_shelf_update.py, and the reason the merge can be
trusted: it builds the same tiny corpus twice, once by adding the last documents
incrementally to a shelf built from the first ones, and once by a full rebuild, then
compares documents, families, captions, vectors and the vector for each document.

If the mirrored primary/duplicate rule in c_shelf_update ever drifts from
c_shelf_build's, this fails.

The corpus is synthetic and small (24 documents) so the test runs in well under a
minute, but it is built through the real frozen builder and the real caption stages.

    python engine/bin/c_shelf_update_selftest.py     # exit 0 pass, 1 fail
"""
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import index_build as IB      # noqa: E402
import labpaths as L          # noqa: E402

PY = sys.executable
FAMS = ("Annual Budget Statement", "White Paper", "Fiscal Risks Statement",
        "Smart Book", "Non-development Volume I")
YEARS = (2023, 2024, 2025, 2026)


def make_corpus(db_path, docs):
    con = sqlite3.connect(str(db_path))
    IB.schema(con)
    for rel, sha, npages, title in docs:
        con.execute(
            "INSERT OR REPLACE INTO files(rel,sha256,size,mtime,status,error_class,n_pages,"
            "indexed_at) VALUES(?,?,?,?,?,?,?,datetime('now'))",
            (rel, sha, npages * 1000, 0.0, "indexed", "", npages))
        # The FIRST page carries a title, a Contents marker and a Table caption: the builder
        # harvests captions from these lines and the caption stages fail with NO_CAPTIONS if
        # a corpus has none, so a synthetic corpus needs them too. Page numbers start at
        # L.FIRST_PAGE_INDEX because that is what index_build writes, and the builder reads a
        # document's first three pages for its title -- a fixture numbered from 0 would have
        # no title pages at all after the numbering change.
        con.execute("INSERT INTO pages(rel,page_index,body) VALUES(?,?,?)",
                    (rel, L.FIRST_PAGE_INDEX,
                     "%s\nContents\n Table 1.1: Expenditure by category\n%s"
                     % (title, "filler words for the card catalog " * 12)))
        for p in range(L.FIRST_PAGE_INDEX + 1, npages + L.FIRST_PAGE_INDEX):
            body = "page %d of %s " % (p, title) + ("filler " * 30)
            if p % 2 == 0:
                body += "\n Table %d.%d: Receipts and expenditure summary" % (p, p)
            con.execute("INSERT INTO pages(rel,page_index,body) VALUES(?,?,?)",
                        (rel, p, body))
    con.commit()
    con.close()


def build_docs():
    """24 documents: five families over four years, two exact copies of one edition,
    and two editions with no fiscal year at all."""
    docs = []
    for fam in FAMS:
        for yy in YEARS:
            title = "%s %d-%02d" % (fam, yy, (yy + 1) % 100)
            rel = "%s/edition %d.pdf" % (fam.replace(" ", "_").lower(), yy)
            docs.append((rel, "sha-%d-%d" % (hash(fam) % 9999, yy), 3 + (yy % 3), title))
    original = docs[3]
    docs.append(("copies of edition/%s" % Path(original[0]).name,
                 original[1], original[2] + 5, original[3]))          # same sha, longer
    docs.append(("copies again/%s" % Path(original[0]).name,
                 original[1], original[2], original[3]))
    docs.append(("misc/annual report.pdf", "sha-nofy-1", 4, "Annual Report"))
    docs.append(("misc/audit notes.pdf", "sha-nofy-2", 4, "Audit Notes"))
    return docs


def build_full(db_path, out_dir):
    """A full build through the real CLI, not an import: build_cards leaves its read-only
    connection open (no close in the function), which on Windows makes the temp directory
    undeletable when it is called in-process."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([PY, "-u", str(BIN / "c_shelf_build.py"), "--db", str(db_path),
                        "--out", str(out), "--state", str(out / "_state")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("c_shelf_build failed: %s" % (r.stderr or "")[-600:])
    for script in ("c_caption_index.py", "c_caption_embed.py"):
        r = subprocess.run([PY, "-u", str(BIN / script), "--out-dir", str(out)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("%s failed: %s" % (script, (r.stderr or "")[-600:]))


def snapshot(out_dir):
    d = Path(out_dir)
    con = sqlite3.connect(str(d / "shelf.db"))
    docs = {}
    for r in con.execute("SELECT rel,title,family,fy_primary,n_pages,is_primary,"
                         "edition_key,n_dupes,dupes FROM docs"):
        docs[r[0]] = (r[1], r[2], r[3], r[4], r[5],
                      json.loads(r[6]) if r[6] else None, r[7],
                      json.loads(r[8]) if r[8] else [])
    fams = {}
    for r in con.execute("SELECT family,n_editions,n_members,fy_list FROM families"):
        fams[r[0]] = (r[1], r[2], json.loads(r[3]) if r[3] else [])
    caps = con.execute("SELECT COUNT(*) FROM captions").fetchone()[0]
    con.close()
    return docs, fams, caps


def meta_check(out_dir):
    """(duplicate_keys, card_vector_rows). shelf.db's meta has no primary key, so a writer
    that uses INSERT OR REPLACE silently grows duplicate rows and readers see a stale value
    -- exactly the bug an update pass must not reintroduce."""
    con = sqlite3.connect(str(Path(out_dir) / "shelf.db"))
    keys = [r[0] for r in con.execute("SELECT k FROM meta")]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    row = con.execute("SELECT v FROM meta WHERE k='card_vector_rows'").fetchone()
    con.close()
    return dupes, (int(row[0]) if row else None)


def vectors(out_dir):
    import numpy as np
    d = Path(out_dir)
    npy, ids = d / "cards.f16.npy", d / "cards_ids.jsonl"
    m = {}
    if npy.exists() and ids.exists():
        arr = np.load(str(npy)).astype("float32")
        for line in ids.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            m[rec["rel"]] = arr[rec["i"]]
    return m


def main():
    checks = []
    docs = build_docs()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        full_db, base_db = td / "full.db", td / "base.db"
        make_corpus(full_db, docs)
        head, tail = docs[:16], docs[16:]
        make_corpus(base_db, head)

        build_full(full_db, td / "full")
        build_full(base_db, td / "base")

        r = subprocess.run([PY, "-u", str(BIN / "c_shelf_update.py"),
                            "--db", str(full_db), "--out", str(td / "base")],
                           capture_output=True, text=True)
        checks.append(("incremental_update_exit_0", r.returncode == 0))
        if r.returncode != 0:
            print((r.stdout or "")[-1500:], (r.stderr or "")[-1500:])

        fd, ff, fc = snapshot(td / "full")
        id_, iff, ic = snapshot(td / "base")

        # the split must actually straddle an edition group, or the merge's
        # re-decision path is never exercised and this test proves little
        head_rels = {d[0] for d in head}
        twin_groups = [rel for rel in fd if fd[rel][6] > 0]
        checks.append(("split_straddles_a_duplicate_group",
                       bool(twin_groups) and any(r not in head_rels for r in twin_groups)))

        checks.append(("same_documents", set(fd) == set(id_)))
        diff = {r for r in fd if fd[r] != id_.get(r)}
        checks.append(("identical_document_fields", not diff))
        if diff:
            for rel in sorted(diff)[:4]:
                print("  %s\n    full %s\n    incr %s"
                      % (rel[:60], fd.get(rel), id_.get(rel)))
        checks.append(("identical_families", ff == iff))
        if ff != iff:
            for k in sorted(set(ff) | set(iff)):
                if ff.get(k) != iff.get(k):
                    print("  family %r\n    full %s\n    incr %s" % (k[:50], ff.get(k), iff.get(k)))
        checks.append(("same_caption_count", fc == ic))

        f_dup, f_rows = meta_check(td / "full")
        i_dup, i_rows = meta_check(td / "base")
        checks.append(("no_duplicate_meta_keys", not i_dup))
        checks.append(("vector_row_count_matches_docs", i_rows == len(id_)))

        fv, iv = vectors(td / "full"), vectors(td / "base")
        checks.append(("same_vector_count", len(fv) == len(iv)))
        worst, worst_rel = 1.0, None
        for rel, v in fv.items():
            o = iv.get(rel)
            if o is None:
                worst, worst_rel = -1.0, rel
                break
            num = float((v * o).sum())
            den = float((v * v).sum() ** 0.5 * (o * o).sum() ** 0.5)
            cos = num / den if den else 0.0
            if cos < worst:
                worst, worst_rel = cos, rel
        checks.append(("vectors_identical_for_every_document", worst > 0.9999))
        print("  lowest vector cosine %.6f (%s)" % (worst, (worst_rel or "-")[:50]))
        checks.append(("up_to_date_after_merge",
                       subprocess.run([PY, str(BIN / "c_shelf_update.py"),
                                       "--db", str(full_db), "--out", str(td / "base")],
                                      capture_output=True, text=True).stdout.startswith(
                                          "UP_TO_DATE")))

        # a shelf that is MOSTLY behind is a different corpus, not an update: refuse and let
        # the caller take the full-build path (setup_folder.py treats rc=6 that way)
        make_corpus(td / "small.db", docs[:4])
        build_full(td / "small.db", td / "small")
        r_small = subprocess.run([PY, str(BIN / "c_shelf_update.py"),
                                  "--db", str(full_db), "--out", str(td / "small")],
                                 capture_output=True, text=True)
        checks.append(("mostly_behind_refuses_with_6", r_small.returncode == 6))
        r_forced = subprocess.run([PY, str(BIN / "c_shelf_update.py"), "--force",
                                   "--db", str(full_db), "--out", str(td / "small")],
                                  capture_output=True, text=True)
        checks.append(("force_overrides_the_threshold", r_forced.returncode == 0))

        # and a reader must refuse while an update is in flight, because the vector files
        # and their id lists are replaced one after the other. --out IS the shelf directory
        # (the builder writes shelf.db straight into it), so the marker sits there too.
        mark = td / "base" / "UPDATE_IN_PROGRESS"
        mark.write_text("{}", encoding="utf-8")
        r_read = subprocess.run([PY, str(BIN / "c_shelf.py"), "--db", str(full_db),
                                 "--shelf", str(td / "base" / "shelf.db"),
                                 "coverage"], capture_output=True, text=True)
        mark.unlink()
        checks.append(("reader_refuses_mid_update",
                       r_read.returncode == 3 and "SHELF_UPDATE_IN_PROGRESS" in
                       (r_read.stdout + r_read.stderr)))
        r_read2 = subprocess.run([PY, str(BIN / "c_shelf.py"), "--db", str(full_db),
                                  "--shelf", str(td / "base" / "shelf.db"),
                                  "coverage"], capture_output=True, text=True)
        checks.append(("reader_works_again_after", r_read2.returncode == 0))

    n_pass = sum(1 for _, ok in checks if ok)
    for name, ok in checks:
        print("%-42s %s" % (name, "pass" if ok else "FAIL"))
    print("incremental shelf: %d/%d pass" % (n_pass, len(checks)))
    return 0 if n_pass == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
