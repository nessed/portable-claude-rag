#!/usr/bin/env python
"""corpus_search.py - the front door. Page-level search over the FTS5 corpus index.

Every result is an addressable location (file + page), never just a filename, and
every result set carries a retrieval receipt so an omission is visible rather than
silent. This is what the agent is redirected to when built-in Grep/Glob are denied.

  corpus_search.py "motor vehicle production"      # ranked page hits
  corpus_search.py --exact "SRO 1247"              # literal phrase
  corpus_search.py --page <rel> <page_index>       # read one page
  corpus_search.py --coverage                      # what is and isn't in the index
"""
import argparse, json, os, re, sqlite3, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labpaths as L  # noqa: E402

# Phase 1.1: derived from the lab root, overridable per stack with CORPUS_DB.
DEFAULT_DB = os.environ.get("CORPUS_DB", str(L.STACKS / "s2_fts5" / "raship.db"))


def connect(db):
    if not Path(db).exists():
        print(f"ERROR: no index at {db}. Run index_build.py first.", file=sys.stderr)
        sys.exit(2)
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


# 2026-09-13: the front door could not take a sentence.
#
# fts_quote() emitted every token as a quoted term, and FTS5's implicit operator
# between terms is AND. So "how does Pakistan's development spending trajectory
# look since 2015" became a nine-way conjunction and matched nothing. Measured on
# the frozen 20: the question's own words returned ZERO pages on 16 of 20, and on
# 13 of the 17 questions whose evidence is sitting in this index. The agent only
# ever got results by inventing short phrases of its own.
#
# Now: try the conjunction first, because when every word really does co-occur
# that page is almost always the right one; fall back to ANY word, ranked by
# bm25, rather than returning nothing. The tier used is printed, so a loose match
# is never mistaken for a precise one.
STOP = set("""a an the of in on at to for and or is are was were be been being with by
from as that this these those it its which what when where who whom how why does do did
doing than then there their them they he she his her you your we our us i me my if but
not no nor so such only own same too very can will just should now over under between
into through during before after above below up down out off again further once here
both each few more most other some any all need want give show tell find look""".split())


def content_words(q):
    """Words worth searching on: 2+ chars, not stopwords, duplicates dropped."""
    out, seen = [], set()
    for t in re.findall(r"[A-Za-z0-9_\-]+", (q or "").lower()):
        if len(t) < 2 or t in STOP or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def fts_quote(q):
    """Legacy AND form, kept so the old behaviour can still be reproduced."""
    toks = re.findall(r"[A-Za-z0-9_\-]+", q)
    if not toks:
        return None
    return " ".join(f'"{t}"' for t in toks)


def fts_all(words):
    return " ".join(f'"{w}"' for w in words) if words else None


def fts_any(words):
    return " OR ".join(f'"{w}"' for w in words) if words else None


def coverage(db):
    rows = dict(db.execute("SELECT status, COUNT(*) FROM files GROUP BY status").fetchall())
    meta = dict(db.execute("SELECT k, v FROM meta").fetchall())
    npages = db.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    discovered = int(meta.get("discovered", 0))
    indexed = rows.get("indexed", 0)
    # os.walk prunes excluded dirs without descending, so the walker cannot count
    # what it never visited. Derive it: everything discovered that never became a
    # candidate was excluded as dependency material. Keeps the identity closed.
    excluded = max(discovered - sum(rows.values()), 0)
    failed = sum(v for k, v in rows.items() if k.startswith("failed_"))
    other = sum(v for k, v in rows.items()
                if not k.startswith("failed_") and k != "indexed")
    out = {
        "corpus_root": meta.get("corpus_root"),
        "built_at": meta.get("built_at"),
        "discovered_on_disk": discovered,
        "intentionally_excluded_dependency": excluded,
        "indexed_ok": indexed,
        "failed": failed,
        "unsupported_or_empty": other,
        "addressable_pages": npages,
        "by_status": rows,
        "accounting_identity_holds":
            discovered == excluded + indexed + failed + other,
    }
    return out


