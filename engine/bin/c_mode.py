#!/usr/bin/env python
"""c_mode.py -- the research assistant's switch, its briefing, and the file guard.

Paste-in layout (2026-10-10): the package sits INSIDE the person's documents folder:

    <their folder>\\CLAUDE.md, .claude\\, engine\\, <all their documents>

Everything here is resolved from this file's own location (engine\\bin\\c_mode.py), so the
folder can be moved and nothing breaks. Three states, decided by two flag files:

    not set up   engine\\artefacts\\READY absent      -> CLAUDE.md says: say `initialize`
    ON           READY present, engine\\OFF absent    -> questions go through the engine;
                                                         nothing in the folder may be written
    OFF          engine\\OFF present                   -> plain Claude Code; /ask still works

Subcommands (all run by hooks.cmd, which the shipped .claude\\settings.json points at):

    status      what state applies, the index date and counts, files added since
    on | off    remove / create engine\\OFF
    prompt      UserPromptSubmit hook: one briefing line per turn, so the state is visible
    session     SessionStart hook: the same, after recounting files on disk
    guard       PreToolUse hook: while ON, refuse any write, move or delete outside engine\\
    --selftest  the guard's decisions on fixed inputs, no folder needed

Exit codes follow the Claude Code hook contract: 0 with a JSON decision on stdout.
"""
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labpaths as L  # noqa: E402

ENGINE = Path(__file__).resolve().parent.parent          # engine\
ROOT = ENGINE.parent                                     # the person's folder
READY = ENGINE / "artefacts" / "READY"
OFF = ENGINE / "OFF"
DB = ENGINE / "artefacts" / "pages.db"
CACHE = ENGINE / "state" / "mode_cache.json"
ANSWER_MD = "engine/ANSWER.md"

# Tools the guard looks at. Write-shaped tools are judged by their target path; shell tools
# by the verbs in the command.
PATH_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}

# A shell command may run while ON only if every piece of it starts with one of these, or
# with the engine's own interpreter / wrapper, and it contains no redirect.
READ_ONLY = {
    "ls", "dir", "cat", "type", "head", "tail", "wc", "grep", "egrep", "fgrep", "findstr",
    "find", "pdftotext", "pdfinfo", "pdftoppm", "echo", "pwd", "cd", "where", "which",
    "sort", "uniq", "cut", "tr", "awk", "sed", "diff", "cmp", "stat", "file", "md5sum",
    "sha256sum", "true", "date", "jq", "less", "more", "tree", "du", "df", "nl", "tac",
    "rev", "basename", "dirname", "realpath", "readlink", "test", "[", "printf", "env",
    "set", "export", "python", "python3", "py",   # bare python only with -c print; see below
    "get-childitem", "get-content", "select-string", "get-item", "test-path",
    "get-location", "get-childitem", "measure-object", "select-object", "where-object",
    "format-table", "format-list", "write-output", "write-host", "get-date", "resolve-path",
    "get-filehash", "gci", "gc", "ls", "sls", "cat", "pwd", "echo",
}
ENGINE_EXES = ("python.exe", "python", "hooks.cmd", "c_mode.py", "c_shelf.py",
               "setup_folder.py")
# `python -c` is read-only only when it prints; anything that opens a file for writing,
# removes, renames or imports a shell is refused.
PY_C_WRITE = re.compile(r"open\([^)]*['\"][wa]|os\.(remove|unlink|rename|replace|rmdir|"
                        r"makedirs|mkdir)|shutil\.|subprocess|rmtree|write_text|write_bytes|"
                        r"Path\([^)]*\)\.(write|unlink|rename|mkdir|touch)", re.I)
REDIRECT_RE = re.compile(r"(?<![0-9])>{1,2}(?!&)")
# A piece that is just a string literal or a variable: PowerShell prints it. The runbook's
# own selftest step ends in `; "selftest exit=$LASTEXITCODE"` and Test 2 saw it refused.
BARE_VALUE_RE = re.compile(r"""^(?:"[^"]*"|'[^']*'|\$[A-Za-z_?][A-Za-z0-9_]*)$""")


