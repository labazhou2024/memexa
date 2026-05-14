"""
Trace Sink (v1, 2026-04-19)

Local JSONL-based session trace store. Minimal dependency (stdlib only).
Serves as measurement substrate for L6 prompt-evolution reward signals,
CEO weekly briefing ("what happened this week"), and future Langfuse
migration dataset.

Schema (one line per event, JSONL):
  {
    "ts":       ISO-8601 naive UTC,
    "session_id": Claude Code SID or "unknown",
    "event":    session_start | tool_use | hook_outcome | ceo_feedback | session_end | autopilot_stage,
    "payload":  free-form dict (bounded 4KB),
  }

session_start payload convention (v3.1 T0.5):
  payload.initiator: one of "ceo" | "agent" | "cron" | "unknown".
    Set by memexa.core.session_heartbeat.emit() at SessionStart hook.
    Only initiator=="ceo" ticks the CEO-active-day counter that
    AC-15b / AC-15c / §6 sunset clock consume. Agent subprocesses
    inherit parent env, so classification precedence is
    agent > cron > ceo > unknown to avoid subagent sessions
    inflating the CEO-active-day count.

Design choices (explicit rejection of Langfuse Docker stack, see
.claude/plans/2026-04-19_oss_integration_report.md Approach A0):

- Zero external deps: stdlib json + pathlib + os (no OTLP / no http).
- Fire-and-forget: write failures logged but never raise (never break
  the Stop hook chain).
- Size bounded: payload truncated to 4KB to prevent runaway memory.
- Append-only: concurrent Claude Code sessions safe via single-write
  atomic append (POSIX O_APPEND; Windows cooperative OK for our load).
- Secret scrubbing: caller responsible. trace_sink itself does NOT apply
  content filters — the ceo_feedback path does its own scrub before
  recording. This is deliberate: trace_sink records a wide set of events
  (hook outcomes, tool names) where aggressive scrubbing would lose
  signal. Callers writing user-prompt-derived content MUST scrub first.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

# Workspace-rooted path. memexa/memexa/core/trace_sink.py -> workspace
_WORKSPACE = Path(__file__).parent.parent.parent.parent
_TRACE_DIR = _WORKSPACE / ".claude" / "data"
_TRACE_FILE = _TRACE_DIR / "traces.jsonl"

_MAX_PAYLOAD_BYTES = 4096
_ALLOWED_EVENTS = {
    # 2026-05-04 Phase 0-2 治理 codify (W1-* + B-* + Rule 0):
    "phase_0_complete", "phase_1_complete", "phase_2_complete",
    "phase_3_complete", "phase_4_complete", "phase_5_complete",
    "phase_6_complete", "wiring_repair_complete",
    "scope_flag_auto_dismissed_warning",
    "fail_open_emergency_authorized", "fail_open_emergency_replay_rejected",
    "fail_open_authorized_with_coverage", "fail_open_reason_rejected",
    "ac_contract_violation", "ac_red_detected",
    "task_spec_integrity_warn", "plan_revision_autopilot_bypass",
    "reviewer_iteration_governance_attest", "reviewer_fallback_triggered",
    "autopilot_flag_write_authorized", "autopilot_flag_write_blocked",
    # 2026-05-04 Phase 3-6 closure (L-1..L-5, I-1..I-3, V-1..V-5):
    "live_finding_reported", "live_finding_cleared",
    "live_findings_gate_block", "replan_live_findings_triggered",
    "integration_matrix_cluster_run", "cross_model_tu_review_requested",
    # self_evolution_reconnect TU-A1..A8 (2026-05-04): 11 new events
    "credit_degraded_mode", "prompt_evolver_check_fired",
    "auto_dream_triggered", "big_loop_triggered", "big_loop_spawn_failed",
    "approval_auto_approved", "approval_auto_deferred",
    "approval_escalation_required", "approval_timeout_run",
    "approval_schema_migrated", "approval_timeout_fallback_triggered",
    # batch_quality_uplift integration (2026-05-04): run_for_cron API trace
    "batch_chat_extract_run_for_cron_completed",
    # Phase B + _memory_paths (2026-05-04): codified after auto-allow review
    "memory_path_reconciled",
    "trace_event_auto_allowed",
    "commit_msg_hook_injected",
    "wechat_batch_started",
    # parallel-agent emitted events (additive — schema-contract test alignment)
    "chat_graph_grayscale_live_probe", "chat_graph_grayscale_day_1",
    "lineage_l1_built", "lineage_l2_clustered",
    "chat_context_db_unreachable", "chat_context_capability_denied",
    "replay_dropped",
    # TU-A1/A2/E1/E2/F1/F2/F3/F4/H1/I1 (2026-05-03): paired_eval full chain + 27B arbiter
    "mac_launchd_installed", "mac_mem_degrade_triggered",
    "arbiter_27b_lock_acquired", "arbiter_27b_lock_blocked",
    "arbiter_27b_lock_released", "arbiter_27b_lock_release_denied",
    "arbiter_27b_swap_to_27b_start", "arbiter_27b_swap_to_27b_complete",
    "arbiter_27b_swap_to_27b_load_failed", "arbiter_27b_swap_to_27b_timeout",
    "arbiter_27b_swap_to_27b_dry_run", "arbiter_27b_swap_back_start",
    "arbiter_27b_swap_back_complete", "arbiter_27b_swap_back_dry_run",
    "arbiter_27b_pending_claimed", "arbiter_27b_status_updated",
    "arbiter_27b_dry_run_arbitrate", "arbiter_27b_disagreement_processed",
    "arbiter_27b_inference_fail", "arbiter_27b_parse_fail",
    "arbiter_27b_pg_insert", "arbiter_27b_pg_insert_dry_run",
    "arbiter_27b_rollback_applied", "arbiter_timer_fired",
    "idle_window_detected", "idle_window_busy",
    "paired_eval_high_stakes_routed", "paired_eval_audit_run",
    "embedding_consolidation_iteration", "embedding_consolidation_revived",
    "retroactive_paired_run_complete", "retroactive_pii_audit_complete",
    "backfill_lineage_run",
    "batch_chat_extract_done",
    "wechat_backfill_quarantine_blocked", "wechat_backfill_month_done",
    "session_start", "tool_use", "hook_outcome",
    "ceo_feedback", "session_end", "autopilot_stage",
    # P0-1 (2026-04-21): heartbeat observability — events already emitted
    # by memory_ingest_watcher but silently dropped before this commit.
    "haiku_extract_start", "haiku_extract_done", "haiku_extract_fail",
    "scan_with_timeout_done", "drain_queue_done",
    # A7 (2026-04-21): fingerprint-merge collision monitoring
    "fingerprint_merge",
    # TU-5 (plan v2 2026-04-22): task_router injection-attempt audit
    # (OWASP LLM01 — prompt-body override tokens rejected but logged)
    "classify_injection_attempt",
    # Planning infrastructure v3 (2026-04-21): council + revision + evidence
    "council_spawn_batch",
    "council_position_submitted",
    "council_synthesis_complete",
    "council_conflict_unresolved",
    "plan_revision_submitted",
    "plan_revision_approved",
    "plan_revision_rejected",
    "plan_depth_soft_warn",
    "depth_gate_block",
    "ac_audit_block",
    "ac_verified",
    "ac_verify_failed",
    "force_complete_submitted",
    "revise_anchor_requested",
    "axis_violation",
    "gate_data_source_unhealthy",
    "task_binding_set",
    "task_binding_cleared",
    "task_binding_degraded",
    # LIVE evidence events for probe_live_evidence
    "cli_invoked",
    "hook_fired",
    "schema_migration",
    "agent_spawned_for_task",
    # 2026-04-24 (plan_v3 AC-3): gate degradation events. session_gate
    # Rules 7/8 and plan_retro_gate emit these; prior to this commit
    # they were dropped by trace_sink allowlist, making gate
    # degradations invisible. Closes feedback_writer_reader_schema_
    # contract.md HARD RULE violation.
    "rule7_fail_open",
    "rule8_fail_open",
    "rule7_block",
    "rule8_block",
    "plan_retro_block",
    "stage6_skipped",
    # 2026-04-26 U3 plan_v4 TU-1: rule-9 Mode-B HMAC self-review governance
    "mode_b_governance_check",
    # 2026-04-26 schema-contract sweep: events emitted by parallel U2/U6
    # sessions but missing from allowlist (writer-reader HARD RULE violation
    # surfaced by test_trace_sink_allowlist self-audit).
    "tombstone_written",
    "tombstone_skipped_idempotent",
    "tombstone_list_cache_hit",
    "tombstone_list_refreshed",
    "tombstone_list_refresh_failed",
    "ingest_encoding_chosen",
    "bench_gate_env_skip",
    "bench_bypass_authorized",
    "coref_injected",
    # autopilot flag lifecycle
    "autopilot_flag_set",
    "autopilot_flag_cleared",
    "autopilot_flag_expired",
    # 2026-04-24 plan_v3: new gate events
    "bootstrap_bypass",
    "bootstrap_bypass_repeated",
    "integration_gate_block",
    "integration_gate_fail_open",
    "integration_gate_result",
    "memory_write_hook_skip",
    "memory_write_hook_spawn",
    "memory_write_hook_error",
    # 2026-04-24 plan_v3 AC-3 schema-contract sweep: pre-existing emitters
    # that violated writer-reader contract (emitted but not listed).
    # Surfaced by tests/test_trace_sink_allowlist.py self-audit.
    "agent_stall_detected",
    "agent_stall_pre_check_error",
    "agent_stall_post_check_error",
    "entity_kind_llm_start",
    "entity_kind_llm_done",
    "entity_kind_llm_cli_missing",
    "entity_kind_llm_subprocess_error",
    "entity_kind_llm_timeout",
    "entity_kind_llm_nonzero_exit",
    "entity_kind_llm_parse_fail",
    "entity_kind_llm_parse_empty",
    "entity_kind_llm_schema_violation",
    "haiku_extract_retry",
    "sandbox_live_verified",
    # 2026-04-24 plan_v4 TU-1 (AC-A3/A4): gates skip budget + HMAC override
    "gate_skipped",      # emitted by _gates_skip_budget.record_skip (AC-A1/A3)
    "gate_infra_error",  # emitted on OSError in budget/used-tokens I/O
    "fallback_to_env",   # gate reader fallback when override file absent but env present
    # 2026-04-24 plan_v4 TU-5 (AC-A4 part-2): override channel audit events
    "override_consumed",  # HMAC token successfully verified + consumed
    "override_invalid",   # override token/file present but invalid
    # 2026-04-24 plan_v4 TU-7 (AC-C1): mini_loop_runner probe lifecycle
    "mini_loop_runner_start",    # probe run begins
    "mini_loop_runner_probe",    # single probe result
    "mini_loop_runner_done",     # probe run complete
    "mini_loop_runner_l2_emitted",  # L2 approval emitted on sync+complex failure
    # 2026-04-24 plan_v4 TU-8 (AC-C3): SessionStart replay
    "session_start_replay_blocked",  # last commit had >=4 skips on complex task
    # 2026-04-24 plan_v4 TU-7b: mini_loop pre-commit hook events
    "pretool_hook_skipped",   # hook fired but did not run probes (non-git / non-complex)
    "pretool_hook_invoked",   # hook fired and is running probes synchronously
    "pretool_hook_blocked",   # hook denied the commit due to probe failure
    # 2026-04-25 plan_v1 (heartbeat audit) TU-D AC-H5: phase3 kill-switch
    "heartbeat_phase3_disabled",  # MEMEXA_HEARTBEAT_PHASE3_DISABLED=1 short-circuit
    # 2026-04-25 plan_v1 (heartbeat audit) TU-A AC-H1: pytest_cache tmpdir lifecycle
    "pytest_cache_tmpdir_leak",
    "pytest_cache_tmpdir_cleanup_error",
    # 2026-04-25 plan_v0 (memory gaps) AC-G2: write_fact lifecycle telemetry
    # Closes 23h silent-failure window between haiku_extract_done and Neo4j land.
    "write_fact_succeeded",
    "write_fact_skipped_no_driver",
    "write_fact_skipped_empty_canon",
    "write_fact_skipped_empty_span",
    "write_fact_skipped_bad_input_type",
    "write_fact_skipped_merge_noop",
    "backfill_ingest_result",
    "extract_rejected",  # AC-G3: dual_llm_extractor reject visibility (was silent before)
    # 2026-04-25 plan_v1 (autopilot enforcement full fix)
    "active_tid_recovered",       # TU-2: session_gate Tier-2 fallback fired
    "reviewer_fallback_triggered",  # TU-5: Mode-A→Mode-B reviewer fallback
    # 2026-04-26 U1 plan_v1: plan_uniformity_check invocation telemetry
    "plan_uniformity_check_invoked",
    # 2026-04-26 U6 bench_runner: continuous benchmark gate events
    "bench_gate_invoked",
    "bench_corpus_completeness_check",
    "bench_gate_decision",
    "bench_gate_skipped",
    # 2026-04-26 U2 plan_v2: PostToolUse hook 切流 + outbox + 7-day audit
    "memory_write_hook_enqueued",
    "memory_write_hook_legacy_fallback",
    "memory_write_hook_kill_switch",
    "outbox_enqueued",
    "outbox_drained_one",
    "outbox_rotated",
    "outbox_dead_letter",
    "outbox_reclaim_zombie",
    "outbox_pid_lock_held",
    "outbox_op_id_fallback",
    "outbox_dir_rejected_unsafe_parent",
    "outbox_drain_rejected_path",
    "outbox_full_refuse",
    "outbox_filelock_timeout",
    "outbox_filelock_missing",
    "outbox_lock_dir_mkdir_failed",
    "outbox_enqueue_dedup",
    "outbox_enqueue_mkdir_failed",
    "outbox_enqueue_write_failed",
    "outbox_pid_dir_rejected",
    "outbox_pid_dir_mkdir_failed",
    "outbox_pid_lock_open_failed",
    "outbox_drain_dir_rejected",
    "outbox_drain_path_check_failed",
    # 2026-04-26 U2 schema-contract sweep: hindsight_outbox.py emits this
    # for NTFS reparse-point rejection during drain, but missed atomic
    # registration. Caught by test_schema_contract_all_jarvis_emissions
    # during U2 regression run — closes feedback_writer_reader_schema_
    # contract HARD RULE for this site.
    "outbox_drain_rejected_reparse_point",
    "outbox_read_failed",
    "outbox_line_missing_keys",
    "outbox_line_bad_status",
    "outbox_line_malformed",
    "outbox_rotate_failed",
    "dual_write_audit_run",
    "pytest_fail_open_authorized",
    # 2026-04-26 U2 plan_v2 (autopilot pipeline rebuild) — atomic
    # registration of all U1-U19 trace events per security-iter1-3
    # fix. Listed BEFORE emit-sites land to prevent writer-reader
    # schema-contract violations (HARD RULE feedback_writer_reader_
    # schema_contract.md).
    # U1: plan_uniformity_check.R19 corpus completeness assertion
    "corpus_completeness_checked",
    # U2: refresh_on_exit decorator firing telemetry
    "stage_refresh_decorator_fired",
    # U3: ceo_approve / session_gate Mode-B self-review governance
    # (mode_b_governance_check already registered above)
    "main_session_detection_ambiguous",
    # U5: plan_v_latest symlink + immutable chmod
    "plan_versioning_symlink_created",
    "plan_versioning_immutable_chmod_failed",
    # U6: hierarchical TU scheduler v2
    "tu_hierarchy_resolved",
    # U7: multi-stage plan + mini-replan trigger
    "mini_replan_triggered",
    "mini_replan_done",
    "mini_replan_stale_context",
    "mini_replan_skipped_due_to_failure_replan",
    "mini_replan_skipped_corrupt_triggers",
    "chmod_failed_post_immutable",
    # U8: phased commit / Stage 5 split
    "phase_commit_done",
    "phase_value_disagreement",
    "phase_audit_pending",
    "orphan_ac_blocked",
    "phase_sentinel_cleared",
    "mode_b_governance_check_phase",
    "lint_cross_phase_file",
    # U9: chunk reviewer + cross-cluster auditor
    "chunk_review_spawned",
    "cross_cluster_audit_done",
    # U10: AC parallel verify + pytest sharding
    "ac_verify_parallel_batch",
    "pytest_shard_done",
    # U11: mid-session checkpoint + /autopilot --resume
    "checkpoint_written",
    "autopilot_resumed",
    # U12: cost telemetry + budget guard
    "cost_recorded",
    "cost_budget_warn",
    "cost_budget_block",
    "cost_record_missing",
    # U12 (2026-04-27 cost_meter): supplemental events
    "cost_meter_unknown_model",
    "cost_meter_log_failed",
    "cost_meter_rotated",
    "cost_meter_rotate_race",
    # U13: failure-cluster backpressure
    "failure_cluster_detected",
    "architect_replan_triggered",
    # U14: BFTS branch survivorship
    "bfts_branch_spawned",
    "bfts_branch_pruned",
    "bfts_winner_selected",
    "bfts_skipped_numeric",
    "bfts_skipped_unknown_class",
    # U15: toy-benchmark physics gate
    "physics_gate_invoked",
    "physics_gate_passed",
    "physics_gate_stub_detected",
    # U16: FunSearch evaluator-pair + cross-model gate
    "evaluator_pair_resolution",
    "cross_model_disagreement",
    "cross_model_block",
    "cross_model_normalized",
    "cross_model_skipped",  # RP-LOGIC-ITER1-3: both OPPOSITE_FAMILY tuple unavailable
    # U17: cross-TU integration matrix
    "integration_matrix_validated",
    # U18: SKILL.md + plan_template super-gate
    "super_gate_passed",
    # U19: briefing schema v2 + improvement_patterns + permanent lessons
    "briefing_schema_v2",
    "improvement_pattern_injected",
    "inherited_lessons_stub",
    # learning_pip (2026-04-30): self-learning pipeline closure
    "plan_retro_record_from_plan_done",
    "plan_retro_parse_failed",
    "plan_retro_parse_warning",
    "pattern_extract_path_b_used",
    "transparency_emit_done",
    "task_type_normalized",
    "r22_lint_violation",
    "plan_retro_dogfood_done",
    "qc_baseline_targeted_done",
    "qc_security_scan_done",
    "qc_cross_pollination_done",
    "qc_stripped_env_done",
    "stage4_review_complete",
    "stage6_status_done",
    # hardrule_gat (2026-04-30): HARD RULE → gate pairing audit
    "hard_rule_audit_done",
    "hard_rule_drift_detected",
    "hard_rule_no_frontmatter",
    "hard_rule_drift_report",
    "hard_rule_backfill_done",
    # Closure A plan_v3 (2026-05-01) TU-1..TU-7 chat-graph events
    "chat_metadata_built",
    "chat_extracted_by_filtered",
    "chat_decay_sweep_complete",
    "chat_invalidation_marker_written",
    "chat_invalidation_filtered",
    "chat_l2_cluster_proposed",
    "chat_l2_audit_truncated",
    "chat_l2_unmerge_applied",
    "chat_health_stats_emitted",
    "graph_maintenance_6h_complete",
    "graph_maintenance_stale_detected",
    "graph_maint_step_failed",
    "schtasks_rewired_to_consolidator",
    # Closure A plan_v4 schema-contract sweep (test_trace_sink_allowlist):
    # backfill pre-existing emitters in memexa/ that were never registered.
    # Sourced from _collect_emitted_events() audit 2026-05-01.
    "wechat_realtime_message",
    "wechat_realtime_event",
    "wechat_db_read",
    "wechat_contacts_loaded",
    "watcher_started",
    "watcher_state_persisted",
    "mlx_lm_invoked",
    "outbox_written",
    "outbox_appended",
    "mac_ui_helper_invoked",
    "mac_ui_osascript_failed",
    "mac_tcc_probe_started",
    "mac_tcc_grant_missing",
    "mac_tcc_post_grant_ok",
    "mac_tcc_popup_count",
    "mac_hindsight_emitted",
    "mac_hindsight_cursor_reset",
    "mac_hindsight_skipped_owned_by_win",
    "mac_hindsight_active_window_unavailable",
    "mac_hindsight_mock_link_down_active",
    "mac_hindsight_outbox",
    "mac_hindsight_pii_blocked",
    "mac_hindsight_ocr_timeout",
    "vision_ocr_recognized",
    "vision_ocr_denied_window",
    "alias_resolved",
    "alias_saved",
    "schedule_polled",
    "entity_pseudonym_minted",
    "entity_pseudonym_revealed",
    "entity_pseudonym_locked",
    "enc_keys_escrowed",
    "chat_extracted",
    "ddl_extracted",
    "email_fetched",
    "keystone_pull_complete",
    "cost_budget_flag_write_failed",
    "wrapper_layer_stub",
    "pytest_targeted_baseline",
    "local_fail_open_authorized",
    "event_name",
    # TU-2 backfill_arc (2026-05-03): cross-model paired_eval events
    # Registered BEFORE emit-sites per feedback_writer_reader_schema_contract.md HARD RULE.
    "paired_eval_call",           # paired_eval.py: start of dual-model call
    "paired_eval_agree",          # paired_eval.py: all triples agreed
    "paired_eval_disagree",       # paired_eval.py: some triples disagreed
    "cross_model_unavailable",    # paired_eval.py + P1 phase: port non-200 fail-loud
    "embedding_service_unavailable_relaxed_disabled",  # Tier-2 disabled (service stale)
    "paired_eval_calibrated",     # calibrate_paired_eval.py: calibration complete
    # TU-Phase0-1/3 paired_eval Phase 0 (2026-05-03): dual mlx_lm.server infra
    # Registered BEFORE emit-sites per feedback_writer_reader_schema_contract.md HARD RULE.
    "mlx_dual_server_launched",   # launch_dual_mlx_server.sh: both servers started
    "gemma_12b_missing",          # launch_dual_mlx_server.sh: 12B not in cache → single server
    "dual_mlx_probe",             # probe_dual_mlx_server.py: tri-state probe result
    # batch_quality_uplift (2026-05-04 plan_v0): 4-layer pipeline events.
    # Registered BEFORE emit-sites per feedback_writer_reader_schema_contract HARD RULE.
    "utterance_merged",                # utterance_merger: Layer A merge applied
    "cut_batches_post_merge",          # batch_chat_extract.main: post-Layer-A cut summary
    "batch_classified",                # batch_classifier: 5-class result + confidence
    "batch_extract_routed",            # batch_chat_extract: type-specific prompt route
    "chat_room_summary_built",         # chat_room_memory_summary: Layer D summary
    "memory_dedup_applied",            # batch_chat_extract: post-hoc cosine/jaccard dedup
    "episode_chain_built",             # episode_chain_builder: Layer E episode_id assignment
    "factrows_episode_id_assigned",    # batch_chat_extract.main: factrow.episode_id wire
    "batch_outbox_enqueued",           # batch_chat_extract.main: outbox file written
    "outbox_write_failed",             # batch_chat_extract: outbox enqueue exception
    "cron_batch_extract_started",      # run_graph_maintenance: batch path step_5 entry
    "cron_batch_extract_done",         # run_graph_maintenance: batch path step_5 exit
    "batch_path_fallback_to_single",   # run_graph_maintenance: batch fail → single fallback
    "acs_live_verified",               # ac_verifier: Stage 6 LIVE-pass summary
    # mac_memory_systemic (2026-05-04 plan_v0): mlx lifecycle + per-batch 27B sync.
    "mlx_lifecycle_already_alive",     # ensure_dual_alive: idempotent both alive
    "mlx_lifecycle_ensure_alive",      # ensure_dual_alive: load actions + result
    "mlx_lifecycle_idle_exit_armed",   # schedule_idle_exit: marker touched
    "mlx_lifecycle_unload",            # unload_dual: symmetric unload result
    "inline_arb_started",              # arbiter_27b_inline: 27B swap engaged
    "inline_arb_completed",            # arbiter_27b_inline: full cycle done
    "inline_arb_swap_failed",          # arbiter_27b_inline: swap_to_27b raise
    "inline_arb_no_pending",           # arbiter_27b_inline: 0 pending early-return
    "batch_inline_arb_invoked",        # batch_chat_extract.main: post-batch trigger
    # P0-1 (2026-05-04): chat_unified → batch_chat_extract subprocess delegation.
    "wechat_ingest_subprocess_error",  # wechat_batch_ingest._extract_and_enqueue: raise
    "wechat_ingest_subprocess_nonzero",# wechat_batch_ingest: subprocess exit !=0
    "wechat_batch_path_result",        # wechat_batch_ingest: 4-layer summary
    "qq_extract_blocked_napcat_or_pipeline_gap", # qq_batch_ingest: typed BLOCK
    # 2026-05-04 backfill safety: prevent silent data loss
    "batches_silently_dropped",        # batch_chat_extract.main: --max-batches cap fired (>0 dropped)
    "batch_split_oversized",           # batch_chat_extract: n_msgs > MAX → split into sub-batches
    "batch_run_aborted_cross_model_streak",  # batch_chat_extract: ≥3 consecutive cross-model fails
    "backfill_preflight_ok",           # preflight_backfill: all checks pass
    "backfill_preflight_failed",       # preflight_backfill: at least 1 check failed
    "arb_drain_loop_iter",             # arbiter_27b_inline.drain_until_empty: per-iter result
    "arb_drain_loop_done",             # arbiter_27b_inline.drain_until_empty: all clear or aborted
    # 2026-05-04 commit 6a62d48 governance drift fix
    "weekly_digest_generated",         # tools/weekly_digest.py: weekly briefing emit
}


def _trace_file() -> Path:
    """Trace path, respects MEMEXA_TRACE_FILE env override (for tests).

    [SEC-HIGH R2 2026-04-19] Path-traversal defense: override must
    resolve to either the workspace tree or the system temp dir.
    Invalid overrides silently fall back to the default _TRACE_FILE.
    """
    override = os.environ.get("MEMEXA_TRACE_FILE")
    if not override:
        return _TRACE_FILE
    try:
        import tempfile as _tf
        candidate = Path(override).resolve()
        workspace_root = _WORKSPACE.resolve()
        tempdir = Path(_tf.gettempdir()).resolve()
        # Must live under workspace OR system temp (for pytest tmpdir)
        is_safe = any(
            str(candidate).startswith(str(allowed) + os.sep) or
            str(candidate) == str(allowed)
            for allowed in (workspace_root, tempdir)
        )
        if not is_safe:
            logger.warning(
                "trace_sink: rejecting MEMEXA_TRACE_FILE outside workspace/temp: %s",
                candidate,
            )
            return _TRACE_FILE
        return candidate
    except Exception as e:
        logger.warning("trace_sink: bad MEMEXA_TRACE_FILE (%s), using default", e)
        return _TRACE_FILE


def _safe_default(o: Any) -> str:
    """[SEC-MED R2 2026-04-19] Replace unknown objects with type-name only
    to avoid leaking __str__/__repr__ content (memory addrs, dict dumps
    with API keys etc). Preserves datetime ISO format for log readability.
    """
    try:
        from datetime import datetime as _dt, date as _d
        if isinstance(o, (_dt, _d)):
            return o.isoformat()
    except Exception:
        pass
    return f"<{type(o).__name__}>"


def _truncate_payload(payload: Any) -> Any:
    """Keep payload under _MAX_PAYLOAD_BYTES when JSON-serialized.

    [R2 2026-04-19] UTF-8 byte-count truncation (not char-count), so
    CJK content doesn't blow through the limit on re-serialization.
    [SEC-MED R2] Uses _safe_default to avoid leaking __str__ content.
    """
    try:
        serialized = json.dumps(payload, ensure_ascii=False, default=_safe_default)
    except Exception:
        return {"_truncated": "unserializable"}
    encoded = serialized.encode("utf-8")
    if len(encoded) <= _MAX_PAYLOAD_BYTES:
        return payload
    # Byte-bounded preview: decode back truncated bytes ignoring mid-char cuts
    preview_bytes = encoded[:_MAX_PAYLOAD_BYTES - 100]
    preview = preview_bytes.decode("utf-8", errors="replace")
    return {"_truncated": True, "preview": preview, "original_bytes": len(encoded)}


_AUTO_ALLOWED_THIS_PROCESS: set = set()  # in-memory to dedup warnings


def _record_pending_review(event: str, payload: Optional[Dict[str, Any]]) -> None:
    """Phase B TU-B5: record unknown event for later codification.

    Writes data/trace_event_pending_review.jsonl (de-dup by event name).
    Best-effort: never raises.
    """
    try:
        pending_path = _WORKSPACE / "memexa" / "data" / "trace_event_pending_review.jsonl"
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        # Read existing to avoid duplicates per event
        seen_events: set = set()
        if pending_path.exists():
            try:
                for line in pending_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if not line.strip():
                        continue
                    try:
                        e = json.loads(line)
                        if e.get("event"):
                            seen_events.add(e["event"])
                    except json.JSONDecodeError:
                        pass
            except OSError:
                pass
        if event in seen_events:
            return  # already recorded
        sample = json.dumps(payload or {}, ensure_ascii=False, default=_safe_default)[:500]
        rec = {
            "event": event,
            "first_seen_ts": datetime.utcnow().isoformat(timespec="microseconds"),
            "sample_payload": sample,
        }
        with open(pending_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # fail-soft


def write_trace_event(
    event: str,
    payload: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
) -> bool:
    """Append one trace event. Returns True on success, False on any error.
    NEVER raises — caller hooks must not break the chain.

    Phase B TU-B5 (2026-05-04): unknown events no longer silently dropped.
    Instead: log warning (deduped per process), write to pending_review.jsonl,
    add to in-memory allowlist, and emit normally. CLI `review-pending` surfaces
    the pending list so CEO can codify into source _ALLOWED_EVENTS.
    """
    if event not in _ALLOWED_EVENTS:
        # First-time pending: log + record + add to in-memory allowlist
        if event not in _AUTO_ALLOWED_THIS_PROCESS:
            logger.warning("trace_sink: unknown event %r (auto-allowed in-memory; "
                           "use 'python -m memexa.core.trace_sink review-pending' to review)",
                           event)
            _record_pending_review(event, payload)
            _AUTO_ALLOWED_THIS_PROCESS.add(event)
            _ALLOWED_EVENTS.add(event)  # in-memory only; source code unchanged
            # Emit special marker (only first time per process)
            try:
                sample = json.dumps(payload or {}, ensure_ascii=False, default=_safe_default)[:200]
                fp_marker = _trace_file()
                fp_marker.parent.mkdir(parents=True, exist_ok=True)
                marker_record = {
                    "ts": datetime.utcnow().isoformat(timespec="microseconds"),
                    "session_id": str(session_id or os.environ.get("CLAUDE_SESSION_ID") or "unknown")[:128],
                    "event": "trace_event_auto_allowed",  # ensure in allowlist
                    "payload": {"unknown_event": event, "sample": sample},
                }
                if "trace_event_auto_allowed" not in _ALLOWED_EVENTS:
                    _ALLOWED_EVENTS.add("trace_event_auto_allowed")  # bootstrap
                with open(fp_marker, "a", encoding="utf-8") as fm:
                    fm.write(json.dumps(marker_record, ensure_ascii=False, default=_safe_default) + "\n")
            except Exception:
                pass

    # [SEC-LOW R2] Bound session_id to avoid runaway lines via CLAUDE_SESSION_ID
    sid_raw = session_id or os.environ.get("CLAUDE_SESSION_ID") or "unknown"
    sid = str(sid_raw)[:128]
    payload = _truncate_payload(payload or {})

    record = {
        "ts": datetime.utcnow().isoformat(timespec="microseconds"),
        "session_id": sid,
        "event": event,
        "payload": payload,
    }

    try:
        fp = _trace_file()
        fp.parent.mkdir(parents=True, exist_ok=True)
        # [R2 2026-04-19] Atomicity: POSIX O_APPEND atomicity only holds
        # for writes <= PIPE_BUF and on Linux. Windows filesystems don't
        # guarantee this. Use a filelock to protect concurrent Claude
        # Code session writes. Falls back to non-locked append if lib
        # unavailable — self-healing read_traces skips malformed lines.
        line = json.dumps(record, ensure_ascii=False, default=_safe_default) + "\n"
        lock = _get_lock(fp)
        if lock is not None:
            with lock:
                with open(fp, "a", encoding="utf-8") as f:
                    f.write(line)
        else:
            with open(fp, "a", encoding="utf-8") as f:
                f.write(line)
        return True
    except Exception as e:
        logger.warning("trace_sink write failed: %s", e)
        return False


def _get_lock(fp: Path):
    """Returns a filelock for the trace file, or None if lib missing."""
    try:
        from filelock import FileLock
        return FileLock(str(fp) + ".lock", timeout=2.0)
    except ImportError:
        return None
    except Exception:
        return None


def read_traces(
    since_iso: Optional[str] = None,
    event_filter: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
) -> list:
    """Read back traces for analysis. Returns list of dicts in write order.

    [R2 2026-04-19] Docstring clarified: returns write order (oldest first
    within the slice). `limit` keeps last N written (most recent N).

    Args:
        since_iso: only return events with ts >= this ISO timestamp.
            Strips "Z" suffix if present — ISO strings compared lexically
            require consistent formatting (see _normalize_since_iso).
        event_filter: iterable of event types to include
        limit: keep last N records after filtering
    """
    fp = _trace_file()
    if not fp.exists():
        return []
    events = set(event_filter) if event_filter else None
    since_norm = _normalize_since_iso(since_iso) if since_iso else None
    out = []
    try:
        for line in fp.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # [COV-HIGH-1 R2] Corrupt line → skip, never block valid
                # records from loading.
                continue
            if since_norm:
                # 2026-05-04 LIVE-fix (B-2 dependency): rec.get("ts") may be
                # str (ISO) OR float (epoch) depending on writer path. Coerce
                # both sides to str for lex compare. Mismatched-type compare
                # used to raise TypeError silently swallowed by outer except,
                # killing the whole read for assert_expected_trace.
                rec_ts = rec.get("ts", "")
                if isinstance(rec_ts, (int, float)):
                    try:
                        from datetime import datetime as _dt
                        rec_ts = _dt.utcfromtimestamp(float(rec_ts)).isoformat()
                    except Exception:
                        rec_ts = ""
                if rec_ts < since_norm:
                    continue
            if events and rec.get("event") not in events:
                continue
            out.append(rec)
    except Exception as e:
        logger.warning("trace_sink read failed: %s", e)
        return []
    if limit:
        out = out[-limit:]
    return out


def _normalize_since_iso(s: str) -> str:
    """[LOG-MED R2] Strip trailing 'Z' and normalize microseconds so
    string comparison works consistently.

    Records stored as "2026-04-19T10:00:00.000123" (no tz). Callers
    passing "2026-04-19T10:00:00Z" must have the Z stripped or lexical
    compare fails (ord 'Z' > ord '.').
    """
    if not s:
        return s
    out = s.rstrip("Z")
    # If no microseconds, append .000000 so lexical compare aligns with
    # written records which always include microseconds.
    if "." not in out and len(out) >= 19:
        out = out + ".000000"
    return out


def _cli():
    """CLI entry: `python -m memexa.core.trace_sink <command> [args...]`

    Commands:
      tail [N]              show last N events (default 20)
      since <iso>           show events since timestamp
      count                 total events
      stats                 counts by event type
      write <event> <json>  write a single event (for hooks)
    """
    if len(sys.argv) < 2:
        print("usage: trace_sink <tail|since|count|stats|write> [args]", file=sys.stderr)
        return 1
    cmd = sys.argv[1]
    if cmd == "tail":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        for r in read_traces(limit=n):
            print(json.dumps(r, ensure_ascii=False))
        return 0
    if cmd == "since":
        if len(sys.argv) < 3:
            print("since requires ISO timestamp", file=sys.stderr)
            return 2
        for r in read_traces(since_iso=sys.argv[2]):
            print(json.dumps(r, ensure_ascii=False))
        return 0
    if cmd == "count":
        print(len(read_traces()))
        return 0
    if cmd == "stats":
        counts = {}
        for r in read_traces():
            counts[r.get("event", "?")] = counts.get(r.get("event", "?"), 0) + 1
        for k, v in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"{k}: {v}")
        return 0
    if cmd == "review-pending":
        # Phase B TU-B5: surface auto-allowed unknown events for codification
        pending_path = _WORKSPACE / "memexa" / "data" / "trace_event_pending_review.jsonl"
        if not pending_path.exists():
            print("(no pending review entries)")
            return 0
        entries = []
        for line in pending_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        print(f"=== {len(entries)} pending events awaiting codification into _ALLOWED_EVENTS ===")
        for e in entries:
            ev = e.get("event", "?")
            ts = e.get("first_seen_ts", "?")
            sample = e.get("sample_payload", "")[:80]
            print(f"  [{ts[:19]}] {ev}\n    sample: {sample}")
        print(f"\nTo codify: edit memexa/core/trace_sink.py _ALLOWED_EVENTS set "
              f"and add the event names above. Then delete data/trace_event_pending_review.jsonl.")
        return 0
    if cmd == "write":
        if len(sys.argv) < 4:
            print("write requires <event> <json>", file=sys.stderr)
            return 2
        event = sys.argv[2]
        try:
            payload = json.loads(sys.argv[3])
        except json.JSONDecodeError as e:
            print(f"bad JSON: {e}", file=sys.stderr)
            return 2
        ok = write_trace_event(event, payload)
        return 0 if ok else 3
    print(f"unknown command {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
