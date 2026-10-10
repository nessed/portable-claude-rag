#!/usr/bin/env python
"""c_shelf.py - build C, Stage 2. The front door onto the shelf built by
c_shelf_build.py: find, have, inside, tables, series, copies, exact, open,
note, notes, coverage, register.

Every subcommand accepts --db PATH --shelf DIR; otherwise the corpus root is
resolved by walking up from the cwd to a key in the shared registry
(%LOCALAPPDATA%\\retrieval-lab\\roots.json). See plans_fable/C_SHELF_FIRST_BUILD.md
Stage 2 for the exact command grammar and output format.

Importable in-process (c_offline_gate.py does this): every subcommand has a
`do_*` function returning a plain dict; the CLI's `main()` only formats and
prints. DB is opened read-only; shelf.db is ours and opened read-write so a
one-time page-range cache can be built into it on first use.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labpaths as L  # noqa: E402
from corpus_search import (content_words, coverage as _cs_coverage,
                           THIN_CHARS as _THIN_CHARS)  # noqa: E402

# Paste-in layout (2026-10-10): the registry lives inside the package (engine\state), so
# nothing is left on the machine outside the folder and the folder can be moved. The old
# machine-global registry is still READ as a fallback so existing lab installs resolve.
REG_PATH = Path(os.environ.get("RETRIEVAL_REGISTRY") or (L.STATE / "roots.json"))
LEGACY_REG_PATH = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "retrieval-lab" / "roots.json"
# Bump when the SHAPE of the coverage dict changes, so a cache written by an older
# build cannot hide a newly added field on an index that has not otherwise changed.
_COVERAGE_VERSION = 3   # 3 adds coverage()['thin_by_file']: a cache written before it would
#                         hide the per-file breakdown forever, because the index it
#                         describes has not changed and its stamp still matches.
FY_RE = re.compile(r"\b(20[0-3]\d-\d\d)\b")

_MODEL = None  # module-level singleton; loaded only when `find` needs vectors


def _model():
    global _MODEL
    if _MODEL is None:
        from fastembed import TextEmbedding
        _MODEL = TextEmbedding("BAAI/bge-small-en-v1.5")
    return _MODEL


# --------------------------------------------------------------------- #
# registry / root resolution
# --------------------------------------------------------------------- #
def _norm_root_key(p):
    return str(Path(p).resolve()).replace("\\", "/").lower()


def _read_registry_file(p):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _load_registry():
    """The merged view used to RESOLVE a root: legacy machine registry, then the package's
    own, which wins on a key clash."""
    reg = _read_registry_file(LEGACY_REG_PATH)
    reg.update(_read_registry_file(REG_PATH))
    return reg


def _save_registry(reg):
    REG_PATH.parent.mkdir(parents=True, exist_ok=True)
    REG_PATH.write_text(json.dumps(reg, indent=1), encoding="utf-8")


def _in_place_root(start):
    """Walk up from `start` for a folder that holds engine/artefacts/pages.db and the
    shelf: the paste-in layout, which needs no registry at all."""
    cur = Path(start).resolve()
    while True:
        db = cur / "engine" / "artefacts" / "pages.db"
        shelf = cur / "engine" / "artefacts" / "shelf" / "shelf.db"
        if db.exists() and shelf.exists():
            return cur, db, shelf
        if cur.parent == cur:
            return None
        cur = cur.parent


def do_register(root, db, shelf):
    # Write back only the package's own file. Loading the merged view here copied every
    # entry of the developer's machine-wide registry into the shipped folder (seen in
    # Test 2: nine lab paths inside the professor's engine/state/roots.json).
    reg = _read_registry_file(REG_PATH)
    key = _norm_root_key(root)
    entry = dict(reg.get(key, {}))
    entry["root"] = str(Path(root).resolve()).replace("\\", "/")
    entry["db"] = str(Path(db).resolve())
    entry["shelf"] = str(Path(shelf).resolve())
    reg[key] = entry
    _save_registry(reg)
    return {"ok": True, "key": key, "entry": entry}


def _resolve(explicit_db, explicit_shelf):
    if explicit_db and explicit_shelf:
        return None, explicit_db, explicit_shelf
    # The paste-in layout first: the index sits beside the documents, so the folder itself
    # says where it is and a registry entry (or its absence after a move) cannot mislead.
    hit = _in_place_root(Path.cwd())
    if hit:
        root, db, shelf = hit
        return root, explicit_db or str(db), explicit_shelf or str(shelf)
    reg = _load_registry()
    cur = Path.cwd().resolve()
    while True:
        key = _norm_root_key(cur)
        if key in reg:
            e = reg[key]
            return cur, explicit_db or e.get("db"), explicit_shelf or e.get("shelf")
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    print("NO_REGISTERED_ROOT", file=sys.stderr)
    sys.exit(2)


def _corpus_key(root_key_str):
    return hashlib.sha256(root_key_str.encode("utf-8")).hexdigest()[:12]


def _notes_dir(root):
    key = _norm_root_key(root) if root else "no-root"
    ck = _corpus_key(key)
    # Inside the package (engine/state/notes), not under %LOCALAPPDATA%: nothing is left
    # on the machine outside the folder. RETRIEVAL_NOTES_DIR overrides for the lab.
    base = Path(os.environ.get("RETRIEVAL_NOTES_DIR") or (L.STATE / "notes"))
    d = base / ck
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------- #
# context: open connections once per process
# --------------------------------------------------------------------- #
class Ctx:
    def __init__(self, db_path, shelf_path, root=None):
        self.db_path = db_path
        self.shelf_path = shelf_path
        self.root = root
        self.db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self.shelf = sqlite3.connect(str(shelf_path))
        self.rel_to_family = {}
        self.rel_to_row = {}
        for row in self.shelf.execute(
            "SELECT rel, title, family, fy_primary, n_pages, size, sha256, "
            "is_primary, edition_key, n_dupes, dupes FROM docs"
        ):
            (rel, title, family, fy_primary, n_pages, size, sha256,
             is_primary, edition_key, n_dupes, dupes) = row
            self.rel_to_family[rel] = family
            self.rel_to_row[rel] = {
                "rel": rel, "title": title, "family": family, "fy_primary": fy_primary,
                "n_pages": n_pages, "size": size, "sha256": sha256,
                "is_primary": is_primary, "edition_key": edition_key,
                "n_dupes": n_dupes, "dupes": json.loads(dupes) if dupes else [],
            }
        # `cards.rel` is FTS5 UNINDEXED -- a `WHERE rel=?` lookup is a full-table
        # scan every time. `find` used to do one such lookup per ranked family
        # (up to a couple hundred), which measured at ~14s warm on "budget".
        # Load once instead, and precompute the two other per-family/per-edition
        # groupings `find` needs so it never does an O(n_docs) scan per family.
        self.rel_to_catalog = dict(self.shelf.execute("SELECT rel, catalog FROM cards"))
        self.family_to_rels = {}
        self.edition_primary = {}  # edition_key -> primary rel
        for rel, row in self.rel_to_row.items():
            self.family_to_rels.setdefault(row["family"], []).append(rel)
            if row["is_primary"]:
                self.edition_primary[row["edition_key"]] = rel

    def coverage(self):
        # corpus_search.coverage() does SELECT COUNT(*) FROM pages -- a full
        # scan of the 1.2M-row FTS5 table every call. The corpus is read-only
        # for the life of this process, so compute it once.
        #
        # Phase 9.1.2: once per PROCESS is not enough. Every shelf command the
        # model runs is a fresh process, and `find` prints the COVERAGE line, so
        # the 7.4s scan was being paid on nearly every call. The corpus is
        # read-only for the life of an index, so the result is cached in
        # shelf.db's meta table alongside the source db's size and mtime; a
        # mismatch on either recomputes and rewrites. The printed line is
        # identical either way -- this caches a count, it changes no ranking.
        if not hasattr(self, "_coverage_cache"):
            self._coverage_cache = self._coverage_cached_or_compute()
        return self._coverage_cache

    def _db_stamp(self):
        try:
            st = os.stat(self.db_path)
            return "%d:%d" % (st.st_size, int(st.st_mtime))
        except OSError:
            return None

    def _coverage_cached_or_compute(self):
        stamp = self._db_stamp()
        if stamp:
            try:
                row = self.shelf.execute(
                    "SELECT v FROM meta WHERE k='coverage_cache'").fetchone()
                if row:
                    rec = json.loads(row[0])
                    # The version guards the SHAPE of the cached dict, not the
                    # index. A cache written before a field was added would
                    # silently hide that field forever, because the index it
                    # describes has not changed and the stamp still matches.
                    if rec.get("stamp") == stamp and rec.get("v") == _COVERAGE_VERSION:
                        return rec["coverage"]
            except Exception:
                pass
        cov = _cs_coverage(self.db)
        if stamp:
            try:
                self.shelf.execute("DELETE FROM meta WHERE k='coverage_cache'")
                self.shelf.execute(
                    "INSERT INTO meta(k, v) VALUES('coverage_cache', ?)",
                    (json.dumps({"stamp": stamp, "v": _COVERAGE_VERSION,
                                 "coverage": cov}),))
                self.shelf.commit()
            except Exception:
                pass  # a read-only shelf must still answer, just slowly
        return cov


_ctx_cache = {}


def _update_in_progress(shelf_path):
    """The marker c_shelf_update leaves for the duration of an update (see that file).

    Reading a shelf mid-update is not a stale-read, it is a WRONG-read: cards_ids.jsonl and
    cards.f16.npy are replaced one after the other, so between the two an id resolves to
    another document's vector. Refusing beats answering from a half-replaced shelf.
    """
    return Path(shelf_path).parent / "UPDATE_IN_PROGRESS"


def get_ctx(db_path, shelf_path, root=None):
    key = (str(db_path), str(shelf_path))
    if key not in _ctx_cache:
        _ctx_cache[key] = Ctx(db_path, shelf_path, root)
    return _ctx_cache[key]


# --------------------------------------------------------------------- #
# page-range cache, built once into shelf.db, so `inside`/`series` never
# need a full scan of the 1.2M-row page table (that scan costs ~4-5s per
# lookup without it -- see corpus_search.py; the pages_content shadow
# table stores one file's pages as a contiguous rowid block, so a single
# one-time sequential pass records (rel -> start_id, end_id) once).
# --------------------------------------------------------------------- #
def _ensure_page_ranges(ctx):
    """The rel -> rowid-range cache behind `open` and `inside`.

    Rebuilt whenever the index changes. Ranges are keyed by rowid, so retrying a
    file (or OCR-ing one) re-inserts its page rows at the end of the table and
    every cached range for that rel becomes wrong: `open` would then return
    another document's page, or nothing. The stamp is the same one the coverage
    cache uses, so both caches agree on what "the index changed" means.
    """
    stamp = ctx._db_stamp()
    has = ctx.shelf.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='page_ranges'"
    ).fetchone()
    if has:
        row = None
        try:
            row = ctx.shelf.execute(
                "SELECT v FROM meta WHERE k='page_ranges_stamp'").fetchone()
        except sqlite3.Error:
            row = None
        if row and row[0] == stamp:
            return
        ctx.shelf.execute("DROP TABLE page_ranges")
    print("(building page-range cache, one-time, ~1 min)", file=sys.stderr)
    ctx.shelf.execute("CREATE TABLE page_ranges(rel TEXT PRIMARY KEY, start_id INTEGER, end_id INTEGER)")
    # min/max rowid per rel, merged rather than one block per contiguous run: a
    # re-indexed file can leave rows in two places, and the range query filters on
    # both rel and page_index, so a wider range is correct -- just marginally slower.
    ranges = {}
    for id_, c0 in ctx.db.execute("SELECT id, c0 FROM pages_content ORDER BY id"):
        r = ranges.get(c0)
        if r is None:
            ranges[c0] = [id_, id_]
        else:
            r[1] = id_
    ctx.shelf.executemany("INSERT OR REPLACE INTO page_ranges VALUES (?,?,?)",
                          [(rel, a, b) for rel, (a, b) in ranges.items()])
    try:
        ctx.shelf.execute("DELETE FROM meta WHERE k='page_ranges_stamp'")
        ctx.shelf.execute("INSERT INTO meta(k, v) VALUES('page_ranges_stamp', ?)", (stamp,))
    except sqlite3.Error:
        pass
    ctx.shelf.commit()


def _load_doc_pages(ctx, rel):
    _ensure_page_ranges(ctx)
    row = ctx.shelf.execute("SELECT start_id, end_id FROM page_ranges WHERE rel=?", (rel,)).fetchone()
    if not row:
        return []
    start_id, end_id = row
    return ctx.db.execute(
        "SELECT c1, c2 FROM pages_content WHERE id BETWEEN ? AND ? AND c0=? ORDER BY id",
        (start_id, end_id, rel)).fetchall()


# --------------------------------------------------------------------- #
# find
# --------------------------------------------------------------------- #
def _lex_search(ctx, query, limit):
    words = content_words(query)
    if not words:
        return []
    m = " OR ".join(f'"{w}"' for w in words)
    try:
        rows = ctx.shelf.execute(
            "SELECT rel FROM cards WHERE cards MATCH ? "
            "ORDER BY bm25(cards, 3.0, 3.0, 2.0, 1.0) LIMIT ?", (m, limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r[0] for r in rows]


def _vec_search(ctx, query, limit):
    import numpy as np
    if not hasattr(ctx, "_vecs"):
        ctx._vecs = np.load(Path(ctx.shelf_path).parent / "cards.f16.npy").astype("float32")
        ctx._vec_ids = [json.loads(l)["rel"] for l in
                        (Path(ctx.shelf_path).parent / "cards_ids.jsonl").read_text(encoding="utf-8").splitlines()]
    qv = np.asarray(next(iter(_model().embed([query]))), dtype="float32")
    n = np.linalg.norm(qv)
    if n > 0:
        qv = qv / n
    scores = ctx._vecs @ qv
    top = np.argsort(-scores)[:limit]
    return [ctx._vec_ids[i] for i in top]


def _rrf_fuse(rank_lists, k=60):
    from collections import defaultdict
    scores = defaultdict(float)
    for lst in rank_lists:
        for rank, rel in enumerate(lst, start=1):
            scores[rel] += 1.0 / (k + rank)
    return scores


def _family_words(family):
    return re.sub(r"\s+", " ", family.replace("{fy}", " ")).strip()


def _why_lines(catalog, title, qwords):
    lines = [l.strip() for l in (catalog or "").splitlines() if l.strip()]
    qw = set(qwords)
    scored = []
    for l in lines:
        overlap = len(set(content_words(l)) & qw)
        if overlap > 0:
            scored.append((overlap, l))
    scored.sort(key=lambda x: -x[0])
    top = [l for _, l in scored[:2]]
    return top if top else [title]


def _editions_for_family(ctx, family):
    rows = [ctx.rel_to_row[rel] for rel in ctx.family_to_rels.get(family, ())]
    primaries_fy = sorted([r for r in rows if r["is_primary"] and r["fy_primary"]],
                           key=lambda r: r["fy_primary"])
    n_copies = sum(1 for r in rows if not r["is_primary"])
    if primaries_fy:
        return primaries_fy, n_copies, "fy"
    singletons = [r for r in rows if r["is_primary"]]
    return singletons, n_copies, "singleton"


# --------------------------------------------------------------------- #
# caption channel (experiment E, 2026-09-15)
#
# A table's caption line is the shortest honest description of what the table
# holds. B2 (F42) showed it is a strong page-level signal inside a document
# already known to be the right one. E asks the harder question: is it also a
# DOCUMENT-level signal at corpus scale -- can "which publication" be answered
# by searching 89,380 caption lines instead of 12,760 catalog cards?
# --------------------------------------------------------------------- #
def _label_words(query):
    """The query reduced to the words a caption would plausibly print: content
    words, minus fiscal-year tokens, minus the trajectory scaffolding words.

    The two exclusion rules are imported rather than restated, so there is
    exactly one definition of each in the repository and no chance of quietly
    widening either. The filler set used to be imported from c_offline_gate --
    deferred to call time, because that module imports this one. It now lives in
    trajectory_filler.py, which imports nothing and opens nothing, so the
    portable package can carry it without carrying a script that reads the
    answer key (Plan E Phase 3.1). Same set, same behaviour.
    """
    from trajectory_filler import _TRAJECTORY_FILLER
    stripped = FY_RE.sub(" ", query or "")
    return [w for w in content_words(stripped) if w not in _TRAJECTORY_FILLER]


def _cap_db(ctx):
    if not hasattr(ctx, "_capdb"):
        p = Path(ctx.shelf_path).parent / "captions_fts.db"
        ctx._capdb = sqlite3.connect(f"file:{p}?mode=ro", uri=True) if p.exists() else None
    return ctx._capdb


def _cap_search(ctx, query, limit):
    """Lexical caption channel. Returns (families in first-seen order,
    ranked [(rel, page_index), ...]).

    All words first: a caption is a label, so requiring every one of them is
    what tells a table ABOUT the subject apart from one that merely mentions
    it. Below 20 hits that is too strict to rank with, so it falls back to any
    word -- the same all-then-any shape `inside` already uses on page bodies.
    """
    db = _cap_db(ctx)
    if db is None:
        return [], []
    words = _label_words(query)
    if not words:
        return [], []

    def run(match):
        try:
            return db.execute(
                "SELECT rel, page_index FROM cap WHERE cap MATCH ? "
                "ORDER BY bm25(cap) LIMIT ?", (match, limit)).fetchall()
        except sqlite3.OperationalError:
            return []

    rows = run(" AND ".join('"%s"' % w for w in words))
    if len(rows) < 20:
        rows = run(" OR ".join('"%s"' % w for w in words))

    families, seen = [], set()
    pages = []
    for rel, page_index in rows:
        pages.append((rel, page_index))
        fam = ctx.rel_to_family.get(rel)
        if fam and fam not in seen:
            seen.add(fam)
            families.append(fam)
    return families, pages


def _cap_vec_search(ctx, query, limit):
    """Dense caption channel (E2). Same label-word string as the lexical one,
    cosine over the caption embeddings."""
    import numpy as np
    base = Path(ctx.shelf_path).parent
    if not hasattr(ctx, "_capvecs"):
        npy, idsf = base / "captions.f16.npy", base / "captions_ids.jsonl"
        if not (npy.exists() and idsf.exists()):
            ctx._capvecs, ctx._capvec_ids = None, None
        else:
            ctx._capvecs = np.load(npy).astype("float32")
            ctx._capvec_ids = [json.loads(l) for l in
                               idsf.read_text(encoding="utf-8").splitlines()]
    if ctx._capvecs is None:
        return [], []
    words = _label_words(query)
    if not words:
        return [], []
    qv = np.asarray(next(iter(_model().embed([" ".join(words)]))), dtype="float32")
    n = np.linalg.norm(qv)
    if n > 0:
        qv = qv / n
    scores = ctx._capvecs @ qv
    top = np.argsort(-scores)[:limit]

    families, seen = [], set()
    pages = []
    for i in top:
        rec = ctx._capvec_ids[int(i)]
        rel = rec["rel"]
        pages.append((rel, rec["page_index"]))
        fam = ctx.rel_to_family.get(rel)
        if fam and fam not in seen:
            seen.add(fam)
            families.append(fam)
    return families, pages


def _rels_first_seen(pages):
    """[(rel, page_index), ...] -> the distinct rels in first-seen order."""
    out, seen = [], set()
    for rel, _ in pages:
        if rel not in seen:
            seen.add(rel)
            out.append(rel)
    return out


def _fam_rank_from_doc_scores(ctx, doc_scores):
    """Collapse a doc-level score map to a family-level one: a family scores
    what its single best-scoring document scores (unchanged from the original
    fusion), and carries that document as its representative."""
    fam_scores, fam_best_doc = {}, {}
    for rel, score in doc_scores.items():
        fam = ctx.rel_to_family.get(rel)
        if not fam:
            continue
        if fam not in fam_scores or score > fam_scores[fam]:
            fam_scores[fam] = score
            fam_best_doc[fam] = rel
    return fam_scores, fam_best_doc


def do_find(ctx, queries, pool=200, fusion="rrf", caption_channel=None):
    """Returns ALL fused families, ranked best-first (not sliced to any k) --
    the CLI printer shows the top --k, the offline gate checks rank within
    whatever depth it needs (measurement uses 50).

    fusion="rrf" (default, unchanged): one reciprocal-rank fusion over every
    query's lexical and vector lists at once, so a family's score is the sum of
    its reciprocal ranks everywhere it appeared.

    fusion="best" (2026-09-15, experiment C): each query is fused on its own
    into its own family ranking; a family is then scored by its BEST rank
    across the queries, ties broken by how many queries returned it at all and
    then by summed per-query RRF score. The point is that summation lets five
    queries that half-found a family outvote one query that found it first,
    which F41 measured as costing three questions their own inputs already had.
    No tunable constant is introduced: k=60 is the same one the rrf rule uses.

    caption_channel (2026-09-15, experiment E) adds the caption line as its own
    retrieval channel: "lex" gives every query a third ranked list from the
    caption FTS index, "lex+vec" a fourth from caption embeddings. It is added
    per query, so both fusion rules see it the same way.
    """
    if fusion not in ("rrf", "best"):
        raise ValueError("unknown fusion: %r" % (fusion,))
    if caption_channel not in (None, "lex", "lex+vec"):
        raise ValueError("unknown caption_channel: %r" % (caption_channel,))

    per_query_lists = []
    for q in queries:
        lists = [_lex_search(ctx, q, pool), _vec_search(ctx, q, pool)]
        # The caption channel ranks CAPTIONS; fusion happens in document
        # space, so a caption list enters as the documents those captions
        # sit in, in first-seen order. Handing over the family list here
        # instead silently contributes nothing at all -- rel_to_family
        # drops every key it does not recognise as a rel, which is how
        # the first E1 run reproduced the baseline to the last question.
        if caption_channel in ("lex", "lex+vec"):
            lists.append(_rels_first_seen(_cap_search(ctx, q, pool)[1]))
        if caption_channel == "lex+vec":
            lists.append(_rels_first_seen(_cap_vec_search(ctx, q, pool)[1]))
        per_query_lists.append(lists)

    if fusion == "rrf":
        all_lists = [lst for lists in per_query_lists for lst in lists]
        doc_scores = _rrf_fuse(all_lists, k=60)
        fam_scores, fam_best_doc = _fam_rank_from_doc_scores(ctx, doc_scores)
        ranked = sorted(fam_scores.items(), key=lambda x: -x[1])
        n_docs_considered = len(doc_scores)
    else:
        best_rank, n_found, sum_rrf = {}, {}, {}
        fam_best_doc, docs_seen = {}, set()
        for lists in per_query_lists:
            doc_scores = _rrf_fuse(lists, k=60)
            docs_seen.update(doc_scores)
            q_fam_scores, q_fam_doc = _fam_rank_from_doc_scores(ctx, doc_scores)
            q_ranked = sorted(q_fam_scores.items(), key=lambda x: -x[1])
            for rank, (fam, score) in enumerate(q_ranked, start=1):
                n_found[fam] = n_found.get(fam, 0) + 1
                sum_rrf[fam] = sum_rrf.get(fam, 0.0) + score
                if fam not in best_rank or rank < best_rank[fam]:
                    best_rank[fam] = rank
                    fam_best_doc[fam] = q_fam_doc[fam]
        order = sorted(best_rank.items(),
                       key=lambda x: (x[1], -n_found[x[0]], -sum_rrf[x[0]]))
        ranked = [(fam, sum_rrf[fam]) for fam, _ in order]
        n_docs_considered = len(docs_seen)

    qwords = []
    for q in queries:
        qwords.extend(content_words(q))

    families_out = []
    for fam, score in ranked:
        best_rel = fam_best_doc[fam]
        best_row = ctx.rel_to_row[best_rel]
        catalog = ctx.rel_to_catalog.get(best_rel, "")
        editions, n_copies, kind = _editions_for_family(ctx, fam)
        ek = best_row["edition_key"]
        primary_rel = best_rel if best_row["is_primary"] else ctx.edition_primary.get(ek, best_rel)
        families_out.append({
            "family": fam, "score": score,
            "editions": [{"fy": r.get("fy_primary"), "rel": r["rel"], "n_pages": r["n_pages"]}
                         for r in editions],
            "n_editions": len(editions), "n_copies_hidden": n_copies, "kind": kind,
            "why": _why_lines(catalog, best_row["title"], qwords),
            "best_rel": best_rel, "primary_rel": primary_rel,
            "evidence_rels": [r["rel"] for r in editions] + [best_rel],
        })

    return {
        "families": families_out,
        "receipt": {"queries": len(queries), "lex": pool, "vec": pool,
                    "fusion": fusion, "caption": caption_channel or "off",
                    "families": len(ranked), "docs_considered": n_docs_considered},
        "coverage": ctx.coverage(),
        "qwords": qwords,
    }


def _compact_line(i, f):
    """One family on one line, in the card shape Experiment H used
    (c_llm_select_gate.build_cards). Built from shelf fields only."""
    eds = f["editions"]
    fys = [e["fy"] for e in eds if e.get("fy")]
    if fys:
        span = ("%d editions %s-%s" % (len(fys), min(fys), max(fys))
                if len(fys) > 1 else "1 edition %s" % fys[0])
    else:
        rel = f.get("primary_rel") or f.get("best_rel") or ""
        ext = Path(rel).suffix.lower().lstrip(".") or "file"
        span = "single file, %s" % ext
    why = " | ".join(w.strip() for w in (f.get("why") or [])[:2] if w and w.strip())
    why = re.sub(r"\s+", " ", why)[:120]
    return "#%d  %s | %s | %s" % (i, _family_words(f["family"]), span, why)


def _print_find_compact(res, n, question):
    """Phase 9.3.1. The model was being shown twelve candidates; offline, the gold
    publication is in the fused top 10 on 10/17 but the top 50 on 14/17, and
    Experiment H (F60, F63) measured that a model shown 100 one-line cards ranks
    the gold in its own top 10 on 11-14/17 -- better than any statistical
    reranker we tried. H was a separate headless call and never shipped. This is
    the same mechanism at zero extra calls: show more of the list, in the session
    that is already running, one line each."""
    fams = res["families"][:n]
    for i, f in enumerate(fams, start=1):
        print(_compact_line(i, f))
    print('expand: find "%s" --show i,j,k   (same flags as this call)' % question)
    r = res["receipt"]
    print(f"RECEIPT queries={r['queries']} lex=[{r['lex']}] vec=[{r['vec']}] "
          f"families={r['families']} docs_considered={r['docs_considered']} "
          f"shown={len(fams)}")
    _print_coverage(res["coverage"])


def _print_find_show(res, indices):
    """The verbose entries for chosen indices only, same format as `find`."""
    fams = res["families"]
    for i in indices:
        if i < 1 or i > len(fams):
            print("#%d  (no such rank; the list held %d families)" % (i, len(fams)))
            continue
        _print_find_entry(i, fams[i - 1], res["qwords"])
    r = res["receipt"]
    print(f"RECEIPT queries={r['queries']} lex=[{r['lex']}] vec=[{r['vec']}] "
          f"families={r['families']} docs_considered={r['docs_considered']} "
          f"shown={len(indices)}")
    _print_coverage(res["coverage"])


def _print_find_entry(i, f, qwords):
    print(f"#{i}  family: {f['family']}")
    eds = f["editions"]
    parts = [f"{e['fy'] or Path(e['rel']).name}*({e['n_pages']}p)" for e in eds]
    if len(parts) > 6:
        shown = parts[:3] + ["…"] + parts[-2:]
    else:
        shown = parts
    print(f"    editions: {' '.join(shown)}   "
          f"[{f['n_editions']} editions, {f['n_copies_hidden']} copies not shown]")
    why = " | ".join(f'"{w}"' for w in f["why"])
    print(f"    why: {why}")
    terms = " ".join(qwords[:6])
    fam_words = _family_words(f["family"])
    # Name the primary copy, and say plainly that the others exist: on v2 the
    # primary is the consensus copy, and a session that opens a hidden one-off
    # instead is reading a draft (Session Log A.5.2).
    hidden = ""
    if f.get("n_copies_hidden"):
        hidden = ('   (%d other copies hidden; copies "%s" lists them)'
                  % (f["n_copies_hidden"], f["primary_rel"]))
    print(f'    open with: inside "{f["primary_rel"]}" "{terms}"   or   '
          f'series "<row words>" --family "{fam_words}"{hidden}')


def _print_find(res, k):
    for i, f in enumerate(res["families"][:k], start=1):
        print(f"#{i}  family: {f['family']}")
        eds = f["editions"]
        parts = [f"{e['fy'] or Path(e['rel']).name}*({e['n_pages']}p)" for e in eds]
        if len(parts) > 6:
            shown = parts[:3] + ["…"] + parts[-2:]
        else:
            shown = parts
        print(f"    editions: {' '.join(shown)}   "
              f"[{f['n_editions']} editions, {f['n_copies_hidden']} copies not shown]")
        why = " | ".join(f'"{w}"' for w in f["why"])
        print(f"    why: {why}")
        terms = " ".join(res["qwords"][:6])
        fam_words = _family_words(f["family"])
        print(f'    open with: inside "{f["primary_rel"]}" "{terms}"   or   '
              f'series "<row words>" --family "{fam_words}"')
    r = res["receipt"]
    print(f"RECEIPT queries={r['queries']} lex=[{r['lex']}] vec=[{r['vec']}] "
          f"families={r['families']} docs_considered={r['docs_considered']}")
    _print_coverage(res["coverage"])


def _print_coverage(cov):
    # Every failed_* status counts, not just two of them. index_build returns
    # failed_timeout when pdftotext runs out of budget under worker contention;
    # the old two-key sum hid that, so a build that dropped its largest volumes
    # still printed failed=0 and the number was believed. Keep the COVERAGE token
    # and the field order: c_selftest greps for the token.
    by = cov["by_status"]
    failed_by = {k: v for k, v in sorted(by.items()) if k.startswith("failed_")}
    failed = sum(failed_by.values())
    line = (f"COVERAGE indexed={by.get('indexed', 0)} "
            f"image_only_no_text={by.get('image_only_no_text', 0)} "
            f"failed={failed} unsupported={by.get('unsupported_type', 0)} "
            f"pages={cov['addressable_pages']}")
    if failed:
        line += " failed_by={%s}" % ", ".join("%s:%d" % kv for kv in failed_by.items())
    # Pages that were stored but carry no citable text. A file with a thin text
    # layer is reported as `indexed` by index_build (one readable page is enough),
    # so this is the only place its unreadable pages are visible at all.
    thin = cov.get("thin_pages")
    if thin:
        line += f" pages_with_no_readable_text={thin}"
        # Which files? A count alone is not actionable: 23 unreadable pages spread over one
        # big scan and 23 spread over 23 documents call for different responses (re-OCR one
        # file vs check the whole corpus). Top offenders only, so the line stays readable;
        # the full list is in `coverage --json`.
        worst = cov.get("thin_by_file") or []
        if worst:
            top = ", ".join("%s %d/%d" % (Path(w["rel"]).name[:28], w["thin_pages"],
                                          w["pages"]) for w in worst[:5])
            more = "" if len(worst) <= 5 else " (+%d more)" % (len(worst) - 5)
            line += f" worst=[{top}{more}]"
    print(line)


# --------------------------------------------------------------------- #
# have
# --------------------------------------------------------------------- #
def _unreadable_matching_name(ctx, words):
    if not words:
        return 0
    lw = [w.lower() for w in words]
    n = 0
    for (rel,) in ctx.db.execute(
        "SELECT rel FROM files WHERE status='image_only_no_text' OR status LIKE 'failed_%'"
    ):
        name = Path(rel).name.lower()
        if all(w in name for w in lw):
            n += 1
    return n


def do_have(ctx, words_str, fy=None):
    words = content_words(words_str)
    unreadable = _unreadable_matching_name(ctx, words)
    if not words:
        return {"families": [], "no_family_matches": True, "words": words_str,
                "unreadable": unreadable}

    # Tiered, most-precise-first (same cascade shape as corpus_search.py):
    # a "have" lookup is a catalog lookup for a NAMED publication, so an exact
    # phrase match confined to the title column is tried before falling back
    # to a bag-of-words score across title+family. Restricting the phrase tier
    # to title-only matters: this corpus also holds audit/log files whose body
    # is a JSON blob echoing a real file's path (e.g. a "profile" record
    # naming the path it processed), and those get long, noisy `family` text
    # that out-scores a real short title on raw bm25 term frequency if
    # `family` is included in this first tier.
    rows = []
    phrase = words_str.strip().replace('"', '""')
    if phrase:
        m_phrase = '{title} : "' + phrase + '"'
        try:
            rows = ctx.shelf.execute(
                "SELECT rel FROM cards WHERE cards MATCH ? "
                "ORDER BY bm25(cards, 3.0, 0, 0, 0) LIMIT 200", (m_phrase,)).fetchall()
        except sqlite3.OperationalError:
            rows = []
    if not rows:
        m_and = "{title family} : (" + " ".join(f'"{w}"' for w in words) + ")"
        try:
            rows = ctx.shelf.execute(
                "SELECT rel FROM cards WHERE cards MATCH ? "
                "ORDER BY bm25(cards, 3.0, 3.0, 2.0, 1.0) LIMIT 200", (m_and,)).fetchall()
        except sqlite3.OperationalError:
            rows = []
    if not rows:
        m_or = "{title family} : (" + " OR ".join(f'"{w}"' for w in words) + ")"
        try:
            rows = ctx.shelf.execute(
                "SELECT rel FROM cards WHERE cards MATCH ? "
                "ORDER BY bm25(cards, 3.0, 3.0, 2.0, 1.0) LIMIT 200", (m_or,)).fetchall()
        except sqlite3.OperationalError:
            rows = []
    fam_order = []
    seen = set()
    for (rel,) in rows:
        fam = ctx.rel_to_family.get(rel)
        if fam and fam not in seen:
            seen.add(fam)
            fam_order.append(fam)
    if not fam_order:
        return {"families": [], "no_family_matches": True, "words": words_str,
                "unreadable": unreadable}

    families_out = []
    for fam in fam_order[:8]:
        fy_list = sorted({r["fy_primary"] for r in ctx.rel_to_row.values()
                           if r["family"] == fam and r["is_primary"] and r["fy_primary"]})
        families_out.append({"family": fam, "fy_list": fy_list})

    result = {"families": families_out, "no_family_matches": False,
              "words": words_str, "unreadable": unreadable}

    if fy is not None:
        best = families_out[0]
        fy_list = best["fy_list"]
        if fy in fy_list:
            row = next(r for r in ctx.rel_to_row.values()
                       if r["family"] == best["family"] and r["fy_primary"] == fy and r["is_primary"])
            result["fy_check"] = {"status": "EDITION_PRESENT", "fy": fy,
                                   "rel": row["rel"], "n_pages": row["n_pages"]}
        else:
            before = max((f for f in fy_list if f < fy), default=None)
            after = min((f for f in fy_list if f > fy), default=None)
            result["fy_check"] = {"status": "NO_EDITION_FOR", "fy": fy,
                                   "family": best["family"],
                                   "nearest_before": before, "nearest_after": after,
                                   "fy_list": fy_list}
    return result


def _print_have(res):
    if res["no_family_matches"]:
        print(f'NO_FAMILY_MATCHES "{res["words"]}"')
        print(f"UNREADABLE_FILES_WITH_MATCHING_NAME={res['unreadable']}")
        return
    for f in res["families"]:
        print(f"family: {f['family']}   fy_list: {f['fy_list']}")
    if "fy_check" in res:
        c = res["fy_check"]
        if c["status"] == "EDITION_PRESENT":
            print(f"EDITION_PRESENT {c['fy']} {c['rel']} ({c['n_pages']}p)")
        else:
            print(f'NO_EDITION_FOR {c["fy"]} in "{c["family"]}"; '
                  f'nearest: {c["nearest_before"]}, {c["nearest_after"]}; '
                  f'editions held: {c["fy_list"]}')
    print(f"UNREADABLE_FILES_WITH_MATCHING_NAME={res['unreadable']}")


# --------------------------------------------------------------------- #
# inside
# --------------------------------------------------------------------- #
def _best_line(body, words):
    lines = body.splitlines()
    best_line, best_n = "", -1
    lw = [w.lower() for w in words]
    for line in lines:
        low = line.lower()
        n = sum(1 for w in lw if w in low)
        if n > best_n:
            best_n, best_line = n, line
    return " ".join(best_line.split())[:200]


def _caption_hit_pages(ctx, rel, words):
    """2026-09-14, experiment B2. Pages of `rel` whose harvested table caption
    contains EVERY query content word. Substring match on the lowercased
    caption, same content_words() normalisation the body tier uses -- a
    caption is a short label, so requiring all of them is the point: it is
    what tells "Table 4.2: <subject>" apart from a prose page that merely
    mentions the subject. Returns (set_of_page_indices, {page: caption})."""
    if not words:
        return set(), {}
    lw = [w.lower() for w in words]
    pages, captions = set(), {}
    for page_index, caption in ctx.shelf.execute(
            "SELECT page_index, caption FROM captions WHERE rel=?", (rel,)):
        low = (caption or "").lower()
        if all(w in low for w in lw):
            pages.add(page_index)
            if page_index not in captions:
                captions[page_index] = caption
    return pages, captions


def _caption_hit_pages_labelled(ctx, rel, query, body_by_page):
    """B2b, 2026-09-15. Two changes to the B2 caption match, both aimed at
    the same measured fact: a question's fiscal year is printed in the
    table BODY, as a column heading, and almost never in the caption, so
    requiring the caption to carry it (which B2 effectively does, since the
    year survives content_words) throws the caption away on exactly the
    questions that name a year.

    1. The caption must contain every LABEL word -- the query minus fiscal
       year tokens and minus trajectory filler, the same reduction the
       corpus-scale caption channel uses.
    2. If the query carries a fiscal year, a caption-hit page is kept only
       if that year appears in the page body -- the year selects the column,
       the caption selects the table.

    If the year filter empties the list the unfiltered caption ordering is
    returned instead, so the channel can never do worse than having no year.
    Returns (pages, {page: caption}, n_dropped_by_year_filter)."""
    words = _label_words(query)
    if not words:
        return set(), {}, 0
    lw = [w.lower() for w in words]
    pages, captions = set(), {}
    for page_index, caption in ctx.shelf.execute(
            "SELECT page_index, caption FROM captions WHERE rel=?", (rel,)):
        low = (caption or "").lower()
        if all(w in low for w in lw):
            pages.add(page_index)
            if page_index not in captions:
                captions[page_index] = caption
    m = FY_RE.search(query or "")
    if not m or not pages:
        return pages, captions, 0
    fy = m.group(1)
    kept = {pi for pi in pages if fy in (body_by_page.get(pi) or "")}
    if not kept:
        return pages, captions, 0
    return kept, {pi: captions[pi] for pi in kept if pi in captions}, len(pages) - len(kept)


def _caption_vector_store(ctx):
    """(npy handle, {rel: [(npy_row, page_index), ...]}) or (None, None).

    Built once per process. The rel -> rows map is cached beside the npy as
    captions_by_rel.json because parsing 89k jsonl lines costs ~1s and every
    shelf command is a fresh process."""
    if hasattr(ctx, "_capvec"):
        return ctx._capvec
    ctx._capvec = (None, None)
    try:
        import numpy as np
        d = Path(ctx.shelf_path).parent
        npy, ids = d / "captions.f16.npy", d / "captions_ids.jsonl"
        if not (npy.exists() and ids.exists()):
            return ctx._capvec
        cache = d / "captions_by_rel.json"
        by_rel = None
        if cache.exists() and cache.stat().st_mtime >= ids.stat().st_mtime:
            try:
                by_rel = json.loads(cache.read_text(encoding="utf-8"))
            except Exception:
                by_rel = None
        if by_rel is None:
            by_rel = {}
            with open(ids, "r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    r = json.loads(line)
                    by_rel.setdefault(r["rel"], []).append([r["i"], r["page_index"]])
            try:
                cache.write_text(json.dumps(by_rel), encoding="utf-8")
            except OSError:
                pass
        ctx._capvec = (np.load(str(npy), mmap_mode="r"), by_rel)
    except Exception:
        ctx._capvec = (None, None)
    return ctx._capvec


def _stored_caption_vectors(ctx, rel, rows_all):
    """Unit-normalised float32 vectors for this rel's NON-BLANK captions, in the
    same order `rows` has them, or None if alignment cannot be proven.

    Alignment is positional: c_caption_embed.py embedded every caption row in
    rowid order, one npy row each, so this rel's stored rows must have exactly
    the same page_index sequence as its table rows read in rowid order. If they
    do not, the npy is stale relative to the shelf and the caller re-embeds."""
    mat, by_rel = _caption_vector_store(ctx)
    if mat is None:
        return None
    ent = by_rel.get(rel)
    if not ent or len(ent) != len(rows_all):
        return None
    if [e[1] for e in ent] != [pi for pi, _c in rows_all]:
        return None
    keep = [ent[j][0] for j, (_pi, c) in enumerate(rows_all) if (c or "").strip()]
    if not keep:
        return None
    try:
        import numpy as np
        v = np.asarray(mat[keep, :], dtype="float32")
        norms = np.linalg.norm(v, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return v / norms
    except Exception:
        return None


def _caption_hit_pages_dense(ctx, rel, query, body_by_page):
    """B2c, 2026-09-15 pm. B2/B2b match a caption by requiring every label word
    to appear in it, and F48 measured what that costs: on every year-asking
    question NO caption in the correct document contains the question's subject
    words at all. The words are simply different words -- a tax listed under its
    statutory name, a series under its official title. That is a vocabulary gap,
    and a lexical matcher cannot cross it however the match is phrased.

    So rank this document's own captions by cosine against the query's label
    words instead of testing them for word containment. The cut is the
    document's OWN median caption score plus a floor of the top 3, so no
    threshold is tuned against the questions: a document whose captions are all
    equally unlike the query still yields its three best, and a document with a
    clear winner yields the half that beats its own middle.

    Returns (pages, {page: caption}, n_dropped_by_year_filter)."""
    import numpy as np
    words = _label_words(query)
    if not words:
        return set(), {}, 0
    # ORDER BY rowid so the row order is the one c_caption_embed.py wrote the
    # vectors in; without it the stored rows cannot be aligned.
    rows_all = ctx.shelf.execute(
        "SELECT page_index, caption FROM captions WHERE rel=? ORDER BY rowid",
        (rel,)).fetchall()
    rows = [(pi, c or "") for pi, c in rows_all if (c or "").strip()]
    if not rows:
        return set(), {}, 0

    qv = np.asarray(next(iter(_model().embed([" ".join(words)]))), dtype="float32")
    n = np.linalg.norm(qv)
    if n > 0:
        qv = qv / n

    # Phase 9.1.3: captions.f16.npy already holds a vector for every caption in
    # the shelf, written by c_caption_embed.py. Re-embedding a 496-page Survey's
    # 144 captions on every `inside` call cost ~1.3s on top of the model load for
    # a result the file on disk already had. Read the document's rows out of the
    # npy instead; only the query is embedded. Falls back to embedding whenever
    # the stored rows cannot be proven to line up with the table rows, so any
    # shelf without caption vectors (a fresh v2 build, say) still answers.
    cvs = _stored_caption_vectors(ctx, rel, rows_all)
    if cvs is None:
        cvs = np.asarray(list(_model().embed([c for _pi, c in rows])), dtype="float32")
        norms = np.linalg.norm(cvs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        cvs = cvs / norms
    scores = cvs @ qv

    order = list(np.argsort(-scores))
    med = float(np.median(scores))
    keep = [i for i in order if float(scores[i]) >= med]
    for i in order[:3]:
        if i not in keep:
            keep.append(i)
    pages = {rows[i][0] for i in keep}
    captions = {rows[i][0]: rows[i][1] for i in keep}

    m = FY_RE.search(query or "")
    if not m or not pages:
        return pages, captions, 0
    fy = m.group(1)
    kept = {pi for pi in pages if fy in (body_by_page.get(pi) or "")}
    if not kept:
        return pages, captions, 0
    return kept, {pi: captions[pi] for pi in kept if pi in captions}, len(pages) - len(kept)


def do_inside(ctx, rel, terms, k=8, caption_channel=None):
    """caption_channel=None is the original behaviour, unchanged.

    "first": pages whose caption matches every query word are ranked ahead of
    everything else, ordered among themselves by their body BM25 rank; a
    caption page the body ranking never returned goes after those; the rest of
    the body ranking follows. Each hit carries `via`.

    "rrf": reciprocal-rank fusion of the caption list (ordered by number of
    matching query words, then page index) with the body list."""
    row = ctx.rel_to_row.get(rel)
    n_pages = row["n_pages"] if row else None
    pages = _load_doc_pages(ctx, rel)
    if not pages:
        return {"rel": rel, "n_pages": n_pages, "tier": "none", "hits": [],
                "n_caption_hits": 0}

    mem = sqlite3.connect(":memory:")
    mem.execute("CREATE VIRTUAL TABLE p USING fts5(page_index UNINDEXED, body)")
    mem.executemany("INSERT INTO p VALUES (?,?)", pages)
    mem.commit()

    words = content_words(terms)
    # the caption channel reorders the body ranking, so it needs to see more of
    # it than the k rows the caller wants back; the final list is still cut to k.
    lim = k if caption_channel is None else max(k * 5, 50)
    tier = "any"
    rows = []
    if words:
        if len(words) <= 4:
            m = '"' + terms.replace('"', '""') + '"'
            tier = "phrase"
        else:
            m = " ".join(f'"{w}"' for w in words)
            tier = "all"
        try:
            rows = mem.execute(
                "SELECT page_index, body, bm25(p) FROM p WHERE p MATCH ? ORDER BY bm25(p) LIMIT ?",
                (m, lim)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if not rows and words:
            m2 = " OR ".join(f'"{w}"' for w in words)
            try:
                rows = mem.execute(
                    "SELECT page_index, body, bm25(p) FROM p WHERE p MATCH ? ORDER BY bm25(p) LIMIT ?",
                    (m2, lim)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            tier = "any"
    hits = []
    for page_index, body, score in rows:
        hits.append({"page_index": page_index, "score": round(score, 3),
                     "line": _best_line(body, words)})

    if caption_channel is None:
        return {"rel": rel, "n_pages": n_pages,
                "tier": tier if words else "none", "hits": hits}

    n_year_dropped = 0
    if caption_channel == "dense_first":
        body_by_page = {pi: body for pi, body in pages}
        cap_pages, cap_text, n_year_dropped = _caption_hit_pages_dense(
            ctx, rel, terms, body_by_page)
    elif caption_channel == "first_label":
        body_by_page = {pi: body for pi, body in pages}
        cap_pages, cap_text, n_year_dropped = _caption_hit_pages_labelled(
            ctx, rel, terms, body_by_page)
    else:
        cap_pages, cap_text = _caption_hit_pages(ctx, rel, words)
    body_pages = {h["page_index"] for h in hits}
    for h in hits:
        h["via"] = "caption" if h["page_index"] in cap_pages else "body"

    if caption_channel in ("first", "first_label", "dense_first"):
        cap_with_body = [h for h in hits if h["page_index"] in cap_pages]
        cap_only = [{"page_index": pi, "score": None, "via": "caption",
                     "line": " ".join((cap_text.get(pi) or "").split())[:200]}
                    for pi in sorted(cap_pages - body_pages)]
        rest = [h for h in hits if h["page_index"] not in cap_pages]
        final = cap_with_body + cap_only + rest
    elif caption_channel == "rrf":
        # caption list ordered by how many query words the caption carries,
        # then page index; fused with the body list at the usual k=60.
        lw = [w.lower() for w in words]
        def _n_words(pi):
            low = (cap_text.get(pi) or "").lower()
            return sum(1 for w in lw if w in low)
        cap_order = sorted(cap_pages, key=lambda pi: (-_n_words(pi), pi))
        body_order = [h["page_index"] for h in hits]
        scores = {}
        for lst in (cap_order, body_order):
            for r, pi in enumerate(lst, start=1):
                scores[pi] = scores.get(pi, 0.0) + 1.0 / (60 + r)
        by_page = {h["page_index"]: h for h in hits}
        final = []
        for pi in sorted(scores, key=lambda x: (-scores[x], x)):
            if pi in by_page:
                final.append(by_page[pi])
            else:
                final.append({"page_index": pi, "score": None, "via": "caption",
                              "line": " ".join((cap_text.get(pi) or "").split())[:200]})
    else:
        raise ValueError("unknown caption_channel: %r" % (caption_channel,))

    return {"rel": rel, "n_pages": n_pages, "tier": tier if words else "none",
            "hits": final[:k], "n_caption_hits": len(cap_pages),
            "n_caption_pages_dropped_by_year": n_year_dropped,
            "caption_channel": caption_channel}


def _print_inside(res):
    print(f"INSIDE {res['rel']} ({res['n_pages']}p) tier={res['tier']}")
    for h in res["hits"]:
        print(f"  p{h['page_index']}  {h['score']}  | {h['line']}")


# --------------------------------------------------------------------- #
# tables
# --------------------------------------------------------------------- #
def do_tables(ctx, rel, grep=None):
    rows = ctx.shelf.execute(
        "SELECT page_index, caption FROM captions WHERE rel=? ORDER BY page_index", (rel,)).fetchall()
    if grep:
        g = grep.lower()
        rows = [r for r in rows if g in r[1].lower()]
    return {"rel": rel, "captions": [{"page_index": p, "caption": c} for p, c in rows]}


def _pdf_path(ctx, rel):
    """The PDF on disk for an indexed rel, or None if it cannot be found.

    Every other command answers from the index, which is why the corpus can move without
    the index breaking. This one needs the actual file, because the index holds a page's
    TEXT LAYER and its caption lines -- not a table's cells as cells.
    """
    root = getattr(ctx, "root", None)
    if not root:
        return None
    direct = Path(root) / rel
    if direct.exists():
        return direct
    # ponytail: one walk of the corpus tree, only when the direct join misses (a rel whose
    # separators or case differ from the tree on disk). Cache rel -> path if this ever gets
    # called in a loop.
    name = Path(rel).name.lower()
    for cand in Path(root).rglob("*"):
        if cand.is_file() and cand.name.lower() == name:
            return cand
    return None


def do_table(ctx, rel, page_index, max_rows=60):
    """Read the table on a page from the PDF's own layout, cell by cell.

    `tables` lists the table CAPTIONS the index found; `open` shows the page's text layer,
    which lays a table out as loose lines with the columns run together. When the question
    is the value in a particular cell, this reads the grid: pdfplumber's line-based
    detection needs the file on disk, so it works only while the corpus is there.

    The cells are NOT a citation. The note guard checks a quote against the index's page
    text, and a cell read from the layout may be absent from that text -- so evidence still
    has to come from open + note, and the guard is right to refuse a cell the text layer
    does not contain.
    """
    out = {"rel": rel, "page_index": page_index, "tables": [], "n_tables": 0}
    path = _pdf_path(ctx, rel)
    if path is None:
        out["error"] = "PDF_NOT_ON_DISK"
        out["hint"] = ("the index answers without the corpus, but a table has to be read "
                       "from the file; the corpus root for this index is %r"
                       % getattr(ctx, "root", None))
        return out
    try:
        import pdfplumber
    except ImportError:
        out["error"] = "PDFPLUMBER_MISSING"
        out["hint"] = "pip install pdfplumber -- or use `open` for the text layer"
        return out
    # pdfplumber's page list is 0-based; the index's numbering lives in labpaths.
    idx = L.pdf_page_number(page_index) - 1
    with pdfplumber.open(str(path)) as pdf:
        out["n_pages_in_pdf"] = len(pdf.pages)
        if idx < 0 or idx >= len(pdf.pages):
            out["error"] = "PAGE_OUT_OF_RANGE"
            return out
        page = pdf.pages[idx]
        raw = page.extract_tables() or []
    for t in raw:
        rows = [] if t is None else t
        clean = [[" ".join((c or "").split())[:120] for c in row] for row in rows]
        clean = [r for r in clean if any(r)]
        if clean:
            out["tables"].append({"n_rows": len(clean),
                                  "n_cols": max(len(r) for r in clean),
                                  "rows": clean[:max_rows]})
    out["n_tables"] = len(out["tables"])
    out["pdf_path"] = str(path)
    return out


def _print_table(res):
    if res.get("error"):
        print(f"TABLE_ERROR {res['error']}  {res['rel']} p{res['page_index']}")
        if res.get("hint"):
            print(f"  {res['hint']}")
        return
    if not res["n_tables"]:
        print(f"TABLE_NONE {res['rel']} p{res['page_index']} -- no table detected on this "
              f"page ({res.get('n_pages_in_pdf')} pages in the file).")
        print("  A borderless table often reads as text: use "
              f"`open \"{res['rel']}\" {res['page_index']}`.")
        return
    print(f"TABLE {res['rel']} p{res['page_index']} tables={res['n_tables']}")
    for n, t in enumerate(res["tables"], 1):
        print(f"  [table {n}] {t['n_rows']} rows x {t['n_cols']} cols")
        for row in t["rows"]:
            print("   | " + " | ".join(row) + " |")
    print("  (read from the PDF's layout. To cite a figure, `open` the page and `note` the "
          "line -- the guard checks quotes against the index's text layer, not this.)")


def _print_tables(res):
    for c in res["captions"]:
        print(f"  p{c['page_index']}  {c['caption']}")


# --------------------------------------------------------------------- #
# series
# --------------------------------------------------------------------- #
def do_series(ctx, row_words, family_words, fy_from=None, fy_to=None, k=1,
              exact_family=False, caption_channel=None):
    """exact_family=True: `family_words` IS already a real docs.family key (the
    caller resolved it directly from ctx.rel_to_family, not from user text) --
    use it as-is instead of re-resolving through do_have's fuzzy search. That
    fuzzy search is for a human's --family "some words"; a family KEY strings
    like "...{fy}" as a literal query (the placeholder brace text matches no
    real title), so it falls through to a bag-of-words tier where a noisy
    same-topic family can out-score the real one on raw term frequency -- the
    exact key is right there, searching for it again can only make it worse."""
    if exact_family:
        family = family_words
        if family not in ctx.family_to_rels:
            return {"editions": [], "matched": 0, "family": None, "error": "NO_FAMILY_MATCHES"}
    else:
        have_res = do_have(ctx, family_words)
        if have_res["no_family_matches"]:
            return {"editions": [], "matched": 0, "family": None, "error": "NO_FAMILY_MATCHES"}
        family = have_res["families"][0]["family"]
    members = [r for r in ctx.rel_to_row.values()
               if r["family"] == family and r["is_primary"] and r["fy_primary"]]
    if fy_from:
        members = [r for r in members if r["fy_primary"] >= fy_from]
    if fy_to:
        members = [r for r in members if r["fy_primary"] <= fy_to]
    members.sort(key=lambda r: r["fy_primary"])

    out, matched = [], 0
    for r in members:
        ins = do_inside(ctx, r["rel"], row_words, k=k,
                        caption_channel=caption_channel)
        if ins["hits"]:
            matched += 1
            h = ins["hits"][0]
            out.append({"fy": r["fy_primary"], "rel": r["rel"],
                        "page_index": h["page_index"], "line": h["line"],
                        "hits": ins["hits"]})
        else:
            out.append({"fy": r["fy_primary"], "rel": r["rel"], "page_index": None,
                        "line": None, "hits": []})
    return {"editions": out, "matched": matched, "family": family, "n": len(out)}


def _print_series(res):
    if res.get("error"):
        print(res["error"])
        return
    for e in res["editions"]:
        if e["page_index"] is not None:
            print(f"{e['fy']}  p{e['page_index']}  {e['rel']}  | {e['line']}")
        else:
            print(f"{e['fy']}  NO_MATCH  {e['rel']}")
    print(f'SERIES_RECEIPT editions={res["n"]} matched={res["matched"]} family="{res["family"]}"')


# --------------------------------------------------------------------- #
# copies
# --------------------------------------------------------------------- #
def do_copies(ctx, rel):
    row = ctx.rel_to_row.get(rel)
    if not row:
        return {"rel": rel, "members": []}
    ek = row["edition_key"]
    primary = next((r for r in ctx.rel_to_row.values()
                     if r["edition_key"] == ek and r["is_primary"]), row)
    members = []
    for r in ctx.rel_to_row.values():
        if r["edition_key"] == ek:
            members.append({"rel": r["rel"], "n_pages": r["n_pages"],
                             "is_primary": bool(r["is_primary"]),
                             "same_hash_as_primary": r["sha256"] == primary["sha256"]})
    for d in row["dupes"]:
        members.append({"rel": d, "n_pages": row["n_pages"], "is_primary": False,
                         "same_hash_as_primary": row["sha256"] == primary["sha256"],
                         "collapsed_duplicate": True})
    return {"rel": rel, "members": members}


def _print_copies(res):
    for m in res["members"]:
        print(f"  {m['rel']}  ({m['n_pages']}p)  is_primary={m['is_primary']}  "
              f"same_hash_as_primary={'yes' if m['same_hash_as_primary'] else 'no'}")


# --------------------------------------------------------------------- #
# exact / open / note / notes / coverage
# --------------------------------------------------------------------- #
def do_exact(ctx, phrase, k=50):
    m = '"' + phrase.replace('"', '""') + '"'
    total = ctx.db.execute("SELECT COUNT(*) FROM pages WHERE pages MATCH ?", (m,)).fetchone()[0]
    rows = ctx.db.execute(
        "SELECT rel, page_index FROM pages WHERE pages MATCH ? LIMIT ?", (m, k)).fetchall()
    grouped = {}
    for rel, pi in rows:
        grouped.setdefault(rel, []).append(pi)
    return {"phrase": phrase, "total": total, "grouped": grouped}


def _print_exact(res):
    print(f"TOTAL_PAGES_MATCHING={res['total']}")
    if res["total"] == 0:
        print("NO_PAGE_IN_THE_INDEX_CONTAINS_THIS_STRING")
    else:
        for rel, pages in res["grouped"].items():
            print(f"  {rel}  pages={pages}")


# _THIN_CHARS is imported from corpus_search: the coverage count and the citation
# warnings must agree on what "no readable text" means.

# The citation tail CLAUDE.md asks for: `<verbatim line> | <path> | p<page>`.
_ELISION_RE = re.compile(r"\.{3,}|\u2026")


def _norm_ws(s):
    """Whitespace-insensitive form. The page text is laid out with columns of
    spaces and the model copies it back with whatever spacing it got, so the
    comparison must not depend on that."""
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _quote_chunks(quote):
    """Split a quote on elision marks; every chunk is then checked on its own,
    so `a ... b` requires both a and b to be on the page. A chunk is kept when
    it is 6+ characters, or 3+ and contains a digit -- a bare figure is exactly
    what an invented note fabricates, so short numeric chunks are checked too.
    ponytail: chunk >=3 chars is the floor; a two-digit number is too weak a
    signal to match anything. Raise it if honest notes start getting refused."""
    out = []
    for c in (_norm_ws(x) for x in _ELISION_RE.split(quote or "")):
        if len(c) >= 6 or (len(c) >= 3 and any(ch.isdigit() for ch in c)):
            out.append(c)
    return out


def _quote_on_page(quote, body):
    """(ok, missing_chunk). Presence, not attribution: this proves the quoted
    text IS on the page, which is what stops an invented line being recorded.
    It cannot prove the figure sits in the row the answer says it does -- the
    limits of a substring check, stated rather than papered over."""
    nb = _norm_ws(body)
    for c in _quote_chunks(quote):
        if c not in nb:
            return False, c
    return True, None


def _page_body(ctx, rel, page_index):
    """The stored text of one page, or None when the index has no such page.

    Extracted from do_open unchanged so that open, note and verify fetch a page
    through ONE path: the string a model quotes is the string the tool checks.
    A second copy of this query would let the guard and the citation drift apart.

    Phase 9.1.1: `SELECT body FROM pages WHERE rel=? AND page_index=?` is a full
    scan of 1.2M rows -- `rel` is UNINDEXED in the FTS5 table -- which cost ~7.5s
    on EVERY open, and a trajectory session issues a dozen of them. The shelf
    already caches (rel -> contiguous rowid block) in page_ranges for `inside`,
    so the same range turns the scan into a rowid lookup. Rank-preserving by
    construction: same row, same bytes (verified byte-identical on 300 pages by
    bin/c_open_equiv_check.py). The original query stays as the fallback so any
    rel absent from the cache behaves exactly as before.
    """
    row = None
    try:
        _ensure_page_ranges(ctx)
        rng = ctx.shelf.execute(
            "SELECT start_id, end_id FROM page_ranges WHERE rel=?", (rel,)).fetchone()
        if rng:
            row = ctx.db.execute(
                "SELECT c2 FROM pages_content WHERE id BETWEEN ? AND ? AND c0=? AND c1=?",
                (rng[0], rng[1], rel, page_index)).fetchone()
    except Exception:
        row = None
    if row is None:
        row = ctx.db.execute("SELECT body FROM pages WHERE rel=? AND page_index=?",
                             (rel, page_index)).fetchone()
    return row[0] if row else None


def _page_sha(body):
    """A short content stamp for a page, so a note can be checked against the page as it
    was when the note was written (see do_verify's PAGE_CHANGED_SINCE_NOTE)."""
    return hashlib.sha256((body or "").encode("utf-8", "ignore")).hexdigest()[:16]


# --------------------------------------------------------------------- #
# answer numbers
#
# The note guard proves a QUOTE is on a page. Nothing tied the FIGURES IN THE ANSWER to
# those quotes, so an answer could quote correctly and then write a different number in its
# prose -- the one remaining way to be wrong while every check passes. These helpers look at
# an answer's text and ask, for each number it states, whether some note's quote contains
# that number.
#
# What is deliberately NOT checked, because flagging it produces noise that gets the check
# ignored:
#   * a four-digit year (1900-2099): a time reference, not a figure
#   * a single digit: page counts, "two years", list numbering
#   * anything inside a citation tail or a `p12`: the answer's own provenance, not a claim
# --------------------------------------------------------------------- #
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_TAIL_STRIP_RE = re.compile(r"\|[^|\n]*\|\s*p\d+\b|\bp\d+\b", re.I)
# A fiscal-year token is a time reference, and its second half is not a figure: "2024-25"
# would otherwise leave "25" looking like a claim the notes never made.
_FY_STRIP_RE = re.compile(r"\b(?:19|20)\d\d\s*[-/]\s*\d\d\b")


def _canon_number(tok):
    """1,234.50 -> 1234.50. Comparing the digits is what lets "Rs. 1,234 mn" answer a quote
    that prints "1,234"."""
    return tok.replace(",", "").rstrip(".")


def _numbers_in(text):
    """The figures a piece of prose states. Fiscal years are removed first (see
    _FY_STRIP_RE), and this is a SCANNER: it does not strip citation tails, because what
    counts as a claim depends on the answer's tails being removed first -- that is
    check_answer_numbers, which is what a caller wants."""
    out = []
    for m in _NUM_RE.finditer(_FY_STRIP_RE.sub(" ", text or "")):
        tok, canon = m.group(0), _canon_number(m.group(0))
        if not canon or canon.endswith("."):
            continue
        digits = canon.replace(".", "")
        if len(digits) < 2:                      # a single digit is not a figure
            continue
        if len(digits) == 4 and digits.isdigit() and 1900 <= int(digits) <= 2099:
            continue                             # a year
        out.append((tok, canon))
    return out


def _quote_numbers(quote):
    """The canonical numbers a quote contains, as whole tokens.

    Matching is on token boundaries: a quote holding "2018-19" must not be read as support
    for the figure 18.
    """
    return {_canon_number(m.group(0)) for m in _NUM_RE.finditer(quote or "")}


def check_answer_numbers(answer_text, notes):
    """Each number the answer states, and whether a note's quote contains it.

    Rounding and unit restatement are the innocent causes of a miss ("610.5 million" for a
    quote that prints 610.456), which is why the verdict names the figure and the quotes
    checked instead of just failing.
    """
    quoted = []
    for n in notes:
        q = n.get("quote")
        if not q:                      # an older note: fall back to the text before the tail
            m = _NOTE_TAIL_RE.search(n.get("text") or "")
            q = (n.get("text") or "")[:m.start()] if m else (n.get("text") or "")
        quoted.append((n.get("text", "")[:60], _quote_numbers(q)))
    prose = _TAIL_STRIP_RE.sub(" ", answer_text or "")
    out, seen = [], set()
    for tok, canon in _numbers_in(prose):
        if canon in seen:
            continue
        seen.add(canon)
        where = [t for t, nums in quoted if canon in nums]
        out.append({"figure": tok, "canonical": canon, "supported": bool(where),
                    "in_notes": where[:3]})
    return out


def do_open(ctx, rel, page_index, slug=None):
    body = _page_body(ctx, rel, page_index)
    if body is None:
        return {"rel": rel, "page_index": page_index, "found": False}
    body = body[:12000]
    if slug:
        nd = _notes_dir(ctx.root)
        log = nd / f"{slug}_opened.jsonl"
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"rel": rel, "page_index": page_index,
                                  "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")
    return {"rel": rel, "page_index": page_index, "found": True, "body": body}


