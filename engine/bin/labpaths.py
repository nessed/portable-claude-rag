#!/usr/bin/env python
"""labpaths.py - the ONE place any lab script learns where anything lives.

Phase 1.1 of the grid run: thirteen hardcoded absolute paths across seven scripts
meant the lab could not be moved. Everything now derives from CORPUS_LAB_ROOT,
which itself defaults to the parent of this file's directory -- so the tree is
relocatable with no env var set at all.

Env overrides, all optional:
  CORPUS_LAB_ROOT     the corpus-lab dir            (default: parent of bin/)
  RETRIEVAL_LAB_ROOT  the umbrella dir              (default: parent of corpus-lab)
  LAB_PRIVATE         answer-key quarantine         (default: <retrieval-lab>/_private)
  HARNESS_ROOT        harness corpora + generator   (default: <retrieval-lab>/harness)
  RASHIP_ROOT         the ra-ship fixture corpus
  RUNS_ROOT           per-session result json/jsonl (default: <private>/results/03_runs)
  SCORES_ROOT         scored batteries              (default: <private>/results/04_scores)
  CANARY_MANIFEST     phase 2.3: scoring-only, NEVER defaulted. Absent = not scoring.
  HARNESS_KEYS        answer_key.json + slots       (default: <private>/harness_keys)
  CLAUDE_CMD          claude.cmd; PATH `claude` is a shell wrapper CreateProcess can't run
  PDFTOTEXT_EXE       pdftotext.exe (default: Git for Windows, then PATH)

_first_existing() lets the same code run before and after the phase 1/2 moves:
a candidate that exists wins, otherwise the post-move location is returned so
that mkdir(parents=True) puts new output in the right place.
"""
import os
import shutil
from pathlib import Path


def _env_path(name):
    v = os.environ.get(name)
    return Path(v).resolve() if v else None


def _first_existing(*cands):
    """Return the first candidate that exists; else the first candidate."""
    cands = [c for c in cands if c is not None]
    for c in cands:
        if c.exists():
            return c
    return cands[0]


# --- anchors -----------------------------------------------------------------
CORPUS_LAB = _env_path("CORPUS_LAB_ROOT") or Path(__file__).resolve().parent.parent
RETRIEVAL_LAB = _env_path("RETRIEVAL_LAB_ROOT") or CORPUS_LAB.parent

# Back-compat: pre-move, `harness` and `corpus-lab` were siblings one level
# above; post-move they are siblings inside retrieval-lab. Both resolve to
# RETRIEVAL_LAB/harness.
PRIVATE = _env_path("LAB_PRIVATE") or (RETRIEVAL_LAB / "_private")
# The pre-move sibling location was an absolute path on the development
# machine. It has not been taken since the phase 1/2 moves completed, and the
# portable package must contain no absolute user path at all (Gate R3 greps
# for them), so it is gone.
HARNESS = _env_path("HARNESS_ROOT") or (RETRIEVAL_LAB / "harness")
# The ra-ship fixture corpus is development-only. Absent the env var there is
# simply no RASHIP root -- callers that need one already fail loudly, and a
# hard-coded default would put an absolute user path into the package.
RASHIP = _env_path("RASHIP_ROOT")

# --- derived dirs ------------------------------------------------------------
BIN = CORPUS_LAB / "bin"
SCRATCH = CORPUS_LAB / "99_scratch"
STATE = CORPUS_LAB / "state"
FINDINGS = CORPUS_LAB / "05_findings"
STACKS = CORPUS_LAB / "02_stacks"

RUNS = _env_path("RUNS_ROOT") or _first_existing(
    PRIVATE / "results" / "03_runs", CORPUS_LAB / "03_runs")
SCORES = _env_path("SCORES_ROOT") or _first_existing(
    PRIVATE / "results" / "04_scores", CORPUS_LAB / "04_scores")
HARNESS_KEYS = _env_path("HARNESS_KEYS") or _first_existing(
    PRIVATE / "harness_keys", HARNESS / "keys")
CANARY_DIR = PRIVATE / "canaries"

# --- files -------------------------------------------------------------------
HOOK = BIN / "hook_frontdoor.py"
SEARCH = BIN / "corpus_search.py"
ASK = BIN / "ask.py"
PLOG = BIN / "plog.py"
ANSWER_KEY = HARNESS_KEYS / "answer_key.json"
CANARY_SLOTS = HARNESS_KEYS / "canary_slots.json"
QUESTION_SAMPLE = SCORES / "question_sample.json"

