"""Run PlexApiGateway's write logic against a simulated Plex HTTP API."""

import re
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from plexapi.exceptions import BadRequest

from syncalist.plex.gateway import PlexApiGateway


class SimServer:
    """Understands the handful of playlist requests the gateway sends."""

    def __init__(self, items, no_access=(), reject_batches_with_inaccessible=False):
        self.machineIdentifier = "m1"
        self.items = list(items)  # ratingKeys in order
        self.ids = [100 + i for i in range(len(self.items))]  # playlistItemIDs
        self.next_id = 1000
        self.no_access = set(no_access)
        self.reject = reject_batches_with_inaccessible
        self._session = SimpleNamespace(put="PUT", delete="DELETE", post="POST")
        self.requests = []

    def _uriRoot(self):
        return "server://m1/com.plexapp.plugins.library"

    def fetchItems(self, path):
        assert path == "/playlists/5/items"
        return [SimpleNamespace(ratingKey=str(k), playlistItemID=i) for k, i in zip(self.items, self.ids)]

    def query(self, path, method):
        self.requests.append((method, path))
        u = urlparse(path)
        if method == "DELETE":
            item_id = int(u.path.rsplit("/", 1)[1])
            idx = self.ids.index(item_id)
            del self.items[idx], self.ids[idx]
        elif method == "PUT" and u.path.endswith("/move"):
            item_id = int(u.path.split("/")[-2])
            idx = self.ids.index(item_id)
            k, i = self.items.pop(idx), self.ids.pop(idx)
            after = parse_qs(u.query).get("after")
            pos = 0 if not after else self.ids.index(int(after[0])) + 1
            self.items.insert(pos, k)
            self.ids.insert(pos, i)
        elif method == "PUT" and u.path == "/playlists/5/items":
            uri = unquote(parse_qs(u.query)["uri"][0])
            keys = [int(k) for k in re.search(r"/library/metadata/([\d,]+)", uri).group(1).split(",")]
            if self.reject and any(k in self.no_access for k in keys):
                raise BadRequest("400")
            for k in keys:
                if k not in self.no_access and k not in self.items:
                    self.items.append(k)
                    self.ids.append(self.next_id)
                    self.next_id += 1
        else:
            raise AssertionError(f"unexpected request {method} {path}")


def run(current, desired, **kw):
    srv = SimServer(current, **kw)
    pl = SimpleNamespace(key="/playlists/5")
    skipped = PlexApiGateway._set_tracks(PlexApiGateway.__new__(PlexApiGateway), srv, pl, desired)
    return srv, skipped


@pytest.mark.parametrize("current,desired", [
    ([1, 2, 3], [3, 1, 2]),
    ([1, 2, 3], [4, 5]),
    ([1, 2, 2, 3], [3, 2, 1, 9]),
    ([], [1, 2, 3]),
    ([5, 4, 3, 2, 1], [1, 2, 3, 4, 5]),
])
def test_set_tracks_reaches_desired(current, desired):
    srv, skipped = run(current, desired)
    assert srv.items == desired and skipped == []


def test_no_requests_when_already_equal():
    srv, _ = run([1, 2, 3], [1, 2, 3])
    assert srv.requests == []


def test_inaccessible_tracks_are_skipped_in_order():
    srv, skipped = run([1], [7, 1, 8, 9], no_access={8})
    assert srv.items == [7, 1, 9] and skipped == [8]


def test_batch_rejected_because_of_one_bad_track_falls_back_to_singles():
    srv, skipped = run([], [1, 2, 3, 4], no_access={3}, reject_batches_with_inaccessible=True)
    assert srv.items == [1, 2, 4] and skipped == [3]
