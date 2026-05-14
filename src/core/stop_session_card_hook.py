"""Stop hook v2: write claude_session + lesson cards to v5 (2026-05-08).

Triggered when Claude session ends (Stop hook). Reads:
  1. events.jsonl (recent activity)
  2. git log (recent commits)
  3. transcript.jsonl (~/.claude/projects/.../<uuid>.jsonl) for user messages

Outputs N cards to memory_full_v5:
  - 1 session_summary card (type=state+report)
  - 0-N lesson/directive/correction cards extracted from user messages
    via regex on lesson_keywords.json patterns

This is the rule-based path (no LLM cost). Future GPU-driven path can add
deeper extraction over the same transcript.

Replaces v1 hook (was writing to deprecated memory_full_v3).

Failure mode: fail-open, never block session exit.
"""
from __future__ import annotations


import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from src.core._path_resolver import workspace_root

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_MEMEXA_ROOT = Path(__file__).resolve().parents[2]
if str(_MEMEXA_ROOT) not in sys.path:
    sys.path.insert(0, str(_MEMEXA_ROOT))

WORKSPACE = workspace_root()
EVENTS_PATH = WORKSPACE / "memexa" / "memexa" / "data" / "events.jsonl"
# Claude Code stores per-project transcripts in ``~/.claude/projects/<slug>``
# where slug = "<workspace-path-with-slashes-replaced>-memexa". We derive the
# slug from the workspace path so it adapts per machine.
_WORKSPACE_SLUG = str(WORKSPACE).replace(":", "").replace("\\", "-").replace("/", "-")
TRANSCRIPT_DIR = Path.home() / ".claude" / "projects" / f"{_WORKSPACE_SLUG}-memexa"

_TARGET_BANK = "memory_full_v5"


# ────────────────────────────────────────────────────────────────────────
# Data readers (events / git / transcript)
# ────────────────────────────────────────────────────────────────────────

