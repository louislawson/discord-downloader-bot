"""Unit tests for AzureBlobBackend — Azure Blob Storage wrapper.

The repository takes an injected ``ContainerClient`` for testability, so these
tests never touch the real Azure SDK or Azurite.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from azure.core.exceptions import AzureError

from downloader_bot.storage.azure import AzureBlobBackend
from downloader_bot.storage.exceptions import SignedUrlError, UploadError


class TestCreate:
    async def test_returns_blob_client_on_success(
        self,
        mock_azure_client,
        mock_blob_client,
    ):
        async with AzureBlobBackend(client=mock_azure_client) as repo:
            blob = await repo.create(name="x.zip", data=b"abc", overwrite=True)

        assert blob is mock_blob_client
        mock_azure_client.upload_blob.assert_awaited_once()

    async def test_wraps_azure_error_as_blob_upload_error(
        self,
        mock_azure_client,
    ):
        mock_azure_client.upload_blob = AsyncMock(
            side_effect=AzureError("boom"),
        )
        repo = AzureBlobBackend(client=mock_azure_client)

        with pytest.raises(UploadError, match="Failed to upload blob"):
            await repo.create(name="x.zip", data=b"abc")


class TestSasUrl:
    async def test_raises_when_account_key_is_falsy(self, mock_azure_client):
        mock_azure_client.credential.account_key = ""
        repo = AzureBlobBackend(client=mock_azure_client)

        with pytest.raises(SignedUrlError, match="account key"):
            await repo.sas_url(blob_name="x.zip", blob_url="http://blob/x.zip")

    async def test_happy_path_returns_signed_url(
        self,
        mocker,
        mock_azure_client,
    ):
        mocker.patch(
            "downloader_bot.storage.azure.generate_blob_sas",
            return_value="sastoken123",
        )
        mocker.patch(
            "downloader_bot.storage.azure.BlobClient.from_blob_url",
            return_value=MagicMock(url="http://blob/x.zip?sastoken123"),
        )
        repo = AzureBlobBackend(client=mock_azure_client)

        url = await repo.sas_url(blob_name="x.zip", blob_url="http://blob/x.zip")

        assert url == "http://blob/x.zip?sastoken123"

    async def test_wraps_azure_error_as_sas_generation_error(
        self,
        mocker,
        mock_azure_client,
    ):
        mocker.patch(
            "downloader_bot.storage.azure.generate_blob_sas",
            side_effect=AzureError("boom"),
        )
        repo = AzureBlobBackend(client=mock_azure_client)

        with pytest.raises(SignedUrlError, match="Failed to generate"):
            await repo.sas_url(blob_name="x.zip", blob_url="http://blob/x.zip")


class TestAsyncContextManager:
    async def test_aexit_closes_client_on_clean_exit(self, mock_azure_client):
        async with AzureBlobBackend(client=mock_azure_client):
            pass

        mock_azure_client.close.assert_awaited_once()

    async def test_aexit_closes_client_when_body_raises(self, mock_azure_client):
        with pytest.raises(RuntimeError, match="boom"):
            async with AzureBlobBackend(client=mock_azure_client):
                raise RuntimeError("boom")

        mock_azure_client.close.assert_awaited_once()
