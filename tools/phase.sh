#!/usr/bin/env bash
# Switch the lab to a given phase and bring it up.
#
#   ./tools/phase.sh 0      # go to Phase 0 and start it
#   ./tools/phase.sh 3
#   ./tools/phase.sh main   # back to the finished state
#   ./tools/phase.sh status
#
# Each phase tag is a SELF-CONSISTENT snapshot: the code, the compose file and
# the docs are all from that moment. Phase 0 genuinely has no Tempo, because
# the point of Phase 0 is reading raw OTLP before any UI exists.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAB="$ROOT/lab"
cd "$ROOT"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
dim()  { printf '\033[2m%s\033[0m\n' "$*"; }

case "${1:-}" in
  status)
    bold "current: $(git describe --tags --exact-match 2>/dev/null || git rev-parse --abbrev-ref HEAD)"
    docker compose -f "$LAB/docker-compose.yml" ps --format '  {{.Service}}\t{{.Status}}' 2>/dev/null || true
    exit 0
    ;;
  ""|-h|--help)
    bold "usage: ./tools/phase.sh <0-7|main|status>"
    git tag -n1 'phase-*' | sed 's/^/  /'
    exit 0
    ;;
esac

TARGET="$1"
[[ "$TARGET" =~ ^[0-7]$ ]] && REF="phase-$TARGET" || REF="$TARGET"

if ! git rev-parse --verify --quiet "$REF" >/dev/null; then
  echo "unknown phase or ref: $REF" >&2; exit 1
fi

# lab/.env is a scratch file — you are MEANT to edit it during a phase, since
# the chaos knobs live there. Stash those edits so a knob left on from one
# phase cannot quietly distort the next, and so the checkout below can proceed.
if ! git diff --quiet -- "$LAB/.env" 2>/dev/null; then
  git stash push -q -m "phase.sh: lab/.env" -- "$LAB/.env"
  dim "    stashed your lab/.env edits (recover with: git stash pop)"
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "You have uncommitted changes. Commit, stash, or discard them first:" >&2
  git status --short >&2
  exit 1
fi

bold "==> tearing down the current stack (containers AND volumes)"
dim  "    a fresh start per phase: stale Tempo/Prometheus data from a later"
dim  "    phase would otherwise show up as history that cannot exist yet"
docker compose -f "$LAB/docker-compose.yml" down -v --remove-orphans 2>/dev/null || true

bold "==> checking out $REF"
git checkout -q "$REF"
git --no-pager log -1 --format='    %h  %s' HEAD

# Phase 0's lesson IS the flip: run with telemetry off, see nothing, turn it
# on, see everything. The committed .env has it already ON, because the file
# was captured after that demo was run — so set the honest starting position.
if [[ "$REF" == "phase-0" ]]; then
  sed -i.bak 's/^OTEL_ENABLED=true/OTEL_ENABLED=false/' "$LAB/.env" && rm -f "$LAB/.env.bak"
fi

bold "==> building and starting"
docker compose -f "$LAB/docker-compose.yml" up -d --build

echo
bold "==> services now running"
docker compose -f "$LAB/docker-compose.yml" ps --format '    {{.Service}}\t{{.Status}}'

echo
if [[ "$REF" == phase-* ]]; then
  N="${REF#phase-}"
  DOC=$(ls "$ROOT/docs/" | grep -E "^0${N}-" || true)
  bold "==> read next"
  [[ -n "$DOC" ]] && echo "    docs/$DOC"
  if [[ "$REF" == "phase-0" ]]; then
    echo
    bold "==> OTEL_ENABLED is OFF — this is the 'before' picture"
    echo "    docker compose -f lab/docker-compose.yml logs -f otel-collector   # silence, by design"
    echo "    curl -s localhost:8080/health                                     # the app works anyway"
    echo
    echo "    Then flip the switch and watch telemetry appear, with no code change:"
    echo "    sed -i '' 's/OTEL_ENABLED=false/OTEL_ENABLED=true/' lab/.env"
    echo "    docker compose -f lab/docker-compose.yml up -d --force-recreate"
  fi
  echo
  dim  "    You are on a detached HEAD. That is fine for reading and running."
  dim  "    To experiment:  git switch -c my-phase-$N"
  dim  "    When done:      ./tools/phase.sh $(( N + 1 ))   (or 'main')"
fi
