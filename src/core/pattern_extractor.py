"""
Pattern Extractor -- metaswarm BEADS 模式的 Python 实现

借鉴 metaswarm 的自改进闭环:
- 从 code review findings / test failures / 架构决策中提取可复用 pattern
- 写入 JSONL 知识库 (data/improvement_patterns.jsonl)
- 支持按文件/关键词/任务类型检索相关 pattern
- 支持置信度评分和使用计数

两种用法:
1. 提取: python -m src.core.pattern_extractor extract --source "code_review" --input "..."
2. 检索: python -m src.core.pattern_extractor prime --files "memex/core/*.py" --keywords "hook"

Lock acquisition order [LOG-R1-009 2026-04-20]
-----------------------------------------------
Three cross-process filelocks may be held in this module AND in
hook_session_end. To prevent deadlock, always acquire them in this order
(finest granularity first, coarsest last; release in reverse):

    1. _sid_lock()              -- current_session_id.txt.lock
                                   (per-sid identity read/write)
    2. _credit_lock(data_dir, sid) in hook_session_end
                                -- .credit_lock_<digest>.lock
                                   (per-sid credit critical section)
    3. _PATTERNS_FILE lock       -- improvement_patterns.jsonl.lock
                                   (global knowledge-base rewrite, held
                                   inside _atomic_rewrite_patterns)

Rationale: _sid_lock() is the narrowest scope (a single file, held for
microseconds). _credit_lock() scopes an entire credit operation for one
session. The patterns-file lock is the widest (global KB) and must be
innermost so a long rewrite never blocks a different session's credit
attempt from acquiring its own sid+credit locks.

DO NOT reverse this order (e.g., grabbing the patterns lock before
_credit_lock) — the two processes doing that while another acquires them
in the documented order will deadlock.

Call-site convention: each acquisition site carries a short
``# LOCK-ORDER: <n>`` comment so the ordering can be audited with ripgrep.
"""

import json
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import List, Literal, Optional

def _resolve_data_dir() -> Path:
    """Resolve data dir. Respects MEMEX_DATA_DIR env var for isolation testing.

    [SEC-3 + LOGIC-HIGH-3] Env var override enables subprocess tests to run
    against a tmp copy of prod data without ever mutating real prod files.
    Falls back to Path(__file__).parent.parent / "data" in production.

    [SEC-MED Round1 fix 2026-04-18] Env override must resolve to a path inside
    the workspace root OR the system temp dir. Otherwise fall back to default
    (prevents subprocess-propagated traversal to attacker-controlled path).
    """
    default = Path(__file__).parent.parent / "data"
    env_override = os.environ.get("MEMEX_DATA_DIR")
    if env_override:
        try:
            p = Path(env_override).resolve()
            if not p.is_dir():
                return default
            workspace_root = Path(__file__).parent.parent.parent.parent.resolve()
            import tempfile as _t
            temp_root = Path(_t.gettempdir()).resolve()
            # Allow only: inside workspace OR inside system temp
            try:
                p.relative_to(workspace_root)
                return p
            except ValueError:
                pass
            try:
                p.relative_to(temp_root)
                return p
            except ValueError:
                pass
            return default  # reject path outside sanctioned roots
        except (OSError, ValueError):
            return default
    return default


_DATA_DIR = _resolve_data_dir()
_PATTERNS_FILE = _DATA_DIR / "improvement_patterns.jsonl"


# ── 数据模型 (对标 metaswarm BEADS schema) ──

@dataclass
class Provenance:
    source: str          # code_review, test_failure, architecture, conversation, agent
    reference: str       # PR/commit/file reference
    date: str            # ISO-8601


@dataclass
class PatternEntry:
    id: str = ""
    type: Literal[
        "pattern", "gotcha", "decision",
        "code_quirk", "anti_pattern", "performance", "security"
    ] = "pattern"
    fact: str = ""                       # 观察到的现象/问题
    recommendation: str = ""             # 推荐的做法
    confidence: Literal["high", "medium", "low"] = "medium"
    tags: List[str] = field(default_factory=list)
    affected_files: List[str] = field(default_factory=list)   # glob patterns
    affected_services: List[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    usage_count: int = 0
    helpful_count: int = 0
    outdated_reports: int = 0
    auto_generated: bool = False  # True if created by meta-pattern extraction (subagent stats, etc.)
    provenance: List[dict] = field(default_factory=list)
    # [L5 Phase 1 2026-04-18] TTL tracking fields
    last_primed: Optional[str] = None   # ISO-8601 UTC, updated every prime() hit
    last_used: Optional[str] = None     # ISO-8601 UTC, updated when primed + no correction follows (implicit confirmation)
    # [Schema v2 2026-04-19 T5] maturity + provenance + promotion pipeline
    # [Schema v3 2026-04-21 Phase A] canonical_tags + fingerprint
    schema_version: int = 3              # v3: adds canonical_tags + fingerprint
    parent_pattern_id: Optional[str] = None  # 派生 pattern 父 ID (poisoning fuse)
    source: Literal[
        "ceo_edit", "human_turn", "autodream",
        "reviewer_finding", "auto_extracted", "human_turn_historical"
    ] = "auto_extracted"
    promotion_status: Literal[
        "draft", "ceo_pending", "ceo_approved",
        "ceo_rejected", "promoted_to_memory"
    ] = "draft"
    last_hit_ts: Optional[str] = None    # ISO-8601 UTC, 最近 prime 命中 (temporal decay)
    # A3 (2026-04-21): canonicalized entity tags via canonicalizer.py. Empty
    # list for legacy v2 entries; populated at ingest time by A2 (via
    # memory_ingest_watcher → canonicalize_entity on Haiku tags + file stem).
    # Used by A4 retrieve-boost (jaccard overlap with query canonicals) and
    # A7 fingerprint-merge (count-accumulation across same-fact extracts).
    canonical_tags: List[str] = field(default_factory=list)
    # A7 (2026-04-21): content-fingerprint for save-time merge. Computed as
    # sha256(sorted(canonical_tags) + fact[:50])[:16]. Legacy v2 entries
    # carry "" (empty) and are excluded from merge buckets to prevent
    # cross-legacy collapse.
    fingerprint: str = ""

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now


# ── 提取逻辑 ──

# metaswarm 的 6 类触发模式
_TRIGGER_PATTERNS = {
    "correction": [
        r"(?:不对|错了|应该是|其实是|No,?\s+actually|that'?s wrong)",
        r"(?:修正|纠正|更正|correct)",
    ],
    "discovery": [
        r"(?:发现|原来是|it turns out|I realized|the issue was|根本原因)",
        r"(?:root cause|bug.*因为|失败.*因为)",
    ],
    "decision": [
        r"(?:决定|选择|采用|let'?s proceed with|we should use)",
        r"(?:方案[A-Z]|选型|architecture decision)",
    ],
    "anti_pattern": [
        r"(?:不要|禁止|避免|不应该|NEVER|never|don'?t)",
        r"(?:anti-?pattern|坑|踩坑|gotcha)",
    ],
    "performance": [
        r"(?:性能|慢|优化|加速|O\(n\)|timeout|内存)",
        r"(?:performance|slow|optimize|bottleneck)",
    ],
    "security": [
        r"(?:安全|注入|XSS|CSRF|漏洞|credential|secret)",
        r"(?:security|injection|vulnerability)",
    ],
}


def classify_trigger(text: str) -> Optional[str]:
    """检测文本中的触发模式类别"""
    for category, patterns in _TRIGGER_PATTERNS.items():
        for p in patterns:
            if re.search(p, text, re.IGNORECASE):
                return category
    return None


def extract_affected_files(text: str) -> List[str]:
    """从文本中提取文件路径"""
    # 匹配常见路径模式
    paths = re.findall(r"[\w./\\-]+\.(?:py|js|ts|md|json|yaml|yml|toml|tex)", text)
    # 去重并转为 glob
    return list(set(paths))


def extract_tags(text: str, source: str) -> List[str]:
    """从文本中提取语义标签"""
    tags = [source]
    tag_patterns = {
        "testing": r"(?:test|pytest|测试|assert)",
        "hook": r"(?:hook|hooks|生命周期)",
        "mcp": r"(?:MCP|mcp_server|tool)",
        "memory": r"(?:memory|记忆|MEMORY\.md)",
        "agent": r"(?:agent|subagent|代理)",
        "config": r"(?:config|settings|配置|yaml)",
        "database": r"(?:sqlite|db|database|SQL)",
        "api": r"(?:API|api|endpoint|route)",
        "git": r"(?:git|commit|branch|merge)",
        "async": r"(?:async|await|asyncio|并发)",
    }
    for tag, pattern in tag_patterns.items():
        if re.search(pattern, text, re.IGNORECASE):
            tags.append(tag)
    return tags


def extract_pattern_from_review(
    findings: str,
    source: str = "code_review",
    reference: str = "",
) -> List[PatternEntry]:
    """
    从 code review findings 中提取可复用 pattern。

    Args:
        findings: review 的 findings 文本 (可以是 JSON 或纯文本)
        source: 来源类型
        reference: 引用 (commit hash, PR number 等)

    Returns:
        提取的 PatternEntry 列表
    """
    entries = []

    # 尝试解析 JSON findings
    try:
        data = json.loads(findings)
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict) and "findings" in data:
            items = data["findings"]
        else:
            items = [data]
    except (json.JSONDecodeError, TypeError):
        # 纯文本: 按段落分割
        items = [{"text": p.strip()} for p in findings.split("\n\n") if p.strip()]

    for item in items:
        text = item.get("text", item.get("message", item.get("description", str(item))))
        if len(text) < 20:
            continue  # 质量门: 太短的跳过

        trigger_type = classify_trigger(text)
        if not trigger_type:
            trigger_type = "pattern"

        # 映射触发类型到 PatternEntry.type
        type_map = {
            "correction": "gotcha",
            "discovery": "code_quirk",
            "decision": "decision",
            "anti_pattern": "anti_pattern",
            "performance": "performance",
            "security": "security",
        }

        # [Schema v2 T5] Map extraction source label -> PatternEntry.source enum.
        # Unknown sources fall back to "auto_extracted" (safest default).
        _source_map = {
            "code_review": "reviewer_finding",
            "session_review": "reviewer_finding",
            "security_scan": "reviewer_finding",
        }
        pe_source = _source_map.get(source, "auto_extracted")
        entry = PatternEntry(
            type=type_map.get(trigger_type, "pattern"),
            fact=text[:500],
            recommendation=item.get("recommendation", item.get("fix", "")),
            confidence=item.get("severity", "medium"),
            tags=extract_tags(text, source),
            affected_files=extract_affected_files(text),
            affected_services=[],
            source=pe_source,
            provenance=[asdict(Provenance(
                source=source,
                reference=reference or "session",
                date=datetime.now().isoformat(),
            ))],
        )

        # 置信度映射
        severity = item.get("severity", "")
        if severity in ("critical", "high"):
            entry.confidence = "high"
        elif severity in ("medium",):
            entry.confidence = "medium"
        elif severity in ("low", "info"):
            entry.confidence = "low"

        entries.append(entry)

    return entries


