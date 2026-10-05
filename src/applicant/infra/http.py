"""The one way this package talks HTTP.

Every client used to carry its own retry loop - four of them, each slightly
different, and LinkedIn with none - and its own idea of how long to wait
between requests. `HttpClient` is that loop, written once:

* transport errors and throttling statuses are retried with exponential
  backoff (and jitter, so parallel runs do not retry in lockstep)
* a 429 that carries `Retry-After` is honoured, up to a cap; a 429 that
  outlasts the retries becomes `Blocked`, never a response to parse
* a network that fails through every retry becomes `Unreachable`
* each host gets a minimum gap between requests, shared by every client in the
  process - so politeness does not depend on each adapter remembering a sleep

Anything else - a 404, a 500 - comes back as a response, because only the
adapter knows whether a 404 means "no such company" or "try the other url".

Tests inject an `httpx.Client` over an `httpx.MockTransport`, exactly as they
did before, and pass `backoff=0, interval=0` (the adapters map their old
`delay=0` onto both).
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable, Collection, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from .. import log
from ..errors import Blocked, Unreachable

logger = logging.getLogger(__name__)

# one identity for plain requests and the browser alike: a site comparing the
# two should see the same desktop Chrome
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36'
)
HEADERS = {'User-Agent': USER_AGENT, 'Accept-Language': 'en-US,en;q=0.9'}

# statuses that mean "not now" rather than "no": retried, then handed back
THROTTLED = (429, 502, 503, 504)
# the longest a Retry-After is allowed to hold a run up
MAX_RETRY_AFTER = 60.0

# host -> when the next request to it may start, across every client
_next_allowed: dict[str, float] = {}
_pacing = threading.Lock()


def _wait_for_turn(host: str, interval: float) -> None:
    """Hold this request until `interval` has passed since the host's last one.

    The slot is claimed under the lock and slept on outside it, so two threads
    aiming at the same host queue up rather than both firing at once.
    """
    if interval <= 0:
        return
    with _pacing:
        now = time.monotonic()
        start = max(now, _next_allowed.get(host, 0.0))
        _next_allowed[host] = start + interval
    if start > now:
        time.sleep(start - now)


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds the server asked us to wait, when it said so in a usable form."""
    value = response.headers.get('Retry-After', '').strip()
    try:
        seconds = float(value)
    except ValueError:
        return None  # an HTTP date, or nothing: fall back to our own backoff
    return seconds if 0 <= seconds <= MAX_RETRY_AFTER else None


class HttpClient:
    """Retries, backoff, per-host pacing and error mapping over `httpx.Client`.

    `name` is what the errors call the source ("could not reach AmbitionBox").
    `retry_on` narrows which statuses are retried: LinkedIn passes one without
    429, because carrying on through a rate limit there is how a working scrape
    becomes a blocked one. `retry_when` retries a response the status alone
    does not condemn - the World Bank answers a throttle with HTTP 200 and an
    error body. `secret` is registered for masking in every log line.
    """

    def __init__(
        self,
        name: str,
        *,
        client: httpx.Client | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        retries: int = 3,
        backoff: float = 1.0,
        interval: float = 1.0,
        retry_on: Collection[int] = THROTTLED,
        retry_when: Callable[[httpx.Response], bool] | None = None,
        secret: str | None = None,
    ):
        self.name = name
        self.retries = max(1, retries)
        self.backoff = backoff
        self.interval = interval
        self.retry_on = frozenset(retry_on)
        self.retry_when = retry_when
        log.register_secret(secret)
        self._owned = client is None
        self.client = client or httpx.Client(
            headers=dict(HEADERS if headers is None else headers),
            timeout=timeout,
            follow_redirects=True,
        )

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request('GET', url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request('POST', url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """One request, retried as the class docstring describes.

        Raises `Unreachable` when the network fails every attempt and `Blocked`
        when the server is still rate limiting after them. Any other status is
        returned for the caller to judge.
        """
        host = urlsplit(url).netloc
        response: httpx.Response | None = None
        failure: httpx.TransportError | None = None

        for attempt in range(self.retries):
            if attempt:
                time.sleep(self._pause(attempt, response))
            _wait_for_turn(host, self.interval)
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.TransportError as error:
                logger.debug(f'{self.name}: attempt {attempt + 1} for {host} failed: {error}')
                failure, response = error, None
                continue

            failure = None
            if response.status_code in self.retry_on:
                logger.debug(f'{self.name}: HTTP {response.status_code} from {host}; backing off')
                continue
            if self.retry_when is not None and self.retry_when(response):
                logger.debug(f'{self.name}: unusable answer from {host}; backing off')
                continue
            break

        if response is None:
            raise Unreachable(
                f'could not reach {self.name}: {failure}', source=self.name.lower()
            ) from failure
        # whether it was retried or not: a rate limit is never a page to parse
        if response.status_code == 429:
            raise Blocked(
                f'{self.name} is rate limiting these requests; slow down or retry later',
                source=self.name.lower(),
            )
        return response

    def _pause(self, attempt: int, previous: httpx.Response | None) -> float:
        """How long to wait before retry number `attempt`."""
        if previous is not None and previous.status_code == 429:
            asked = _retry_after(previous)
            if asked is not None:
                return asked
        if self.backoff <= 0:
            return 0.0
        base = self.backoff * 2**attempt
        return base + random.uniform(0, base / 2)

    def close(self) -> None:
        """Release the connection pool, unless it was handed to us."""
        if self._owned:
            self.client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
