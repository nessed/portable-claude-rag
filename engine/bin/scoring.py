#!/usr/bin/env python
"""scoring.py - the ONE definition of "did it find the canary".

Grid-run phase 0.2. The night-1 scorer was:

    found = exp in ans or (base in ans and len(base) > 8)

`base` is the bare filename. One pass-1 canary lives in
`02_Source_Documents_Read_Only/README.md`, so `base` is `readme.md`, length 9,
which clears the `> 8` guard. Any answer that mentioned any README anywhere in a
217,527-file tree scored as a find. `layout_summary.md`, `gap_report.md` and
`project_config.json` are the same class of problem: common names, no path.

The rule now: an answer has found the canary only if it names enough of the path
to ADDRESS the file. Concretely, the last two path segments ("dir/file.ext"), or
the full relative path. For a file at depth 0 there is no parent segment, so the
filename alone must stand -- and it only counts if it is distinctive enough that
naming it is not a lucky guess (DISTINCTIVE_BASENAME_MIN chars).

Retrieval is scored separately from the answer text, because they fail
independently: a session can open the right file and then describe it wrongly,
or name the right path having never opened it (it guessed, or it read a listing).
Phase 7.3 requires both numbers.
"""
import re

# A depth-0 canary has no parent directory to disambiguate it, so the basename
# carries the whole burden. 12 chars rules out readme.md / index.md / base.py
# while admitting LOCAL_ENVIRONMENT_SETUP.md.
DISTINCTIVE_BASENAME_MIN = 12


def norm_path(p):
    """Forward-slashed, lowercase, no leading ./ or / -- for comparing paths."""
    if not p:
        return ""
    s = str(p).replace("\\", "/").lower().strip()
    while s.startswith("./") or s.startswith("/"):
        s = s.lstrip("/")
        if s.startswith("./"):
            s = s[2:]
    return s


def norm_text(t):
    """Answer text, backslashes folded to slashes so path spellings compare."""
    return (t or "").replace("\\", "/").lower()


def path_tail(rel, n=2):
    """Last n segments of a relative path, e.g. 'deliverables/findings.html'."""
    segs = [s for s in norm_path(rel).split("/") if s]
    return "/".join(segs[-n:]) if segs else ""


def answer_names_path(answer, rel):
    """Did the answer text ADDRESS this file, not merely mention a common name?

    Returns (found: bool, how: str). `how` records which rule fired so a
    borderline hit can be audited rather than trusted.
    """
    ans = norm_text(answer)
    exp = norm_path(rel)
    if not ans or not exp:
        return False, "empty"

    if exp in ans:
        return True, "full_path"

    segs = [s for s in exp.split("/") if s]
    base = segs[-1] if segs else ""

    if len(segs) >= 2:
        tail = path_tail(exp, 2)
        if tail in ans:
            return True, "last_two_segments"
        # Tolerate an answer that uses a different separator run or quotes the
        # parent and child adjacently but not literally joined, e.g.
        # "README.md in 02_Source_Documents_Read_Only". Require both tokens AND
        # that they sit within 80 chars of each other, so two unrelated mentions
        # elsewhere in a long answer do not combine into a false hit.
        parent, child = segs[-2], base
        for m in re.finditer(re.escape(child), ans):
            window = ans[max(0, m.start() - 80):m.end() + 80]
            if parent in window:
                return True, "parent_and_child_adjacent"
        return False, "basename_only_or_absent"

    # depth 0: the basename is all there is
    if base and base in ans:
        if len(base) >= DISTINCTIVE_BASENAME_MIN:
            return True, "distinctive_basename_depth0"
        return False, "basename_too_generic"
    return False, "absent"


def retrieval_hit(files_opened, rel):
    """Did the session actually OPEN the expected file?

    files_opened entries are already corpus-relative and lowercased by ask.py.
    Matching is on the full relative path or its last two segments, same rule as
    the answer text, so the two numbers are comparable.
    """
    exp = norm_path(rel)
    tail = path_tail(exp, 2)
    for f in files_opened or []:
        p = norm_path(f.get("path"))
        if not p:
            continue
        if p == exp or p.endswith("/" + exp) or exp.endswith("/" + p):
            return True, f.get("tool")
        if tail and len(tail.split("/")) >= 2 and p.endswith(tail):
            return True, f.get("tool")
    return False, None


def score_one(result, rel):
    """Score a single ask.py result dict against an expected relative path."""
    ans = result.get("answer_text") or ""
    found, how = answer_names_path(ans, rel)
    ret, tool = retrieval_hit(result.get("files_opened", []), rel)
    return {
        "answer_found": found,
        "answer_match_rule": how,
        "retrieval_found": ret,
        "retrieval_tool": tool,
        "said_not_found": "not_found" in norm_text(ans),
    }


def legacy_found(answer, rel):
    """The night-1 rule, kept ONLY so the re-score can quantify the difference."""
    ans = norm_text(answer)
    exp = norm_path(rel)
    base = exp.split("/")[-1]
    return bool(exp in ans or (base in ans and len(base) > 8))
