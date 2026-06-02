from __future__ import annotations

import csv
from pathlib import Path

from .models import Batch, TargetGroup


def load_groups_csv(path: str | Path) -> list[TargetGroup]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "group_name" not in reader.fieldnames:
            raise ValueError("群列表 CSV 必须包含 group_name 表头")
        seen: set[str] = set()
        groups: list[TargetGroup] = []
        for row_no, row in enumerate(reader, start=2):
            name = (row.get("group_name") or "").strip()
            if not name:
                continue
            if name in seen:
                continue
            seen.add(name)
            groups.append(TargetGroup(name))
    if not groups:
        raise ValueError("群列表为空")
    return groups


def limit_groups(groups: list[TargetGroup], max_total_send: int) -> list[TargetGroup]:
    if max_total_send <= 0:
        raise ValueError("max_total_send 必须 > 0")
    return groups[:max_total_send]


def split_batches(groups: list[TargetGroup], batch_size: int) -> list[Batch]:
    if batch_size <= 0 or batch_size > 9:
        raise ValueError("batch_size 必须在 1..9 之间")
    return [Batch(batch_no, groups[i : i + batch_size]) for batch_no, i in enumerate(range(0, len(groups), batch_size), start=1)]