def _print_open(res):
    if not res["found"]:
        print(f"NO SUCH PAGE: {res['rel']} page {res['page_index']}")
        print("  (no such page in the index -- the file may have failed extraction, "
              "or that page carries no text layer. Run `coverage`.)")
        return
    # The citation tail carries the INDEX's page number (`p<page_index>`) -- what `open`
    # takes and what the note guard checks. When the index numbers its pages from 1 that
    # number IS the PDF page a reader opens, so nothing more needs saying; with an older
    # 0-based index the PDF page is printed beside it, because a citation a human cannot
    # find on the page they open is not a citation they can check.
    pdf_page = L.pdf_page_number(res["page_index"])
    where = f" (PDF page {pdf_page})" if pdf_page != res["page_index"] else ""
    print(f"OPENED {res['rel']} p{res['page_index']}{where}")
    if len(res["body"].strip()) < _THIN_CHARS:
        print(f"PAGE_HAS_NO_READABLE_TEXT ({len(res['body'].strip())} chars) -- a scan or "
              f"a blank page. It cannot support a citation; say the page is unreadable "
              f"rather than quoting it.")
    print(res["body"])


# `... | <path> | p<page_index>` -- the citation tail CLAUDE.md already asks for.
_NOTE_TAIL_RE = re.compile(r"\|\s*(?P<path>[^|]+?)\s*\|\s*p(?P<page>\d+)\s*$", re.I)


def do_note(ctx, slug, text):
    """A note is the one place a figure crosses from a page into an answer, so
    it is the one place provenance can be enforced by the tool rather than
    asked for in prose. The 2026-09-15 batteries measured 5 and then 8 answers
    citing a file the session never opened, with the instruction to open first
    sitting in CLAUDE.md the whole time. An instruction the tool does not
    enforce is a suggestion.

    So: the note must carry the `| <path> | p<n>` tail the policy already
    prescribes, that (path, page) must appear in this slug's own
    `_opened.jsonl`, and the quoted text must actually occur on that page.
    Nothing is recorded when any of the three fails.

    Path comparison is by the last two segments, lowercased, matching
    scoring.py's rule -- the agent quotes the path as `find` printed it, and
    requiring a byte-identical string would fail honest notes for punctuation.
    """
    m = _NOTE_TAIL_RE.search(text or "")
    if not m:
        return {"ok": False, "error": "NOTE_REFUSED_NO_CITATION"}
    cited_path = m.group("path").strip().strip('"\'')
    cited_page = int(m.group("page"))

    nd = _notes_dir(ctx.root)
    opened_log = nd / f"{slug}_opened.jsonl"
    opened = []
    if opened_log.exists():
        for line in opened_log.read_text(encoding="utf-8").splitlines():
            try:
                opened.append(json.loads(line))
            except Exception:
                pass
    want = _tail2(cited_path)
    match = next((o for o in opened
                  if o.get("page_index") == cited_page and _tail2(o.get("rel")) == want), None)
    if match is None:
        return {"ok": False, "error": "NOTE_REFUSED_PAGE_NOT_OPENED",
                "path": cited_path, "page_index": cited_page}

    # Opening a page is not the same as quoting it. Until now this function
    # checked the (path, page) tail and nothing else, so a note could satisfy the
    # guard by opening page 7 and then carry a figure that is on page 9, or on no
    # page at all -- the guard proved an open, never the quote. The page body is
    # one call away, so the quote is now checked against it and refused if absent.
    quote = text[:m.start()].strip().strip("|\"' ")
    if not _quote_chunks(quote):
        return {"ok": False, "error": "NOTE_REFUSED_NO_QUOTE",
                "path": match.get("rel"), "page_index": cited_page}
    body = _page_body(ctx, match.get("rel"), cited_page)
    if body is None:
        return {"ok": False, "error": "NOTE_REFUSED_PAGE_NOT_IN_INDEX",
                "path": match.get("rel"), "page_index": cited_page}
    ok, missing = _quote_on_page(quote, body)
    if not ok:
        return {"ok": False, "error": "NOTE_REFUSED_QUOTE_NOT_ON_PAGE",
                "path": match.get("rel"), "page_index": cited_page,
                "quoted": quote[:200], "missing": missing}

    log = nd / f"{slug}_notes.jsonl"
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"text": text, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                             "page_sha": _page_sha(body), "quote": quote[:200]}) + "\n")
    return {"ok": True}