def _run(db, m, limit):
    total = db.execute("SELECT COUNT(*) FROM pages WHERE pages MATCH ?", (m,)).fetchone()[0]
    rows = db.execute(
        "SELECT rel, page_index, snippet(pages,2,'>>','<<','...',24), bm25(pages) "
        "FROM pages WHERE pages MATCH ? ORDER BY bm25(pages) LIMIT ?",
        (m, limit)).fetchall()
    return rows, total


def search(db, query, limit, exact, legacy=False):
    """-> rows, total, tier. tier is 'exact', 'all', 'any' or 'none'."""
    if exact:
        rows, total = _run(db, f'"{query}"', limit)
        return rows, total, "exact"
    if legacy:
        m = fts_quote(query)
        if not m:
            return [], 0, "none"
        rows, total = _run(db, m, limit)
        return rows, total, "all"

    words = content_words(query)
    if not words:
        return [], 0, "none"
    # precise first: every word on one page
    m = fts_all(words)
    try:
        rows, total = _run(db, m, limit)
    except sqlite3.OperationalError:
        rows, total = [], 0
    if rows:
        return rows, total, "all"
    # then loose: any word, ranked. Better a ranked list than "NO MATCHES".
    m = fts_any(words)
    try:
        rows, total = _run(db, m, limit)
    except sqlite3.OperationalError:
        return [], 0, "none"
    return rows, total, ("any" if rows else "none")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="*")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--exact", action="store_true")
    ap.add_argument("--coverage", action="store_true")
    ap.add_argument("--page", nargs=2, metavar=("REL", "PAGE_INDEX"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--legacy-and", action="store_true",
                    help="pre-2026-09-13 behaviour: require every word on one page")
    a = ap.parse_args()
    db = connect(a.db)

    if a.coverage:
        print(json.dumps(coverage(db), indent=1)); return

    if a.page:
        rel, pi = a.page[0].replace("\\", "/"), int(a.page[1])
        r = db.execute("SELECT body FROM pages WHERE rel=? AND page_index=?",
                       (rel, pi)).fetchone()
        if not r:
            print(f"NO SUCH PAGE: {rel} page {pi}"); sys.exit(1)
        print(f"=== {rel}  page_index={pi} ===\n{r[0]}"); return

    q = " ".join(a.query).strip()
    if not q:
        ap.error("give a query, or --coverage, or --page")
    rows, total, tier = search(db, q, a.limit, a.exact, legacy=a.legacy_and)
    cov = coverage(db)

    if a.json:
        print(json.dumps({
            "query": q, "match_tier": tier, "returned": len(rows),
            "total_matching_pages": total,
            "results": [{"path": r[0], "page_index": r[1], "snippet": r[2],
                         "score": round(r[3], 3)} for r in rows],
            "receipt": cov}, indent=1))
        return

    if not rows:
        print(f"NO MATCHES for {q!r}.")
    else:
        if tier == "any":
            print(f"LOOSE MATCH: no page holds every word, so these are ranked on")
            print(f"overlap. Treat the ranking as a lead, not an answer.\n")
        print(f"{total} matching pages; showing top {len(rows)}:\n")
        for rel, pi, snip, score in rows:
            print(f"  {rel}  [page_index={pi}]  score={score:.2f}")
            print(f"      {' '.join(snip.split())[:240]}\n")
    # the receipt - printed always, so an omission is visible rather than silent
    print("--- retrieval receipt ---")
    print(f"  searched {cov['addressable_pages']} indexed pages across "
          f"{cov['indexed_ok']} files (index built {cov['built_at']})")
    print(f"  NOT searched: {cov['failed']} files failed extraction, "
          f"{cov['unsupported_or_empty']} unsupported/empty, "
          f"{cov['intentionally_excluded_dependency']} excluded as dependency material")
    print("  a 'no match' here means no indexed page matched - NOT that the corpus lacks it")


if __name__ == "__main__":
    main()
