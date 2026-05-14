"""
Tool Synthesizer -- Scaffold Self-Evolution Engine

Inspired by Live-SWE-agent on-the-fly tool synthesis (arXiv 2511.13646).
Generates, validates, registers, and manages custom Python tools
that emerge from task execution patterns.

Lifecycle:
1. PROPOSE: Given a task pattern, propose a tool specification
2. GENERATE: Generate Python code for the tool
3. VALIDATE: ast.parse + sandbox test + security scan
4. REGISTER: Add to tool registry (JSONL)
5. INVOKE: Make tool available for future tasks
6. RETIRE: Mark unused tools as deprecated after TTL

Tool types:
- analyzer: Code analysis utilities (e.g., find all async functions)
- transformer: Code transformation (e.g., convert dict access to dataclass)
- checker: Validation tools (e.g., verify all imports resolve)
- reporter: Output formatting (e.g., generate coverage heatmap)
"""

import ast
import json
import logging
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
_TOOLS_FILE = _DATA_DIR / "synthesized_tools.jsonl"
_SUGGESTED_TOOLS_FILE = _DATA_DIR / "suggested_tools.json"

# Patterns that are absolute blockers regardless of context
_ABSOLUTE_BLOCKERS = [
    "eval(",
    "exec(",
    "os.system(",
    "shell=True",
    "__import__(",
    "__subclasses__",
    "__bases__",
    "__mro__",
    "__globals__",
    "__code__",
    "__builtins__",
    "getattr(",
    "setattr(",
    "delattr(",
]


# ── Data Model ──

@dataclass
class ToolSpec:
    """Specification for a synthesized tool."""
    name: str                     # snake_case function name
    description: str              # What this tool does
    tool_type: Literal["analyzer", "transformer", "checker", "reporter"] = "analyzer"
    input_schema: Dict[str, str] = field(default_factory=dict)    # param_name -> type_str
    output_schema: Dict[str, str] = field(default_factory=dict)   # "return" -> type_str
    source_code: str = ""         # Complete Python function definition
    created_at: str = ""
    usage_count: int = 0
    last_used: str = ""
    confidence: Literal["high", "medium", "low"] = "medium"
    provenance: str = ""          # What task/pattern inspired this tool
    retired: bool = False
    tool_id: str = ""

    def __post_init__(self):
        if not self.tool_id:
            self.tool_id = str(uuid.uuid4())[:8]
        now = datetime.utcnow().isoformat() + "Z"
        if not self.created_at:
            self.created_at = now
        if not self.last_used:
            self.last_used = now

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ToolSpec":
        """Reconstruct ToolSpec from a dict, tolerating missing fields."""
        known_fields = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)


# ── Registry Operations ──

def register_tool(spec: ToolSpec) -> bool:
    """
    Validate and save a ToolSpec to the JSONL registry.

    Returns True if the tool was registered successfully.
    """
    valid, reason = validate_tool(spec)
    if not valid:
        logger.warning("Tool '%s' failed validation: %s", spec.name, reason)
        return False

    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Check for duplicate name (non-retired)
    existing = load_tools()
    for tool in existing:
        if tool.name == spec.name and not tool.retired:
            logger.info(
                "Tool '%s' already registered (id=%s). Skipping.",
                spec.name, tool.tool_id,
            )
            return False

    with _TOOLS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(spec.to_dict(), ensure_ascii=False) + "\n")

    logger.info(
        "Registered tool '%s' (id=%s, type=%s)",
        spec.name, spec.tool_id, spec.tool_type,
    )
    return True


