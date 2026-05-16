"""Storage backend dispatcher.

Selects the concrete backend named by ``settings.STORAGE_BACKEND`` and lazy-
imports its module so deployments without that provider's SDK installed
don't fail at import time.
"""

from downloader_bot.config import settings
from downloader_bot.storage.base import StorageBackend
from downloader_bot.storage.exceptions import (
    SignedUrlError,
    StorageConfigError,
    UploadError,
)


def get_storage_backend() -> StorageBackend:
    """Return the configured backend; caller wraps it in ``async with``.

    Returns:
        A ``StorageBackend`` instance matching ``settings.STORAGE_BACKEND``.

    Raises:
        StorageConfigError: ``settings.STORAGE_BACKEND`` names an unknown
            backend.
    """
    backend = settings.STORAGE_BACKEND
    if backend == "azure":
        # Lazy import keeps non-azure deployments from importing azure-storage-blob
        from downloader_bot.storage.azure import AzureBlobBackend

        return AzureBlobBackend()
    raise StorageConfigError(f"Unknown STORAGE_BACKEND: {backend!r}")


__all__ = [
    "SignedUrlError",
    "StorageBackend",
    "StorageConfigError",
    "UploadError",
    "get_storage_backend",
]
