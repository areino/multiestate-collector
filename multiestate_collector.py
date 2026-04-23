#!/usr/bin/env python3
"""
Multi-estate Sophos Central SIEM → Taegis XDR File Upload collector.

Single-file deployment: JSON config + `python multiestate_collector.py --config ... --once|--loop`.
Dependencies: httpx, pydantic (see requirements.txt).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, TypeVar
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, Field

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_SKIP_RECORD_KEYS = frozenset(
    (
        "name",
        "msg",
        "args",
        "created",
        "msecs",
        "relativeCreated",
        "levelno",
        "levelname",
        "pathname",
        "filename",
        "module",
        "lineno",
        "funcName",
        "exc_info",
        "exc_text",
        "stack_info",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
    )
)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for k, v in record.__dict__.items():
            if k not in _SKIP_RECORD_KEYS and not k.startswith("_"):
                payload[k] = v
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> logging.Logger:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    return logging.getLogger("collector")


def slog(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """Emit a log line with structured fields (stdlib requires `extra=`, not arbitrary kwargs)."""
    if fields:
        logger.log(level, event, extra=fields)
    else:
        logger.log(level, event)


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class BreakerState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass
class CircuitBreakerConfig:
    failures_to_open: int = 5
    open_cooldown_seconds: float = 1800.0
    half_open_max_calls: int = 2
    half_open_successes_to_close: int = 2
    failure_reset_window_seconds: float = 3600.0


@dataclass
class CircuitBreakerSnapshot:
    state: BreakerState = BreakerState.CLOSED
    failure_count: int = 0
    window_failures: int = 0
    last_failure_at: float | None = None
    opened_at: float | None = None
    next_attempt_at: float | None = None
    reason: str | None = None
    half_open_calls: int = 0
    half_open_successes: int = 0
    consecutive_open_count: int = 0


def utc_now_ts() -> float:
    return datetime.now(UTC).timestamp()


class EstateCircuitBreaker:
    def __init__(self, cfg: CircuitBreakerConfig, snapshot: CircuitBreakerSnapshot | None = None) -> None:
        self.cfg = cfg
        self.snap = snapshot or CircuitBreakerSnapshot()

    def snapshot(self) -> CircuitBreakerSnapshot:
        return self.snap

    def allow_call(self, now: float | None = None) -> tuple[bool, list[tuple[str, BreakerState, BreakerState]]]:
        now = now if now is not None else utc_now_ts()
        transitions: list[tuple[str, BreakerState, BreakerState]] = []
        self._expire_window_failures(now)
        if self.snap.state == BreakerState.CLOSED:
            return True, transitions
        if self.snap.state == BreakerState.OPEN:
            if self.snap.next_attempt_at is not None and now >= self.snap.next_attempt_at:
                old, new = self.snap.state, BreakerState.HALF_OPEN
                self._transition_to_half_open(now)
                transitions.append(("OPEN→HALF_OPEN", old, new))
                return True, transitions
            return False, transitions
        return self.snap.half_open_calls < self.cfg.half_open_max_calls, transitions

    def on_success(self, now: float | None = None) -> list[tuple[str, BreakerState, BreakerState]]:
        now = now if now is not None else utc_now_ts()
        transitions: list[tuple[str, BreakerState, BreakerState]] = []
        self._expire_window_failures(now)
        if self.snap.state == BreakerState.HALF_OPEN:
            self.snap.half_open_successes += 1
            if self.snap.half_open_successes >= self.cfg.half_open_successes_to_close:
                old, new = self.snap.state, BreakerState.CLOSED
                self.snap.state = BreakerState.CLOSED
                self.snap.failure_count = 0
                self.snap.window_failures = 0
                self.snap.opened_at = None
                self.snap.next_attempt_at = None
                self.snap.reason = None
                self.snap.half_open_calls = 0
                self.snap.half_open_successes = 0
                transitions.append(("HALF_OPEN→CLOSED", old, new))
        elif self.snap.state == BreakerState.CLOSED:
            self.snap.failure_count = 0
            self.snap.window_failures = 0
        return transitions

    def on_failure(self, reason: str, *, trip: bool, now: float | None = None) -> list[tuple[str, BreakerState, BreakerState]]:
        now = now if now is not None else utc_now_ts()
        transitions: list[tuple[str, BreakerState, BreakerState]] = []
        self.snap.last_failure_at = now
        self.snap.reason = reason

        if self.snap.state == BreakerState.HALF_OPEN:
            old, new = self.snap.state, BreakerState.OPEN
            self.snap.state = BreakerState.OPEN
            cooldown = self._cooldown_seconds()
            self.snap.opened_at = now
            self.snap.next_attempt_at = now + cooldown
            self.snap.half_open_calls = 0
            self.snap.half_open_successes = 0
            self.snap.consecutive_open_count += 1
            transitions.append(("HALF_OPEN→OPEN", old, new))
            return transitions

        self.snap.window_failures += 1
        if trip:
            self.snap.failure_count += 1

        if self.snap.state == BreakerState.CLOSED and trip and self.snap.failure_count >= self.cfg.failures_to_open:
            old, new = self.snap.state, BreakerState.OPEN
            self.snap.state = BreakerState.OPEN
            cooldown = self._cooldown_seconds()
            self.snap.opened_at = now
            self.snap.next_attempt_at = now + cooldown
            self.snap.consecutive_open_count += 1
            transitions.append(("CLOSED→OPEN", old, new))
        return transitions

    def begin_request(self) -> None:
        if self.snap.state == BreakerState.HALF_OPEN:
            self.snap.half_open_calls += 1

    def _transition_to_half_open(self, now: float) -> None:
        self.snap.state = BreakerState.HALF_OPEN
        self.snap.half_open_calls = 0
        self.snap.half_open_successes = 0
        _ = now

    def _cooldown_seconds(self) -> float:
        base = self.cfg.open_cooldown_seconds
        mult = 1.0 + min(4, self.snap.consecutive_open_count) * 0.25
        return base * mult

    def _expire_window_failures(self, now: float) -> None:
        if self.snap.last_failure_at is None:
            return
        if now - self.snap.last_failure_at > self.cfg.failure_reset_window_seconds:
            self.snap.failure_count = 0
            self.snap.window_failures = 0


# ---------------------------------------------------------------------------
# Retry / backoff
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackoffConfig:
    initial_seconds: float = 1.0
    max_seconds: float = 120.0
    multiplier: float = 2.0
    jitter_ratio: float = 0.2


def parse_retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    raw = raw.strip()
    try:
        return float(raw)
    except ValueError:
        return None


def backoff_delay(attempt: int, cfg: BackoffConfig) -> float:
    base = min(cfg.initial_seconds * (cfg.multiplier**attempt), cfg.max_seconds)
    jitter = base * cfg.jitter_ratio * random.random()
    return base + jitter


async def sleep_for(seconds: float) -> None:
    await asyncio.sleep(max(0.0, seconds))


async def retry_async(
    op: Callable[[], Awaitable[T]],
    *,
    should_retry: Callable[[Exception], bool],
    max_attempts: int,
    backoff: BackoffConfig,
    on_retry: Callable[[int, Exception, float], Any] | None = None,
    retry_after_parser: Callable[[Exception], float | None] | None = None,
) -> T:
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await op()
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts - 1 or not should_retry(exc):
                raise
            delay = backoff_delay(attempt, backoff)
            if retry_after_parser:
                maybe_ra = retry_after_parser(exc)
                if maybe_ra is not None:
                    delay = max(delay, maybe_ra)
            if on_retry:
                on_retry(attempt, exc, delay)
            await sleep_for(delay)
    assert last_exc is not None
    raise last_exc


def http_should_retry(exc: Exception) -> bool:
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 429, 500, 502, 503, 504}
    return False


def http_retry_after_from_exc(exc: Exception) -> float | None:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        return parse_retry_after(exc.response)
    return None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class SophosEstateConfig(BaseModel):
    name: str
    tenant_id: str
    client_id: str
    client_secret: str
    data_region: str = Field(..., description="Sophos data region for api-{region}.central.sophos.com")
    exclude_types: list[str] | None = None
    oauth_token_url: str | None = None
    token_scope: str = Field(default="token")
    sophos_api_base_url: str | None = None
    min_request_interval_seconds: float = Field(default=0.25, ge=0.0)


class TaegisConfig(BaseModel):
    api_endpoint: str
    client_id: str
    client_secret: str
    tenant_id: str
    auth_path: str = "/auth/api/v2/auth/token"
    s3_signer_path: str = "/s3-signer/v2/signed-s3url"
    # Official presign docs list only file_name, content_length, sensor_id (optional).
    # Some tenants return 400 if `service` is present or if sensor_id is not hostname-like.
    service: str | None = Field(
        default=None,
        description="Optional query param; leave unset unless Taegis expects a specific integration name.",
    )
    sensor_id: str | None = Field(
        default=None,
        description="Optional; omit to use API default (toaster.localhost). Use e.g. collector.localhost if you set it.",
    )


class BatchingConfig(BaseModel):
    max_events: int = Field(default=5000, ge=1)
    max_bytes: int = Field(default=8_000_000, ge=1024)
    max_age_seconds: float = Field(default=300.0, ge=1.0)


class CircuitBreakerSettings(BaseModel):
    failures_to_open: int = Field(default=5, ge=1)
    open_cooldown_seconds: float = Field(default=1800.0, ge=1.0)
    half_open_max_calls: int = Field(default=2, ge=1)
    half_open_successes_to_close: int = Field(default=2, ge=1)
    failure_reset_window_seconds: float = Field(default=3600.0, ge=1.0)


class RetryPolicy(BaseModel):
    sophos_max_attempts: int = Field(default=6, ge=1)
    taegis_max_attempts: int = Field(default=8, ge=1)
    backoff_initial_seconds: float = Field(default=1.0, ge=0.1)
    backoff_max_seconds: float = Field(default=120.0, ge=1.0)


class ObservabilityConfig(BaseModel):
    summary_interval_seconds: float = Field(default=900.0, ge=60.0)


class CollectorConfig(BaseModel):
    sophos_estates: list[SophosEstateConfig]
    poll_interval_seconds: float = Field(default=3600.0, ge=10.0)
    max_concurrent_estates: int = Field(default=3, ge=1)
    max_pages_per_estate_per_cycle: int = Field(default=50, ge=1)
    initial_backfill_minutes: int = Field(default=120, ge=1, le=1440)
    behind_warning_minutes: float = Field(default=45.0, ge=1.0)
    sophos_events_limit: int = Field(default=1000, ge=1, le=1000)
    taegis: TaegisConfig
    output_format: str = Field(default="jsonl")
    batching: BatchingConfig = Field(default_factory=BatchingConfig)
    state_dir: str = Field(default="./var/state")
    spool_dir: str = Field(default="./var/spool")
    circuit_breaker: CircuitBreakerSettings = Field(default_factory=CircuitBreakerSettings)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    log_level: str = Field(default="INFO")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _nested_set(target: dict[str, Any], keys: list[str], value: Any) -> None:
    cur = target
    for key in keys[:-1]:
        cur = cur.setdefault(key, {})
        if not isinstance(cur, dict):
            raise ValueError(f"Cannot override non-dict segment at {key}")
    cur[keys[-1]] = value


def _coerce_env_value(val: str) -> Any:
    lowered = val.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        if "." in val:
            return float(val)
        return int(val)
    except ValueError:
        return val


def load_config(path: str | Path, env_os: dict[str, str] | None = None) -> CollectorConfig:
    env_os = env_os if env_os is not None else os.environ
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    prefix = "MULTIESTATE_"
    overrides: dict[str, Any] = {}
    for k, v in env_os.items():
        if not k.startswith(prefix):
            continue
        remainder = k[len(prefix) :]
        if not remainder or remainder == "CONFIG":
            continue
        keys = remainder.lower().split("__")
        _nested_set(overrides, keys, _coerce_env_value(v))
    merged = _deep_merge(raw, overrides)
    return CollectorConfig.model_validate(merged)


# ---------------------------------------------------------------------------
# State store
# ---------------------------------------------------------------------------


def safe_filename_component(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", value.strip())
    return cleaned or "unknown"


@dataclass
class CursorState:
    next_cursor: str | None = None
    last_cursor_saved_at: float | None = None
    last_event_seen_at: float | None = None


@dataclass
class HealthState:
    last_successful_sophos_pull: dict[str, float]
    last_successful_taegis_upload: float | None
    spool_depth: int


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.cursors_dir = self.state_dir / "cursors"
        self.breakers_dir = self.state_dir / "breakers"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cursors_dir.mkdir(parents=True, exist_ok=True)
        self.breakers_dir.mkdir(parents=True, exist_ok=True)

    def _cursor_path(self, estate_key: str) -> Path:
        return self.cursors_dir / f"{safe_filename_component(estate_key)}.json"

    def _breaker_path(self, estate_key: str) -> Path:
        return self.breakers_dir / f"{safe_filename_component(estate_key)}.json"

    def load_cursor(self, estate_key: str) -> CursorState:
        path = self._cursor_path(estate_key)
        if not path.exists():
            return CursorState()
        data = json.loads(path.read_text(encoding="utf-8"))
        return CursorState(
            next_cursor=data.get("next_cursor"),
            last_cursor_saved_at=data.get("last_cursor_saved_at"),
            last_event_seen_at=data.get("last_event_seen_at"),
        )

    def save_cursor(self, estate_key: str, cursor: CursorState) -> None:
        path = self._cursor_path(estate_key)
        path.write_text(json.dumps(asdict(cursor), indent=2), encoding="utf-8")

    def load_breaker(self, estate_key: str) -> CircuitBreakerSnapshot | None:
        path = self._breaker_path(estate_key)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        st = data.get("state", "CLOSED")
        try:
            state = BreakerState(st)
        except ValueError:
            state = BreakerState.CLOSED
        return CircuitBreakerSnapshot(
            state=state,
            failure_count=int(data.get("failure_count", 0)),
            window_failures=int(data.get("window_failures", 0)),
            last_failure_at=data.get("last_failure_at"),
            opened_at=data.get("opened_at"),
            next_attempt_at=data.get("next_attempt_at"),
            reason=data.get("reason"),
            half_open_calls=int(data.get("half_open_calls", 0)),
            half_open_successes=int(data.get("half_open_successes", 0)),
            consecutive_open_count=int(data.get("consecutive_open_count", 0)),
        )

    def save_breaker(self, estate_key: str, snap: CircuitBreakerSnapshot) -> None:
        path = self._breaker_path(estate_key)
        payload = asdict(snap)
        payload["state"] = snap.state.value
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def write_health(self, health: HealthState) -> None:
        path = self.state_dir / "health.json"
        path.write_text(
            json.dumps(
                {
                    "last_successful_sophos_pull": health.last_successful_sophos_pull,
                    "last_successful_taegis_upload": health.last_successful_taegis_upload,
                    "spool_depth": health.spool_depth,
                },
                indent=2,
            ),
            encoding="utf-8",
        )


def spool_queue_depth(spool_dir: Path) -> int:
    spool_dir = Path(spool_dir)
    if not spool_dir.exists():
        return 0
    return sum(1 for p in spool_dir.iterdir() if p.is_file() and not p.name.startswith("."))


# ---------------------------------------------------------------------------
# Batcher
# ---------------------------------------------------------------------------


def _event_size_bytes(obj: dict[str, Any]) -> int:
    return len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) + 1


@dataclass
class JsonlBatcher:
    spool_dir: Path
    cfg: BatchingConfig
    events: list[dict[str, Any]] = field(default_factory=list)
    buffer_bytes: int = 0
    started_at: float = field(default_factory=time.time)

    def _should_flush(self, next_obj: dict[str, Any] | None) -> bool:
        if not self.events:
            return False
        if self.cfg.max_age_seconds and (time.time() - self.started_at) >= self.cfg.max_age_seconds:
            return True
        next_b = _event_size_bytes(next_obj) if next_obj is not None else 0
        if len(self.events) >= self.cfg.max_events:
            return True
        if self.buffer_bytes + next_b > self.cfg.max_bytes:
            return True
        return False

    def add(self, obj: dict[str, Any]) -> list[Path]:
        written: list[Path] = []
        if self._should_flush(obj):
            written.extend(self.flush())
        self.events.append(obj)
        self.buffer_bytes += _event_size_bytes(obj)
        if self._should_flush(None):
            written.extend(self.flush())
        return written

    def extend(self, objs: Iterable[dict[str, Any]]) -> list[Path]:
        written: list[Path] = []
        for o in objs:
            written.extend(self.add(o))
        return written

    def flush(self) -> list[Path]:
        if not self.events:
            return []
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        # Use .log extension: File Upload API expects plain-text logs; .jsonl can trigger presign 400 on some stacks.
        name = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"_{uuid.uuid4().hex}.log"
        path = self.spool_dir / name
        body = "\n".join(json.dumps(ev, ensure_ascii=False) for ev in self.events) + "\n"
        path.write_text(body, encoding="utf-8")
        self.events.clear()
        self.buffer_bytes = 0
        self.started_at = time.time()
        return [path]


# ---------------------------------------------------------------------------
# Sophos client
# ---------------------------------------------------------------------------

DEFAULT_OAUTH_URL = "https://id.sophos.com/api/v2/oauth2/token"


def sophos_api_base(estate: SophosEstateConfig) -> str:
    if estate.sophos_api_base_url:
        return estate.sophos_api_base_url.rstrip("/")
    region = estate.data_region.strip().lower()
    return f"https://api-{region}.central.sophos.com"


@dataclass
class SophosEventsPage:
    items: list[dict[str, Any]]
    has_more: bool
    next_cursor: str | None


@dataclass
class _SophosToken:
    access_token: str
    expires_at: float


class SophosClient:
    def __init__(self, retry: RetryPolicy) -> None:
        self._retry = retry
        self._tokens: dict[str, _SophosToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_request_at: dict[str, float] = {}

    def _estate_key(self, estate: SophosEstateConfig) -> str:
        return estate.tenant_id

    async def _pace(self, estate: SophosEstateConfig) -> None:
        key = self._estate_key(estate)
        interval = estate.min_request_interval_seconds
        if interval <= 0:
            return
        last = self._last_request_at.get(key)
        now = time.monotonic()
        if last is not None:
            wait = interval - (now - last)
            if wait > 0:
                await asyncio.sleep(wait)

    def _touch_request(self, estate: SophosEstateConfig) -> None:
        self._last_request_at[self._estate_key(estate)] = time.monotonic()

    async def _get_token(self, estate: SophosEstateConfig, client: httpx.AsyncClient) -> str:
        key = self._estate_key(estate)
        now = time.time()
        tok = self._tokens.get(key)
        if tok and now < tok.expires_at - 60:
            return tok.access_token
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        async with self._locks[key]:
            tok = self._tokens.get(key)
            now = time.time()
            if tok and now < tok.expires_at - 60:
                return tok.access_token
            token_url = estate.oauth_token_url or DEFAULT_OAUTH_URL
            data = {
                "grant_type": "client_credentials",
                "client_id": estate.client_id,
                "client_secret": estate.client_secret,
                "scope": estate.token_scope,
            }
            resp = await client.post(token_url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
            resp.raise_for_status()
            payload = resp.json()
            access = payload["access_token"]
            expires_in = float(payload.get("expires_in", 3600))
            self._tokens[key] = _SophosToken(access_token=access, expires_at=time.time() + expires_in)
            return access

    async def fetch_events_page(
        self,
        estate: SophosEstateConfig,
        *,
        limit: int,
        cursor: str | None,
        from_date: int | None,
        exclude_types: list[str] | None,
        client: httpx.AsyncClient,
    ) -> SophosEventsPage:
        base = sophos_api_base(estate)
        key = self._estate_key(estate)
        backoff = BackoffConfig(
            initial_seconds=self._retry.backoff_initial_seconds,
            max_seconds=self._retry.backoff_max_seconds,
        )

        def sophos_should_retry(exc: Exception) -> bool:
            if isinstance(exc, httpx.HTTPStatusError):
                code = exc.response.status_code
                if code == 401:
                    return True
                return http_should_retry(exc)
            return http_should_retry(exc)

        async def op() -> SophosEventsPage:
            await self._pace(estate)
            token = await self._get_token(estate, client)
            params: dict[str, Any] = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            elif from_date is not None:
                params["from_date"] = from_date
            if exclude_types:
                params["exclude_types"] = ",".join(exclude_types)
            headers = {
                "Authorization": f"Bearer {token}",
                "X-Tenant-ID": estate.tenant_id,
                "Accept": "application/json",
            }
            self._touch_request(estate)
            r = await client.get(f"{base}/siem/v1/events", headers=headers, params=params)
            if r.status_code == 401:
                self._tokens.pop(key, None)
            if r.status_code in {401, 403}:
                r.raise_for_status()
            if r.status_code >= 400:
                r.raise_for_status()
            data = r.json()
            items = data.get("items") or data.get("events") or []
            if not isinstance(items, list):
                items = []
            has_more = bool(data.get("has_more", False))
            next_cursor = data.get("next_cursor")
            return SophosEventsPage(items=items, has_more=has_more, next_cursor=next_cursor)

        return await retry_async(
            op,
            should_retry=sophos_should_retry,
            max_attempts=self._retry.sophos_max_attempts,
            backoff=backoff,
            retry_after_parser=http_retry_after_from_exc,
        )


def classify_sophos_failure(exc: Exception) -> tuple[bool, bool]:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in {401, 403}:
            return True, True
        if code == 429:
            return True, False
        if code >= 500:
            return True, False
        return False, False
    if isinstance(exc, httpx.RequestError):
        return True, False
    return True, False


# ---------------------------------------------------------------------------
# Taegis client
# ---------------------------------------------------------------------------

MIN_UPLOAD_BYTES = 1024


@dataclass
class TaegisToken:
    access_token: str
    expires_at: float


class TaegisClient:
    def __init__(self, cfg: TaegisConfig, retry: RetryPolicy) -> None:
        self.cfg = cfg
        self._retry = retry
        self._token: TaegisToken | None = None

    def _endpoint(self) -> str:
        return self.cfg.api_endpoint.rstrip("/")

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        now = time.time()
        if self._token and now < self._token.expires_at - 120:
            return self._token.access_token
        auth_url = f"{self._endpoint()}{self.cfg.auth_path}"
        resp = await client.post(
            auth_url,
            headers={"Content-Type": "application/json", "X-Tenant-Context": self.cfg.tenant_id},
            auth=(self.cfg.client_id, self.cfg.client_secret),
            json={"grant_type": "client_credentials"},
        )
        resp.raise_for_status()
        body = resp.json()
        token = body["access_token"]
        expires_in = float(body.get("expires_in", 36000))
        self._token = TaegisToken(access_token=token, expires_at=time.time() + expires_in)
        return token

    async def upload_file(self, path: Path, client: httpx.AsyncClient) -> None:
        raw = Path(path).read_bytes()
        if len(raw) < MIN_UPLOAD_BYTES:
            raw = raw + (b"\n" * (MIN_UPLOAD_BYTES - len(raw)))
        signer = f"{self._endpoint()}{self.cfg.s3_signer_path}"
        params: dict[str, str] = {
            "file_name": Path(path).name,
            "content_length": str(len(raw)),
        }
        if self.cfg.service:
            params["service"] = self.cfg.service
        if self.cfg.sensor_id:
            params["sensor_id"] = self.cfg.sensor_id
        url = f"{signer}?{urlencode(params)}"
        backoff = BackoffConfig(
            initial_seconds=self._retry.backoff_initial_seconds,
            max_seconds=self._retry.backoff_max_seconds,
        )

        async def op() -> None:
            token = await self._access_token(client)
            r = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}", "X-Tenant-Context": self.cfg.tenant_id},
            )
            if r.status_code in {401, 403}:
                self._token = None
                r.raise_for_status()
            if r.status_code >= 400:
                body_preview = (r.text or "")[:4000]
                raise httpx.HTTPStatusError(
                    f"{r.status_code} {r.reason_phrase} for url {r.url!r} — presign body: {body_preview}",
                    request=r.request,
                    response=r,
                )
            body: dict[str, Any] = r.json()
            location = body.get("location") or body.get("url")
            if not location:
                raise RuntimeError("Taegis signer response missing location/url")
            put = await client.put(location, content=raw, headers={"Content-Type": "application/octet-stream"})
            if put.status_code >= 400:
                put.raise_for_status()

        await retry_async(
            op,
            should_retry=http_should_retry,
            max_attempts=self._retry.taegis_max_attempts,
            backoff=backoff,
            retry_after_parser=http_retry_after_from_exc,
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _iso_utc(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")


def _read_health_raw(store: StateStore) -> dict[str, Any]:
    hp = store.state_dir / "health.json"
    if not hp.exists():
        return {}
    try:
        return json.loads(hp.read_text(encoding="utf-8"))
    except OSError:
        return {}


def _enrich_event(raw: Any, estate_name: str, estate_tenant_id: str, pulled_at: str) -> dict[str, Any]:
    if isinstance(raw, dict):
        out = dict(raw)
    else:
        out = {"value": raw}
    out["estate_name"] = estate_name
    out["estate_tenant_id"] = estate_tenant_id
    out["pulled_at"] = pulled_at
    return out


async def _poll_single_estate(
    estate_cfg: SophosEstateConfig,
    *,
    cfg: CollectorConfig,
    sophos: SophosClient,
    store: StateStore,
    breaker_cfg: CircuitBreakerConfig,
    http: httpx.AsyncClient,
    log: logging.Logger,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    estate_key = estate_cfg.tenant_id
    stats: dict[str, Any] = {
        "estate": estate_cfg.name,
        "tenant_id": estate_key,
        "pages": 0,
        "events": 0,
        "skipped_breaker": False,
        "breaker_transitions": [],
        "duration_seconds": 0.0,
    }
    t0 = time.perf_counter()
    pulled_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    snap = store.load_breaker(estate_key)
    breaker = EstateCircuitBreaker(breaker_cfg, snap)
    allowed, trans = breaker.allow_call(utc_now_ts())
    for tr in trans:
        stats["breaker_transitions"].append({"label": tr[0], "from": tr[1].value, "to": tr[2].value})
        slog(
            log,
            logging.INFO,
            "circuit_breaker_transition",
            estate=estate_cfg.name,
            tenant_id=estate_key,
            transition=tr[0],
            from_state=tr[1].value,
            to_state=tr[2].value,
        )
    if not allowed:
        stats["skipped_breaker"] = True
        slog(
            log,
            logging.WARNING,
            "estate_skipped_breaker_open",
            estate=estate_cfg.name,
            tenant_id=estate_key,
            reason=breaker.snapshot().reason,
            next_attempt_at=_iso_utc(breaker.snapshot().next_attempt_at),
            state=breaker.snapshot().state.value,
        )
        store.save_breaker(estate_key, breaker.snapshot())
        stats["duration_seconds"] = time.perf_counter() - t0
        return [], stats

    cursor_state = store.load_cursor(estate_key)
    collected: list[dict[str, Any]] = []
    cursor = cursor_state.next_cursor
    pages = 0
    poll_finished = False

    try:
        while pages < cfg.max_pages_per_estate_per_cycle:
            allowed, trans = breaker.allow_call(utc_now_ts())
            if not allowed:
                for tr in trans:
                    stats["breaker_transitions"].append({"label": tr[0], "from": tr[1].value, "to": tr[2].value})
                    slog(
                        log,
                        logging.INFO,
                        "circuit_breaker_transition",
                        estate=estate_cfg.name,
                        transition=tr[0],
                        from_state=tr[1].value,
                        to_state=tr[2].value,
                    )
                break
            if breaker.snapshot().state == BreakerState.HALF_OPEN:
                breaker.begin_request()

            from_date = None
            if not cursor:
                now_ts = int(utc_now_ts())
                max_lookback = min(cfg.initial_backfill_minutes * 60, 24 * 3600 - 120)
                from_date = max(0, now_ts - int(max_lookback))

            try:
                page = await sophos.fetch_events_page(
                    estate_cfg,
                    limit=cfg.sophos_events_limit,
                    cursor=cursor,
                    from_date=from_date if not cursor else None,
                    exclude_types=estate_cfg.exclude_types,
                    client=http,
                )
            except Exception as exc:  # noqa: BLE001
                trip, _auth = classify_sophos_failure(exc)
                msg = f"{type(exc).__name__}: {exc}"
                btrans = breaker.on_failure(msg, trip=trip)
                for tr in btrans:
                    stats["breaker_transitions"].append({"label": tr[0], "from": tr[1].value, "to": tr[2].value})
                    slog(
                        log,
                        logging.INFO,
                        "circuit_breaker_transition",
                        estate=estate_cfg.name,
                        transition=tr[0],
                        from_state=tr[1].value,
                        to_state=tr[2].value,
                    )
                store.save_breaker(estate_key, breaker.snapshot())
                stats["duration_seconds"] = time.perf_counter() - t0
                slog(
                    log,
                    logging.ERROR,
                    "sophos_pull_failed",
                    estate=estate_cfg.name,
                    tenant_id=estate_key,
                    error=str(exc),
                    trip=trip,
                )
                return collected, stats

            pages += 1
            stats["pages"] = pages
            for ev in page.items:
                collected.append(_enrich_event(ev, estate_cfg.name, estate_key, pulled_at))
            stats["events"] = len(collected)

            cursor_state.next_cursor = page.next_cursor if page.has_more else None
            cursor_state.last_cursor_saved_at = utc_now_ts()
            store.save_cursor(estate_key, cursor_state)

            if not page.has_more or not page.next_cursor:
                poll_finished = True
                break
            cursor = page.next_cursor

        poll_finished = True

    finally:
        if poll_finished:
            for tr in breaker.on_success():
                stats["breaker_transitions"].append({"label": tr[0], "from": tr[1].value, "to": tr[2].value})
                slog(
                    log,
                    logging.INFO,
                    "circuit_breaker_transition",
                    estate=estate_cfg.name,
                    tenant_id=estate_key,
                    transition=tr[0],
                    from_state=tr[1].value,
                    to_state=tr[2].value,
                )
            store.save_breaker(estate_key, breaker.snapshot())
        stats["duration_seconds"] = time.perf_counter() - t0
        store.save_breaker(estate_key, breaker.snapshot())

    slog(
        log,
        logging.INFO,
        "estate_pull_complete",
        estate=estate_cfg.name,
        tenant_id=estate_key,
        pages=stats["pages"],
        events=len(collected),
        duration_seconds=round(stats["duration_seconds"], 3),
        skipped_breaker=False,
    )
    return collected, stats


async def _upload_spool(
    cfg: CollectorConfig,
    taegis: TaegisClient,
    http: httpx.AsyncClient,
    store: StateStore,
    log: logging.Logger,
) -> None:
    spool = Path(cfg.spool_dir)
    if not spool.exists():
        return
    paths = sorted(p for p in spool.iterdir() if p.is_file() and p.suffix in {".log", ".jsonl", ".txt"})
    for path in paths:
        try:
            raw_health = _read_health_raw(store)
            sophos_pull = dict(raw_health.get("last_successful_sophos_pull") or {})
            sz = path.stat().st_size
            t0 = time.perf_counter()
            await taegis.upload_file(path, http)
            dt = time.perf_counter() - t0
            path.unlink(missing_ok=True)
            slog(
                log,
                logging.INFO,
                "taegis_upload_ok",
                file=str(path),
                upload_bytes=sz,
                duration_seconds=round(dt, 3),
            )
            store.write_health(
                HealthState(
                    last_successful_sophos_pull=sophos_pull,
                    last_successful_taegis_upload=utc_now_ts(),
                    spool_depth=spool_queue_depth(Path(cfg.spool_dir)),
                )
            )
        except Exception as exc:  # noqa: BLE001
            slog(log, logging.ERROR, "taegis_upload_failed", file=str(path), error=str(exc))


async def run_cycle(cfg: CollectorConfig, log: logging.Logger) -> None:
    store = StateStore(Path(cfg.state_dir))
    sophos = SophosClient(cfg.retry)
    taegis = TaegisClient(cfg.taegis, cfg.retry)
    brk_cfg = CircuitBreakerConfig(
        failures_to_open=cfg.circuit_breaker.failures_to_open,
        open_cooldown_seconds=cfg.circuit_breaker.open_cooldown_seconds,
        half_open_max_calls=cfg.circuit_breaker.half_open_max_calls,
        half_open_successes_to_close=cfg.circuit_breaker.half_open_successes_to_close,
        failure_reset_window_seconds=cfg.circuit_breaker.failure_reset_window_seconds,
    )
    limits = httpx.Limits(max_connections=max(32, cfg.max_concurrent_estates * 4))
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0), limits=limits) as http:
        await _upload_spool(cfg, taegis, http, store, log)
        sem = asyncio.Semaphore(cfg.max_concurrent_estates)

        async def guarded(est: SophosEstateConfig):
            async with sem:
                return await _poll_single_estate(est, cfg=cfg, sophos=sophos, store=store, breaker_cfg=brk_cfg, http=http, log=log)

        results = await asyncio.gather(*[asyncio.create_task(guarded(e)) for e in cfg.sophos_estates])
        all_events: list[dict[str, Any]] = []
        estate_health: dict[str, float] = {}
        for events, stats in results:
            all_events.extend(events)
            tid = stats.get("tenant_id")
            if tid and not stats.get("skipped_breaker"):
                estate_health[tid] = utc_now_ts()
            slog(
                log,
                logging.INFO,
                "estate_pull_stats",
                **{k: v for k, v in stats.items() if k != "breaker_transitions"},
            )

        batcher = JsonlBatcher(Path(cfg.spool_dir), cfg.batching)
        written: list[Path] = []
        written.extend(batcher.extend(all_events))
        written.extend(batcher.flush())

        raw_health = _read_health_raw(store)
        merged_pull = dict(raw_health.get("last_successful_sophos_pull") or {})
        merged_pull.update(estate_health)
        prev_upload = raw_health.get("last_successful_taegis_upload")
        store.write_health(
            HealthState(
                last_successful_sophos_pull=merged_pull,
                last_successful_taegis_upload=float(prev_upload) if prev_upload is not None else None,
                spool_depth=spool_queue_depth(Path(cfg.spool_dir)),
            )
        )

        now_ts = utc_now_ts()
        for estate in cfg.sophos_estates:
            tid = estate.tenant_id
            last = merged_pull.get(tid)
            if last is None:
                continue
            lag_min = (now_ts - last) / 60.0
            if lag_min > cfg.behind_warning_minutes:
                slog(
                    log,
                    logging.WARNING,
                    "estate_pull_lag_warning",
                    estate=estate.name,
                    tenant_id=tid,
                    lag_minutes=round(lag_min, 2),
                    hint="Sophos SIEM window is 24h; ensure poll_interval keeps pace with volume",
                )

        slog(
            log,
            logging.INFO,
            "cycle_batch_summary",
            events_total=len(all_events),
            spool_files_created=len(written),
            spool_depth=spool_queue_depth(Path(cfg.spool_dir)),
        )
        await _upload_spool(cfg, taegis, http, store, log)


async def loop_runner(cfg: CollectorConfig, log: logging.Logger) -> None:
    summary_every = cfg.observability.summary_interval_seconds
    next_summary = time.monotonic() + summary_every
    while True:
        t0 = time.monotonic()
        try:
            await run_cycle(cfg, log)
        except Exception as exc:  # noqa: BLE001
            log.error("cycle_failed", extra={"error": str(exc)}, exc_info=True)
        dt = time.perf_counter() - t0
        if time.monotonic() >= next_summary:
            depth = spool_queue_depth(Path(cfg.spool_dir))
            slog(
                log,
                logging.INFO,
                "periodic_summary",
                poll_interval_seconds=cfg.poll_interval_seconds,
                spool_depth=depth,
                estates=len(cfg.sophos_estates),
                last_cycle_seconds=round(dt, 3),
            )
            next_summary = time.monotonic() + summary_every
        await asyncio.sleep(cfg.poll_interval_seconds)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Multi-estate Sophos → Taegis collector")
    p.add_argument("--config", required=True, help="Path to JSON configuration file")
    p.add_argument("--once", action="store_true", help="Run a single collection cycle and exit")
    p.add_argument("--loop", action="store_true", help="Run forever with poll_interval_seconds sleep")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    log = setup_logging(cfg.log_level)

    if args.once and args.loop:
        print("Choose only one of --once or --loop", file=sys.stderr)
        raise SystemExit(2)
    if not args.once and not args.loop:
        print("Specify --once or --loop", file=sys.stderr)
        raise SystemExit(2)

    if args.once:
        asyncio.run(run_cycle(cfg, log))
    else:
        asyncio.run(loop_runner(cfg, log))


if __name__ == "__main__":
    main()
