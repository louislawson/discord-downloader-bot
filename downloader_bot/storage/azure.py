"""Azure Blob Storage backend."""

from datetime import UTC, datetime, timedelta
from typing import IO

from azure.core.exceptions import AzureError
from azure.storage.blob import BlobSasPermissions, generate_blob_sas
from azure.storage.blob.aio import BlobClient, ContainerClient

from downloader_bot.config import settings
from downloader_bot.storage.base import StorageBackend
from downloader_bot.storage.exceptions import SignedUrlError, UploadError


def _build_client() -> ContainerClient:
    """Build a ContainerClient from the centralised settings object."""
    return ContainerClient.from_connection_string(
        conn_str=settings.AZURE_CONN_STR,
        container_name=settings.AZURE_CONTAINER,
    )


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
        self.con_client: ContainerClient = client or _build_client()

    async def __aenter__(self) -> "AzureBlobBackend":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        await self.con_client.close()
        return False

    async def upload_and_sign(
        self,
        name: str,
        data: bytes | IO[bytes],
        *,
        ttl: timedelta = timedelta(hours=1),
        overwrite: bool = True,
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
            )
        except AzureError as e:
            raise UploadError(
                f"Failed to upload blob '{name}' to container "
                f"'{self.con_client.container_name}': {e}"
            ) from e

        now = datetime.now(UTC)
        try:
            sas_token = generate_blob_sas(
                account_name=self.con_client.account_name,
                container_name=self.con_client.container_name,
                blob_name=blob_client.blob_name,
                account_key=credential.account_key,
                permission=BlobSasPermissions(read=True),
                start=now,
                expiry=now + ttl,
            )
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
