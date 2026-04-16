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


class QueueJoinRestrictedError(MatchingQueueError):
    pass


class RetryableTaskError(Exception):
    pass


class LeaderboardError(Exception):
    pass


class InvalidLeaderboardPageError(LeaderboardError):
    pass


class LeaderboardPageNotFoundError(LeaderboardError):
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


class MatchParentRecruitmentClosedError(MatchParentAlreadyAssignedError):
    pass


class MatchReportNotOpenError(MatchFlowError):
    pass


class MatchReportingClosedError(MatchFlowError):
    pass


class MatchReportApprovalInProgressError(MatchReportingClosedError):
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


class MatchSpectatingRestrictedError(MatchFlowError):
    pass


class MatchParticipantCannotSpectateError(MatchParticipantError):
    pass


class PlayerAccessRestrictionError(Exception):
    pass


class InvalidPlayerAccessRestrictionTypeError(PlayerAccessRestrictionError):
    pass


class InvalidPlayerAccessRestrictionDurationError(PlayerAccessRestrictionError):
    pass


class PlayerAccessRestrictionAlreadyExistsError(PlayerAccessRestrictionError):
    pass


class SeasonError(Exception):
    pass


class SeasonNotFoundError(SeasonError):
    pass


class SeasonAlreadyExistsError(SeasonError):
    pass


class InvalidSeasonNameError(SeasonError):
    pass


class InvalidSeasonNameRequiredError(InvalidSeasonNameError):
    pass


class SeasonNameTooLongError(InvalidSeasonNameError):
    pass


class SeasonNameLeadingDigitError(InvalidSeasonNameError):
    pass


class PlayerSeasonStatsNotFoundError(SeasonError):
    pass


class SeasonStateError(SeasonError):
    pass
