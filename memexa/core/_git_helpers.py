"""Centralized SHA validator.

ALL git subprocess invocations across the gate codebase MUST route through
``validate_sha()`` before passing the SHA to ``subprocess.run`` /
``subprocess.check_output``.  This prevents shell-injection via a crafted
LAST_COMMIT_SHA value (e.g. ``"deadbee; rm -rf /"``).

Usage::

    from memexa.core._git_helpers import validate_sha
    safe_sha = validate_sha(raw_sha)
    subprocess.check_output(["git", "show", safe_sha, "--stat"])
"""
from __future__ import annotations

import re


def validate_sha(sha: str) -> str:
    """Validate and return *sha* if it looks like a legal git object identifier.

    A legal git SHA is a hex string of 7 to 40 lowercase characters
    (``[0-9a-f]``).  Abbreviated SHAs shorter than 7 chars are ambiguous in
    large repos, so they are rejected.

    Args:
        sha: Candidate git SHA string.

    Returns:
        The *sha* argument unchanged if it passes validation.

    Raises:
        ValueError: If *sha* is not a ``str`` or does not match the expected
                    pattern.  The error message is safe to log.

    Examples::

        >>> validate_sha("abc1234")
        'abc1234'
        >>> validate_sha("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
        'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef'
        >>> validate_sha("xxx; rm -rf /")  # raises ValueError
    """
    if not isinstance(sha, str):
        raise ValueError("sha must be str")
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        raise ValueError("invalid git sha format")
    return sha
