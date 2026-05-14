"""
Super Strategist — KAIROS 的战略大脑。

与 TaskBrain 的区别:
  - TaskBrain: 被动/防御性（测试坏了修、错误多了查）
  - SuperStrategist: 主动/前瞻性（综合多渠道信号，自主决定系统进化方向）

数据源 (11 路感知):
  内部系统:
    S1: strategic-advisor 历史建议（big_loop Q6 输出）
    S2: evolution 健康趋势 + pattern 缺口
    S3: agent 效能分析（谁做得好、谁做得差）
    S4: API 用量 + 成本分析（Kimi/Claude 调用模式）
    S5: heartbeat ��史（系统稳定性趋势）
  外部信号:
    S6: 用户渠道消息（WeChat/QQ 最近消息中的需求/反馈）
    S7: dashboard 使用模式（CEO 关注什么）
    S8: 日程/DDL 上下文（用户当前压力和优先级）
  进化反馈:
    S9: KAIROS 项目历史成效（什么类型的任务 ROI 最高）
    S10: 未覆盖领域检测（系统能力缺口）
  质量审计:
    S11: Dashboard UX 质量（硬编码字符串/Loading卡死/空数据/静默错误）

输出:
  生成 KAIROS 项目列表，按 ROI 排序，包含战略理由。
  不是"修 bug"而是"系统应该向什么方向进化"。

调用时机:
  - heartbeat Phase 4.5（每 6 小时）
  - daemon on_queue_empty（队列空时代替 TaskBrain.generate_projects）
  - 手动: python -m memexa.core.super_strategist
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MEMEXA_ROOT = Path(__file__).parent.parent.parent
_WORKSPACE = _MEMEXA_ROOT.parent
_DATA = Path(__file__).parent.parent / "data"


class SuperStrategist:
    """KAIROS 的战略大脑。综合 11 路信号自主决定进化方向。"""

    def __init__(self):
        self._signals: Dict[str, Any] = {}

    # ==============================================================
    # SENSE: 10 路信号采集
    # ==============================================================

    def _s1_strategic_history(self) -> List[Dict]:
        """S1: strategic-advisor 历史建议。"""
        results = []
        # 从 big_loop 结果中提取战略建议
        for f in sorted(_DATA.glob("big_loop_*.json"), reverse=True)[:3]:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                specs = data.get("agent_specs", [])
                for s in specs:
                    if s.get("agent") == "strategic-advisor":
                        results.append({
                            "source": f.stem,
                            "prompt": s.get("prompt", "")[:500],
                            "timestamp": data.get("timestamp", ""),
                        })
            except Exception:
                continue

        # 从最新 briefing 中提取
        briefing_file = _DATA / "latest_briefing.md"
        if briefing_file.exists():
            try:
                content = briefing_file.read_text(encoding="utf-8")[:2000]
                results.append({"source": "cto_briefing", "content": content})
            except Exception:
                pass

        return results

    def _s2_evolution_gaps(self) -> Dict:
        """S2: evolution 健康趋势 + pattern 缺口。"""
        result = {"patterns": 0, "health_trend": [], "gaps": []}

        # Pattern store
        pf = _DATA / "semantic_patterns.json"
        if pf.exists():
            try:
                data = json.loads(pf.read_text(encoding="utf-8"))
                result["patterns"] = data.get("pattern_count", 0)
                result["episodes_pending"] = data.get("episodes_since_consolidation", 0)
                # Analyze pattern coverage — what topics are NOT covered
                covered_tags = set()
                for p in data.get("patterns", {}).values():
                    covered_tags.update(t.lower() for t in p.get("tags", []))
                result["covered_tags"] = list(covered_tags)
            except Exception:
                pass

        # Evolution metrics trend
        mf = _DATA / "evolution_metrics.json"
        if mf.exists():
            try:
                data = json.loads(mf.read_text(encoding="utf-8"))
                snapshots = data.get("snapshots", [])[-10:]
                result["health_trend"] = [
                    {"ts": s["timestamp"], "score": s["health_score"]}
                    for s in snapshots
                ]
            except Exception:
                pass

        return result

    def _s3_agent_performance(self) -> Dict[str, Dict]:
        """S3: agent 效能分析。"""
        agents = {}
        ff = _DATA / "kairos_feedback.jsonl"
        if not ff.exists():
            return agents

        try:
            for line in ff.read_text(encoding="utf-8").strip().splitlines():
                try:
                    entry = json.loads(line)
                    role = entry.get("agent_role", "unknown")
                    if role not in agents:
                        agents[role] = {"total": 0, "success": 0, "total_quality": 0,
                                       "total_cost": 0, "titles": []}
                    agents[role]["total"] += 1
                    if entry.get("success"):
                        agents[role]["success"] += 1
                    agents[role]["total_quality"] += entry.get("quality_score", 0)
                    agents[role]["total_cost"] += entry.get("cost_usd", 0)
                    agents[role]["titles"].append(entry.get("title", "")[:60])
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass

        # Compute averages
        for role, data in agents.items():
            data["avg_quality"] = round(data["total_quality"] / max(data["total"], 1), 2)
            data["success_rate"] = round(data["success"] / max(data["total"], 1), 2)
            data["avg_cost"] = round(data["total_cost"] / max(data["total"], 1), 3)

        return agents

    def _s4_api_usage(self) -> Dict:
        """S4: API 用量 + 成本分析。"""
        usage = {"kimi_calls": 0, "claude_cost": 0, "kimi_cost_estimate": 0}

        # Count Kimi API calls from events
        ef = _DATA / "events.jsonl"
        if ef.exists():
            try:
                for line in ef.read_text(encoding="utf-8").strip().splitlines()[-200:]:
                    try:
                        e = json.loads(line)
                        if "kimi" in str(e).lower() or "moonshot" in str(e).lower():
                            usage["kimi_calls"] += 1
                    except json.JSONDecodeError:
                        continue
            except Exception:
                pass

        # Claude cost from KAIROS feedback
        ff = _DATA / "kairos_feedback.jsonl"
        if ff.exists():
            try:
                for line in ff.read_text(encoding="utf-8").strip().splitlines():
                    try:
                        entry = json.loads(line)
                        usage["claude_cost"] += entry.get("cost_usd", 0)
                    except json.JSONDecodeError:
                        continue
            except Exception:
                pass

        usage["claude_cost"] = round(usage["claude_cost"], 2)
        return usage

    def _s5_heartbeat_history(self) -> List[Dict]:
        """S5: heartbeat 历史（稳定性趋势）。"""
        log_dir = _DATA / "log"
        entries = []
        if not log_dir.exists():
            return entries

        # Read recent daily logs
        for f in sorted(log_dir.glob("*.md"), reverse=True)[:3]:
            try:
                content = f.read_text(encoding="utf-8")
                entries.append({"date": f.stem, "content": content[:1000]})
            except Exception:
                continue

        return entries

    def _s6_user_channels(self) -> Dict:
        """S6: 用户渠道消息（WeChat/QQ 最近内容）。"""
        channels = {"wechat": None, "qq": None}

        # Check chat_history.db for recent messages
        chat_db = _DATA / "chat_history.db"
        if chat_db.exists():
            try:
                import sqlite3
                conn = sqlite3.connect(str(chat_db))
                cursor = conn.cursor()
                # Get recent messages
                cursor.execute(
                    "SELECT sender, content, timestamp FROM messages "
                    "ORDER BY timestamp DESC LIMIT 20"
                )
                rows = cursor.fetchall()
                channels["recent_messages"] = [
                    {"sender": r[0], "content": r[1][:200], "ts": r[2]}
                    for r in rows
                ]
                conn.close()
            except Exception as e:
                channels["chat_db_error"] = str(e)

        # Check WeChat DB for latest messages
        try:
            from memexa.reader import read_latest_messages
            channels["wechat"] = "reader module available"
        except Exception:
            channels["wechat"] = "reader unavailable"

        # Check QQ
        try:
            from memexa.qq_reader import read_latest_messages
            channels["qq"] = "qq_reader available"
        except Exception:
            channels["qq"] = "qq_reader unavailable"

        return channels

    def _s7_schedule_context(self) -> Dict:
        """S7: 日程/DDL 上下文。"""
        schedule_file = _WORKSPACE / "schedule_data.json"
        ctx = {"ddls": [], "events_today": [], "stress_level": "unknown"}

        if not schedule_file.exists():
            return ctx

        try:
            data = json.loads(schedule_file.read_text(encoding="utf-8"))
            today = datetime.now().strftime("%Y-%m-%d")

            # Find upcoming DDLs (within 7 days)
            for event in data.get("events", []):
                if event.get("type") == "ddl":
                    ddl_date = event.get("date", "")
                    if ddl_date >= today:
                        ctx["ddls"].append({
                            "title": event.get("title", ""),
                            "date": ddl_date,
                        })
                if event.get("date") == today:
                    ctx["events_today"].append(event.get("title", ""))

            ctx["ddls"] = ctx["ddls"][:10]
            ctx["events_today"] = ctx["events_today"][:10]
        except Exception:
            pass

        return ctx

    def _s8_project_roi(self) -> List[Dict]:
        """S8: KAIROS 项目历史成效（什么类型任务 ROI 最高）。"""
        roi = {}
        ff = _DATA / "kairos_feedback.jsonl"
        if not ff.exists():
            return []

        try:
            for line in ff.read_text(encoding="utf-8").strip().splitlines():
                try:
                    entry = json.loads(line)
                    # Categorize by task type
                    title = entry.get("title", "")
                    if "FIX:" in title:
                        cat = "bug_fix"
                    elif "TEST:" in title:
                        cat = "test_writing"
                    elif "DASHBOARD:" in title:
                        cat = "dashboard"
                    elif "BigLoop" in title:
                        cat = "quality_cycle"
                    elif "EVOLVE:" in title:
                        cat = "evolution"
                    else:
                        cat = "other"

                    if cat not in roi:
                        roi[cat] = {"count": 0, "total_quality": 0, "total_cost": 0,
                                   "successes": 0}
                    roi[cat]["count"] += 1
                    roi[cat]["total_quality"] += entry.get("quality_score", 0)
                    roi[cat]["total_cost"] += entry.get("cost_usd", 0)
                    if entry.get("success"):
                        roi[cat]["successes"] += 1
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass

        # Compute ROI = quality / cost
        results = []
        for cat, data in roi.items():
            avg_q = data["total_quality"] / max(data["count"], 1)
            avg_c = data["total_cost"] / max(data["count"], 1)
            roi_score = avg_q / max(avg_c, 0.01)  # quality per dollar
            results.append({
                "category": cat,
                "count": data["count"],
                "avg_quality": round(avg_q, 2),
                "avg_cost": round(avg_c, 3),
                "success_rate": round(data["successes"] / max(data["count"], 1), 2),
                "roi_score": round(roi_score, 2),
            })

        results.sort(key=lambda x: -x["roi_score"])
        return results

    def _s9_capability_gaps(self) -> List[str]:
        """S9: 系统能力缺口检测。"""
        gaps = []

        # Check which core modules have no tests
        core = _MEMEXA_ROOT / "memexa" / "core"
        tests = _MEMEXA_ROOT / "tests"
        if core.exists() and tests.exists():
            tested = {f.stem.replace("test_", "") for f in tests.glob("test_*.py")}
            untested = [f.stem for f in sorted(core.glob("*.py"))
                       if f.name != "__init__.py" and f.stem not in tested]
            if untested:
                gaps.append(f"Untested modules ({len(untested)}): {', '.join(untested[:5])}")

        # Check dashboard API coverage vs data available
        dashboard_file = _MEMEXA_ROOT / "memexa" / "api" / "dashboard_server.py"
        if dashboard_file.exists():
            content = dashboard_file.read_text(encoding="utf-8")
            has_social = "wechat" in content.lower() or "message" in content.lower()
            has_api_usage = "api_usage" in content.lower() or "llm_router" in content.lower()
            has_heartbeat_detail = "heartbeat_history" in content.lower()
            has_agent_logs = "agent_log" in content.lower() or "project_reports" in content.lower()

            if not has_social:
                gaps.append("Dashboard missing: social channel integration (WeChat/QQ messages)")
            if not has_api_usage:
                gaps.append("Dashboard missing: API usage/cost analytics")
            if not has_heartbeat_detail:
                gaps.append("Dashboard missing: heartbeat history & detail view")
            if not has_agent_logs:
                gaps.append("Dashboard missing: agent work log viewer")

        return gaps

    def _sense_dashboard_ux(self) -> Dict:
        """S11: Dashboard UX quality audit.

        Static analysis of dashboard.html and dashboard_server.py to detect:
        - Hardcoded strings not using i18n (data-i18n / t())
        - Sections that may show 'Loading...' indefinitely
        - Render functions missing null/empty data guards
        - API endpoints with silent error handling
        """
        import re

        result: Dict[str, Any] = {
            "hardcoded_strings": [],
            "stuck_loading": [],
            "missing_null_guards": [],
            "silent_error_endpoints": [],
        }

        # --- Audit dashboard.html ---
        html_file = _MEMEXA_ROOT / "memexa" / "api" / "static" / "dashboard.html"
        if not html_file.exists():
            result["html_missing"] = True
            return result

        try:
            html = html_file.read_text(encoding="utf-8")
        except Exception:
            result["html_read_error"] = True
            return result

        html_lines = html.splitlines()

        # 1) Hardcoded user-visible strings not using i18n
        #    Match HTML text content between > and < that is not inside <script>/<style>
        in_script = False
        in_style = False
        i18n_pattern = re.compile(r'data-i18n')
        # Strings that are obviously not user-facing
        skip_tokens = {"", "memexa", "CTO", "CEO", "KAIROS", "API", "DDL",
                       "A/B", "SVG", "UTF-8"}

        for lineno, line in enumerate(html_lines, 1):
            stripped = line.strip()
            if stripped.startswith("<script"):
                in_script = True
            if stripped.startswith("</script"):
                in_script = False
                continue
            if stripped.startswith("<style"):
                in_style = True
            if stripped.startswith("</style"):
                in_style = False
                continue
            if in_script or in_style:
                continue

            # Find text content in HTML tags: >visible text<
            texts = re.findall(r'>([^<>{]+)<', line)
            for txt in texts:
                txt_clean = txt.strip()
                if not txt_clean or len(txt_clean) < 2:
                    continue
                # Skip CSS values, JS expressions, attribute-like content
                if txt_clean.startswith('{') or txt_clean.startswith('{{'):
                    continue
                if txt_clean in skip_tokens:
                    continue
                # Check if the tag has data-i18n
                if i18n_pattern.search(line):
                    continue
                # Only flag strings that look like real words (contain letters)
                if not re.search(r'[a-zA-Z\u4e00-\u9fff]', txt_clean):
                    continue
                result["hardcoded_strings"].append({
                    "line": lineno,
                    "text": txt_clean[:80],
                })

        # 2) Loading... sections that may get stuck
        #    Find divs with "Loading..." that are NOT replaced by a render call
        #    Skip <script>/<style> blocks (JS/CSS content is not user-facing)
        loading_pattern = re.compile(r'id=["\']([^"\']+)["\']')
        in_script_loading = False
        in_style_loading = False
        for lineno, line in enumerate(html_lines, 1):
            stripped_l = line.strip()
            if stripped_l.startswith("<script"):
                in_script_loading = True
            if stripped_l.startswith("</script"):
                in_script_loading = False
                continue
            if stripped_l.startswith("<style"):
                in_style_loading = True
            if stripped_l.startswith("</style"):
                in_style_loading = False
                continue
            if in_script_loading or in_style_loading:
                continue
            if "Loading..." in line:
                id_match = loading_pattern.search(line)
                panel_id = id_match.group(1) if id_match else None
                has_i18n = "data-i18n" in line
                if not has_i18n:
                    result["stuck_loading"].append({
                        "line": lineno,
                        "panel_id": panel_id,
                        "reason": "Loading... without data-i18n fallback",
                    })

        # 3) Render functions missing null/empty data guards
        #    Check if render functions have early return or fallback for empty data
        render_fn_re = re.compile(r'function\s+(render\w+)\s*\(')
        current_fn = None
        fn_start = 0
        fn_lines: List[str] = []

        for lineno, line in enumerate(html_lines, 1):
            m = render_fn_re.search(line)
            if m:
                # Check previous function
                if current_fn and fn_lines:
                    body = "\n".join(fn_lines[:15])  # First 15 lines
                    has_guard = any(kw in body for kw in [
                        "if (!",  "if(!",  "|| {", "|| []",
                        "?.", "empty", "no data", "no_",
                        ".length === 0", ".length==0",
                    ])
                    if not has_guard:
                        result["missing_null_guards"].append({
                            "function": current_fn,
                            "line": fn_start,
                        })
                current_fn = m.group(1)
                fn_start = lineno
                fn_lines = []
            elif current_fn:
                fn_lines.append(line)

        # Check last function
        if current_fn and fn_lines:
            body = "\n".join(fn_lines[:15])
            has_guard = any(kw in body for kw in [
                "if (!",  "if(!",  "|| {", "|| []",
                "?.", "empty", "no data", "no_",
                ".length === 0", ".length==0",
            ])
            if not has_guard:
                result["missing_null_guards"].append({
                    "function": current_fn,
                    "line": fn_start,
                })

        # --- Audit dashboard_server.py ---
        server_file = _MEMEXA_ROOT / "memexa" / "api" / "dashboard_server.py"
        if not server_file.exists():
            result["server_missing"] = True
            return result

        try:
            server_src = server_file.read_text(encoding="utf-8")
        except Exception:
            result["server_read_error"] = True
            return result

        server_lines = server_src.splitlines()

        # 4) API endpoints that may return errors silently
        #    Find @app.get/post handlers and check if they have try/except
        #    If file-level middleware handles errors, skip per-endpoint checks
        has_global_error_middleware = (
            "catch_exceptions_middleware" in server_src
            or ("@app.middleware" in server_src and '"error"' in server_src
                and "except" in server_src)
        )
        if has_global_error_middleware:
            # Global middleware handles errors for all endpoints
            result["summary"] = {
                "hardcoded_string_count": len(result["hardcoded_strings"]),
                "stuck_loading_count": len(result["stuck_loading"]),
                "missing_null_guard_count": len(result["missing_null_guards"]),
                "silent_error_count": 0,
                "endpoints_checked": 0,
                "total_issues": (len(result["hardcoded_strings"])
                                 + len(result["stuck_loading"])
                                 + len(result["missing_null_guards"])),
            }
            return result

        endpoint_re = re.compile(r'@app\.(get|post|put|delete)\s*\(\s*["\']([^"\']+)')
        current_endpoint = None
        ep_start = 0
        ep_body_lines: List[str] = []
        endpoints_checked = 0

        for lineno, line in enumerate(server_lines, 1):
            m = endpoint_re.search(line)
            if m:
                # Check previous endpoint
                if current_endpoint and ep_body_lines:
                    endpoints_checked += 1
                    body = "\n".join(ep_body_lines)
                    has_try = "try:" in body
                    has_http_exc = "HTTPException" in body
                    has_except = "except" in body
                    returns_error_field = '"error"' in body or "'error'" in body

                    if not has_try and not has_http_exc and not returns_error_field:
                        result["silent_error_endpoints"].append({
                            "method": current_endpoint[0],
                            "path": current_endpoint[1],
                            "line": ep_start,
                            "reason": "No try/except or HTTPException",
                        })
                    elif has_try and has_except and not has_http_exc and not returns_error_field:
                        # Has try/except but silently swallows errors
                        if "return {}" in body or "return []" in body or "return None" in body:
                            result["silent_error_endpoints"].append({
                                "method": current_endpoint[0],
                                "path": current_endpoint[1],
                                "line": ep_start,
                                "reason": "Catches exceptions but returns empty response silently",
                            })

                current_endpoint = (m.group(1), m.group(2))
                ep_start = lineno
                ep_body_lines = []
            elif current_endpoint:
                ep_body_lines.append(line)

        # Check last endpoint
        if current_endpoint and ep_body_lines:
            endpoints_checked += 1
            body = "\n".join(ep_body_lines)
            has_try = "try:" in body
            has_http_exc = "HTTPException" in body
            has_except = "except" in body
            returns_error_field = '"error"' in body or "'error'" in body

            if not has_try and not has_http_exc and not returns_error_field:
                result["silent_error_endpoints"].append({
                    "method": current_endpoint[0],
                    "path": current_endpoint[1],
                    "line": ep_start,
                    "reason": "No try/except or HTTPException",
                })
            elif has_try and has_except and not has_http_exc and not returns_error_field:
                if "return {}" in body or "return []" in body or "return None" in body:
                    result["silent_error_endpoints"].append({
                        "method": current_endpoint[0],
                        "path": current_endpoint[1],
                        "line": ep_start,
                        "reason": "Catches exceptions but returns empty response silently",
                    })

        result["summary"] = {
            "hardcoded_string_count": len(result["hardcoded_strings"]),
            "stuck_loading_count": len(result["stuck_loading"]),
            "missing_null_guard_count": len(result["missing_null_guards"]),
            "silent_error_count": len(result["silent_error_endpoints"]),
            "endpoints_checked": endpoints_checked,
            "total_issues": (
                len(result["hardcoded_strings"])
                + len(result["stuck_loading"])
                + len(result["missing_null_guards"])
                + len(result["silent_error_endpoints"])
            ),
        }

        return result

    # ==============================================================
    # SYNTHESIZE: 综合判断 → 生成战略任务
    # ==============================================================

    def sense_all(self) -> Dict[str, Any]:
        """采集所有 11 路信号。"""
        self._signals = {
            "s1_strategic_history": self._s1_strategic_history(),
            "s2_evolution_gaps": self._s2_evolution_gaps(),
            "s3_agent_performance": self._s3_agent_performance(),
            "s4_api_usage": self._s4_api_usage(),
            "s5_heartbeat_history": self._s5_heartbeat_history(),
            "s6_user_channels": self._s6_user_channels(),
            "s7_schedule_context": self._s7_schedule_context(),
            "s8_project_roi": self._s8_project_roi(),
            "s9_capability_gaps": self._s9_capability_gaps(),
            "s11_dashboard_ux": self._sense_dashboard_ux(),
        }
        return self._signals

    def generate_strategic_tasks(self, budget_minutes: int = 120) -> List[Dict]:
        """综合所有信号，生成 ROI 排序的战略任务。

        与 TaskBrain.generate_projects 的区别:
          - TaskBrain: 只看内部状态，生成防御性任务（修/查/测）
          - SuperStrategist: 看全局，生成进化性任务（建/集成/优化）

        Returns:
            List of KAIROS project dicts, sorted by strategic priority.
        """
        if not self._signals:
            self.sense_all()

        tasks = []

        # --- 基于 S9 能力缺口生成任务 ---
        gaps = self._signals.get("s9_capability_gaps", [])
        for i, gap in enumerate(gaps):
            if "social channel" in gap.lower():
                tasks.append({
                    "title": "STRATEGIC: Dashboard social channel integration (WeChat/QQ feed)",
                    "prompt": (
                        "Integrate WeChat and QQ message feeds into the memexa CEO Dashboard.\n\n"
                        "## Available Data Sources\n"
                        "- memexa/data/chat_history.db (SQLite, recent messages)\n"
                        "- memexa/reader.py (WeChat message reader)\n"
                        "- memexa/qq_reader.py (QQ message reader)\n"
                        "- memexa/hooks/history_manager.py (message history)\n\n"
                        "## Requirements\n"
                        "1. New dashboard section: 'Social Feed' showing recent messages\n"
                        "2. Backend: GET /api/social-feed reading from chat_history.db\n"
                        "3. Show: sender, content preview, timestamp, platform (WeChat/QQ icon)\n"
                        "4. Highlight messages containing keywords: DDL, urgent, deadline, help\n"
                        "5. Click to expand full message\n"
                        "6. Filter by platform (All / WeChat / QQ)\n\n"
                        "Read existing code first. Follow dark theme + i18n pattern."
                    ),
                    "priority": 5,
                    "strategic_reason": "CEO needs visibility into user channel signals",
                    "estimate_min": 15,
                })
            elif "API usage" in gap:
                tasks.append({
                    "title": "STRATEGIC: Dashboard API usage analytics panel",
                    "prompt": (
                        "Add API usage/cost analytics to the memexa CEO Dashboard.\n\n"
                        "## Data Sources\n"
                        "- memexa/core/llm_router.py has get_stats() with model_usage, cost\n"
                        "- memexa/data/events.jsonl has API call events\n"
                        "- memexa/data/kairos_feedback.jsonl has per-project cost\n\n"
                        "## Requirements\n"
                        "1. New section: 'API Usage' with:\n"
                        "   - Total calls by model (Kimi 8k/32k/128k, Claude Opus/Sonnet)\n"
                        "   - Cost breakdown: pie chart (SVG) by model\n"
                        "   - Daily cost trend: bar chart (last 7 days)\n"
                        "   - Per-project cost table (from kairos_feedback)\n"
                        "2. Backend: GET /api/api-usage aggregating from llm_router stats + events\n"
                        "3. Show budget remaining if configured\n"
                        "4. Highlight cost anomalies (>$5 single project)\n\n"
                        "Read existing code. Dark theme + i18n."
                    ),
                    "priority": 4,
                    "strategic_reason": "Cost visibility prevents budget overrun",
                    "estimate_min": 15,
                })
            elif "heartbeat history" in gap:
                tasks.append({
                    "title": "STRATEGIC: Dashboard heartbeat history & detail view",
                    "prompt": (
                        "Add heartbeat monitoring detail to memexa CEO Dashboard.\n\n"
                        "## Data Sources\n"
                        "- memexa/data/log/YYYY-MM-DD.md (daily heartbeat logs)\n"
                        "- memexa/data/kairos_heartbeat.json (current status)\n"
                        "- memexa/data/evolution_metrics.json (health snapshots)\n\n"
                        "## Requirements\n"
                        "1. New section: 'Heartbeat Monitor' showing:\n"
                        "   - Timeline of heartbeat events (last 24h)\n"
                        "   - Each beat: verdict, phase reached, duration, actions taken\n"
                        "   - Color: green(OK), blue(ACTIONS), yellow(STRATEGIC), red(ERROR)\n"
                        "2. Expandable detail per heartbeat showing full log entry\n"
                        "3. Heartbeat uptime indicator (how long since last beat)\n"
                        "4. Backend: GET /api/heartbeat-history parsing daily logs\n\n"
                        "Read existing code. Dark theme + i18n."
                    ),
                    "priority": 4,
                    "strategic_reason": "System reliability needs continuous monitoring",
                    "estimate_min": 15,
                })
            elif "agent work log" in gap.lower():
                tasks.append({
                    "title": "STRATEGIC: Dashboard agent execution log viewer",
                    "prompt": (
                        "Add agent work detail viewer to memexa CEO Dashboard.\n\n"
                        "## Data Sources\n"
                        "- memexa/data/project_reports/proj_*.json (full execution reports)\n"
                        "- memexa/data/kairos_feedback.jsonl (scored summaries)\n"
                        "- memexa/data/events.jsonl (agent events)\n\n"
                        "## Requirements\n"
                        "1. Enhance existing Projects panel to show detailed execution:\n"
                        "   - Full output text (expandable, with syntax highlighting)\n"
                        "   - Git diff summary if commit was made\n"
                        "   - Files modified list\n"
                        "   - Pipeline phases completed (A/B/C/D badges)\n"
                        "   - Review findings summary\n"
                        "2. Agent detail page (click agent card → see all their executions)\n"
                        "3. Backend: enhance /api/projects/{id} to include full report data\n"
                        "4. Log viewer: GET /api/agent-log/{agent_name} returning execution history\n\n"
                        "Read existing code. Dark theme + i18n."
                    ),
                    "priority": 4,
                    "strategic_reason": "Agent transparency needed for trust and debugging",
                    "estimate_min": 15,
                })

        # --- 基于 S3 agent 效能分析 ---
        agents = self._signals.get("s3_agent_performance", {})
        low_performers = [
            (role, data) for role, data in agents.items()
            if data["total"] >= 3 and data["avg_quality"] < 3.5
        ]
        if low_performers:
            details = "\n".join(
                f"- {role}: avg_quality={d['avg_quality']}, success_rate={d['success_rate']}"
                for role, d in low_performers
            )
            tasks.append({
                "title": f"STRATEGIC: Improve underperforming agents ({len(low_performers)})",
                "prompt": (
                    f"These agents are underperforming:\n{details}\n\n"
                    "Analyze their execution history in kairos_feedback.jsonl. "
                    "Identify root causes and improve their .claude/agents/*.md prompts."
                ),
                "priority": 3,
                "strategic_reason": "Low-performing agents waste resources",
                "estimate_min": 20,
            })

        # --- 基于 S8 ROI 分析：投资高 ROI 类别 ---
        roi_data = self._signals.get("s8_project_roi", [])
        if roi_data:
            top_roi = roi_data[0]
            if top_roi["roi_score"] > 5 and top_roi["count"] >= 3:
                tasks.append({
                    "title": f"STRATEGIC: Scale high-ROI category '{top_roi['category']}'",
                    "prompt": (
                        f"The '{top_roi['category']}' task category has the highest ROI:\n"
                        f"  avg_quality={top_roi['avg_quality']}, avg_cost=${top_roi['avg_cost']}, "
                        f"roi_score={top_roi['roi_score']}\n\n"
                        "Generate 3 more tasks in this category that would add value."
                    ),
                    "priority": 3,
                    "strategic_reason": f"Highest ROI category: {top_roi['roi_score']}",
                    "estimate_min": 10,
                })

        # --- 基于 S11 Dashboard UX 质量审计 ---
        ux = self._signals.get("s11_dashboard_ux", {})
        ux_summary = ux.get("summary", {})
        total_ux_issues = ux_summary.get("total_issues", 0)
        if total_ux_issues > 0:
            parts = []
            if ux_summary.get("hardcoded_string_count"):
                samples = ux.get("hardcoded_strings", [])[:5]
                sample_text = ", ".join(f"L{s['line']}:{s['text'][:30]}" for s in samples)
                parts.append(
                    f"- {ux_summary['hardcoded_string_count']} hardcoded strings "
                    f"(not using data-i18n/t()): {sample_text}"
                )
            if ux_summary.get("stuck_loading_count"):
                ids = [s.get("panel_id", "?") for s in ux.get("stuck_loading", [])]
                parts.append(
                    f"- {ux_summary['stuck_loading_count']} Loading... sections "
                    f"without i18n fallback: {', '.join(ids)}"
                )
            if ux_summary.get("missing_null_guard_count"):
                fns = [s["function"] for s in ux.get("missing_null_guards", [])]
                parts.append(
                    f"- {ux_summary['missing_null_guard_count']} render functions "
                    f"missing null/empty guards: {', '.join(fns)}"
                )
            if ux_summary.get("silent_error_count"):
                eps = [s["path"] for s in ux.get("silent_error_endpoints", [])]
                parts.append(
                    f"- {ux_summary['silent_error_count']} API endpoints with "
                    f"silent error handling: {', '.join(eps[:5])}"
                )

            detail_block = "\n".join(parts)
            priority = 5 if total_ux_issues >= 10 else 4 if total_ux_issues >= 5 else 3
            tasks.append({
                "title": f"UX: Fix {total_ux_issues} dashboard quality issues",
                "prompt": (
                    "Dashboard UX audit found the following issues:\n\n"
                    f"{detail_block}\n\n"
                    "## Fix Plan\n"
                    "1. For hardcoded strings: wrap in data-i18n or use t() in JS\n"
                    "2. For stuck Loading: add data-i18n='loading' attribute\n"
                    "3. For missing null guards: add early return or fallback\n"
                    "4. For silent errors: return {error: message} or raise HTTPException\n\n"
                    "Read dashboard.html and dashboard_server.py first. "
                    "Follow existing i18n and error handling patterns."
                ),
                "priority": priority,
                "category": "ux_quality",
                "strategic_reason": f"Dashboard has {total_ux_issues} UX quality issues",
                "estimate_min": min(total_ux_issues * 2, 30),
            })

        # --- 基于 S7 日程上下文：DDL 临近时降低系统负载 ---
        schedule = self._signals.get("s7_schedule_context", {})
        upcoming_ddls = schedule.get("ddls", [])
        if len(upcoming_ddls) >= 3:
            # User is busy, focus on stability not features
            tasks = [t for t in tasks if t["priority"] >= 4]
            logger.info("SuperStrategist: %d DDLs upcoming, focusing on high-priority only",
                       len(upcoming_ddls))

        # Sort by priority desc
        tasks.sort(key=lambda t: -t.get("priority", 3))

        # Budget fit
        fitted = []
        used = 0
        for t in tasks:
            est = t.get("estimate_min", 15)
            if used + est <= budget_minutes:
                fitted.append(t)
                used += est

        logger.info("SuperStrategist: generated %d strategic tasks from %d candidates",
                    len(fitted), len(tasks))
        return fitted

    def report(self) -> str:
        """生成战略报告（给 CEO 看的简报）。"""
        if not self._signals:
            self.sense_all()

        lines = ["# Super Strategist Report", ""]

        # API costs
        api = self._signals.get("s4_api_usage", {})
        lines.append(f"## API Usage")
        lines.append(f"- Claude cost (KAIROS): ${api.get('claude_cost', 0):.2f}")
        lines.append(f"- Kimi calls (events): {api.get('kimi_calls', 0)}")

        # Agent performance
        agents = self._signals.get("s3_agent_performance", {})
        if agents:
            lines.append(f"\n## Agent Performance")
            for role, data in sorted(agents.items(), key=lambda x: -x[1]["total"]):
                lines.append(
                    f"- {role}: {data['total']} runs, "
                    f"quality={data['avg_quality']}, "
                    f"success={data['success_rate']:.0%}, "
                    f"avg_cost=${data['avg_cost']:.3f}"
                )

        # ROI
        roi = self._signals.get("s8_project_roi", [])
        if roi:
            lines.append(f"\n## Task ROI Ranking")
            for r in roi:
                lines.append(
                    f"- {r['category']}: ROI={r['roi_score']:.1f} "
                    f"(quality={r['avg_quality']}, cost=${r['avg_cost']:.3f}, n={r['count']})"
                )

        # Capability gaps
        gaps = self._signals.get("s9_capability_gaps", [])
        if gaps:
            lines.append(f"\n## Capability Gaps")
            for g in gaps:
                lines.append(f"- {g}")

        # Dashboard UX
        ux = self._signals.get("s11_dashboard_ux", {})
        ux_summary = ux.get("summary", {})
        if ux_summary.get("total_issues", 0) > 0:
            lines.append(f"\n## Dashboard UX Quality")
            lines.append(f"- Hardcoded strings: {ux_summary.get('hardcoded_string_count', 0)}")
            lines.append(f"- Stuck loading: {ux_summary.get('stuck_loading_count', 0)}")
            lines.append(f"- Missing null guards: {ux_summary.get('missing_null_guard_count', 0)}")
            lines.append(f"- Silent error endpoints: {ux_summary.get('silent_error_count', 0)}")

        # Evolution
        evo = self._signals.get("s2_evolution_gaps", {})
        lines.append(f"\n## Evolution")
        lines.append(f"- Patterns: {evo.get('patterns', 0)}")
        lines.append(f"- Episodes pending: {evo.get('episodes_pending', 0)}")

        return "\n".join(lines)


# Singleton
_instance: Optional[SuperStrategist] = None


def get_super_strategist() -> SuperStrategist:
    global _instance
    if _instance is None:
        _instance = SuperStrategist()
    return _instance


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    ss = SuperStrategist()
    signals = ss.sense_all()
    print(ss.report())
    print("\n" + "=" * 50)
    print("Strategic Tasks:")
    tasks = ss.generate_strategic_tasks()
    for t in tasks:
        print(f"\n  P{t['priority']} | {t['title']}")
        print(f"     Reason: {t.get('strategic_reason', 'N/A')}")
