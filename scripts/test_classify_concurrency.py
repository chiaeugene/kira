"""Regression test for classify()'s Pass 2 running chunks CONCURRENTLY.

Field bug (2026-07-25): a 19-day daily-takings batch (~190 lines, ~10
classification chunks at batch_size=20) took long enough posting through
sequential Claude calls that Render's proxy returned 502 before the server
finished - even though nothing on the server actually failed. Chunks are
independent (no shared state between them), so there's no reason to run
them one after another. This proves: (1) correctness is unaffected - every
row still gets coded, in any chunk-completion order; (2) wall-clock time
for N chunks is close to ONE chunk's delay, not N times it.

Run:  python scripts/test_classify_concurrency.py
"""
from __future__ import annotations

import os
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")

import kira.classify as classify_mod  # noqa: E402
from kira.context import ClientContext  # noqa: E402
from kira.rules import RuleStore  # noqa: E402

CHUNK_DELAY = 0.4  # seconds - simulates one Claude round-trip
N_ROWS = 100        # 5 chunks at batch_size=20


def fake_classify_batch_llm(client, model, max_tokens, context_block, chunk):
    time.sleep(CHUNK_DELAY)  # every chunk "call" takes the same time
    return {
        r["row_id"]: {
            "doc_type": "journal", "party_code": "", "account_code": "500-000",
            "contra_account": "", "tax_code": "", "confidence": "high",
            "reason": "test",
        }
        for r in chunk
    }


classify_mod._classify_batch_llm = fake_classify_batch_llm
sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=lambda: object())

df = pd.DataFrame({
    "supplier": [""] * N_ROWS,
    "description": [f"line {i}" for i in range(N_ROWS)],
    "date": ["2026-07-01"] * N_ROWS,
    "amount": [1.0] * N_ROWS,
    "tax": [0.0] * N_ROWS,
    "doc_type_hint": [""] * N_ROWS,
    "doc_no": [""] * N_ROWS,
})
df.index = range(N_ROWS)  # row_id == index, as ensure_row_ids would set up
df["row_id"] = df.index

ctx = ClientContext(name="TEST")
store = RuleStore.__new__(RuleStore)  # no rules on disk needed
store.rules = {}
store.lookup = lambda *a, **k: None  # force every row to Pass 2

started = time.time()
out = classify_mod.classify(df, ctx, store, batch_size=20)
elapsed = time.time() - started

n_chunks = -(-N_ROWS // 20)  # ceil
print(f"[classify] {N_ROWS} rows -> {n_chunks} chunks, "
     f"{elapsed:.2f}s elapsed (sequential would be ~{n_chunks * CHUNK_DELAY:.2f}s)")

assert (out["account_code"] == "500-000").all(), \
    "every row must still be coded correctly regardless of chunk order"
assert len(out) == N_ROWS
print(f"1. all {N_ROWS} rows correctly coded across {n_chunks} chunks  OK")

# Concurrent (up to 5 workers) should land near ONE chunk's delay, not
# n_chunks worth - generous margin for CI/slow-machine jitter.
assert elapsed < CHUNK_DELAY * 2.5, \
    f"took {elapsed:.2f}s - looks sequential, not concurrent (chunks={n_chunks})"
print(f"2. wall-clock ({elapsed:.2f}s) matches concurrent execution, "
     f"not {n_chunks}x sequential ({n_chunks * CHUNK_DELAY:.2f}s)  OK")

print("\nAll classify-concurrency checks passed.")
