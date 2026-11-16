"""Sign in with Plex (plex.tv PIN / OAuth flow) and look up the signed-in account.

Flow:
  1. create_pin()   -> plex.tv returns a pin id + code
  2. auth_url(...)  -> browser goes to app.plex.tv, the user signs in, plex.tv
                       redirects back to our forward URL
  3. check_pin(id)  -> returns the account token once the user has signed in
"""

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from .. import __version__

PLEX_TV = "https://plex.tv"
PRODUCT = "Syncalist"


@dataclass(frozen=True)
class Pin:
    id: int
    code: str


@dataclass(frozen=True)
class AccountInfo:
    id: int
    title: str
    username: str | None
    managed: bool = False  # a managed Plex Home user (no plex.tv login of their own)


class PlexAuthError(Exception):
    pass


class PlexAuth:
    def __init__(self, timeout: float = 10.0):
        self._timeout = timeout

    def _headers(self, client_id: str, token: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "X-Plex-Product": PRODUCT,
            "X-Plex-Version": __version__,
            "X-Plex-Client-Identifier": client_id,
        }
        if token:
            headers["X-Plex-Token"] = token
        return headers

    def create_pin(self, client_id: str) -> Pin:
        try:
            r = httpx.post(f"{PLEX_TV}/api/v2/pins", params={"strong": "true"},
                           headers=self._headers(client_id), timeout=self._timeout)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise PlexAuthError(f"Couldn't reach plex.tv: {e}") from e
        data = r.json()
        return Pin(id=data["id"], code=data["code"])

    def auth_url(self, client_id: str, pin: Pin, forward_url: str) -> str:
        params = {
            "clientID": client_id,
            "code": pin.code,
            "forwardUrl": forward_url,
            "context[device][product]": PRODUCT,
        }
        return "https://app.plex.tv/auth#?" + urlencode(params)

    def check_pin(self, client_id: str, pin_id: int) -> str | None:
        """Returns the account token once the user has signed in, else None."""
        try:
            r = httpx.get(f"{PLEX_TV}/api/v2/pins/{pin_id}",
                          headers=self._headers(client_id), timeout=self._timeout)
        except httpx.HTTPError as e:
            raise PlexAuthError(f"Couldn't reach plex.tv: {e}") from e
        if r.status_code == 404:
            raise PlexAuthError("The sign-in request expired. Please try again.")
        r.raise_for_status()
        return r.json().get("authToken") or None

    def account(self, client_id: str, token: str) -> AccountInfo:
        try:
            r = httpx.get(f"{PLEX_TV}/api/v2/user",
                          headers=self._headers(client_id, token), timeout=self._timeout)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise PlexAuthError(f"Couldn't look up the Plex account: {e}") from e
        data = r.json()
        return AccountInfo(id=int(data["id"]), title=data.get("title") or data.get("username") or "",
                           username=data.get("username"), managed=bool(data.get("restricted")))
