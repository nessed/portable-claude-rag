#!/usr/bin/env python
"""stack.py - install / remove a candidate stack's delivery layer in a corpus root.

Writes ONLY into <corpus>/.claude/settings.json and <corpus>/CLAUDE.md, records
whatever was there before, and restores exactly on teardown. Nothing else in the
corpus is ever touched -- that is a hard stop in the grid-run spec.

Stacks:
  s0_baseline  NOTHING. No CLAUDE.md, no hook. settings.json carries only the
               phase-2.5 backstop deny and nothing else.
  s1_policy    CLAUDE.md tells the agent the index exists. No hook. Asserts no
               hook block is present first.  -> is TELLING enough?
  s2_hook      PreToolUse hook denies Grep/Glob and Bash crawls, redirecting to
               the index. Plus the same CLAUDE.md.  -> or is FORCING required?
  s3_hybrid    as s2, but the front door runs FTS5+embeddings hybrid.
  s4_pdfmcp    as s2's deny, but the front door is the pdf-mcp MCP server.

Grid-run phase fixes:
  0.4  s0_baseline installs NO CLAUDE.md. Night 1's S0 and S2 differed by two
       things at once, so neither number isolated anything.
  0.5  s1_policy ABORTS if a hook block already exists -- a leftover install
       silently converts the telling-vs-forcing test into a forcing test.
  0.8  state file is written BEFORE the corpus is mutated, so a crash mid-install
       still leaves a record of a live policy or hook. Backups are keyed by corpus
       (hash of the root path), so two corpora that both have a CLAUDE.md cannot
       collide and restore the wrong bytes into the wrong tree.
  2.5  every stack, including s0, gets the backstop deny on the private tree.
"""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labpaths as L

PY = sys.executable

STACKS = ("s0_baseline", "s1_policy", "s2_hook", "s3_hybrid", "s4_pdfmcp", "s5_recoll")
POLICY_STACKS = ("s1_policy", "s2_hook", "s3_hybrid", "s4_pdfmcp", "s5_recoll")
HOOK_STACKS = ("s2_hook", "s3_hybrid", "s4_pdfmcp", "s5_recoll")

# Phase 2.5 backstop. Present in EVERY stack, s0 included, where it is the only
# content of settings.json.
#
# MEASURED, phase 2.6 + bin/diag_denyform.py -- the spec's pattern
# `Read(**/_private/**)` DOES NOT WORK. Under --permission-mode bypassPermissions
# a Read() deny binds only when the pattern carries an ABSOLUTE path prefix.
# Relative globs match nothing and fail open, silently:
#
#   Read(**/_private/**)                    LEAKS   <- the spec's pattern
#   Read(*_private*)                        LEAKS
#   Read(**/canary_manifest*)               LEAKS
#   Read(//C:/.../_private/**)              LEAKS   (leading // breaks it)
#   Read(C:\...\_private\**)                BINDS
#   Read(C:/.../_private/**)                BINDS
#   Read(C:/.../_private/**/*)              BINDS
#
# Bash() patterns are matched against the command STRING, so the relative forms
# there do bind and are kept. The absolute Read denies necessarily name the
# directory they protect; that is fine, because knowing a denied path does not
# help you read it, and a path-based deny cannot work any other way.
def _abs_read_denies():
    out = []
    for p in (L.PRIVATE, Path.home() / ".claude" / "projects"):
        s = str(p).replace("\\", "/").rstrip("/")
        out.append(f"Read({s}/**)")
        out.append(f"Read({str(p).rstrip(chr(92))}\\**)")
    return out


BACKSTOP_DENY = _abs_read_denies() + [
    # Bash is matched on the command string, so relative patterns DO bind here.
    "Bash(*_private*)",
    "Bash(*canary*)",
    "Bash(*answer_key*)",
    "Bash(*harness_keys*)",
    "Bash(*canary_manifest*)",
    "Bash(*canary_companion*)",
    "Bash(*plant_backups*)",
    # The transcript route (see quarantine_transcripts.py): ra-ship deliberately
    # does not move, so every ra-ship session runs under the same
    # .claude/projects key whose transcripts quoted the canaries.
    "Bash(*.claude/projects*)",
    "Bash(*projects/C--*)",
]

