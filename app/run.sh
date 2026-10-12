#!/bin/sh
# The entire "do we have observability?" switch.
#
# OTEL_ENABLED=false -> the app runs exactly as its developer wrote it.
# OTEL_ENABLED=true  -> `opentelemetry-instrument` loads the SDK, monkey-patches
#                       FastAPI/httpx/asyncpg/redis, and starts emitting OTLP.
#
# Note what did NOT change to turn telemetry on: a single line of application code.
# This is the whole point of auto-instrumentation, and it is a DevOps-side lever.
set -e
if [ "${OTEL_ENABLED:-false}" = "true" ]; then
  exec opentelemetry-instrument "$@"
else
  exec "$@"
fi
