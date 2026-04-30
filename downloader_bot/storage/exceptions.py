"""Custom exceptions for the storage layer."""


class StorageConfigError(Exception):
    """Raised when the StorageBackend cannot be configured."""


class UploadError(Exception):
    """Raised when a blob upload fails."""


class SignedUrlError(Exception):
    """Raised when a SAS URL cannot be generated for a blob."""
