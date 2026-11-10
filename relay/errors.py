from __future__ import annotations


class RelayError(Exception):
    """An API-visible error: HTTP status + machine-readable code + human message."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def body(self) -> dict:
        return {"error": {"message": self.message, "code": self.code}}
