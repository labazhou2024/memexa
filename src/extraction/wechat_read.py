"""wechat_read: WeChatDBReader adapter (mac_win_integration U6 v2; replaces v1 pywinauto).

Thin adapter over `src.wechat_db.WeChatDBReader`. DB-direct read; no UIA tree;
no GBK chat-name mangle (per CEO 2026-04-30 rewrite directive — use mature skills).

Failure contract:
- Weixin not running OR key extraction failed → typed `WeChatDBNotInitialized`
- Empty chat for date → return [] (legitimate)

Smoke:
    python -m memex.src.extraction.wechat_read --smoke --date 2026-04-30 --chat Alice
"""
from __future__ import annotations

import io
import sys
from dataclasses import asdict, dataclass
from datetime import date as _date_t, datetime
from typing import Optional

from src.extraction.keystone_outbox import write_envelope
from src.wechat_db import WeChatDBReader, WxMessage


class WeChatDBNotInitialized(Exception):
    """WeChatDBReader.initialize() succeeded but enc_key is None (Weixin not running OR key extraction failed)."""


def _emit_trace(event: str, payload: dict) -> None:
    try:
        from src.core.trace_sink import emit  # type: ignore
        emit(event, payload)
    except Exception:
        pass


def _get_reader() -> WeChatDBReader:
    """Returns initialized WeChatDBReader. Raises WeChatDBNotInitialized if no per-DB
    keys extracted (any DB) per 2026-04-30 fix.
    """
    r = WeChatDBReader()
    r.initialize()
    if not r.enc_keys:
        raise WeChatDBNotInitialized(
            "Weixin enc_keys empty; Weixin.exe not running OR key extraction failed for all DBs."
        )
    return r


def list_top_chats(target_date: Optional[str] = None, n: int = 10) -> list[dict]:
    """List top-N most active wxids on date with sample content.

    Helps CEO map display name (e.g. "Alice") → wxid via `--list-chats`.
    """
    from collections import Counter
    if target_date is None:
        target_date = _date_t.today().isoformat()
    reader = _get_reader()
    all_msgs: list[WxMessage] = reader.read_by_date(target_date)
    counter = Counter(m.chat_name for m in all_msgs)
    out = []
    for chat_id, count in counter.most_common(n):
        samples = [m for m in all_msgs if m.chat_name == chat_id][:3]
        out.append({
            "chat_id": chat_id,
            "count": count,
            "samples": [
                {"sender": m.sender, "content": m.content[:60]}
                for m in samples
            ],
        })
    return out


def read_recent_messages(
    chat_name: str = "Alice",
    target_date: Optional[str] = None,
) -> list[dict]:
    """Read messages from `chat_name` on `target_date` (YYYY-MM-DD; default today).

    chat_name semantics:
    - if matches wxid pattern (`wxid_*` or `*@chatroom`): exact match against m.chat_name
    - else: treat as display name; substring search in chat_name + sender (best-effort)

    Returns list of dict (typed dataclass shape). Never raises on empty;
    raises `WeChatDBNotInitialized` only on infrastructure problem.
    """
    if target_date is None:
        target_date = _date_t.today().isoformat()
    reader = _get_reader()
    all_msgs: list[WxMessage] = reader.read_by_date(target_date)
    # WeChat 4.x DB stores wxid not display name. Two-tier matching:
    # 1) exact wxid (wxid_* or *@chatroom)
    # 2) display-name fallback: substring in chat_name OR sender (best-effort;
    #    caller should use list_top_chats() to find the right wxid)
    is_wxid_form = chat_name.startswith("wxid_") or "@chatroom" in chat_name
    if is_wxid_form:
        matched = [m for m in all_msgs if m.chat_name == chat_name]
    else:
        matched = [m for m in all_msgs
                   if chat_name in m.chat_name or chat_name in m.sender]
    payload = [
        {
            "timestamp": m.timestamp.isoformat() if isinstance(m.timestamp, datetime) else str(m.timestamp),
            "sender": m.sender,
            "content": m.content,
            "chat_name": m.chat_name,
            "msg_type": m.msg_type,
        }
        for m in matched
    ]
    _emit_trace("wechat_db_read", {
        "count": len(payload),
        "date": target_date,
        "chat_name_match": chat_name,
        "db_initialized": True,
    })
    return payload


def poll_and_write(
    chat_name: str = "Alice",
    target_date: Optional[str] = None,
):
    """Run read + write envelope. Always writes (even count=0)."""
    try:
        msgs = read_recent_messages(chat_name=chat_name, target_date=target_date)
        return write_envelope("wechat", msgs, scrubbed_count=0)
    except WeChatDBNotInitialized as e:
        _emit_trace("wechat_db_read", {"count": 0, "db_initialized": False, "error": str(e)[:120]})
        return write_envelope("wechat", [], scrubbed_count=0)


