"""Shared error type for service handlers.

A service's handle(params) raises ServiceError to report a problem —
gateway.py catches it and maps status_code/message straight into the
HTTP response, so individual services never touch Flask/HTTP directly.
"""


class ServiceError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
