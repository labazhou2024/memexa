"""
Agent Dispatcher — Generate structured dispatch plans from BigLoop agent specs.

Converts raw agent spec dicts (produced by BigLoop Q2/Q3/Q5/Q6/Q7) into:
  1. A structured dispatch plan with phase grouping and dependency tracking
  2. Human-readable dispatch instructions the CTO (Claude) can execute directly

Usage:
    from src.core.agent_dispatcher import AgentDispatcher

    dispatcher = AgentDispatcher()
    plan = dispatcher.generate_dispatch_plan(agent_specs)
    print(dispatcher.format_dispatch_instructions(plan))
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Phase definitions: agents that belong to each dispatch phase.
# Agents in the same phase can run in parallel when parallel=True.
_PHASE_CONFIG: List[Dict[str, Any]] = [
    {
        "phase": 2,
        "agents": ["qa-director"],
        "parallel": False,
        "depends_on": [],
        "description": "QA Director deep review — must complete before fixes",
    },
    {
        "phase": 3,
        "agents": ["sonnet-executor"],
        "parallel": True,
        "depends_on": [2],
        "description": "Bug-fix executors — one per file batch, all parallel",
    },
    {
        "phase": 5,
        "agents": ["release-gate", "strategic-advisor"],
        "parallel": True,
        "depends_on": [3],
        "description": "Release gate + strategic advisor — parallel after Q4 passes",
    },
    {
        "phase": 7,
        "agents": ["briefing-agent"],
        "parallel": False,
        "depends_on": [5],
        "description": "Briefing report — after all agents complete",
    },
]

# Map agent name to its canonical phase number.
_AGENT_TO_PHASE: Dict[str, int] = {}
for _cfg in _PHASE_CONFIG:
    for _agent in _cfg["agents"]:
        _AGENT_TO_PHASE[_agent] = _cfg["phase"]


@dataclass
class DispatchEntry:
    """A single agent invocation within a dispatch plan phase."""

    agent: str
    model: str
    prompt: str
    extra_fields: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "agent": self.agent,
            "model": self.model,
            "prompt": self.prompt,
        }
        d.update(self.extra_fields)
        return d


@dataclass
class DispatchPhase:
    """One phase in the dispatch plan (agents that share the same dependency horizon)."""

    phase: int
    description: str
    parallel: bool
    depends_on: List[int]
    agents: List[DispatchEntry] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "description": self.description,
            "parallel": self.parallel,
            "depends_on": self.depends_on,
            "agents": [a.to_dict() for a in self.agents],
        }


@dataclass
class DispatchPlan:
    """Full structured dispatch plan output by AgentDispatcher."""

    plan_id: str
    total_agents: int
    phases: List[DispatchPhase] = field(default_factory=list)
    unrecognized_agents: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "total_agents": self.total_agents,
            "phases": [p.to_dict() for p in self.phases],
            "unrecognized_agents": self.unrecognized_agents,
        }


class AgentDispatcher:
    """
    Converts BigLoop agent specs into structured dispatch plans.

    The main workflow:
      specs  -->  generate_dispatch_plan()  -->  DispatchPlan
             -->  format_dispatch_instructions()  -->  str  (CTO-readable)
    """

    def generate_dispatch_plan(
        self, agent_specs: List[Dict[str, Any]], plan_id: str = "dispatch_plan"
    ) -> Dict[str, Any]:
        """
        Group agent specs by phase and build a full dispatch plan.

        Parameters
        ----------
        agent_specs:
            Raw spec dicts produced by BigLoop (each must have at least "agent" key).
        plan_id:
            Identifier embedded in the plan dict (useful for tracing).

        Returns
        -------
        dict
            Serialisable plan compatible with data/agent_dispatch_plan.json.
        """
        # Build phase buckets keyed by phase number
        phase_buckets: Dict[int, List[DispatchEntry]] = {}
        unrecognized: List[Dict[str, Any]] = []

        for raw in agent_specs:
            agent_name = raw.get("agent", "")
            phase_num = _AGENT_TO_PHASE.get(agent_name)

            if phase_num is None:
                logger.warning("AgentDispatcher: unrecognized agent '%s'", agent_name)
                unrecognized.append(raw)
                continue

            entry = self._build_entry(raw)
            if phase_num not in phase_buckets:
                phase_buckets[phase_num] = []
            phase_buckets[phase_num].append(entry)

        # Build DispatchPhase objects only for phases that have agents
        phases: List[DispatchPhase] = []
        for cfg in _PHASE_CONFIG:
            phase_num = cfg["phase"]
            entries = phase_buckets.get(phase_num)
            if not entries:
                continue

            # sonnet-executor phase: always parallel when multiple batches exist
            parallel = cfg["parallel"] if len(entries) <= 1 else cfg["parallel"]

            phases.append(
                DispatchPhase(
                    phase=phase_num,
                    description=cfg["description"],
                    parallel=parallel,
                    depends_on=list(cfg["depends_on"]),
                    agents=entries,
                )
            )

        plan = DispatchPlan(
            plan_id=plan_id,
            total_agents=len(agent_specs),
            phases=phases,
            unrecognized_agents=unrecognized,
        )
        return plan.to_dict()

    def format_agent_prompt(self, spec: Dict[str, Any]) -> str:
        """
        Generate the full prompt string for a single agent spec.

        Appends structured context (files, bugs) to the base prompt so the
        dispatched agent has everything it needs in a single string.

        Parameters
        ----------
        spec:
            Raw agent spec dict (keys: agent, model, prompt, optionally
            files_to_review, target_files, bugs, output_format).

        Returns
        -------
        str
            Complete prompt text ready to paste into an Agent tool call.
        """
        agent = spec.get("agent", "unknown-agent")
        base_prompt = spec.get("prompt", "")
        lines: List[str] = [f"[Agent: {agent}]", base_prompt]

        files_to_review = spec.get("files_to_review", [])
        if files_to_review:
            lines.append(
                f"\nFiles to review ({len(files_to_review)} total):\n"
                + "\n".join(f"  - {f}" for f in files_to_review[:50])
            )

        target_files = spec.get("target_files", [])
        if target_files:
            lines.append(
                "\nTarget files:\n"
                + "\n".join(f"  - {f}" for f in target_files)
            )

        bugs = spec.get("bugs", [])
        if bugs:
            lines.append(f"\nBugs to fix ({len(bugs)} total):")
            for i, bug in enumerate(bugs, 1):
                desc = bug.get("description", str(bug))[:120]
                sev = bug.get("severity", "")
                sev_tag = f" [{sev.upper()}]" if sev else ""
                lines.append(f"  {i}.{sev_tag} {desc}")

        output_format = spec.get("output_format", "")
        if output_format:
            lines.append(f"\nOutput format: {output_format}")

        return "\n".join(lines)

    def format_dispatch_instructions(self, plan: Dict[str, Any]) -> str:
        """
        Generate human-readable instructions for the CTO to execute the plan.

        The output shows exactly which Agent tool calls to make, in which order,
        and which ones can be parallelised within a single response.

        Parameters
        ----------
        plan:
            Output of generate_dispatch_plan() (already a plain dict).

        Returns
        -------
        str
            Multi-line instruction block ready to display to the CTO.
        """
        lines: List[str] = []
        phases: List[Dict[str, Any]] = plan.get("phases", [])
        total = plan.get("total_agents", 0)
        plan_id = plan.get("plan_id", "unknown")

        lines.append("=" * 60)
        lines.append(f"AGENT DISPATCH PLAN  [{plan_id}]")
        lines.append(f"Total agents: {total}  |  Phases: {len(phases)}")
        lines.append("=" * 60)

        if not phases:
            lines.append("(No agents to dispatch)")
            unrecognized = plan.get("unrecognized_agents", [])
            if unrecognized:
                lines.append(
                    f"\nWARNING: {len(unrecognized)} unrecognized agent(s) — "
                    "check agent names:\n"
                    + "\n".join(f"  - {a.get('agent','?')}" for a in unrecognized)
                )
            return "\n".join(lines)

        for phase_dict in phases:
            phase_num = phase_dict.get("phase")
            description = phase_dict.get("description", "")
            parallel = phase_dict.get("parallel", False)
            depends_on = phase_dict.get("depends_on", [])
            agents_in_phase: List[Dict[str, Any]] = phase_dict.get("agents", [])

            dep_str = (
                ", ".join(f"Phase {d}" for d in depends_on) if depends_on else "none"
            )
            exec_mode = "PARALLEL (single response)" if parallel and len(agents_in_phase) > 1 else "SEQUENTIAL"

            lines.append("")
            lines.append(f"--- Phase {phase_num}: {description} ---")
            lines.append(f"    Depends on: {dep_str}")
            lines.append(f"    Execution:  {exec_mode}")
            lines.append(f"    Agents ({len(agents_in_phase)}):")

            for i, agent_dict in enumerate(agents_in_phase, 1):
                agent_name = agent_dict.get("agent", "?")
                model = agent_dict.get("model", "?")
                prompt_preview = (agent_dict.get("prompt", "")[:80] + "...").replace(
                    "\n", " "
                )
                lines.append(f"      {i}. [{model}] {agent_name}")
                lines.append(f"         Prompt: {prompt_preview}")

                # Show file counts if present
                ftr = agent_dict.get("files_to_review", [])
                tgt = agent_dict.get("target_files", [])
                bgs = agent_dict.get("bugs", [])
                if ftr:
                    lines.append(f"         Files to review: {len(ftr)}")
                if tgt:
                    lines.append(f"         Target files: {', '.join(tgt[:3])}")
                if bgs:
                    lines.append(f"         Bugs: {len(bgs)}")

            if parallel and len(agents_in_phase) > 1:
                lines.append(
                    f"    >>> Spawn all {len(agents_in_phase)} agents in ONE response using Task tool <<<"
                )
            else:
                lines.append(
                    f"    >>> Spawn agent using Task tool, await result before Phase {phase_num + 1 if phase_num < 7 else 'end'} <<<"
                )

        unrecognized = plan.get("unrecognized_agents", [])
        if unrecognized:
            lines.append("")
            lines.append(
                f"WARNING: {len(unrecognized)} unrecognized agent spec(s) not included in plan:"
            )
            for ua in unrecognized:
                lines.append(f"  - agent='{ua.get('agent','?')}'")

        lines.append("")
        lines.append("=" * 60)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_entry(self, raw: Dict[str, Any]) -> DispatchEntry:
        """Extract known fields; store remaining fields in extra_fields."""
        known = {"agent", "model", "prompt"}
        extra = {k: v for k, v in raw.items() if k not in known}
        return DispatchEntry(
            agent=raw.get("agent", ""),
            model=raw.get("model", "sonnet"),
            prompt=self.format_agent_prompt(raw),
            extra_fields=extra,
        )


def save_dispatch_plan(plan: Dict[str, Any], data_dir: Optional[Path] = None) -> Path:
    """
    Save a dispatch plan to data/agent_dispatch_plan.json.

    Parameters
    ----------
    plan:
        Output of AgentDispatcher.generate_dispatch_plan().
    data_dir:
        Override for the data directory. Defaults to memex/data/.

    Returns
    -------
    Path
        Absolute path of the written file.
    """
    if data_dir is None:
        data_dir = Path(__file__).parent.parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    out = data_dir / "agent_dispatch_plan.json"
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Dispatch plan saved: %s", out)
    return out