def _split_pieces(cmd):
    """Split a shell command at ; | && || and newlines, but not inside quotes, so a
    `python -c "import os; print(...)"` is one piece and not two."""
    out, buf, q, i = [], [], None, 0
    while i < len(cmd):
        ch = cmd[i]
        if q:
            buf.append(ch)
            if ch == q:
                q = None
        elif ch in ("'", '"'):
            q = ch
            buf.append(ch)
        elif ch == "&" and buf and buf[-1] == ">":
            buf.append(ch)                    # 2>&1 / >&2: a redirect, not a separator
        elif ch in "\n;|&":
            out.append("".join(buf))
            buf = []
            while i + 1 < len(cmd) and cmd[i + 1] in "|&;":
                i += 1
        else:
            buf.append(ch)
        i += 1
    out.append("".join(buf))
    return [x for x in out if x.strip()]

DENY_PATH = ("Research assistant is ON, so it never changes files in this folder: your "
             "documents are read-only to it and the only place it may write is engine\\. "
             "If you want this change made, switch it off first with /rag-off (plain Claude "
             "Code), then ask again.")
DENY_SHELL = ("Research assistant is ON, so only read-only commands and the assistant's own "
              "engine commands run. This command could create, change or delete a file. If "
              "that is what you want, switch it off first with /rag-off, then ask again.")


# --------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------- #
def state(root=None):
    r = Path(root) if root else ROOT
    if (r / "engine" / "OFF").exists():
        return "off"
    if (r / "engine" / "artefacts" / "READY").exists():
        return "on"
    return "not_set_up"