CLAUDE_MD = """# Corpus retrieval policy

The built-in Grep and Glob cannot be relied on to find research material in this
tree. They miss files silently -- a "No files found" from them is not evidence
that the material is absent.

A page-level index of the whole corpus (including PDFs, DOCX, XLSX and anything
excluded from ordinary search) is available. Use it first:

    python "{search}" "a whole question in plain words"   # ranked page hits
    python "{search}" --exact "literal"       # exact phrase
    python "{search}" --page <path> <n>       # read one page
    python "{search}" --coverage              # what is and is not indexed

## Never answer from a search result alone

A search result gives you a path, a page number and a short snippet. The snippet
is an advertisement for a page, not the page. It is cut mid-sentence, it omits the
table the number lives in, and the ranking that produced it is often wrong.

Before you cite any file, you MUST:

1. open the page you intend to cite -- `--page <path> <page_index>`, or Read the
   file if it is not a PDF;
2. quote, verbatim and in your answer, the line or table cell carrying the number
   or fact you are asserting;
3. put the file path and page_index next to that quote.

If you did not open it, you may not cite it. If you opened it and the fact is not
on that page, say so and keep looking -- do not cite the page anyway because it
was ranked first.

If no page you opened contains the answer, say it was not found and list what you
searched. A wrong number with a confident citation is worse than no answer: the
reader cannot tell it is wrong without redoing the work themselves, which is the
work they asked you to do.

Before saying something is absent, run --coverage and say what was not searched.
"""

# S4 points at an off-the-shelf MCP server instead of our own front door. Same
# shape of instruction, so the comparison is about the retrieval layer and not
# about how differently the two were described.
CLAUDE_MD_S4 = """# Corpus retrieval policy

The built-in Grep and Glob cannot be relied on to find research material in this
tree. They miss files silently -- a "No files found" from them is not evidence
that the material is absent.

A PDF corpus server is available over MCP. Use it first:

    pdf_corpus_warm        index the tree before searching it
    pdf_corpus_search      search the warmed corpus, returns file + page hits
    pdf_read_pages         read specific pages of a specific PDF
    pdf_corpus_overview    what the server knows about this tree

Cite file path + page number for every claim. Before saying something is absent,
say what the server did and did not cover.

NOTE: this server handles PDFs only. Material in DOCX, XLSX, CSV, JSON, HTML or
markdown is outside what it can see, and a miss there is a limit of this tool
rather than evidence the material is absent.
"""


def corpus_key(root):
    """Short stable id for a corpus root. Phase 0.8: backups keyed by corpus so
    two trees that both hold a CLAUDE.md cannot overwrite each other's backup."""
    r = str(Path(root).resolve()).lower()
    return Path(root).name + "_" + hashlib.sha1(r.encode()).hexdigest()[:8]


def backup_dir(root):
    d = L.SCRATCH / "backups" / corpus_key(root)
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path(root):
    return L.SCRATCH / "backups" / f"stack_state__{corpus_key(root)}.json"


def settings_for(stack):
    """Every stack gets the backstop deny. Only hook stacks get a hook."""
    cfg = {"permissions": {"deny": list(BACKSTOP_DENY)}}
    if stack in HOOK_STACKS:
        cfg["hooks"] = {"PreToolUse": [{
            "matcher": "Grep|Glob|Bash",
            "hooks": [{"type": "command",
                       "command": '"{}" "{}"'.format(PY, L.HOOK)}]}]}
    return cfg


def has_hook_block(path):
    if not Path(path).exists():
        return False
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return False
    return bool(cfg.get("hooks"))


# --------------------------------------------------------------------------- #
# evidence_v1 -- Stage 7.1. Unlike the grid-run stacks above (which always
# write onto a clean fixture baseline and so can safely overwrite), this
# stack must MERGE into whatever CLAUDE.md / settings.json Ali's real folder
# already has, own only a clearly marked section/keys, and be able to remove
# exactly those on uninstall without touching anything else. Added, not
# substituted for, the existing STACKS machinery above.
# --------------------------------------------------------------------------- #

EV1_MD_BEGIN = "<!-- EVIDENCE_V1:BEGIN -->"
EV1_MD_END = "<!-- EVIDENCE_V1:END -->"
EV1_MARKER = "__evidence_v1_owned__"


def _ev1_policy_text():
    policy_path = Path(__file__).resolve().parent.parent / "evidence_v1" / "policy.txt"
    return policy_path.read_text(encoding="utf-8").strip()


def _ev1_merge_claude_md(existing_text, cli_path):
    body = "{}\n\n{}\n{}".format(EV1_MD_BEGIN, _ev1_policy_text(), EV1_MD_END)
    if existing_text is None:
        return body + "\n"
    if EV1_MD_BEGIN in existing_text and EV1_MD_END in existing_text:
        pre = existing_text.split(EV1_MD_BEGIN)[0]
        post = existing_text.split(EV1_MD_END)[1]
        return pre + body + post
    sep = "\n\n" if existing_text and not existing_text.endswith("\n\n") else ""
    return existing_text + sep + body + "\n"


