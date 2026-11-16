"""Work out the fewest Plex operations that turn one track list into another.

Pure functions only: no Plex, no database. Track lists are lists of ratingKeys.
Shared playlists never hold duplicates, so `desired` is de-duplicated and any
duplicates in `current` are removed (the first occurrence is kept).
"""

from bisect import bisect_left
from dataclasses import dataclass, field


@dataclass
class Plan:
    removes: list[int] = field(default_factory=list)  # keys to remove (one entry per occurrence)
    adds: list[int] = field(default_factory=list)  # keys to append, in desired order
    moves: list[tuple[int, int | None]] = field(default_factory=list)  # (key, after_key or None = top)

    @property
    def is_empty(self) -> bool:
        return not (self.removes or self.adds or self.moves)


def dedupe(keys: list[int]) -> list[int]:
    seen: set[int] = set()
    return [k for k in keys if not (k in seen or seen.add(k))]


def union(*lists: list[int]) -> list[int]:
    """All tracks from every list, no duplicates. Order: the first list, then each
    later list's new tracks in their own order."""
    return dedupe([k for lst in lists for k in lst])


def plan(current: list[int], desired: list[int]) -> Plan:
    desired = dedupe(desired)
    wanted = set(desired)
    kept: list[int] = []
    removes: list[int] = []
    seen: set[int] = set()
    for k in current:
        if k in wanted and k not in seen:
            kept.append(k)
            seen.add(k)
        else:
            removes.append(k)
    adds = [k for k in desired if k not in seen]
    return Plan(removes=removes, adds=adds, moves=moves(kept + adds, desired))


def moves(current: list[int], desired: list[int]) -> list[tuple[int, int | None]]:
    """Moves that reorder `current` into `desired` (same unique keys).

    Tracks on the longest increasing subsequence already sit in the right
    relative order and stay put. Every other track is moved directly after its
    predecessor in `desired`, working left to right, which is the minimum
    number of single-item moves.
    """
    pos = {k: i for i, k in enumerate(current)}
    seq = [pos[k] for k in desired]
    keep = _lis_indices(seq)
    return [(k, desired[i - 1] if i else None) for i, k in enumerate(desired) if i not in keep]


def apply(current: list[int], p: Plan) -> list[int]:
    """Simulate a plan the way Plex applies it. Used by tests and the fake gateway."""
    out = list(current)
    for k in p.removes:
        # Remove the last occurrence so the first (kept) one survives.
        idx = len(out) - 1 - out[::-1].index(k)
        del out[idx]
    out.extend(p.adds)
    for k, after in p.moves:
        out.remove(k)
        out.insert(0 if after is None else out.index(after) + 1, k)
    return out


def _lis_indices(seq: list[int]) -> set[int]:
    """Indices of one longest strictly increasing subsequence (O(n log n))."""
    tails: list[int] = []  # tails[j] = smallest tail value of an increasing run of length j+1
    tail_idx: list[int] = []
    prev = [-1] * len(seq)
    for i, v in enumerate(seq):
        j = bisect_left(tails, v)
        if j == len(tails):
            tails.append(v)
            tail_idx.append(i)
        else:
            tails[j] = v
            tail_idx[j] = i
        prev[i] = tail_idx[j - 1] if j else -1
    out: set[int] = set()
    i = tail_idx[-1] if tail_idx else -1
    while i != -1:
        out.add(i)
        i = prev[i]
    return out