def do_notes(ctx, slug):
    nd = _notes_dir(ctx.root)
    opened_log = nd / f"{slug}_opened.jsonl"
    notes_log = nd / f"{slug}_notes.jsonl"
    opened = [json.loads(l) for l in opened_log.read_text(encoding="utf-8").splitlines()] \
        if opened_log.exists() else []
    notes = [json.loads(l) for l in notes_log.read_text(encoding="utf-8").splitlines()] \
        if notes_log.exists() else []
    return {"slug": slug, "opened": opened, "notes": notes}


def _print_notes(res):
    print(f"NOTES for slug={res['slug']}")
    print(f"opened ({len(res['opened'])}):")
    for o in res["opened"]:
        print(f"  {o['rel']} p{o['page_index']}  ({o['ts']})")
    print(f"notes ({len(res['notes'])}):")
    for n in res["notes"]:
        print(f"  {n['text']}  ({n['ts']})")


def _tail2(s):
    """Last two path segments, lowercased. How scoring.py and the note guard
    compare a cited path against an indexed one, hoisted to module level so the
    write-time guard and `verify` compare paths by exactly the same rule."""
    segs = [x for x in str(s).replace("\\", "/").lower().split("/") if x]
    return "/".join(segs[-2:])


def _resolve_rel(ctx, cited_path):
    """The indexed rel a citation's path refers to, matched on the last two path
    segments the way scoring.py and the note guard do, so a citation written from
    a printed path resolves to the same file the index holds.

    ponytail: one pass over files per call. At 15k files that is a 15k-row scan
    per verify, which is nothing next to the pass itself; index it if verify ever
    runs per-answer in a loop."""
    want = _tail2(cited_path)
    for (rel,) in ctx.db.execute("SELECT rel FROM files"):
        if _tail2(rel) == want:
            return rel
    return None