# ── 持久化 ──

def _compute_fingerprint(canonical_tags: List[str], fact: str) -> str:
    """A7 (2026-04-21): content-fingerprint for save-time merge.

    sha256(sorted(canonical_tags) + "|" + fact[:50])[:16].

    Design rationale:
    - fact[:50] cutoff trades precision for merge-rate. Two rules that
      diverge only after char 50 merge incorrectly; that's covered by
      Risk #10 monitoring (see plan v2) and the MEMEX_FINGERPRINT_MERGE
      env kill-switch.
    - Legacy entries (pre-A3) carry fingerprint="" and MUST be excluded
      from merge buckets to prevent mass-collapse of unrelated patterns
      (A7-4 test).
    - Empty canonical_tags is allowed: the fingerprint still hashes on
      fact prefix, so two identical-fact entries dedupe even without
      entity signal.
    """
    import hashlib
    payload = json.dumps(sorted(canonical_tags), ensure_ascii=False) + "|" + (fact or "")[:50]
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def save_patterns(entries: List[PatternEntry]) -> int:
    """追加 pattern 到 JSONL 文件。返回新增条数。

    A7 (2026-04-21): before appending, compute fingerprint per incoming
    entry. If fingerprint matches an existing pattern's fingerprint,
    increment that pattern's helpful_count via bump_helpful_count (an
    existing _atomic_rewrite_patterns-based mutator, race-safe) and
    SKIP the append. This collapses repeat extracts of the same canonical
    fact into one entry with growing helpful_count — directly answers
    CEO 2026-04-20 23:50 concern about "冗余节点".

    A7 kill-switch: set env MEMEX_FINGERPRINT_MERGE=0 to disable merge
    and fall back to pre-A7 append-only behavior. Default ON.
    """
    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    merge_enabled = os.environ.get("MEMEX_FINGERPRINT_MERGE", "1") != "0"

    # A7: build fp → existing_id map from current on-disk state. Exclude
    # entries with empty fingerprint (legacy v2) to prevent cross-legacy
    # collapse. bump_helpful_count uses _atomic_rewrite_patterns for
    # race-safety (proven in Audit Closeout Final e50ad34).
    fp_to_existing: Dict[str, str] = {}

    # 加载现有 pattern 用于去重
    existing_facts = set()
    if _PATTERNS_FILE.exists():
        for line in _PATTERNS_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    data = json.loads(line)
                    existing_facts.add(data.get("fact", "")[:100])
                    if merge_enabled:
                        fp = data.get("fingerprint", "")
                        if fp:  # skip empty = legacy
                            fp_to_existing[fp] = data.get("id", "")
                except json.JSONDecodeError:
                    pass

    # A7: collect merge-targets BEFORE opening the patterns file for
    # append. _atomic_rewrite_patterns (called by record_pattern_helpful)
    # uses os.replace which fails on Windows if this process holds the
    # target open — so we must sequence: [pass 1] compute + append, close
    # file, [pass 2] bump helpful_count for merge-targets.
    # LOGIC-R1-02 fix: use dict keyed by existing_id so in-batch duplicate
    # fingerprints don't double-count Risk-10 trace events.
    merge_seen: Dict[str, dict] = {}  # existing_id -> trace payload

    added = 0
    with open(_PATTERNS_FILE, "a", encoding="utf-8") as f:
        for entry in entries:
            # A7: populate fingerprint on the entry itself so on-disk
            # asdict() carries it. LOGIC-R1-04 fix: unconditionally
            # recompute when merge_enabled so a pre-set stale fingerprint
            # cannot induce a spurious merge.
            if merge_enabled:
                entry.fingerprint = _compute_fingerprint(
                    entry.canonical_tags, entry.fact,
                )

            # A7: fingerprint-merge check BEFORE fact[:100] dedup.
            # If matching existing pattern, queue a helpful_count bump
            # (deferred until after file close) and skip the append.
            # LOGIC-R1-02: in-batch duplicates collapse via merge_seen dict.
            if merge_enabled and entry.fingerprint and entry.fingerprint in fp_to_existing:
                existing_id = fp_to_existing[entry.fingerprint]
                if existing_id not in merge_seen:
                    merge_seen[existing_id] = {
                        "existing_id": existing_id,
                        "fingerprint": entry.fingerprint,
                        "canonical_tags": entry.canonical_tags[:5],
                    }
                continue  # merge-intent queued; skip the append

            # 去重: 比较 fact 前 100 字符
            if entry.fact[:100] in existing_facts:
                continue
            # [V-5] Mojibake guard: skip entries with encoding corruption
            mojibake = detect_mojibake(entry.fact + " " + entry.recommendation)
            if mojibake:
                # Log warning via events file if available (best-effort)
                try:
                    from src.core._hook_utils import log_hook_event
                    log_hook_event("pattern_encoding_warning", "save_patterns", details={
                        "detection": mojibake,
                        "fact_preview": entry.fact[:80],
                    })
                except Exception:
                    pass
                continue  # don't poison KB
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
            existing_facts.add(entry.fact[:100])
            # A7: new entries contribute to the merge map for subsequent
            # entries in the same batch.
            if merge_enabled and entry.fingerprint:
                fp_to_existing[entry.fingerprint] = entry.id
            added += 1

    # A7 pass 2: now that the append handle is closed, bump helpful_count
    # for each merge target. _atomic_rewrite_patterns can safely os.replace
    # because no one in this process holds _PATTERNS_FILE open.
    if merge_enabled and merge_seen:
        try:
            record_pattern_helpful(
                list(merge_seen.keys()),
                reason="fingerprint_merge",
            )
            # Risk #10 collision monitoring — one trace event per unique target.
            try:
                from src.core.trace_sink import write_trace_event
                for payload in merge_seen.values():
                    write_trace_event("fingerprint_merge", payload)
            except Exception:
                pass
        except Exception:
            # Bump failure is observational; incoming entry already skipped,
            # so worst case is helpful_count didn't go up. Fail-soft.
            pass

    return added


_TTL_DAYS = 30  # 30 天未被 prime 命中则降级

# Session-level tracking files (for helpful_count feedback loop)
_PRIMED_SESSION_FILE = _DATA_DIR / "primed_patterns_session.jsonl"
_CORRECTION_PROV_FILE = _DATA_DIR / "correction_provenance.jsonl"
_CURRENT_SESSION_FILE = _DATA_DIR / "current_session_id.txt"

