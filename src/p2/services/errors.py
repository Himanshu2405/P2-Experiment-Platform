"""Errors the UI shows to people. All derive from PlatformError so a page can catch one type."""


class PlatformError(Exception):
    pass


class PermissionDenied(PlatformError):
    pass


class NotFound(PlatformError):
    pass


class Conflict(PlatformError):
    """Someone else changed the same thing first; reload and try again."""


class InvalidInput(PlatformError, ValueError):
    pass


class TransientError(PlatformError):
    """A temporary problem (BigQuery busy, rate limited or briefly unavailable). Trying again later usually works."""


_TRANSIENT_CODES = (429, 500, 502, 503, 504)
_TRANSIENT_REASONS = {"rateLimitExceeded", "jobRateLimitExceeded", "quotaExceeded", "backendError", "internalError", "jobBackendError",
                      "resourcesExceeded", "timeout"}
_TRANSIENT_WORDS = ("rate limit", "too many concurrent", "too many dml", "resources exceeded", "service unavailable", "backend error",
                    "deadline exceeded", "timed out", "connection reset", "connection aborted", "temporarily unavailable", "quota exceeded")


def is_transient(problem) -> bool:
    """True when an exception (or the text of a failed step) looks like a temporary BigQuery problem worth retrying."""
    if isinstance(problem, BaseException):
        if getattr(problem, "code", None) in _TRANSIENT_CODES:
            return True
        reasons = {e.get("reason") for e in (getattr(problem, "errors", None) or []) if isinstance(e, dict)}
        if reasons & _TRANSIENT_REASONS:
            return True
        text = str(problem)
    else:
        text = str(problem or "")
    text = text.lower()
    return any(w in text for w in _TRANSIENT_WORDS)
