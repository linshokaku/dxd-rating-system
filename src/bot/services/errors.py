class RegistrationError(Exception):
    pass


class PlayerAlreadyRegisteredError(RegistrationError):
    pass


class MatchingQueueError(Exception):
    pass


class PlayerNotRegisteredError(MatchingQueueError):
    pass


class QueueAlreadyJoinedError(MatchingQueueError):
    pass


class QueueNotJoinedError(MatchingQueueError):
    pass


class RetryableTaskError(Exception):
    pass


class MatchError(Exception):
    pass


class MatchNotFoundError(MatchError):
    pass


class MatchParticipantError(MatchError):
    pass


class ParentAlreadyDecidedError(MatchError):
    pass


class ParentVolunteerClosedError(MatchError):
    pass


class MatchReportNotOpenError(MatchError):
    pass


class MatchReportClosedError(MatchError):
    pass


class MatchApprovalNotOpenError(MatchError):
    pass


class MatchApprovalNotRequiredError(MatchError):
    pass
