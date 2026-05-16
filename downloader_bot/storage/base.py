"""Abstract object-storage interface shared by all backend implementations."""

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
    async def __aenter__(self) -> "StorageBackend":
        """Enter the backend context; called once per worker process."""

    @abstractmethod
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Exit the backend context and release any SDK clients."""

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

        Args:
            name: Storage key (the blob/object name).
            data: Bytes, file-like, or async iterable of bytes. The async
                iterable arm is what the streaming-zip pipeline uses.
            ttl: How long the returned signed URL stays valid.
            overwrite: If ``True`` (default), replace any existing object at
                ``name``.
            content_type: Optional MIME type stamped on the stored object.
            download_filename: When set, the signed URL includes a
                Content-Disposition response override so browsers save the
                download under this name. Encoded per RFC 5987 for non-ASCII.

        Returns:
            A pre-signed read URL valid for ``ttl``.

        Raises:
            UploadError: Upload step failed.
            SignedUrlError: Upload succeeded but URL signing failed.
            StorageConfigError: Backend is misconfigured (non-recoverable).
        """

    @abstractmethod
    async def delete_blob(self, name: str) -> None:
        """Delete the blob at ``name``; missing blobs are success.

        Args:
            name: The storage key to delete.

        Raises:
            UploadError: A non-not-found backend error occurred. Implementations
                must wrap SDK exceptions in ``UploadError`` so callers don't
                depend on a specific SDK.
        """
