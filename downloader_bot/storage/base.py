from abc import ABC, abstractmethod
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
        data: bytes | IO[bytes],
        *,
        ttl: timedelta = timedelta(hours=1),
        overwrite: bool = True,
    ) -> str:
        """Upload ``data`` under key ``name`` and return a pre-signed URL.

        Raises:
            UploadError: upload step failed.
            SignedUrlError: upload succeeded but URL signing failed.
            StorageConfigError: backend is misconfigured (non-recoverable).
        """