def _ev1_strip_claude_md(existing_text):
    if EV1_MD_BEGIN not in existing_text or EV1_MD_END not in existing_text:
        return existing_text, False
    pre = existing_text.split(EV1_MD_BEGIN)[0]
    post = existing_text.split(EV1_MD_END)[1]
    return pre + post, True


def _ev1_hook_entry(py_exe, script_path, extra_env_note=""):
    return {"type": "command", "command": '"{}" "{}"'.format(py_exe, script_path)}


def ev1_settings_fragment(py_exe, hooks_dir, cli_path):
    deny = list(BACKSTOP_DENY)
    return {
        "permissions": {"deny": deny},
        "hooks": {
            "SessionStart": [{"matcher": "", "hooks": [
                _ev1_hook_entry(py_exe, str(hooks_dir / "session_start.py"))]}],
            "UserPromptSubmit": [{"matcher": "", "hooks": [
                _ev1_hook_entry(py_exe, str(hooks_dir / "user_prompt_submit.py"))]}],
            "PreToolUse": [{"matcher": "Bash|Read|Grep|Glob|Write|Edit", "hooks": [
                _ev1_hook_entry(py_exe, str(hooks_dir / "pretooluse_entry.py"))]}],
            "Stop": [{"matcher": "", "hooks": [
                _ev1_hook_entry(py_exe, str(hooks_dir / "stop_entry.py"))]}],
        },
        EV1_MARKER: True,
    }


def _merge_deny_lists(existing_deny, new_deny):
    merged = list(existing_deny or [])
    for d in new_deny:
        if d not in merged:
            merged.append(d)
    return merged


def _merge_hook_event(existing_events, new_events):
    merged = list(existing_events or [])
    merged.extend(new_events)
    return merged


def setup_evidence_v1(corpus, py_exe, hooks_dir, cli_path):
    root = Path(corpus).resolve()
    if not root.is_dir():
        sys.exit(f"corpus root does not exist: {root}")
    cdir = root / ".claude"
    md = root / "CLAUDE.md"
    sj = cdir / "settings.json"

    bdir = backup_dir(root)
    state = {"stack": "evidence_v1", "corpus": str(root), "corpus_key": corpus_key(root),
             "backup_dir": str(bdir), "created": [], "backed_up": [], "status": "installing",
             "created_claude_dir": not cdir.exists(), "merged": True}
    sp = state_path(root)
    sp.parent.mkdir(parents=True, exist_ok=True)

    orig_md_bytes = md.read_bytes() if md.exists() else None
    orig_sj_bytes = sj.read_bytes() if sj.exists() else None
    state["orig_md_sha256"] = hashlib.sha256(orig_md_bytes).hexdigest() if orig_md_bytes else None
    state["orig_sj_sha256"] = hashlib.sha256(orig_sj_bytes).hexdigest() if orig_sj_bytes else None
    state["created" if orig_md_bytes is None else "backed_up"].append(str(md))
    state["created" if orig_sj_bytes is None else "backed_up"].append(str(sj))
    sp.write_text(json.dumps(state, indent=1), encoding="utf-8")

    cdir.mkdir(exist_ok=True)
    if orig_md_bytes is not None:
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "CLAUDE.md.bak").write_bytes(orig_md_bytes)
    if orig_sj_bytes is not None:
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "settings.json.bak").write_bytes(orig_sj_bytes)

    existing_md_text = orig_md_bytes.decode("utf-8") if orig_md_bytes else None
    new_md_text = _ev1_merge_claude_md(existing_md_text, cli_path)
    md.write_text(new_md_text, encoding="utf-8")

    existing_cfg = json.loads(orig_sj_bytes.decode("utf-8")) if orig_sj_bytes else {}
    fragment = ev1_settings_fragment(py_exe, hooks_dir, cli_path)
    merged_cfg = dict(existing_cfg)
    merged_cfg.setdefault("permissions", {})
    merged_cfg["permissions"]["deny"] = _merge_deny_lists(
        existing_cfg.get("permissions", {}).get("deny"), fragment["permissions"]["deny"])
    merged_cfg.setdefault("hooks", {})
    for event, entries in fragment["hooks"].items():
        merged_cfg["hooks"][event] = _merge_hook_event(
            existing_cfg.get("hooks", {}).get(event), entries)
    merged_cfg[EV1_MARKER] = True
    sj.write_text(json.dumps(merged_cfg, indent=2), encoding="utf-8")

    state["status"] = "installed"
    sp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    print(f"INSTALLED evidence_v1 in {root}")
    print("  CLAUDE.md: merged, marked section owned")
    print("  settings.json: merged, unrelated keys preserved")
    return {"ok": True, "root": str(root)}


