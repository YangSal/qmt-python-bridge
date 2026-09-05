"""External adapters for the QMT embedded-Python data bridge."""


class QmtDataError(RuntimeError):
    """Bridge failure: must not be interpreted as an empty trading day."""
