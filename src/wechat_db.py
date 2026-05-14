"""WeChat database reader — optional plugin module (STUB).

The full WeChat DB reader is intentionally NOT bundled with this project,
because reading the live WeChat client database is a platform-specific,
gray-area capability that should be provided by upstream tools such as:

    - https://github.com/LC044/WeChatMsg
    - https://github.com/git-jiadong/wechatDataBackup
    - https://github.com/xaoyaoo/PyWxDump

The expected interface is documented below. To enable WeChat ingestion, ship
your own implementation of this module (or use the export workflow described
in ``docs/integrations/wechat.md``) and place it at ``src/wechat_db.py``.

Until then, importing this module raises NotImplementedError so callers can
provide a fall-back path.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional


_MISSING_MSG = (
    "src.wechat_db is a stub — the production reader is not bundled with this "
    "OSS distribution. See docs/integrations/wechat.md for plug-in instructions, "
    "or export your chat history with an upstream tool (e.g. WeChatMsg) and feed "
    "the resulting JSON to src/ingestion/v5_wechat_batch_builder.py instead."
)


@dataclass(frozen=True)
class WxMessage:
    """Canonical message shape consumed by the v5 ingest pipeline.

    A real implementation must populate every field for each message.
    """
    msg_id: str
    chat_room: str
    sender: str
    sender_wxid: Optional[str]
    timestamp: float
    text: str
    msg_type: int


class WeChatDBReader:
    """Iterate over WeChat client database messages.

    A real implementation should expose at minimum:
      • ``__init__(db_path: Optional[Path] = None)`` — open the database.
      • ``iter_messages(start, end, room=None) -> Iterator[WxMessage]``.
      • ``close()`` — release resources.
    """

    def __init__(self, *_, **__):
        raise NotImplementedError(_MISSING_MSG)

    def iter_messages(self, *_, **__) -> Iterator[WxMessage]:  # pragma: no cover
        raise NotImplementedError(_MISSING_MSG)

    def close(self) -> None:  # pragma: no cover
        raise NotImplementedError(_MISSING_MSG)


class WeChatDBWatcher:
    """Watch for new messages appended to the WeChat client database.

    A real implementation should expose:
      • ``__init__(callback)`` — register a per-message callback.
      • ``start()`` / ``stop()`` — control the watcher thread.
    """

    def __init__(self, *_, **__):
        raise NotImplementedError(_MISSING_MSG)

    def start(self) -> None:  # pragma: no cover
        raise NotImplementedError(_MISSING_MSG)

    def stop(self) -> None:  # pragma: no cover
        raise NotImplementedError(_MISSING_MSG)
