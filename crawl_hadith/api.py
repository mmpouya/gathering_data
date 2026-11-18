"""HTTP client for the Inoor hadith service."""

from __future__ import annotations

import json
import logging
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SITE_URL = "https://hadith.inoor.ir"
SERVICE_URL = f"{SITE_URL}/service"

DEFAULT_RATE_LIMIT_COOLDOWN = 30.0
MAX_RATE_LIMIT_COOLDOWN = 600.0

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


class RateLimited(ApiError):
    """The service rate-limited this IP; retrying it immediately is pointless."""

    def __init__(
        self, message: str, retry_after: float = DEFAULT_RATE_LIMIT_COOLDOWN
    ) -> None:
        super().__init__(message)
        self.retry_after = max(0.0, retry_after)


class MaxLimit(Exception):
    """The service's query quota is exhausted; every request fails until it resets.

    Not an ``ApiError``: stage code catches ``ApiError`` and degrades to the
    fallback endpoint, which would hide a blocked run as "fallback" data.
    """


def normalize_proxy(value: str) -> str:
    """Accept ``host:port`` or an HTTP proxy URL; reject SOCKS (stdlib cannot)."""
    value = value.strip()
    if "://" not in value:
        value = f"http://{value}"
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in {"http", "https"}:
        raise ValueError(
            f"unsupported proxy scheme {parts.scheme!r}; "
            "use the HTTP inbound port (NekoRay defaults to 2081)"
        )
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"invalid proxy URL: {value!r}") from exc
    if not parts.hostname or port is None:
        raise ValueError(f"invalid proxy URL: {value!r} (expected host:port)")
    return value


