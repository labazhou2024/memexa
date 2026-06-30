"""End-to-end ingestion of the bundled synthetic demo dataset.

Usage::

    python -m examples.demo_dataset.ingest

When ``--no-llm`` is set (the default), each batch is processed by a
stub extractor that emits a deterministic V2 envelope based on the raw
content. This makes the smoke test runnable without any LLM endpoint,
at the cost of card quality (no semantic compression, no entity
resolution).

Set ``MEMEXA_REMOTE_LLM_BASE_URL`` and remove ``--no-llm`` to use a
real OpenAI-compatible endpoint.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

WECHAT_FILE = ROOT / "wechat_demo.json"
QQ_FILE = ROOT / "qq_demo.json"
EMAIL_FILE = ROOT / "email_demo.json"
BROWSER_FILE = ROOT / "browser_demo.json"
CLAUDE_FILE = ROOT / "claude_demo.jsonl"
AUDIO_FILE = ROOT / "audio_demo_transcript.json"


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _stub_envelope(source: str, when_iso: str, narrative: str, entities: list[str]) -> dict[str, Any]:
    """Emit a deterministic V2-envelope dict that mirrors what the real
    extractor would produce, without calling an LLM."""
    return {
        "source": source,
        "when_start": when_iso,
        "salience": 0.5,
        "narrative": narrative,
        "entities": [{"surface": e, "kind": "person" if e[0].isupper() else "thing"} for e in entities],
        "predicates": [],
        "evidence_quotes": [narrative],
        "types_csv": "announcement",
    }


def ingest_wechat(stub: bool) -> list[dict[str, Any]]:
    if not WECHAT_FILE.exists():
        return []
    msgs = json.loads(WECHAT_FILE.read_text(encoding="utf-8"))
    # Bundle every 4 messages into a "batch", produce 1 card per batch
    cards = []
    for i in range(0, len(msgs), 4):
        chunk = msgs[i : i + 4]
        if not chunk:
            continue
        entities = sorted({m["sender"] for m in chunk})
        narrative = " | ".join(f"{m['sender']}: {m['content']}" for m in chunk)
        when_iso = chunk[0]["send_time"]
        cards.append(_stub_envelope("wechat", when_iso, narrative, entities))
    return cards


def ingest_qq(stub: bool) -> list[dict[str, Any]]:
    if not QQ_FILE.exists():
        return []
    msgs = json.loads(QQ_FILE.read_text(encoding="utf-8"))
    cards = []
    for i in range(0, len(msgs), 3):
        chunk = msgs[i : i + 3]
        if not chunk:
            continue
        entities = sorted({m["sender"] for m in chunk})
        narrative = " | ".join(f"{m['sender']}: {m['content']}" for m in chunk)
        when_iso = chunk[0]["send_time"]
        cards.append(_stub_envelope("qq", when_iso, narrative, entities))
    return cards


def ingest_email(stub: bool) -> list[dict[str, Any]]:
    if not EMAIL_FILE.exists():
        return []
    msgs = json.loads(EMAIL_FILE.read_text(encoding="utf-8"))
    cards = []
    for m in msgs:
        entities = [m["from"].split("@")[0]] + [t.split("@")[0] for t in m["to"]]
        narrative = f"[{m['subject']}] {m['body'][:200]}"
        cards.append(_stub_envelope("email", m["sent_at"], narrative, entities))
    return cards


def ingest_browser(stub: bool) -> list[dict[str, Any]]:
    if not BROWSER_FILE.exists():
        return []
    msgs = json.loads(BROWSER_FILE.read_text(encoding="utf-8"))
    cards = []
    for m in msgs:
        narrative = f"{m['title']} ({m['url']})"
        cards.append(_stub_envelope("browser_session", m["visit_time"], narrative, []))
    return cards


def ingest_claude(stub: bool) -> list[dict[str, Any]]:
    if not CLAUDE_FILE.exists():
        return []
    msgs = [json.loads(ln) for ln in CLAUDE_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    cards = []
    for i in range(0, len(msgs), 2):
        chunk = msgs[i : i + 2]
        if not chunk:
            continue
        narrative = " | ".join(f"{m['role']}: {m['content']}" for m in chunk)
        cards.append(_stub_envelope("claude_code", chunk[0]["ts"], narrative, []))
    return cards


def ingest_audio(stub: bool) -> list[dict[str, Any]]:
    if not AUDIO_FILE.exists():
        return []
    data = json.loads(AUDIO_FILE.read_text(encoding="utf-8"))
    when_iso = data["started_at"]
    narrative = " | ".join(u["text"] for u in data["utterances"])
    entities = data["speakers"]
    return [_stub_envelope("audio", when_iso, narrative, entities)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", default=True,
                    help="Use the stub extractor (default). Pass --use-llm to call a real LLM.")
    ap.add_argument("--use-llm", dest="no_llm", action="store_false",
                    help="Call a real OpenAI-compatible LLM endpoint (slower).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Accepted for compatibility; the open demo is always offline.")
    args = ap.parse_args()

    print(f"[info] mode = {'stub' if args.no_llm else 'real LLM'}")
    print("[info] backend = offline demo (no POST, no API key)")
    print()

    all_cards = []
    for label, fn in [
        ("wechat", ingest_wechat),
        ("qq", ingest_qq),
        ("email", ingest_email),
        ("browser", ingest_browser),
        ("claude", ingest_claude),
        ("audio", ingest_audio),
    ]:
        cards = fn(args.no_llm)
        print(f"  {label:10s} → {len(cards):3d} cards")
        all_cards.extend(cards)
    print(f"\n[info] total = {len(all_cards)} cards across 6 sources")

    print("[info] offline mode; not POSTing anywhere")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
