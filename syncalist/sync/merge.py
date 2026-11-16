"""Three-way merge of shared playlist edits. Pure functions only.

Each member's edit is described by what they had after our last write (`prev`) and what they
have now (`cur`). Diffing each member against their own `prev`, instead of against the group's
list, means tracks a member could never see (skipped) or hasn't received yet are never mistaken
for removals.

Rules (see PLAN.md):
  * Removals win: a track any member removed is removed for everyone.
  * Additions from everyone are kept, placed after the track they followed in that member's list
    (or at the end, if they added it at the end).
  * Order: the most recent edit that reordered tracks decides the order.
  * No duplicates.
"""

from dataclasses import dataclass
from datetime import datetime

from .planner import dedupe


@dataclass(frozen=True)
class Edit:
    member: str  # label for logging
    prev: list[int]  # what this member had after our last write
    cur: list[int]  # what they have now
    at: datetime | None = None  # when they changed it (Plex updatedAt)

    @property
    def added(self) -> list[int]:
        before = set(self.prev)
        return [k for k in dedupe(self.cur) if k not in before]

    @property
    def removed(self) -> list[int]:
        now = set(self.cur)
        return [k for k in dedupe(self.prev) if k not in now]

    @property
    def reordered(self) -> bool:
        common_prev = [k for k in dedupe(self.prev) if k in set(self.cur)]
        common_cur = [k for k in dedupe(self.cur) if k in set(self.prev)]
        return common_prev != common_cur


@dataclass
class MergeResult:
    tracks: list[int]
    added: dict[str, list[int]]  # member -> tracks they added that made it in
    removed: dict[str, list[int]]  # member -> tracks they removed
    order_from: str | None  # member whose order was used


def merge(base: list[int], edits: list[Edit]) -> MergeResult:
    edits = sorted(edits, key=lambda e: (e.at is not None, e.at), reverse=True)  # newest first
    removed_by = {e.member: [k for k in e.removed if k in set(base)] for e in edits}
    removed = {k for ks in removed_by.values() for k in ks}

    result = [k for k in dedupe(base) if k not in removed]

    order_src = next((e for e in edits if e.reordered), None)
    if order_src is not None:
        result = _follow_order(result, dedupe(order_src.cur))

    added_by: dict[str, list[int]] = {}
    for e in edits:
        got = []
        for k in e.added:
            if k in removed or k in result:
                continue
            _insert_after_predecessor(result, k, dedupe(e.cur))
            got.append(k)
        added_by[e.member] = got

    return MergeResult(tracks=result, added=added_by, removed=removed_by,
                       order_from=order_src.member if order_src else None)


def _follow_order(items: list[int], order: list[int]) -> list[int]:
    """Reorder `items` so those in `order` follow its sequence. Items not in `order`
    stay directly after the item they followed before."""
    present = set(items)
    ordered = [k for k in order if k in present]
    in_order = set(ordered)
    out = list(ordered)
    for i, k in enumerate(items):
        if k in in_order:
            continue
        prev = next((items[j] for j in range(i - 1, -1, -1) if items[j] in set(out)), None)
        out.insert(out.index(prev) + 1 if prev is not None else 0, k)
    return out


def _insert_after_predecessor(result: list[int], k: int, source: list[int]) -> None:
    idx = source.index(k)
    have = set(result)
    if not any(x in have for x in source[idx + 1:]):
        result.append(k)  # added at the end of their list: keep it at the end
        return
    for j in range(idx - 1, -1, -1):
        if source[j] in have:
            result.insert(result.index(source[j]) + 1, k)
            return
    if idx == 0:
        result.insert(0, k)  # it was first in their list
    else:
        result.append(k)  # whatever it followed is gone
