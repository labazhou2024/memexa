"""
autoDream — Memory consolidation engine with log rotation and knowledge compilation.
Triggered when sessions_since_last_dream >= 5.

5-stage pipeline:
  ORIENT: Analyze current memory state (patterns count, events stats, health)
  GATHER: Collect un-consolidated episodes from events.jsonl
  CONSOLIDATE: LLM-driven pattern extraction via semantic_memory.consolidate()
  COMPILE: Generate structured Markdown articles from episodes (Karpathy-style)
  PRUNE: apply_decay + rotate events log + reset dream counter

Knowledge compilation (inspired by Karpathy "LLM Knowledge Bases"):
  Episodes → LLM compile → Markdown articles with frontmatter + backlinks
  Stored in knowledge_base/articles/ with auto-maintained INDEX.md

Storage: data/dream_reports/{YYYY-MM-DD}_dream.json
         knowledge_base/articles/{YYYY-MM-DD}_{slug}.md
"""

import json
import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
_DREAM_DIR = _DATA_DIR / "dream_reports"
_KB_DIR = Path(__file__).parent.parent.parent / "knowledge_base"
_ARTICLES_DIR = _KB_DIR / "articles"

# Maximum dream reports to keep (rolling window)
MAX_DREAM_REPORTS = 30
# Minimum episodes needed to compile an article
MIN_EPISODES_FOR_ARTICLE = 3
# Maximum articles to keep (rolling window)
MAX_ARTICLES = 100


@dataclass
class DreamReport:
    """Result of one autoDream cycle."""
    timestamp: str
    stage_results: Dict[str, Any]
    patterns_before: int
    patterns_after: int
    patterns_added: int
    patterns_pruned: int
    episodes_processed: int
    health_score: float  # 0-1
    events_rotated: bool
    articles_compiled: int = 0
    success: bool = True
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