# In-process cache: avoid re-reading the JSONL on every UserPromptSubmit hook.
# load_all_patterns is called from smart_prime which fires on every message.
_patterns_cache: tuple = (0.0, [])  # (mtime, entries)

# Gate event rate-limit cache: (gate, rule, target_basename) -> last_emit_ts
_gate_allow_dedup: dict = {}


# ── Mojibake detector [V-5] ──

_STOPWORDS = {
    "the", "is", "a", "an", "in", "on", "with", "not", "do", "don't",
    "be", "and", "or", "use", "file", "path", "should", "must",
    "to", "for", "of", "at", "by", "from", "as", "it", "that", "this",
    "i", "you", "we", "they", "he", "she", "but", "if", "when", "where",
}


def detect_mojibake(text: str) -> Optional[str]:
    """Detect encoding corruption in text. Returns detection type or None.

    [V-5 + SEC-9] Narrowed to reduce false positives on scientific Unicode:
      (a) Unicode replacement char (U+FFFD)
      (b) Known GBK-as-UTF-8 byte sequences
      (c) CJK + C1 control codepoints (U+0080-U+009F) mixed runs >=3
          (was U+0080-U+00FF which falsely flagged angstrom/accented symbols)
    """
    if not isinstance(text, str) or not text:
        return None
    if "\ufffd" in text:
        return "replacement_char"
    if "ʹ��" in text or "�Դ" in text:
        return "gbk_mojibake"
    has_cjk = any("\u4e00" <= c <= "\u9fff" for c in text)
    if has_cjk:
        run = 0
        for c in text:
            # Only C1 control range (U+0080-U+009F) - these are genuine mojibake signals
            # Excludes U+00A0-U+00FF which includes valid chars like Å, é, °, µ
            if "\u0080" <= c <= "\u009f":
                run += 1
                if run >= 3:
                    return "mixed_encoding"
            else:
                run = 0
    return None


def _sanitize_correction_text(s: str, max_len: int = 200) -> str:
    """[SEC-3, SEC-6] Sanitize user text before audit/KB storage.

    - Strip C0 control chars except tab/newline
    - Strip Unicode direction-override/format chars (U+200E..U+202E, U+2066..U+2069)
    - Redact obvious API key patterns (sk-*, api-*, Bearer *, 40+ hex)
    - Truncate to max_len
    """
    import re as _re
    if not isinstance(s, str):
        s = str(s)
    # Strip C0 controls except \t\n
    s = _re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", s)
    # Strip direction-override + format chars
    s = _re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", s)
    # Redact common secret patterns
    s = _re.sub(r"sk-[A-Za-z0-9]{10,}", "[REDACTED_KEY]", s)
    s = _re.sub(r"(?i)bearer\s+[A-Za-z0-9._-]{10,}", "[REDACTED_BEARER]", s)
    s = _re.sub(r"\b[a-fA-F0-9]{40,}\b", "[REDACTED_HEX]", s)
    return s[:max_len].strip()


# ── Session ID helpers [V-3] ──

_cached_fallback_sid: Optional[str] = None  # stable within-process fallback


def _sid_lock():
    """[AC-6] Cross-process filelock for sid.txt read/write serialization.

    Returns a filelock context manager, or nullcontext if the library is
    unavailable. Prevents a racy interleaving where reader A sees missing
    sid.txt and generates fallback-T1 while writer B is mid-write of a
    real sid, leading to divergent sids across two hook processes.
    """
    # LOCK-ORDER: 1 (sid identity)
    try:
        import filelock
    except ImportError:
        from contextlib import nullcontext
        return nullcontext()
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return filelock.FileLock(str(_CURRENT_SESSION_FILE) + ".lock", timeout=5)


def get_current_session_id() -> str:
    """Return current session UUID, or cached stable fallback if file missing.

    [LOGIC-H2] If sid.txt disappears mid-session, we MUST return the SAME
    fallback on every call (not a fresh timestamp) — otherwise prime() tags
    with fallback-T1 but hook_session_end reads fallback-T2 and finds 0
    matching lines, silently losing all credit.

    [AC-6] Priority order:
      1. CLAUDE_SESSION_ID env var (set by Claude Code harness, authoritative)
      2. sid.txt file (filelock-protected read)
      3. Process-cached fallback sid (stable across calls even if sid.txt
         is deleted mid-session)
    """
    global _cached_fallback_sid
    # [AC-6] Env var wins: Claude Code harness authoritative source
    env_sid = os.environ.get("CLAUDE_SESSION_ID", "").strip()
    if env_sid:
        return env_sid
    # Filelock around file read to avoid torn-read with concurrent writer
    try:
        with _sid_lock():
            if _CURRENT_SESSION_FILE.exists():
                sid = _CURRENT_SESSION_FILE.read_text(encoding="utf-8").strip()
                if sid:
                    return sid
    except Exception:
        # filelock Timeout or OSError — fall through to cached fallback
        pass
    # Fallback: lazy-init ONCE per process, reused for rest of session
    if _cached_fallback_sid is None:
        _cached_fallback_sid = f"fallback-{int(datetime.now().timestamp())}"
    return _cached_fallback_sid


def set_current_session_id(session_id: str) -> None:
    """Set current session ID (called from SessionStart hook or keyword_router).

    [AC-6] Write is filelock-protected so concurrent readers see either the
    old value or the new one, never a torn partial.
    """
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _sid_lock():
            _CURRENT_SESSION_FILE.write_text(session_id, encoding="utf-8")
    except OSError:
        pass
    except Exception:
        # filelock Timeout — degrade to best-effort unlocked write
        try:
            _CURRENT_SESSION_FILE.write_text(session_id, encoding="utf-8")
        except OSError:
            pass


def _log_primed_session(pattern_ids: List[str]) -> None:
    """Append primed pattern IDs with session_id + timestamp to session log."""
    if not pattern_ids:
        return
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        sid = get_current_session_id()
        now = datetime.now().isoformat()
        with open(_PRIMED_SESSION_FILE, "a", encoding="utf-8") as f:
            for pid in pattern_ids:
                f.write(json.dumps({
                    "session_id": sid, "pattern_id": pid, "timestamp": now,
                }, ensure_ascii=False) + "\n")
    except OSError:
        pass  # Non-blocking


def _read_primed_for_session(session_id: str, max_age_sec: int = 0) -> List[str]:
    """Read primed pattern IDs for a given session_id.

    If max_age_sec > 0, only return IDs primed within that window from now.
    """
    if not _PRIMED_SESSION_FILE.exists():
        return []
    ids = []
    cutoff = None
    if max_age_sec > 0:
        cutoff = datetime.now().timestamp() - max_age_sec
    try:
        for line in _PRIMED_SESSION_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if entry.get("session_id") != session_id:
                    continue
                if cutoff is not None:
                    ts = datetime.fromisoformat(entry.get("timestamp", "")).timestamp()
                    if ts < cutoff:
                        continue
                pid = entry.get("pattern_id")
                if pid:
                    ids.append(pid)
            except (json.JSONDecodeError, ValueError):
                continue
    except OSError:
        return []
    # Dedup preserving order
    seen = set()
    return [pid for pid in ids if not (pid in seen or seen.add(pid))]


def _remove_primed_for_session(session_id: str) -> None:
    """Remove session's entries from primed_patterns_session.jsonl (mark processed)."""
    if not _PRIMED_SESSION_FILE.exists():
        return
    try:
        kept = []
        for line in _PRIMED_SESSION_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if entry.get("session_id") != session_id:
                    kept.append(line)
            except json.JSONDecodeError:
                kept.append(line)
        _PRIMED_SESSION_FILE.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    except OSError:
        pass


# ── Feedback write API [V-1, V-2] ──

_rewrite_lock = None  # lazy-init threading.Lock for in-process serialization


