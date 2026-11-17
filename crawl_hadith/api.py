"""HTTP client for the Inoor hadith service."""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

SITE_URL = "https://hadith.inoor.ir"
SERVICE_URL = f"{SITE_URL}/service"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Origin": SITE_URL,
    "Referer": f"{SITE_URL}/fa/hadith/",
}


class ApiError(Exception):
    """A request failed after all retries."""


class NotFound(ApiError):
    """The endpoint or resource does not exist."""


class Client:
    def __init__(
        self,
        *,
        service_url: str = SERVICE_URL,
        timeout: float = 60.0,
        retries: int = 5,
        delay: float = 0.0,
        backoff: float = 1.0,
    ) -> None:
        self.service_url = service_url.rstrip("/")
        self.timeout = max(1.0, timeout)
        self.retries = max(1, retries)
        self.delay = max(0.0, delay)
        self.backoff = max(0.0, backoff)

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    def _fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        body: Any = None,
        params: dict[str, Any] | None = None,
        raw: bool = False,
    ) -> Any:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        data = None
        headers = dict(HEADERS)
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            if self.delay:
                time.sleep(self.delay)
            try:
                request = urllib.request.Request(
                    url, data=data, headers=headers, method=method
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = response.read()
                if raw:
                    return payload
                decoded = json.loads(payload.decode("utf-8-sig"))
                if isinstance(decoded, dict) and self._app_error(decoded):
                    raise ApiError(
                        "application error: "
                        f"{decoded.get('message') or decoded.get('exception')}"
                    )
                return decoded
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise NotFound(f"HTTP 404: {url}") from exc
                last_error = exc
                if not self._http_retryable(exc.code) or attempt == self.retries:
                    raise ApiError(f"HTTP {exc.code}: {url}") from exc
            except ApiError as exc:
                last_error = exc
                if attempt == self.retries:
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_error = exc
                if attempt == self.retries:
                    raise ApiError(f"network error: {exc}") from exc
            except json.JSONDecodeError as exc:
                last_error = exc
                if attempt == self.retries:
                    raise ApiError(f"invalid JSON from {url}") from exc

            wait = self.backoff * (2 ** (attempt - 1)) + random.uniform(0.0, 0.25)
            logger.warning(
                "request failed (%s), retry %s/%s in %.1fs: %s",
                last_error,
                attempt,
                self.retries,
                wait,
                url,
            )
            time.sleep(wait)

        raise ApiError(f"request failed: {url}") from last_error

    @staticmethod
    def _http_retryable(code: int) -> bool:
        return code in {403, 408, 425, 429, 500, 502, 503, 504}

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = self._fetch(
            f"{self.service_url}{path}", method=method, body=body, params=params
        )
        if not isinstance(payload, dict):
            raise ApiError(f"unexpected response from {path}")
        if self._app_error(payload):
            raise ApiError(
                f"{path}: {payload.get('message') or payload.get('exception')}"
            )
        return payload

    @staticmethod
    def _app_error(payload: dict[str, Any]) -> bool:
        status = payload.get("statusCode") or 0
        message = str(payload.get("message") or payload.get("exception") or "")
        if payload.get("isSuccess") is False or status >= 500:
            return True
        return "403" in message or "Forbidden" in message or "timeout" in message.lower()

    def get_bytes(self, url: str) -> bytes:
        return self._fetch(url, raw=True)

    # ------------------------------------------------------------------
    # static metadata
    # ------------------------------------------------------------------

    def source_list(self) -> dict[str, Any]:
        return self._request("/api/hadith/SourceList")

    def format_list(self) -> dict[str, Any]:
        return self._request("/api/meta/FormatList")

    def language_list(self) -> dict[str, Any]:
        return self._request("/api/language/LanguageList")

    def qael_role_list(self) -> dict[str, Any]:
        return self._request("/api/hadith/QaelRoleList")

    def statistics(self) -> dict[str, Any]:
        return self._request("/api/elastic/Statistics")

    # ------------------------------------------------------------------
    # hadith data
    # ------------------------------------------------------------------

    def hadith_detail(self, hadith_id: int) -> dict[str, Any] | None:
        payload = self._request(
            "/api/elastic/ElasticHadithById",
            method="POST",
            body={
                "hadithId": [hadith_id],
                "searchPhrase": "",
                "is_short": False,
            },
        )
        data = payload.get("data") or []
        if not isinstance(data, list) or not data:
            return None
        return data[0]

    def hadith_detail_fallback(self, hadith_id: int) -> dict[str, Any] | None:
        payload = self._request(
            "/api/hadith/HadithById", params={"id": hadith_id}
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    def translations(self, hadith_id: int) -> dict[str, Any]:
        return self._request(
            "/api/hadith/TranslateList", params={"hadithId": hadith_id}
        )

    def explanations(self, hadith_id: int) -> dict[str, Any]:
        return self._request(
            "/api/hadith/ExplanationList", params={"hadithId": hadith_id}
        )

    def rejal(self, hadith_id: int) -> dict[str, Any]:
        return self._request(
            "/api/hadith/HadithRejalList/v2", params={"hadithId": hadith_id}
        )

    def rejal_fallback(self, hadith_id: int) -> dict[str, Any]:
        return self._request(
            "/api/hadith/HadithRejalList", params={"hadithId": hadith_id}
        )

    # ------------------------------------------------------------------
    # id discovery
    # ------------------------------------------------------------------

    def source_hadith_ids(self, source_id: int, page: int, page_size: int = 50) -> list[int]:
        payload = self._request(
            "/api/elastic/v2/ElasticHadithList",
            method="POST",
            body={
                "searchInText": True,
                "pageNumber": page,
                "pageSize": page_size,
                "flexibleForLetter": True,
                "flexibleForStem": True,
                "flexibleForRoot": False,
                "searchType": "and",
                "searchArea": "all",
                "searchIn": "hadith",
                "text": "",
                "bookId": source_id,
            },
        )
        data = payload.get("data") or {}
        result = data.get("resultList") or []
        ids: list[int] = []
        for item in result:
            value = item.get("hadithId") or item.get("id")
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                continue
        return ids
