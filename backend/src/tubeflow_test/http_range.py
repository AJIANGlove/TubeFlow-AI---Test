from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ByteRange:
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


class RangeNotSatisfiable(ValueError):
    pass


def parse_single_range(value: str | None, size: int) -> ByteRange | None:
    if value is None:
        return None
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", value)
    if match is None:
        raise RangeNotSatisfiable
    first, last = match.groups()
    if not first and not last:
        raise RangeNotSatisfiable
    try:
        if first:
            start = int(first)
            end = int(last) if last else size - 1
            if start < 0 or end < start or start >= size:
                raise RangeNotSatisfiable
            return ByteRange(start, min(end, size - 1))
        suffix = int(last)
        if suffix <= 0 or size <= 0:
            raise RangeNotSatisfiable
        return ByteRange(max(0, size - suffix), size - 1)
    except ValueError as error:
        raise RangeNotSatisfiable from error
