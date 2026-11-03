from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

from tubeflow_test.domain.errors import DomainError

_AAD = b"tubeflow:deletion-receipt:v1"
_KEY_CONTEXT = b"tubeflow:deletion-receipt:key:v1\0"
_TOKEN_VERSION = 1
_NONCE_BYTES = 12
_TAG_BYTES = 16
_MAX_TOKEN_LENGTH = 2048
_INVALID_RECEIPT = ("not_found", "Resource not found")


@dataclass(frozen=True, slots=True)
class DeletionReceiptClaims:
    project_id: UUID
    owner_id: UUID
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedDeletionReceipt:
    token: str
    expires_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "deletion_receipt": self.token,
            "receipt_expires_at": self.expires_at.isoformat().replace("+00:00", "Z"),
        }


class DeletionReceiptCodec:
    def __init__(
        self,
        secret: str,
        *,
        now: Callable[[], datetime] | None = None,
        ttl: timedelta = timedelta(minutes=10),
    ) -> None:
        if len(secret) < 32:
            raise ValueError("deletion receipt secret must be at least 32 characters")
        if ttl != timedelta(minutes=10):
            raise ValueError("deletion receipt TTL must be exactly 10 minutes")
        self._key = hashlib.sha256(_KEY_CONTEXT + secret.encode("utf-8")).digest()
        self._now = now or (lambda: datetime.now(UTC))
        self._ttl = ttl

    def issue(self, project_id: UUID, owner_id: UUID) -> IssuedDeletionReceipt:
        issued_at = self._utc_now().replace(microsecond=0)
        expires_at = issued_at + self._ttl
        payload = json.dumps(
            {
                "v": _TOKEN_VERSION,
                "project_id": str(project_id),
                "owner_id": str(owner_id),
                "issued_at": int(issued_at.timestamp()),
                "expires_at": int(expires_at.timestamp()),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        cipher = AES.new(
            self._key, AES.MODE_GCM, nonce=get_random_bytes(_NONCE_BYTES), mac_len=_TAG_BYTES
        )
        cipher.update(_AAD)
        ciphertext, tag = cipher.encrypt_and_digest(payload)
        token = self._encode(bytes(cipher.nonce) + bytes(tag) + bytes(ciphertext))
        return IssuedDeletionReceipt(token, expires_at)

    def verify(
        self, token: str, *, project_id: UUID, owner_id: UUID
    ) -> DeletionReceiptClaims:
        try:
            if not token or len(token) > _MAX_TOKEN_LENGTH:
                raise ValueError
            raw = self._decode(token)
            if len(raw) <= _NONCE_BYTES + _TAG_BYTES:
                raise ValueError
            nonce = raw[:_NONCE_BYTES]
            tag = raw[_NONCE_BYTES : _NONCE_BYTES + _TAG_BYTES]
            ciphertext = raw[_NONCE_BYTES + _TAG_BYTES :]
            cipher = AES.new(self._key, AES.MODE_GCM, nonce=nonce, mac_len=_TAG_BYTES)
            cipher.update(_AAD)
            claims = self._parse_claims(cipher.decrypt_and_verify(ciphertext, tag))
            if claims.project_id != project_id or claims.owner_id != owner_id:
                raise ValueError
            if self._utc_now() >= claims.expires_at:
                raise ValueError
            if claims.expires_at - claims.issued_at != self._ttl:
                raise ValueError
            return claims
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise DomainError(*_INVALID_RECEIPT) from error

    def _parse_claims(self, payload: bytes) -> DeletionReceiptClaims:
        parsed: Any = json.loads(payload)
        if not isinstance(parsed, dict) or set(parsed) != {
            "v",
            "project_id",
            "owner_id",
            "issued_at",
            "expires_at",
        }:
            raise ValueError
        if parsed["v"] != _TOKEN_VERSION:
            raise ValueError
        issued_at = datetime.fromtimestamp(self._exact_int(parsed["issued_at"]), UTC)
        expires_at = datetime.fromtimestamp(self._exact_int(parsed["expires_at"]), UTC)
        return DeletionReceiptClaims(
            UUID(self._exact_str(parsed["project_id"])),
            UUID(self._exact_str(parsed["owner_id"])),
            issued_at,
            expires_at,
        )

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deletion receipt clock must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _exact_int(value: object) -> int:
        if type(value) is not int:
            raise ValueError
        return value

    @staticmethod
    def _exact_str(value: object) -> str:
        if not isinstance(value, str):
            raise ValueError
        return value

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode(value: str) -> bytes:
        padding = "=" * (-len(value) % 4)
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