def normalize_proxies(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        url = normalize_proxy(value)
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def load_proxies(path: str | Path) -> list[str]:
    """Read one proxy per line; blank lines and ``#`` comments are ignored."""
    lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    values = [
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return normalize_proxies(values)


def _proxy_label(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.hostname}:{parts.port}"


@dataclass
class _Proxy:
    url: str
    label: str
    opener: urllib.request.OpenerDirector = field(init=False, repr=False)
    next_ok: float = 0.0
    cooldown_until: float = 0.0
    failures: int = 0
    inflight: int = 0

    def __post_init__(self) -> None:
        handler = urllib.request.ProxyHandler({"http": self.url, "https": self.url})
        self.opener = urllib.request.build_opener(handler)


class ProxyPool:
    """Rotate HTTP proxies with per-proxy pacing and rate-limit cooldowns."""

    def __init__(
        self, proxies: list[str], *, delay: float = 0.0, max_inflight: int = 1
    ) -> None:
        if not proxies:
            raise ValueError("proxy pool needs at least one proxy")
        self.delay = max(0.0, delay)
        self.max_inflight = max(1, max_inflight)
        self._proxies = [_Proxy(url, _proxy_label(url)) for url in proxies]
        self._next = 0
        self._last_wait_log = 0.0
        self._cv = threading.Condition()

    def __len__(self) -> int:
        return len(self._proxies)

    def acquire(self) -> _Proxy:
        with self._cv:
            while True:
                now = time.monotonic()
                count = len(self._proxies)
                wait: float | None = None
                for step in range(count):
                    proxy = self._proxies[(self._next + step) % count]
                    if proxy.inflight >= self.max_inflight:
                        continue
                    ready_at = max(proxy.next_ok, proxy.cooldown_until)
                    if ready_at <= now:
                        proxy.inflight += 1
                        proxy.next_ok = now + self.delay
                        self._next = (self._next + step + 1) % count
                        return proxy
                    remaining = ready_at - now
                    if wait is None or remaining < wait:
                        wait = remaining
                timeout = 0.5 if wait is None else max(0.05, min(wait, 5.0))
                if (
                    wait is not None
                    and wait >= 5.0
                    and now - self._last_wait_log >= 30.0
                ):
                    logger.info("all proxies busy or cooling, waiting %.1fs", wait)
                    self._last_wait_log = now
                self._cv.wait(timeout)

    def release(self, proxy: _Proxy, *, ok: bool) -> None:
        with self._cv:
            proxy.inflight = max(0, proxy.inflight - 1)
            if ok:
                proxy.failures = 0
                proxy.cooldown_until = 0.0
            self._cv.notify_all()

    def cool(self, proxy: _Proxy, retry_after: float | None = None) -> float:
        with self._cv:
            proxy.failures += 1
            base = min(
                MAX_RATE_LIMIT_COOLDOWN,
                DEFAULT_RATE_LIMIT_COOLDOWN * (2 ** min(proxy.failures - 1, 4)),
            )
            seconds = max(base, retry_after or 0.0)
            proxy.cooldown_until = max(
                proxy.cooldown_until, time.monotonic() + seconds
            )
            self._cv.notify_all()
            return seconds


class Client:
    def __init__(
        self,
        *,
        service_url: str = SERVICE_URL,
        timeout: float = 60.0,
        retries: int = 5,
        delay: float = 0.0,
        backoff: float = 1.0,
        proxies: list[str] | None = None,
    ) -> None:
        self.service_url = service_url.rstrip("/")
        self.timeout = max(1.0, timeout)
        self.retries = max(1, retries)
        self.delay = max(0.0, delay)
        self.backoff = max(0.0, backoff)
        self.proxy_pool = (
            ProxyPool(normalize_proxies(proxies), delay=self.delay) if proxies else None
        )
        if self.proxy_pool is not None:
            logger.info(
                "proxy pool: %s proxy(ies), delay %.1fs each",
                len(self.proxy_pool),
                self.delay,
            )

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
            proxy = self.proxy_pool.acquire() if self.proxy_pool else None
            success = False
            try:
                if proxy is None and self.delay:
                    time.sleep(self.delay)
                request = urllib.request.Request(
                    url, data=data, headers=headers, method=method
                )
                opener = proxy.opener if proxy is not None else None
                if opener is None:
                    response_cm = urllib.request.urlopen(request, timeout=self.timeout)
                else:
                    response_cm = opener.open(request, timeout=self.timeout)
                with response_cm as response:
                    payload = response.read()
                if raw:
                    success = True
                    return payload
                decoded = json.loads(payload.decode("utf-8-sig"))
                if isinstance(decoded, dict):
                    retry_after = self._app_rate_limit(decoded)
                    if retry_after is not None:
                        raise RateLimited(
                            "application rate limit: "
                            f"{decoded.get('message') or decoded.get('exception')}",
                            retry_after,
                        )
                    if self._app_error(decoded):
                        raise ApiError(
                            "application error: "
                            f"{decoded.get('message') or decoded.get('exception')}"
                        )
                success = True
                return decoded
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise NotFound(f"HTTP 404: {url}") from exc
                if exc.code == 430:
                    # The quota is global (every exit IP failed at once), so
                    # retrying or cooling a proxy only wastes requests.
                    raise MaxLimit(f"HTTP 430 (quota exhausted): {url}") from exc
                if exc.code == 429:
                    limited = RateLimited(f"HTTP 429: {url}", self._retry_after(exc))
                    if proxy is not None:
                        seconds = self.proxy_pool.cool(proxy, limited.retry_after)
                        logger.warning(
                            "proxy %s rate-limited (HTTP 429), cooling %.0fs",
                            proxy.label,
                            seconds,
                        )
                        if attempt < self.retries:
                            continue
                    if attempt == self.retries:
                        raise limited from exc
                    last_error = limited
                elif exc.code == 403 and proxy is not None:
                    seconds = self.proxy_pool.cool(proxy)
                    logger.warning(
                        "proxy %s got HTTP 403, cooling %.0fs",
                        proxy.label,
                        seconds,
                    )
                    if attempt < self.retries:
                        continue
                    raise ApiError(f"HTTP {exc.code}: {url}") from exc
                else:
                    last_error = exc
                    if not self._http_retryable(exc.code) or attempt == self.retries:
                        raise ApiError(f"HTTP {exc.code}: {url}") from exc
            except RateLimited as exc:
                last_error = exc
                if proxy is not None:
                    seconds = self.proxy_pool.cool(proxy, exc.retry_after)
                    logger.warning(
                        "proxy %s rate-limited (%s), cooling %.0fs",
                        proxy.label,
                        exc,
                        seconds,
                    )
                    if attempt < self.retries:
                        continue
                if attempt == self.retries:
                    raise
            except ApiError as exc:
                last_error = exc
                if attempt == self.retries:
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_error = exc
                if proxy is not None:
                    self.proxy_pool.cool(proxy, 5.0)
                if attempt == self.retries:
                    raise ApiError(f"network error: {exc}") from exc
            except json.JSONDecodeError as exc:
                last_error = exc
                if proxy is not None:
                    self.proxy_pool.cool(proxy, 5.0)
                if attempt == self.retries:
                    raise ApiError(f"invalid JSON from {url}") from exc
            finally:
                if proxy is not None:
                    self.proxy_pool.release(proxy, ok=success)

            wait = self.backoff * (2 ** (attempt - 1)) + random.uniform(0.0, 0.25)
            where = f" via {proxy.label}" if proxy is not None else ""
            logger.warning(
                "request failed (%s)%s, retry %s/%s in %.1fs: %s",
                last_error,
                where,
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

    @staticmethod
    def _retry_after(exc: urllib.error.HTTPError) -> float:
        value = exc.headers.get("Retry-After") if exc.headers else None
        if value:
            try:
                return max(0.0, min(float(value), 3600.0))
            except (TypeError, ValueError):
                pass
        return DEFAULT_RATE_LIMIT_COOLDOWN

    @staticmethod
    def _app_rate_limit(payload: dict[str, Any]) -> float | None:
        status = str(payload.get("statusCode") or "")
        message = str(payload.get("message") or payload.get("exception") or "")
        if (
            status == "429"
            or "429" in message
            or "too many requests" in message.lower()
        ):
            return DEFAULT_RATE_LIMIT_COOLDOWN
        return None

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
        if self._app_max_limit(payload):
            raise MaxLimit(
                f"{path}: {payload.get('message') or payload.get('exception')}"
            )
        if self._app_error(payload):
            raise ApiError(
                f"{path}: {payload.get('message') or payload.get('exception')}"
            )
        return payload

    @staticmethod
    def _app_max_limit(payload: dict[str, Any]) -> bool:
        message = str(payload.get("message") or payload.get("exception") or "")
        return str(payload.get("statusCode") or "") == "430" or (
            "max limit" in message.lower()
        )

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

    def source_hadith_page(
        self, source_id: int, page: int, page_size: int = 50
    ) -> dict[str, Any]:
        """One page of a book, with group links (``isGroup``) but no ``tocInfo``."""
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
                "isGroup": True,
            },
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def source_hadith_ids(self, source_id: int, page: int, page_size: int = 50) -> list[int]:
        data = self.source_hadith_page(source_id, page, page_size)
        result = data.get("resultList") or []
        ids: list[int] = []
        for item in result:
            value = item.get("hadithId") or item.get("id")
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                continue
        return ids
