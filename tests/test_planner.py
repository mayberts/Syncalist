import random

import pytest

from syncalist.sync.planner import apply, dedupe, plan, union


@pytest.mark.parametrize("current,desired", [
    ([], [1, 2, 3]),
    ([1, 2, 3], []),
    ([1, 2, 3], [1, 2, 3]),
    ([1, 2, 3], [3, 2, 1]),
    ([1, 2, 3, 4], [4, 1, 2, 3]),
    ([1, 2, 2, 3, 1], [1, 2, 3]),  # duplicates in current are removed
    ([1, 2, 3], [3, 3, 1]),  # duplicates in desired are ignored
    ([5, 6], [1, 2]),
])
def test_plan_reaches_desired(current, desired):
    assert apply(current, plan(current, desired)) == dedupe(desired)


def test_no_ops_when_equal():
    assert plan([1, 2, 3], [1, 2, 3]).is_empty


def test_single_move_to_top():
    p = plan([1, 2, 3, 4], [4, 1, 2, 3])
    assert p.moves == [(4, None)] and not p.adds and not p.removes


def test_reverse_uses_n_minus_1_moves():
    n = 50
    p = plan(list(range(n)), list(range(n))[::-1])
    assert len(p.moves) == n - 1


def test_append_only_needs_no_moves():
    p = plan([1, 2], [1, 2, 3, 4])
    assert p.adds == [3, 4] and p.moves == []


def test_random_plans_always_reach_desired():
    rng = random.Random(1234)
    for _ in range(2000):
        universe = list(range(rng.randint(0, 40)))
        current = [rng.choice(universe) for _ in range(rng.randint(0, 30))] if universe else []
        desired = rng.sample(universe, rng.randint(0, len(universe)))
        p = plan(current, desired)
        assert apply(current, p) == desired
        # Never move something that is already in a valid position chain longer than needed:
        assert len(p.moves) <= len(desired)


def test_large_playlist_is_fast():
    rng = random.Random(7)
    current = list(range(5000))
    desired = current[:]
    rng.shuffle(desired)
    assert apply(current, plan(current, desired)) == desired


def test_union_keeps_first_order_then_new_tracks():
    assert union([1, 2, 3], [3, 4, 1, 5], [6, 2]) == [1, 2, 3, 4, 5, 6]
