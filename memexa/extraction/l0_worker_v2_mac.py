"""Mac L0 v5 Worker v2 — mlx_lm.server endpoints (Mac Studio via Tailscale).

Spec: docs/l0_v5/MASTER_PLAN.md §4.1, §4.3 (Mac variant)

Architecture:
- Designed to run on Windows client, dispatching HTTP to Mac mlx_lm.server
- Pass-1 mode: Gemma-31B alone (gatekeeper not needed for assertion mining)
- Pass-2 mode: Qwen3-14B gatekeeper (:18080, always-on)
               → Gemma-31B extractor (:18081, on-demand, serial-locked)

Endpoints (mlx_lm.server, OpenAI-compatible):
  Stage A (Qwen3-14B):  http://127.0.0.1:18080/v1/chat/completions
  Stage B (Gemma-31B):  http://127.0.0.1:18081/v1/chat/completions

Serial lock: only ONE worker process may talk to :18081 at a time.
  Uses memexa.extraction.mlx_lm_serial_lock if available; falls back to no-op
  stub with WARNING so the file imports cleanly even when A1 agent has not
  yet written the lock module.

Concurrency:
  --concurrent defaults to 1 (Stage-B serial lock). CLI override allowed but
  warns that parallel requests will queue behind the lock anyway.

Idempotency:
  Skips batches that already have output card JSON.
  .done sentinel files mirror the your-org approach for resume support.

Usage:
  python l0_worker_v2_mac.py \\
    --pass {1,2} \\
    --batches-dir data/l0_v5/input_batches/<date>/shard0 \\
    --done-dir   data/l0_v5/done/shard0 \\
    --out-dir    data/l0_v5/work/cards_v2_45/shard0 \\
    --concurrent 1   # keep default; Stage B is serial-locked
    [--dry-run]      # print plan without HTTP calls
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
import traceback
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import httpx
except ImportError:
    print("FATAL: httpx not installed. pip install httpx", file=sys.stderr)
    sys.exit(2)

# 2026-05-10: ensure workspace root is on sys.path so `memexa.dispatch.*`
# imports resolve when invoked as a script from cron driver subprocess.
_WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

# ── guarded import of serial lock ────────────────────────────────────────────
# memexa.extraction.mlx_lm_serial_lock is written by parallel agent A1.
# If it doesn't exist yet, fall back to no-op stubs so this file imports clean.
try:
    from memexa.extraction.mlx_lm_serial_lock import acquire as _lock_acquire
    from memexa.extraction.mlx_lm_serial_lock import release as _lock_release
    _SERIAL_LOCK_AVAILABLE = True
except ImportError:
    warnings.warn(
        "[mac_worker] memexa.extraction.mlx_lm_serial_lock not found — "
        "running WITHOUT serial lock. Multiple workers may contend on :18081.",
        ImportWarning,
        stacklevel=1,
    )
    _SERIAL_LOCK_AVAILABLE = False

    def _lock_acquire(
        model: str = "gemma-31b",
        ttl_sec: int = 600,
        owner: str = "",
        timeout_sec: int = 1800,
    ) -> Optional[str]:
        """No-op stub — returns a fake token immediately."""
        return "noop-token"

    def _lock_release(token: Optional[str]) -> None:  # noqa: ARG001
        """No-op stub."""
        pass


# ── local prompt helpers (same directory) ────────────────────────────────────
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from pass1_prompt import (
    PASS1_SYSTEM_PROMPT,
    Pass1OutputError,
    build_pass1_user_prompt,
    compute_pass1_prompt_sha,
    parse_pass1_output,
)
from pass2_prompt import (
    PASS2_SYSTEM_PROMPT,
    Pass2OutputError,
    build_pass2_user_prompt,
    compute_pass2_prompt_sha,
    parse_pass2_output,
    validate_card_dict,
)
try:
    from pass2_tools import (
        MAX_TOOL_CALLS_PER_BATCH,
        TOOLS_PROMPT_SECTION,
        execute_tool,
        format_tool_result,
        parse_tool_call,
    )
    _RAG_AVAILABLE = True
except ImportError:
    _RAG_AVAILABLE = False
    TOOLS_PROMPT_SECTION = ""
    MAX_TOOL_CALLS_PER_BATCH = 3

logger = logging.getLogger("mac_worker")

# ── Mac endpoint constants ────────────────────────────────────────────────────
MAC_QWEN_URL   = "http://127.0.0.1:18080/v1/chat/completions"   # always-on
MAC_GEMMA_URL  = "http://127.0.0.1:18081/v1/chat/completions"   # on-demand
MAC_GEMMA_MODELS_URL = "http://127.0.0.1:18081/v1/models"
MAC_GEMMA_PLIST = "com.user.mlx_server_gemma_31b"

QWEN_MODEL_NAME  = "mlx-community/Qwen3-14B-4bit"
GEMMA_MODEL_NAME = "mlx-community/gemma-4-31b-it-4bit"  # matches plist com.user.mlx_server_gemma_31b


# ────────────────────────── LLM client ───────────────────────────────────────

class MlxLmClient:
    """Thin httpx wrapper for mlx_lm.server OpenAI-compatible endpoint.

    Unlike VllmClient in the your-org version, no round-robin is needed because
    the Mac server is a single instance. Kept structurally similar for easy
    diff reading.
    """

    def __init__(
        self,
        base_url: str,
        model_name: str,
        timeout_s: float = 600.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # Strip /v1/chat/completions suffix if caller passed the full URL
        # so health-check path is predictable.
        if self.base_url.endswith("/v1/chat/completions"):
            self._completions_url = self.base_url
            self._base_host = self.base_url[: -len("/v1/chat/completions")]
        else:
            self._base_host = self.base_url
            self._completions_url = f"{self.base_url}/v1/chat/completions"
        self.model_name = model_name
        self.timeout = timeout_s
        self._client = httpx.Client(timeout=timeout_s)

    def chat(
        self,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.1,
        stop: Optional[List[str]] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Send chat completion. Returns (content, raw_response)."""
        body: Dict[str, Any] = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if stop:
            body["stop"] = stop
        r = self._client.post(self._completions_url, json=body)
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        return content, data

    def health(self) -> bool:
        """Returns True if /v1/models responds 200."""
        try:
            r = self._client.get(f"{self._base_host}/v1/models", timeout=5.0)
            return r.status_code == 200
        except Exception:
            return False

    def close(self) -> None:
        self._client.close()