def _is_under(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except (ValueError, OSError):
        return False


def _resolve_target(p, cwd):
    p = str(p or "").strip().strip('"').strip("'")
    if not p:
        return None
    q = Path(p)
    if not q.is_absolute():
        q = Path(cwd or os.getcwd()) / q
    return q


# --------------------------------------------------------------------- #
# the guard (pure: root and cwd are parameters so --selftest can drive it)
# --------------------------------------------------------------------- #
def _first_token(piece):
    piece = piece.strip()
    if not piece:
        return ""
    m = re.match(r'"([^"]+)"|\'([^\']+)\'|(\S+)', piece)
    if not m:
        return ""
    return (m.group(1) or m.group(2) or m.group(3) or "").strip()


def _is_engine_exe(tok, root):
    t = tok.replace("\\", "/").lower()
    base = t.rsplit("/", 1)[-1]
    if base not in ENGINE_EXES:
        return False
    if base in ("python.exe", "python"):
        # only the engine's own interpreter counts, not whatever `python` is on PATH
        return "/engine/.venv/" in t or t.startswith("engine/.venv/")
    return "engine/bin/" in t or t.startswith("engine/bin/") or base in ("hooks.cmd",)


HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
SUBSHELL_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
REDIRECT_TARGET_RE = re.compile(r"""(?<![0-9<])>{1,2}(?!&)\s*("[^"]*"|'[^']*'|\S+)?""")
SHELL_NOISE = {"done", "fi", "esac", "}", ")", "{", "(", ";;", "do", "then", "else"}
SHELL_PREFIX = ("do ", "then ", "else ", "elif ", "if ", "while ", "until ", "! ", "time ")


def _strip_heredocs(cmd):
    """Drop here-document bodies, so a draft's prose lines are not read as commands."""
    out, lines, i = [], cmd.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = HEREDOC_RE.search(line)
        i += 1
        if m:
            delim = m.group(2)
            while i < len(lines) and lines[i].strip() != delim:
                i += 1
            i += 1
    return "\n".join(out)


def _shell_cwd(cmd, root):
    """`cd "<dir>" && ...` is how Claude Code runs things; relative paths that follow
    resolve against that dir, otherwise against the folder."""
    first = _split_pieces(cmd)[:1]
    if first and _first_token(first[0]) == "cd":
        rest = first[0].strip()[2:].strip()
        return _resolve_target(rest, root) or Path(root)
    return Path(root)


WRITE_IN_SCRATCH = {"rm", "mv", "cp", "touch", "sed", "del", "copy", "move"}
SCRATCH_DIRS = ("99_scratch", "state")


def _split_args(piece):
    return re.findall(r'"[^"]*"|\'[^\']*\'|\S+', piece.strip())


def _in_scratch(target, root, cwd):
    q = _resolve_target(target, cwd)
    if q is None:
        return False
    for d in SCRATCH_DIRS:
        base = Path(root) / "engine" / d
        if _is_under(q, base) and q.resolve() != base.resolve():
            return True
    return False


def _under_engine(target, root, cwd):
    q = _resolve_target(target, cwd)
    return q is not None and _is_under(q, Path(root) / "engine")


def _redirects_ok(cmd, root, cwd):
    """Every `>` / `>>` must point at /dev/null or at a file under engine/."""
    for m in REDIRECT_TARGET_RE.finditer(cmd):
        target = (m.group(1) or "").strip().strip('"').strip("'")
        if target.lower() in ("/dev/null", "nul"):
            continue
        if not target or not _under_engine(target, root, cwd):
            return False
    return True


def shell_allowed(command, root):
    """True if the command is read-only or an engine command. False means deny."""
    cmd = _strip_heredocs(command or "")
    cwd = _shell_cwd(cmd, root)
    if not _redirects_ok(cmd, root, cwd):
        return False
    for m in SUBSHELL_RE.finditer(cmd):
        inner = m.group(1) or m.group(2) or ""
        if inner.strip() and not shell_allowed(inner, root):
            return False
    for piece in _split_pieces(cmd):
        piece = piece.strip()
        while piece.startswith(SHELL_PREFIX):
            piece = piece.split(" ", 1)[1].strip()
        if (_first_token(piece) in SHELL_NOISE or piece.startswith("for ")
                or piece.startswith("case ")):
            continue                          # loop header or closer: runs nothing itself
        tok = _first_token(piece)
        if not tok:
            continue
        if tok == "mkdir":
            args = [a for a in piece.split()[1:] if not a.startswith("-")]
            if args and all(_under_engine(a, root, cwd) for a in args):
                continue
            return False
        if tok in WRITE_IN_SCRATCH:
            # the engine's own working files: every path must sit under engine/99_scratch or
            # engine/state (Test 3, Q13: `sed -i` on the draft answer was refused). For sed
            # the script argument is not a path, so only the last argument is checked.
            args = [a.strip('"').strip("'") for a in _split_args(piece)[1:] if not a.startswith("-")]
            if tok == "sed":
                args = args[-1:]
            if args and all(_in_scratch(a, root, cwd) for a in args):
                continue
            return False
        if BARE_VALUE_RE.match(piece.strip()):
            # a PowerShell expression that only prints: "selftest exit=$LASTEXITCODE", $?
            continue
        if _is_engine_exe(tok, root):
            continue
        base = tok.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if base.endswith(".exe"):
            base = base[:-4]
        if base not in READ_ONLY:
            return False
        if base in ("python", "python3", "py"):
            # a bare interpreter: allow only `python -c "<print-only>"` and `--version`
            rest = piece.strip()[len(tok):].strip()
            if rest.startswith("--version") or rest.startswith("-V"):
                continue
            # interpreter flags before -c (python -I -c, -B, -u, -E, -s, -S, -X ...) are harmless
            rest = re.sub(r"^(?:-[IBuEsSqbdvW]+\s+|-X\s+\S+\s+)+", "", rest)
            if not rest.startswith("-c"):
                return False
            if PY_C_WRITE.search(rest):
                return False
        if base in ("sed",) and re.search(r"(^|\s)-i", piece):
            return False
        if base in ("find",) and re.search(r"-(delete|exec|execdir|ok)\b", piece):
            return False
        if base in ("tee",):
            return False
    return True


def guard_decision(payload, root=None):
    """-> reason string to DENY with, or None to allow."""
    root = Path(root) if root else ROOT
    if state(root) != "on":
        return None
    tool = str(payload.get("tool_name") or "")
    inp = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or str(root)
    if tool in PATH_TOOLS:
        target = _resolve_target(inp.get("file_path") or inp.get("notebook_path"), cwd)
        if target is None:
            return None
        if not _is_under(target, root):
            return None                      # outside the person's folder: not ours to police
        if _is_under(target, root / "engine"):
            return None
        return DENY_PATH
    if tool in SHELL_TOOLS:
        if shell_allowed(str(inp.get("command") or ""), root):
            return None
        return DENY_SHELL
    return None


# --------------------------------------------------------------------- #
# coverage and freshness
# --------------------------------------------------------------------- #
def _index_counts():
    if not DB.exists():
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True)
        by = dict(con.execute("SELECT status, COUNT(*) FROM files GROUP BY status"))
        meta = dict(con.execute("SELECT k, v FROM meta"))
        rels = {r[0] for r in con.execute("SELECT rel FROM files")}
        con.close()
    except sqlite3.Error:
        return None
    return {"by_status": by, "built_at": meta.get("built_at", ""),
            "corpus_root": meta.get("corpus_root", ""), "rels": rels}


def _files_on_disk():
    """The candidate files the indexer would see today, by the indexer's own walk."""
    import index_build as IB
    items, _excluded = IB.walk(str(ROOT), exclude_top=("engine",))
    return {rel for _p, rel in items}


