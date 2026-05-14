"""src.core.gates — pre-commit quality gate modules.

Each gate module exposes uniform `check(task_id) -> tuple[bool, str]` so
gate_runner.py can invoke them interchangeably at Stage 3.5.
"""
