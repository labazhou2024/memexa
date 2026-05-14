"""keystone_pull: unified entry point — pulls 3 keystone sources (mac_win_integration U6 v2).

Calls wechat_read + email_read + schedule_poll adapters per-source. Per-source
try/except so 1 failure doesn't kill the others. Each adapter writes its own
envelope; this module orchestrates + emits aggregate trace.

Smoke:
    python -m memexa.memexa.extraction.keystone_pull --smoke
"""
from __future__ import annotations

import io
import sys
from typing import Optional


def _emit_trace(event: str, payload: dict) -> None:
    try:
        from memexa.core.trace_sink import emit  # type: ignore
        emit(event, payload)
    except Exception:
        pass


def pull_all(
    chat_name: str = "Alice",
    target_date: Optional[str] = None,
    email_n: int = 10,
) -> dict:
    """Run all 3 adapters; return summary `{wechat, email, schedule, errors}`."""
    summary = {"wechat": None, "email": None, "schedule": None, "errors": {}}

    # WeChat
    try:
        from memexa.extraction.wechat_read import poll_and_write as wechat_pw
        summary["wechat"] = str(wechat_pw(chat_name=chat_name, target_date=target_date))
    except Exception as e:
        summary["errors"]["wechat"] = f"{type(e).__name__}: {str(e)[:120]}"

    # Email
    try:
        from memexa.extraction.email_read import poll_and_write as email_pw
        summary["email"] = str(email_pw(n=email_n))
    except Exception as e:
        summary["errors"]["email"] = f"{type(e).__name__}: {str(e)[:120]}"

    # Schedule
    try:
        from memexa.extraction.schedule_poll import poll_and_write as schedule_pw
        summary["schedule"] = str(schedule_pw())
    except Exception as e:
        summary["errors"]["schedule"] = f"{type(e).__name__}: {str(e)[:120]}"

    _emit_trace("keystone_pull_complete", {
        "envelopes_written": sum(1 for k, v in summary.items() if k != "errors" and v),
        "errors_count": len(summary["errors"]),
    })
    return summary


def main(argv: list[str]) -> int:
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass
    smoke = "--smoke" in argv
    chat = "Alice"
    if "--chat" in argv:
        chat = argv[argv.index("--chat") + 1]
    summary = pull_all(chat_name=chat)
    if smoke:
        print(f"KEYSTONE PULL smoke complete:")
        for k in ("wechat", "email", "schedule"):
            v = summary.get(k)
            err = summary["errors"].get(k)
            if v:
                # Just show filename of envelope path
                from pathlib import Path
                print(f"  {k}: WROTE {Path(v).name}")
            elif err:
                print(f"  {k}: SKIP ({err})")
            else:
                print(f"  {k}: NO_RESULT")
        if summary["errors"]:
            print(f"  errors: {list(summary['errors'].keys())}")
        return 0
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