def do_verify(ctx, slug, answer_path=None):
    """Re-derive every note of a slug from the index alone.

    `notes` prints what was written. This asks the index instead: does the cited
    file exist, does that page exist, and does the quoted text occur on it? It
    uses the same `_quote_on_page` as the write-time guard, so a note cannot pass
    here by being checked differently. Exit code is non-zero on any failure, which
    is what lets a handover be gated on it.

    With `answer_path`, every figure the answer states is also checked against the
    notes' quotes: a correct quote next to a different number in the prose is the one
    way left to be wrong with every other check passing.
    """
    res = do_notes(ctx, slug)
    checks = []
    for n in res["notes"]:
        text = n.get("text", "")
        m = _NOTE_TAIL_RE.search(text or "")
        if not m:
            checks.append({"text": text, "ok": False, "error": "NO_CITATION_TAIL"})
            continue
        cited = m.group("path").strip().strip("\"' ")
        page = int(m.group("page"))
        rel = _resolve_rel(ctx, cited)
        if rel is None:
            checks.append({"text": text, "ok": False, "error": "CITED_FILE_NOT_IN_INDEX",
                           "path": cited, "page_index": page})
            continue
        body = _page_body(ctx, rel, page)
        if body is None:
            checks.append({"text": text, "ok": False, "error": "CITED_PAGE_NOT_IN_INDEX",
                           "path": rel, "page_index": page})
            continue
        ok, missing = _quote_on_page(text[:m.start()], body)
        note_sha = n.get("page_sha")
        now_sha = _page_sha(body)
        if not ok and note_sha and note_sha != now_sha:
            # The note recorded the page's content when it was written. If that content has
            # changed, a missing quote means "the index moved under this citation", not "the
            # answer made it up" -- two different problems with two different fixes.
            checks.append({"text": text, "ok": False, "error": "PAGE_CHANGED_SINCE_NOTE",
                           "path": rel, "page_index": page, "missing": missing,
                           "page_sha": note_sha, "page_sha_now": now_sha})
            continue
        if not ok:
            checks.append({"text": text, "ok": False, "error": "QUOTE_NOT_ON_PAGE",
                           "path": rel, "page_index": page, "missing": missing})
            continue
        checks.append({"text": text, "ok": True, "path": rel, "page_index": page,
                       "page_chars": len(body.strip()),
                       "page_changed": bool(note_sha and note_sha != now_sha)})
    n_bad = sum(1 for c in checks if not c["ok"])
    out = {"slug": slug, "n_notes": len(checks), "n_bad": n_bad, "checks": checks}
    if answer_path:
        try:
            text = Path(answer_path).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            out["answer"] = {"path": answer_path, "error": type(e).__name__}
            out["n_bad_answer"] = 1
            return out
        claims = check_answer_numbers(text, res["notes"])
        unsupported = [c for c in claims if not c["supported"]]
        out["answer"] = {"path": answer_path, "figures": claims,
                         "unsupported": unsupported}
        out["n_bad_answer"] = len(unsupported)
    return out


