#!/usr/bin/env python
"""c_setup_selftest.py -- asserts for the install/teardown ownership contract.

Two cases that only appear on a folder this tool did not build in place:

  * an install pointed at an index that already belongs to a DIFFERENT folder
    ([1/5] skips when pages.db exists, so without the check a shelf gets built over
    another folder's pages and the install prints Ready)
  * a teardown of a folder that has been MOVED, whose state file -- keyed by the
    corpus's absolute path -- cannot be found under the new path at all

Both used to end in a refusal that named the tool's own files as foreign.

    python engine/bin/c_setup_selftest.py     # exit 0 pass, 1 fail
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import c_stack as CS          # noqa: E402
import setup_folder as SF     # noqa: E402


def _fake_corpus(td, title=None):
    """A folder holding exactly the two files the installer writes."""
    root = Path(td) / "research"
    (root / ".claude").mkdir(parents=True)
    (root / "CLAUDE.md").write_text((title or CS.CLAUDE_MD_TITLE) + "\nbody\n",
                                    encoding="utf-8")
    (root / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [
            {"command": "py c_stop_guard.py --v2"}]}]}}), encoding="utf-8")
    return root


def main():
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    # --- whose index is it ---------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "pages.db")
        check("no_index_has_no_owner", SF.index_owner(db) is None)
        con = sqlite3.connect(db)
        con.execute("create table meta(k text primary key, v text)")
        con.execute("insert into meta values('corpus_root', 'C:/somewhere/else')")
        con.commit()
        con.close()
        check("owner_is_read_back", SF.index_owner(db) == "C:/somewhere/else")
        check("repoint_succeeds", SF._repoint_index(db, td) is True)
        check("repoint_is_recorded", SF.index_owner(db) == str(Path(td).resolve()))

    # --- a moved install can clean up after itself ---------------------------
    with tempfile.TemporaryDirectory() as td:
        root = _fake_corpus(td)
        check("our_files_are_recognised", CS._looks_like_ours(root))
        check("moved_install_teardown_exits_0", CS.do_teardown(str(root)) == 0)
        check("claude_md_removed", not (root / "CLAUDE.md").exists())
        check("claude_dir_removed", not (root / ".claude").exists())

    # --- anything else is still refused -------------------------------------
    with tempfile.TemporaryDirectory() as td:
        root = _fake_corpus(td, title="# Someone else's project notes")
        check("foreign_claude_md_is_not_ours", not CS._looks_like_ours(root))
        check("foreign_folder_still_refused", CS.do_teardown(str(root)) == 3)
        check("foreign_files_left_alone", (root / "CLAUDE.md").exists())

    with tempfile.TemporaryDirectory() as td:
        root = _fake_corpus(td)
        (root / ".claude" / "settings.json").write_text(json.dumps(
            {"permissions": {"deny": []}}), encoding="utf-8")
        check("foreign_settings_is_not_ours", not CS._looks_like_ours(root))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "empty"
        root.mkdir()
        check("clean_folder_is_already_clean", CS.do_teardown(str(root)) == 0)

    n_pass = sum(1 for _, ok in checks if ok)
    for name, ok in checks:
        print("%-38s %s" % (name, "pass" if ok else "FAIL"))
    print("setup contract: %d/%d pass" % (n_pass, len(checks)))
    return 0 if n_pass == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
