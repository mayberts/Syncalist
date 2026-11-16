from datetime import datetime, timedelta

from syncalist.sync.merge import Edit, merge

T0 = datetime(2026, 11, 16, 12, 0)


def at(s):
    return T0 + timedelta(seconds=s)


BASE = [1, 2, 3, 4, 5]


def test_no_edits_keeps_base():
    assert merge(BASE, []).tracks == BASE


def test_single_add_goes_after_its_predecessor():
    r = merge(BASE, [Edit("kate", BASE, [1, 2, 9, 3, 4, 5], at(1))])
    assert r.tracks == [1, 2, 9, 3, 4, 5] and r.added == {"kate": [9]}


def test_add_at_start_and_end():
    assert merge(BASE, [Edit("k", BASE, [9] + BASE, at(1))]).tracks == [9, 1, 2, 3, 4, 5]
    assert merge(BASE, [Edit("k", BASE, BASE + [9], at(1))]).tracks == BASE + [9]


def test_concurrent_adds_from_two_people_both_kept():
    r = merge(BASE, [Edit("kate", BASE, [1, 7, 2, 3, 4, 5], at(1)),
                     Edit("nick", BASE, [1, 2, 3, 4, 5, 8], at(2))])
    assert r.tracks == [1, 7, 2, 3, 4, 5, 8]


def test_same_track_added_by_two_people_appears_once():
    r = merge(BASE, [Edit("kate", BASE, BASE + [9], at(1)), Edit("nick", BASE, [9] + BASE, at(2))])
    assert r.tracks.count(9) == 1


def test_removal_wins_over_unchanged_and_over_move():
    r = merge(BASE, [Edit("kate", BASE, [1, 2, 4, 5], at(1)),  # removed 3
                     Edit("nick", BASE, [3, 1, 2, 4, 5], at(2))])  # moved 3 to top
    assert 3 not in r.tracks and r.removed["kate"] == [3]


def test_newest_reorder_wins():
    r = merge(BASE, [Edit("kate", BASE, [5, 4, 3, 2, 1], at(1)), Edit("nick", BASE, [2, 1, 3, 4, 5], at(2))])
    assert r.tracks == [2, 1, 3, 4, 5] and r.order_from == "nick"


def test_reorder_plus_someone_elses_add():
    r = merge(BASE, [Edit("kate", BASE, [5, 4, 3, 2, 1], at(2)), Edit("nick", BASE, [1, 2, 9, 3, 4, 5], at(1))])
    assert r.tracks == [5, 4, 3, 2, 9, 1]  # 9 follows 2 in Nick's list


def test_skipped_tracks_are_not_removals():
    # Cleo can't see track 3, so it was never in her playlist (prev excludes it).
    cleo_prev = [1, 2, 4, 5]
    r = merge(BASE, [Edit("cleo", cleo_prev, cleo_prev + [9], at(1))])
    assert r.tracks == [1, 2, 3, 4, 5, 9]


def test_member_who_has_not_received_latest_adds_does_not_remove_them():
    base = BASE + [9]  # 9 was added by someone else last round; Cleo's copy wasn't updated yet
    r = merge(base, [Edit("cleo", BASE, BASE + [8], at(1))])
    assert 9 in r.tracks and 8 in r.tracks


def test_add_of_a_track_someone_removed_is_kept_only_if_new():
    # Kate removes 2; Nick adds 2 again elsewhere? It was never gone for Nick, so not an add. Removal wins.
    r = merge(BASE, [Edit("kate", BASE, [1, 3, 4, 5], at(1)), Edit("nick", BASE, [2, 1, 3, 4, 5], at(2))])
    assert 2 not in r.tracks


def test_duplicates_in_current_are_ignored():
    r = merge(BASE, [Edit("k", BASE, [1, 1, 2, 3, 4, 5, 9, 9], at(1))])
    assert r.tracks == [1, 2, 3, 4, 5, 9]


def test_everyone_removes_everything():
    assert merge(BASE, [Edit("k", BASE, [], at(1))]).tracks == []


def test_add_at_end_stays_at_end_after_someone_reorders():
    r = merge(BASE, [Edit("kate", BASE, BASE + [8], at(1)), Edit("nick", BASE, [5, 1, 2, 3, 4], at(2))])
    assert r.tracks == [5, 1, 2, 3, 4, 8]