def teardown_evidence_v1(corpus):
    root = Path(corpus).resolve()
    sp = state_path(root)
    if not sp.exists():
        return {"ok": True, "note": "no evidence_v1 state for this corpus"}
    state = json.loads(sp.read_text(encoding="utf-8"))
    if state.get("stack") != "evidence_v1":
        return {"ok": False, "reason": "state file belongs to a different stack; not touching it"}

    md = root / "CLAUDE.md"
    sj = root / ".claude" / "settings.json"
    bdir = Path(state["backup_dir"])

    if md.exists():
        current = md.read_text(encoding="utf-8")
        stripped, had_section = _ev1_strip_claude_md(current)
        if had_section:
            if state.get("orig_md_sha256") is None:
                if stripped.strip() == "":
                    md.unlink()
                else:
                    md.write_text(stripped, encoding="utf-8")
            else:
                bak = bdir / "CLAUDE.md.bak"
                if bak.exists() and hashlib.sha256(bak.read_bytes()).hexdigest() == state["orig_md_sha256"]:
                    shutil.copy2(bak, md)
                else:
                    return {"ok": False, "reason": "CONFIG_CONFLICT",
                            "detail": "cannot prove exact restoration of CLAUDE.md; "
                                      "both current and backup preserved, nothing removed"}

    if sj.exists():
        try:
            cfg = json.loads(sj.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"ok": False, "reason": "CONFIG_CONFLICT",
                     "detail": "settings.json is not valid JSON; leaving it untouched"}
        if cfg.get(EV1_MARKER):
            if state.get("orig_sj_sha256") is None:
                cfg.pop(EV1_MARKER, None)
                for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"):
                    if event in cfg.get("hooks", {}):
                        cfg["hooks"][event] = [
                            h for h in cfg["hooks"][event]
                            if "evidence_v1" not in json.dumps(h)]
                        if not cfg["hooks"][event]:
                            del cfg["hooks"][event]
                if not cfg.get("hooks"):
                    cfg.pop("hooks", None)
                for d in BACKSTOP_DENY:
                    if d in cfg.get("permissions", {}).get("deny", []):
                        cfg["permissions"]["deny"].remove(d)
                if not cfg.get("permissions", {}).get("deny"):
                    cfg.pop("permissions", None)
                if cfg:
                    sj.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
                else:
                    sj.unlink()
            else:
                bak = bdir / "settings.json.bak"
                if bak.exists() and hashlib.sha256(bak.read_bytes()).hexdigest() == state["orig_sj_sha256"]:
                    shutil.copy2(bak, sj)
                else:
                    return {"ok": False, "reason": "CONFIG_CONFLICT",
                             "detail": "cannot prove exact restoration of settings.json; "
                                       "both current and backup preserved, nothing removed"}

    if state.get("created_claude_dir"):
        cdir = root / ".claude"
        try:
            if cdir.is_dir() and not any(cdir.iterdir()):
                cdir.rmdir()
        except OSError:
            pass
    sp.unlink()
    return {"ok": True, "root": str(root)}


