"""Unit tests for AzureBlobBackend — Azure Blob Storage implementation.

The backend takes an injected ``ContainerClient`` for testability, so these
tests never touch the real Azure SDK or Azurite. They verify the public
``StorageBackend`` contract (``upload_and_sign`` + ``delete_blob`` +
async-CM dunders) rather than internal helpers.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from azure.core.exceptions import AzureError, ResourceNotFoundError

from downloader_bot.config import settings
from downloader_bot.storage.azure import AzureBlobBackend, _format_content_disposition
from downloader_bot.storage.exceptions import SignedUrlError, UploadError


@pytest.fixture
def patched_sas(mocker):
    """Patch SAS token generation + URL composition to return a stable URL."""
    mocker.patch(
        "downloader_bot.storage.azure.generate_blob_sas",
        return_value="sastoken123",
    )
    mocker.patch(
        "downloader_bot.storage.azure.BlobClient.from_blob_url",
        return_value=MagicMock(url="http://blob/x.zip?sastoken123"),
    )


class TestUploadAndSign:
    async def test_happy_path_returns_signed_url(
        self,
        monkeypatch,
        mock_azure_client,
        patched_sas,
    ):
        # Pin to prod so the dev-mode URL rewrite branch doesn't interfere.
        monkeypatch.setattr(settings, "ENVIRONMENT", "prod")

        async with AzureBlobBackend(client=mock_azure_client) as backend:
            url = await backend.upload_and_sign(name="x.zip", data=b"abc")

        assert url == "http://blob/x.zip?sastoken123"
        mock_azure_client.upload_blob.assert_awaited_once()

    async def test_dev_mode_rewrites_internal_host_to_external(
        self,
        mocker,
        mock_azure_client,
    ):
        # The mocked URL contains AZURE_INT_URL (set in conftest) so the
        # rewrite has something to replace.
        mocker.patch(
            "downloader_bot.storage.azure.generate_blob_sas",
            return_value="sastoken123",
        )
        mocker.patch(
            "downloader_bot.storage.azure.BlobClient.from_blob_url",
            return_value=MagicMock(
                url="http://azurite:10000/devstoreaccount1/media/x.zip?sastoken123"
            ),
        )

        async with AzureBlobBackend(client=mock_azure_client) as backend:
            url = await backend.upload_and_sign(name="x.zip", data=b"abc")

        assert url.startswith("http://localhost:10000/")
        assert "azurite" not in url

    async def test_missing_account_key_raises_before_upload(self, mock_azure_client):
        mock_azure_client.credential.account_key = ""
        backend = AzureBlobBackend(client=mock_azure_client)

        with pytest.raises(SignedUrlError, match="account key"):
            await backend.upload_and_sign(name="x.zip", data=b"abc")
        mock_azure_client.upload_blob.assert_not_awaited()

    async def test_upload_failure_raises_upload_error(
        self,
        mock_azure_client,
        patched_sas,
    ):
        mock_azure_client.upload_blob = AsyncMock(side_effect=AzureError("boom"))
        backend = AzureBlobBackend(client=mock_azure_client)

        with pytest.raises(UploadError, match="Failed to upload blob"):
            await backend.upload_and_sign(name="x.zip", data=b"abc")

    async def test_sign_failure_raises_signed_url_error(
        self,
        mocker,
        mock_azure_client,
    ):
        mocker.patch(
            "downloader_bot.storage.azure.generate_blob_sas",
            side_effect=AzureError("boom"),
        )
        backend = AzureBlobBackend(client=mock_azure_client)

        with pytest.raises(SignedUrlError, match="Failed to generate"):
            await backend.upload_and_sign(name="x.zip", data=b"abc")


class TestContentTypeFlowThrough:
    async def test_default_content_type_is_application_zip(
        self,
        mock_azure_client,
        patched_sas,
        monkeypatch,
    ):
        # The downloader always produces zip archives — application/zip
        # is the right default for browsers that go on Content-Type.
        monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
        backend = AzureBlobBackend(client=mock_azure_client)

        await backend.upload_and_sign(name="x.zip", data=b"abc")

        content_settings = mock_azure_client.upload_blob.await_args.kwargs[
            "content_settings"
        ]
        assert content_settings.content_type == "application/zip"

    async def test_custom_content_type_overrides_default(
        self,
        mock_azure_client,
        patched_sas,
        monkeypatch,
    ):
        monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
        backend = AzureBlobBackend(client=mock_azure_client)

        await backend.upload_and_sign(
            name="x.bin",
            data=b"abc",
            content_type="application/octet-stream",
        )

        content_settings = mock_azure_client.upload_blob.await_args.kwargs[
            "content_settings"
        ]
        assert content_settings.content_type == "application/octet-stream"


class TestDownloadFilename:
    async def test_filename_added_to_sas_via_content_disposition(
        self,
        mocker,
        mock_azure_client,
        monkeypatch,
    ):
        # download_filename plumbs through to generate_blob_sas as
        # content_disposition (rscd= on the SAS URL), so the same blob
        # can be served under different filenames per URL.
        monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
        gen = mocker.patch(
            "downloader_bot.storage.azure.generate_blob_sas",
            return_value="sastoken123",
        )
        mocker.patch(
            "downloader_bot.storage.azure.BlobClient.from_blob_url",
            return_value=MagicMock(url="http://blob/x.zip?sastoken123"),
        )
        backend = AzureBlobBackend(client=mock_azure_client)

        await backend.upload_and_sign(
            name="x.zip",
            data=b"abc",
            download_filename="channel-general.zip",
        )

        kwargs = gen.call_args.kwargs
        assert "content_disposition" in kwargs
        assert "channel-general.zip" in kwargs["content_disposition"]

    async def test_no_filename_no_content_disposition(
        self,
        mocker,
        mock_azure_client,
        monkeypatch,
    ):
        monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
        gen = mocker.patch(
            "downloader_bot.storage.azure.generate_blob_sas",
            return_value="sastoken123",
        )
        mocker.patch(
            "downloader_bot.storage.azure.BlobClient.from_blob_url",
            return_value=MagicMock(url="http://blob/x.zip?sastoken123"),
        )
        backend = AzureBlobBackend(client=mock_azure_client)

        await backend.upload_and_sign(name="x.zip", data=b"abc")

        assert "content_disposition" not in gen.call_args.kwargs


class TestFormatContentDisposition:
    def test_ascii_filename_uses_simple_form(self):
        result = _format_content_disposition("channel-general.zip")

        assert result == 'attachment; filename="channel-general.zip"'

    def test_non_ascii_uses_rfc_5987_dual_form(self):
        # Browsers prefer filename*= when both are present; legacy
        # clients fall back to filename=.
        result = _format_content_disposition("naïve_😀.zip")

        assert 'filename="' in result
        assert "filename*=UTF-8''" in result

    def test_non_ascii_percent_encoded(self):
        result = _format_content_disposition("héllo.zip")

        # The starred form is percent-encoded UTF-8.
        assert "%C3%A9" in result  # é = 0xC3 0xA9


class TestTtl:
    async def test_default_ttl_is_24h(
        self,
        mocker,
        mock_azure_client,
        monkeypatch,
    ):
        monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
        gen = mocker.patch(
            "downloader_bot.storage.azure.generate_blob_sas",
            return_value="sastoken123",
        )
        mocker.patch(
            "downloader_bot.storage.azure.BlobClient.from_blob_url",
            return_value=MagicMock(url="http://blob/x.zip?sastoken123"),
        )
        backend = AzureBlobBackend(client=mock_azure_client)

        await backend.upload_and_sign(name="x.zip", data=b"abc")

        start = gen.call_args.kwargs["start"]
        expiry = gen.call_args.kwargs["expiry"]
        assert expiry - start == timedelta(hours=24)

    async def test_custom_ttl_propagates(
        self,
        mocker,
        mock_azure_client,
        monkeypatch,
    ):
        monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
        gen = mocker.patch(
            "downloader_bot.storage.azure.generate_blob_sas",
            return_value="sastoken123",
        )
        mocker.patch(
            "downloader_bot.storage.azure.BlobClient.from_blob_url",
            return_value=MagicMock(url="http://blob/x.zip?sastoken123"),
        )
        backend = AzureBlobBackend(client=mock_azure_client)

        await backend.upload_and_sign(
            name="x.zip",
            data=b"abc",
            ttl=timedelta(hours=2),
        )

        start = gen.call_args.kwargs["start"]
        expiry = gen.call_args.kwargs["expiry"]
        assert expiry - start == timedelta(hours=2)


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


class TestDeleteBlob:
    async def test_delete_blob_calls_container_delete_blob(self, mock_azure_client):
        backend = AzureBlobBackend(client=mock_azure_client)

        await backend.delete_blob("x.zip")

        mock_azure_client.delete_blob.assert_awaited_once_with("x.zip")

    async def test_delete_blob_swallows_resource_not_found(self, mock_azure_client):
        mock_azure_client.delete_blob = AsyncMock(
            side_effect=ResourceNotFoundError("missing")
        )
        backend = AzureBlobBackend(client=mock_azure_client)

        # Should not raise — a missing blob is success for cleanup callers.
        await backend.delete_blob("x.zip")

    async def test_delete_blob_reraises_other_azure_errors_as_upload_error(
        self,
        mock_azure_client,
    ):
        mock_azure_client.delete_blob = AsyncMock(side_effect=AzureError("boom"))
        backend = AzureBlobBackend(client=mock_azure_client)

        with pytest.raises(UploadError, match="Failed to delete blob"):
            await backend.delete_blob("x.zip")
