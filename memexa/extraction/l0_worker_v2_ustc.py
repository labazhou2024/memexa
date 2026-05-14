"""your-org L0 v5 Worker v2 (Pass-1 OR Pass-2, dual-LLM Pass-2 mode).

Spec: docs/l0_v5/MASTER_PLAN.md §4.1, §4.3, §11

Architecture:
- Designed to run on your-org GPU server (one worker per GPU shard)
- Pass-1 mode: Gemma 4 31B AWQ alone (gatekeeper not needed for assertion mining)
- Pass-2 mode: Qwen3-14B-AWQ gatekeeper → Gemma 4 31B AWQ extractor

Inputs (per batch):
- prompt.json: {batch_id, chat_room, room_hash, batch_window_local,
                sender_list, messages, manifest_slice}

Outputs:
- Pass-1: <out>/<shard>/<batch_id>.json with {meta, output: {assertions...}}
- Pass-2: <out>/<shard>/<batch_id>.json with {meta, cards: [...]}

Resilience:
- Per-batch try/except — one failure doesn't stop shard
- .done sentinel files (batch -> done) for resume
- Idempotent: re-running on existing done batches is no-op

Concurrency:
- ThreadPoolExecutor with --concurrent N httpx requests
- vLLM is the bottleneck, this just queues batches

Usage (your-org side):
  python l0_worker_v2_ustc.py \
    --pass {1,2} \
    --batches-dir /tmp/memexa_l0_ustc/data/.../batches/shard0 \
    --done-dir   /tmp/memexa_l0_ustc/data/.../done/shard0 \
    --out-dir    /tmp/memexa_l0_ustc/data/.../<pass1_out|cards_v2>/shard0 \
    --gatekeeper-url http://127.0.0.1:8001  # Qwen3-14B (pass2 only)
    --extractor-url  http://127.0.0.1:8011  # Gemma 4 31B AWQ
    --concurrent 4
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import httpx
except ImportError:
    print("FATAL: httpx not installed. pip install httpx", file=sys.stderr)
    sys.exit(2)

# Add data/l0_v5/code to path so we can import prompts
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

logger = logging.getLogger("l0_worker_v2")


# ────────────────────────── LLM client ──────────────────────────

class VllmClient:
    """Thin httpx wrapper for vLLM /v1/chat/completions.

    Supports multi-URL round-robin (CSV of URLs) for GPU load balancing.
    """

    def __init__(
        self,
        base_url: str,
        model_name: str,
        timeout_s: float = 600.0,
    ):
        # base_url may be CSV: "http://host:8011,http://host:8211"
        urls = [u.strip().rstrip("/") for u in base_url.split(",") if u.strip()]
        self.base_urls = urls
        self.base_url = urls[0]  # for back-compat
        self.model_name = model_name
        self.timeout = timeout_s
        self._client = httpx.Client(timeout=timeout_s)
        import threading
        self._rr_lock = threading.Lock()
        self._rr_idx = 0

    def _next_url(self) -> str:
        if len(self.base_urls) <= 1:
            return self.base_urls[0]
        with self._rr_lock:
            url = self.base_urls[self._rr_idx]
            self._rr_idx = (self._rr_idx + 1) % len(self.base_urls)
        return url

    def chat(
        self,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.1,
        stop: Optional[List[str]] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Send chat completion. Returns (content, raw_response)."""
        body = {
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
        url = self._next_url()
        r = self._client.post(f"{url}/v1/chat/completions", json=body)
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        return content, data

    def health(self) -> bool:
        # Healthy if at least one URL responds
        for url in self.base_urls:
            try:
                r = self._client.get(f"{url}/health", timeout=5.0)
                if r.status_code == 200:
                    return True
            except Exception:
                continue
        return False

    def close(self) -> None:
        self._client.close()


# ────────────────────────── Gatekeeper ──────────────────────────

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


# ────────────────────────── Per-batch processing ──────────────────────────

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


def process_batch_pass1(
    batch_path: Path,
    out_dir: Path,
    done_dir: Path,
    extractor: VllmClient,
    stats: WorkerStats,
) -> bool:
    """Process one batch in Pass-1 mode. Returns True on success."""
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

    sender_list = prompt_data.get("sender_list", [])
    manifest_slice = prompt_data.get("manifest_slice", {})

    user_prompt = build_pass1_user_prompt(
        batch_id=batch_id,
        chat_room=prompt_data.get("chat_room", ""),
        room_hash=prompt_data.get("room_hash", ""),
        batch_window_local=prompt_data.get("batch_window_local", ""),
        sender_list=sender_list,
        manifest_slice=manifest_slice,
        messages=prompt_data.get("messages", []),
    )

    prompt_sha = compute_pass1_prompt_sha(PASS1_SYSTEM_PROMPT, user_prompt)

    try:
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
    out_path.write_text(json.dumps(out_record, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    done_dir.mkdir(parents=True, exist_ok=True)
    done_marker.write_text(str(time.time()), encoding="utf-8")

    n_assertions = (len(parsed.get("identity_assertions", []))
                    + len(parsed.get("relation_assertions", [])))
    stats.assertions_total += n_assertions
    stats.processed += 1
    return True


def process_batch_pass2(
    batch_path: Path,
    out_dir: Path,
    done_dir: Path,
    gatekeeper: Optional[VllmClient],
    extractor: VllmClient,
    stats: WorkerStats,
    *,
    rag_enabled: bool = False,
    manifest_store=None,
) -> bool:
    """Process one batch in Pass-2 dual-LLM mode."""
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

    # ── Gatekeeper (Qwen3-14B) ──
    verdict = "HIGH"
    if gatekeeper is not None:
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
            logger.warning(f"  {batch_id}: gatekeeper failed ({e}); defaulting to MEDIUM")
            verdict = "MEDIUM"

    if verdict == "HIGH":
        stats.gated_high += 1
    elif verdict == "MEDIUM":
        stats.gated_medium += 1
    else:
        stats.gated_low += 1

    if verdict == "LOW":
        # Skip extraction, write empty cards record
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
        out_path.write_text(json.dumps(out_record, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        done_dir.mkdir(parents=True, exist_ok=True)
        done_marker.write_text(str(time.time()), encoding="utf-8")
        stats.processed += 1
        return True

    # ── Extractor (Gemma 4 31B AWQ) ──
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

    # RAG agentic loop
    rag_calls_used = 0
    extended_user = user_prompt
    final_content = None
    try:
        for iter_idx in range(MAX_TOOL_CALLS_PER_BATCH + 1):
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
    out_path.write_text(json.dumps(out_record, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    done_dir.mkdir(parents=True, exist_ok=True)
    done_marker.write_text(str(time.time()), encoding="utf-8")

    stats.cards_total += len(valid_cards)
    stats.processed += 1
    return True


# ────────────────────────── CLI ──────────────────────────

def collect_batch_paths(batches_dir: Path) -> List[Path]:
    """Each batch is either a prompt.json under <batches_dir>/<batch_id>/ or
    flat <batches_dir>/<batch_id>.json.
    """
    paths: List[Path] = []
    for p in batches_dir.iterdir():
        if p.is_file() and p.suffix == ".json":
            paths.append(p)
        elif p.is_dir() and (p / "prompt.json").exists():
            paths.append(p / "prompt.json")
    return sorted(paths)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pass", dest="which_pass", type=int, required=True,
                        choices=[1, 2])
    parser.add_argument("--batches-dir", type=Path, required=True)
    parser.add_argument("--done-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--gatekeeper-url", type=str, default=None,
                        help="vLLM URL of Qwen3-14B-AWQ (Pass-2 only)")
    parser.add_argument("--gatekeeper-model", type=str, default="memexa-gatekeeper")
    parser.add_argument("--extractor-url", type=str, required=True,
                        help="vLLM URL of Gemma 4 31B AWQ")
    parser.add_argument("--extractor-model", type=str, default="memexa-extractor")
    parser.add_argument("--concurrent", type=int, default=4)
    parser.add_argument("--max-batches", type=int, default=None,
                        help="Stop after N processed batches (debug)")
    parser.add_argument("--rag-enabled", action="store_true",
                        help="Phase B: enable RAG (recall_graph + manifest_lookup) "
                             "tools. Requires bank ≥500 cards + manifest ≥30 persons.")
    parser.add_argument("--clean-after-done", action="store_true",
                        help="PII: delete source prompt.json after .done sentinel "
                             "(safe; cards already in out_dir).")
    parser.add_argument("--manifest-path", type=str,
                        default="data/identity_manifest.yaml",
                        help="Used for manifest_lookup tool when --rag-enabled")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        force=True,
    )

    extractor = VllmClient(args.extractor_url, args.extractor_model)
    if not extractor.health():
        logger.error(f"extractor at {args.extractor_url} not healthy")
        return 3

    gatekeeper: Optional[VllmClient] = None
    if args.which_pass == 2 and args.gatekeeper_url:
        gatekeeper = VllmClient(args.gatekeeper_url, args.gatekeeper_model)
        if not gatekeeper.health():
            logger.error(f"gatekeeper at {args.gatekeeper_url} not healthy")
            return 3

    stats = WorkerStats()
    batch_paths = collect_batch_paths(args.batches_dir)
    stats.total = len(batch_paths)
    logger.info(f"found {len(batch_paths)} batches in {args.batches_dir}")

    if args.max_batches:
        batch_paths = batch_paths[: args.max_batches]
        logger.info(f"limited to first {args.max_batches}")

    # Load manifest for RAG mode
    manifest_store = None
    if args.rag_enabled and args.which_pass == 2:
        if not _RAG_AVAILABLE:
            logger.error("--rag-enabled set but pass2_tools module not available")
            return 4
        try:
            ROOT = Path(__file__).resolve().parent.parent.parent.parent
            sys.path.insert(0, str(ROOT))
            from memexa.core.identity_manifest import ManifestStore
            manifest_store = ManifestStore.load(args.manifest_path)
            logger.info(f"RAG enabled. manifest stats: {manifest_store.stats()}")
        except Exception as e:
            logger.error(f"Failed to load manifest for RAG: {e}")
            return 5

    # Worker fn
    def _worker(bp: Path) -> bool:
        try:
            if args.which_pass == 1:
                ok = process_batch_pass1(bp, args.out_dir, args.done_dir,
                                          extractor, stats)
            else:
                ok = process_batch_pass2(bp, args.out_dir, args.done_dir,
                                          gatekeeper, extractor, stats,
                                          rag_enabled=args.rag_enabled,
                                          manifest_store=manifest_store)
            # PII: optional auto-clean of source after .done sentinel
            if ok and args.clean_after_done:
                bid = bp.parent.name if bp.name == "prompt.json" else bp.stem
                done_marker = args.done_dir / f"{bid}.done"
                if done_marker.exists():
                    try:
                        bp.unlink()
                        # Also remove parent dir if empty
                        if bp.parent != bp.parent.parent and not list(bp.parent.iterdir()):
                            bp.parent.rmdir()
                    except OSError:
                        pass
            return ok
        except Exception as e:
            logger.error(f"worker exception on {bp}: {e}\n{traceback.format_exc()}")
            stats.failed += 1
            return False

    last_log = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrent) as ex:
        futures = {ex.submit(_worker, bp): bp for bp in batch_paths}
        for fut in as_completed(futures):
            if time.time() - last_log > 30:
                logger.info(f"PROGRESS: {stats.report()}")
                last_log = time.time()

    logger.info(f"FINAL: {stats.report()}")

    extractor.close()
    if gatekeeper:
        gatekeeper.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