def _read_recent_events(since_ts: float, max_n: int = 200) -> List[Dict]:
    if not EVENTS_PATH.exists():
        return []
    out = []
    try:
        with EVENTS_PATH.open("rb") as f:
            f.seek(0, 2)
            sz = f.tell()
            f.seek(max(0, sz - 256_000))
            f.readline()
            for raw in f:
                try:
                    d = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    continue
                ts_str = d.get("ts") or d.get("timestamp") or ""
                try:
                    ts = dt.datetime.fromisoformat(
                        ts_str.replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                if ts >= since_ts:
                    out.append(d)
                    if len(out) >= max_n:
                        break
    except Exception:
        pass
    return out


def _git_recent_commits(since_ts: float) -> List[Dict]:
    try:
        since_dt = dt.datetime.fromtimestamp(since_ts)
        out = subprocess.run(
            ["git", "-C", str(WORKSPACE / "memexa"), "log",
             "--pretty=format:%H|%aI|%s", "--since", since_dt.isoformat()],
            capture_output=True, text=True, timeout=30, encoding="utf-8")
        commits = []
        for ln in (out.stdout or "").splitlines():
            parts = ln.split("|", 2)
            if len(parts) == 3:
                commits.append({"sha": parts[0][:12],
                                 "ts": parts[1], "msg": parts[2]})
        return commits[:10]
    except Exception:
        return []


def _read_transcript_user_messages(
    transcript_path: Optional[Path] = None,
    session_uuid: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Read user messages from current session transcript.

    Tries (in order):
      1. explicit transcript_path arg
      2. session_uuid → <uuid>.jsonl in TRANSCRIPT_DIR
      3. most-recently-modified .jsonl in TRANSCRIPT_DIR

    Returns list of {ts_iso, content, idx} dicts (user role only).
    """
    target: Optional[Path] = None
    if transcript_path and transcript_path.exists():
        target = transcript_path
    elif session_uuid:
        cand = TRANSCRIPT_DIR / f"{session_uuid}.jsonl"
        if cand.exists():
            target = cand
    else:
        if TRANSCRIPT_DIR.exists():
            jsonls = sorted(TRANSCRIPT_DIR.glob("*.jsonl"),
                             key=lambda p: p.stat().st_mtime, reverse=True)
            if jsonls:
                target = jsonls[0]

    if not target or not target.exists():
        return []

    user_msgs: List[Dict[str, Any]] = []
    try:
        with target.open("r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                # Claude Code transcript: each entry has 'type' or 'role' keys
                role = (rec.get("role") or
                        rec.get("message", {}).get("role") or
                        rec.get("type"))
                if role != "user":
                    continue
                # Content may be in different locations
                content = (rec.get("content") or
                           rec.get("message", {}).get("content") or "")
                if isinstance(content, list):
                    # OpenAI-style content blocks (may nest list-of-lists
                    # in older Claude transcript schemas).
                    text_parts: List[str] = []
                    def _walk(node: Any) -> None:
                        if isinstance(node, str):
                            if node.strip():
                                text_parts.append(node)
                        elif isinstance(node, dict):
                            t = node.get("text")
                            if isinstance(t, str) and t.strip():
                                text_parts.append(t)
                            else:
                                _walk(node.get("content"))
                        elif isinstance(node, list):
                            for x in node:
                                _walk(x)
                    _walk(content)
                    content = "\n".join(text_parts)
                if not isinstance(content, str) or len(content) < 5:
                    continue
                # Skip system-injected messages
                if content.startswith(("<system-reminder>",
                                        "[SYSTEM",
                                        "<task-notification>",
                                        "Caveat:")):
                    continue
                ts = (rec.get("timestamp") or rec.get("ts") or "")
                user_msgs.append({"idx": idx, "ts_iso": ts, "content": content})
    except Exception as e:
        print(f"<!-- transcript read fail: {type(e).__name__}: {e} -->",
              file=sys.stderr)
    return user_msgs


# ────────────────────────────────────────────────────────────────────────
# Session summary card builder (kept from v1, but writes to v5)
# ────────────────────────────────────────────────────────────────────────

def _build_session_summary_card(
    events: List[Dict], commits: List[Dict], session_start_ts: float
) -> Optional[Dict[str, Any]]:
    if not events and not commits:
        return None

    type_counts: Dict[str, int] = {}
    for e in events:
        t = e.get("type", "")
        type_counts[t] = type_counts.get(t, 0) + 1

    n_error = (type_counts.get("error", 0)
               + type_counts.get("test_failure", 0))
    n_commit = len(commits)

    when_start_iso = dt.datetime.fromtimestamp(
        session_start_ts, tz=dt.timezone.utc).astimezone() \
        .isoformat(timespec="seconds")
    when_end_iso = dt.datetime.now(tz=dt.timezone.utc).astimezone() \
        .isoformat(timespec="seconds")

    parts = [
        f"Claude (Alice) 在 {when_start_iso[:19]} 到 {when_end_iso[:19]} "
        f"的会话中产生了 {sum(type_counts.values())} 个 events."
    ]
    if commits:
        parts.append(
            f"完成 {n_commit} 个 git commit: " +
            "; ".join(f'"{c["msg"][:60]}"' for c in commits[:3]))
    if n_error > 0:
        parts.append(f"会话中 {n_error} 个 error/test_failure events.")
    top = sorted(type_counts.items(), key=lambda x: -x[1])[:5]
    if top:
        parts.append("主要事件类型: " +
                     ", ".join(f"{t}({n})" for t, n in top))
    narrative = " ".join(parts)
    if len(narrative) < 30:
        return None

    eq = []
    if commits:
        eq.append(commits[0]["msg"][:200])
    if not eq and events:
        eq.append(json.dumps(events[0].get("details", {}),
                              ensure_ascii=False)[:200])
    if not eq:
        eq = ["(empty session)"]

    if n_commit >= 3:
        salience, sr = 0.7, f"{n_commit} commits, {sum(type_counts.values())} events"
    elif n_commit >= 1:
        salience, sr = 0.55, f"{n_commit} commits"
    elif n_error >= 1:
        salience, sr = 0.4, f"{n_error} errors"
    else:
        salience, sr = 0.25, "low-activity session"

    from src.core.lesson_card_v1 import build_lesson_card

    entities = [{"canonical_name": "Alice", "role_in_card": "subject",
                 "surface_form": "Alice"}]
    for c in commits[:4]:
        entities.append({"canonical_name": c["sha"], "role_in_card": "object",
                          "surface_form": c["sha"]})

    # NOTE: session summary uses subtype="session_summary" (NOT "lesson")
    # so it does NOT carry opentype:lesson tag. Otherwise every L2 lesson
    # recall would be polluted by ~5 session-summaries/day.
    card = build_lesson_card(
        narrative=narrative[:1200],
        evidence_quotes=eq[:5],
        when_start=when_start_iso, when_end=when_end_iso,
        where_chat_room="claude-code:memexa",
        speaker_role="self",
        enforcement_tier="general",
        lesson_subtype="session_summary",  # ← not "lesson"
        entities=entities,
        source="claude_code",
        salience=salience,
        salience_reason=sr,
        attestation_tier="probe_v2",
        extraction_prompt_sha="stop_hook_v2",
        extra_metadata={
            "card_subtype": "session_summary",
            "n_events": str(sum(type_counts.values())),
            "n_commits": str(n_commit),
            "n_errors": str(n_error),
        },
        extra_tags=["opentype:session_summary", "src:stop_hook"],
    )
    # Strip opentype:lesson if it leaked in via builder defaults
    if card:
        card.setdefault("tags", [])
        if "opentype:lesson" in card["tags"]:
            card["tags"].remove("opentype:lesson")
    return card


# ────────────────────────────────────────────────────────────────────────
# User-message lesson scanner
# ────────────────────────────────────────────────────────────────────────

def _build_user_lesson_cards(
    user_messages: List[Dict[str, Any]],
    session_start_iso: str,
    session_end_iso: str,
) -> List[Dict[str, Any]]:
    """Scan user_messages, emit type=state+opentype:lesson cards for matches."""
    from src.core.lesson_card_v1 import (
        build_lesson_card, classify_enforcement_tier,
        is_lesson_candidate, extract_topics,
    )

    cards = []
    for msg in user_messages:
        content = msg["content"].strip()
        matched, subtype = is_lesson_candidate(content)
        if not matched:
            continue

        tier = classify_enforcement_tier(content)
        if tier == "general" and subtype == "directive":
            tier = "warn"  # CEO directive = at least warn-tier

        topics = extract_topics(content, max_topics=5)
        entities = [{"canonical_name": t, "role_in_card": "mentioned",
                     "surface_form": t} for t in topics[:5]]
        if not entities:
            entities = [{"canonical_name": "Alice", "role_in_card": "subject",
                         "surface_form": "Alice"}]

        # narrative: contextualize
        narrative = (
            f"CEO 在 {msg.get('ts_iso', session_end_iso)[:19]} 的提示中"
            f"({subtype}/{tier}) 表达: {content[:1000]}"
        )

        when_start = msg.get("ts_iso") or session_start_iso
        when_end = msg.get("ts_iso") or session_end_iso

        # Salience boost: lesson > correction > directive > diagnostic
        salience = {"lesson": 0.9, "correction": 0.85,
                    "directive": 0.75, "diagnostic": 0.5}.get(subtype, 0.7)

        card = build_lesson_card(
            narrative=narrative[:1200],
            evidence_quotes=[content[:200]],  # CEO's verbatim words
            when_start=when_start, when_end=when_end,
            where_chat_room="claude-code:memexa",
            speaker_role="self",  # CEO speaking
            enforcement_tier=tier,
            lesson_subtype=subtype,
            entities=entities,
            source="claude_code",
            salience=salience,
            salience_reason=f"user_msg/{subtype}/tier={tier}"[:60],
            attestation_tier="probe_v2",
            extraction_prompt_sha="stop_hook_v2_user_msg",
            extra_metadata={
                "card_subtype": "ceo_directive_or_lesson",
                "msg_idx": str(msg.get("idx", -1)),
            },
            extra_tags=["opentype:lesson", "speaker:ceo", "src:stop_hook"],
        )
        if card:
            cards.append(card)
    return cards


# ────────────────────────────────────────────────────────────────────────
# Driver
# ────────────────────────────────────────────────────────────────────────

def main() -> int:
    session_start = time.time() - 4 * 3600  # default 4h window

    # Try parse stdin for session_id (Claude Code may pass it)
    session_uuid = None
    try:
        if not sys.stdin.isatty():
            data = sys.stdin.read()
            if data:
                try:
                    j = json.loads(data)
                    session_uuid = j.get("session_id") or j.get("session_uuid")
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass

    events = _read_recent_events(session_start)
    commits = _git_recent_commits(session_start)
    user_msgs = _read_transcript_user_messages(session_uuid=session_uuid)

    cards: List[Dict[str, Any]] = []

    # Session summary
    summary = _build_session_summary_card(events, commits, session_start)
    if summary:
        cards.append(summary)

    # User-message lessons
    when_start_iso = dt.datetime.fromtimestamp(
        session_start, tz=dt.timezone.utc).astimezone().isoformat(timespec="seconds")
    when_end_iso = dt.datetime.now(tz=dt.timezone.utc).astimezone().isoformat(timespec="seconds")
    user_lessons = _build_user_lesson_cards(user_msgs, when_start_iso, when_end_iso)
    cards.extend(user_lessons)

    if not cards:
        print("<!-- stop_session_card_hook v2: nothing to write -->")
        return 0

    print(f"<!-- stop_session_card_hook v2: {len(cards)} cards "
          f"({len(user_lessons)} user-msg lessons + "
          f"{1 if summary else 0} session summary) -->")

    # POST to v5
    try:
        import httpx
        base_url = os.environ.get("MEMEXA_HINDSIGHT_URL",
                                    "http://127.0.0.1:8888")
        with httpx.Client(base_url=base_url, timeout=60.0) as client:
            r = client.post(
                f"/v1/default/banks/{_TARGET_BANK}/memories",
                json={"items": cards, "async": False})
        if r.status_code in (200, 201):
            print(f"<!-- POST OK to {_TARGET_BANK} -->")
        else:
            print(f"<!-- POST fail {r.status_code}: {r.text[:200]} -->",
                  file=sys.stderr)
    except Exception as e:
        print(f"<!-- POST exception: {type(e).__name__}: {e} -->",
              file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
