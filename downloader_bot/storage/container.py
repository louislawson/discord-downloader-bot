"""Azure Container Blob service module."""

from datetime import UTC, datetime, timedelta

from azure.core.exceptions import AzureError
from azure.storage.blob import (
    BlobSasPermissions,
    BlobType,
    ContentSettings,
    generate_blob_sas,
)
from azure.storage.blob.aio import BlobClient, ContainerClient

from downloader_bot.config import settings
from downloader_bot.storage.exceptions import BlobUploadError, SasGenerationError


def _build_client() -> ContainerClient:
    """Build a ContainerClient from the centralised settings object."""
    return ContainerClient.from_connection_string(
        conn_str=settings.ST_CONN_STR,
        container_name=settings.ST_CONTAINER,
    )


class ContainerRepository:
    """
    A wrapper class for async interactions with Azure Blob Storage containers.

    Supports dependency injection of a ``ContainerClient`` for testability. If
    no client is provided, one is built from the centralised ``settings``
    object (which validates required values at startup).

    All Azure errors are caught and re-raised as domain-specific exceptions
    (``BlobUploadError``, ``SasGenerationError``) so callers can handle
    failure cases without depending on the azure-storage-blob package.

    Usage::

        # Production — reads from settings
        async with ContainerRepository() as repo:
            blob_client = await repo.create(name="file.zip", data=data)
            url = await repo.sas_url(blob_client.blob_name, blob_client.url)

        # Testing — inject a mock client
        async with ContainerRepository(client=mock_client) as repo:
            ...

    Attributes:
        con_client (ContainerClient): The Azure Blob Storage container client.
    """

    def __init__(self, client: ContainerClient | None = None) -> None:
        """
        Initialise the ContainerRepository.

        Args:
            client (ContainerClient, optional): An injected container client.
                If omitted, one is built from settings.
        """
        self.con_client: ContainerClient = client or _build_client()

    async def __aenter__(self) -> "ContainerRepository":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        await self.con_client.close()
        return False

    async def create(
        self,
        name: str,
        data: bytes | str,
        length: int | None = None,
        blob_type: BlobType = BlobType.BLOCKBLOB,
        metadata: dict[str, str] | None = None,
        content_settings: ContentSettings | None = None,
        overwrite: bool = False,
        tags: dict[str, str] | None = None,
    ) -> BlobClient:
        """
        Upload a new blob to the container.

        Args:
            name (str): The name of the blob.
            data (bytes | str): The content of the blob.
            length (int, optional): The length of the data in bytes.
            blob_type (BlobType): The type of blob to create.
            metadata (dict[str, str], optional): Metadata to associate with the blob.
            content_settings (ContentSettings, optional): Blob content settings
                (e.g. content type, encoding).
            overwrite (bool): Whether to overwrite an existing blob of the same name.
            tags (dict[str, str], optional): Index tags to associate with the blob.

        Returns:
            BlobClient: A client for the uploaded blob.

        Raises:
            BlobUploadError: If the upload fails for any reason.
        """
        try:
            return await self.con_client.upload_blob(
                name=name,
                data=data,
                blob_type=blob_type,
                length=length,
                metadata=metadata,
                content_settings=content_settings,
                overwrite=overwrite,
                tags=tags,
            )
        except AzureError as e:
            raise BlobUploadError(
                f"Failed to upload blob '{name}' to container "
                f"'{self.con_client.container_name}': {e}"
            ) from e

    async def sas_url(
        self,
        blob_name: str,
        blob_url: str,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> str:
        """
        Generate a time-limited SAS URL for a blob.

        Args:
            blob_name (str): The name of the blob.
            blob_url (str): The base URL of the blob (without SAS token).
            valid_from (datetime, optional): When the SAS token becomes valid.
                Defaults to now (UTC).
            valid_to (datetime, optional): When the SAS token expires.
                Defaults to one hour from now (UTC).

        Returns:
            str: A fully-signed SAS URL for the blob.

        Raises:
            SasGenerationError: If the credential is incompatible or Azure
                SAS generation fails.
        """
        credential = self.con_client.credential
        if not hasattr(credential, "account_key") or not credential.account_key:
            raise SasGenerationError(
                "SAS URL generation requires an account key credential. "
                "Ensure ST_CONN_STR contains an AccountKey, or pass a client "
                "configured with account key auth."
            )

        valid_from = valid_from or datetime.now(UTC)
        valid_to = valid_to or datetime.now(UTC) + timedelta(hours=1)

        try:
            sas_token = generate_blob_sas(
                account_name=self.con_client.account_name,
                container_name=self.con_client.container_name,
                blob_name=blob_name,
                account_key=credential.account_key,
                permission=BlobSasPermissions(read=True),
                expiry=valid_to,
                start=valid_from,
            )
        except AzureError as e:
            raise SasGenerationError(
                f"Failed to generate SAS token for blob '{blob_name}': {e}"
            ) from e

        return BlobClient.from_blob_url(
            blob_url=blob_url,
            credential=sas_token,
        ).url
