"""your-org LLM platform OpenAI-compatible client — rate-paced + retry-backed.

Initial benchmark (BENCHMARK_REPORT.md 2026-05-12):
  - true concurrent = 1 (c>=2 triggers ~3 min cool-down where ALL reqs fail)
  - >=10s gap between requests is safe; <3s gap triggers cold-start timeouts

2026-05-13 LIVE re-verify (data/ustc_llm_verify/live_2026_05_13/c{1,2,3,4}.json):
  - c=1..4 all 4/4 ok, wall ~0.7-0.9s — platform concurrency limit RELAXED
  - default min_gap_sec lowered 10s → 2s (more headroom but conservative vs c=4
    LIVE 0s gap result). Override via MEMEX_your-org_MIN_GAP_SEC env to revert.
  - server has response caching → idempotent retry is free
  - max input ~30-50k tokens, max output ~4k (default) works
  - 100M token/day quota = effectively unlimited for cron flow

Public API:
    client = UstcLLMClient()  # reads MEMEX_your-org_LLM_KEY from env
    out = client.chat(model, system, user, max_tokens, temperature)
    # → {"ok": bool, "content": str, "usage": {...}, "latency_s": float, ...}

NEVER pass api_key in constructor — read from env only. The key must NEVER
appear in git history, logs, or task spec.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("ustc_llm_client")


def _load_dotenv():
    """Auto-load data/secrets/ustc_llm.env into os.environ if KEY env unset.

    Lets cron / interactive shells / agents share one source of truth without
    setting env explicitly every time. Existing env vars take precedence.
    """
    if os.environ.get("MEMEX_your-org_LLM_KEY"):
        return
    # repo root: this file lives at memex/dispatch/ustc_llm_client.py
    repo = Path(__file__).resolve().parents[2]
    env_path = repo / "data" / "secrets" / "ustc_llm.env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            os.environ.setdefault(k, v)
    except Exception as e:
        logger.warning(f"failed to load {env_path}: {e}")


# Load at import time (idempotent if env already set)
_load_dotenv()


_BASE_URL = os.environ.get("MEMEX_your-org_LLM_BASE_URL",
                            "https://api.llm.ustc.edu.cn/v1")
_GATEKEEPER_MODEL = os.environ.get("MEMEX_your-org_GATEKEEPER_MODEL", "qwen3.6-chat")
_EXTRACTOR_MODEL = os.environ.get("MEMEX_your-org_EXTRACTOR_MODEL",
                                   "deepseek-v4-flash-ascend")
_MIN_GAP_SEC = float(os.environ.get("MEMEX_your-org_MIN_GAP_SEC", "2"))
_DEFAULT_TIMEOUT = float(os.environ.get("MEMEX_your-org_TIMEOUT_SEC", "120"))
_RETRY_BACKOFF = [30, 90, 300]  # seconds; 3-retry pattern from benchmark


class UstcLLMClient:
    """Single-instance pacer for the your-org API.

    Shares a process-wide call-time lock to enforce 10s gap between any
    requests, even when multiple worker threads call concurrently. The
    benchmark proved >=10s spacing keeps the API healthy.
    """

    _global_last_call = [0.0]
    _global_lock = threading.Lock()

    def __init__(self, base_url: str = _BASE_URL,
                 min_gap_sec: float = _MIN_GAP_SEC,
                 default_timeout: float = _DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url
        self.min_gap_sec = min_gap_sec
        self.default_timeout = default_timeout
        api_key = os.environ.get("MEMEX_your-org_LLM_KEY")
        if not api_key:
            raise RuntimeError(
                "MEMEX_your-org_LLM_KEY env var not set. "
                "Get key from https://llm.ustc.edu.cn"
            )
        self._api_key = api_key
        self._http = httpx.Client(timeout=default_timeout)

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _pace(self) -> None:
        with self._global_lock:
            gap = time.time() - self._global_last_call[0]
            if gap < self.min_gap_sec:
                time.sleep(self.min_gap_sec - gap)
            self._global_last_call[0] = time.time()

    def chat(self, model: str, system: str, user: str,
             max_tokens: int = 4096, temperature: float = 0.1,
             timeout: float | None = None,
             label: str = "") -> dict[str, Any]:
        """Single chat completion. Returns dict, never raises."""
        timeout = timeout or self.default_timeout

        last_err = None
        for attempt, backoff in enumerate([0] + _RETRY_BACKOFF):
            if backoff:
                logger.warning(
                    f"[ustc][{label}] retry {attempt}/{len(_RETRY_BACKOFF)} "
                    f"after {backoff}s backoff (prev_err={last_err})"
                )
                time.sleep(backoff)

            self._pace()
            t0 = time.time()
            try:
                r = self._http.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}",
                             "Content-Type": "application/json"},
                    json={"model": model,
                          "messages": [
                              {"role": "system", "content": system},
                              {"role": "user", "content": user},
                          ],
                          "max_tokens": max_tokens,
                          "temperature": temperature},
                    timeout=timeout,
                )
            except httpx.ReadTimeout:
                last_err = "read_timeout"
                continue
            except Exception as e:
                last_err = f"{type(e).__name__}:{str(e)[:120]}"
                continue

            latency = time.time() - t0
            if r.status_code != 200:
                last_err = f"http_{r.status_code}:{r.text[:200]}"
                # 4xx is not worth retrying
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    return {"ok": False, "status": r.status_code,
                            "error": last_err, "latency_s": round(latency, 2),
                            "label": label}
                continue

            try:
                body = r.json()
            except Exception as e:
                last_err = f"json_parse:{e}"
                continue

            choice = (body.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content", "")
            usage = body.get("usage", {})
            finish_reason = choice.get("finish_reason")
            return {
                "ok": True,
                "status": 200,
                "content": content,
                "usage": usage,
                "finish_reason": finish_reason,
                "latency_s": round(latency, 2),
                "attempts": attempt + 1,
                "label": label,
                "tps_total": round(usage.get("total_tokens", 0) / max(latency, 0.01), 1),
                "tps_out": round(usage.get("completion_tokens", 0) / max(latency, 0.01), 1),
            }

        return {"ok": False, "error": last_err or "unknown",
                "attempts": len(_RETRY_BACKOFF) + 1, "label": label}

    def gatekeeper(self, system: str, user: str, timeout: float = 30,
                   label: str = "gk") -> dict[str, Any]:
        return self.chat(_GATEKEEPER_MODEL, system, user, max_tokens=128,
                         temperature=0.0, timeout=timeout, label=label)

    def extractor(self, system: str, user: str, timeout: float = 240,
                  label: str = "ext", max_tokens: int = 16384) -> dict[str, Any]:
        return self.chat(_EXTRACTOR_MODEL, system, user, max_tokens=max_tokens,
                         temperature=0.1, timeout=timeout, label=label)


# Module-level singleton (lazy init)
_singleton: UstcLLMClient | None = None
_singleton_lock = threading.Lock()


def get_client() -> UstcLLMClient:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = UstcLLMClient()
    return _singleton


# ──────────────────── CLI smoke test ────────────────────
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    c = get_client()
    print(f"base_url={c.base_url}")
    print(f"gatekeeper={_GATEKEEPER_MODEL} extractor={_EXTRACTOR_MODEL}")
    print(f"min_gap_sec={c.min_gap_sec}")
    print("---")
    r = c.gatekeeper("you respond with one of HIGH/MEDIUM/LOW.",
                     "Message: 测试。\n\nVerdict:", timeout=30)
    print(f"gatekeeper: ok={r['ok']} lat={r.get('latency_s')}s "
          f"content={r.get('content', '')[:80]!r}")
    r = c.extractor("you are a concise assistant.",
                    "Reply OK in one word.", max_tokens=10, timeout=60)
    print(f"extractor: ok={r['ok']} lat={r.get('latency_s')}s "
          f"content={r.get('content', '')[:80]!r}")