def _print_verify(res):
    print(f"VERIFY slug={res['slug']} notes={res['n_notes']} failed={res['n_bad']}")
    for c in res["checks"]:
        if c["ok"]:
            warn = "  page text changed since the note" if c.get("page_changed") else ""
            print(f"  OK   p{c['page_index']} ({c['page_chars']} chars)  {c['path']}{warn}")
        else:
            extra = f"  missing={c['missing']!r}" if c.get("missing") else ""
            if c["error"] == "PAGE_CHANGED_SINCE_NOTE":
                extra = (f"  page was {c['page_sha']} when noted, now {c['page_sha_now']} "
                         f"-- re-indexed since; re-make this note from the current page")
            print(f"  FAIL {c['error']}  {c.get('path', '?')} p{c.get('page_index', '?')}{extra}")
    if res["n_bad"]:
        print("VERIFY_FAILED -- do not hand over an answer built on these notes")
    elif res["n_notes"]:
        print("VERIFY_OK -- every note's quote is on the page it cites")
    else:
        print("VERIFY_EMPTY -- no notes for this slug; nothing was cited")
    if "answer" in res:
        a = res["answer"]
        if a.get("error"):
            print(f"ANSWER_UNREADABLE {a['error']}  {a['path']}")
            return
        figs = a["figures"]
        print(f"ANSWER_NUMBERS {a['path']} figures={len(figs)} "
              f"unsupported={len(a['unsupported'])}")
        for c in a["unsupported"]:
            print(f"  NOT IN ANY NOTE QUOTE: {c['figure']}  (rounding or a restated unit is "
                  f"the usual innocent cause -- check it, then cite it)")
        if a["unsupported"]:
            print("ANSWER_UNSUPPORTED_FIGURES -- a figure the notes do not contain")
        elif figs:
            print("ANSWER_OK -- every figure the answer states appears in a note's quote")


