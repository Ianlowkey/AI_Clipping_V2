"""Gemini credentials backed exclusively by Windows Credential Manager."""

import logging
import re
import sys

from app.core.config import settings

logger = logging.getLogger(__name__)

SERVICE_NAME = "AIClipping.Gemini.APIKey"
ACCOUNT_NAME = "local-user"
_VALID_KEY = re.compile(r"^[A-Za-z0-9_-]{20,256}$")


class CredentialStoreUnavailable(RuntimeError):
    """Windows Credential Manager cannot be accessed by this process."""


def validate_api_key(value: object) -> str:
    if not isinstance(value, str) or not _VALID_KEY.fullmatch(value):
        raise ValueError("API key must be 20–256 letters, digits, underscores or hyphens.")
    return value


def _windows_backend():
    if sys.platform != "win32":
        raise CredentialStoreUnavailable("Windows Credential Manager is available only on Windows.")
    try:
        from keyring.backends.Windows import WinVaultKeyring

        return WinVaultKeyring()
    except Exception as exc:
        logger.warning("Windows Credential Manager backend is unavailable (%s)", type(exc).__name__)
        raise CredentialStoreUnavailable("Windows Credential Manager is unavailable. Install or repair the secure-storage support in the app environment.") from None


def read_saved_api_key() -> str | None:
    try:
        return _windows_backend().get_password(SERVICE_NAME, ACCOUNT_NAME)
    except CredentialStoreUnavailable:
        raise
    except Exception as exc:
        logger.warning("Could not read the Gemini credential from Windows Credential Manager (%s)", type(exc).__name__)
        raise CredentialStoreUnavailable("Windows Credential Manager could not be accessed. No insecure storage fallback is available.") from None


def get_api_key() -> str | None:
    """Prefer the saved credential; use the explicit environment setting as fallback."""
    try:
        saved = read_saved_api_key()
    except CredentialStoreUnavailable:
        # The environment variable is an explicit operator setting, not a storage downgrade.
        if settings.gemini_api_key:
            return settings.gemini_api_key
        raise
    return saved or settings.gemini_api_key


def save_api_key(value: str) -> None:
    try:
        _windows_backend().set_password(SERVICE_NAME, ACCOUNT_NAME, validate_api_key(value))
    except CredentialStoreUnavailable:
        raise
    except Exception as exc:
        logger.warning("Could not save the Gemini credential to Windows Credential Manager (%s)", type(exc).__name__)
        raise CredentialStoreUnavailable("The Gemini API key could not be saved to Windows Credential Manager.") from None


def delete_saved_api_key() -> None:
    try:
        backend = _windows_backend()
        if backend.get_password(SERVICE_NAME, ACCOUNT_NAME) is not None:
            backend.delete_password(SERVICE_NAME, ACCOUNT_NAME)
    except CredentialStoreUnavailable:
        raise
    except Exception as exc:
        logger.warning("Could not delete the Gemini credential from Windows Credential Manager (%s)", type(exc).__name__)
        raise CredentialStoreUnavailable("The saved Gemini API key could not be deleted from Windows Credential Manager.") from None


def configuration_status() -> dict:
    try:
        saved = read_saved_api_key()
        storage_available = True
    except CredentialStoreUnavailable:
        saved = None
        storage_available = False
    source = "credential_manager" if saved else "environment" if settings.gemini_api_key else "none"
    return {
        "configured": bool(saved or settings.gemini_api_key),
        "saved_in_credential_manager": bool(saved),
        "secure_storage_available": storage_available,
        "source": source,
        "model": settings.gemini_model,
        "key_environment_variable": "AICLIPPING_GEMINI_API_KEY",
    }
