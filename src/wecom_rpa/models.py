from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TargetStatus(StrEnum):
    PENDING = "pending"
    SELECTED = "selected"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class TargetGroup:
    group_name: str


@dataclass(frozen=True)
class Batch:
    batch_no: int
    targets: list[TargetGroup]