# Night-1's undiagnosed stream-json break, root-caused in phase 2
# (see bin/diag_quotes.py and state/diag_quotes.json).
#
# claude.cmd is a cmd.exe batch wrapper whose operative line is
#     "%dp0%\node_modules\@anthropic-ai\claude-code\bin\claude.exe"   %*
# and cmd.exe's %* expansion TRUNCATES AT THE FIRST NEWLINE inside an argument.
# So any prompt containing "\n" silently loses every argument after it --
# including --output-format stream-json. The session then runs in default text
# mode: prose out, returncode 0, empty stderr. Exactly the reported symptom.
#
# It was never the corpus root. ra-ship ran single-line canary questions and
# worked; run_harness.py appends "\n\nCite the exact file paths..." to every
# harness question, so 100% of harness questions broke and 0% of ra-ship canary
# questions did. The apparent correlation with the root was an artifact of which
# question builder each root happened to use.
#
# Fix: call claude.exe directly, never through cmd.exe. Verified across 8 prompt
# shapes (newlines, double/single quotes, backticks, ^ & %): all 8 STREAM_JSON
# via the .exe, 2 of 8 PROSE via the .cmd.
# Phase 11 (F-01): the two absolute paths below used to be this machine's and
# nothing else. INSTALL.md promises "Claude Code on the PATH" and a Git for
# Windows pdftotext, and neither was ever looked up, so on any second machine
# doctor printed FAIL next to a directory the reader does not have. Both are now
# resolved in order: env var, then this machine's historical location (so no
# recorded run changes), then PATH, then the ordinary install locations. The
# last resort is a bare name, which fails with "not found" rather than with a
# stranger's directory.
_NVM_CLAUDE_EXE = Path(r"C:\nvm4w\nodejs\node_modules\@anthropic-ai\claude-code\bin\claude.exe")


def _sibling_claude_exe(p):
    """npm puts claude.cmd in the global bin and the real .exe under
    node_modules/@anthropic-ai/claude-code/bin beside it. CreateProcess cannot
    run the .cmd wrapper (see the note above), so prefer the .exe when present."""
    exe = p.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    return exe if exe.exists() else None


def _exists(p):
    try:
        return p is not None and str(p) and p.exists()
    except OSError:
        return False


def find_claude():
    """Absolute path to a runnable Claude Code executable, else the bare name."""
    v = os.environ.get("CLAUDE_CMD")
    if v:
        return v
    if _exists(_NVM_CLAUDE_EXE):
        return str(_NVM_CLAUDE_EXE)
    for name in ("claude.exe", "claude.cmd", "claude"):
        w = shutil.which(name)
        if not w:
            continue
        w = Path(w)
        if w.suffix.lower() == ".exe":
            return str(w)
        return str(_sibling_claude_exe(w) or w)
    tail = ("node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")
    for base in (Path.home() / ".local" / "bin",
                 Path(os.environ.get("APPDATA", ".")) / "npm",
                 Path(os.environ.get("ProgramFiles", ".")) / "nodejs"):
        cand = base / "claude.exe"
        if _exists(cand):
            return str(cand)
        cand = base.joinpath(*tail)
        if _exists(cand):
            return str(cand)
    return "claude.exe"


CLAUDE = find_claude()

_GIT_PDFTOTEXT = Path(r"C:\Program Files\Git\mingw64\bin\pdftotext.exe")


def find_pdftotext():
    """Absolute path to pdftotext, else the bare name."""
    v = os.environ.get("PDFTOTEXT_EXE")
    if v:
        return v
    if _exists(_GIT_PDFTOTEXT):
        return str(_GIT_PDFTOTEXT)
    w = shutil.which("pdftotext")
    if w:
        return w
    for cand in (Path(os.environ.get("ProgramFiles", ".")) / "Git" / "mingw64" / "bin" / "pdftotext.exe",
                 Path(os.environ.get("ProgramFiles(x86)", ".")) / "Git" / "mingw64" / "bin" / "pdftotext.exe",
                 Path(os.environ.get("LOCALAPPDATA", ".")) / "Programs" / "Git" / "mingw64" / "bin" / "pdftotext.exe",
                 Path(os.environ.get("USERPROFILE", ".")) / "scoop" / "shims" / "pdftotext.exe",
                 Path(os.environ.get("ProgramData", ".")) / "chocolatey" / "bin" / "pdftotext.exe"):
        if _exists(cand):
            return str(cand)
    return "pdftotext.exe"


PDFTOTEXT = find_pdftotext()


def canary_manifest(required=True):
    """Phase 2.3: the pass-1 manifest location is a secret the scripts must not
    carry. Only the scoring invocation sets CANARY_MANIFEST; a test session that
    reads this file learns the name of an env var and nothing else."""
    v = os.environ.get("CANARY_MANIFEST")
    if not v:
        if required:
            raise SystemExit(
                "CANARY_MANIFEST is not set. The canary manifest path is deliberately "
                "not hardcoded (grid-run phase 2.3). Set it for scoring invocations only.")
        return None
    return Path(v)


def rung(n):
    return HARNESS / f"corpus_{n}"


def hook_log(stack, corpus_label):
    """Phase 0.9: one log per (stack, corpus). A single shared log interleaves
    evidence from every stack and becomes a de-facto phrase list."""
    p = RUNS / "hooks" / f"{stack}__{corpus_label}.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def describe():
    return {k: str(v) for k, v in sorted(globals().items())
            if isinstance(v, Path) and not k.startswith("_")}


if __name__ == "__main__":
    import json
    d = describe()
    d["CLAUDE"] = CLAUDE
    d["PDFTOTEXT"] = PDFTOTEXT
    d["CANARY_MANIFEST(env)"] = os.environ.get("CANARY_MANIFEST", "<unset>")
    print(json.dumps(d, indent=1))
