"""Domain error types mapped to HTTP responses in main.py."""


class ReportBuilderError(Exception):
    status_code = 400
    code = "bad_request"


class NotFound(ReportBuilderError):
    status_code = 404
    code = "not_found"


class ValidationFailed(ReportBuilderError):
    status_code = 422
    code = "validation_failed"


class PermissionDenied(ReportBuilderError):
    status_code = 403
    code = "permission_denied"


class RevisionConflict(ReportBuilderError):
    """Raised on optimistic-lock mismatch (concurrent modification)."""

    status_code = 409
    code = "revision_conflict"


class BrokenSnapshot(ReportBuilderError):
    """A saved snapshot references retired / unreachable upstream entities."""

    status_code = 409
    code = "broken_snapshot"