def load_tools(include_retired: bool = False) -> List[ToolSpec]:
    """Load all registered tools from the JSONL registry."""
    if not _TOOLS_FILE.exists():
        return []

    tools = []
    for i, line in enumerate(_TOOLS_FILE.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            spec = ToolSpec.from_dict(data)
            if include_retired or not spec.retired:
                tools.append(spec)
        except Exception as e:
            logger.warning("Skipped malformed tool registry line %d: %s", i + 1, e)

    return tools


def find_tool(query: str) -> Optional[ToolSpec]:
    """
    Search for a registered tool by keyword.

    Matches against name, description, provenance, and tool_type.
    Returns the best match (highest usage_count among matching tools).
    """
    if not query:
        return None

    query_lower = query.lower()
    tools = load_tools()
    candidates = []

    for tool in tools:
        searchable = " ".join([
            tool.name,
            tool.description,
            tool.provenance,
            tool.tool_type,
        ]).lower()
        if query_lower in searchable:
            candidates.append(tool)

    if not candidates:
        return None

    # Prefer higher usage_count, then higher confidence
    confidence_rank = {"high": 2, "medium": 1, "low": 0}
    candidates.sort(
        key=lambda t: (t.usage_count, confidence_rank.get(t.confidence, 0)),
        reverse=True,
    )
    return candidates[0]


def retire_tool(name: str) -> bool:
    """
    Mark a tool as retired in the registry.

    Rewrites the entire JSONL file with the updated retired flag.
    Returns True if a tool was found and retired.
    """
    if not _TOOLS_FILE.exists():
        return False

    tools_all = load_tools(include_retired=True)
    found = False
    for tool in tools_all:
        if tool.name == name and not tool.retired:
            tool.retired = True
            found = True

    if not found:
        return False

    _rewrite_registry(tools_all)
    logger.info("Retired tool '%s'", name)
    return True


def _rewrite_registry(tools: List[ToolSpec]) -> None:
    """Overwrite the JSONL registry with the provided tool list."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(t.to_dict(), ensure_ascii=False) for t in tools]
    _TOOLS_FILE.write_text(
        "\n".join(lines) + ("\n" if lines else ""),
        encoding="utf-8",
    )


# ── Tool Validation ──

def validate_tool(spec: ToolSpec) -> Tuple[bool, str]:
    """
    Validate a ToolSpec before registration or invocation.

    Steps:
    1. ast.parse the source_code
    2. Check for dangerous/blocked patterns (text + AST)
    3. Verify function definition exists with name matching spec.name
    4. Verify input_schema keys appear in function signature
    5. Compile the source_code (dry run)

    Returns (valid: bool, reason: str).
    """
    if not spec.name:
        return False, "Tool name is empty"

    if not spec.source_code:
        return False, "source_code is empty"

    # --- Step 1: ast.parse ---
    try:
        tree = ast.parse(spec.source_code)
    except SyntaxError as e:
        return False, f"SyntaxError in source_code: {e}"

    # --- Step 2: Security scan (text level) ---
    for blocked in _ABSOLUTE_BLOCKERS:
        if blocked in spec.source_code:
            return False, f"Blocked security pattern found: '{blocked}'"

    # Walk AST to catch dynamic attribute access that might bypass text scan
    for node in ast.walk(tree):
        # Detect os.system / os.popen via attribute access
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "os":
                if node.attr in ("system", "popen", "execv", "execve", "execvp"):
                    return False, f"Blocked os.{node.attr}() call detected"
        # Detect subprocess with shell=True via keyword arg
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if (
                    kw.arg == "shell"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True
                ):
                    return False, "Blocked subprocess call with shell=True"

    # --- Step 3: Verify function definition with matching name ---
    func_defs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    if not func_defs:
        return False, "source_code must contain at least one function definition"

    matching_funcs = [f for f in func_defs if f.name == spec.name]
    if not matching_funcs:
        func_names = [f.name for f in func_defs]
        return False, (
            f"No function named '{spec.name}' found in source_code. "
            f"Found: {func_names}"
        )

    func_def = matching_funcs[0]

    # --- Step 4: Verify input_schema keys in function signature ---
    if spec.input_schema:
        param_names = {arg.arg for arg in func_def.args.args}
        param_names.discard("self")
        missing_params = set(spec.input_schema.keys()) - param_names
        if missing_params:
            return False, (
                f"input_schema keys {missing_params} not found in "
                f"function signature {param_names}"
            )

    # --- Step 5: Compile dry run ---
    try:
        compile(spec.source_code, f"<tool:{spec.name}>", "exec")
    except Exception as e:
        return False, f"Compile error: {e}"

    return True, "OK"


# ── Tool Invocation ──

def invoke_tool(name: str, **kwargs) -> Any:
    """
    Load, compile, and execute a registered tool in a restricted namespace.

    The tool runs with a curated safe-builtins dict. No access to os, sys,
    subprocess, or file I/O is provided by default.

    Raises:
        KeyError: if tool not found in registry
        ValueError: if tool fails validation at invoke time
        RuntimeError: if tool execution raises an exception
    """
    tool = find_tool(name)
    if tool is None:
        raise KeyError(f"Tool '{name}' not found in registry")

    # Re-validate before execution
    valid, reason = validate_tool(tool)
    if not valid:
        raise ValueError(f"Tool '{name}' failed validation: {reason}")

    # Build restricted execution namespace with safe builtins only
    # Restricted builtins: NO getattr/setattr/hasattr (sandbox escape via
    # object graph traversal, e.g. getattr(str,'__class__').__bases__[0].__subclasses__())
    # NO type (can access __bases__), NO issubclass (can probe class hierarchy)
    safe_builtins = {
        "len": len, "range": range, "enumerate": enumerate,
        "zip": zip, "map": map, "filter": filter,
        "sorted": sorted, "reversed": reversed,
        "list": list, "dict": dict, "set": set, "tuple": tuple,
        "str": str, "int": int, "float": float, "bool": bool,
        "isinstance": isinstance,
        "print": print, "repr": repr,
        "min": min, "max": max, "sum": sum, "abs": abs,
        "round": round, "any": any, "all": all,
        "None": None, "True": True, "False": False,
        "Exception": Exception, "ValueError": ValueError,
        "TypeError": TypeError, "KeyError": KeyError,
    }

    namespace: Dict[str, Any] = {"__builtins__": safe_builtins}

    # Compile the validated source code
    try:
        code_obj = compile(tool.source_code, f"<tool:{tool.name}>", "exec")
    except Exception as e:
        raise RuntimeError(f"Failed to compile tool '{name}': {e}") from e

    # Execute the function definition into namespace (loads the function object)
    _exec_in_namespace(code_obj, namespace)

    if tool.name not in namespace:
        raise RuntimeError(
            f"Tool function '{tool.name}' not found in compiled namespace"
        )

    func = namespace[tool.name]

    # Invoke the tool function
    try:
        result = func(**kwargs)
    except Exception as e:
        raise RuntimeError(
            f"Tool '{name}' raised an error during execution: {e}"
        ) from e

    # Update usage metadata
    _update_usage(tool.name)

    return result


def _exec_in_namespace(code_obj: Any, namespace: Dict[str, Any]) -> None:
    """
    Execute a compiled code object in the given namespace.

    Isolated into its own function to make the exec call auditable.
    This is intentional: code_obj is produced only from pre-validated tool source code.
    """
    exec(code_obj, namespace)  # noqa: S102 -- pre-validated by validate_tool


def _update_usage(name: str) -> None:
    """Increment usage_count and update last_used for a named tool."""
    try:
        tools_all = load_tools(include_retired=True)
        updated = False
        for tool in tools_all:
            if tool.name == name:
                tool.usage_count += 1
                tool.last_used = datetime.utcnow().isoformat() + "Z"
                updated = True
                break
        if updated:
            _rewrite_registry(tools_all)
    except Exception as e:
        logger.warning("Failed to update usage for tool '%s': %s", name, e)


# ── Pattern-to-Tool Suggestion ──

def suggest_tools_from_patterns(patterns: List[Any]) -> List[ToolSpec]:
    """
    Analyze pattern_extractor entries for recurring task themes and
    suggest tool specifications (without code) that could automate them.

    Implements the "suggest, don't auto-generate" safe MVP approach:
    - Generates ToolSpec objects with empty source_code
    - Suggestions saved to suggested_tools.json for CTO review
    - A synthesis prompt hint is included in provenance for each suggestion

    Args:
        patterns: list of PatternEntry-like objects or dicts from pattern_extractor

    Returns:
        list of ToolSpec suggestions (source_code is empty, needs CTO-approved generation)
    """
    if not patterns:
        return []

    # Normalize: support PatternEntry objects, dataclass instances, and plain dicts
    pattern_dicts: List[Dict[str, Any]] = []
    for p in patterns:
        if hasattr(p, "to_dict") and callable(p.to_dict):
            pattern_dicts.append(p.to_dict())
        elif hasattr(p, "__dataclass_fields__"):
            pattern_dicts.append(asdict(p))
        elif isinstance(p, dict):
            pattern_dicts.append(p)

    # Group patterns by tag/theme to find recurring topics
    theme_counts: Dict[str, int] = defaultdict(int)
    theme_examples: Dict[str, List[str]] = defaultdict(list)

    for pd in pattern_dicts:
        tags = pd.get("tags", [])
        fact = pd.get("fact", pd.get("text", ""))
        for tag in tags:
            theme_counts[tag] += 1
            if fact and len(theme_examples[tag]) < 3:
                theme_examples[tag].append(str(fact)[:120])

    # Only suggest tools for themes with >= 2 pattern occurrences
    suggestions: List[ToolSpec] = []
    already_suggested: set = set()

    for theme, count in sorted(theme_counts.items(), key=lambda x: -x[1]):
        if count < 2:
            continue

        tool_name, description, tool_type = _theme_to_tool_spec(
            theme, theme_examples[theme]
        )
        if tool_name in already_suggested:
            continue

        already_suggested.add(tool_name)
        examples_text = "; ".join(theme_examples[theme][:2])
        synthesis_hint = generate_synthesis_prompt_text(tool_name, description, tool_type)
        provenance = (
            f"Suggested from {count} patterns tagged '{theme}'. "
            f"Examples: {examples_text}. "
            f"Synthesis hint: {synthesis_hint}"
        )

        spec = ToolSpec(
            name=tool_name,
            description=description,
            tool_type=tool_type,
            input_schema={},
            output_schema={"return": "Any"},
            source_code="",    # Empty -- requires CTO to trigger code generation
            confidence="low",  # Suggestions start as low confidence
            provenance=provenance,
        )
        suggestions.append(spec)

    return suggestions


def _theme_to_tool_spec(
    theme: str,
    examples: List[str],
) -> Tuple[str, str, Literal["analyzer", "transformer", "checker", "reporter"]]:
    """
    Map a recurring pattern theme to a (tool_name, description, tool_type) tuple.

    Falls back to a generic analyzer for unknown themes.
    """
    theme_map: Dict[
        str,
        Tuple[str, str, Literal["analyzer", "transformer", "checker", "reporter"]],
    ] = {
        "testing": (
            "test_failure_analyzer",
            "Analyze test failure output to extract failing test names and error messages",
            "analyzer",
        ),
        "hook": (
            "hook_coverage_checker",
            "Verify that all hook trigger points have corresponding handler registrations",
            "checker",
        ),
        "import": (
            "import_resolver_checker",
            "Check that all import statements in Python files resolve without errors",
            "checker",
        ),
        "security": (
            "security_pattern_reporter",
            "Scan Python source files for known insecure patterns and report findings",
            "reporter",
        ),
        "async": (
            "async_usage_analyzer",
            "Find all async/await usage patterns and identify potential concurrency issues",
            "analyzer",
        ),
        "memory": (
            "memory_reference_checker",
            "Verify that all memory file references in MEMORY.md index actually exist on disk",
            "checker",
        ),
        "agent": (
            "agent_spec_validator",
            "Validate agent specification dicts for required fields and model names",
            "checker",
        ),
        "config": (
            "config_key_checker",
            "Check that all expected config keys are present in a settings dict",
            "checker",
        ),
        "database": (
            "db_schema_reporter",
            "Report table schemas and row counts for SQLite databases",
            "reporter",
        ),
        "api": (
            "api_endpoint_lister",
            "List all route/endpoint definitions found in a Python project",
            "analyzer",
        ),
        "git": (
            "git_commit_formatter",
            "Format a list of change descriptions into a conventional commit message",
            "transformer",
        ),
        "performance": (
            "performance_hotspot_finder",
            "Identify Python functions that appear in multiple performance-related patterns",
            "analyzer",
        ),
        "mcp": (
            "mcp_tool_inventory",
            "List all MCP tools registered across server configuration files",
            "reporter",
        ),
    }

    mapping = theme_map.get(theme.lower())
    if mapping is not None:
        return mapping

    # Generic fallback for unknown themes
    safe_theme = "".join(c if c.isalnum() else "_" for c in theme.lower())
    name = f"{safe_theme}_pattern_analyzer"
    desc = f"Analyze and report on patterns related to '{theme}' in the codebase"
    return name, desc, "analyzer"


def generate_synthesis_prompt(spec: ToolSpec) -> str:
    """
    Generate a prompt that can be given to Claude to write the actual tool code.

    Includes the spec, constraints, and an example format skeleton.
    """
    input_params = ", ".join(
        f"{k}: {v}" for k, v in spec.input_schema.items()
    ) or "data: Any"

    return_type = spec.output_schema.get("return", "Any")

    lines = [
        f"Write a Python function named `{spec.name}` that implements the following tool:",
        "",
        f"**Description**: {spec.description}",
        f"**Type**: {spec.tool_type}",
        f"**Provenance**: {spec.provenance}",
        "",
        "**Requirements**:",
        "1. The function must be a complete, self-contained Python function",
        "2. Parameters: " + (
            input_params if spec.input_schema else "(choose appropriate parameters)"
        ),
        f"3. Return type: {return_type}",
        "4. Use ONLY Python stdlib (no third-party packages)",
        "5. No eval(), exec(), os.system(), subprocess with shell=True, or __import__()",
        "6. Include a docstring explaining parameters and return value",
        "7. Handle errors gracefully and return a meaningful result on failure",
        "",
        "**Example format**:",
        "```python",
        f"def {spec.name}({input_params}) -> {return_type}:",
        '    """',
        f"    {spec.description}",
        "",
        "    Args:",
        "        (describe each parameter)",
        "",
        "    Returns:",
        f"        {return_type}: (describe return value)",
        '    """',
        "    # Implementation here",
        "    ...",
        "```",
        "",
        "Provide ONLY the function definition, no import statements or test code.",
    ]
    return "\n".join(lines)


