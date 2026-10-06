class PermanentError(Exception):
    """Retrying cannot help (corrupt or encrypted file, unsupported content). The job fails
    immediately instead of burning attempts and landing in the dead-letter state."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message or code
        super().__init__(f"{code}: {self.message}")


class RetryableError(Exception):
    """A transient failure (dependency down, timeout, crash). Retried with backoff."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message or code
        super().__init__(f"{code}: {self.message}")
