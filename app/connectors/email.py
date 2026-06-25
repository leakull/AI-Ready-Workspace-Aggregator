"""Email connector — IMAP.

Reads UNSEEN messages over IMAP, parses MIME with the stdlib ``email`` package,
and normalizes each into a :class:`UnifiedMessage`. Unlike the other connectors,
attachment bytes are already in hand after parsing, so they are uploaded to S3
inline and the resulting bucket/key is attached to the message.

At-least-once: messages are fetched with ``BODY.PEEK[]`` so the IMAP ``\\Seen``
flag is *not* set during fetch. The flag is set only after the sync task commits
(see :meth:`on_sync_committed`), so a failed run re-processes the same messages
instead of silently skipping them next time.

``external_id`` is the ``Message-ID`` header; ``thread_external_id`` is taken
from ``In-Reply-To`` / ``References`` so a reply links to its thread.
"""

from __future__ import annotations

import email
import imaplib
from collections.abc import Iterator
from email.header import decode_header, make_header
from email.message import Message as MIMEMessage
from email.utils import parsedate_to_datetime

from app.connectors.base import BaseConnector, ConnectorNotConfigured, RetryableError
from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.message import SourceSystem, UnifiedAttachment, UnifiedMessage
from app.services.storage import build_object_key, ensure_bucket, upload_bytes

log = get_logger(__name__)


def _decode(value: str | None) -> str | None:
    """Decode an RFC 2047 encoded header (e.g. ``=?utf-8?B?...?=``)."""
    if not value:
        return None
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


class EmailConnector(BaseConnector):
    source_system = SourceSystem.email.value

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        mailbox: str | None = None,
    ):
        self.host = host if host is not None else settings.imap_host
        self.port = port if port is not None else settings.imap_port
        self.user = user if user is not None else settings.imap_user
        self.password = password if password is not None else settings.imap_password
        self.mailbox = mailbox if mailbox is not None else settings.imap_mailbox
        self._pending_uids: list[str] = []

    def is_configured(self) -> bool:
        return bool(self.host and self.user and self.password)

    # -- IMAP (isolated so tests can stub it) -------------------------------
    def _connect(self) -> imaplib.IMAP4:
        if settings.imap_use_ssl:
            return imaplib.IMAP4_SSL(self.host, self.port)
        return imaplib.IMAP4(self.host, self.port)

    def _login(self, imap: imaplib.IMAP4) -> None:
        imap.login(self.user, self.password)

    def _fetch_raw_messages(self) -> list[tuple[str, bytes]]:
        """Return [(uid, rfc822_bytes)] for UNSEEN messages, without marking seen."""
        out: list[tuple[str, bytes]] = []
        imap = self._connect()
        try:
            self._login(imap)
            imap.select(self.mailbox)
            typ, data = imap.uid("search", None, "UNSEEN")
            if typ != "OK" or not data or not data[0]:
                return out
            for uid in data[0].split()[: settings.imap_max_messages]:
                typ, msgdata = imap.uid("fetch", uid, "(BODY.PEEK[])")
                if typ != "OK" or not msgdata or not isinstance(msgdata[0], tuple):
                    continue
                out.append((uid.decode(), msgdata[0][1]))
        finally:
            try:
                imap.logout()
            except Exception:
                pass
        return out

    def _mark_seen(self, uids: list[str]) -> None:
        imap = self._connect()
        try:
            self._login(imap)
            imap.select(self.mailbox)
            for uid in uids:
                imap.uid("store", uid, "+FLAGS", "(\\Seen)")
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    # -- fetch --------------------------------------------------------------
    def fetch(self) -> Iterator[UnifiedMessage]:
        if not self.is_configured():
            raise ConnectorNotConfigured("IMAP_HOST / IMAP_USER / IMAP_PASSWORD are not set")

        try:
            raw_messages = self._fetch_raw_messages()
        except (imaplib.IMAP4.abort, OSError) as exc:
            raise RetryableError(f"imap error: {exc}") from exc

        if not raw_messages:
            return

        ensure_bucket()
        self._pending_uids = []
        log.info("email.fetched", count=len(raw_messages), mailbox=self.mailbox)
        for uid, raw in raw_messages:
            self._pending_uids.append(uid)
            yield self._normalize(raw)

    def on_sync_committed(self) -> None:
        if not self._pending_uids:
            return
        try:
            self._mark_seen(self._pending_uids)
            log.info("email.marked_seen", count=len(self._pending_uids))
        finally:
            self._pending_uids = []

    # -- normalization ------------------------------------------------------
    def _normalize(self, raw: bytes) -> UnifiedMessage:
        msg = email.message_from_bytes(raw)

        message_id = (msg.get("Message-ID") or "").strip()
        thread = (msg.get("In-Reply-To") or msg.get("References") or "").strip().split()
        date = msg.get("Date")

        return UnifiedMessage(
            source_system=SourceSystem.email,
            external_id=message_id or f"no-id:{hash(raw)}",
            thread_external_id=thread[0] if thread else None,
            author=_decode(msg.get("From")),
            title=_decode(msg.get("Subject")),
            body=_extract_body(msg),
            url=None,
            source_created_at=_safe_date(date),
            raw={"headers": {k: _decode(v) for k, v in msg.items()}},
            attachments=self._extract_attachments(msg, message_id or "no-id"),
        )

    def _extract_attachments(
        self, msg: MIMEMessage, message_external_id: str
    ) -> list[UnifiedAttachment]:
        attachments: list[UnifiedAttachment] = []
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            filename = part.get_filename()
            disposition = (part.get("Content-Disposition") or "").lower()
            if not filename or "attachment" not in disposition:
                continue

            filename = _decode(filename) or "attachment"
            data = part.get_payload(decode=True) or b""

            key = build_object_key(self.source_system, message_external_id, filename)
            bucket, key = upload_bytes(key, data, part.get_content_type())

            attachments.append(
                UnifiedAttachment(
                    external_id=part.get("Content-ID") or filename,
                    filename=filename,
                    content_type=part.get_content_type(),
                    size_bytes=len(data),
                    s3_bucket=bucket,
                    s3_key=key,
                )
            )
        return attachments


def _safe_date(value: str | None):
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def _extract_body(msg: MIMEMessage) -> str:
    """Prefer the first text/plain part; fall back to text/html stripped of tags."""
    if not msg.is_multipart():
        return _part_text(msg)

    html_fallback = ""
    for part in msg.walk():
        content_type = part.get_content_type()
        disposition = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition:
            continue
        if content_type == "text/plain":
            return _part_text(part)
        if content_type == "text/html" and not html_fallback:
            html_fallback = _part_text(part)
    return html_fallback


def _part_text(part: MIMEMessage) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, ValueError):
        return payload.decode("utf-8", errors="replace")