def generate_synthesis_prompt_text(
    name: str,
    description: str,
    tool_type: Literal["analyzer", "transformer", "checker", "reporter"],
) -> str:
    """Compact single-line synthesis prompt for embedding in provenance strings."""
    return (
        f"Implement `def {name}(...)` -- {description}. "
        f"Type: {tool_type}. Stdlib only. No eval/exec/shell=True."
    )


# ── Lifecycle Management ──

def cleanup_tools(ttl_days: int = 60) -> int:
    """
    Retire tools that have not been used for more than ttl_days days.

    Returns the count of newly retired tools.
    """
    if not _TOOLS_FILE.exists():
        return 0

    tools_all = load_tools(include_retired=True)
    now = datetime.utcnow()
    newly_retired = 0

    for tool in tools_all:
        if tool.retired:
            continue
        try:
            last_used_dt = datetime.fromisoformat(tool.last_used.rstrip("Z"))
        except (ValueError, AttributeError):
            try:
                last_used_dt = datetime.fromisoformat(tool.created_at.rstrip("Z"))
            except (ValueError, AttributeError):
                continue

        age_days = (now - last_used_dt).days
        if age_days > ttl_days:
            tool.retired = True
            newly_retired += 1
            logger.info(
                "Retired tool '%s' (unused for %d days, TTL=%d)",
                tool.name, age_days, ttl_days,
            )

    if newly_retired > 0:
        _rewrite_registry(tools_all)

    return newly_retired


