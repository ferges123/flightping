from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class CredentialError(ValueError):
    pass


class CredentialCipher:
    def __init__(self, key: str):
        try:
            self._fernet = Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            raise CredentialError("FPB_CREDENTIALS_KEY is not a valid Fernet key") from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise CredentialError("Stored AeroAPI credential cannot be decrypted") from exc
