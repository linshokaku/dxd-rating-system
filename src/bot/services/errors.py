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
