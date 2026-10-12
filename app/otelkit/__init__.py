"""otelkit - the thin internal telemetry library a platform team ships to its developers.

The whole purpose is this: a developer should add business context in ONE line,
not twenty. If adding observability is tedious, it does not get added.

Design note worth understanding. This module imports only the OpenTelemetry
*API*, never the SDK. The API is a deliberate no-op when no SDK is configured,
so every call here costs almost nothing when OTEL_ENABLED=false. That property
is why libraries can ship instrumentation without forcing it on their users, and
it is why this file is safe to import unconditionally.
"""
from __future__ import annotations

import functools
import inspect
import json
import logging
import sys
import time
from typing import Any, Callable

from opentelemetry import metrics, trace
from opentelemetry.trace import Status, StatusCode

_tracer = trace.get_tracer("otelkit")

# One meter per library, same pattern as tracers. Application code creates its
# instruments from this.
meter = metrics.get_meter("otelkit")


def set_attrs(**attrs: Any) -> None:
    """Attach business context to the currently-active span.

    Auto-instrumentation already created that span. It knows the request was
    `POST /orders` and took 70ms. It cannot know whose order it was, what it was
    worth, or which tier the customer is on. That is what this adds.

        set_attrs(**{"order.id": order_id, "customer.tier": "premium"})

    Dotted keys are conventional in OTel. Pass them with ** on a dict.
    """
    span = trace.get_current_span()
    if not span.is_recording():
        return
    for key, value in attrs.items():
        if value is not None:
            span.set_attribute(key, value)


def add_event(name: str, **attrs: Any) -> None:
    """Record a timestamped event inside the current span.

    Use an event, not a child span, when something happened at a POINT in time
    and has no meaningful duration: a retry fired, a cache was invalidated, a
    decision was taken. Events are much cheaper than spans.
    """
    span = trace.get_current_span()
    if span.is_recording():
        span.add_event(name, {k: v for k, v in attrs.items() if v is not None})


def record_error(exc: BaseException, **attrs: Any) -> None:
    """Mark the current span as failed and attach the exception.

    `record_exception` stores the stack trace as a span event. `set_status`
    is what actually makes the span count as an error in Tempo, in the service
    graph, and in any error-rate metric derived from spans. You almost always
    want both - recording the exception alone leaves the span looking green.
    """
    span = trace.get_current_span()
    if not span.is_recording():
        return
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, str(exc)))
    for key, value in attrs.items():
        if value is not None:
            span.set_attribute(key, value)


def traced(name: str | None = None, **static_attrs: Any) -> Callable:
    """Wrap a function in its own span. Works on both sync and async callables.

        @traced("fraud.check", component="risk")
        async def run_fraud_check(...): ...

    Use this for meaningful units of BUSINESS work. Auto-instrumentation covers
    anything that crosses a process boundary; it cannot see a pure function, no
    matter how slow or important that function is.

    Do not decorate everything. Every span costs ingest, storage and query time,
    and a trace with 400 trivial spans is harder to read than one with 20.
    """
    def decorator(fn: Callable) -> Callable:
        span_name = name or fn.__qualname__

        def _apply_static(span) -> None:
            for key, value in static_attrs.items():
                span.set_attribute(key, value)

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                with _tracer.start_as_current_span(span_name) as span:
                    _apply_static(span)
                    try:
                        return await fn(*args, **kwargs)
                    except Exception as exc:
                        span.record_exception(exc)
                        span.set_status(Status(StatusCode.ERROR, str(exc)))
                        raise
            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            with _tracer.start_as_current_span(span_name) as span:
                _apply_static(span)
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    raise
        return sync_wrapper
    return decorator


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
# There are only four instrument types worth memorising, and picking the wrong
# one is the most common metrics mistake:
#
#   Counter          monotonically increases, never decreases.
#                    Requests served, orders placed, bytes sent, errors.
#                    You almost always query it as rate(), never as a raw value.
#
#   UpDownCounter    goes up and down. Queue depth, in-flight requests,
#                    active connections, pool checkouts.
#
#   Histogram        records a DISTRIBUTION of values. Latency, payload size,
#                    order value. Use this whenever you will ever care about a
#                    percentile - which, for latency, is always.
#
#   Gauge            a value sampled at a point in time, where only the latest
#                    reading matters. Temperature, disk free, current price.
#                    Usually observed via a callback rather than recorded.
#
# The decision rule: if you will ever want a p99, it MUST be a histogram. You
# cannot compute a percentile from an average, and averaging percentiles across
# instances produces a number that is not a percentile of anything.


def counter(name: str, description: str = "", unit: str = "1"):
    """A value that only goes up. Query with rate()."""
    return meter.create_counter(name, unit=unit, description=description)


def up_down_counter(name: str, description: str = "", unit: str = "1"):
    """A value that goes up and down. Query directly, not with rate()."""
    return meter.create_up_down_counter(name, unit=unit, description=description)


def histogram(name: str, description: str = "", unit: str = "ms"):
    """A distribution. The only instrument from which percentiles are honest."""
    return meter.create_histogram(name, unit=unit, description=description)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Two things have to be true for logs to be worth having.
#
# 1. They must be STRUCTURED. `grep` across 40 services is not an investigation
#    strategy. JSON means you can filter on a field instead of guessing at a
#    substring.
#
# 2. They must carry trace_id. This is what welds the three signals together:
#    a metric points at a trace via an exemplar, and the trace points at these
#    log lines via trace_id. Without it, "find the logs for this request" means
#    grepping by timestamp and hoping.

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
}


class TraceContextJSONFormatter(logging.Formatter):
    """Emits one JSON object per line, stamped with the active trace context."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # The active span's context. `format(x, "032x")` is the canonical
        # lowercase-hex rendering - the same form Tempo and Loki expect, so the
        # IDs match exactly across all three signals.
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            payload["trace_id"] = format(ctx.trace_id, "032x")
            payload["span_id"] = format(ctx.span_id, "016x")

        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def _is_otel_handler(handler: logging.Handler) -> bool:
    return type(handler).__module__.startswith("opentelemetry")


def setup_logging(level: int = logging.INFO) -> None:
    """Install structured logging without disturbing OTLP log export.

    Deliberately NOT logging.basicConfig(force=True): auto-instrumentation has
    already attached its own LoggingHandler to the root logger, and that handler
    is what ships log records over OTLP. Wiping the handler list - which
    force=True does - silently kills log export while leaving stdout looking
    perfectly healthy.
    """
    root = logging.getLogger()
    root.setLevel(level)

    for handler in list(root.handlers):
        if isinstance(handler, logging.StreamHandler) and not _is_otel_handler(handler):
            root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(TraceContextJSONFormatter())
    root.addHandler(stream)

    # Every one of these emits a line per request that duplicates a span we
    # already have, with less detail and no extra information. Logging what you
    # already trace is how observability bills grow while the signal does not.
    # Raising them to WARNING keeps the failures and drops the narration.
    for noisy in ("uvicorn.access", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    setup_logging(level)
    return logging.getLogger(name)


__all__ = [
    "set_attrs", "add_event", "record_error", "traced",
    "meter", "counter", "up_down_counter", "histogram",
    "setup_logging", "get_logger", "TraceContextJSONFormatter",
]
