#!/usr/bin/env python
"""c_shelf_build_v2.py - the shelf builder with the three Phase 9.2 structural fixes.

A copy of the frozen c_shelf_build.py. That file is a frozen instrument and is
neither run nor edited here; every recorded number must still reproduce from it.
This writes to its own OUT directory, so the v1 shelf is untouched.

The three changes, and nothing else (measured on shelf.db on 2026-09-15):

(a) BARE YEARS ARE NORMALISED. make_family already replaced a fiscal-year token
    like 2012-13 with {fy}, but left a bare 2019 alone, so 263 families carried a
    bare year in their name and a yearly report became one family per year. A
    standalone four-digit year now becomes {yr}, and a document whose title, path
    and first pages carry no fiscal-year token but do carry a bare year Y gets
    fy_primary = "Y" with fy_kind = "calendar" (new column, default "fiscal").

(b) FRAGMENT FAMILIES ARE MERGED. The family key comes from the first title-like
    line of page 0; when that line is a citation fragment, the same publication
    becomes a separate family. 51 families were another family's name with 1-3
    junk leading tokens, carrying 93 editions between them, so the same
    publication occupied four separate slots in the top ten of a `find`. A family
    whose token list is another family's with 1-3 extra LEADING tokens is folded
    into it. This is a structural suffix rule over the family strings themselves:
    no title, publication name or word list is written down here.

(c) THE PRIMARY IS THE CONSENSUS COPY, NOT THE SHORTEST PATH. The old key was
    (-n_pages, -size, len(rel), rel). In 166 of 1,332 multi-copy edition clusters
    the marked primary had fewer identical-hash siblings than another survivor,
    and in 153 the primary was a singleton while a sibling existed in >= 2 copies
    -- so the one-off file (a draft, an "old tables" export) won the `*` and
    became the file `find` told the model to open. The key is now
    (marker_penalty, -n_identical_copies, -n_pages, -size, len(rel), rel), where
    marker_penalty uses the EXISTING fixed MARKER_RE, unextended.

Writes:
  OUT/shelf.db            docs, captions, families, cards (FTS5), meta
  OUT/cards.f16.npy        L2-normalised float16 vectors, one row per docs row
  OUT/cards_ids.jsonl      {"i","rel"} mapping npy row -> docs.rel
  OUT/build_manifest.json  what this run did
  STATE/c_shelf_build_v2.json the build report

Read-only against the index; the only writes are under OUT and STATE.
"""
import argparse
import json
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labpaths as L  # noqa: E402

FY_RE = re.compile(r"\b(20[0-3]\d-\d\d)\b")
# (a) A standalone four-digit year. YEAR_RE.findall returns the whole match, so
# the group is non-capturing; `\b` on both sides keeps it off the tail of an
# already-substituted fiscal year and off longer digit runs.
YEAR_RE = re.compile(r"\b(?:19|20)\d\d\b")
CAP_RE = re.compile(
    r"^[ \t]*Table\s+[A-Za-z]?\.?\d+(?:\.\d+)*\s*[:.\-–]\s*.{3,160}$",
    re.MULTILINE,
)
CONTENTS_MARK_RE = re.compile(r"contents|list of tables", re.IGNORECASE)

# Fixed marker list (C_SHELF_FIRST_BUILD.md 1.1). Do not extend.
_MARKERS = [
    r"\(1\)", r"\(2\)", r"\(3\)", r"\(4\)", r"\(5\)", r"\(6\)", r"\(7\)", r"\(8\)", r"\(9\)",
    "copy", "draft", "final", "new", "old",
    "v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8", "v9",
    "rev", "revised", "updated", "use this one", "backup",
]
MARKER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:" + "|".join(_MARKERS) + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
PUNCT_KEEP_BRACE_RE = re.compile(r"[^a-z0-9{}]+")
WS_RE = re.compile(r"\s+")


def fy_tokens(text):
    return FY_RE.findall(text or "")


