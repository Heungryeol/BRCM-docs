"""
HTTP 수집기 — robots.txt 준수, 사이트별 요청 간격(crawl-delay), 재시도·Retry-After 처리.

자동 수집을 막는 사이트(방화벽/CDN 403)는 우회하지 않습니다. 해당 사이트는 'blocked' 로 기록하고
브라우저로 받은 파일을 `brcm-docs import` 로 넣는 방식을 안내합니다.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field

from .log import log

_SAFE = ":/?&=%#+@;,~!$'()*"


def quote_url(url: str) -> str:
    """비 ASCII URL(번역판 KB 등)을 percent-encoding"""
    return urllib.parse.quote(url, safe=_SAFE)


def host_of(url: str) -> str:
    m = re.match(r"https?://[^/]+", url)
    return m.group(0) if m else url


@dataclass
class Resp:
    url: str
    status: int
    data: bytes = b""
    headers: dict = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()

    def text(self) -> str:
        m = re.search(r"charset=([\w-]+)", self.headers.get("content-type") or "")
        try:
            return self.data.decode(m.group(1) if m else "utf-8", "replace")
        except LookupError:
            return self.data.decode("utf-8", "replace")


class Blocked(Exception):
    """robots.txt 금지 또는 사이트가 자동 수집을 차단"""


class Fetcher:
    def __init__(self, user_agent: str, delay: float = 2.0, timeout: int = 60,
                 max_bytes: int = 80 * 1024 * 1024, retries: int = 3):
        self.ua = user_agent
        self.delay = delay
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.retries = retries
        self._last: dict[str, float] = {}
        self._robots: dict[str, tuple[urllib.robotparser.RobotFileParser | None, str]] = {}
        self._lock = threading.Lock()
        self.stop = threading.Event()

    # ------------------------------------------------------------------ robots
    def robots(self, url: str) -> tuple[urllib.robotparser.RobotFileParser | None, str]:
        """(파서, 상태설명). 파서가 None 이면 그 사이트는 수집하지 않음."""
        host = host_of(url)
        if host in self._robots:
            return self._robots[host]
        rp = urllib.robotparser.RobotFileParser(host + "/robots.txt")
        r = self._raw(host + "/robots.txt")
        if r.ok:
            rp.parse(r.text().splitlines())
            note = f"robots.txt OK (crawl-delay {rp.crawl_delay('*') or '-'})"
        elif r.status in (404, 410):
            rp.parse([])
            note = "robots.txt 없음 (제한 없음)"
        else:
            rp = None
            note = (f"robots.txt 응답 {r.status or r.error} — 사이트가 자동 수집 요청을 막고 있어 수집하지 않습니다"
                    if r.status in (401, 403) else f"robots.txt 를 받을 수 없음: {r.status or r.error}")
        self._robots[host] = (rp, note)
        log(f"{host}: {note}")
        return rp, note

    def allowed(self, url: str) -> bool:
        rp, _ = self.robots(url)
        return bool(rp and rp.can_fetch(self.ua, url))

    def wait_for(self, url: str) -> float:
        rp, _ = self.robots(url)
        cd = 0.0
        if rp:
            try:
                cd = float(rp.crawl_delay(self.ua) or rp.crawl_delay("*") or 0)
            except (TypeError, ValueError):
                cd = 0.0
        return max(self.delay, cd)

    # ------------------------------------------------------------------ GET
    def _raw(self, url: str) -> Resp:
        req = urllib.request.Request(quote_url(url), headers={
            "User-Agent": self.ua, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8", "Accept-Encoding": "identity"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = r.read(self.max_bytes + 1)
                if len(data) > self.max_bytes:
                    return Resp(url, 413, b"", {}, f"응답이 {self.max_bytes // 1048576}MB 초과")
                return Resp(r.geturl(), r.status, data, {k.lower(): v for k, v in r.headers.items()})
        except urllib.error.HTTPError as e:
            return Resp(url, e.code, b"", {k.lower(): v for k, v in (e.headers or {}).items()}, str(e.reason))
        except Exception as e:  # noqa: BLE001
            return Resp(url, 0, b"", {}, f"{type(e).__name__}: {e}")

    def get(self, url: str, check_robots: bool = True) -> Resp:
        if check_robots and not self.allowed(url):
            raise Blocked(self.robots(url)[1] if self.robots(url)[0] is None else "robots.txt 에 의해 제외")
        host = host_of(url)
        wait = self.wait_for(url) if check_robots else self.delay
        r = Resp(url, 0)
        for attempt in range(self.retries):
            with self._lock:
                dt = time.time() - self._last.get(host, 0)
                if dt < wait:
                    self.stop.wait(wait - dt)
                if self.stop.is_set():
                    return Resp(url, 0, error="중지 요청")
                r = self._raw(url)
                self._last[host] = time.time()
            if r.ok or r.status in (404, 410, 401, 403, 413):
                return r
            # 429/503: Retry-After 존중, 그 외(5xx·네트워크): 점증 대기
            ra = r.headers.get("retry-after", "")
            backoff = float(ra) if ra.isdigit() else 15.0 * (attempt + 1)
            if r.status in (429, 503):
                backoff = max(backoff, 60.0)
            log(f"  {r.status or r.error} → {backoff:.0f}s 후 재시도 ({attempt + 1}/{self.retries}): {url}")
            self.stop.wait(backoff)
        return r
