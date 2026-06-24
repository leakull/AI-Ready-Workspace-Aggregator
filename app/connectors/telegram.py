"""Telegram connector — STUB.

Planned approach: long polling via ``getUpdates`` (no public URL needed, unlike
webhooks). Each update is normalized into a ``UnifiedMessage`` with
``external_id = str(message_id)`` and ``thread_external_id = str(chat_id)``.
Attachments (documents/photos) become ``UnifiedAttachment`` with the Telegram
``file_id`` as ``external_id``; the file bytes are fetched lazily by the
attachment task.

Left intentionally unimplemented so the GitHub slice can be proven end-to-end
first. Wiring is already in place: it is registered in the connector registry,
exposed by the API, and picked up by ``sync_all_connectors``.
"""

from __future__ import annotations

from collections.abc import Iterator

from app.connectors.base import BaseConnector, ConnectorNotConfigured
from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.message import SourceSystem, UnifiedMessage

log = get_logger(__name__)


class TelegramConnector(BaseConnector):
    source_system = SourceSystem.telegram.value

    def is_configured(self) -> bool:
        return bool(settings.telegram_bot_token)

    def fetch(self) -> Iterator[UnifiedMessage]:
        if not self.is_configured():
            raise ConnectorNotConfigured("TELEGRAM_BOT_TOKEN is not set")
        log.warning("telegram.not_implemented")
        return iter(())  # TODO: long-poll getUpdates and normalize
