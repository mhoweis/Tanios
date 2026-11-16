"""Encryption for secrets kept in the database, such as Google sign-in tokens.

The key comes from TANIOS_ENCRYPTION_KEY and never leaves the server's settings.
"""

from cryptography.fernet import Fernet, InvalidToken


class SealError(Exception):
    """The key is not usable, or the stored value was not sealed with it."""


class TokenBox:
    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode())
        except (ValueError, TypeError):
            raise SealError("TANIOS_ENCRYPTION_KEY is not a valid key") from None

    def seal(self, secret: str) -> str:
        return self._fernet.encrypt(secret.encode()).decode()

    def open(self, sealed: str) -> str:
        try:
            return self._fernet.decrypt(sealed.encode()).decode()
        except (InvalidToken, ValueError):
            raise SealError("stored value cannot be opened with this key") from None
