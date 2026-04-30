"""Tests for ``get_storage_backend()`` — the provider dispatcher.

Covers the two branches the factory has to get right: returning the right
concrete backend for a known ``STORAGE_BACKEND`` value, and surfacing
``StorageConfigError`` for an unknown one. The latter case bypasses pydantic
validation by monkeypatching the settings instance directly, since
``Settings`` already constrains ``STORAGE_BACKEND`` to a ``Literal``.
"""

import pytest

from downloader_bot.config import settings
from downloader_bot.storage import get_storage_backend
from downloader_bot.storage.azure import AzureBlobBackend
from downloader_bot.storage.exceptions import StorageConfigError


class TestGetStorageBackend:
    async def test_returns_azure_backend_for_azure(self):
        async with get_storage_backend() as backend:
            assert isinstance(backend, AzureBlobBackend)

    async def test_raises_for_unknown_backend(self, monkeypatch):
        monkeypatch.setattr(settings, "STORAGE_BACKEND", "totally-fake")

        with pytest.raises(StorageConfigError, match="Unknown STORAGE_BACKEND"):
            get_storage_backend()