def recount():
    """Compare disk with index; cache the result for the per-prompt briefing."""
    ic = _index_counts()
    if ic is None:
        return None
    on_disk = _files_on_disk()
    new = sorted(on_disk - ic["rels"])
    gone = sorted(ic["rels"] - on_disk)
    by = ic["by_status"]
    total = sum(by.values())
    indexed = by.get("indexed", 0)
    unreadable = {k: v for k, v in by.items() if k != "indexed"}
    out = {"built_at": ic["built_at"], "indexed": indexed, "total": total,
           "unreadable": unreadable, "n_new": len(new), "new_sample": new[:10],
           "n_gone": len(gone), "gone_sample": gone[:10],
           "counted_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(out, indent=1), encoding="utf-8")
    except OSError:
        pass
    return out


def cached():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def briefing(st, c):
    if st == "not_set_up":
        return ("[research assistant: NOT SET UP] When the user says initialize, read "
                "engine/INITIALIZE.md and follow it, start to finish.")
    if st == "off":
        return ("[research assistant: OFF] Plain Claude Code: the document index is not "
                "consulted and file changes are allowed if the user approves them. /rag-on "
                "switches it back on; /ask <question> uses it for one question.")
    parts = ["[research assistant: ON]"]
    if c:
        unread = sum(c["unreadable"].values())
        parts.append("Index of %s: %d of %d files readable" % (
            (c["built_at"] or "?")[:10], c["indexed"], c["total"]))
        if unread:
            kinds = ", ".join("%d %s" % (v, k.replace("_", " ")) for k, v in
                              sorted(c["unreadable"].items(), key=lambda kv: -kv[1]))
            parts[-1] += ", %d not readable (%s)" % (unread, kinds)
        parts[-1] += "."
        if c["n_new"]:
            parts.append("%s added to the folder since then and NOT yet searchable; "
                         "tell the user to say /rag-refresh before relying on them." %
                         _plural(c["n_new"], "file"))
        if c["n_gone"]:
            parts.append("%s in the index no longer on disk." % _plural(c["n_gone"], "file"))
    parts.append("For any question about the documents, read %s and follow it exactly; "
                 "never use Grep, Glob or Read on the documents instead. Every reply while ON, "
                 "however short, starts with the line `(research assistant on)` so the state "
                 "is visible. Writes "
                 "outside engine\\ are refused while ON; /rag-off switches to plain Claude "
                 "Code." % ANSWER_MD)
    return " ".join(parts)


# --------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------- #
def cmd_status():
    st = state()
    c = recount() if st != "not_set_up" else None
    print("state: %s" % st.upper().replace("_", " "))
    print("folder: %s" % ROOT)
    if c:
        print("index built: %s" % (c["built_at"] or "?"))
        print("files: %d indexed of %d; not readable: %s" % (
            c["indexed"], c["total"], json.dumps(c["unreadable"])))
        print("added since the index was built: %d%s" % (
            c["n_new"], (" e.g. " + "; ".join(c["new_sample"][:5])) if c["n_new"] else ""))
        print("removed since the index was built: %d" % c["n_gone"])
    if st == "on":
        print("switch off: /rag-off    refresh after adding files: /rag-refresh")
    elif st == "off":
        print("switch on: /rag-on    one question through it anyway: /ask <question>")
    return 0


def cmd_on():
    if OFF.exists():
        OFF.unlink()
    print("research assistant: ON" if READY.exists() else
          "research assistant: not set up yet -- say initialize")
    return 0


def cmd_off():
    OFF.parent.mkdir(parents=True, exist_ok=True)
    OFF.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
    print("research assistant: OFF")
    return 0


def cmd_prompt():
    st = state()
    print(briefing(st, cached() if st == "on" else None))
    return 0


def cmd_session():
    st = state()
    c = None
    if st == "on":
        try:
            c = recount()
        except Exception:
            c = cached()
        _adopt_if_moved()
    print(briefing(st, c))
    return 0


def _adopt_if_moved():
    """The index records the folder it was built in. In the paste-in layout the index can
    only ever belong to the folder that holds it, so a mismatch means the folder was moved:
    repoint silently rather than refuse."""
    try:
        con = sqlite3.connect(str(DB))
        row = con.execute("SELECT v FROM meta WHERE k='corpus_root'").fetchone()
        if row and Path(row[0]).resolve() != ROOT.resolve():
            con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('corpus_root',?)",
                        (str(ROOT.resolve()),))
            con.commit()
        con.close()
    except sqlite3.Error:
        pass