class AutoDream:
    """autoDream memory consolidation engine."""

    def __init__(self):
        self._data_dir = _DATA_DIR
        self._dream_dir = _DREAM_DIR

    def orient(self) -> Dict[str, Any]:
        """Stage 1: Analyze current memory state."""
        from .semantic_memory import get_semantic_memory
        from .event_bus import read_events, get_event_count

        sm = get_semantic_memory()
        events = read_events(last_n=500)

        # Count event types
        event_types: Dict[str, int] = {}
        for e in events:
            t = e.get("type", "unknown")
            event_types[t] = event_types.get(t, 0) + 1

        error_events = sum(
            1 for e in events
            if "fail" in e.get("type", "").lower()
            or "error" in e.get("type", "").lower()
        )

        return {
            "pattern_count": sm.pattern_count,
            "avg_confidence": sm.stats["avg_confidence"],
            "episodes_pending": sm._episodes_since_consolidation,
            "total_events": len(events),
            "total_events_on_disk": get_event_count(),
            "error_events": error_events,
            "error_rate": error_events / max(len(events), 1),
            "event_type_distribution": event_types,
        }

    def gather(self) -> List[Dict]:
        """Stage 2: Collect un-consolidated episodes from events.

        Filters to only meaningful episode types and deduplicates by task name.
        """
        from .event_bus import read_events

        events = read_events(last_n=500)

        # Extract episodes from relevant event types
        seen_tasks = set()
        episodes = []
        for e in events:
            etype = e.get("type", "")
            details = e.get("details", {})
            if etype in ("agent_complete", "episode_recorded", "review_result", "gate_result"):
                task = details.get("task", etype)

                # Dedup by task name
                if task in seen_tasks:
                    continue
                seen_tasks.add(task)

                episode = {
                    "task": task,
                    "output": details.get("output_summary", ""),
                    "score": details.get("score", details.get("verdict", 3)),
                    "agent": e.get("agent", "system"),
                }
                # Normalize score to int
                if isinstance(episode["score"], str):
                    episode["score"] = 4 if episode["score"] == "APPROVED" else 2
                episodes.append(episode)

        # Also extract patterns from recent git log
        git_episodes = self._extract_git_patterns()
        for ge in git_episodes:
            if ge["task"] not in seen_tasks:
                seen_tasks.add(ge["task"])
                episodes.append(ge)

        return episodes

    def _extract_git_patterns(self) -> List[Dict]:
        """Extract patterns from git commit messages."""
        memexa_dir = self._data_dir.parent.parent  # memexa/
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "-20", "--format=%s"],
                capture_output=True, text=True, cwd=str(memexa_dir), timeout=10,
            )
            if result.returncode != 0:
                return []
            commits = result.stdout.strip().splitlines()
            episodes = []
            for msg in commits:
                if any(kw in msg.lower() for kw in ["fix", "bug", "refactor", "improve", "add"]):
                    episodes.append({
                        "task": f"git commit: {msg}",
                        "output": msg,
                        "score": 4,
                        "agent": "git",
                    })
            return episodes
        except Exception:
            return []

    async def consolidate(self, episodes: List[Dict]) -> Dict[str, Any]:
        """Stage 3: LLM-driven consolidation."""
        from .semantic_memory import get_semantic_memory

        sm = get_semantic_memory()
        patterns_before = sm.pattern_count

        if not episodes:
            return {"new_patterns": [], "patterns_before": patterns_before, "patterns_after": patterns_before}

        new_ids = await sm.consolidate(episodes)

        return {
            "new_patterns": new_ids,
            "patterns_before": patterns_before,
            "patterns_after": sm.pattern_count,
        }

    def prune(self) -> Dict[str, Any]:
        """Stage 4: Reset counter + rotate events log + clean old reports.

        Note: apply_decay() is NOT called here because sm.consolidate()
        (called in Stage 3) already calls apply_decay() internally.
        """
        from .semantic_memory import get_semantic_memory
        from .auto_trigger import reset_dream_counter
        from .event_bus import rotate_events

        sm = get_semantic_memory()
        after = sm.pattern_count

        reset_dream_counter()

        # Rotate events log if needed
        rotated = rotate_events()

        # Clean old dream reports (keep only MAX_DREAM_REPORTS)
        self._clean_old_reports()

        return {
            "patterns_after_prune": after,
            "pruned": 0,  # pruning happened inside consolidate's apply_decay
            "events_rotated": rotated is not None,
            "events_archive": rotated,
        }

    async def run(self) -> DreamReport:
        """Execute full autoDream cycle: Orient -> Gather -> Consolidate -> Prune."""
        from .event_bus import log_event

        log_event("dream_start", agent="auto_dream")
        stages: Dict[str, Any] = {}

        try:
            # Stage 1: Orient
            orient_result = self.orient()
            stages["orient"] = orient_result
            patterns_before = orient_result["pattern_count"]

            # Stage 2: Gather
            episodes = self.gather()
            stages["gather"] = {"episodes_found": len(episodes)}

            # Stage 3: Consolidate
            consolidate_result = await self.consolidate(episodes)
            stages["consolidate"] = consolidate_result

            # Stage 3.5: Compile knowledge articles (Karpathy-style)
            compiled_articles = await self.compile_articles(episodes)
            stages["compile"] = {
                "articles_created": len(compiled_articles),
                "article_paths": [str(p.name) for p in compiled_articles],
            }

            # Stage 4: Prune + rotate
            prune_result = self.prune()
            stages["prune"] = prune_result

            patterns_after = prune_result["patterns_after_prune"]
            patterns_added = len(consolidate_result.get("new_patterns", []))
            patterns_pruned = prune_result["pruned"]

            # Health score: higher is better
            error_rate = orient_result.get("error_rate", 0)
            health = max(0.0, min(1.0, 1.0 - error_rate))

            report = DreamReport(
                timestamp=datetime.utcnow().isoformat() + "Z",
                stage_results=stages,
                patterns_before=patterns_before,
                patterns_after=patterns_after,
                patterns_added=patterns_added,
                patterns_pruned=patterns_pruned,
                episodes_processed=len(episodes),
                health_score=round(health, 3),
                events_rotated=prune_result.get("events_rotated", False),
                articles_compiled=len(compiled_articles),
                success=True,
            )

        except Exception as e:
            logger.error("autoDream failed: %s", e)
            report = DreamReport(
                timestamp=datetime.utcnow().isoformat() + "Z",
                stage_results=stages,
                patterns_before=0,
                patterns_after=0,
                patterns_added=0,
                patterns_pruned=0,
                episodes_processed=0,
                health_score=0.0,
                events_rotated=False,
                articles_compiled=0,
                success=False,
                error=str(e),
            )

        # Save report
        self._save_report(report)

        log_event("dream_complete", agent="auto_dream", details={
            "success": report.success,
            "patterns_added": report.patterns_added,
            "patterns_pruned": report.patterns_pruned,
            "episodes_processed": report.episodes_processed,
            "health_score": report.health_score,
            "events_rotated": report.events_rotated,
            "articles_compiled": report.articles_compiled,
        })

        return report

    async def compile_articles(self, episodes: List[Dict]) -> List[Path]:
        """Stage 3.5: Compile episodes into structured Markdown articles.

        Inspired by Karpathy "LLM Knowledge Bases" — compile raw execution
        episodes into encyclopedic articles with frontmatter, backlinks, and
        source traceability. Articles are stored in knowledge_base/articles/.

        Returns list of created article paths.
        """
        if len(episodes) < MIN_EPISODES_FOR_ARTICLE:
            logger.info("Skipping article compilation: only %d episodes (need %d)",
                       len(episodes), MIN_EPISODES_FOR_ARTICLE)
            return []

        from .llm_router import get_router, TaskType

        # Group episodes by theme for focused articles
        groups = self._group_episodes_by_theme(episodes)
        created = []

        for theme, theme_episodes in groups.items():
            if len(theme_episodes) < 2:
                continue

            article_path = await self._compile_single_article(theme, theme_episodes)
            if article_path:
                created.append(article_path)

        # Update the knowledge base index
        if created:
            self._update_kb_index()

        return created

    async def _compile_single_article(self, theme: str, episodes: List[Dict]) -> Optional[Path]:
        """Compile a group of related episodes into one Markdown article."""
        from .llm_router import get_router, TaskType

        episode_text = "\n".join(
            f"- [{e.get('agent', '?')}] (score={e.get('score', '?')}) {e.get('task', '')[:150]}\n"
            f"  Output: {e.get('output', '')[:200]}"
            for e in episodes[:15]
        )

        prompt = f"""You are a knowledge compiler. Analyze these {len(episodes)} execution episodes
about "{theme}" and compile them into a structured knowledge article.

## Episodes
{episode_text}

## Output Format
Write a concise Markdown article (200-500 words) that:
1. Synthesizes the key insights from these episodes into reusable knowledge
2. Identifies what worked well and what failed
3. Extracts actionable rules for future similar tasks
4. Notes any contradictions or open questions

Structure:
- Title (## heading, descriptive)
- Summary (2-3 sentences)
- Key Findings (bullet points with evidence)
- Actionable Rules (specific, not generic)
- Open Questions (if any)
- Source Episodes (list the episode tasks that contributed)

Write in English. Be specific and evidence-based, not generic."""

        router = get_router()
        client = router.get_client()
        if not client:
            return None

        try:
            response = router.call(
                task_type=TaskType.SUMMARY,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1500,
            )

            if not response or len(response.strip()) < 50:
                return None

            # Generate slug from theme
            slug = re.sub(r'[^\w\s-]', '', theme.lower())
            slug = re.sub(r'[\s]+', '-', slug)[:50].strip('-')
            date_str = datetime.utcnow().strftime("%Y-%m-%d")
            filename = f"{date_str}_{slug}.md"

            # Build article with frontmatter
            source_tasks = [e.get('task', '')[:80] for e in episodes[:10]]
            article = f"""---
title: "{theme}"
date: {date_str}
type: compiled-knowledge
source_count: {len(episodes)}
avg_score: {sum(e.get('score', 0) for e in episodes) / max(len(episodes), 1):.1f}
themes: [{', '.join(set(e.get('agent', 'system') for e in episodes))}]
status: draft
---

{response.strip()}

---

*Compiled from {len(episodes)} episodes on {date_str} by autoDream knowledge compiler.*
"""

            # Write article
            _ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
            article_path = _ARTICLES_DIR / filename
            article_path.write_text(article, encoding="utf-8")
            logger.info("Compiled article: %s (%d episodes)", filename, len(episodes))
            return article_path

        except Exception as e:
            logger.warning("Article compilation failed for '%s': %s", theme, e)
            return None

    def _group_episodes_by_theme(self, episodes: List[Dict]) -> Dict[str, List[Dict]]:
        """Group episodes by common theme for focused article compilation."""
        groups: Dict[str, List[Dict]] = {}

        for ep in episodes:
            task = ep.get("task", "").lower()
            agent = ep.get("agent", "system")

            # Determine theme from task keywords and agent
            if any(kw in task for kw in ["fix", "bug", "error", "fail"]):
                theme = "Bug Fixing Patterns"
            elif any(kw in task for kw in ["refactor", "clean", "delete", "simplify"]):
                theme = "Code Refactoring Insights"
            elif any(kw in task for kw in ["test", "pytest", "coverage"]):
                theme = "Testing Strategies"
            elif any(kw in task for kw in ["review", "audit", "quality"]):
                theme = "Code Review Findings"
            elif any(kw in task for kw in ["evolv", "prompt", "optim"]):
                theme = "Evolution and Optimization"
            elif any(kw in task for kw in ["dashboard", "ui", "frontend"]):
                theme = "Dashboard and UI Development"
            elif any(kw in task for kw in ["feat", "add", "implement", "new"]):
                theme = "Feature Development"
            elif "git commit" in task:
                theme = "Development Patterns from Git History"
            else:
                theme = f"Agent Operations: {agent}"

            groups.setdefault(theme, []).append(ep)

        return groups

    def _update_kb_index(self):
        """Regenerate knowledge_base/INDEX.md from all articles."""
        if not _ARTICLES_DIR.exists():
            return

        articles = sorted(_ARTICLES_DIR.glob("*.md"), reverse=True)
        if not articles:
            return

        lines = [
            "# Knowledge Base Index",
            "",
            f"> Auto-maintained by autoDream. Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
            f"> Total articles: {len(articles)}",
            "",
            "## Articles",
            "",
        ]

        for article_path in articles[:MAX_ARTICLES]:
            # Extract title from frontmatter
            try:
                content = article_path.read_text(encoding="utf-8")
                title_match = re.search(r'^title:\s*"?(.+?)"?\s*$', content, re.MULTILINE)
                title = title_match.group(1) if title_match else article_path.stem
                date_match = re.search(r'^date:\s*(\S+)', content, re.MULTILINE)
                date = date_match.group(1) if date_match else "?"
                status_match = re.search(r'^status:\s*(\S+)', content, re.MULTILINE)
                status = status_match.group(1) if status_match else "draft"
            except Exception:
                title = article_path.stem
                date = "?"
                status = "unknown"

            status_icon = "✅" if status == "verified" else "📝"
            lines.append(f"- {status_icon} [{title}](articles/{article_path.name}) — {date}")

        # Clean up old articles if over limit
        if len(articles) > MAX_ARTICLES:
            for old in articles[MAX_ARTICLES:]:
                try:
                    old.unlink()
                    logger.info("Cleaned old article: %s", old.name)
                except Exception:
                    pass

        index_path = _KB_DIR / "INDEX.md"
        index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("KB index updated: %d articles", min(len(articles), MAX_ARTICLES))

    def _save_report(self, report: DreamReport):
        """Save dream report to disk."""
        self._dream_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.utcnow().strftime("%Y-%m-%d_%H%M%S")
        report_file = self._dream_dir / f"{date_str}_dream.json"
        report_file.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Dream report saved: %s", report_file)

    def _clean_old_reports(self):
        """Keep only the most recent MAX_DREAM_REPORTS dream reports."""
        if not self._dream_dir.exists():
            return
        reports = sorted(self._dream_dir.glob("*_dream.json"))
        if len(reports) > MAX_DREAM_REPORTS:
            for old_report in reports[:-MAX_DREAM_REPORTS]:
                try:
                    old_report.unlink()
                    logger.info("Cleaned old dream report: %s", old_report.name)
                except Exception:
                    pass


