#!/usr/bin/env python
"""c_caption_embed.py -- Experiment E2, 2026-09-15.

Embeds every harvested table caption in the shelf with the same bge-small model
the document cards use, so a caption line can be a dense retrieval unit in its
own right rather than only a lexical one.

  OUT/captions.f16.npy      L2-normalised float16 vectors, one row per caption
  OUT/captions_ids.jsonl    {"i","rel","page_index"} mapping npy row -> caption

Reads shelf.db read-only and never writes to it. Deliberately does NOT import
c_shelf: this job runs in the background while c_shelf is being edited for
Phase 3, and it must not pick up a half-written module.

Checkpointed every 20 batches, resumable, mirroring c_shelf_build.py -- an
earlier embedding job in this repository was killed by a session teardown with
nothing recoverable.

  python -u corpus-lab/bin/c_caption_embed.py
"""
import hashlib
import json
import sqlite3
import pathlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labpaths as L  # noqa: E402

BATCH = 256
DIM = 384
CKPT_EVERY = 20  # batches


def main():
    t0 = time.time()
    # Phase 9.2.2: --out-dir so the v2 shelf (and a portable folder's shelf) can
    # have its own caption artefacts. Default preserves behaviour exactly.
    import argparse
    _ap = argparse.ArgumentParser()
    _ap.add_argument("--out-dir", dest="out_dir", default=str(L.STACKS / "s7_shelf"))
    _a = _ap.parse_args()
    out_dir = pathlib.Path(_a.out_dir)
    shelf_path = out_dir / "shelf.db"
    db = sqlite3.connect(f"file:{shelf_path}?mode=ro", uri=True)

    rows = db.execute(
        "SELECT rowid, rel, page_index, caption FROM captions ORDER BY rowid"
    ).fetchall()
    db.close()
    n = len(rows)
    print(f"captions: {n}", flush=True)
    if n == 0:
        raise SystemExit("NO_CAPTIONS")

    ids = [(r[1], r[2]) for r in rows]
    texts = [(r[3] or "") for r in rows]

    import numpy as np
    from fastembed import TextEmbedding

    model = TextEmbedding("BAAI/bge-small-en-v1.5")

    ids_hash = hashlib.sha256(
        "\n".join(f"{rel}\t{pi}" for rel, pi in ids).encode("utf-8")).hexdigest()
    ckpt_npy = out_dir / "captions.f16.partial.npy"
    ckpt_meta = out_dir / "captions.f16.partial.json"

    start_i = 0
    vecs = np.zeros((n, DIM), dtype=np.float32)
    if ckpt_npy.exists() and ckpt_meta.exists():
        try:
            cm = json.loads(ckpt_meta.read_text(encoding="utf-8"))
            if cm.get("n") == n and cm.get("ids_hash") == ids_hash:
                partial = np.load(ckpt_npy).astype(np.float32)
                start_i = cm["done_upto"]
                vecs[:start_i] = partial[:start_i]
                print(f"resuming from checkpoint at {start_i}/{n}", flush=True)
        except Exception as e:
            print(f"checkpoint unusable ({e}), starting from 0", flush=True)
            start_i = 0

    for i in range(start_i, n, BATCH):
        batch = texts[i:i + BATCH]
        for j, v in enumerate(model.embed(batch, batch_size=BATCH)):
            v = np.asarray(v, dtype=np.float32)
            norm = np.linalg.norm(v)
            if norm > 0:
                v = v / norm
            vecs[i + j] = v
        done = min(i + BATCH, n)
        if (i // BATCH) % CKPT_EVERY == 0 or done == n:
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            print(f"embed progress {done}/{n} ({rate:.1f}/s, {elapsed:.0f}s elapsed)",
                  flush=True)
            np.save(ckpt_npy, vecs.astype(np.float16))
            ckpt_meta.write_text(
                json.dumps({"n": n, "ids_hash": ids_hash, "done_upto": done}),
                encoding="utf-8")

    np.save(out_dir / "captions.f16.npy", vecs.astype(np.float16))
    with open(out_dir / "captions_ids.jsonl", "w", encoding="utf-8") as fh:
        for i, (rel, page_index) in enumerate(ids):
            fh.write(json.dumps({"i": i, "rel": rel, "page_index": page_index},
                                ensure_ascii=False) + "\n")
    for p in (ckpt_npy, ckpt_meta):
        if p.exists():
            p.unlink()

    wall = time.time() - t0
    rec = {"n_captions": n, "dim": DIM, "batch": BATCH,
           "wall_s": round(wall, 1),
           "rate_per_s": round(n / wall, 2) if wall > 0 else None}
    (L.STATE / "c_caption_embed.json").write_text(
        json.dumps(rec, indent=1), encoding="utf-8")
    print("DONE " + json.dumps(rec), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
