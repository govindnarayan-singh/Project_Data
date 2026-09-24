class AuditError(Exception):
    """Base error for controlled governance-audit failures."""


class AuditValidationError(AuditError):
    pass


class AuditWriteError(AuditError):
    pass


class GovernanceApprovalError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