def _atomic_rewrite_patterns(mutator) -> int:
    """Atomically rewrite the patterns file using a mutator(dict)->dict fn.

    Concurrency strategy [SEC-1, SEC-2]:
      - threading.Lock for in-process serialization (prevents simultaneous rewrite)
      - filelock.FileLock for cross-process serialization (hook subprocess safety)
      - try/finally around tempfile lifecycle — orphans cleaned on any exit

    [LOG-R1-007 2026-04-20] Windows atomicity guard: Path.replace can fail
    silently (or raise PermissionError) when another process holds the target
    for reading. Previously the callers swallowed the exception and believed
    the mutation succeeded. We now retry up to 3 times (50ms backoff) on
    PermissionError/OSError and raise IOError if every attempt fails so the
    caller sees the failure instead of reporting a false-positive mutation count.
    """
    if not _PATTERNS_FILE.exists():
        return 0
    import tempfile
    import threading
    global _rewrite_lock
    if _rewrite_lock is None:
        _rewrite_lock = threading.Lock()

    # Acquire cross-process file lock (optional — filelock may not be installed)
    # LOCK-ORDER: 3 (global KB rewrite — innermost; never grab sid/credit lock after this)
    fl = None
    try:
        import filelock
        fl = filelock.FileLock(str(_PATTERNS_FILE) + ".lock", timeout=10)
    except Exception:
        fl = None

    with _rewrite_lock:
        if fl is not None:
            try:
                fl.acquire()
            except Exception:
                fl = None

        tmp_path = None
        try:
            lines = _PATTERNS_FILE.read_text(encoding="utf-8").splitlines()
            out_lines = []
            mutated = 0
            for line in lines:
                if not line.strip():
                    out_lines.append(line)
                    continue
                try:
                    data = json.loads(line)
                    # [Schema v2 2026-04-19 T5] Upgrade fields in-place during
                    # rewrite so persisted records match schema v2. This is the
                    # only place where lazy-read upgrades get flushed to disk,
                    # and it happens only when a mutator touches the record.
                    sv = data.get("schema_version")
                    if sv is None or sv == 1:
                        data.setdefault("source", "human_turn_historical")
                        data.setdefault("promotion_status", "draft")
                        data.setdefault("parent_pattern_id", None)
                        data.setdefault("last_hit_ts", None)
                        data["schema_version"] = 2
                    # A3 (2026-04-21): v2 -> v3 on-disk mutator upgrade.
                    # When any mutator rewrites a record, backfill v3 fields
                    # and bump schema_version. Readers of pre-upgrade files
                    # receive defaults via the read-side lazy block below.
                    if data.get("schema_version", 2) <= 2:
                        data.setdefault("canonical_tags", [])
                        data.setdefault("fingerprint", "")
                        data["schema_version"] = 3
                    new_data = mutator(data)
                    if new_data is not data:
                        # [LOGIC-MED Round2 fix 2026-04-18] Strip _mutated
                        # flag even when mutator returned a different dict,
                        # preventing JSONL pollution from future mutators.
                        if isinstance(new_data, dict):
                            new_data.pop("_mutated", None)
                        mutated += 1
                    elif new_data is not None and new_data.get("_mutated"):
                        new_data.pop("_mutated", None)
                        mutated += 1
                    out_lines.append(json.dumps(new_data, ensure_ascii=False))
                except json.JSONDecodeError:
                    out_lines.append(line)
            if mutated > 0:
                tmp_fd, tmp_path = tempfile.mkstemp(
                    dir=str(_DATA_DIR), suffix=".jsonl.tmp",
                )
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    f.write("\n".join(out_lines) + "\n")
                # [LOG-R1-007 2026-04-20] os.replace is the POSIX atomic rename
                # on both POSIX and Windows (unlike Path.replace before 3.8
                # MoveFileEx semantics, os.replace consistently uses
                # ReplaceFile/MoveFileExW). Retry loop handles the Windows
                # race where a reader briefly holds the target.
                import time as _time
                last_exc = None
                renamed = False
                for attempt in range(3):
                    try:
                        os.replace(str(tmp_path), str(_PATTERNS_FILE))
                        renamed = True
                        break
                    except (PermissionError, OSError) as exc:
                        last_exc = exc
                        _time.sleep(0.05)
                if not renamed:
                    # Leave tmp_path set so finally cleans it, then surface
                    # the failure rather than silently returning success.
                    raise IOError(
                        f"atomic rewrite of {_PATTERNS_FILE} failed after 3 "
                        f"attempts: {last_exc!r}"
                    )
                tmp_path = None  # renamed successfully
                # [COV-CLOSEOUT 2026-04-20] Invalidate cache so next read
                # doesn't return stale data when mtime didn't advance past
                # the previous read (Windows filesystem clock-tick race
                # in fast enqueue -> promote sequences).
                global _patterns_cache
                _patterns_cache = (0.0, [])
            return mutated
        finally:
            # Cleanup orphan tempfile on any failure path
            if tmp_path is not None:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except OSError:
                    pass
            if fl is not None:
                try:
                    fl.release()
                except Exception:
                    pass


# LOGIC-R1-01 fix (2026-04-21): first copy of `record_pattern_helpful`
# deleted. An identical-signature duplicate lived at L1220 which was
# shadowing this one; all callers now resolve to the unified version
# below. Both defs were semantically equivalent (helpful_count+=1 under
# _atomic_rewrite_patterns mutator); only the default `reason` string and
# the provenance dict shape differed cosmetically. The L1220 version is
# the authoritative one since 2026-04-18 logic-review-fix commit.


def record_pattern_outdated(
    pattern_ids: List[str],
    reason: str,
    correction_text: str = "",
) -> int:
    """Increment outdated_reports + auto-downgrade at threshold. Audit logged.

    [V-2] Logs every demotion to correction_provenance.jsonl for CEO audit.
    """
    if not pattern_ids:
        return 0
    ids_set = set(pattern_ids)
    now = datetime.now().isoformat()
    sid = get_current_session_id()
    pre_conf_map = {}

    def mutator(data):
        pid = data.get("id")
        if pid in ids_set:
            pre_conf_map[pid] = data.get("confidence", "medium")
            new_count = int(data.get("outdated_reports", 0)) + 1
            data["outdated_reports"] = new_count
            data["updated_at"] = now
            # Auto-downgrade at 3 strikes (only downgrade, never delete)
            if new_count >= 3 and data.get("confidence") != "low":
                data["confidence"] = "low"
            data["_mutated"] = True
        return data

    try:
        mutated = _atomic_rewrite_patterns(mutator)
    except Exception:
        return 0

    # Audit trail: one provenance entry per affected pattern [SEC-3]
    if mutated > 0:
        sanitized_corr = _sanitize_correction_text(correction_text, max_len=200)
        try:
            with open(_CORRECTION_PROV_FILE, "a", encoding="utf-8") as f:
                for pid in pattern_ids:
                    if pid in pre_conf_map:
                        # ensure_ascii=True: direction-override and C1 chars already
                        # stripped by _sanitize_correction_text; this is belt+suspenders
                        f.write(json.dumps({
                            "timestamp": now,
                            "session_id": sid,
                            "correction_text": sanitized_corr,
                            "demoted_pattern_id": pid,
                            "reason": reason,
                            "pre_confidence": pre_conf_map[pid],
                        }, ensure_ascii=True) + "\n")
        except OSError:
            pass
    return mutated