# ────────────────────────── Gemma-31B auto-launch ────────────────────────────

def _ensure_31b_running(timeout_sec: int = 120) -> bool:
    """Ensure Gemma-31B server is up; kickstart via ssh primary-host if needed.

    Returns True when /v1/models responds 200 within timeout_sec.
    Raises RuntimeError if it never comes online.
    """
    # Fast path: already running
    try:
        r = httpx.get(MAC_GEMMA_MODELS_URL, timeout=2.0)
        if r.status_code == 200:
            return True
    except Exception:
        pass

    logger.info("[mac_worker] Gemma-31B not responding — kickstarting via launchctl")
    subprocess.run(
        [
            "ssh", "primary-host",
            f"launchctl kickstart gui/501/{MAC_GEMMA_PLIST}",
        ],
        check=True,
        timeout=10,
    )

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            r = httpx.get(MAC_GEMMA_MODELS_URL, timeout=2.0)
            if r.status_code == 200:
                logger.info("[mac_worker] Gemma-31B is online")
                return True
        except Exception:
            pass
        time.sleep(3.0)

    raise RuntimeError(
        f"Gemma-31B failed to come online in {timeout_sec}s"
    )


# ────────────────────────── Gatekeeper prompt helpers ────────────────────────

GATEKEEPER_SYSTEM_PROMPT = """/no_think
你是 batch 信息浓度守门员。给定一段聊天 batch, 判定是否值得花算力提炼成 Card.

输出 1 行: HIGH | MEDIUM | LOW + 一句话理由 ≤30 字.

判定标准:
- HIGH: 有承诺/决定/重要信息/关系建立/购买行为/学术结论/约定时间
- MEDIUM: 闲聊但有轻量信息 (分享链接/讨论话题/问候)
- LOW: 纯 emoji / 单字符 / 重复消息 / 测试 / spam

提示: 倾向于宁可放过也不错杀; LOW 仅 obvious case.
"""


