"""Module for Storage utility functions"""

from datetime import datetime, timedelta, timezone


def datetime_now() -> datetime:
    """
    Factory function to return the current datetime.

    Returns:
        datetime: current datetime.
    """
    return datetime.now(timezone.utc)


def datetime_plus_one_hour() -> datetime:
    """
    Factory function to return the current datetime plus one hour.

    Returns:
        datetime: current datetime plus one hour.
    """
    return datetime.now(timezone.utc) + timedelta(hours=1)
