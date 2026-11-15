"""A small Gitea client over urllib. It holds a token, so it never logs a header or a URL's query."""

from __future__ import annotations

import email.utils
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

PAGE = 50


class GiteaError(Exception):
    """`status` is None when the request never got an answer. Both mean: try again later."""

    def __init__(self, status: int | None, message: str):
        super().__init__(f"Gitea {status}: {message}" if status else f"Gitea unreachable: {message}")
        self.status = status
        self.message = message


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib would carry the Authorization header to wherever a redirect points.
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


@dataclass
class Response:
    status: int
    body: Any
    server_time: float | None


def parse_time(text: str | None) -> float:
    if not text:
        return 0.0
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def format_time(when: float) -> str:
    return datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class GiteaClient:
    """`base_url` is the instance root, like https://git.example.com. The token file is read at each
    request, so replacing it is a rotation and needs no restart."""

    def __init__(self, base_url: str, token_file: str, timeout: float = 30.0):
        self.base = base_url.rstrip("/") + "/api/v1"
        self.token_file = token_file
        self.timeout = timeout
        self.server_time: float | None = None
        self._opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))

    def _token(self) -> str:
        try:
            with open(self.token_file, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as exc:
            raise GiteaError(None, f"can't read the token file ({exc.strerror})") from None

    def request(self, method: str, path: str, *, query: dict | None = None, body: Any = None, raw: bool = False) -> Response:
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode()
        headers = {"Authorization": "token " + self._token(), "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                payload = response.read()
                when = self._note_time(response.headers.get("Date"))
                if raw:
                    return Response(response.status, payload.decode("utf-8", "replace"), when)
                return Response(response.status, json.loads(payload) if payload.strip() else None, when)
        except urllib.error.HTTPError as exc:
            self._note_time(exc.headers.get("Date") if exc.headers else None)
            raise GiteaError(exc.code, _message(exc)) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise GiteaError(None, type(exc).__name__) from None
        except ValueError as exc:
            raise GiteaError(None, f"not JSON: {exc}") from None

    def _note_time(self, date: str | None) -> float | None:
        if date:
            try:
                self.server_time = email.utils.parsedate_to_datetime(date).timestamp()
            except (TypeError, ValueError):
                pass
        return self.server_time

    def get(self, path: str, **query: Any) -> Any:
        return self.request("GET", path, query=query or None).body

    def get_or_none(self, path: str, **query: Any) -> Any:
        try:
            return self.get(path, **query)
        except GiteaError as exc:
            if exc.status == 404:
                return None
            raise

    def text(self, path: str) -> str:
        return self.request("GET", path, raw=True).body

    def post(self, path: str, body: Any = None, **query: Any) -> Any:
        return self.request("POST", path, query=query or None, body=body if body is not None else {}).body

    def pages(self, path: str, **query: Any) -> Iterator[dict]:
        """Every item of a paged list. A short page is the last."""
        page = 1
        while True:
            items = self.get(path, **{**query, "limit": PAGE, "page": page}) or []
            yield from items
            if len(items) < PAGE:
                return
            page += 1

    def login(self) -> str:
        return self.get("/user")["login"]


def _message(exc: urllib.error.HTTPError) -> str:
    try:
        return str(json.loads(exc.read()).get("message") or exc.reason)[:300]
    except (ValueError, OSError, AttributeError):
        return str(exc.reason)[:300]


def repo_path(repo: str) -> str:
    return "/repos/" + urllib.parse.quote(repo, safe="/")


def branch_path(branch: str) -> str:
    return urllib.parse.quote(branch, safe="")