# Singleton
_instance: Optional[AutoDream] = None

def get_auto_dream() -> AutoDream:
    global _instance
    if _instance is None:
        _instance = AutoDream()
    return _instance


# ─── Phase B TU-B1 (2026-05-04) — weekly_meta_digest ───────────────────────
# Refactor of auto_dream for graph-memory era: image-storage role done by
# Hindsight + paired_eval; what remains is narrative + meta-observations +
# CEO digest. This function is the new role.

def weekly_meta_digest(force_run: bool = False) -> Dict[str, Any]:
    """Generate weekly meta-digest markdown for CEO consumption.

    Pulls from:
      - tools/self_evolution_health (W1-W7 status)
      - .claude/data/traces.jsonl (last 7d auto-actions)
      - memexa/data/events.jsonl (errors + cron)
      - data/last_credit.json + data/approval_timeout_last_run.json
      - git log --since='1 week ago' (commits)

    Output: reports/weekly_digest_<YYYY-MM-DD>.md (markdown ≤500 lines)

    Returns: {written: bool, path: str, summary: dict}

    Frequency: should be called weekly (e.g. step_10 in run_graph_maintenance,
    only on Monday first-run). force_run=True bypasses Monday check.
    """
    import time as _t
    import os as _os

    workspace = Path(__file__).resolve().parents[2]
    reports_dir = workspace / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    last_state_path = workspace / "data" / "last_weekly_digest.json"

    today = datetime.utcnow().strftime("%Y-%m-%d")
    today_iso = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%SZ")

    # Throttle: skip if same week already done (unless force_run)
    if not force_run and last_state_path.exists():
        try:
            last = json.loads(last_state_path.read_text(encoding="utf-8"))
            last_ts = last.get("ts", 0)
            if _t.time() - last_ts < 6 * 86400:  # within 6 days
                return {"written": False, "skipped": True,
                        "reason": "already_run_this_week"}
        except (OSError, json.JSONDecodeError):
            pass

    # Section 1: W1-W7 self-evolution health
    health_section = "## Self-Evolution Health (W1-W7)\n\n"
    try:
        from tools.self_evolution_health import gather_all
        report = gather_all()
        health_section += "| W# | Subsystem | Status |\n|----|-----------|--------|\n"
        for c in report.get("checks", []):
            emoji = {"ALIVE": "✅", "DEAD": "🔴",
                     "DEGRADED": "🟡", "WARN": "🟡",
                     "STALE": "🟡"}.get(c.get("status"), "❓")
            health_section += f"| {c.get('name','?')[:50]} | {emoji} {c.get('status')} |\n"
    except Exception as e:
        health_section += f"_(unavailable: {type(e).__name__}: {e})_\n"

    # Section 2: Auto-actions last 7 days (from traces.jsonl)
    auto_section = "\n## Auto-Actions (last 7 days)\n\n"
    try:
        traces_path = workspace / ".claude" / "data" / "traces.jsonl"
        threshold = _t.time() - 7 * 86400
        action_counts: Dict[str, int] = {}
        if traces_path.exists():
            for line in traces_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if "approval_auto" not in line and "auto_dream" not in line and "big_loop" not in line:
                    continue
                try:
                    e = json.loads(line)
                    ev = e.get("event", "")
                    if ev.startswith(("approval_auto_", "auto_dream_", "big_loop_", "credit_")):
                        action_counts[ev] = action_counts.get(ev, 0) + 1
                except json.JSONDecodeError:
                    pass
        if action_counts:
            for ev, n in sorted(action_counts.items(), key=lambda x: -x[1]):
                auto_section += f"- `{ev}`: **{n}** times\n"
        else:
            auto_section += "_(no auto-actions recorded)_\n"
    except Exception as e:
        auto_section += f"_(error: {e})_\n"

    # Section 3: Errors / Warnings (events.jsonl)
    err_section = "\n## Errors & Warnings (last 7 days)\n\n"
    try:
        events_path = workspace / "memexa" / "data" / "events.jsonl"
        err_types = {"posttool_failure", "stop_failure", "agent_rate_limited",
                     "pattern_encoding_warning", "outbox_dead_letter"}
        err_counts: Dict[str, int] = {}
        if events_path.exists():
            with events_path.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not any(et in line for et in err_types):
                        continue
                    try:
                        e = json.loads(line)
                        if e.get("type") in err_types:
                            err_counts[e["type"]] = err_counts.get(e["type"], 0) + 1
                    except json.JSONDecodeError:
                        pass
        for et in sorted(err_types):
            err_section += f"- `{et}`: {err_counts.get(et, 0)}\n"
    except Exception as e:
        err_section += f"_(error: {e})_\n"

    # Section 4: Approval queue snapshot
    approval_section = "\n## Approval Queue\n\n"
    try:
        last_run_path = workspace / "data" / "approval_timeout_last_run.json"
        pending_path = workspace / "data" / "pending_approvals.json"
        if last_run_path.exists():
            lr = json.loads(last_run_path.read_text(encoding="utf-8"))
            approval_section += f"- last cron run: {lr.get('ts_iso', '?')}\n"
            approval_section += f"- last actions: {lr.get('actions', {})}\n"
        if pending_path.exists():
            p = json.loads(pending_path.read_text(encoding="utf-8"))
            n = len(p.get("candidates", []))
            approval_section += f"- pending candidates: **{n}**\n"
    except Exception as e:
        approval_section += f"_(error: {e})_\n"

    # Section 5: Git activity
    git_section = "\n## Git Activity (last 7 days)\n\n"
    try:
        r = subprocess.run(
            ["git", "log", "--since=1 week ago", "--pretty=format:- %h %s"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, cwd=str(workspace),
        )
        if r.returncode == 0 and r.stdout.strip():
            git_section += r.stdout[:3000]
        else:
            git_section += "_(no commits or git error)_\n"
    except Exception as e:
        git_section += f"_(error: {e})_\n"

    # Section 6: Pending review surfaces
    pending_section = "\n## Pending CEO Review\n\n"
    try:
        pending_trace = workspace / "data" / "trace_event_pending_review.jsonl"
        if pending_trace.exists():
            n_pending = sum(1 for line in pending_trace.read_text(encoding="utf-8").splitlines() if line.strip())
            pending_section += f"- trace events pending codification: **{n_pending}** "
            pending_section += "(`python -m src.core.trace_sink review-pending` to see)\n"
        else:
            pending_section += "- trace events pending codification: 0\n"
    except Exception:
        pending_section += "_(error reading pending review)_\n"

    # Compose full markdown
    md_lines = [
        f"# Weekly Meta-Digest — {today}",
        "",
        f"**generated**: {today_iso}",
        f"**source**: tools/self_evolution_health + traces.jsonl + events.jsonl + git",
        "",
        "---",
        "",
        health_section,
        auto_section,
        err_section,
        approval_section,
        git_section,
        pending_section,
        "",
        "---",
        "",
        "## How to use this digest",
        "- ✅ **All ALIVE**: nothing to do",
        "- 🔴 **DEAD**: check W# detail in `python -m tools.self_evolution_health`",
        "- 🟡 **DEGRADED**: investigate in next 7 days",
        "- **Pending CEO**: scan list, codify or reject",
    ]
    md = "\n".join(md_lines)

    out_path = reports_dir / f"weekly_digest_{today}.md"
    try:
        out_path.write_text(md, encoding="utf-8")
    except OSError as e:
        return {"written": False, "error": f"write failed: {e}"}

    # Update state file
    summary = {
        "ts": _t.time(),
        "ts_iso": datetime.utcnow().isoformat() + "Z",
        "report_path": str(out_path)[-100:],
    }
    try:
        last_state_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass

    # Emit trace
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("weekly_digest_generated", {
            "report_path": str(out_path)[-80:],
            "n_lines": len(md_lines),
        })
    except Exception:
        pass

    return {"written": True, "path": str(out_path), "n_lines": len(md_lines),
            "summary": summary}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "weekly-digest":
        force = "--force" in sys.argv
        result = weekly_meta_digest(force_run=force)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        sys.exit(0 if result.get("written") or result.get("skipped") else 1)

