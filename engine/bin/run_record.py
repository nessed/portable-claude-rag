#!/usr/bin/env python
"""run_record.py - the documentation standard, enforced by a mechanism.

Plan E (plans_fable/E_TRUST_AND_REPRO/EXECUTE_2026-09-16_NIGHT.md) section D:

    if the repo, the portable package and a corpus are handed to another
    competent person, they can reproduce the build and see exactly where every
    headline number came from, without this chat or Ali's memory.

Every important run writes a `run_record.json` beside its outputs. This script
is the only writer. Rule 16: no battery, build, gate or re-score starts without
`start`; none is reported without `finish`.

  start  --id <id> --out <dir> [--cmd "<command being recorded>"]
         [--reads <path>...] [--writes <path>...] [--spec <spec file>]
         [--rung <name>] [--models <id>...] [--no-probe]
  note   <id> --out <dir> "<text>"
  finish <id> --out <dir> [--supersedes <path>] [--conclusion "<text>"]
         [--adopted yes|no|n/a]
  show   <id> --out <dir>

`finish` refuses to close a record whose declared output already existed when
`start` ran, unless `--supersedes <path>` is given -- and then it writes the
`<old>.SUPERSEDED.md` sibling itself (rule 15).

Everything here is read-only with respect to the corpora and to `_private/**`;
this script never opens an answer key.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labpaths as L  # noqa: E402

RECORD_NAME = "run_record.json"

# The env vars that relocate the lab (labpaths.py header). Recorded whether set
# or not, because "unset" is the fact a clean-room gate needs to assert.
ENV_OVERRIDES = [
    "CORPUS_LAB_ROOT", "RETRIEVAL_LAB_ROOT", "LAB_PRIVATE", "HARNESS_ROOT",
    "RASHIP_ROOT", "RUNS_ROOT", "SCORES_ROOT", "HARNESS_KEYS", "CANARY_MANIFEST",
    "CLAUDE_CMD", "OMP_NUM_THREADS", "PYTHONHASHSEED",
]

PY_PKGS = ["fastembed", "onnxruntime", "numpy", "pdfplumber"]

EMBED_MODEL = "BAAI/bge-small-en-v1.5"


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _sha256(path):
    """sha256 of a file; None if it does not exist, an error string if unreadable."""
    p = Path(path)
    if not p.exists():
        return None
    if p.is_dir():
        return "<dir>"
    h = hashlib.sha256()
    try:
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except Exception as e:
        return "<unreadable: %s>" % e
    return h.hexdigest()


def _run(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        return "<failed: %s>" % e


def _git(*args):
    return _run(["git", "-C", str(L.RETRIEVAL_LAB)] + list(args))


def _pkg_versions():
    out = {}
    for name in PY_PKGS:
        code = ("import importlib.metadata as m;print(m.version(%r))" % name)
        v = _run([sys.executable, "-c", code])
        out[name] = v.splitlines()[-1] if v and "<failed" not in v else "<absent>"
    return out


def _embed_model_files():
    """sha256 of every cached .onnx under the fastembed cache, keyed by a short
    relative name. The cache location is an env/temp path, so the names are what
    another machine can compare, not the absolute paths."""
    out = {}
    cache = os.environ.get("FASTEMBED_CACHE_PATH") or str(Path(os.environ.get(
        "LOCALAPPDATA", str(Path.home()))) / "Temp" / "fastembed_cache")
    root = Path(cache)
    if not root.exists():
        return {"<cache>": "<absent: %s>" % cache}
    try:
        for p in sorted(root.rglob("*.onnx")):
            parts = p.relative_to(root).parts
            key = parts[0] + "/" + p.name if parts else p.name
            out[key] = _sha256(p)
    except Exception as e:
        out["<error>"] = str(e)
    return out


def _rung_facts(rung):
    """Name, file count and checksum label for the corpus a run used."""
    if not rung:
        return None
    p = Path(rung)
    if not p.is_absolute():
        cand = L.HARNESS / rung
        p = cand if cand.exists() else p
    d = {"name": p.name, "path": str(p), "exists": p.exists()}
    if p.exists():
        try:
            d["n_files"] = sum(1 for x in p.rglob("*") if x.is_file())
        except Exception as e:
            d["n_files"] = "<error: %s>" % e
    return d


def _spec_facts(spec):
    """The gate spec's own commit and that commit's author date.

    docs_check.py fails a record whose spec commit is dated after the run
    started -- the mechanism behind 'gates are written before numbers exist'.
    """
    if not spec:
        return None
    p = Path(spec)
    rel = str(p.resolve()).replace(str(L.RETRIEVAL_LAB) + os.sep, "").replace(os.sep, "/")
    commit = _git("log", "-1", "--format=%H", "--", rel)
    date = _git("log", "-1", "--format=%aI", "--", rel) if commit else ""
    return {"path": rel, "sha256": _sha256(p), "commit": commit or None,
            "commit_author_date": date or None}


def _claude_version():
    exe = L.CLAUDE
    if not Path(exe).exists():
        return "<absent: %s>" % exe
    return _run([exe, "--version"], timeout=120).splitlines()[-1:] and \
        _run([exe, "--version"], timeout=120).strip().splitlines()[-1]


def record_path(out_dir):
    return Path(out_dir) / RECORD_NAME


def load(out_dir, rid):
    p = record_path(out_dir)
    if not p.exists():
        raise SystemExit("no run record at %s (did you run `start`?)" % p)
    d = json.loads(p.read_text(encoding="utf-8"))
    if d.get("id") != rid:
        raise SystemExit("run record at %s is id=%r, not %r" % (p, d.get("id"), rid))
    return d


def save(out_dir, d):
    p = record_path(out_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)
    return p


# --- commands ----------------------------------------------------------------

def cmd_start(a):
    out = Path(a.out)
    p = record_path(out)
    if p.exists():
        prev = json.loads(p.read_text(encoding="utf-8"))
        if prev.get("finished_at") is None:
            print("NOTE: reopening an unfinished record id=%s" % prev.get("id"))
        else:
            raise SystemExit(
                "a finished run record already exists at %s (id=%s). Rule 15: nothing "
                "recorded is overwritten. Use a new --out dir." % (p, prev.get("id")))

    d = {
        "id": a.id,
        "schema": "run_record/1",
        "started_at": _now(),
        "finished_at": None,
        "command": a.cmd,
        "recorder_argv": sys.argv,
        "cwd": os.getcwd(),
        "git": {
            "head": _git("rev-parse", "HEAD"),
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool([x for x in _git("status", "--short").splitlines() if x.strip()]),
            "dirty_files": [x for x in _git("status", "--short").splitlines() if x.strip()],
        },
        "env_overrides": {k: os.environ.get(k) for k in ENV_OVERRIDES},
        "runtime": {
            "python": sys.version.split()[0],
            "python_exe": sys.executable,
            "packages": _pkg_versions(),
            "claude_exe": str(L.CLAUDE),
            "claude_version": _claude_version() if not a.no_probe else "<skipped>",
            "workers": a.workers,
            "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS"),
            "PYTHONHASHSEED": os.environ.get("PYTHONHASHSEED"),
        },
        "models": {
            "requested_ids": list(a.models or []),
            "embedding_model": EMBED_MODEL,
            "embedding_model_files_sha256": _embed_model_files(),
        },
        "rung": _rung_facts(a.rung),
        "spec": _spec_facts(a.spec),
        "inputs": {str(x): _sha256(x) for x in (a.reads or [])},
        # Recorded at start so `finish` can tell a fresh output from a clobber.
        "declared_outputs": list(a.writes or []),
        "outputs_existed_at_start": {str(x): Path(x).exists() for x in (a.writes or [])},
        "outputs": {},
        "notes": [],
        "conclusion": None,
        "adopted": None,
        "supersedes": None,
    }
    p = save(out, d)
    print("START %s -> %s" % (a.id, p))
    pre = [k for k, v in d["outputs_existed_at_start"].items() if v]
    if pre:
        print("  WARNING: %d declared output(s) already exist; `finish` will require "
              "--supersedes: %s" % (len(pre), ", ".join(pre)))
    return 0


def cmd_note(a):
    d = load(a.out, a.id)
    d["notes"].append("%s  %s" % (_now(), " ".join(a.text)))
    save(a.out, d)
    print("NOTE recorded on %s (%d total)" % (a.id, len(d["notes"])))
    return 0


def _write_superseded_sibling(old_path, by_record, why, where):
    old = Path(old_path)
    sib = old.with_name(old.name + ".SUPERSEDED.md")
    if sib.exists():
        return sib, False
    sib.write_text(
        "# SUPERSEDED\n\n"
        "`%s` is superseded by run record `%s` (%s).\n\n"
        "**Why:** %s\n\n"
        "**Where the replacement lives:** %s\n\n"
        "The superseded file itself is untouched (Plan E rule 15): it is still the "
        "record of what was measured at the time, and every number reported from it "
        "stands as a historical result.\n" % (
            old.name, by_record, _now(), why or "(not given)", where or "(not given)"),
        encoding="utf-8")
    return sib, True


def cmd_finish(a):
    d = load(a.out, a.id)
    if d.get("finished_at"):
        raise SystemExit("run record %s is already finished at %s" % (a.id, d["finished_at"]))

    clobbered = [k for k, v in d.get("outputs_existed_at_start", {}).items() if v]
    if clobbered and not a.supersedes:
        raise SystemExit(
            "REFUSED: these declared outputs already existed when `start` ran:\n  %s\n"
            "Rule 15: nothing recorded is overwritten. Either write to a new file named "
            "for its version and phase tag, or pass --supersedes <path> and a reason."
            % "\n  ".join(clobbered))

    if a.supersedes:
        sib, made = _write_superseded_sibling(
            a.supersedes, a.id, a.conclusion,
            ", ".join(d.get("declared_outputs", [])) or str(a.out))
        d["supersedes"] = {"path": str(a.supersedes),
                           "sha256": _sha256(a.supersedes),
                           "sibling": str(sib), "sibling_written_now": made}

    d["outputs"] = {str(x): _sha256(x) for x in d.get("declared_outputs", [])}
    missing = [k for k, v in d["outputs"].items() if v is None]
    if missing:
        d["notes"].append("%s  WARNING declared output(s) absent at finish: %s"
                          % (_now(), ", ".join(missing)))
    d["finished_at"] = _now()
    d["conclusion"] = a.conclusion
    d["adopted"] = a.adopted
    p = save(a.out, d)
    print("FINISH %s -> %s" % (a.id, p))
    print("  conclusion: %s" % (a.conclusion or "(none)"))
    print("  adopted   : %s" % (a.adopted or "(none)"))
    if missing:
        print("  WARNING absent outputs: %s" % ", ".join(missing))
    return 0


def cmd_show(a):
    print(json.dumps(load(a.out, a.id), indent=1, ensure_ascii=False))
    return 0


def main(argv):
    ap = argparse.ArgumentParser(prog="run_record.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="c", required=True)

    s = sub.add_parser("start")
    s.add_argument("--id", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--cmd", default=None, help="the command this record documents")
    s.add_argument("--reads", nargs="*", default=[])
    s.add_argument("--writes", nargs="*", default=[])
    s.add_argument("--spec", default=None)
    s.add_argument("--rung", default=None)
    s.add_argument("--models", nargs="*", default=[])
    s.add_argument("--workers", default=None)
    s.add_argument("--no-probe", action="store_true",
                   help="skip `claude.exe --version` (offline runs)")
    s.set_defaults(fn=cmd_start)

    n = sub.add_parser("note")
    n.add_argument("id")
    n.add_argument("--out", required=True)
    n.add_argument("text", nargs="+")
    n.set_defaults(fn=cmd_note)

    f = sub.add_parser("finish")
    f.add_argument("id")
    f.add_argument("--out", required=True)
    f.add_argument("--supersedes", default=None)
    f.add_argument("--conclusion", default=None)
    f.add_argument("--adopted", choices=["yes", "no", "n/a"], default=None)
    f.set_defaults(fn=cmd_finish)

    w = sub.add_parser("show")
    w.add_argument("id")
    w.add_argument("--out", required=True)
    w.set_defaults(fn=cmd_show)

    a = ap.parse_args(argv[1:])
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
