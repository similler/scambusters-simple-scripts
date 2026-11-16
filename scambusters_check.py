"""Read-only Python client for ScamBusters website lookups and submission history."""

from collections.abc import Callable, Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Literal, TypedDict
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener


BASE_URL = "https://scambuster.intelligenceforgood.org"
CHECK_URL = f"{BASE_URL}/api/check"
HISTORY_URL = f"{BASE_URL}/api/submission_history"
DEFAULT_STATE_DIR = Path(__file__).resolve().parent / ".venv"
KEY_NAME = "SCAMBUSTERS_API_KEY"
KEY_PATTERN = re.compile(r"sb_[0-9a-f]{32}")
KEY_LINE = re.compile(rf"^\s*(?:export\s+)?{KEY_NAME}\s*=(.*)$")

MAX_SITES = 200
HOUR_WINDOW = 3601
MAX_RATE_WAIT = 120
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT = 15

SiteStatus = Literal[
    "unreported", "reported_no_wallets", "reported_with_wallets", "failed", "not_checked"
]
BatchErrorCode = Literal[
    "api_key_missing", "api_key_invalid", "api_key_expired",
    "rate_limited", "connection_failed", "local_error", "request_failed", "invalid_response",
]


class SiteResult(TypedDict):
    website: str
    status: SiteStatus
    error: str | None


class BatchError(TypedDict):
    code: BatchErrorCode
    message: str
    retry_after_seconds: int | None


class BatchResult(TypedDict):
    results: list[SiteResult]
    error: BatchError | None


class HistoryResult(TypedDict):
    submissions: list[dict[str, object]]
    pages: list[int]
    total_pages: int | None
    total_submissions: int | None
    next_page: int | None
    error: BatchError | None


@dataclass(frozen=True)
class _RateLimits:
    database: str
    spacing: float
    hourly_limit: int | None = None


_CHECK_LIMITS = _RateLimits(".check-rate.sqlite3", spacing=1.5, hourly_limit=500)
_HISTORY_LIMITS = _RateLimits(".history-rate.sqlite3", spacing=61)


class _RequestError(Exception):
    """The current lookup failed."""