def cmd_guard():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    reason = guard_decision(payload)
    if reason:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason}}))
    else:
        print(json.dumps({}))
    return 0


# --------------------------------------------------------------------- #
# selftest
# --------------------------------------------------------------------- #
def _selftest():
    import tempfile
    checks = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "docs"
        (root / "engine" / "artefacts").mkdir(parents=True)
        (root / "Papers").mkdir()
        (root / "engine" / "artefacts" / "READY").write_text("x", encoding="utf-8")
        eng_py = "engine/.venv/Scripts/python.exe"

        def g(tool, **inp):
            return guard_decision({"tool_name": tool, "tool_input": inp, "cwd": str(root)}, root)

        checks.append(("write_to_document_denied",
                       g("Write", file_path=str(root / "Papers" / "x.md")) is not None))
        checks.append(("write_relative_denied", g("Write", file_path="Papers/x.md") is not None))
        checks.append(("write_under_engine_allowed",
                       g("Write", file_path=str(root / "engine" / "99_scratch" / "a.md")) is None))
        checks.append(("edit_root_claude_md_denied", g("Edit", file_path="CLAUDE.md") is not None))
        checks.append(("write_outside_folder_allowed",
                       g("Write", file_path=str(Path(td) / "elsewhere.md")) is None))
        checks.append(("engine_python_allowed",
                       g("Bash", command='"%s" engine/bin/c_shelf.py find "x"' % eng_py) is None))
        checks.append(("engine_python_backslash_allowed",
                       g("Bash", command='"engine\\.venv\\Scripts\\python.exe" "engine\\bin\\c_shelf.py" open "a.pdf" 3') is None))
        checks.append(("hooks_cmd_allowed", g("Bash", command='"engine/bin/hooks.cmd" off') is None))
        checks.append(("ls_allowed", g("Bash", command='ls -la "Papers/Tax effort"') is None))
        checks.append(("pdftotext_allowed",
                       g("Bash", command='pdftotext -f 3 -l 3 "Papers/a.pdf" - | head -40') is None))
        checks.append(("rm_denied", g("Bash", command="rm -rf Papers") is not None))
        checks.append(("del_denied", g("Bash", command='del "Papers\\x.pdf"') is not None))
        checks.append(("mv_denied", g("Bash", command="mv a.pdf b.pdf") is not None))
        checks.append(("redirect_denied", g("Bash", command="echo hi > notes.txt") is not None))
        checks.append(("append_denied", g("Bash", command="cat a >> b") is not None))
        checks.append(("chained_write_denied",
                       g("Bash", command='ls && "%s" x.py && touch y' % eng_py) is not None))
        checks.append(("system_python_denied", g("Bash", command="python script.py") is not None))
        checks.append(("python_c_print_allowed",
                       g("Bash", command='python -c "import os; print(os.cpu_count())"') is None))
        checks.append(("python_I_c_print_allowed",
                       g("Bash", command='python -I -c "import sqlite3; c=sqlite3.connect(\'engine/artefacts/pages.db\'); print(c.execute(\'select count(*) from pages\').fetchone())"') is None))
        checks.append(("python_I_script_denied", g("Bash", command="python -I script.py") is not None))
        checks.append(("python_c_write_denied",
                       g("Bash", command='python -c "open(\'x\',\'w\').write(1)"') is not None))
        checks.append(("sed_inplace_denied", g("Bash", command="sed -i s/a/b/ f.txt") is not None))
        # Test 3 (Q13): the engine's own draft under 99_scratch may be edited from the shell
        checks.append(("sed_inplace_in_scratch_allowed",
                       g("Bash", command='cd "%s" && sed -i "s/a/b/" engine/99_scratch/pjadp_answer.md && cat engine/99_scratch/pjadp_answer.md' % root) is None))
        checks.append(("rm_in_scratch_allowed", g("Bash", command="rm engine/99_scratch/old_answer.md") is None))
        checks.append(("rm_engine_itself_denied", g("Bash", command="rm -rf engine/99_scratch") is not None))
        checks.append(("rm_engine_bin_denied", g("Bash", command="rm engine/bin/c_shelf.py") is not None))
        checks.append(("mv_into_documents_denied", g("Bash", command="mv engine/99_scratch/a.md Papers/a.md") is not None))
        checks.append(("powershell_remove_denied",
                       g("PowerShell", command="Remove-Item Papers -Recurse") is not None))
        checks.append(("powershell_gci_allowed",
                       g("PowerShell", command="Get-ChildItem Papers") is None))
        # Test 2 (2026-10-10): four denials of the engine's own read-only commands
        checks.append(("stderr_merge_allowed",
                       g("Bash", command='cd "%s" && "%s" engine/bin/c_shelf.py exact "71.2" 2>&1 | head -60'
                         % (root, eng_py)) is None))
        checks.append(("for_loop_readonly_allowed",
                       g("Bash", command='for f in "a.csv" "b.csv"; do echo "=== $f ==="; "%s" engine/bin/c_shelf.py inside "$f" "x"; done'
                         % eng_py) is None))
        checks.append(("done_with_redirect_allowed",
                       g("Bash", command='for t in a b; do echo "=== $t ==="; done 2>/dev/null; ls') is None))
        checks.append(("for_loop_with_write_denied",
                       g("Bash", command='for f in a b; do rm "$f"; done') is not None))
        checks.append(("dev_null_allowed", g("Bash", command="ls Papers 2>/dev/null; echo x > /dev/null") is None))
        checks.append(("heredoc_to_engine_scratch_allowed",
                       g("Bash", command='cd "%s" && mkdir -p engine/99_scratch && cat > engine/99_scratch/imr_answer.md <<\'EOF\'\nThe rate was 69.6.\nrm -rf everything (this is prose)\nEOF'
                         % root) is None))
        checks.append(("heredoc_to_documents_denied",
                       g("Bash", command='cat > Papers/x.md <<\'EOF\'\nhi\nEOF') is not None))
        checks.append(("mkdir_outside_engine_denied", g("Bash", command="mkdir -p Papers/new") is not None))
        checks.append(("subshell_write_denied", g("Bash", command='echo $(rm -rf Papers)') is not None))
        checks.append(("subshell_read_allowed", g("Bash", command='echo $(ls Papers | wc -l)') is None))
        checks.append(("time_prefix_allowed", g("Bash", command='time "%s" engine/bin/c_shelf.py find "x"' % eng_py) is None))
        checks.append(("powershell_selftest_echo_allowed",
                       g("PowerShell", command='%s engine/bin/c_selftest.py --db x; "selftest exit=$LASTEXITCODE"'
                         % eng_py) is None))
        checks.append(("bare_string_is_not_a_loophole",
                       g("PowerShell", command='"x"; Remove-Item Papers') is not None))
        checks.append(("other_tool_allowed", g("Read", file_path="Papers/x.pdf") is None))
        # OFF: everything allowed
        (root / "engine" / "OFF").write_text("x", encoding="utf-8")
        checks.append(("off_allows_write", g("Write", file_path="Papers/x.md") is None))
        checks.append(("off_allows_rm", g("Bash", command="rm -rf Papers") is None))
        (root / "engine" / "OFF").unlink()
        # not set up: everything allowed
        (root / "engine" / "artefacts" / "READY").unlink()
        checks.append(("not_set_up_allows_write", g("Write", file_path="Papers/x.md") is None))
        checks.append(("state_not_set_up", state(root) == "not_set_up"))
        (root / "engine" / "artefacts" / "READY").write_text("x", encoding="utf-8")
        checks.append(("state_on", state(root) == "on"))
        (root / "engine" / "OFF").write_text("x", encoding="utf-8")
        checks.append(("state_off", state(root) == "off"))
    b_on = briefing("on", {"built_at": "2026-10-09T21:50:07", "indexed": 1850, "total": 2000,
                           "unreadable": {"image_only_no_text": 124}, "n_new": 3,
                           "new_sample": [], "n_gone": 0})
    checks.append(("briefing_on_names_state", b_on.startswith("[research assistant: ON]")))
    checks.append(("briefing_on_counts_new_files", "3 files added" in b_on))
    checks.append(("briefing_off", briefing("off", None).startswith("[research assistant: OFF]")))
    checks.append(("briefing_not_set_up", "initialize" in briefing("not_set_up", None)))
    n = sum(1 for _, ok in checks if ok)
    for name, ok in checks:
        print("%-36s %s" % (name, "pass" if ok else "FAIL"))
    print("mode guard: %d/%d pass" % (n, len(checks)))
    return 0 if n == len(checks) else 1


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args[0] == "--selftest":
        return _selftest()
    return {"status": cmd_status, "on": cmd_on, "off": cmd_off, "prompt": cmd_prompt,
            "session": cmd_session, "guard": cmd_guard}.get(args[0], lambda: 2)()


if __name__ == "__main__":
    sys.exit(main())
