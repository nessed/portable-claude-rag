#!/usr/bin/env python
"""trajectory_filler.py - the scaffolding words a trajectory question is made of.

`corpus_search.content_words()`'s English stopword list already strips most of
the frame from a question like "how has development spending changed over the
last decade", leaving a small residue of trajectory-question scaffolding to drop
too. The offline gate needs it to build a row query; the shelf needs it to reduce
a query to the words a caption would plausibly print.

The contamination rule (Plan D section 0 rule 4) permits exactly this list: it is
generic English and carries no title, family, caption, label, synonym,
publication name or year from the corpus.

**This is a move, not a change.** The set below is byte-for-byte the one that
lived in `c_offline_gate.py`, and `c_offline_gate` now imports it from here, so
there is still exactly one definition in the repository and no chance of quietly
widening it. The move exists because `c_shelf.py` imported the constant from the
offline gate, which meant the portable package could not be built without
shipping a script that opens the answer key (Plan E Phase 3.1). Now the package
carries the word list without the gate.

The offline-gate baseline that depends on this list -- B2c 52/57 -- was re-run
after the move and reproduces exactly.
"""

_TRAJECTORY_FILLER = set("""
    has have gone period much decade so trajectory years we me give
""".split())