def gatekeeper_user_prompt(messages: List[Dict[str, Any]]) -> str:
    lines = ["# messages"]
    for m in messages:
        ts = m.get("ts", "??")
        c = (m.get("content") or "").strip()
        if len(c) > 200:
            c = c[:197] + "..."
        lines.append(f"[{ts}]: {c}")
    lines.append("")
    lines.append("# 判定")
    return "\n".join(lines)


def parse_gatekeeper_verdict(raw: str) -> Tuple[str, str]:
    """Returns (verdict, reason)."""
    text = raw.strip().split("\n")[0].upper()
    for v in ("HIGH", "MEDIUM", "LOW"):
        if v in text:
            return v, raw.strip()
    return "MEDIUM", raw.strip()  # default to MEDIUM if unclear


# ────────────────────────── Worker stats ─────────────────────────────────────

class WorkerStats:
    def __init__(self) -> None:
        self.total = 0
        self.skipped_done = 0
        self.processed = 0
        self.failed = 0
        self.cards_total = 0
        self.assertions_total = 0
        self.gated_low = 0
        self.gated_medium = 0
        self.gated_high = 0
        self.start_ts = time.time()

    def report(self) -> str:
        elapsed = time.time() - self.start_ts
        rate = self.processed / max(elapsed, 1.0)
        return (
            f"total={self.total} done={self.skipped_done} "
            f"proc={self.processed} fail={self.failed} "
            f"low/med/high={self.gated_low}/{self.gated_medium}/{self.gated_high} "
            f"cards={self.cards_total} assertions={self.assertions_total} "
            f"rate={rate:.2f}/s elapsed={elapsed:.0f}s"
        )


# ────────────────────────── Per-batch: Pass-1 ────────────────────────────────

def process_batch_pass1(
    batch_path: Path,
    out_dir: Path,
    done_dir: Path,
    extractor: MlxLmClient,
    stats: WorkerStats,
    *,
    dry_run: bool = False,
) -> bool:
    """Process one batch in Pass-1 mode (Gemma-31B only). Returns True on success."""
    batch_id = batch_path.parent.name if batch_path.name == "prompt.json" else batch_path.stem
    done_marker = done_dir / f"{batch_id}.done"
    if done_marker.exists():
        stats.skipped_done += 1
        return True

    try:
        prompt_data = json.loads(batch_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"  {batch_id}: failed to read prompt.json: {e}")
        stats.failed += 1
        return False

    user_prompt = build_pass1_user_prompt(
        batch_id=batch_id,
        chat_room=prompt_data.get("chat_room", ""),
        room_hash=prompt_data.get("room_hash", ""),
        batch_window_local=prompt_data.get("batch_window_local", ""),
        sender_list=prompt_data.get("sender_list", []),
        manifest_slice=prompt_data.get("manifest_slice", {}),
        messages=prompt_data.get("messages", []),
    )
    prompt_sha = compute_pass1_prompt_sha(PASS1_SYSTEM_PROMPT, user_prompt)

    if dry_run:
        print(
            f"[mac_worker][dry-run] batch={batch_id} pass=1 "
            f"extractor={extractor.model_name} prompt_sha={prompt_sha[:12]}…"
        )
        stats.processed += 1
        return True

    # ── Serial lock around Gemma-31B ─────────────────────────────────────────
    import os
    lock_wait_start = time.time()
    token = _lock_acquire(
        model="gemma-31b",
        ttl_sec=600,
        owner=f"l0_worker_v2_mac:{os.getpid()}",
        timeout_sec=1800,
    )
    if not token:
        raise RuntimeError("Could not acquire gemma-31b lock in 30min")
    lock_wait_ms = (time.time() - lock_wait_start) * 1000

    try:
        _ensure_31b_running()

        content, _raw = extractor.chat(
            system=PASS1_SYSTEM_PROMPT,
            user=user_prompt,
            max_tokens=4096,
            temperature=0.1,
        )
    except Exception as e:
        logger.error(f"  {batch_id}: LLM call failed: {e}")
        stats.failed += 1
        return False
    finally:
        _lock_release(token)

    try:
        parsed = parse_pass1_output(content)
    except Pass1OutputError as e:
        logger.warning(f"  {batch_id}: parse failed ({e}); skipping")
        stats.failed += 1
        return False

    out_record = {
        "meta": {
            "batch_id": batch_id,
            "chat_room": prompt_data.get("chat_room", ""),
            "room_hash": prompt_data.get("room_hash", ""),
            "when_start": prompt_data.get("batch_window_local", "").split(" ~ ")[0],
            "when_end": prompt_data.get("batch_window_local", "").split(" ~ ")[-1],
            "extraction_prompt_sha": prompt_sha,
            "extractor_model": extractor.model_name,
            "ts": time.time(),
        },
        "output": parsed,
    }

    out_path = out_dir / f"{batch_id}.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_record, ensure_ascii=False, indent=2), encoding="utf-8")

    done_dir.mkdir(parents=True, exist_ok=True)
    done_marker.write_text(str(time.time()), encoding="utf-8")

    n_assertions = (
        len(parsed.get("identity_assertions", []))
        + len(parsed.get("relation_assertions", []))
    )
    stats.assertions_total += n_assertions
    stats.processed += 1

    logger.info(
        f"[mac_worker] batch={batch_id} stage_a=N/A stage_b={n_assertions}_assertions "
        f"lock_wait_ms={lock_wait_ms:.0f}"
    )
    return True