def save_suggested_tools(suggestions: List[ToolSpec]) -> None:
    """
    Persist tool suggestions to suggested_tools.json for CTO review.

    Merges with existing suggestions, de-duplicating by tool name.
    """
    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    existing: List[Dict[str, Any]] = []
    if _SUGGESTED_TOOLS_FILE.exists():
        try:
            existing = json.loads(
                _SUGGESTED_TOOLS_FILE.read_text(encoding="utf-8")
            )
        except Exception:
            existing = []

    existing_names = {s.get("name") for s in existing}
    added = 0
    for spec in suggestions:
        if spec.name not in existing_names:
            existing.append(spec.to_dict())
            existing_names.add(spec.name)
            added += 1

    _SUGGESTED_TOOLS_FILE.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(
        "Saved %d new tool suggestions (total %d) to %s",
        added, len(existing), _SUGGESTED_TOOLS_FILE,
    )


def get_tool_stats() -> Dict[str, Any]:
    """Return summary statistics about the tool registry."""
    all_tools = load_tools(include_retired=True)
    active = [t for t in all_tools if not t.retired]
    retired_list = [t for t in all_tools if t.retired]

    type_counts: Dict[str, int] = defaultdict(int)
    for t in active:
        type_counts[t.tool_type] += 1

    return {
        "total": len(all_tools),
        "active": len(active),
        "retired": len(retired_list),
        "by_type": dict(type_counts),
        "total_invocations": sum(t.usage_count for t in active),
        "most_used": max(active, key=lambda t: t.usage_count).name if active else None,
    }