def revert_demotion(pattern_id: str) -> bool:
    """CEO-reversible: reset outdated_reports=0, restore pre_confidence.

    Reads first correction_provenance entry for the pattern to find pre_confidence.
    """
    if not pattern_id or not _CORRECTION_PROV_FILE.exists():
        return False
    # Find pre_confidence from first provenance entry
    pre_conf = None
    try:
        for line in _CORRECTION_PROV_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if entry.get("demoted_pattern_id") == pattern_id:
                    pre_conf = entry.get("pre_confidence", "medium")
                    break
            except json.JSONDecodeError:
                continue
    except OSError:
        return False
    if pre_conf is None:
        return False

    now = datetime.now().isoformat()

    def mutator(data):
        if data.get("id") == pattern_id:
            data["outdated_reports"] = 0
            data["confidence"] = pre_conf
            data["updated_at"] = now
            data["_mutated"] = True
        return data

    try:
        mutated = _atomic_rewrite_patterns(mutator)
    except Exception:
        return False
    # Audit: log reversal
    try:
        with open(_CORRECTION_PROV_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": now,
                "reverted_pattern_id": pattern_id,
                "restored_confidence": pre_conf,
                "action": "ceo_reversal",
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return mutated > 0


def _tokenize_mixed(text: str) -> set:
    """[COV-3] Tokenize Latin + CJK text.

    - Latin/numeric: whole-word tokens via \\b regex, ≥3 chars, not stopwords
    - CJK (U+4E00-U+9FFF): individual Han chars as tokens (character-based
      indexing is the standard approach for Chinese without a full segmenter)
    """
    import re as _re
    tokens = set()
    # Latin words
    for tok in _re.findall(r"\b[\w]+\b", text.lower()):
        # Skip pure-digit tokens and stopwords and 1-2 char tokens
        if len(tok) >= 3 and tok not in _STOPWORDS and not tok.isdigit():
            tokens.add(tok)
    # CJK: every Han character is a "token" (each carries semantic weight)
    for c in text:
        if "\u4e00" <= c <= "\u9fff":
            tokens.add(c)
    return tokens


def find_patterns_for_correction(
    correction_text: str,
    min_overlap: int = 5,
    max_age_sec: int = 3600,
) -> List[str]:
    """Find recently-primed patterns matching a correction text.

    [V-2 + COV-3] Stricter matching: ≥5 meaningful token overlap,
    primed within last hour. Supports Latin words + CJK per-char tokens.

    Returns list of pattern IDs to be reported as outdated.
    """
    if not correction_text or len(correction_text) < 10:
        return []
    sid = get_current_session_id()
    recent_ids = set(_read_primed_for_session(sid, max_age_sec=max_age_sec))
    if not recent_ids:
        return []
    corr_tokens = _tokenize_mixed(correction_text)
    if len(corr_tokens) < min_overlap:
        return []
    all_p = load_all_patterns()
    id_to_entry = {e.id: e for e in all_p}
    matched = []
    for pid in recent_ids:
        entry = id_to_entry.get(pid)
        if not entry:
            continue
        pat_text = (entry.fact + " " + entry.recommendation)
        pat_tokens = _tokenize_mixed(pat_text)
        overlap = corr_tokens & pat_tokens
        if len(overlap) >= min_overlap:
            matched.append(pid)
    return matched


def load_all_patterns() -> List[PatternEntry]:
    """加载所有 pattern (mtime-cached). 自动降级过期条目"""
    global _patterns_cache
    if not _PATTERNS_FILE.exists():
        return []

    # mtime-based cache: skip read if file unchanged
    try:
        mtime = _PATTERNS_FILE.stat().st_mtime
        if mtime == _patterns_cache[0] and _patterns_cache[1]:
            return _patterns_cache[1]
    except OSError:
        pass

    entries = []
    now = datetime.now()
    for line in _PATTERNS_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                data = json.loads(line)
                # [Schema v2 2026-04-19 T5] Lazy v1 -> v2 migration (in-memory only).
                # If schema_version is missing or 1, fill v2 defaults marking this
                # record as historical. Disk write is NOT triggered here -- callers
                # who rewrite (save_patterns / _atomic_rewrite_patterns) will persist
                # v2 naturally via asdict(). Backfill script handles bulk conversion.
                sv = data.get("schema_version")
                if sv is None or sv == 1:
                    data.setdefault("source", "human_turn_historical")
                    data.setdefault("promotion_status", "draft")
                    data.setdefault("parent_pattern_id", None)
                    data.setdefault("last_hit_ts", None)
                    data["schema_version"] = 2
                # A3 (2026-04-21): v2 read-side lazy backfill for v3 fields.
                # Do NOT bump schema_version here — on-disk stays v2 until a
                # mutator rewrites (§L647-664 handles that transition).
                data.setdefault("canonical_tags", [])
                data.setdefault("fingerprint", "")
                entry = PatternEntry(**{
                    k: v for k, v in data.items()
                    if k in PatternEntry.__dataclass_fields__
                })

                # TTL 过期检查: 超过 30 天未使用且 usage_count==0 则降级
                # F-DEBUG fix (2026-04-20 autopilot): TZ-aware created_at used
                # to raise TypeError here (offset-naive vs offset-aware), which
                # the OUTER except(TypeError) caught — silently dropping the
                # entire entry. Coerce to naive for arithmetic.
                try:
                    created_raw = entry.created_at.replace("Z", "")
                    created = datetime.fromisoformat(created_raw)
                    if created.tzinfo is not None:
                        created = created.replace(tzinfo=None)
                    age_days = (now - created).days
                    if age_days > _TTL_DAYS and entry.usage_count == 0 and entry.confidence != "low":
                        entry.confidence = "low"  # 降级但不删除
                except (ValueError, AttributeError, TypeError):
                    pass

                entries.append(entry)
            except (json.JSONDecodeError, TypeError):
                pass

    # Update cache
    try:
        _patterns_cache = (_PATTERNS_FILE.stat().st_mtime, entries)
    except OSError:
        pass
    return entries


def _increment_usage(matched_ids: List[str]) -> None:
    """增加匹配 pattern 的 usage_count 并更新 last_primed,持久化 (atomic+locked).

    [L5 Phase 1 2026-04-18] 同时写 last_primed = now (ISO-8601 UTC).
    [LOGIC-HIGH Round1 fix] Uses _atomic_rewrite_patterns() for lock protection.
    """
    if not matched_ids or not _PATTERNS_FILE.exists():
        return
    id_set = set(matched_ids)
    now_iso = datetime.utcnow().isoformat() + "Z"
    def _mutator(data: dict) -> dict:
        if data.get("id") in id_set:
            data["usage_count"] = data.get("usage_count", 0) + 1
            data["updated_at"] = datetime.now().isoformat()
            data["last_primed"] = now_iso
            # [Schema v2 T5] last_hit_ts mirrors last_primed on every prime hit;
            # kept distinct to allow future temporal-decay scoring independent
            # of legacy last_primed semantics.
            data["last_hit_ts"] = now_iso
            data["_mutated"] = True  # signal to _atomic_rewrite_patterns
        return data
    try:
        _atomic_rewrite_patterns(_mutator)
    except Exception:
        pass  # non-blocking


def mark_patterns_used(pattern_ids: List[str]) -> None:
    """[L5 Phase 1 2026-04-18] 当 primed 的 pattern 未被用户纠正时 (隐式确认),
    更新 last_used.

    [LOGIC-HIGH Round1 fix] Uses _atomic_rewrite_patterns for race safety.

    通常由 Stop hook 在会话结束时批量调用 (当无 correction 信号时).
    """
    if not pattern_ids or not _PATTERNS_FILE.exists():
        return
    id_set = set(pattern_ids)
    now_iso = datetime.utcnow().isoformat() + "Z"
    def _mutator(data: dict) -> dict:
        if data.get("id") in id_set:
            data["last_used"] = now_iso
            data["helpful_count"] = data.get("helpful_count", 0) + 1
            data["_mutated"] = True
        return data
    try:
        _atomic_rewrite_patterns(_mutator)
    except Exception:
        pass  # non-blocking


# [P3 2026-04-18 autopilot] Stronger "helpful" signal wiring
_PRIMED_LOG = _DATA_DIR / "primed_patterns_session.jsonl"
_MAX_HELPFUL_CREDIT_PER_SESSION = 10  # verifier MED on B7 risk: bound credit


def _to_naive_utc(t: datetime) -> datetime:
    """Coerce any datetime to naive UTC. Non-tz-aware inputs assumed already UTC."""
    from datetime import timezone as _tz
    if t.tzinfo is not None:
        return t.astimezone(_tz.utc).replace(tzinfo=None)
    return t


def get_session_primed_ids(since_ts: Optional[str] = None) -> List[str]:
    """Return pattern_ids primed in the current session tail of primed_patterns_session.jsonl.

    [Logic-review fix 2026-04-18]
      - timezone: both cutoff and record timestamps normalized via _to_naive_utc
        (uses .astimezone(UTC) first; prior impl dropped tzinfo blindly).
      - cutoff break -> continue (reversed tail may contain out-of-order entries;
        break would drop legitimately recent ids after one stale entry).

    Args:
        since_ts: ISO-8601 cutoff. Default: last 12 hours.

    Returns up to _MAX_HELPFUL_CREDIT_PER_SESSION distinct ids (most recent first).
    """
    from datetime import timedelta
    if not _PRIMED_LOG.exists():
        return []
    try:
        if since_ts:
            cutoff_dt = datetime.fromisoformat(since_ts.replace("Z", "+00:00"))
            cutoff = _to_naive_utc(cutoff_dt)
        else:
            cutoff = datetime.utcnow() - timedelta(hours=12)
    except Exception:
        cutoff = datetime.utcnow() - timedelta(hours=12)

    ids: List[str] = []
    seen = set()
    try:
        with open(_PRIMED_LOG, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-5000:]
        for line in reversed(tail):  # most recent first
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
                pid = ev.get("pattern_id")
                if not pid or pid in seen:
                    continue
                ts_raw = ev.get("timestamp", "")
                try:
                    t = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    t_naive = _to_naive_utc(t)
                    if t_naive < cutoff:
                        continue  # out-of-order stale entry; keep scanning
                except Exception:
                    pass  # missing/bad timestamp: count it anyway
                ids.append(pid)
                seen.add(pid)
                if len(ids) >= _MAX_HELPFUL_CREDIT_PER_SESSION:
                    break
            except Exception:
                continue
    except Exception:
        return []
    return ids


def record_pattern_helpful(pattern_ids: List[str], reason: str = "autopilot_complete") -> int:
    """[Logic-review fix 2026-04-18] Dedicated helpful_count bump, NO last_used touch.

    Distinct from mark_patterns_used (which also sets last_used). This is the
    "explicit success" signal called by credit_session_helpful. Returns number
    of patterns actually mutated (checked via _atomic_rewrite_patterns return).

    Args:
        pattern_ids: ids to credit
        reason: label stored in provenance (e.g. "session_completed",
                "autopilot_complete", "test_credit")

    Provenance semantic: appends {"source": "outcome_feedback", "reason": reason,
    "date": ISO-8601} to each mutated pattern's provenance list.
    """
    if not pattern_ids or not _PATTERNS_FILE.exists():
        return 0
    id_set = set(pattern_ids)
    now_iso = datetime.utcnow().isoformat() + "Z"
    prov_entry = {
        "source": "outcome_feedback",
        "reason": str(reason)[:80],
        "date": now_iso,
    }
    def _mutator(data: dict) -> dict:
        if data.get("id") in id_set:
            data["helpful_count"] = data.get("helpful_count", 0) + 1
            prov = data.get("provenance") or []
            if isinstance(prov, list):
                prov.append(prov_entry)
                data["provenance"] = prov
            data["_mutated"] = True
        return data
    try:
        n = _atomic_rewrite_patterns(_mutator)
        return n if isinstance(n, int) else 0
    except Exception:
        return 0


def credit_session_helpful() -> int:
    """Wired into persistent_mode.mark_completed() on successful autopilot close.

    [Logic-review fix 2026-04-18]
      - Calls record_pattern_helpful (dedicated path), NOT mark_patterns_used.
        This prevents double-bump when both paths fire in one session.
      - Returns actual mutation count (not len(ids)), so silent write failure
        no longer lies to the caller about success.

    TU-4 (learning_pip 2026-04-30): also writes data/last_credit.json transparency.
    """
    ids = get_session_primed_ids()
    if not ids:
        _write_transparency_credit(0, [])
        return 0
    n = record_pattern_helpful(ids)
    _write_transparency_credit(n, ids)
    return n


def _write_transparency_credit(count_credited: int, pattern_ids: list) -> None:
    """TU-4 (learning_pip 2026-04-30): emit data/last_credit.json for transparency.

    Schema: {ts, action: "credit", count_credited, pattern_ids}.
    Fail-soft: never raises.
    """
    try:
        path = _DATA_DIR / "last_credit.json"
        payload = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "action": "credit",
            "count_credited": int(count_credited),
            "pattern_ids": [str(i)[:120] for i in (pattern_ids or [])],
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            from src.core.trace_sink import write_trace_event
            write_trace_event("transparency_emit_done", {
                "file_name": "last_credit.json",
                "action": "credit",
                "count": int(count_credited),
            })
        except Exception:
            pass
    except Exception:
        pass


# ── 检索 (Prime) ──

def prime(
    files: Optional[List[str]] = None,
    keywords: Optional[List[str]] = None,
    work_type: Optional[str] = None,
    limit: int = 10,
    query_text: Optional[str] = None,
    write_usage_count: bool = True,
) -> List[PatternEntry]:
    """
    检索与当前任务相关的 pattern (对标 metaswarm /prime 命令)。

    Args:
        files: 当前涉及的文件路径 (glob 匹配)
        keywords: 关键词列表
        work_type: 工作类型 (implementation, testing, debugging, review)
        limit: 最大返回条数

    Returns:
        按相关性排序的 PatternEntry 列表
    """
    all_patterns = load_all_patterns()
    if not all_patterns:
        return []

    scored: List[tuple] = []  # (score, file_match_count, entry)

    # [V-6 + LOGIC-M5] Self-reference suppression: skip auto_generated
    # meta-patterns when the query is itself in the subagent/meta domain.
    # Use regex word boundaries to avoid substring collision (meta vs metadata/metaclass).
    import re as _re
    q_text_lower = " ".join((keywords or []) + [query_text or "", (work_type or "")]).lower()
    suppress_auto = bool(_re.search(r"\b(subagent|agent_output|meta)\b", q_text_lower))

    for entry in all_patterns:
        # [V-6] Skip auto_generated meta-patterns in self-referential queries
        if suppress_auto and getattr(entry, "auto_generated", False):
            continue

        score = 0.0
        file_match_count = 0

        # [Item #9a + LOGIC-M6] 文件匹配权重提升: 精确 glob 匹配每次 +5.0
        # Directory-match capped at +1.0 TOTAL per user_file (was per pattern_file,
        # allowing runaway +10.0 when pattern has 10 files in same dir).
        if files:
            for user_file in files:
                user_parent = Path(user_file).parent
                dir_match_counted = False
                for pattern_file in entry.affected_files:
                    if fnmatch(user_file, pattern_file) or fnmatch(pattern_file, user_file):
                        score += 5.0
                        file_match_count += 1
                        break
                    # 部分匹配 (同目录): once per user_file, not per pattern_file
                    if not dir_match_counted and user_parent == Path(pattern_file).parent:
                        score += 1.0
                        dir_match_counted = True

        # 关键词匹配
        if keywords:
            text = f"{entry.fact} {entry.recommendation} {' '.join(entry.tags)}"
            for kw in keywords:
                if kw.lower() in text.lower():
                    score += 2.0

        # 工作类型匹配
        if work_type:
            type_tag_map = {
                "implementation": ["pattern", "decision", "code_quirk"],
                "testing": ["gotcha", "anti_pattern"],
                "debugging": ["code_quirk", "gotcha", "performance"],
                "review": ["anti_pattern", "security", "performance"],
            }
            if entry.type in type_tag_map.get(work_type, []):
                score += 1.5

        # 置信度加权
        confidence_weight = {"high": 1.5, "medium": 1.0, "low": 0.5}
        score *= confidence_weight.get(entry.confidence, 1.0)

        # 使用频率加权
        score += min(entry.usage_count * 0.1, 1.0)

        # [Item #1] helpful_count 加权: 历史验证过有效的 pattern 权重更高
        score += min(getattr(entry, "helpful_count", 0) * 0.3, 2.0)

        # [Item #1] outdated_reports 软抑制: 用户反复反驳的 pattern 权重下降
        outdated = getattr(entry, "outdated_reports", 0)
        if outdated > 0:
            score *= max(0.2, 1.0 - outdated * 0.15)

        if score > 0:
            scored.append((score, file_match_count, entry))

    # 按分数降序排序, 文件匹配数作为 tiebreak
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

    results = [entry for _, _, entry in scored[:limit]]

    # 自动追踪 usage_count (prime 命中 = 被使用)
    # plan_v1 (2026-04-23) M2 C1: gate-side (pretool) prime calls set
    # write_usage_count=False to avoid hot-path disk write + lock contention.
    if results and write_usage_count:
        matched_ids = [e.id for e in results]
        _increment_usage(matched_ids)
        # [V-3] Also log to session-level primed file for helpful_count feedback
        _log_primed_session(matched_ids)

    return results


# ── Hybrid smart_prime (semantic + keyword) ──

# Lazy import to avoid circular dependency; falls back gracefully if unavailable
def _try_import_hybrid_prime():
    """Import hybrid_prime from semantic_search, returning None if unavailable."""
    try:
        from .semantic_search import hybrid_prime  # noqa: PLC0415
        return hybrid_prime
    except Exception:
        return None


def smart_prime(
    files: Optional[List[str]] = None,
    keywords: Optional[List[str]] = None,
    work_type: Optional[str] = None,
    limit: int = 10,
    query_text: Optional[str] = None,
    use_semantic: bool = True,
) -> List[PatternEntry]:
    """
    Smart retrieval combining keyword and semantic search.

    [L4 Phase 3 2026-04-18]: add bge-small-zh semantic fallback when
    keywords or query_text are provided. Degrades gracefully if
    semantic_kb unavailable (校园网 block / model missing).

    Args:
        files: File paths for glob matching
        keywords: Keywords for text matching
        work_type: Work type for type-based scoring
        limit: Maximum results to return
        query_text: Optional free-text query for semantic search
        use_semantic: Include bge embedding semantic hits (default True)

    Returns:
        List of PatternEntry sorted by relevance. Semantic hits deduplicated
        by id against keyword hits; semantic-only hits appended at end.
    """
    # Path 1: existing hybrid_prime (TF-IDF/BOW) wins if available
    hybrid_prime_fn = _try_import_hybrid_prime()
    keyword_results: List[PatternEntry] = []
    if hybrid_prime_fn is not None:
        try:
            keyword_results = hybrid_prime_fn(
                files=files,
                keywords=keywords,
                work_type=work_type,
                query_text=query_text,
                limit=limit,
            )
        except Exception:
            keyword_results = []
    if not keyword_results:
        keyword_results = prime(
            files=files, keywords=keywords,
            work_type=work_type, limit=limit, query_text=query_text,
        )

    # Path 2: [L4] semantic fallback — only if query text exists and flag on
    if not use_semantic:
        return keyword_results
    semantic_query = query_text or (" ".join(keywords) if keywords else "")
    if not semantic_query.strip():
        return keyword_results
    try:
        from src.core.semantic_kb import semantic_search, is_enabled as sem_on
        if not sem_on():
            return keyword_results
        sem_hits = semantic_search(semantic_query, top_k=5, min_score=0.4)
        if not sem_hits:
            return keyword_results
        # Merge: dedup by id, keyword results keep rank, semantic-only appended
        existing_ids = {e.id for e in keyword_results}
        sem_id_set = {pid for pid, _ in sem_hits if pid not in existing_ids}
        if sem_id_set:
            all_patterns = load_all_patterns()
            id_map = {p.id: p for p in all_patterns}
            extra = [id_map[pid] for pid, _ in sem_hits
                     if pid in sem_id_set and pid in id_map]
            # Track these for last_primed
            if extra:
                _increment_usage([e.id for e in extra])
            return (keyword_results + extra)[:limit]
    except Exception:
        pass  # silent degrade
    return keyword_results


def format_prime_output(entries: List[PatternEntry]) -> str:
    """格式化 prime 输出，按 metaswarm 优先级分层"""
    if not entries:
        return "No relevant patterns found in knowledge base."

    # 分层: MUST_FOLLOW > GOTCHAS > PATTERNS > DECISIONS
    must_follow = [e for e in entries if e.confidence == "high"]
    gotchas = [e for e in entries if e.type in ("gotcha", "anti_pattern")]
    patterns = [e for e in entries if e.type == "pattern" and e.confidence != "high"]
    decisions = [e for e in entries if e.type == "decision"]

    lines = ["## Knowledge Base Priming\n"]

    if must_follow:
        lines.append("### MUST FOLLOW (high confidence)")
        for e in must_follow:
            lines.append(f"- **{e.fact[:120]}**")
            if e.recommendation:
                lines.append(f"  Recommendation: {e.recommendation[:200]}")

    if gotchas:
        lines.append("\n### GOTCHAS")
        for e in gotchas:
            lines.append(f"- {e.fact[:120]}")

    if patterns:
        lines.append("\n### PATTERNS")
        for e in patterns:
            lines.append(f"- {e.fact[:120]}")

    if decisions:
        lines.append("\n### DECISIONS")
        for e in decisions:
            lines.append(f"- {e.fact[:120]}")

    return "\n".join(lines)


# ── Session Learning (post-autopilot) ──

def _extract_from_task_dir(task_id: Optional[str]) -> List[PatternEntry]:
    """TU-2 (learning_pip 2026-04-30): Path B reader for autopilot v2.0 review findings.

    Reads `<task_dir>/review_findings/<role>_iter<N>.json` (autopilot v2.0 §4.2 standard).
    Closes writer/reader path drift: extract_from_session previously only read Path A
    (`memex/memex/data/last_review*.json`), missing all autopilot-class session output.

    Returns list of PatternEntry, or [] if task_id None / dir missing / no findings.
    Per HARD RULE feedback_writer_reader_schema_contract.
    """
    if not task_id:
        return []
    try:
        from src.core.task_dir_layout import task_dir as _tdir
        td = _tdir(task_id)
    except Exception:
        return []
    rf_dir = td / "review_findings"
    if not rf_dir.is_dir():
        return []
    out: List[PatternEntry] = []
    files_found = 0
    for path in sorted(rf_dir.glob("*_iter*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        findings = data.get("findings") or []
        if not isinstance(findings, list) or not findings:
            continue
        files_found += 1
        try:
            entries = extract_pattern_from_review(
                json.dumps(findings, ensure_ascii=False),
                source=f"task_dir_review/{path.parent.parent.name}",
                reference=f"review_findings/{path.name}",
            )
            out.extend(entries)
        except Exception:
            continue
    # Emit Path B trace (best-effort)
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("pattern_extract_path_b_used", {
            "task_id": task_id, "files_found": files_found,
            "entries_extracted": len(out),
        })
    except Exception:
        pass
    return out


def extract_from_session(max_patterns: int = 5,
                         task_id: Optional[str] = None) -> int:
    """Extract learnings from the current autopilot session.

    Scans recent artifact files (review findings, verifier concerns, etc.)
    and extracts the most valuable patterns into the JSONL knowledge base.

    TU-2 (learning_pip 2026-04-30): added optional `task_id` for Path B
    (read `task_dir/review_findings/*.json` per autopilot v2.0). Path A
    (legacy `last_review*.json`) preserved for backward-compat. Dedup by
    fact[:80] when finding_id absent.

    Bounded: max_patterns limits output to prevent KB bloat.

    Returns:
        Number of new patterns added.
    """
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    all_entries: List[PatternEntry] = []

    # Source 1: Review findings from last_review_*.json files
    review_files = list(_DATA_DIR.glob("last_review*.json"))
    for rf in review_files:
        try:
            data = json.loads(rf.read_text(encoding="utf-8"))
            findings = data.get("findings") or data.get("issues") or []
            if isinstance(findings, list) and findings:
                entries = extract_pattern_from_review(
                    json.dumps(findings, ensure_ascii=False),
                    source="session_review",
                    reference=rf.name,
                )
                all_entries.extend(entries)
        except Exception:
            pass

    # Source 2: Verifier concerns from last_verifier.json
    verifier_file = _DATA_DIR / "last_verifier.json"
    if verifier_file.exists():
        try:
            data = json.loads(verifier_file.read_text(encoding="utf-8"))
            concerns = data.get("concerns", [])
            if concerns:
                for concern in concerns:
                    text = concern if isinstance(concern, str) else str(concern)
                    if len(text) >= 20:
                        all_entries.append(PatternEntry(
                            type="gotcha",
                            fact=text[:500],
                            recommendation="Verifier raised this concern during plan review.",
                            confidence="medium",
                            tags=["session_verifier", "plan_review"],
                            provenance=[asdict(Provenance(
                                source="verifier",
                                reference="last_verifier.json",
                                date=datetime.now().isoformat(),
                            ))],
                        ))
        except Exception:
            pass

    # Source 3: Security scan findings from last_review_security.json
    security_file = _DATA_DIR / "last_review_security.json"
    if security_file.exists():
        try:
            data = json.loads(security_file.read_text(encoding="utf-8"))
            findings = data.get("findings", [])
            if isinstance(findings, list) and findings:
                entries = extract_pattern_from_review(
                    json.dumps(findings, ensure_ascii=False),
                    source="security_scan",
                    reference="last_security_scan.json",
                )
                all_entries.extend(entries)
        except Exception:
            pass

    # TU-2 Path B: also read task_dir/review_findings/ if task_id provided
    try:
        path_b_entries = _extract_from_task_dir(task_id)
        if path_b_entries:
            all_entries.extend(path_b_entries)
    except Exception:
        pass

    if not all_entries:
        # TU-4 transparency: emit empty extract record so consumers see "ran but nothing"
        _write_transparency_extract(0, [])
        return 0

    # Quality filter: deduplicate, sort by confidence, take top N
    seen = set()
    unique = []
    for e in all_entries:
        key = e.fact[:80]
        if key not in seen:
            seen.add(key)
            unique.append(e)

    # Sort: high confidence first, then by fact length (longer = more specific)
    conf_order = {"high": 0, "medium": 1, "low": 2}
    unique.sort(key=lambda e: (conf_order.get(e.confidence, 2), -len(e.fact)))

    # Bounded: only take top max_patterns
    to_save = unique[:max_patterns]
    added = save_patterns(to_save)
    # TU-4 transparency: list what was added (best-effort)
    try:
        ids = [getattr(e, "id", None) or e.fact[:60] for e in to_save]
        _write_transparency_extract(added, ids)
    except Exception:
        pass
    return added


def _write_transparency_extract(count_added: int, pattern_ids: list) -> None:
    """TU-4 (learning_pip 2026-04-30): emit data/last_extract.json for transparency.

    Schema: {ts, action: "extract", count_added, pattern_ids}.
    Fail-soft: never raises (transparency must not block the main learning path).
    """
    try:
        path = _DATA_DIR / "last_extract.json"
        payload = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "action": "extract",
            "count_added": int(count_added),
            "pattern_ids": [str(i)[:120] for i in (pattern_ids or [])],
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            from src.core.trace_sink import write_trace_event
            write_trace_event("transparency_emit_done", {
                "file_name": "last_extract.json",
                "action": "extract",
                "count": int(count_added),
            })
        except Exception:
            pass
    except Exception:
        pass


def prune_expired_patterns(ttl_days: int = 30) -> int:
    """Remove patterns that have been unused for ttl_days.

    Uses load_all_patterns() to apply in-memory TTL downgrades first,
    then prunes patterns that are:
    - created_at > ttl_days ago AND
    - usage_count == 0 AND
    - confidence == "low" (after in-memory downgrade)

    Rewrites the JSONL file with surviving patterns (flushes downgrades).

    Returns:
        Number of patterns removed.
    """
    if not _PATTERNS_FILE.exists():
        return 0

    # load_all_patterns applies TTL downgrades in memory
    all_entries = load_all_patterns()
    if not all_entries:
        return 0

    now = datetime.now()
    kept = []
    removed = 0

    for entry in all_entries:
        try:
            created = datetime.fromisoformat(entry.created_at.replace("Z", ""))
            age_days = (now - created).days
        except (ValueError, AttributeError):
            age_days = 0

        if age_days > ttl_days and entry.usage_count == 0 and entry.confidence == "low":
            removed += 1
            continue  # prune

        kept.append(entry)

    # Rewrite file with surviving entries (flushing any in-memory downgrades)
    if removed > 0 or any(e.confidence == "low" for e in kept):
        _PATTERNS_FILE.write_text(
            "\n".join(json.dumps(asdict(e), ensure_ascii=False) for e in kept) + "\n",
            encoding="utf-8",
        )

    return removed


# ── CLI 入口 ──

def main():
    """CLI: extract / prime / stats"""
    if len(sys.argv) < 2:
        print("Usage: python -m src.core.pattern_extractor [extract|prime|stats]")
        return

    cmd = sys.argv[1]

    if cmd == "extract":
        # 从 stdin 读取 findings
        source = "code_review"
        reference = ""
        for i, arg in enumerate(sys.argv[2:], 2):
            if arg == "--source" and i + 1 < len(sys.argv):
                source = sys.argv[i + 1]
            elif arg == "--reference" and i + 1 < len(sys.argv):
                reference = sys.argv[i + 1]

        findings = sys.stdin.read().strip()
        if not findings:
            print("No input provided on stdin", file=sys.stderr)
            return

        entries = extract_pattern_from_review(findings, source, reference)
        added = save_patterns(entries)
        print(f"Extracted {len(entries)} candidates, added {added} new patterns to knowledge base")

    elif cmd == "prime":
        files = []
        keywords = []
        work_type = None
        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--files" and i + 1 < len(sys.argv):
                files = sys.argv[i + 1].split(",")
                i += 2
            elif sys.argv[i] == "--keywords" and i + 1 < len(sys.argv):
                keywords = sys.argv[i + 1].split(",")
                i += 2
            elif sys.argv[i] == "--work-type" and i + 1 < len(sys.argv):
                work_type = sys.argv[i + 1]
                i += 2
            else:
                i += 1

        results = prime(files=files or None, keywords=keywords or None, work_type=work_type)
        print(format_prime_output(results))

    elif cmd == "stats":
        entries = load_all_patterns()
        if not entries:
            print("Knowledge base is empty.")
            return
        by_type = {}
        by_confidence = {}
        for e in entries:
            by_type[e.type] = by_type.get(e.type, 0) + 1
            by_confidence[e.confidence] = by_confidence.get(e.confidence, 0) + 1

        print(f"Total patterns: {len(entries)}")
        print(f"By type: {json.dumps(by_type, ensure_ascii=False)}")
        print(f"By confidence: {json.dumps(by_confidence, ensure_ascii=False)}")

    elif cmd == "audit":
        # arch_v2 §F P5: audit lineage — find machine-chain patterns
        # whose human ancestor is beyond 2 hops.
        lineage_flag = "--lineage" in sys.argv
        entries = load_all_patterns()
        idx = {e.id: e for e in entries}
        machine_sources = {"auto_extracted", "autodream", "reviewer_finding"}
        human_sources = {"ceo_edit", "human_turn", "human_turn_historical",
                         "ceo_approved_promotion"}
        violations = []
        for e in entries:
            if getattr(e, "source", "") not in machine_sources:
                continue
            # Walk parent chain
            depth = 0
            cur = e
            seen = set()
            rooted = False
            while cur and cur.id not in seen and depth < 5:
                seen.add(cur.id)
                if getattr(cur, "source", "") in human_sources:
                    rooted = True
                    break
                parent_id = getattr(cur, "parent_pattern_id", None)
                if not parent_id:
                    break
                cur = idx.get(parent_id)
                depth += 1
            if not rooted and depth > 2:
                violations.append({
                    "id": e.id, "source": e.source,
                    "depth_walked": depth,
                    "tags": list(getattr(e, "tags", []))[:3],
                })
        print(f"Total patterns: {len(entries)}")
        print(f"Machine-rooted lineage violations (depth>2, no human): {len(violations)}")
        if lineage_flag:
            for v in violations[:20]:
                print(f"  {v}")
        if violations:
            sys.exit(1)

    elif cmd == "audit-entities":
        # A5 (2026-04-21): canonical_tags coverage + unknown mention audit.
        entries = load_all_patterns()
        if not entries:
            print("(no patterns)")
            sys.exit(0)
        with_canon = [e for e in entries if getattr(e, "canonical_tags", [])]
        coverage = len(with_canon) / len(entries) if entries else 0.0

        # Top canonical counts
        from collections import Counter as _Counter
        ct_counter: _Counter = _Counter()
        for e in entries:
            for ct in getattr(e, "canonical_tags", []):
                ct_counter[ct] += 1

        # Unknown entities log (if present)
        unknown_log_path = _DATA_DIR / "unknown_entities.log"
        unknown_counter: _Counter = _Counter()
        if unknown_log_path.exists():
            for line in unknown_log_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                line = line.strip()
                if line:
                    unknown_counter[line] += 1

        # Fingerprint collision monitor (Risk #10)
        fp_counter: _Counter = _Counter()
        for e in entries:
            fp = getattr(e, "fingerprint", "")
            if fp:
                fp_counter[fp] += 1
        fp_collisions = {fp: n for fp, n in fp_counter.items() if n > 1}

        print(f"Total patterns: {len(entries)}")
        print(f"Canonical-tags coverage: {len(with_canon)}/{len(entries)} = {coverage:.1%}")
        print(f"Top-20 canonical_tags counts:")
        for cid, n in ct_counter.most_common(20):
            print(f"  {cid:30s}  {n}")
        print(f"Top-20 unknown entity mentions:")
        for raw, n in unknown_counter.most_common(20):
            print(f"  {raw:40s}  {n}")
        print(f"Fingerprint-merge collisions "
              f"(fp seen on multiple patterns, unexpected after A7): "
              f"{len(fp_collisions)}")
        if fp_collisions:
            for fp, n in list(fp_collisions.items())[:5]:
                print(f"  {fp}  appears in {n} patterns")
        # Observational CLI; always exit 0.
        sys.exit(0)

    elif cmd == "delete":
        # arch_v2 §F P5: delete by id
        if len(sys.argv) < 3:
            print("usage: pattern_extractor delete <pattern_id>", file=sys.stderr)
            sys.exit(1)
        target_id = sys.argv[2]

        def _mutator(lst):
            return [e for e in lst if e.id != target_id]

        removed_before = len(load_all_patterns())
        _atomic_rewrite_patterns(_mutator)
        removed_after = len(load_all_patterns())
        deleted = removed_before - removed_after
        print(f"Deleted {deleted} pattern(s) with id={target_id}")
        if deleted == 0:
            sys.exit(1)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)


def extract_from_chat(chat_fact_iter, confidence_threshold: float = 0.85) -> list[dict]:
    """Extract improvement patterns from chat facts. RP-LOG schema: source='chat-derived', id field (NOT pattern_id).

    Triggers: text contains '应该' / '下次别' / '我觉得' / 'should ' / "don't"
    Returns list of dicts with id, source, topic, predicate, confidence, ts, helpful_count.
    """
    import time as _t
    patterns = []
    for fact in chat_fact_iter:
        topic = (fact.get('topic') or '')
        predicate = (fact.get('predicate') or '')
        text = topic + ' ' + predicate
        if not any(p in text for p in ['应该', '下次别', '我觉得', 'should ', "don't"]):
            continue
        conf = float(fact.get('confidence', 0.0) or 0.0)
        if conf < confidence_threshold:
            continue
        chat_hash = fact.get('chat_room_hash', '')
        patterns.append({
            "id": f"chat_{chat_hash[:8]}_{int(_t.time()*1000)}",
            "source": "chat-derived",
            "topic": topic,
            "predicate": predicate,
            "confidence": conf,
            "ts": _t.time(),
            "helpful_count": 0,
        })
    return patterns


if __name__ == "__main__":
    main()
