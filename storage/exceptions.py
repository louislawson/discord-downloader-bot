"""Custom exceptions for the storage layer."""


class ContainerConfigError(Exception):
    """
    Raised when the ContainerRepository cannot be configured.

    This typically means a required environment variable (ST_CONN_STR or
    ST_CONTAINER) is missing or the credential type is incompatible with
    the requested operation.
    """


class BlobUploadError(Exception):
    """
    Raised when a blob upload to Azure fails.

    Wraps the underlying AzureError so callers don't need to import
    azure-storage-blob just to handle upload failures.
    """


class SasGenerationError(Exception):
    """
    Raised when a SAS URL cannot be generated for a blob.

    Wraps credential mismatches and Azure-side failures.
    """
