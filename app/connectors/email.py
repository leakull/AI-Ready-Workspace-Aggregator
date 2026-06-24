"""Email (IMAP) connector — STUB.

Planned approach: connect over IMAP (``imaplib``), select INBOX, search UNSEEN,
fetch RFC822, parse with the stdlib ``email`` package. ``external_id`` is the
``Message-ID`` header; ``thread_external_id`` is derived from
``References``/``In-Reply-To``. MIME attachments become ``UnifiedAttachment``
and their bytes are uploaded to S3 by the attachment task.

Use a throwaway mailbox or a local SMTP/IMAP sink (e.g. mailpit) for testing to
avoid Gmail OAuth/app-password friction.

Left intentionally unimplemented; registry/API/scheduler wiring is already done.
"""

from __future__ import annotations

from collections.abc import Iterator

from app.connectors.base import BaseConnector, ConnectorNotConfigured
from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.message import SourceSystem, UnifiedMessage

log = get_logger(__name__)


class EmailConnector(BaseConnector):
    source_system = SourceSystem.email.value

    def is_configured(self) -> bool:
        return bool(settings.imap_host and settings.imap_user and settings.imap_password)

    def fetch(self) -> Iterator[UnifiedMessage]:
        if not self.is_configured():
            raise ConnectorNotConfigured("IMAP_HOST / IMAP_USER / IMAP_PASSWORD are not set")
        log.warning("email.not_implemented")
        return iter(())  # TODO: IMAP UNSEEN search + MIME parse + attachments