def do_coverage(ctx):
    return ctx.coverage()


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(prog="c_shelf.py")
    ap.add_argument("--db")
    ap.add_argument("--shelf")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("find"); p.add_argument("question"); p.add_argument("--q", action="append", default=[])
    p.add_argument("--k", type=int, default=12); p.add_argument("--slug")
    # Phase 9.3.1. Both default to off, so the output with neither flag is
    # byte-for-byte what it was.
    p.add_argument("--compact", type=int, default=None,
                   help="print the top N families as one line each, fused order")
    p.add_argument("--show", default=None,
                   help="comma-separated ranks from a --compact list; print those in full")
    p.add_argument("--fusion", choices=["rrf", "best"], default="rrf")
    p.add_argument("--caption-channel", dest="caption_channel",
                   choices=["off", "lex", "lex+vec"], default="off")

    p = sub.add_parser("have"); p.add_argument("words"); p.add_argument("--fy")

    p = sub.add_parser("inside"); p.add_argument("rel"); p.add_argument("terms")
    p.add_argument("--k", type=int, default=8)
    p.add_argument("--caption", choices=["off", "first", "first_label", "dense_first"],
                   default="dense_first")  # B2c adopted 2026-09-15 (F55); the
    # Python default stays None so every recorded number reproduces with explicit flags

    p = sub.add_parser("tables"); p.add_argument("rel"); p.add_argument("--grep")
    p = sub.add_parser("table"); p.add_argument("rel"); p.add_argument("--page", type=int, required=True)
    p.add_argument("--max-rows", type=int, default=60)

    p = sub.add_parser("series"); p.add_argument("row_words"); p.add_argument("--family", required=True)
    p.add_argument("--from", dest="fy_from"); p.add_argument("--to", dest="fy_to"); p.add_argument("--slug")
    p.add_argument("--caption", choices=["off", "first", "first_label", "dense_first"],
                   default="dense_first")  # B2c adopted 2026-09-15 (F55); the
    # Python default stays None so every recorded number reproduces with explicit flags

    p = sub.add_parser("copies"); p.add_argument("rel")

    p = sub.add_parser("exact"); p.add_argument("phrase"); p.add_argument("--k", type=int, default=50)

    p = sub.add_parser("open"); p.add_argument("rel"); p.add_argument("page_index", type=int)
    p.add_argument("--slug")

    p = sub.add_parser("note"); p.add_argument("--slug", required=True); p.add_argument("text")

    p = sub.add_parser("notes"); p.add_argument("--slug", required=True)
    p = sub.add_parser("verify"); p.add_argument("--slug", required=True)
    p.add_argument("--answer", help="also check every figure in this answer file against "
                                    "the notes' quotes")

    p = sub.add_parser("coverage")
    p.add_argument("--json", dest="as_json", action="store_true",
                   help="the whole coverage dict, including every file's thin-page count")

    p = sub.add_parser("register"); p.add_argument("--root", required=True)
    p.add_argument("--db", dest="reg_db", required=True); p.add_argument("--shelf", dest="reg_shelf", required=True)

    a = ap.parse_args()
    if not a.cmd:
        ap.print_usage(); sys.exit(2)

    if a.cmd == "register":
        r = do_register(a.root, a.reg_db, a.reg_shelf)
        print(json.dumps(r, indent=1)); return

    root, db, shelf = _resolve(a.db, a.shelf)
    if not str(shelf).endswith(":memory:") and _update_in_progress(shelf).exists():
        print("SHELF_UPDATE_IN_PROGRESS %s\n  this shelf is being updated right now; an "
              "answer taken now could cite the wrong document. Wait for c_shelf_update.py "
              "to finish, or re-run it if it was interrupted."
              % _update_in_progress(shelf), file=sys.stderr)
        sys.exit(3)
    if not db or not shelf:
        print("NO_REGISTERED_ROOT", file=sys.stderr); sys.exit(2)
    ctx = get_ctx(db, shelf, root)

    if a.cmd == "find":
        queries = [a.question] + list(a.q)
        cc = None if a.caption_channel == "off" else a.caption_channel
        res = do_find(ctx, queries, fusion=a.fusion, caption_channel=cc)
        if a.show:
            idx = []
            for part in str(a.show).replace(" ", ",").split(","):
                if part.strip().isdigit():
                    idx.append(int(part.strip()))
            _print_find_show(res, idx)
        elif a.compact:
            _print_find_compact(res, a.compact, a.question)
        else:
            _print_find(res, a.k)
    elif a.cmd == "have":
        res = do_have(ctx, a.words, fy=a.fy)
        _print_have(res)
    elif a.cmd == "inside":
        res = do_inside(ctx, a.rel, a.terms, k=a.k,
                        caption_channel=None if a.caption == "off" else a.caption)
        _print_inside(res)
    elif a.cmd == "tables":
        res = do_tables(ctx, a.rel, grep=a.grep)
        _print_tables(res)
    elif a.cmd == "table":
        res = do_table(ctx, a.rel, a.page, max_rows=a.max_rows)
        _print_table(res)
    elif a.cmd == "series":
        res = do_series(ctx, a.row_words, a.family, fy_from=a.fy_from, fy_to=a.fy_to,
                        caption_channel=None if a.caption == "off" else a.caption)
        _print_series(res)
    elif a.cmd == "copies":
        res = do_copies(ctx, a.rel)
        _print_copies(res)
    elif a.cmd == "exact":
        res = do_exact(ctx, a.phrase, k=a.k)
        _print_exact(res)
    elif a.cmd == "open":
        res = do_open(ctx, a.rel, a.page_index, slug=a.slug)
        _print_open(res)
    elif a.cmd == "note":
        r = do_note(ctx, a.slug, a.text)
        if not r.get("ok"):
            err = r["error"]
            if err == "NOTE_REFUSED_PAGE_NOT_OPENED":
                print("NOTE_REFUSED_PAGE_NOT_OPENED %s p%d -- open that page first"
                      % (r["path"], r["page_index"]), file=sys.stderr)
            elif err == "NOTE_REFUSED_PAGE_NOT_IN_INDEX":
                print("NOTE_REFUSED_PAGE_NOT_IN_INDEX %s p%d -- the index holds no such page"
                      % (r["path"], r["page_index"]), file=sys.stderr)
            elif err == "NOTE_REFUSED_QUOTE_NOT_ON_PAGE":
                print("NOTE_REFUSED_QUOTE_NOT_ON_PAGE %s p%d -- this text is not on that "
                      "page: %r" % (r["path"], r["page_index"], r.get("missing")),
                      file=sys.stderr)
            elif err == "NOTE_REFUSED_NO_QUOTE":
                print("NOTE_REFUSED_NO_QUOTE -- a note is `<verbatim line> | <path> | p<n>`; "
                      "the quoted line is what gets checked", file=sys.stderr)
            else:
                print("NOTE_REFUSED_NO_CITATION", file=sys.stderr)
            sys.exit(3)
        print("NOTED")
    elif a.cmd == "notes":
        res = do_notes(ctx, a.slug)
        _print_notes(res)
    elif a.cmd == "verify":
        res = do_verify(ctx, a.slug, answer_path=a.answer)
        _print_verify(res)
        if res["n_bad"] or res.get("n_bad_answer"):
            sys.exit(1)
    elif a.cmd == "coverage":
        cov = do_coverage(ctx)
        if getattr(a, "as_json", False):
            print(json.dumps(cov, indent=1))
        else:
            _print_coverage(cov)
    else:
        ap.print_usage(); sys.exit(2)


if __name__ == "__main__":
    main()
