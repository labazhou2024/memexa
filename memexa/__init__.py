"""memexa - self-hosted Chinese personal memory graph demo."""

try:
    from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
    from importlib.metadata import version as _version

    try:
        __version__ = _version("memexa")
    except _PackageNotFoundError:
        __version__ = "0.1.0"
except Exception:
    __version__ = "0.1.0"

__all__ = ["__version__"]