class _BatchStop(Exception):
    def __init__(
        self, code: BatchErrorCode, message: str, retry_after_seconds: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds

    def as_result(self) -> BatchError:
        return {
            "code": self.code,
            "message": str(self),
            "retry_after_seconds": self.retry_after_seconds,
        }


class _CredentialError(_BatchStop):
    """The lookup needs a valid key."""


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _quiet(_message: str) -> None:
    pass


def _normalize_websites(websites: str | Iterable[str]) -> list[str]:
    if isinstance(websites, str):
        websites = (websites,)
    entries: list[str] = []
    for website in websites:
        if not isinstance(website, str):
            raise TypeError("Each website must be a string.")
        site = website.strip()
        if not site:
            continue
        entries.append(site)
        if len(entries) > MAX_SITES:
            break
    if not 1 <= len(entries) <= MAX_SITES:
        raise ValueError(f"Provide between 1 and {MAX_SITES} website entries.")
    return list(dict.fromkeys(entries))


def _read_key_lines(state_dir: Path) -> list[str]:
    try:
        return (state_dir / ".env").read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return []


def _load_key(state_dir: Path) -> str:
    for line in reversed(_read_key_lines(state_dir)):
        assignment = KEY_LINE.match(line)
        if assignment:
            value = assignment[1].partition("#")[0].strip()
            if len(value) > 1 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            return value
    return os.environ.get(KEY_NAME, "").strip()


def _take_request_slot(
    connection: sqlite3.Connection, limits: _RateLimits, notify: Callable[[str], None]
) -> None:
    while True:
        # Reserve each attempt before sending it, including retries and key changes.
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            now = time.time()
            connection.execute("DELETE FROM requests WHERE sent_at <= ?", (now - HOUR_WINDOW,))
            count, oldest, newest = connection.execute(
                "SELECT COUNT(*), MIN(sent_at), MAX(sent_at) FROM requests"
            ).fetchone()
            connection.execute("DELETE FROM cooldowns WHERE retry_at <= ?", (now,))
            (server_ready_at,) = connection.execute("SELECT MAX(retry_at) FROM cooldowns").fetchone()
            ready_at = max(now, server_ready_at or now)
            if newest is not None:
                ready_at = max(ready_at, newest + limits.spacing)
            hour_full = limits.hourly_limit is not None and count >= limits.hourly_limit
            if hour_full:
                ready_at = max(ready_at, oldest + HOUR_WINDOW)
            wait = ready_at - now
            if wait <= 0:
                connection.execute("INSERT INTO requests VALUES (?)", (now,))
                return

        if wait > MAX_RATE_WAIT:
            seconds = math.ceil(wait)
            raise _BatchStop(
                "rate_limited", f"Request cooldown in effect. Try again in about {seconds} seconds.", seconds
            )
        if server_ready_at is not None:
            notify(f"ScamBusters cooldown in effect. Waiting {math.ceil(wait)} seconds.")
        elif hour_full:
            notify(f"Local hourly limit reached. Waiting {math.ceil(wait)} seconds.")
        elif wait >= 2:
            notify(f"Waiting {math.ceil(wait)} seconds before the next request.")
        time.sleep(wait)


def _rate_wait(error: HTTPError, limits: _RateLimits) -> float:
    value = error.headers.get("Retry-After", "").strip()
    if value.isascii() and value.isdecimal():
        try:
            return max(limits.spacing, float(int(value)))
        except (ValueError, OverflowError):
            return HOUR_WINDOW
    if value:
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            return max(limits.spacing, deadline.timestamp() - time.time())
        except (ValueError, OverflowError, OSError):
            pass
    try:
        body = error.read(4096).lower()
    except (OSError, HTTPException):
        return HOUR_WINDOW if limits.hourly_limit is not None else 61
    if limits.hourly_limit is not None and b"per hour" in body:
        return HOUR_WINDOW
    remaining = re.search(rb"try again in ([0-9]{1,9})s\b", body)
    return max(limits.spacing, float(remaining[1])) if remaining else 61


def _classify(body: bytes) -> SiteStatus:
    try:
        data = json.loads(body)
    except ValueError:
        raise _RequestError("ScamBusters returned invalid JSON.") from None
    if not isinstance(data, dict):
        raise _RequestError("ScamBusters returned an unexpected response.")
    reported = data.get("is_reported")
    wallets = data.get("has_wallets")
    if type(reported) is not bool or type(wallets) is not bool or (wallets and not reported):
        raise _RequestError("ScamBusters returned an unexpected report or wallet status.")
    if not reported:
        return "unreported"
    return "reported_with_wallets" if wallets else "reported_no_wallets"


@dataclass(repr=False)
class _Session:
    connection: sqlite3.Connection
    opener: OpenerDirector
    key: str
    limits: _RateLimits
    replace_key: Callable[[_CredentialError, bool], str] | None
    notify: Callable[[str], None]

    def read(self, url: str, invalid_input: str) -> bytes:
        try:
            return _request_body(url, self, invalid_input)
        except _CredentialError as error:
            if self.replace_key is None:
                raise
            self.key = self.replace_key(error, True)
            return _request_body(url, self, invalid_input)


def _request_body(url: str, session: _Session, invalid_input: str) -> bytes:
    request = Request(
        url, headers={"Authorization": f"Bearer {session.key}", "Accept": "application/json"},
    )
    for attempt in range(1, MAX_ATTEMPTS + 1):
        _take_request_slot(session.connection, session.limits, session.notify)
        wait = None
        try:
            with session.opener.open(request, timeout=REQUEST_TIMEOUT) as response:
                if response.status != 200:
                    raise _RequestError(f"ScamBusters returned HTTP {response.status}.")
                return response.read()
        except HTTPError as error:
            with error:
                if error.code == 401:
                    raise _CredentialError(
                        "api_key_invalid", "ScamBusters rejected the API key as invalid or revoked."
                    ) from None
                if error.code == 403:
                    raise _CredentialError("api_key_expired", "ScamBusters says the API key has expired.") from None
                if error.code == 429:
                    wait = _rate_wait(error, session.limits)
                elif 500 <= error.code < 600:
                    reason = f"ScamBusters returned HTTP {error.code}."
                elif error.code == 400:
                    raise _RequestError(invalid_input) from None
                else:
                    raise _RequestError(f"ScamBusters returned HTTP {error.code}.") from None
        except (OSError, HTTPException):
            reason = "Could not connect to ScamBusters or the request timed out."
            if attempt == MAX_ATTEMPTS:
                raise _BatchStop("connection_failed", f"{reason} Stopped after {MAX_ATTEMPTS} attempts.") from None

        if wait is not None:
            seconds = math.ceil(wait)
            with session.connection:
                session.connection.execute("INSERT INTO cooldowns VALUES (?)", (time.time() + wait,))
            if wait > MAX_RATE_WAIT or attempt == MAX_ATTEMPTS:
                raise _BatchStop(
                    "rate_limited", f"ScamBusters rate limit reached. Try again in about {seconds} seconds.", seconds
                )
        elif attempt == MAX_ATTEMPTS:
            raise _RequestError(f"{reason} Failed after {MAX_ATTEMPTS} attempts.")
        else:
            session.notify(f"{reason} Retrying the request.")
            time.sleep(2 ** attempt)


def _select_key(state_dir: Path, api_key: str | None) -> str:
    if api_key is not None and not isinstance(api_key, str):
        raise TypeError("api_key must be a string or None.")
    key = api_key.strip() if api_key is not None else _load_key(state_dir)
    if not key:
        raise _CredentialError("api_key_missing", "A valid API key was not found.")
    if not KEY_PATTERN.fullmatch(key):
        raise _CredentialError("api_key_invalid", "The API key format is invalid.")
    return key


@contextmanager
def _open_session(
    limits: _RateLimits,
    *,
    state_dir: Path,
    api_key: str | None = None,
    replace_key: Callable[[_CredentialError, bool], str] | None = None,
    notify: Callable[[str], None] = _quiet,
) -> Iterator[_Session]:
    try:
        try:
            key = _select_key(state_dir, api_key)
        except _CredentialError as error:
            if replace_key is None:
                raise
            key = replace_key(error, False)

        opener = build_opener(_NoRedirects())
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(sqlite3.connect(state_dir / limits.database)) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS requests (sent_at REAL NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS cooldowns (retry_at REAL NOT NULL)")
            yield _Session(connection, opener, key, limits, replace_key, notify)
    except (OSError, UnicodeError, sqlite3.Error) as error:
        raise _BatchStop("local_error", f"Local setup or request history error ({error}).") from None


def _run_checks(
    sites: list[str],
    results: dict[str, SiteResult],
    *,
    state_dir: Path,
    api_key: str | None = None,
    replace_key: Callable[[_CredentialError, bool], str] | None = None,
    notify: Callable[[str], None] = _quiet,
) -> None:
    with _open_session(
        _CHECK_LIMITS, state_dir=state_dir, api_key=api_key, replace_key=replace_key, notify=notify
    ) as session:
        for index, site in enumerate(sites, start=1):
            notify(f"Checking website {index} of {len(sites)}.")
            query = urlencode({"site_url": site})
            try:
                body = session.read(f"{CHECK_URL}?{query}", "ScamBusters rejected the website (HTTP 400).")
                status = _classify(body)
            except _RequestError as error:
                results[site] = {"website": site, "status": "failed", "error": str(error)}
            else:
                results[site] = {"website": site, "status": status, "error": None}


def check_websites(
    websites: str | Iterable[str],
    *,
    api_key: str | None = None,
    state_dir: str | Path | None = None,
) -> BatchResult:
    """Check 1 to 200 websites and return JSON-serializable results in input order.

    An explicit key overrides the state directory's .env and the environment.
    The state directory holds the shared request history and defaults to .venv
    beside this module. An early stop returns partial results and a batch error.
    """
    sites = _normalize_websites(websites)
    directory = DEFAULT_STATE_DIR if state_dir is None else Path(state_dir).expanduser()
    results: dict[str, SiteResult] = {}
    stopped: BatchError | None = None
    try:
        _run_checks(sites, results, api_key=api_key, state_dir=directory)
    except _BatchStop as error:
        stopped = error.as_result()
    return {
        "results": [
            results.get(site, {"website": site, "status": "not_checked", "error": None})
            for site in sites
        ],
        "error": stopped,
    }


def _new_history(page: int) -> HistoryResult:
    if type(page) is not int or page < 1:
        raise ValueError("Page must be a positive integer.")
    return {
        "submissions": [],
        "pages": [],
        "total_pages": None,
        "total_submissions": None,
        "next_page": page,
        "error": None,
    }


def _parse_history_page(
    body: bytes, requested: int
) -> tuple[int, int, int, list[dict[str, object]]]:
    try:
        data = json.loads(body)
    except ValueError:
        raise _BatchStop("invalid_response", "ScamBusters returned invalid history JSON.") from None
    if not isinstance(data, dict):
        raise _BatchStop("invalid_response", "ScamBusters returned an unexpected history response.")

    page, total_pages = data.get("page"), data.get("total_pages")
    total_submissions, submissions = data.get("total_submissions"), data.get("submissions")
    if (
        type(page) is not int or page < 0
        or type(total_pages) is not int or total_pages < 0
        or type(total_submissions) is not int or total_submissions < 0
    ):
        raise _BatchStop("invalid_response", "ScamBusters returned invalid history pagination.")
    if not isinstance(submissions, list) or any(not isinstance(row, dict) for row in submissions):
        raise _BatchStop("invalid_response", "ScamBusters returned an invalid list of submissions.")
    if total_pages == 0:
        if page not in (0, 1) or total_submissions or submissions:
            raise _BatchStop("invalid_response", "ScamBusters returned inconsistent empty history.")
    elif page != min(requested, total_pages):
        raise _BatchStop("invalid_response", f"ScamBusters returned an unexpected page for page {requested}.")
    return page, total_pages, total_submissions, submissions


def _run_history(
    history: HistoryResult,
    *,
    all_pages: bool,
    state_dir: Path,
    api_key: str | None = None,
    replace_key: Callable[[_CredentialError, bool], str] | None = None,
    notify: Callable[[str], None] = _quiet,
) -> None:
    with _open_session(
        _HISTORY_LIMITS, state_dir=state_dir, api_key=api_key, replace_key=replace_key, notify=notify
    ) as session:
        while (requested := history["next_page"]) is not None:
            notify(f"Fetching submission history page {requested}.")
            try:
                body = session.read(
                    f"{HISTORY_URL}?page={requested}", "ScamBusters rejected the history page (HTTP 400)."
                )
            except _RequestError as error:
                raise _BatchStop("request_failed", str(error)) from None
            page, total_pages, total_submissions, submissions = _parse_history_page(body, requested)
            if page in history["pages"]:
                raise _BatchStop("invalid_response", f"ScamBusters returned history page {page} twice.")
            history["submissions"].extend(submissions)
            history["pages"].append(page)
            history["total_pages"] = total_pages
            history["total_submissions"] = total_submissions
            history["next_page"] = page + 1 if page < total_pages else None
            if not all_pages:
                break


def get_submission_history(
    page: int = 1,
    *,
    all_pages: bool = False,
    api_key: str | None = None,
    state_dir: str | Path | None = None,
) -> HistoryResult:
    """Read one page of scored submissions or all pages from the given page onward.

    The history uses a separate rate limit and the same key as website checks.
    On an early stop, the result retains fetched pages and the next page to try.
    """
    if type(all_pages) is not bool:
        raise TypeError("all_pages must be a boolean.")
    history = _new_history(page)
    directory = DEFAULT_STATE_DIR if state_dir is None else Path(state_dir).expanduser()
    try:
        _run_history(history, all_pages=all_pages, state_dir=directory, api_key=api_key)
    except _BatchStop as error:
        history["error"] = error.as_result()
    return history
