from abc import ABC, abstractmethod
from collections.abc import AsyncIterable
from datetime import timedelta
from typing import IO


class StorageBackend(ABC):
    """Async object-storage backend producing pre-signed read URLs.

    Implementations must be usable as ``async with``; ``__aexit__`` is
    where SDK clients are closed. All errors must be raised as
    ``StorageConfigError`` / ``UploadError`` / ``SignedUrlError`` so
    callers don't depend on a particular SDK.
    """

    @abstractmethod
    async def __aenter__(self) -> "StorageBackend": ...

    @abstractmethod
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool: ...

    @abstractmethod
    async def upload_and_sign(
        self,
        name: str,
        data: bytes | IO[bytes] | AsyncIterable[bytes],
        *,
        ttl: timedelta = timedelta(hours=24),
        overwrite: bool = True,
        content_type: str | None = None,
        download_filename: str | None = None,
    ) -> str:
        """Upload ``data`` under key ``name`` and return a pre-signed URL.

        download_filename: when set, the SAS URL includes a
            Content-Disposition response override so browsers save the
            download under this name. Encoded per RFC 5987 for non-ASCII.

        Raises:
            UploadError: upload step failed.
            SignedUrlError: upload succeeded but URL signing failed.
            StorageConfigError: backend is misconfigured (non-recoverable).
        """

    @abstractmethod
    async def delete_blob(self, name: str) -> None:
        """Delete the blob at ``name``.

        Implementations must treat a missing blob as success (no-op). Other
        backend errors should re-raise as ``UploadError`` so callers can
        handle them without depending on a specific SDK.
        """
