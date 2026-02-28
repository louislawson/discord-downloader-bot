"""Azure Container Blob service module"""

from datetime import datetime, timedelta, timezone
import os
from typing import Dict

from azure.storage.blob import (
    BlobSasPermissions,
    BlobType,
    ContentSettings,
    generate_blob_sas,
)
from azure.storage.blob.aio import BlobClient, ContainerClient


class ContainerRepository:
    """
    A wrapper class for async interactions with Azure Blob Storage containers.

    This class provides high-level methods for uploading and generating SAS URLs
    using an injected `ContainerClient`.

    Attributes:
        con_client (ContainerClient): The Azure Blob Storage container client.

    Methods:
        create(): Uploads a new blob with optional metadata, tags, etc..
        sas_url(): Generate a SAS URL for a given blob.
    """

    def __init__(
        self,
    ) -> None:
        """Initializes the Container with a ContainerClient instance."""
        self.con_client = ContainerClient.from_connection_string(
            conn_str=os.getenv("ST_CONN_STR"),
            container_name=os.getenv("ST_CONTAINER"),
        )

    async def create(
        self,
        name: str,
        data: bytes | str,
        length: int | None = None,
        blob_type: BlobType = BlobType.BLOCKBLOB,
        metadata: Dict[str, str] | None = None,
        content_settings: ContentSettings | None = None,
        overwrite: bool = False,
        tags: Dict[str, str] | None = None,
    ) -> BlobClient:
        """
        Uploads a new blob to the container.

        Args:
            name (str): The name of the blob.
            data (bytes | str): The content of the blob.
            length (int, optional): The length of the data.
            blob_type (BlobType): The type of the blob.
            metadata (dict[str, str], optional): Metadata associated with the blob.
            content_settings (ContentSettings, optional): Blob content settings.
            overwrite (bool): Whether to overwrite an existing blob (by name).
            tags (dict[str, str], optional): Tags to associate with the blob.

        Returns:
            BlobClient: The client for the uploaded blob.
        """
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

    async def sas_url(
        self,
        blob_client: BlobClient,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> str:
        """
        Generates a SAS URL for a blob.

        Args:
            blob_client (BlobClient): BlobClient to generate a SAS for.
            valid_from (datetime, optional): SAS token valid from datetime.
            valid_to (datetime, optional): SAS token valid to datetime.

        Returns:
            str: The blob SAS URL.
        """
        if not valid_from:
            valid_from = datetime.now(timezone.utc)
        if not valid_to:
            valid_to = datetime.now(timezone.utc) + timedelta(hours=1)

        sas_token = generate_blob_sas(
            account_name=self.con_client.account_name,
            container_name=self.con_client.container_name,
            blob_name=blob_client.blob_name,
            account_key=self.con_client.credential.account_key,
            permission=BlobSasPermissions(read=True),
            expiry=valid_to,
            start=valid_from,
        )
        blob_client_sas = BlobClient.from_blob_url(
            blob_url=blob_client.url,
            credential=sas_token,
        )
        return blob_client_sas.url
