class LectioAdapterError(RuntimeError):
    """Base error for failures at the Lectio adapter boundary."""


class LectioSessionExpired(LectioAdapterError):
    """The Lectio response indicates that the stored session is no longer valid."""


class LectioResponseChanged(LectioAdapterError):
    """Lectio returned a page that no longer matches the adapter's known structure."""
