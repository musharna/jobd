"""HTTP client wrapper for the jobd broker. Shared by job_cli and jobd.mcp."""

from __future__ import annotations


class BrokerUnreachable(Exception):
    """Network failure — DNS, connect refused, TLS, connect timeout."""


class BrokerServerError(Exception):
    """Broker returned 5xx."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class BrokerRefusal(Exception):
    """Broker returned 4xx with a `detail` body."""

    def __init__(self, message: str, *, status_code: int, detail: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail
