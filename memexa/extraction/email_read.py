"""email_read: QQEmail + your-orgEmail adapter (mac_win_integration U6 v2; replaces outlook_read).

Thin adapter over `memexa.qq_email.QQEmailClient` + `memexa.ustc_email.RemoteEmailClient`.
IMAP-based; reads user's actual email accounts (NOT Outlook).

Failure contract:
- config.yaml missing auth_code → typed `EmailConfigMissing` (NOT bare ValueError)
- Network fetch fail → typed `EmailFetchFailed`
- 1 client OK + 1 fail → return partial; emit per-source trace

Smoke:
    python -m memexa.memexa.extraction.email_read --smoke -n 2
"""
from __future__ import annotations

import io
import sys
from dataclasses import asdict
from typing import Optional

from memexa.extraction.keystone_outbox import scrub_pii, write_envelope


class EmailConfigMissing(Exception):
    """config.yaml section missing or auth_code absent."""


class EmailFetchFailed(Exception):
    """IMAP connection / fetch failed."""


def _emit_trace(event: str, payload: dict) -> None:
    try:
        from memexa.core.trace_sink import emit  # type: ignore
        emit(event, payload)
    except Exception:
        pass


def _safe_qq_client():
    """Return QQEmailClient or raise EmailConfigMissing (typed wrap).

    Per logic-iter1-2: catches yaml.YAMLError + general Exception via broader except
    to typed-wrap any config-load-time error.
    """
    try:
        from memexa.qq_email import QQEmailClient
        return QQEmailClient()
    except Exception as e:  # ValueError / KeyError / FileNotFoundError / OSError / yaml.YAMLError / ImportError
        raise EmailConfigMissing(f"qq_email config error ({type(e).__name__}): {e}") from e


def _safe_ustc_client():
    """Return RemoteEmailClient or raise EmailConfigMissing."""
    try:
        from memexa.ustc_email import RemoteEmailClient
        return RemoteEmailClient()
    except Exception as e:
        raise EmailConfigMissing(f"ustc_email config error ({type(e).__name__}): {e}") from e


def _serialize(msg) -> dict:
    """EmailMessage dataclass → JSON-safe dict. Body is scrubbed; sender/subject NOT scrubbed (typed metadata)."""
    raw_body = getattr(msg, "body", "") or ""
    clean_body, _ = scrub_pii(raw_body[:500])
    return {
        "sender": str(getattr(msg, "sender", "") or ""),
        "subject": str(getattr(msg, "subject", "") or ""),
        "date": str(getattr(msg, "date", "") or ""),
        "uid": str(getattr(msg, "uid", "") or ""),
        "body_preview": clean_body,
    }


def read_recent_qq(n: int = 10) -> list[dict]:
    """Fetch n recent QQ emails. Raises EmailConfigMissing/EmailFetchFailed."""
    client = _safe_qq_client()
    try:
        msgs = client.fetch_recent(n=n)
        return [_serialize(m) for m in msgs]
    except Exception as e:
        if isinstance(e, EmailConfigMissing):
            raise
        raise EmailFetchFailed(f"QQ fetch failed: {type(e).__name__}: {e}") from e


def read_recent_ustc(n: int = 10) -> list[dict]:
    """Fetch n recent your-org emails. Raises EmailConfigMissing/EmailFetchFailed."""
    client = _safe_ustc_client()
    try:
        msgs = client.fetch_recent(n=n)
        return [_serialize(m) for m in msgs]
    except Exception as e:
        if isinstance(e, EmailConfigMissing):
            raise
        raise EmailFetchFailed(f"your-org fetch failed: {type(e).__name__}: {e}") from e


def read_all_recent(n: int = 10) -> dict:
    """Best-effort read both. Returns {qq: [...], ustc: [...], errors: {...}}.

    Per-source try/except so 1 failure doesn't kill the other.
    """
    out = {"qq": [], "ustc": [], "errors": {}}
    try:
        out["qq"] = read_recent_qq(n=n)
    except (EmailConfigMissing, EmailFetchFailed) as e:
        out["errors"]["qq"] = f"{type(e).__name__}: {e}"
    try:
        out["ustc"] = read_recent_ustc(n=n)
    except (EmailConfigMissing, EmailFetchFailed) as e:
        out["errors"]["ustc"] = f"{type(e).__name__}: {e}"
    _emit_trace("email_fetched", {
        "count_qq": len(out["qq"]),
        "count_ustc": len(out["ustc"]),
        "errors": list(out["errors"].keys()),
    })
    return out


def poll_and_write(n: int = 10):
    out = read_all_recent(n=n)
    payload = (
        [{**m, "_source": "qq"} for m in out["qq"]]
        + [{**m, "_source": "ustc"} for m in out["ustc"]]
    )
    return write_envelope("email", payload, scrubbed_count=0)


def main(argv: list[str]) -> int:
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass
    n = 10
    smoke = False
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--smoke":
            smoke = True
        elif a == "-n" and i + 1 < len(argv):
            n = int(argv[i + 1])
            i += 1
        i += 1
    if smoke:
        out = read_all_recent(n=n)
        print(f"EMAIL smoke: qq={len(out['qq'])} ustc={len(out['ustc'])} errors={out['errors']}")
        return 0
    p = poll_and_write(n=n)
    print(f"WROTE {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