# ────────────────────────── Per-batch: Pass-2 ────────────────────────────────

def process_batch_pass2(
    batch_path: Path,
    out_dir: Path,
    done_dir: Path,
    gatekeeper: Optional[MlxLmClient],
    extractor: MlxLmClient,
    stats: WorkerStats,
    *,
    rag_enabled: bool = False,
    manifest_store: Any = None,
    dry_run: bool = False,
) -> bool:
    """Process one batch in Pass-2 dual-LLM mode (Qwen gate → Gemma extract)."""
    batch_id = batch_path.parent.name if batch_path.name == "prompt.json" else batch_path.stem
    done_marker = done_dir / f"{batch_id}.done"
    if done_marker.exists():
        stats.skipped_done += 1
        return True

    try:
        prompt_data = json.loads(batch_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"  {batch_id}: read failed: {e}")
        stats.failed += 1
        return False

    messages = prompt_data.get("messages", [])

    # ── Stage A: Gatekeeper (Qwen3-14B, :18080, no lock needed) ─────────────
    verdict = "HIGH"
    if gatekeeper is not None:
        if dry_run:
            verdict = "HIGH"
            logger.info(f"[mac_worker][dry-run] batch={batch_id} stage_a=would_call_qwen")
        else:
            try:
                gk_user = gatekeeper_user_prompt(messages)
                gk_content, _ = gatekeeper.chat(
                    system=GATEKEEPER_SYSTEM_PROMPT,
                    user=gk_user,
                    max_tokens=128,
                    temperature=0.0,
                )
                verdict, _reason = parse_gatekeeper_verdict(gk_content)
            except Exception as e:
                logger.warning(
                    f"  {batch_id}: gatekeeper failed ({e}); defaulting to MEDIUM"
                )
                verdict = "MEDIUM"

    if verdict == "HIGH":
        stats.gated_high += 1
    elif verdict == "MEDIUM":
        stats.gated_medium += 1
    else:
        stats.gated_low += 1

    if verdict == "LOW":
        if dry_run:
            print(
                f"[mac_worker][dry-run] batch={batch_id} stage_a=reject stage_b=skipped"
            )
            stats.processed += 1
            return True
        out_record = {
            "meta": {
                "batch_id": batch_id,
                "skipped_by_gatekeeper": True,
                "verdict": verdict,
                "ts": time.time(),
            },
            "cards": [],
        }
        out_path = out_dir / f"{batch_id}.json"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out_record, ensure_ascii=False, indent=2), encoding="utf-8")
        done_dir.mkdir(parents=True, exist_ok=True)
        done_marker.write_text(str(time.time()), encoding="utf-8")
        stats.processed += 1
        logger.info(
            f"[mac_worker] batch={batch_id} stage_a=reject stage_b=0_cards lock_wait_ms=0"
        )
        return True

    # ── Stage B: Extractor (Gemma-31B, :18081, serial lock) ──────────────────
    # Dry-run: print plan and exit BEFORE building the prompt (avoids hitting
    # sender_list format expectations in pass2_prompt library).
    if dry_run:
        print(
            f"[mac_worker][dry-run] batch={batch_id} stage_a=accept "
            f"stage_b=would_call_gemma lock_wait_ms=0"
        )
        stats.processed += 1
        return True

    user_prompt = build_pass2_user_prompt(
        batch_id=batch_id,
        chat_room=prompt_data.get("chat_room", ""),
        room_hash=prompt_data.get("room_hash", ""),
        batch_window_local=prompt_data.get("batch_window_local", ""),
        sender_list=prompt_data.get("sender_list", []),
        manifest_slice=prompt_data.get("manifest_slice", {}),
        messages=messages,
        chinese_calendar_window=prompt_data.get("chinese_calendar_window"),
        user_calendar_window=prompt_data.get("user_calendar_window"),
    )

    system_prompt = PASS2_SYSTEM_PROMPT
    if rag_enabled and _RAG_AVAILABLE and manifest_store is not None:
        system_prompt = PASS2_SYSTEM_PROMPT + "\n\n" + TOOLS_PROMPT_SECTION

    prompt_sha = compute_pass2_prompt_sha(system_prompt, user_prompt)

    import os
    lock_wait_start = time.time()
    token = _lock_acquire(
        model="gemma-31b",
        ttl_sec=600,
        owner=f"l0_worker_v2_mac:{os.getpid()}",
        timeout_sec=1800,
    )
    if not token:
        raise RuntimeError("Could not acquire gemma-31b lock in 30min")
    lock_wait_ms = (time.time() - lock_wait_start) * 1000

    final_content: Optional[str] = None
    rag_calls_used = 0
    extended_user = user_prompt

    try:
        _ensure_31b_running()

        for _iter_idx in range(MAX_TOOL_CALLS_PER_BATCH + 1):
            content, _raw = extractor.chat(
                system=system_prompt,
                user=extended_user,
                max_tokens=8192,
                temperature=0.2,
            )
            if not (rag_enabled and _RAG_AVAILABLE):
                final_content = content
                break
            tool_call = parse_tool_call(content)
            if tool_call is None or rag_calls_used >= MAX_TOOL_CALLS_PER_BATCH:
                final_content = content
                break
            tool_name, tool_args = tool_call
            result, dur_ms = execute_tool(tool_name, tool_args, manifest_store)
            rag_calls_used += 1
            logger.info(
                f"  {batch_id}: RAG tool#{rag_calls_used} {tool_name} "
                f"({dur_ms:.0f}ms) → {str(result)[:120]}"
            )
            extended_user += format_tool_result(tool_name, tool_args, result)
        content = final_content
    except Exception as e:
        logger.error(f"  {batch_id}: extractor LLM failed: {e}")
        stats.failed += 1
        return False
    finally:
        _lock_release(token)

    try:
        cards = parse_pass2_output(content)
    except Pass2OutputError as e:
        logger.warning(f"  {batch_id}: pass2 parse failed ({e}); marking 0 cards")
        cards = []

    # Backfill required fields
    for c in cards:
        c["batch_id"] = batch_id
        c["extraction_prompt_sha"] = prompt_sha
        c["schema_v"] = 2
        if "attestation_tier" not in c:
            c["attestation_tier"] = "paired_v2"
        if "source" not in c:
            c["source"] = "wechat"

    # Validate
    valid_cards = []
    invalid_count = 0
    for c in cards:
        issues = validate_card_dict(c)
        if issues:
            logger.warning(f"  {batch_id}: card invalid {issues[:2]}; skip")
            invalid_count += 1
        else:
            valid_cards.append(c)

    out_record = {
        "meta": {
            "batch_id": batch_id,
            "chat_room": prompt_data.get("chat_room", ""),
            "room_hash": prompt_data.get("room_hash", ""),
            "verdict": verdict,
            "extraction_prompt_sha": prompt_sha,
            "gatekeeper_model": gatekeeper.model_name if gatekeeper else None,
            "extractor_model": extractor.model_name,
            "n_invalid_dropped": invalid_count,
            "ts": time.time(),
        },
        "cards": valid_cards,
    }

    out_path = out_dir / f"{batch_id}.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_record, ensure_ascii=False, indent=2), encoding="utf-8")

    done_dir.mkdir(parents=True, exist_ok=True)
    done_marker.write_text(str(time.time()), encoding="utf-8")

    stats.cards_total += len(valid_cards)
    stats.processed += 1

    stage_a_verdict = "accept" if verdict != "LOW" else "reject"
    logger.info(
        f"[mac_worker] batch={batch_id} stage_a={stage_a_verdict} "
        f"stage_b={len(valid_cards)}_cards lock_wait_ms={lock_wait_ms:.0f}"
    )
    return True


