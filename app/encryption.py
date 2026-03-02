"""
Field-level encryption for sensitive data (email, doctor info, medicines).
Uses Fernet symmetric encryption from the cryptography library.
"""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet

from app.config import get_settings


class FieldEncryptor:
    """Encrypt/decrypt individual field values using Fernet."""

    def __init__(self, key: str):
        # Derive a proper 32-byte key from the config key
        key_bytes = hashlib.sha256(key.encode()).digest()
        fernet_key = base64.urlsafe_b64encode(key_bytes)
        self._fernet = Fernet(fernet_key)

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a string, return base64-encoded ciphertext."""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a base64-encoded ciphertext back to string."""
        return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")


@lru_cache()
def get_encryptor() -> FieldEncryptor:
    """Return cached encryptor singleton."""
    settings = get_settings()
    return FieldEncryptor(settings.encryption_key)
