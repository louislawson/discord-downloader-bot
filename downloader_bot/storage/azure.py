"""Azure Blob Storage backend."""

from collections.abc import AsyncIterable
from datetime import UTC, datetime, timedelta
from typing import IO
from urllib import parse

from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.storage.blob import BlobSasPermissions, ContentSettings, generate_blob_sas
from azure.storage.blob.aio import BlobClient, ContainerClient

from downloader_bot.config import settings
from downloader_bot.storage.base import StorageBackend
from downloader_bot.storage.exceptions import SignedUrlError, UploadError


def _build_client() -> ContainerClient:
    """Build a ContainerClient tuned for long streaming uploads.

    Defaults bumped from Azure SDK's stock values:
    - read_timeout: 60s -> 600s. A single Put Block on a multi-GB stream
      can legitimately take minutes; the default falsely surfaces slow
      backends as failures.
    - retry_total: 10 -> 5. We don't want 10 retries on a permanent
      failure prolonging worker-side cleanup; 5 is plenty for transients.
    - retry_backoff_max: 120s left at default — caps how long any single
      retry waits.
    """
    return ContainerClient.from_connection_string(
        conn_str=settings.AZURE_CONN_STR,
        container_name=settings.AZURE_CONTAINER,
        connection_timeout=20,
        read_timeout=600,
        retry_total=5,
        retry_connect=3,
        retry_read=3,
        retry_status=3,
    )


def _format_content_disposition(filename: str) -> str:
    """Build a Content-Disposition value that browsers honour for any filename.

    RFC 6266 + RFC 5987: emit ``filename="..."`` with ASCII fallback for
    legacy clients, and ``filename*=UTF-8''...`` for the real value.
    Modern browsers prefer the starred form when both are present.
    """
    try:
        filename.encode("ascii")
        return f'attachment; filename="{filename}"'
    except UnicodeEncodeError:
        ascii_fallback = filename.encode("ascii", "replace").decode("ascii")
        encoded = parse.quote(filename, safe="")
        return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


class AzureBlobBackend(StorageBackend):
    """Async Azure Blob Storage implementation of ``StorageBackend``.

    Supports dependency injection of a ``ContainerClient`` for testability.
    If no client is provided, one is built from the centralised
    ``settings`` object (which validates required values at startup).

    All ``AzureError``s are caught and re-raised as ``UploadError`` /
    ``SignedUrlError`` so callers can handle failures without depending on
    azure-storage-blob.

    Usage::

        # Production — reads from settings
        async with AzureBlobBackend() as backend:
            url = await backend.upload_and_sign(name="file.zip", data=data)

        # Testing — inject a mock client
        async with AzureBlobBackend(client=mock_client) as backend:
            ...
    """

    def __init__(self, client: ContainerClient | None = None) -> None:
        """Wrap an injected ``ContainerClient`` or build one from settings.

        Args:
            client: Optional pre-built client (used by unit tests). If
                ``None``, one is built from ``settings`` via
                :func:`_build_client`.
        """
        self.con_client: ContainerClient = client or _build_client()

    async def __aenter__(self) -> "AzureBlobBackend":
        """Enter the backend context; the SDK client is already open."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Close the underlying ``ContainerClient``."""
        await self.con_client.close()
        return False

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
        """Upload ``data`` under key ``name`` and return a SAS URL valid for ``ttl``.

        Raises:
            UploadError: ``upload_blob`` failed.
            SignedUrlError: signing failed, or the configured credential
                lacks an account key (SAS-token-only / MSI auth).
        """
        # Account-key precondition: SAS generation needs the raw key, not a
        # SAS-token-only credential. Failing here surfaces a config problem
        # before we waste an upload round-trip.
        credential = self.con_client.credential
        if not getattr(credential, "account_key", None):
            raise SignedUrlError(
                "SAS URL generation requires an account key credential. "
                "Ensure AZURE_CONN_STR contains an AccountKey, or pass a "
                "client configured with account key auth."
            )

        try:
            blob_client = await self.con_client.upload_blob(
                name=name,
                data=data,
                overwrite=overwrite,
                content_settings=ContentSettings(
                    content_type=content_type or "application/zip",
                ),
            )
        except AzureError as e:
            raise UploadError(
                f"Failed to upload blob '{name}' to container "
                f"'{self.con_client.container_name}': {e}"
            ) from e

        now = datetime.now(UTC)

        sas_kwargs = {
            "account_name": self.con_client.account_name,
            "container_name": self.con_client.container_name,
            "blob_name": blob_client.blob_name,
            "account_key": credential.account_key,
            "permission": BlobSasPermissions(read=True),
            "start": now,
            "expiry": now + ttl,
        }
        if download_filename is not None:
            # Adds rscd= to the SAS, overriding Content-Disposition on response.
            sas_kwargs["content_disposition"] = _format_content_disposition(
                download_filename
            )
        try:
            sas_token = generate_blob_sas(**sas_kwargs)
        except AzureError as e:
            raise SignedUrlError(
                f"Failed to generate SAS token for blob '{blob_client.blob_name}': {e}"
            ) from e

        url = BlobClient.from_blob_url(
            blob_url=blob_client.url,
            credential=sas_token,
        ).url

        # Azurite quirk: SAS URLs use the in-network hostname, but a user
        # opening the link from their browser needs the host-reachable one.
        if (
            settings.ENVIRONMENT == "dev"
            and settings.AZURE_INT_URL
            and settings.AZURE_EXT_URL
        ):
            url = url.replace(settings.AZURE_INT_URL, settings.AZURE_EXT_URL)
        return url

    async def delete_blob(self, name: str) -> None:
        """Delete blob ``name`` from the container. Missing blobs are success.

        Raises:
            UploadError: any non-not-found ``AzureError``. Caller treats this
                as best-effort cleanup; a failure here should not abort the
                request flow.
        """
        try:
            await self.con_client.delete_blob(name)
        except ResourceNotFoundError:
            return
        except AzureError as e:
            raise UploadError(
                f"Failed to delete blob '{name}' from container "
                f"'{self.con_client.container_name}': {e}"
            ) from e