def _looks_like_title_line(line):
    """1.3 recovery: 'you may adjust how the title line is chosen' -- this narrows
    which line QUALIFIES as a title candidate, without touching the marker list.

    Two real failure shapes turned up in the Gate 1 sanity probe: a single-line
    JSON blob (an agent-transcript/audit-log file, e.g. `[{"tool":"profile",...}]`)
    and a comma-separated data header (a CSV survey extract, e.g.
    `hhid,round,province,district,...`). Both have >=3 regex-matched "alphabetic
    words" per the base rule, so both were passing as titles. Neither looks like
    a real document title: a real title is a short run of words; these are long
    runs of short tokens (JSON keys/values, or column names), regardless of
    whether the separator is a space or a comma. Token count via the SAME regex
    used to qualify a line (not str.split(), which would miss comma-joined CSV
    headers entirely) catches both, generically -- not tied to any word seen in
    this corpus.
    """
    if re.match(r"^\s*[\[{]", line):
        return False
    if len(re.findall(r"[A-Za-z0-9_]+", line)) > 25:
        return False
    return True


def extract_title(p0, filename_stem):
    for raw in (p0 or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if len(re.findall(r"[A-Za-z]{2,}", line)) >= 3 and _looks_like_title_line(line):
            return line[:160], "page0"
    stem = filename_stem.replace("_", " ").replace("-", " ")
    return stem[:160], "filename"


def make_family(title, fy_all):
    s = title.lower()
    for fy in fy_all:
        s = s.replace(fy.lower(), "{fy}")
    # (a) A bare calendar year is an edition marker exactly as a fiscal year is.
    # Leaving it in the family string made every annual edition of a yearly
    # report its own family. Runs after the fiscal-year pass so the second half
    # of an already-substituted 2012-13 is not caught.
    s = YEAR_RE.sub("{yr}", s)
    s = MARKER_RE.sub(" ", s)
    s = PUNCT_KEEP_BRACE_RE.sub(" ", s)
    s = WS_RE.sub(" ", s).strip()
    return s


def year_tokens(text):
    return YEAR_RE.findall(text or "")


def clean_filename_stem(stem):
    return stem.replace("_", " ").replace("-", " ")


def build_cards(db_path, out_dir, state_dir):
    t_start = time.time()
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    files_meta = {}
    for rel, sha256, size, n_pages in db.execute(
        "SELECT rel, sha256, size, n_pages FROM files WHERE status='indexed'"
    ):
        files_meta[rel] = {"sha256": sha256, "size": size, "n_pages": n_pages}
    n_files = len(files_meta)
    print(f"files_meta loaded: {n_files}")

    docs_raw = {}
    captions_raw = defaultdict(list)   # rel -> [(page_index, caption)]
    caption_seen = defaultdict(set)    # rel -> set(caption text) for dedup

    cur_rel = None
    p_bodies = {}          # page_index -> body, for page_index 0,1,2 of current doc
    contents_parts = []    # bodies of first-15 pages containing Contents/List of Tables
    page_count_seen = 0

    def flush(rel):
        if rel is None or rel not in files_meta:
            return
        meta = files_meta[rel]
        stem = Path(rel).stem
        p0 = p_bodies.get(0, "")
        p1 = p_bodies.get(1, "")
        p2 = p_bodies.get(2, "")
        title, title_source = extract_title(p0, stem)
        ordered_fy, seen_fy = [], set()
        for src in (title, rel, p0, p1, p2):
            for tok in fy_tokens(src):
                if tok not in seen_fy:
                    seen_fy.add(tok)
                    ordered_fy.append(tok)
        fy_primary = None
        fy_kind = "fiscal"
        for src in (title, rel, p0):
            toks = fy_tokens(src)
            if toks:
                fy_primary = toks[0]
                break
        if fy_primary is None:
            # (a) No fiscal-year token anywhere, but a bare year is still an
            # edition marker. Without this, every edition of a calendar-year
            # report clustered as fy_primary=None, which the primary rule below
            # treats as "every survivor is a primary" -- no edition walk at all.
            for src in (title, rel, p0, p1, p2):
                toks = year_tokens(src)
                if toks:
                    fy_primary = toks[0]
                    fy_kind = "calendar"
                    break
        family = make_family(title, ordered_fy)
        if len(family) < 4:
            family = make_family(clean_filename_stem(stem), ordered_fy)

        cap_list = captions_raw.get(rel, [])[:400]
        cap_text = "\n".join(c for _, c in cap_list)
        contents_text = "".join(contents_parts)[:6000]
        catalog = (contents_text + "\n" + cap_text).strip()
        head = (p0 + "\n" + p1)[:1500]
        ext = Path(rel).suffix.lower().lstrip(".")

        docs_raw[rel] = {
            "rel": rel, "title": title, "title_source": title_source,
            "family": family, "fy_primary": fy_primary, "fy_kind": fy_kind,
            "fy_all": ordered_fy,
            "n_pages": meta["n_pages"], "size": meta["size"], "sha256": meta["sha256"],
            "ext": ext, "catalog": catalog, "head": head,
        }

    t_scan = time.time()
    n_rows = 0
    for _id, c0, c1, c2 in db.execute(
        "SELECT id, c0, c1, c2 FROM pages_content ORDER BY id"
    ):
        n_rows += 1
        if c0 != cur_rel:
            flush(cur_rel)
            cur_rel = c0
            p_bodies = {}
            contents_parts = []
            page_count_seen = 0
        page_count_seen += 1
        if c1 in (0, 1, 2):
            p_bodies[c1] = c2
        if page_count_seen <= 15 and CONTENTS_MARK_RE.search(c2):
            contents_parts.append(c2)
        for m in CAP_RE.finditer(c2):
            cap = m.group(0).strip().rstrip("\r")
            seen = caption_seen[cur_rel]
            if cap not in seen and len(captions_raw[cur_rel]) < 400:
                seen.add(cap)
                captions_raw[cur_rel].append((c1, cap))
    flush(cur_rel)
    t_scan_done = time.time()
    print(f"page scan done: {n_rows} rows in {t_scan_done - t_scan:.1f}s, "
          f"{len(docs_raw)} docs built")

    # --- (b) merge fragment families ------------------------------------
    # A family whose token list is another family's with 1-3 extra LEADING
    # tokens is that family with a citation fragment stuck on the front. Fold it
    # in, but only into a family that is at least as populous, so a real
    # publication is never absorbed by a rarer one. Up to 3 passes, because a
    # fragment can itself be a fragment of a fragment.
    n_merges = 0
    n_members_moved = 0
    merge_log = []
    for _pass in range(3):
        fam_count = defaultdict(int)
        for rec in docs_raw.values():
            fam_count[rec["family"]] += 1
        remap = {}
        for fam, cnt in fam_count.items():
            toks = fam.split()
            if len(toks) < 4:
                continue
            for k in (1, 2, 3):
                if len(toks) - k < 3:
                    break
                g = " ".join(toks[k:])
                if g in fam_count and g != fam and fam_count[g] >= cnt:
                    remap[fam] = g
                    break
        # do not chain within a pass: a target that is itself being remapped
        # this pass keeps its members here and moves on the next pass
        remap = {f: g for f, g in remap.items() if g not in remap}
        if not remap:
            break
        for rec in docs_raw.values():
            if rec["family"] in remap:
                rec["family"] = remap[rec["family"]]
                n_members_moved += 1
        for f, g in remap.items():
            merge_log.append({"n_members": fam_count[f], "into_n_members": fam_count[g]})
        n_merges += len(remap)
    print(f"family merges: {n_merges} families folded, {n_members_moved} members moved")

    # --- clustering -----------------------------------------------------
    by_edition = defaultdict(list)
    for rel, rec in docs_raw.items():
        by_edition[(rec["family"], rec["fy_primary"])].append(rel)

    final_docs = {}
    n_multi_copy_clusters = [0]
    n_primary_changed = [0]
    for (family, fy_primary), rels in by_edition.items():
        by_hash = defaultdict(list)
        for rel in rels:
            by_hash[docs_raw[rel]["sha256"]].append(rel)
        survivors = []
        for _h, hrels in by_hash.items():
            if len(hrels) == 1:
                survivors.append((hrels[0], []))
            else:
                hrels_sorted = sorted(hrels, key=lambda r: (len(r), r))
                survivors.append((hrels_sorted[0], hrels_sorted[1:]))

        if fy_primary is None:
            primary_rel_set = {rel for rel, _ in survivors}
        else:
            # (c) Consensus primary. n_identical_copies is how many files in the
            # folder carry this survivor's sha256 -- the number of people who
            # kept the same bytes. A file the marker list flags (draft, old,
            # copy, v2 ...) goes last whatever its page count. The old key's
            # remaining terms stay as the tie-break so nothing else moves.
            def sort_key(item):
                rel, dupes = item
                rec = docs_raw[rel]
                marker_penalty = 1 if MARKER_RE.search(Path(rel).stem) else 0
                n_identical_copies = len(dupes) + 1
                return (marker_penalty, -n_identical_copies,
                        -rec["n_pages"], -rec["size"], len(rel), rel)

            def sort_key_v1(item):
                rel, _dupes = item
                rec = docs_raw[rel]
                return (-rec["n_pages"], -rec["size"], len(rel), rel)

            survivors_sorted = sorted(survivors, key=sort_key)
            primary_rel_set = {survivors_sorted[0][0]}
            if len(survivors) > 1:
                n_multi_copy_clusters[0] += 1
                if sorted(survivors, key=sort_key_v1)[0][0] != survivors_sorted[0][0]:
                    n_primary_changed[0] += 1

        for rel, dupes in survivors:
            rec = dict(docs_raw[rel])
            rec["is_primary"] = 1 if rel in primary_rel_set else 0
            rec["edition_key"] = json.dumps([family, fy_primary])
            rec["dupes"] = dupes
            rec["n_dupes"] = len(dupes)
            final_docs[rel] = rec

    t_cluster_done = time.time()
    n_dupes_total = sum(r["n_dupes"] for r in final_docs.values())

    # --- families ---------------------------------------------------------
    fam_members = defaultdict(list)
    for rel, rec in final_docs.items():
        fam_members[rec["family"]].append(rec)
    families = {}
    for family, recs in fam_members.items():
        fy_list = sorted({r["fy_primary"] for r in recs
                           if r["is_primary"] and r["fy_primary"]})
        families[family] = {
            "family": family, "n_editions": len(fy_list),
            "n_members": len(recs), "fy_list": fy_list,
        }

    # --- sanity probe -------------------------------------------------
    top_family = max(families.values(), key=lambda f: f["n_editions"], default=None)
    sanity = {}
    if top_family:
        recs = [r for r in fam_members[top_family["family"]]
                if r["is_primary"] and r["fy_primary"]]
        avg_pages = sum(r["n_pages"] for r in recs) / len(recs) if recs else 0
        sanity = {
            "family": top_family["family"], "n_editions": top_family["n_editions"],
            "avg_primary_pages": round(avg_pages, 1),
        }

    # --- write shelf.db -------------------------------------------------
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    shelf_path = out_dir / "shelf.db"
    if shelf_path.exists():
        shelf_path.unlink()
    sdb = sqlite3.connect(str(shelf_path))
    sdb.execute("""CREATE TABLE docs(
        rel TEXT PRIMARY KEY, title TEXT, title_source TEXT, family TEXT,
        fy_primary TEXT, fy_all TEXT, n_pages INTEGER, size INTEGER, sha256 TEXT,
        ext TEXT, is_primary INTEGER, edition_key TEXT, n_dupes INTEGER, dupes TEXT,
        fy_kind TEXT DEFAULT 'fiscal')""")
    sdb.execute("CREATE TABLE captions(rel TEXT, page_index INTEGER, caption TEXT)")
    sdb.execute("""CREATE TABLE families(
        family TEXT PRIMARY KEY, n_editions INTEGER, n_members INTEGER, fy_list TEXT)""")
    sdb.execute("""CREATE VIRTUAL TABLE cards USING fts5(
        rel UNINDEXED, title, family, catalog, head)""")
    sdb.execute("CREATE TABLE meta(k TEXT, v TEXT)")
    sdb.execute("CREATE INDEX idx_docs_family ON docs(family)")
    sdb.execute("CREATE INDEX idx_docs_edition ON docs(edition_key)")
    sdb.execute("CREATE INDEX idx_captions_rel ON captions(rel)")

    ordered_rels = sorted(final_docs.keys())
    for rel in ordered_rels:
        rec = final_docs[rel]
        sdb.execute(
            "INSERT INTO docs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec["rel"], rec["title"], rec["title_source"], rec["family"],
             rec["fy_primary"], json.dumps(rec["fy_all"]), rec["n_pages"],
             rec["size"], rec["sha256"], rec["ext"], rec["is_primary"],
             rec["edition_key"], rec["n_dupes"], json.dumps(rec["dupes"]),
             rec.get("fy_kind", "fiscal")))
        sdb.execute("INSERT INTO cards VALUES (?,?,?,?,?)",
                    (rec["rel"], rec["title"], rec["family"], rec["catalog"], rec["head"]))
        for page_index, caption in captions_raw.get(rel, []):
            sdb.execute("INSERT INTO captions VALUES (?,?,?)", (rel, page_index, caption))
    for family, frec in families.items():
        sdb.execute("INSERT INTO families VALUES (?,?,?,?)",
                    (family, frec["n_editions"], frec["n_members"], json.dumps(frec["fy_list"])))

    n_captions_total = sum(len(captions_raw.get(rel, [])) for rel in final_docs)
    built_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    meta_rows = [
        ("built_at", built_at),
        ("source_db", str(db_path)),
        ("source_db_size", str(Path(db_path).stat().st_size)),
        ("n_docs", str(len(final_docs))),
        ("n_families", str(len(families))),
        ("n_captions", str(n_captions_total)),
    ]
    for k, v in meta_rows:
        sdb.execute("INSERT INTO meta VALUES (?,?)", (k, v))
    sdb.commit()

    # --- embeddings -------------------------------------------------------
    # Checkpointed every 20 batches: an earlier run of this script was killed
    # by a session teardown mid-embedding with nothing recoverable, so this
    # resumes from the last checkpoint instead of re-embedding from zero.
    t_embed_start = time.time()
    from fastembed import TextEmbedding
    import hashlib
    import numpy as np

    model = TextEmbedding("BAAI/bge-small-en-v1.5")
    texts = [f"{final_docs[r]['title']} | {final_docs[r]['family']} | "
             f"{final_docs[r]['catalog'][:1200]}" for r in ordered_rels]
    n = len(texts)
    dim = 384
    B = 128
    rels_hash = hashlib.sha256("\n".join(ordered_rels).encode("utf-8")).hexdigest()
    ckpt_npy = out_dir / "cards.f16.partial.npy"
    ckpt_meta = out_dir / "cards.f16.partial.json"

    start_i = 0
    vecs = np.zeros((n, dim), dtype=np.float32)
    if ckpt_npy.exists() and ckpt_meta.exists():
        try:
            cm = json.loads(ckpt_meta.read_text(encoding="utf-8"))
            if cm.get("n") == n and cm.get("rels_hash") == rels_hash:
                partial = np.load(ckpt_npy).astype(np.float32)
                start_i = cm["done_upto"]
                vecs[:start_i] = partial[:start_i]
                print(f"resuming embedding from checkpoint at {start_i}/{n}")
        except Exception as e:
            print(f"checkpoint unusable ({e}), starting from 0")
            start_i = 0

    for i in range(start_i, n, B):
        batch = texts[i:i + B]
        for j, v in enumerate(model.embed(batch, batch_size=B)):
            v = np.asarray(v, dtype=np.float32)
            norm = np.linalg.norm(v)
            if norm > 0:
                v = v / norm
            vecs[i + j] = v
        done = min(i + B, n)
        if (i // B) % 10 == 0 or done == n:
            elapsed = time.time() - t_embed_start
            rate = done / elapsed if elapsed > 0 else 0
            print(f"embed progress {done}/{n} ({rate:.1f}/s, {elapsed:.0f}s elapsed)", flush=True)
            np.save(ckpt_npy, vecs.astype(np.float16))
            ckpt_meta.write_text(json.dumps({"n": n, "rels_hash": rels_hash, "done_upto": done}),
                                 encoding="utf-8")

    vecs16 = vecs.astype(np.float16)
    np.save(out_dir / "cards.f16.npy", vecs16)
    with open(out_dir / "cards_ids.jsonl", "w", encoding="utf-8") as fh:
        for i, rel in enumerate(ordered_rels):
            fh.write(json.dumps({"i": i, "rel": rel}, ensure_ascii=False) + "\n")
    if ckpt_npy.exists():
        ckpt_npy.unlink()
    if ckpt_meta.exists():
        ckpt_meta.unlink()
    t_embed_done = time.time()
    embed_rate = n / (t_embed_done - t_embed_start) if t_embed_done > t_embed_start else 0

    sdb.execute("INSERT INTO meta VALUES (?,?)", ("card_vector_rows", str(n)))
    sdb.commit()
    sdb.close()

    # --- report -------------------------------------------------------
    n_docs = len(final_docs)
    n_title_from_page0 = sum(1 for r in final_docs.values() if r["title_source"] == "page0")
    n_with_fy = sum(1 for r in final_docs.values() if r["fy_primary"])
    n_families = len(families)
    n_families_2plus = sum(1 for f in families.values() if f["n_editions"] >= 2)

    n_calendar = sum(1 for r in final_docs.values() if r.get("fy_kind") == "calendar")
    n_bare_year_families = sum(1 for f in families if "{yr}" in f)

    report = {
        "n_docs": n_docs,
        "n_docs_expected": n_files,
        "n_docs_matches_expected": n_docs == n_files,
        # the accounting identity: every indexed file is either a docs row or a
        # duplicate collapsed into one.
        "n_docs_plus_dupes": n_docs + n_dupes_total,
        "n_indexed_files": n_files,
        "accounting_identity_holds": (n_docs + n_dupes_total) == n_files,
        # the three Phase 9.2 changes, counted
        "n_families_merged": n_merges,
        "n_members_moved_by_merge": n_members_moved,
        "n_bare_year_families_normalised": n_bare_year_families,
        "n_docs_with_calendar_year_edition": n_calendar,
        "n_multi_copy_edition_clusters": n_multi_copy_clusters[0],
        "n_primary_changed": n_primary_changed[0],
        "n_title_from_page0": n_title_from_page0,
        "n_with_fy": n_with_fy,
        "n_families": n_families,
        "n_families_with_2plus_editions": n_families_2plus,
        "n_captions": n_captions_total,
        "n_dupes": n_dupes_total,
        "card_vector_rows": n,
        "sanity_probe": sanity,
        "timing_s": {
            "page_scan": round(t_scan_done - t_scan, 1),
            "clustering": round(t_cluster_done - t_scan_done, 1),
            "db_write": round(t_embed_start - t_cluster_done, 1),
            "embed": round(t_embed_done - t_embed_start, 1),
            "total": round(t_embed_done - t_start, 1),
        },
        "embed_rate_per_s": round(embed_rate, 2),
        "built_at": built_at,
    }
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "c_shelf_build_v2.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    (out_dir / "build_manifest.json").write_text(json.dumps({
        "db": str(db_path), "out": str(out_dir), "built_at": built_at,
        "n_docs": n_docs, "n_families": n_families,
    }, indent=1), encoding="utf-8")
    print(json.dumps(report, indent=1))
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--state", default=str(L.STATE))
    a = ap.parse_args()
    build_cards(a.db, a.out, a.state)


if __name__ == "__main__":
    main()
