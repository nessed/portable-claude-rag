#!/usr/bin/env python
"""c_shelf_update.py -- bring a built shelf up to date with its index, without re-embedding.

Why: every stage of setup_folder skips only when its output exists, so the shelf builds as
a unit. Adding one document to a large corpus therefore costs a full re-embed of every
document (measured on the 15,000-document fixture: 85 minutes for the card embed), when the
work actually required is one card and its captions.

How: the per-document semantics are NOT reimplemented here. The affected documents are
copied into a fragment index, the FROZEN builder (c_shelf_build.py) and the frozen caption
index/embed scripts are run over that fragment through their own CLIs, and this script folds
the fragment's artefacts into the existing shelf.

The one subtlety that makes this more than an append: a full build does not merely FLAG
duplicates, it omits them -- two documents with the same content hash become one `docs` row
and the other is recorded only in the survivor's `dupes` list. So a new document that lands
in an EXISTING edition group (same family and fiscal year) can push an existing document out
of `docs` entirely, and the mark for that group has to be re-decided. Therefore:

  * the fragment holds the missing documents, and if any of them share an edition group with
    documents already in the shelf it is rebuilt to include those group members as well
  * every document the fragment covers is REPLACED: its rows are removed from
    docs / captions / cards / captions_fts and from the card and caption vector files, then
    the fragment's rows are appended
  * the families table is rebuilt from docs (it is derived) and meta updated

Safety: an UPDATE_IN_PROGRESS marker is written before anything is touched. Between
rewriting cards.f16.npy and rewriting cards_ids.jsonl a reader would resolve an id to the
wrong row, so c_shelf refuses to answer while the marker exists. A crash leaves the marker
behind deliberately -- a visible "an update did not finish" beats a quietly half-replaced
shelf -- and re-running this script finishes the job, because what is missing is recomputed
from the index each time.

Equivalence with a full rebuild is the acceptance test: c_shelf_update_selftest.py builds the
same corpus both ways and compares documents, families, captions and every vector.

  python engine/bin/c_shelf_update.py --db <pages.db> --out <artefacts>/shelf
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import index_build as IB       # noqa: E402  (schema(), so a fragment has the right shape)
import labpaths as L           # noqa: E402

STAGES = ("c_caption_index.py", "c_caption_embed.py")


def _ro(db_path):
    return sqlite3.connect("file:%s?mode=ro" % Path(db_path).as_posix(), uri=True)


def indexed_rels(db_path):
    con = _ro(db_path)
    rels = [r for (r,) in con.execute("SELECT rel FROM files WHERE status='indexed'")]
    con.close()
    return rels


def shelf_rels(out_dir):
    """Every indexed document the shelf already accounts for: the `docs` rows PLUS the
    duplicates recorded in their `dupes` lists.

    A full build deliberately omits duplicate documents from `docs` (they exist only as
    names inside the survivor's dupes list), so comparing against `docs` alone would make
    every update run believe those documents were missing and re-add them each time.
    """
    p = Path(out_dir) / "shelf.db"
    if not p.exists():
        return None
    con = _ro(p)
    rels = {r for (r,) in con.execute("SELECT rel FROM docs")}
    for (dupes,) in con.execute("SELECT dupes FROM docs"):
        if dupes:
            try:
                rels.update(json.loads(dupes))
            except Exception:
                pass
    con.close()
    return rels


def shelf_groups(out_dir):
    """(family, fy_primary) -> [rel] for the group's documents, INCLUDING the duplicates
    it hides. A re-decided group has to be rebuilt with every same-hash member present,
    because which member survives depends on all of them."""
    con = _ro(Path(out_dir) / "shelf.db")
    groups = defaultdict(list)
    for rel, family, fy, dupes in con.execute(
            "SELECT rel, family, fy_primary, dupes FROM docs"):
        groups[(family, fy)].append(rel)
        if dupes:
            try:
                groups[(family, fy)].extend(json.loads(dupes))
            except Exception:
                pass
    con.close()
    return groups


def copy_subset(src_db, dst_db, rels):
    """A valid index holding exactly these documents, same schema, text copied verbatim.

    Filtering happens in Python rather than a WHERE IN (...) because the set can be larger
    than SQLite's bound-parameter limit on a large corpus.
    """
    want = set(rels)
    src, dst = _ro(src_db), sqlite3.connect(str(dst_db))
    IB.schema(dst)
    dst.executemany(
        "INSERT OR REPLACE INTO files(rel,sha256,size,mtime,status,error_class,n_pages,"
        "indexed_at) VALUES(?,?,?,?,?,?,?,?)",
        [r for r in src.execute("SELECT rel,sha256,size,mtime,status,error_class,n_pages,"
                                "indexed_at FROM files") if r[0] in want])
    dst.executemany(
        "INSERT INTO pages(rel,page_index,body) VALUES(?,?,?)",
        ((c0, c1, c2) for c0, c1, c2 in
         src.execute("SELECT c0,c1,c2 FROM pages_content") if c0 in want))
    for k, v in src.execute("SELECT k, v FROM meta"):
        dst.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, v))
    dst.commit()
    dst.close()
    src.close()


def build_fragment(db_path, rels, workdir, state_dir):
    """Run the frozen builder and the caption stages over just these documents.

    Run as subprocesses, not imported: c_shelf_build leaves its read-only connection open
    (no close in build_cards), and on Windows that makes the fragment directory undeletable
    for the rest of the process.
    """
    frag_db, frag_shelf = Path(workdir) / "frag.db", Path(workdir) / "frag_shelf"
    Path(workdir).mkdir(parents=True, exist_ok=True)
    copy_subset(db_path, frag_db, rels)
    r = subprocess.run([sys.executable, "-u", str(L.BIN / "c_shelf_build.py"),
                        "--db", str(frag_db), "--out", str(frag_shelf),
                        "--state", str(state_dir)], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("FRAGMENT_BUILD_FAILED\n%s" % (r.stderr or "")[-800:])
    for script in STAGES:
        r = subprocess.run([sys.executable, "-u", str(L.BIN / script),
                            "--out-dir", str(frag_shelf)], capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("FRAGMENT_STAGE_FAILED %s\n%s" % (script, (r.stderr or "")[-800:]))
    con = _ro(frag_shelf / "shelf.db")
    docs = {r[0]: (r[1], r[2]) for r in
            con.execute("SELECT rel, family, fy_primary FROM docs")}
    con.close()
    return frag_shelf, docs


def replace_into_shelf(out_dir, frag_dir, replaced):
    """Delete the replaced documents everywhere they appear in shelf.db, insert the
    fragment's rows, rebuild families and update meta."""
    sdb = sqlite3.connect(str(Path(out_dir) / "shelf.db"))
    fdb = _ro(Path(frag_dir) / "shelf.db")
    for rel in replaced:
        sdb.execute("DELETE FROM docs WHERE rel=?", (rel,))
        sdb.execute("DELETE FROM captions WHERE rel=?", (rel,))
        sdb.execute("DELETE FROM cards WHERE rel=?", (rel,))
    doc_cols = [d[1] for d in fdb.execute("PRAGMA table_info(docs)")]
    sdb.executemany("INSERT OR REPLACE INTO docs VALUES (%s)" % ",".join("?" * len(doc_cols)),
                    [tuple(r) for r in fdb.execute("SELECT * FROM docs")])
    sdb.executemany("INSERT INTO captions VALUES (?,?,?)",
                    fdb.execute("SELECT rel,page_index,caption FROM captions"))
    sdb.executemany("INSERT INTO cards VALUES (?,?,?,?,?)",
                    fdb.execute("SELECT rel,title,family,catalog,head FROM cards"))
    sdb.commit()

    # families is derived from docs; rebuild it rather than patch it.
    fam = defaultdict(list)
    for rel, family, fy, prim in sdb.execute(
            "SELECT rel, family, fy_primary, is_primary FROM docs"):
        fam[family].append((fy, prim))
    sdb.execute("DELETE FROM families")
    for family, members in fam.items():
        fy_list = sorted({fy for fy, prim in members if prim and fy})
        sdb.execute("INSERT INTO families VALUES (?,?,?,?)",
                    (family, len(fy_list), len(members), json.dumps(fy_list)))

    counts = {
        "n_docs": sdb.execute("SELECT COUNT(*) FROM docs").fetchone()[0],
        "n_families": sdb.execute("SELECT COUNT(*) FROM families").fetchone()[0],
        "n_captions": sdb.execute("SELECT COUNT(*) FROM captions").fetchone()[0],
    }
    # shelf.db's meta table has NO primary key (`CREATE TABLE meta(k TEXT, v TEXT)`), so
    # INSERT OR REPLACE on an existing key inserts a SECOND row instead of replacing it and
    # readers then see whichever comes first -- a stale count. Delete, then insert.
    for k, v in list(counts.items()) + [
            ("card_vector_rows", counts["n_docs"]),
            ("updated_at", time.strftime("%Y-%m-%dT%H:%M:%S"))]:
        sdb.execute("DELETE FROM meta WHERE k=?", (k,))
        sdb.execute("INSERT INTO meta(k,v) VALUES(?,?)", (k, str(v)))
    sdb.commit()
    sdb.close()
    fdb.close()
    return counts


def _marker(out_dir):
    """A file that names the update in progress.

    Between the moment cards.f16.npy gains rows and the moment cards_ids.jsonl is rewritten,
    a reader would resolve an id to the WRONG row -- a silently wrong vector. c_shelf
    therefore refuses to answer while this file exists, and a crash leaves it behind on
    purpose: a visible "an update did not finish" beats a quietly half-replaced shelf.
    """
    return Path(out_dir) / "shelf" / "UPDATE_IN_PROGRESS"


def replace_vectors(out_dir, frag_dir, replaced):
    """Drop the replaced documents' rows from the vector files, then append the fragment's.

    c_shelf resolves vectors through cards_ids.jsonl / captions_ids.jsonl, so the row order
    only has to agree with the ids file. Each file is written under a temp name and moved
    into place, so a reader can never see a half-written vector file.
    """
    import numpy as np
    out = {}
    for name in ("cards", "captions"):
        tf, ff = Path(out_dir) / (name + ".f16.npy"), Path(frag_dir) / (name + ".f16.npy")
        ti, fi = Path(out_dir) / (name + "_ids.jsonl"), Path(frag_dir) / (name + "_ids.jsonl")
        if not (ff.exists() and fi.exists()):
            continue
        frag = np.load(str(ff))
        frag_ids = [json.loads(l) for l in fi.read_text(encoding="utf-8").splitlines()]
        dropped = 0
        if tf.exists() and ti.exists():
            arr = np.load(str(tf))
            ids = [json.loads(l) for l in ti.read_text(encoding="utf-8").splitlines()]
            keep = [i for i, rec in enumerate(ids) if rec["rel"] not in replaced]
            dropped = len(ids) - len(keep)
            arr = arr[keep] if len(keep) != len(ids) else arr
            ids = [dict(rec, i=n) for n, rec in enumerate(ids[i] for i in keep)]
            base, off = arr, len(arr)
        else:
            base, ids, off = None, [], 0
        stacked = frag if base is None else np.vstack([base, frag])
        # np.save APPENDS ".npy" when the name does not already end in it, so the temp name
        # has to keep the suffix or os.replace looks for a file that was never written.
        tmp_npy = Path(str(tf)[:-4] + ".tmp.npy")
        tmp_ids = Path(str(ti) + ".tmp")
        np.save(str(tmp_npy), stacked)
        with open(tmp_ids, "w", encoding="utf-8") as fh:
            for rec in ids:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            for rec in frag_ids:
                fh.write(json.dumps(dict(rec, i=int(rec["i"]) + off),
                                    ensure_ascii=False) + "\n")
        os.replace(str(tmp_npy), str(tf))
        os.replace(str(tmp_ids), str(ti))
        out[name] = {"dropped": dropped, "appended": len(frag_ids), "total": len(stacked)}
    return out


def replace_caption_fts(out_dir, frag_dir, replaced):
    tf, ff = Path(out_dir) / "captions_fts.db", Path(frag_dir) / "captions_fts.db"
    if not (tf.exists() and ff.exists()):
        return None
    con = sqlite3.connect(str(tf))
    n_del = 0
    for rel in replaced:
        n_del += con.execute("DELETE FROM cap WHERE rel=?", (rel,)).rowcount
    con.execute("ATTACH DATABASE ? AS frag", (str(ff),))
    con.execute("INSERT INTO cap(rel,page_index,caption) "
                "SELECT rel,page_index,caption FROM frag.cap")
    con.commit()
    con.execute("DETACH DATABASE frag")
    total = con.execute("SELECT COUNT(*) FROM cap").fetchone()[0]
    con.close()
    return {"dropped": n_del, "total": total}


def run_update(a, out, missing, stale):
    t0 = time.time()
    print("missing=%d -- building a fragment for just those" % len(missing), flush=True)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        state_dir = Path(a.state) if a.state else td / "state"
        frag_shelf, frag_docs = build_fragment(a.db, missing, td / "first", state_dir)
        replaced = list(missing)
        # A new document can land in an edition group that already has members, in which case
        # the group's primary/duplicate marks have to be re-decided with the whole group
        # present -- so rebuild the fragment to include those members and replace the group
        # rather than append to it.
        existing = shelf_groups(out)
        overlap = {k for k, rels in existing.items()
                   if k in {(f, fy) for f, fy in frag_docs.values()}}
        if overlap:
            extra = [r for k in overlap for r in existing[k]]
            replaced = sorted(set(missing) | set(extra))
            print("  %d edition group(s) already have members -- fragment rebuilt over %d "
                  "documents" % (len(overlap), len(replaced)), flush=True)
            frag_shelf, frag_docs = build_fragment(a.db, replaced, td / "second", state_dir)
        else:
            print("  all new documents start new edition groups -- append only", flush=True)
        counts = replace_into_shelf(out, frag_shelf, replaced)
        vecs = replace_vectors(out, frag_shelf, replaced)
        fts = replace_caption_fts(out, frag_shelf, replaced)
    return {
        "missing_documents": len(missing),
        "replaced_documents": len(replaced),
        "edition_groups_rebuilt": len(overlap),
        "stale_in_shelf": len(stale),
        "total_s": round(time.time() - t0, 1),
        "shelf": counts,
        "vectors": vecs,
        "caption_fts": fts,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="the corpus's pages.db")
    ap.add_argument("--out", required=True, help="the artefacts dir holding shelf/")
    ap.add_argument("--state", default=None, help="state dir for the fragment report")
    ap.add_argument("--force", action="store_true",
                    help="update even when most of the corpus is newer than the shelf")
    a = ap.parse_args()

    out = Path(a.out).resolve()
    rels_index = indexed_rels(a.db)
    rels_shelf = shelf_rels(out)
    if rels_shelf is None:
        print("NO_SHELF %s -- run the full build first" % (out / "shelf.db"), file=sys.stderr)
        return 3
    missing = [r for r in rels_index if r not in rels_shelf]
    stale = [r for r in rels_shelf if r not in set(rels_index)]
    if not missing:
        print("UP_TO_DATE docs=%d stale_in_shelf=%d" % (len(rels_shelf), len(stale)))
        return 0

    # A shelf that is mostly behind is not an incremental update, it is a different corpus
    # (or a first build onto an inherited index). The merge would cost what a rebuild costs
    # and would keep rows for documents the index no longer has, so say so and let the
    # caller rebuild -- setup_folder.py treats this code as "take the full-build path".
    if len(missing) * 2 >= len(rels_index) and not a.force:
        print("SHELF_REBUILD_PREFERRED missing=%d indexed=%d -- more than half the corpus is "
              "newer than the shelf, so a full rebuild is simpler and leaves no stale rows. "
              "Pass --force to update incrementally anyway."
              % (len(missing), len(rels_index)), file=sys.stderr)
        return 6

    marker = _marker(out)
    if marker.exists():
        print("SHELF_UPDATE_IN_PROGRESS %s\n  a previous update did not finish. Re-running "
              "this script completes it (what is missing is recomputed from the index). "
              "Delete the marker only if you have rebuilt the shelf since." % marker,
              file=sys.stderr)
        return 5
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"pid": os.getpid(),
                                  "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                  "missing": len(missing)}), encoding="utf-8")
    try:
        result = run_update(a, out, missing, stale)
    finally:
        marker.unlink(missing_ok=True)

    print(json.dumps(result, indent=1))
    if stale:
        print("NOTE: %d documents are in the shelf but no longer indexed; they were left "
              "alone. Remove them from shelf.db if the index is authoritative." % len(stale))
    return 0


if __name__ == "__main__":
    sys.exit(main())
