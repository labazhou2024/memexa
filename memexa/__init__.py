"""memexa — self-hosted Chinese personal memory graph."""

# 2026-05-16 rc4: read version dynamically from the installed package
# metadata so this file never drifts out of sync with pyproject.toml.
# Hard-coding bit rc1/rc2/rc3 — all four releases shipped with
# memexa.__version__ still reading "0.1.0a0".
try:
    from importlib.metadata import version as _version, PackageNotFoundError as _PNE
    try:
        __version__ = _version("memexa")
    except _PNE:
        __version__ = "0.0.0+unknown"
except Exception:
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
