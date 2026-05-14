"""semantic_quorum.py — BGE-M3 cosine-similarity matching for paired-eval facts.

Replaces the lexical (s,p,o) string-equality match used in
batch_chat_extract.py:270-272 with semantic embedding cosine match.

Why: "user studies physics" vs "user is studying physics" are textually
different but semantically identical — string equality drops one as a
disagreement, wastes a DeepSeek arbitration call. BGE-M3 embeddings collapse
them via cosine ≥ threshold.

API:
    quorum(facts_a, facts_b, threshold=0.85) -> {
        "paired":   [(a_fact, b_fact, sim), ...],   # both sides matched
        "only_a":   [a_fact, ...],                  # no match in b
        "only_b":   [b_fact, ...],                  # no match in a
        "n_embed":  int,
    }

The matcher is a greedy 1:1 assignment (each b_fact matches at most one
a_fact, picked by max similarity above threshold). Greedy is cheap and OK
for typical batch sizes (≤30 facts/side); replace with Hungarian only if
benchmarks show degenerate matching.

BGE-M3 is on Mac at 127.0.0.1:18082 (bound to localhost for security).
We invoke via SSH+curl. Round-trip ~2-5s per batch of 30.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
from typing import Any, Dict, List, Sequence, Tuple

SSH_ALIAS = os.environ.get("MEMEXA_MAC_SSH_ALIAS", "primary-host")
BGE_PORT = int(os.environ.get("MEMEXA_BGE_PORT", "18082"))
DEFAULT_THRESHOLD = float(os.environ.get("MEMEXA_QUORUM_THRESHOLD", "0.85"))


class BGEUnavailableError(RuntimeError):
    """BGE-M3 sidecar unreachable or returned malformed payload."""


def _fact_text(fact: Dict[str, Any]) -> str:
    """Concatenate (s, p, o) into a single sentence for embedding."""
    s = (fact.get("s") or "").strip()
    p = (fact.get("p") or "").strip()
    o = (fact.get("o") or "").strip()
    return f"{s} {p} {o}".strip()


def embed_batch(texts: Sequence[str], *, timeout: int = 60) -> List[List[float]]:
    """Embed a batch of texts via Mac BGE-M3 sidecar (over SSH).

    Raises BGEUnavailableError on transport / parse failure.
    """
    if not texts:
        return []
    payload = json.dumps({"inputs": list(texts)}, ensure_ascii=False)
    payload_b64 = payload.encode("utf-8").hex()
    # Pass payload as hex via SSH to avoid shell quoting issues with Chinese / quotes
    cmd = (
        f"python3 -c \"import sys,binascii,urllib.request,json;"
        f"body=binascii.unhexlify('{payload_b64}');"
        f"req=urllib.request.Request('http://127.0.0.1:{BGE_PORT}/embed',"
        f"data=body,headers={{'Content-Type':'application/json'}},method='POST');"
        f"r=urllib.request.urlopen(req,timeout={timeout});"
        f"sys.stdout.buffer.write(r.read())\""
    )
    full = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
            SSH_ALIAS, cmd]
    try:
        r = subprocess.run(full, capture_output=True, timeout=timeout + 15)
        if r.returncode != 0:
            raise BGEUnavailableError(
                f"ssh rc={r.returncode} stderr={r.stderr.decode('utf-8', 'replace')[:200]}"
            )
        data = json.loads(r.stdout.decode("utf-8"))
    except subprocess.TimeoutExpired as e:
        raise BGEUnavailableError(f"ssh timeout after {timeout + 15}s") from e
    except json.JSONDecodeError as e:
        raise BGEUnavailableError(
            f"non-JSON response: {r.stdout[:200]!r}"
        ) from e

    embs = data
    if isinstance(data, dict):
        embs = data.get("embeddings") or data.get("data") or data.get("vectors")
    if not isinstance(embs, list) or (embs and not isinstance(embs[0], list)):
        raise BGEUnavailableError(f"unexpected response shape: {str(data)[:200]}")
    if len(embs) != len(texts):
        raise BGEUnavailableError(
            f"embedding count mismatch: got {len(embs)} for {len(texts)} inputs"
        )
    return embs


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b:
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


def quorum(
    facts_a: List[Dict[str, Any]],
    facts_b: List[Dict[str, Any]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    timeout: int = 60,
) -> Dict[str, Any]:
    """Greedy 1:1 cosine match between two fact lists.

    Returns dict with paired / only_a / only_b / n_embed.
    """
    if not facts_a and not facts_b:
        return {"paired": [], "only_a": [], "only_b": [], "n_embed": 0}

    texts_a = [_fact_text(f) for f in facts_a]
    texts_b = [_fact_text(f) for f in facts_b]
    all_texts = texts_a + texts_b
    embs = embed_batch(all_texts, timeout=timeout)
    embs_a = embs[: len(texts_a)]
    embs_b = embs[len(texts_a):]

    # Greedy: for each a, find best unmatched b above threshold
    used_b = set()
    paired: List[Tuple[Dict[str, Any], Dict[str, Any], float]] = []
    only_a: List[Dict[str, Any]] = []
    for i, fa in enumerate(facts_a):
        best_j = -1
        best_sim = threshold  # require >= threshold to match
        for j, fb in enumerate(facts_b):
            if j in used_b:
                continue
            sim = cosine(embs_a[i], embs_b[j])
            if sim >= best_sim:
                best_sim = sim
                best_j = j
        if best_j >= 0:
            paired.append((fa, facts_b[best_j], float(best_sim)))
            used_b.add(best_j)
        else:
            only_a.append(fa)

    only_b = [fb for j, fb in enumerate(facts_b) if j not in used_b]

    return {
        "paired": paired,
        "only_a": only_a,
        "only_b": only_b,
        "n_embed": len(all_texts),
    }


if __name__ == "__main__":  # pragma: no cover - smoke
    import argparse, sys
    p = argparse.ArgumentParser()
    p.add_argument("--smoke", action="store_true", help="Run smoke test against live BGE-M3")
    args = p.parse_args()

    if args.smoke:
        a = [
            {"s": "user", "p": "studies", "o": "physics"},
            {"s": "memexa", "p": "is", "o": "an assistant"},
        ]
        b = [
            {"s": "user", "p": "is studying", "o": "physics"},
            {"s": "Claude", "p": "is", "o": "an AI"},
        ]
        try:
            res = quorum(a, b)
            print(f"paired={len(res['paired'])} only_a={len(res['only_a'])} only_b={len(res['only_b'])}")
            for fa, fb, sim in res["paired"]:
                print(f"  pair: '{_fact_text(fa)}' ~ '{_fact_text(fb)}' (sim={sim:.3f})")
            for fa in res["only_a"]:
                print(f"  only_a: '{_fact_text(fa)}'")
            for fb in res["only_b"]:
                print(f"  only_b: '{_fact_text(fb)}'")
        except BGEUnavailableError as e:
            print(f"BGE unavailable: {e}", file=sys.stderr)
            sys.exit(1)
