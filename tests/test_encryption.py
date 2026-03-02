"""
Tests for the encryption module.
"""

import pytest
from app.encryption import FieldEncryptor


class TestFieldEncryptor:

    def setup_method(self):
        self.encryptor = FieldEncryptor("test-encryption-key-for-testing!!")

    def test_encrypt_decrypt_roundtrip(self):
        """Encrypting then decrypting should return the original value."""
        original = "test@email.com"
        encrypted = self.encryptor.encrypt(original)
        decrypted = self.encryptor.decrypt(encrypted)
        assert decrypted == original

    def test_encrypted_is_different_from_original(self):
        """Encrypted value should not equal original."""
        original = "sensitive-data"
        encrypted = self.encryptor.encrypt(original)
        assert encrypted != original

    def test_encrypt_empty_string(self):
        """Should handle empty strings."""
        original = ""
        encrypted = self.encryptor.encrypt(original)
        decrypted = self.encryptor.decrypt(encrypted)
        assert decrypted == original

    def test_encrypt_unicode(self):
        """Should handle unicode characters."""
        original = "Dr. José García-López 日本語"
        encrypted = self.encryptor.encrypt(original)
        decrypted = self.encryptor.decrypt(encrypted)
        assert decrypted == original

    def test_different_keys_produce_different_ciphertexts(self):
        """Different keys should produce different encrypted outputs."""
        enc1 = FieldEncryptor("key-one-xxxxxxxxxxxxxxxxxxxxxxxxx")
        enc2 = FieldEncryptor("key-two-xxxxxxxxxxxxxxxxxxxxxxxxx")
        original = "test@email.com"
        assert enc1.encrypt(original) != enc2.encrypt(original)

    def test_wrong_key_cannot_decrypt(self):
        """Decrypting with wrong key should fail."""
        enc1 = FieldEncryptor("key-one-xxxxxxxxxxxxxxxxxxxxxxxxx")
        enc2 = FieldEncryptor("key-two-xxxxxxxxxxxxxxxxxxxxxxxxx")
        encrypted = enc1.encrypt("secret")
        with pytest.raises(Exception):
            enc2.decrypt(encrypted)
