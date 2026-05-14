"""
[DEPRECATED 2026-05-12] Last successful cycle: 2026-04-04 evo_1775312270
(deployed 2 prompts then KAIROS daemon stopped). Only callers are
kairos_daemon.py:869 and :1052 — KAIROS hasn't run for 5+ weeks.
Kept in place because kairos_daemon imports it; if you wake KAIROS up,
this code is still correct, just unused. Use src.core.memory_query
for current user-history recall (memory_full_v5, 13.8k cards, alive).

Evolution Orchestrator -- Wires L2->L3->L4 into automated closed loop.

5-stage pipeline:
  1. OBSERVE: read events + test results + semantic patterns
  2. REFLECT: run llm_judge on recent agent outputs
  3. CONSOLIDATE: semantic_memory.consolidate() if threshold met
  4. EVOLVE: prompt_evolver.evolve() for low-score agents
  5. MEASURE: record metrics and trend analysis

Trigger: SessionStart / auto_trigger / daily_daemon / manual
Output: Appends to data/evolution_runs.jsonl
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
_RUNS_FILE = _DATA_DIR / "evolution_runs.jsonl"
_WORKSPACE = Path(__file__).parent.parent.parent.parent  # claude workspace/
_AGENTS_DIR = _WORKSPACE / ".claude" / "agents"


@dataclass
class EvolutionRunResult:
    """Result of one evolution orchestrator run."""
    run_id: str
    timestamp: str
    duration_seconds: float
    stages: Dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    success: bool = True
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        # Populate top-level summary fields for monitoring dashboards
        reflect = d.get("stages", {}).get("reflect", {})
        evolve_s = d.get("stages", {}).get("evolve", {})
        consol = d.get("stages", {}).get("consolidate", {})
        d["avg_quality_score"] = reflect.get("avg_score", 0)
        d["llm_judged"] = reflect.get("llm_judged", 0)
        d["patterns_count"] = consol.get("pattern_count", 0)
        d["prompts_deployed"] = evolve_s.get("deployed", 0)
        return d


class EvolutionOrchestrator:
    """Wires L2 Reflexion -> L3 SemanticMemory -> L4 PromptEvolver."""

    def __init__(self):
        self._data_dir = _DATA_DIR
        self._agents_dir = _AGENTS_DIR

    async def stage_observe(self) -> Dict[str, Any]:
        """Stage 1: Read events, test results, patterns."""
        from .event_bus import read_events
        from .semantic_memory import get_semantic_memory

        events = read_events(last_n=200)
        sm = get_semantic_memory()

        # Run pytest
        test_result = self._run_pytest()

        # Extract agent execution events (including kairos_feedback for Route A)
        agent_events = [e for e in events if e.get("type") in
                       ("agent_complete", "agent_fail", "review_result",
                        "episode_recorded", "kairos_feedback", "low_quality_agent")]

        # Resource snapshot (if available)
        resource_info = {}
        try:
            from .resource_monitor import ResourceMonitor
            rm = ResourceMonitor()
            snapshot = rm.capture_snapshot()
            resource_info = {
                "memory_percent": snapshot.memory_percent,
                "disk_percent": snapshot.disk_percent,
                "cpu_percent": snapshot.cpu_percent,
            }
            # Check thresholds
            alerts = rm.thresholds.check(snapshot)
            if alerts:
                resource_info["alerts"] = [a["message"] for a in alerts]
                logger.warning("Resource alerts: %s", resource_info["alerts"])
        except Exception:
            pass  # psutil not available or other issue

        return {
            "total_events": len(events),
            "agent_events": len(agent_events),
            "pattern_count": sm.pattern_count,
            "episodes_pending": sm._episodes_since_consolidation,
            "test_passed": test_result.get("success", False),
            "test_count": test_result.get("passed", 0),
            "events_sample": agent_events[-10:],
            "resources": resource_info,
        }

    def _run_pytest(self) -> Dict:
        """Get pytest results using shared cache (no redundant subprocess)."""
        try:
            from .pytest_cache import get_test_results
            return get_test_results()
        except Exception as e:
            return {"passed": 0, "failed": 0, "success": False, "error": str(e)}

    async def stage_reflect(self, observations: Dict) -> Dict[str, Any]:
        """Stage 2: Judge recent agent outputs using REAL signals.

        Route A fix: instead of just reading pre-existing scores from events,
        we now:
        1. Read kairos_feedback events (not just agent_complete/fail)
        2. Read quality_verified events for continuous 0-1 scores (more discriminating)
        3. Call llm_judge.judge() independently on recent KAIROS outputs
        4. Use RELATIVE threshold (below population mean) instead of absolute 3.0
        """
        from .event_bus import read_events

        # Source 1: kairos_feedback events (real KAIROS execution data)
        all_events = read_events(last_n=200)
        kairos_events = [
            e for e in all_events
            if e.get("type") in ("kairos_feedback", "low_quality_agent")
        ]

        scores: Dict[str, List[float]] = {}

        # Process kairos_feedback — these have real quality_score from feedback_collector
        for e in kairos_events:
            agent = e.get("agent", "unknown")
            details = e.get("details", {})
            if e.get("type") == "kairos_feedback":
                score = details.get("quality_score", 3)
            elif e.get("type") == "low_quality_agent":
                score = details.get("quality_score", 2)
            else:
                continue
            if agent not in scores:
                scores[agent] = []
            scores[agent].append(float(score))

        # Source 2: quality_verified events (continuous 0-1, more discriminating)
        # These come from real_quality_verifier's 7-signal weighted scoring
        continuous_scores: Dict[str, List[float]] = {}
        for e in all_events:
            if e.get("type") == "quality_verified":
                details = e.get("details", {})
                # Map project_id back to agent via kairos_feedback
                raw_score = details.get("score", 0)  # 0-1 continuous
                agent = e.get("agent", "real_quality_verifier")
                # Find matching kairos_feedback for this project to get agent name
                proj_id = details.get("project_id", "")
                for kf in kairos_events:
                    if kf.get("details", {}).get("project_id") == proj_id:
                        agent = kf.get("agent", agent)
                        break
                if agent not in continuous_scores:
                    continuous_scores[agent] = []
                continuous_scores[agent].append(raw_score)

        # Source 3: legacy agent events (backward compat)
        agent_events = observations.get("events_sample", [])
        for e in agent_events:
            agent = e.get("agent", "unknown")
            details = e.get("details", {})
            score = details.get("score", 3)
            if isinstance(score, str):
                score = 4.0 if score == "APPROVED" else 2.0
            if agent not in scores:
                scores[agent] = []
            scores[agent].append(float(score))

        if not scores:
            return {"judged": 0, "avg_score": 0, "low_score_agents": []}

        # Source 4: Independent LLM Judge on recent KAIROS outputs
        judged_count = 0
        try:
            from .llm_judge import judge
            feedback_file = self._data_dir / "kairos_feedback.jsonl"
            if feedback_file.exists():
                lines = feedback_file.read_text(encoding="utf-8").strip().splitlines()
                recent = lines[-5:]  # Judge last 5 KAIROS outputs
                for line in recent:
                    try:
                        fb = json.loads(line)
                        agent = fb.get("agent_role", "unknown")
                        title = fb.get("title", "")
                        summary = fb.get("summary", "")
                        if title and summary:
                            verdict = await judge(
                                task_description=title,
                                output=summary[:2000],
                            )
                            llm_score = float(verdict.get("score", 3))
                            if agent not in scores:
                                scores[agent] = []
                            scores[agent].append(llm_score)
                            judged_count += 1
                    except Exception:
                        continue
        except Exception as e:
            logger.debug("LLM Judge skipped in reflect: %s", e)

        avg_scores = {a: sum(s) / len(s) for a, s in scores.items()}

        total_scores = [s for sl in scores.values() for s in sl]
        overall_avg = sum(total_scores) / max(len(total_scores), 1)

        # RELATIVE threshold: agents below population mean are candidates for evolution
        # This ensures evolution always has targets even when all scores are high
        # Also include any agent with absolute score < 3.0 (clearly bad)
        low_score = []
        if len(avg_scores) >= 2:
            # Relative: below mean (always targets bottom half)
            low_score = [a for a, avg in avg_scores.items() if avg < overall_avg]
        # Always include absolute low performers
        for a, avg in avg_scores.items():
            if avg < 3.0 and a not in low_score:
                low_score.append(a)

        # Also factor in continuous scores: agents with avg 0-1 score < 0.6
        for agent, c_scores in continuous_scores.items():
            if agent in ("real_quality_verifier",):
                continue  # Skip the verifier itself
            c_avg = sum(c_scores) / len(c_scores)
            if c_avg < 0.6 and agent not in low_score:
                low_score.append(agent)
                logger.info("Continuous score: %s avg=%.2f (below 0.6), adding to evolution targets", agent, c_avg)

        return {
            "judged": len(total_scores),
            "llm_judged": judged_count,
            "avg_score": round(overall_avg, 2),
            "agent_scores": {a: round(v, 2) for a, v in avg_scores.items()},
            "continuous_scores": {a: round(sum(s)/len(s), 3) for a, s in continuous_scores.items()},
            "low_score_agents": low_score,
            "overall_avg": round(overall_avg, 2),
            "sources": ["kairos_feedback", "quality_verified", "agent_events", "llm_judge"],
        }

    async def stage_consolidate(self, observations: Dict) -> Dict[str, Any]:
        """Stage 3: Consolidate episodes into semantic patterns."""
        from .semantic_memory import get_semantic_memory, CONSOLIDATION_THRESHOLD

        sm = get_semantic_memory()
        if sm._episodes_since_consolidation < CONSOLIDATION_THRESHOLD:
            return {
                "skipped": True,
                "reason": f"Only {sm._episodes_since_consolidation} episodes (threshold: {CONSOLIDATION_THRESHOLD})",
                "new_patterns": [],
            }

        # Build episodes from events
        from .event_bus import read_events
        events = read_events(last_n=200)
        episodes = []
        for e in events:
            if e.get("type") in ("episode_recorded", "agent_complete"):
                details = e.get("details", {})
                episodes.append({
                    "task": details.get("task", e.get("type", "")),
                    "output": details.get("output_summary", ""),
                    "score": details.get("score", 3),
                    "agent": e.get("agent", "system"),
                })

        if not episodes:
            return {"skipped": True, "reason": "No episodes to consolidate", "new_patterns": []}

        new_ids = await sm.consolidate(episodes[:30])  # Cap at 30
        return {
            "skipped": False,
            "episodes_processed": len(episodes[:30]),
            "new_patterns": new_ids,
            "pattern_count": sm.pattern_count,
        }

    async def stage_evolve(self, reflections: Dict) -> Dict[str, Any]:
        """Stage 4: Evolve prompts for low-score agents.

        Route A fix: merge two signal sources for low-score detection:
        1. stage_reflect()'s low_score_agents (from avg scores)
        2. low_quality_agent events from feedback_collector (W5)
        """
        from .prompt_evolver import get_prompt_evolver
        from .event_bus import read_events

        # Source 1: from stage_reflect
        low_score_agents = list(reflections.get("low_score_agents", []))

        # Source 2: from W5 — low_quality_agent events in last 200 events
        try:
            recent_events = read_events(last_n=200)
            for e in recent_events:
                if e.get("type") == "low_quality_agent":
                    agent = e.get("agent", "")
                    if agent and agent not in low_score_agents:
                        low_score_agents.append(agent)
                        logger.info("W5 signal: adding %s to evolution targets", agent)
        except Exception:
            pass
        if not low_score_agents:
            return {"evolved": 0, "deployed": 0, "agents": []}

        evolver = get_prompt_evolver()
        evolved_agents = []

        for agent_name in low_score_agents:
            # Read current agent prompt
            agent_file = self._agents_dir / f"{agent_name}.md"
            if not agent_file.exists():
                continue

            current_prompt = agent_file.read_text(encoding="utf-8")

            # Build outcomes from events
            events = read_events(last_n=100)
            outcomes = []
            for e in events:
                if e.get("agent") == agent_name:
                    details = e.get("details", {})
                    outcomes.append({
                        "task": details.get("task", ""),
                        "score": details.get("score", 3),
                        "output_summary": details.get("output_summary", ""),
                    })

            if len(outcomes) < 2:
                continue

            # Attempt evolution
            new_prompt = await evolver.evolve(agent_name, current_prompt, outcomes)
            if new_prompt:
                # Before deploying, submit L2 approval for CEO review
                from .approval_queue import submit_approval
                apr_id = submit_approval(
                    level="L2",
                    category="prompt_evolution",
                    title=f"Prompt evolution for {agent_name}",
                    context=f"Agent {agent_name} scored avg {sum(o.get('score', 3) for o in outcomes) / len(outcomes):.1f}/5 over {len(outcomes)} runs",
                    proposal=f"Deploy evolved prompt (improvement: {evolver._history[-1].improvement:.1%})" if evolver._history else "Deploy evolved prompt",
                    evidence=[f"outcomes: {len(outcomes)}", f"low_score: True"],
                )
                logger.info("Submitted L2 approval %s for %s prompt evolution", apr_id, agent_name)
                # Deploy immediately (L2 = non-blocking), CEO reviews async
                # Write back to agent file
                deployed = self._deploy_prompt(agent_name, agent_file, current_prompt, new_prompt)
                # Read improvement from last history record (stats doesn't have it)
                improvement = 0.0
                if evolver._history:
                    improvement = evolver._history[-1].improvement
                evolved_agents.append({
                    "agent": agent_name,
                    "deployed": deployed,
                    "improvement": improvement,
                })

        deployed_count = sum(1 for a in evolved_agents if a.get("deployed"))
        return {
            "evolved": len(evolved_agents),
            "deployed": deployed_count,
            "agents": evolved_agents,
        }

    def _deploy_prompt(self, agent_name: str, agent_file: Path,
                       old_prompt: str, new_prompt: str) -> bool:
        """Write evolved prompt back to agent md file."""
        from .event_bus import log_event

        # L3 gate for core Opus agents
        PROTECTED_AGENTS = {"qa-director", "strategic-advisor", "chief-researcher"}
        if agent_name in PROTECTED_AGENTS:
            from .approval_queue import submit_approval
            apr_id = submit_approval(
                level="L3",
                category="prompt_evolution",
                title=f"BLOCKED: Prompt change for core agent {agent_name}",
                context=f"Core Opus agent prompt modification requires CEO approval",
                proposal=f"New prompt: {new_prompt[:200]}...",
                blocked_tasks=[f"deploy_{agent_name}"],
            )
            logger.warning("L3 blocked: %s prompt change queued as %s", agent_name, apr_id)
            return False

        try:
            # Backup
            backup = agent_file.with_suffix(".md.bak")
            backup.write_text(old_prompt, encoding="utf-8")

            # Preserve YAML frontmatter (must start at line 1)
            # Only match leading frontmatter, not mid-content "---" separators
            import re
            fm_match = re.match(r'^(---\n.*?\n---)\n', old_prompt, re.DOTALL)
            if fm_match:
                new_content = fm_match.group(1) + "\n\n" + new_prompt
            else:
                new_content = new_prompt

            agent_file.write_text(new_content, encoding="utf-8")
            log_event("prompt_deployed", agent=agent_name, details={
                "backup": str(backup),
            })
            logger.info("Deployed evolved prompt for %s", agent_name)
            return True
        except Exception as e:
            logger.error("Failed to deploy prompt for %s: %s", agent_name, e)
            return False

    async def stage_measure(self, stages: Dict[str, Any]) -> Dict[str, Any]:
        """Stage 5: Record metrics and compute trends."""
        observe = stages.get("observe", {})
        reflect = stages.get("reflect", {})
        consolidate = stages.get("consolidate", {})
        evolve = stages.get("evolve", {})

        metrics = {
            "test_pass_rate": 1.0 if observe.get("test_passed") else 0.0,
            "avg_judge_score": reflect.get("avg_score", 0),
            "patterns_active": consolidate.get("pattern_count", observe.get("pattern_count", 0)),
            "patterns_added": len(consolidate.get("new_patterns", [])),
            "prompts_evolved": evolve.get("evolved", 0),
            "prompts_deployed": evolve.get("deployed", 0),
        }

        # Cost tracking (feature flag gated)
        try:
            from memexa.config_loader import get_feature_flag
            if get_feature_flag("cost_tracking", False):
                from .event_bus import read_events
                recent = read_events(last_n=100)
                cost_events = [e for e in recent if e.get("type") == "cost_record"]
                total_cost = sum(e.get("details", {}).get("usd", 0) for e in cost_events)
                metrics["estimated_cost_usd"] = round(total_cost, 4)
        except Exception:
            pass

        return metrics

    async def run(self) -> EvolutionRunResult:
        """Execute full evolution cycle."""
        from .event_bus import log_event

        run_id = f"evo_{int(time.time())}"
        start = time.time()
        stages: Dict[str, Any] = {}

        log_event("evolution_cycle_start", agent="orchestrator", details={"run_id": run_id})

        try:
            # Stage 1
            stages["observe"] = await self.stage_observe()
            logger.info("Stage 1 OBSERVE: %d events, %d patterns",
                       stages["observe"]["total_events"], stages["observe"]["pattern_count"])

            # Stage 2
            stages["reflect"] = await self.stage_reflect(stages["observe"])
            logger.info("Stage 2 REFLECT: avg_score=%.2f, low_score=%s",
                       stages["reflect"]["avg_score"], stages["reflect"]["low_score_agents"])

            # Stage 3
            stages["consolidate"] = await self.stage_consolidate(stages["observe"])
            logger.info("Stage 3 CONSOLIDATE: %s",
                       "skipped" if stages["consolidate"].get("skipped") else
                       f"{len(stages['consolidate'].get('new_patterns', []))} new patterns")

            # Stage 4
            stages["evolve"] = await self.stage_evolve(stages["reflect"])
            logger.info("Stage 4 EVOLVE: %d evolved, %d deployed",
                       stages["evolve"]["evolved"], stages["evolve"]["deployed"])

            # Stage 5
            stages["measure"] = await self.stage_measure(stages)
            logger.info("Stage 5 MEASURE: %s", stages["measure"])

            duration = time.time() - start
            summary_parts = []
            if stages["consolidate"].get("new_patterns"):
                summary_parts.append(f"{len(stages['consolidate']['new_patterns'])} patterns added")
            if stages["evolve"]["deployed"] > 0:
                summary_parts.append(f"{stages['evolve']['deployed']} prompts deployed")
            summary_parts.append(f"avg_score={stages['reflect']['avg_score']}")

            # Check approval queue status
            try:
                from .approval_queue import get_pending
                pending = get_pending()
                l3_count = sum(1 for p in pending if p.get("level") == "L3" and p.get("status") == "pending")
                if l3_count > 0:
                    summary_parts.append(f"{l3_count} L3 approvals pending")
            except Exception:
                pass

            result = EvolutionRunResult(
                run_id=run_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
                duration_seconds=round(duration, 2),
                stages=stages,
                summary=", ".join(summary_parts) or "no changes",
                success=True,
            )

        except Exception as e:
            logger.error("Evolution cycle failed: %s", e)
            result = EvolutionRunResult(
                run_id=run_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
                duration_seconds=round(time.time() - start, 2),
                stages=stages,
                summary=f"failed: {e}",
                success=False,
                error=str(e),
            )

        # Append to runs log
        self._append_run(result)
        log_event("evolution_cycle_end", agent="orchestrator", details={
            "run_id": run_id, "success": result.success, "summary": result.summary,
        })

        return result

    def _append_run(self, result: EvolutionRunResult):
        """Append run result to evolution_runs.jsonl."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(_RUNS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("Failed to write run log: %s", e)


# Convenience function
async def run_evolution_cycle() -> Dict[str, Any]:
    """Run one evolution cycle and return summary."""
    orch = EvolutionOrchestrator()
    result = await orch.run()
    return result.to_dict()


def run_evolution_cycle_sync() -> Dict[str, Any]:
    """Synchronous wrapper."""
    return asyncio.run(run_evolution_cycle())
