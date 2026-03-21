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


class InvalidQueueNameError(MatchingQueueError):
    pass


class InvalidMatchFormatError(MatchingQueueError):
    pass


class QueueJoinNotAllowedError(MatchingQueueError):
    pass


class RetryableTaskError(Exception):
    pass


class MatchFlowError(Exception):
    pass


class MatchNotFoundError(MatchFlowError):
    pass


class MatchNotFinalizedError(MatchFlowError):
    pass


class MatchParticipantError(MatchFlowError):
    pass


class MatchParentAlreadyAssignedError(MatchFlowError):
    pass


class MatchReportNotOpenError(MatchFlowError):
    pass


class MatchReportingClosedError(MatchFlowError):
    pass


class MatchApprovalNotAvailableError(MatchFlowError):
    pass


class MatchApprovalNotRequiredError(MatchFlowError):
    pass


class MatchAlreadyFinalizedError(MatchFlowError):
    pass


class MatchSpectatingClosedError(MatchFlowError):
    pass


class MatchSpectatorAlreadyRegisteredError(MatchFlowError):
    pass


class MatchSpectatorCapacityError(MatchFlowError):
    pass