# ────────────────────────── Batch discovery ──────────────────────────────────

def collect_batch_paths(batches_dir: Path) -> List[Path]:
    """Find all prompt.json batches under batches_dir, recursively.

    Supports two structures:
    - <batches_dir>/<batch_id>/prompt.json  (your-org flat layout)
    - <batches_dir>/<date>/<batch_id>/prompt.json  (cron incremental layout, e.g. 2026-05-09/abc.../prompt.json)
    Also accepts flat <batches_dir>/<batch_id>.json files.
    2026-05-10: changed from single-level iterdir to rglob for cron compat.
    """
    paths: List[Path] = []
    # Recursive prompt.json search (covers both flat and date-nested layouts)
    paths.extend(batches_dir.rglob("prompt.json"))
    # Flat .json files at top level (legacy)
    for p in batches_dir.iterdir():
        if p.is_file() and p.suffix == ".json" and p.name != "prompt.json":
            paths.append(p)
    return sorted(paths)


# ────────────────────────── CLI main ─────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:  # noqa: C901
    parser = argparse.ArgumentParser(
        description="Mac L0 v5 Worker v2 — mlx_lm.server (Qwen3-14B + Gemma-31B)"
    )
    parser.add_argument(
        "--pass", dest="which_pass", type=int, required=True, choices=[1, 2]
    )
    parser.add_argument("--batches-dir", type=Path, required=True)
    parser.add_argument("--done-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)

    # Mac endpoints (overridable via env / args)
    parser.add_argument(
        "--gatekeeper-url", type=str,
        default=MAC_QWEN_URL,
        help="mlx_lm.server URL for Qwen3-14B gatekeeper (Pass-2 only)",
    )
    parser.add_argument(
        "--gatekeeper-model", type=str, default=QWEN_MODEL_NAME
    )
    parser.add_argument(
        "--extractor-url", type=str,
        default=MAC_GEMMA_URL,
        help="mlx_lm.server URL for Gemma-31B extractor",
    )
    parser.add_argument(
        "--extractor-model", type=str, default=GEMMA_MODEL_NAME
    )

    # Concurrency: default 1 because Stage B is serial-locked
    parser.add_argument(
        "--concurrent", type=int, default=1,
        help="Thread count for batch dispatch. Default 1 (Stage B is serial-locked). "
             "Higher values still queue behind the Gemma-31B lock.",
    )
    parser.add_argument(
        "--max-batches", type=int, default=None,
        help="Stop after N processed batches (debug / dry-run)",
    )
    parser.add_argument(
        "--rag-enabled", action="store_true",
        help="Enable RAG (recall_graph + manifest_lookup) tools in Pass-2.",
    )
    parser.add_argument(
        "--manifest-path", type=str,
        default="data/identity_manifest.yaml",
        help="Manifest path for --rag-enabled",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would happen for each batch without making HTTP calls.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        force=True,
    )

    if args.concurrent > 1:
        logger.warning(
            f"[mac_worker] --concurrent={args.concurrent} > 1. "
            "Stage B (Gemma-31B) is serial-locked; extra threads will queue "
            "behind the lock and will NOT speed up Stage B throughput."
        )

    if not _SERIAL_LOCK_AVAILABLE:
        logger.warning(
            "[mac_worker] Serial lock module unavailable — no concurrency "
            "protection on Gemma-31B :18081. Install memexa.extraction.mlx_lm_serial_lock."
        )

    # 2026-05-11 HARD invariant (CEO directive 2026-05-11): gatekeeper and
    # extractor MUST be different LLMs. Stage A = light judge, Stage B =
    # heavy extractor. Same-model collapse silently disables dual-LLM safety.
    if args.which_pass == 2 and args.gatekeeper_model \
            and args.gatekeeper_model == args.extractor_model:
        logger.error(
            f"DUAL-LLM INVARIANT BROKEN: gatekeeper_model == extractor_model "
            f"== {args.extractor_model}. Refusing to run."
        )
        return 4

    # ── Health check (skip in dry-run mode) ──────────────────────────────────
    extractor = MlxLmClient(args.extractor_url, args.extractor_model)
    gatekeeper: Optional[MlxLmClient] = None

    if not args.dry_run:
        # Qwen3-14B must be up (always-on)
        if args.which_pass == 2 and args.gatekeeper_url:
            gatekeeper = MlxLmClient(args.gatekeeper_url, args.gatekeeper_model)
            if not gatekeeper.health():
                logger.error(
                    f"[mac_worker] Qwen3-14B gatekeeper at {args.gatekeeper_url} not healthy. "
                    "Ensure mlx_lm.server :18080 is running on Mac."
                )
                return 3

        # Gemma-31B: auto-launch if absent
        try:
            _ensure_31b_running(timeout_sec=120)
        except RuntimeError as e:
            logger.error(f"[mac_worker] {e}")
            return 3
    else:
        logger.info("[mac_worker] dry-run mode — skipping health checks and HTTP calls")
        if args.which_pass == 2:
            gatekeeper = MlxLmClient(args.gatekeeper_url, args.gatekeeper_model)

    # ── Collect batches ───────────────────────────────────────────────────────
    stats = WorkerStats()
    batch_paths = collect_batch_paths(args.batches_dir)
    stats.total = len(batch_paths)
    logger.info(f"[mac_worker] found {len(batch_paths)} batches in {args.batches_dir}")

    if args.max_batches:
        batch_paths = batch_paths[: args.max_batches]
        logger.info(f"[mac_worker] limited to first {args.max_batches}")

    if args.dry_run:
        print(
            f"[mac_worker][dry-run] plan: pass={args.which_pass} "
            f"batches={len(batch_paths)} concurrent={args.concurrent} "
            f"gatekeeper={args.gatekeeper_url} extractor={args.extractor_url}"
        )

    # ── Load manifest for RAG mode ────────────────────────────────────────────
    manifest_store = None
    if args.rag_enabled and args.which_pass == 2:
        if not _RAG_AVAILABLE:
            logger.error("--rag-enabled set but pass2_tools module not available")
            return 4
        try:
            ROOT = Path(__file__).resolve().parent.parent.parent.parent
            sys.path.insert(0, str(ROOT))
            from memexa.core.identity_manifest import ManifestStore  # type: ignore[import]
            manifest_store = ManifestStore.load(args.manifest_path)
            logger.info(f"[mac_worker] RAG enabled. manifest stats: {manifest_store.stats()}")
        except Exception as e:
            logger.error(f"[mac_worker] Failed to load manifest for RAG: {e}")
            return 5

    # ── Worker function ───────────────────────────────────────────────────────
    def _worker(bp: Path) -> bool:
        try:
            if args.which_pass == 1:
                return process_batch_pass1(
                    bp, args.out_dir, args.done_dir, extractor, stats,
                    dry_run=args.dry_run,
                )
            else:
                return process_batch_pass2(
                    bp, args.out_dir, args.done_dir,
                    gatekeeper, extractor, stats,
                    rag_enabled=args.rag_enabled,
                    manifest_store=manifest_store,
                    dry_run=args.dry_run,
                )
        except Exception as e:
            logger.error(f"worker exception on {bp}: {e}\n{traceback.format_exc()}")
            stats.failed += 1
            return False

    last_log = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrent) as ex:
        futures = {ex.submit(_worker, bp): bp for bp in batch_paths}
        for _ in as_completed(futures):
            if time.time() - last_log > 30:
                logger.info(f"[mac_worker] PROGRESS: {stats.report()}")
                last_log = time.time()

    logger.info(f"[mac_worker] FINAL: {stats.report()}")

    extractor.close()
    if gatekeeper:
        gatekeeper.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