def watch_realtime(
    chat_name: str = "Alice",
    duration_sec: float = 0.0,
    on_message=None,
) -> int:
    """Real-time message capture daemon (CEO directive 2026-04-30).

    Args:
        chat_name: filter to this chat — accepts display_name (e.g. "Alice") OR
                   wxid_xxxx. TU-6 alias resolver auto-saves first lookup.
        duration_sec: max runtime (0 = block until SIGINT)
        on_message: callback fn(WxMessage); default = keystone_outbox.append_message

    Returns: count of messages captured during session.
    """
    import signal
    import time
    from src.wechat_db import WeChatDBWatcher
    from src.extraction.keystone_outbox import append_message

    reader = _get_reader()  # raises WeChatDBNotInitialized if no keys

    # TU-6: resolve display_name → wxid via aliases.json + contact lookup.
    # AliasAmbiguous (multiple matches) is surfaced; other errors fall back
    # gracefully to original chat_name (covers mocked contacts in tests).
    try:
        from memex.wechat_aliases import resolve_chat as _resolve_chat, AliasAmbiguous
        contacts = None
        try:
            contacts = reader._load_contacts()
        except Exception:
            pass
        try:
            resolved = _resolve_chat(chat_name, contacts=contacts)
            if resolved != chat_name:
                print(f"[watch] alias '{chat_name}' → wxid '{resolved}'")
            chat_name = resolved
        except AliasAmbiguous:
            raise  # ambiguous = user must disambiguate; surface
        except Exception as e:
            # Mocked contacts / unexpected resolver state → keep original name
            print(f"[watch] alias resolve fallback (using {chat_name!r} as-is): {e}")
    except ImportError:
        pass  # wechat_aliases optional

    captured = [0]

    def default_callback(msg):
        # TU-2: Convert WxMessage → dict for outbox, including display fields
        try:
            payload = {
                "timestamp": msg.timestamp.isoformat(),
                "sender": msg.sender,
                "sender_display_name": msg.sender_display_name,
                "content": msg.content,
                "chat_name": msg.chat_name,
                "chat_display_name": msg.chat_display_name,
                "is_group_chat": msg.is_group_chat,
                "msg_type": msg.msg_type,
            }
            append_message("wechat", payload)
            captured[0] += 1
        except Exception as e:
            print(f"[watch] append failed: {e}")

    cb = on_message or default_callback
    watcher = WeChatDBWatcher(reader, chat_name=chat_name)

    stop_flag = [False]

    def handle_sigint(sig, frame):
        stop_flag[0] = True

    try:
        signal.signal(signal.SIGINT, handle_sigint)
    except (ValueError, AttributeError):
        pass  # signal may fail in non-main thread / Windows quirks

    watcher.start(cb)
    print(f"WATCH started: chat={chat_name!r} duration={duration_sec or '∞'}s")
    start = time.time()
    try:
        while not stop_flag[0]:
            if duration_sec > 0 and (time.time() - start) >= duration_sec:
                break
            time.sleep(0.5)
    finally:
        watcher.stop()
    elapsed = time.time() - start
    print(f"WATCH stopped: captured {captured[0]} message(s) in {elapsed:.1f}s")
    return captured[0]


def main(argv: list[str]) -> int:
    # UTF-8 stdout (avoid GBK mangle on Windows console)
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass
    chat = "Alice"
    target = None
    smoke = False
    watch = False
    duration = 0.0
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--smoke":
            smoke = True
        elif a == "--watch":
            watch = True
        elif a == "--duration" and i + 1 < len(argv):
            try:
                duration = float(argv[i + 1])
            except ValueError:
                duration = 0.0
            i += 1
        elif a == "--chat" and i + 1 < len(argv):
            chat = argv[i + 1]
            i += 1
        elif a == "--date" and i + 1 < len(argv):
            target = argv[i + 1]
            i += 1
        i += 1
    list_chats = "--list-chats" in argv
    if list_chats:
        try:
            chats = list_top_chats(target_date=target, n=15)
            print(f"=== TOP {len(chats)} chats on {target or 'today'} ===")
            for c in chats:
                print(f"  {c['count']:>4}x  {c['chat_id']}")
                for s in c["samples"][:2]:
                    print(f"         └─ {s['sender']}: {s['content']}")
            print("\n用法: 找到对应的 wxid_* 后用 --chat <wxid> 取消歧义")
            return 0
        except WeChatDBNotInitialized as e:
            print(f"LIST_CHATS typed-graceful: {e}")
            return 0
    if watch:
        try:
            n = watch_realtime(chat_name=chat, duration_sec=duration)
            print(f"WATCH OK: {n} message(s) captured")
            return 0
        except WeChatDBNotInitialized as e:
            print(f"WATCH typed-graceful: {e}")
            return 0
    if smoke:
        try:
            msgs = read_recent_messages(chat_name=chat, target_date=target)
            print(f"WECHAT smoke OK: chat={chat!r} date={target} count={len(msgs)}")
            for m in msgs[:3]:
                print(f"  {m['timestamp'][:19]} {m['sender']}: {m['content'][:60]}")
            return 0
        except WeChatDBNotInitialized as e:
            print(f"WECHAT typed-graceful: {e}")
            return 0  # graceful (typed) is exit 0 per AC-1 contract
    p = poll_and_write(chat_name=chat, target_date=target)
    print(f"WROTE {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
