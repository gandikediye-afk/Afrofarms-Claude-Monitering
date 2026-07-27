"""Small, dependency-free Anthropic Compliance API client."""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator, Mapping


class ComplianceError(RuntimeError):
    def __init__(self, status: int, message: str, request_id: str | None = None):
        super().__init__(f"Compliance API returned HTTP {status}: {message}")
        self.status, self.request_id = status, request_id


@dataclass(frozen=True)
class ComplianceCheck:
    ok: bool
    message: str


class _OriginBoundRedirect(urllib.request.HTTPRedirectHandler):
    """Allow redirects only when the API-key header stays on its configured origin."""

    def __init__(self, origin: str):
        self.origin = origin

    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> urllib.request.Request | None:
        redirected = urllib.parse.urlsplit(newurl)
        target_origin = f"{redirected.scheme}://{redirected.netloc}"
        if target_origin != self.origin:
            raise urllib.error.HTTPError(req.full_url, code, "cross-origin redirect refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class Page:
    data: list[dict[str, Any]]
    has_more: bool
    first_id: str | None
    last_id: str | None
    request_id: str | None


class AnthropicClient:
    """Read-only client; deliberately exposes no delete methods."""

    def __init__(self, access_key: str, base_url: str = "https://api.anthropic.com", *, max_retries: int = 5, timeout: float = 30):
        self._key = access_key
        origin = urllib.parse.urlsplit(base_url)
        if origin.scheme != "https" or not origin.netloc or origin.username or origin.password or origin.query or origin.fragment:
            raise ValueError("the configured Anthropic API origin must be an HTTPS origin")
        if origin.path not in ("", "/"):
            raise ValueError("the configured Anthropic API origin must not contain a path")
        self.origin = f"{origin.scheme}://{origin.netloc}"
        self.base_url = self.origin + "/v1/compliance"
        self._opener = urllib.request.build_opener(_OriginBoundRedirect(self.origin))
        self.max_retries, self.timeout = max_retries, timeout
        self.last_request_id: str | None = None

    def _safe(self, value: object) -> str:
        return str(value).replace(self._key, "[REDACTED]")

    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], str | None]:
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = self.base_url + path + ("?" + query if query else "")
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(url, headers={"x-api-key": self._key, "accept": "application/json"})
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    request_id = response.headers.get("request-id") or response.headers.get("x-request-id")
                    self.last_request_id = request_id
                    if response.status != 200:
                        raise ComplianceError(response.status, "unexpected success status", request_id)
                    return json.load(response), request_id
            except urllib.error.HTTPError as exc:
                request_id = exc.headers.get("request-id") or exc.headers.get("x-request-id")
                self.last_request_id = request_id
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if retryable and attempt < self.max_retries:
                    retry_after = exc.headers.get("retry-after")
                    delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else min(30.0, 0.5 * 2**attempt)
                    time.sleep(delay + random.uniform(0, min(1.0, delay / 4)))
                    continue
                body = exc.read(2048).decode("utf-8", "replace")
                raise ComplianceError(exc.code, self._safe(body), request_id) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < self.max_retries:
                    delay = min(30.0, 0.5 * 2**attempt)
                    time.sleep(delay + random.uniform(0, min(1.0, delay / 4)))
                    continue
                raise ComplianceError(0, self._safe(exc), self.last_request_id) from exc
        raise AssertionError("retry loop exhausted")

    def page(self, path: str, params: Mapping[str, Any] | None = None) -> Page:
        body, request_id = self._get(path, params)
        return Page(list(body.get("data", [])), bool(body.get("has_more")), body.get("first_id"), body.get("last_id"), request_id)

    def pages(self, path: str, params: Mapping[str, Any] | None = None, *, cursor_parameter: str = "after_id") -> Iterator[Page]:
        current = dict(params or {})
        while True:
            page = self.page(path, current)
            yield page
            if not page.has_more:
                return
            cursor = page.last_id if cursor_parameter == "after_id" else page.first_id
            if not cursor:
                raise ComplianceError(200, "pagination response has_more=true but contains no cursor", page.request_id)
            current[cursor_parameter] = cursor

    def chats(self, **params: Any) -> Page: return self.page("/apps/chats", params)

    def compliance_check(self) -> ComplianceCheck:
        """Verify transcript read access with the smallest possible chat request."""
        try:
            self.chats(limit=1)
        except ComplianceError as exc:
            if exc.status == 403:
                return ComplianceCheck(False, "HTTP 403: the key is generally the wrong key type or is missing the required read scope")
            if exc.status in (0, 404, 405, 501):
                return ComplianceCheck(False, "the Compliance API endpoint is unavailable; Compliance API access is generally not enabled")
            return ComplianceCheck(False, f"Compliance API check failed with HTTP {exc.status}")
        except (ValueError, json.JSONDecodeError):
            return ComplianceCheck(False, "Compliance API returned an invalid response")
        return ComplianceCheck(True, "Compliance API transcript access is available (HTTP 200)")
    def chat(self, chat_id: str) -> dict[str, Any]: return self._get(f"/apps/chats/{urllib.parse.quote(chat_id, safe='')}")[0]
    def messages(self, chat_id: str, **params: Any) -> Page: return self.page(f"/apps/chats/{urllib.parse.quote(chat_id, safe='')}/messages", params)
    def activities(self, **params: Any) -> Page: return self.page("/activities", params)
    def activity(self, activity_id: str) -> dict[str, Any]: return self._get(f"/activities/{urllib.parse.quote(activity_id, safe='')}")[0]
    def users(self, **params: Any) -> Page: return self.page("/users", params)
    def user(self, user_id: str) -> dict[str, Any]: return self._get(f"/users/{urllib.parse.quote(user_id, safe='')}")[0]
    def projects(self, **params: Any) -> Page: return self.page("/apps/projects", params)
    def project(self, project_id: str) -> dict[str, Any]: return self._get(f"/apps/projects/{urllib.parse.quote(project_id, safe='')}")[0]