def setup(corpus, stack, db):
    if stack not in STACKS:
        sys.exit(f"unknown stack {stack!r}; known: {', '.join(STACKS)}")
    root = Path(corpus).resolve()
    if not root.is_dir():
        sys.exit(f"corpus root does not exist: {root}")
    cdir = root / ".claude"
    md = root / "CLAUDE.md"
    sj = cdir / "settings.json"

    # --- 0.5: the policy-only stack must not inherit someone else's hook -------
    if stack == "s1_policy" and has_hook_block(sj):
        sys.exit(
            "ABORT: s1_policy requires NO hook block, but one is present at\n"
            f"  {sj}\n"
            "A leftover install would silently turn the telling-vs-forcing test "
            "into a forcing test. Run `stack.py teardown` first.")

    bdir = backup_dir(root)
    state = {"stack": stack, "corpus": str(root), "corpus_key": corpus_key(root),
             "backup_dir": str(bdir), "created": [], "backed_up": [],
             "db": str(db), "status": "installing"}

    # --- 0.8: write the state file BEFORE mutating anything --------------------
    # A crash between here and the end of setup leaves a recoverable record; the
    # old order (state last) could leave a live hook with nothing pointing at it.
    want_md = stack in POLICY_STACKS
    if want_md:
        state["created" if not md.exists() else "backed_up"].append(str(md))
    state["created" if not sj.exists() else "backed_up"].append(str(sj))
    # If we create .claude/ ourselves, we remove it again on teardown -- otherwise
    # the "untouched corpus" baseline quietly acquires a directory that persists
    # into every later stack.
    state["created_claude_dir"] = not cdir.exists()
    sp = state_path(root)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(state, indent=1), encoding="utf-8")

    cdir.mkdir(exist_ok=True)
    if md.exists() and want_md:
        shutil.copy2(md, bdir / "CLAUDE.md.bak")
    if sj.exists():
        shutil.copy2(sj, bdir / "settings.json.bak")

    # --- 0.4: baseline installs NO policy file --------------------------------
    if want_md:
        body = (CLAUDE_MD_S4 if stack == "s4_pdfmcp"
                else CLAUDE_MD.format(search=L.SEARCH))
        md.write_text(body, encoding="utf-8")
    sj.write_text(json.dumps(settings_for(stack), indent=2), encoding="utf-8")

    state["status"] = "installed"
    sp.write_text(json.dumps(state, indent=1), encoding="utf-8")

    print(f"INSTALLED {stack} in {root}")
    print(f"  CLAUDE.md   : {'written' if want_md else 'NOT written (baseline)'}")
    print(f"  settings.json: backstop deny"
          f"{' + PreToolUse hook' if stack in HOOK_STACKS else ' ONLY'}")
    print(f"  backups     : {bdir}")
    print(f"  CORPUS_DB should be set to: {db}")


def teardown(corpus=None):
    """Restore every corpus that has a state file, or just the one named."""
    bdir = L.SCRATCH / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    if corpus:
        states = [state_path(corpus)]
    else:
        states = sorted(bdir.glob("stack_state__*.json"))
    states = [s for s in states if s.exists()]
    if not states:
        print("no stack installed")
        return
    for sp in states:
        state = json.loads(sp.read_text(encoding="utf-8"))
        d = Path(state["backup_dir"])
        for p in state.get("created", []):
            if Path(p).exists():
                Path(p).unlink()
                print(f"  removed {p}")
        for p in state.get("backed_up", []):
            bak = d / (Path(p).name + ".bak")
            if bak.exists():
                shutil.copy2(bak, p)
                print(f"  restored {p} from {bak}")
            else:
                print(f"  WARNING: no backup for {p}; left as-is", file=sys.stderr)
        if state.get("created_claude_dir"):
            cdir = Path(state["corpus"]) / ".claude"
            try:
                if cdir.is_dir() and not any(cdir.iterdir()):
                    cdir.rmdir()
                    print(f"  removed {cdir} (we created it)")
            except OSError as e:
                print(f"  WARNING: could not remove {cdir}: {e}", file=sys.stderr)
        sp.unlink()
        print(f"REMOVED {state['stack']} from {state['corpus']}")


def status(corpus):
    root = Path(corpus).resolve()
    sp = state_path(root)
    md = root / "CLAUDE.md"
    sj = root / ".claude" / "settings.json"
    out = {
        "corpus": str(root),
        "state_file": str(sp), "state_present": sp.exists(),
        "state": json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else None,
        "CLAUDE.md_present": md.exists(),
        "settings.json_present": sj.exists(),
        "hook_block_present": has_hook_block(sj),
        "settings_content": (json.loads(sj.read_text(encoding="utf-8"))
                             if sj.exists() else None),
    }
    print(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["setup", "teardown", "status",
                                        "setup-evidence-v1", "teardown-evidence-v1"])
    ap.add_argument("--corpus", default=None)
    ap.add_argument("--stack", default="s2_hook")
    ap.add_argument("--db", default=None)
    a = ap.parse_args()
    L.SCRATCH.mkdir(parents=True, exist_ok=True)
    corpus = a.corpus or str(L.RASHIP)
    db = a.db or str(L.STACKS / "s2_fts5" / "raship.db")
    if a.action == "setup-evidence-v1":
        py_exe = sys.executable
        hooks_dir = Path(__file__).resolve().parent.parent / "evidence_v1"
        cli_path = Path(__file__).resolve().parent / "evidence.py"
        r = setup_evidence_v1(corpus, py_exe, hooks_dir, cli_path)
        print(json.dumps(r))
        sys.exit(0 if r.get("ok") else 1)
    elif a.action == "teardown-evidence-v1":
        r = teardown_evidence_v1(corpus)
        print(json.dumps(r))
        sys.exit(0 if r.get("ok") else 1)
    elif a.action == "setup":
        setup(corpus, a.stack, db)
    elif a.action == "status":
        status(corpus)
    else:
        teardown(a.corpus)
