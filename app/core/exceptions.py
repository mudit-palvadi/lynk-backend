from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class LynkException(Exception):
    def __init__(
        self, message: str, status_code: int = 500, *, headers: dict[str, str] | None = None
    ):
        self.message = message
        self.status_code = status_code
        self.headers = headers
        super().__init__(self.message)


class UnauthorizedException(LynkException):
    def __init__(self, message: str = "Missing or invalid credential"):
        super().__init__(message, status_code=401)


class InvalidInputException(LynkException):
    def __init__(self, message: str):
        super().__init__(message, status_code=400)


class TransferNotFoundException(LynkException):
    def __init__(self, message: str = "Transfer not found or expired"):
        super().__init__(message, status_code=404)


class TransferNotReadyException(LynkException):
    def __init__(self, message: str = "Transfer is not ready"):
        super().__init__(message, status_code=409)


class LimitExceededException(LynkException):
    def __init__(self, message: str):
        super().__init__(message, status_code=413)


class RateLimitExceededException(LynkException):
    def __init__(self, message: str = "Rate limit exceeded", *, retry_after: int | None = None):
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
        super().__init__(message, status_code=429, headers=headers)


class ServiceUnavailableException(LynkException):
    def __init__(self, message: str = "Redis or R2 storage unavailable"):
        super().__init__(message, status_code=503)


def register_exception_handlers(app: FastAPI):
    """Registers global handlers to convert LynkExceptions into JSONResponses."""

    @app.exception_handler(LynkException)
    async def lynk_exception_handler(request: Request, exc: LynkException):
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.message}, headers=exc.headers
        )
