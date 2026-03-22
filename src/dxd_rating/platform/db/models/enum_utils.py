from enum import StrEnum


def enum_values(enum_type: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_type]
