"""HTTP access to flowerstore.ph, the same way the storefront's own pages call it.

Cloudflare in front of the site challenges clients whose TLS handshake is not a browser's, so
requests go through curl_cffi with a Chrome fingerprint. The JSON endpoints under /api are
Laravel routes behind CSRF protection: a first GET hands out the XSRF-TOKEN cookie, which is
echoed back in the X-XSRF-TOKEN header on every POST, as axios does on the site.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from typing import Any

from curl_cffi import requests

BASE = "https://flowerstore.ph"
TIMEOUT = 30
MIN_INTERVAL = 0.5  # seconds between requests from one process
RETRY_STATUSES = {500, 502}


class FlowerstoreError(Exception):
    """A failure the CLI reports on stderr. `code` is the process exit code."""

    code = 1

    def __init__(self, message: str, **extra: Any):
        super().__init__(message)
        self.extra = extra


class BlockedError(FlowerstoreError):
    code = 4


class NotFoundError(FlowerstoreError):
    code = 3


class Client:
    def __init__(self, base: str = BASE):
        self.base = base
        self.session = requests.Session(impersonate="chrome")
        self._xsrf: str | None = None
        self._last = 0.0
        self.retries: list[dict[str, Any]] = []

    # -- transport -------------------------------------------------------------------------

    def _wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - gap)
        self._last = time.monotonic()

    def _request(self, method: str, path: str, *, label: str, allow: tuple[int, ...] = (),
                 timeout: int = TIMEOUT, **kw: Any):
        """One request, retried once on a dropped connection or a 500/502.

        Statuses in `allow` are returned to the caller instead of raising.
        """
        errors: list[str] = []
        for attempt in (1, 2):
            self._wait()
            try:
                r = self.session.request(method, self.base + path, timeout=timeout,
                                         allow_redirects=False, **kw)
            except requests.RequestsError as e:  # dropped connection, timeout, DNS
                errors.append(f"connection failed ({type(e).__name__})")
                if attempt == 1:
                    continue
                self._note_retry(label, errors, recovered=False)
                raise FlowerstoreError(f"{label}: could not reach flowerstore.ph", errors=errors)
            if r.status_code in RETRY_STATUSES and attempt == 1:
                errors.append(f"server error ({r.status_code})")
                continue
            if errors:
                self._note_retry(label, errors, recovered=r.status_code < 400)
            if r.status_code not in allow:
                self._check(r, label)
            return r
        raise AssertionError("unreachable")

    def _note_retry(self, label: str, errors: list[str], recovered: bool) -> None:
        self.retries.append({"request": label, "tries": len(errors) + (1 if recovered else 0),
                             "errors": errors, "recovered": recovered})

    @staticmethod
    def _check(r, label: str) -> None:
        if r.status_code == 403 and r.headers.get("cf-mitigated"):
            raise BlockedError(f"{label}: Cloudflare challenged the request; try again later "
                               "or open the site in a browser")
        if r.status_code == 429:
            raise BlockedError(f"{label}: rate limited by flowerstore.ph; wait a minute",
                               retry_after=r.headers.get("retry-after"))
        if r.status_code in (301, 302) and "page-not-found" in (r.headers.get("location") or ""):
            raise NotFoundError(f"{label}: not found")
        if r.status_code in (301, 302):
            raise FlowerstoreError(f"{label}: unexpected redirect to {r.headers.get('location')}")
        if r.status_code == 404:
            raise NotFoundError(f"{label}: not found")
        if r.status_code >= 500 or r.status_code in (401, 403, 405, 419):
            raise FlowerstoreError(f"{label}: flowerstore.ph answered {r.status_code}")

    def _ensure_session(self) -> None:
        if self._xsrf:
            return
        # Any uncached storefront page starts a session. The homepage is served from a
        # guest cache without cookies, the search page is not.
        self._request("GET", "/product/search", label="session")
        token = self.session.cookies.get("XSRF-TOKEN")
        if not token:
            raise FlowerstoreError("session: flowerstore.ph did not hand out a CSRF token")
        self._xsrf = urllib.parse.unquote(token)

    def post(self, path: str, payload: dict[str, Any] | None = None, *, label: str,
             allow: tuple[int, ...] = ()):
        """POST a JSON body to /api/<path>, as the storefront's axios calls do."""
        self._ensure_session()
        headers = {
            "X-XSRF-TOKEN": self._xsrf or "",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
            "Origin": self.base,
            "Referer": self.base + "/",
        }
        return self._request("POST", "/api/" + path, label=label, allow=allow,
                             json=payload or {}, headers=headers)

    def api(self, path: str, payload: dict[str, Any] | None = None, *, label: str) -> Any:
        """POST to /api/<path> and return the decoded JSON."""
        r = self.post(path, payload, label=label)
        try:
            return r.json()
        except ValueError:
            raise FlowerstoreError(f"{label}: flowerstore.ph sent something that is not JSON")

    def page_props(self, path: str, *, label: str) -> dict[str, Any]:
        """The Inertia props a storefront page embeds as JSON in its HTML."""
        r = self._request("GET", path, label=label)
        m = re.search(r'<script data-page="app" type="application/json">(.*?)</script>', r.text, re.S)
        if not m:
            raise FlowerstoreError(f"{label}: the page no longer embeds its data the way the CLI expects")
        return json.loads(m.group(1))["props"]

    def get_bytes(self, path: str, *, label: str, timeout: int = 120) -> bytes:
        return self._request("GET", path, label=label, timeout=timeout).content
