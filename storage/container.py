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


def datetime_now() -> datetime:
    """
    Factory function to return the current datetime.

    This function is used by QueryParams to provide an up-to-date value when
    they are NOT passed in the URL.

    See:
        https://docs.pydantic.dev/latest/concepts/models/#fields-with-dynamic-default-values
        https://github.com/fastapi/fastapi/discussions/9802#discussioncomment-6377392

    Returns:
        datetime: current datetime.
    """
    return datetime.now(timezone.utc)


def datetime_plus_one_hour() -> datetime:
    """
    Factory function to return the current datetime plus one hour.

    This function is used by QueryParams to provide an up-to-date value when
    they are NOT passed in the URL.

    See:
        https://docs.pydantic.dev/latest/concepts/models/#fields-with-dynamic-default-values
        https://github.com/fastapi/fastapi/discussions/9802#discussioncomment-6377392

    Returns:
        datetime: current datetime plus one hour.
    """
    return datetime.now(timezone.utc) + timedelta(hours=1)


class ContainerRepository:
    """
    A wrapper class for async interactions with Azure Blob Storage containers.

    This class provides high-level methods for listing, querying, uploading, and
    deleting blobs using an injected `ContainerClient`. It supports blob metadata,
    tags, and content settings, and includes internal validation for tag constraints.

    Attributes:
        con_client (ContainerClient): The Azure Blob Storage container client.

    Methods:
        list(): Lists blobs in the container with optional filters and metadata.
        get(): Queries blobs by tag expressions.
        create(): Uploads a new blob with optional metadata, tags, etc..
        delete(): Deletes one or more blobs from the container.
        sas_url(): Generate a SAS URL for a given blob.
    """

    def __init__(
        self,
    ) -> None:
        """
        Initializes the Container with a ContainerClient instance.

        Args:
            con_client (ContainerClient): The Azure Blob Storage container client.
        """
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
        valid_from: datetime = datetime_now(),
        valid_to: datetime = datetime_plus_one_hour(),
    ) -> str:
        """
        Generates a SAS URL for a blob.

        Args:
            blob_client (BlobClient): BlobClient to generate a SAS for.
            valid_from (datetime): SAS token valid from datetime.
            valid_to (datetime): SAS token valid to datetime.

        Returns:
            str: The blob SAS URL.
        """
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
