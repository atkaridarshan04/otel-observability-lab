#!/usr/bin/env bash
# Move the lab to a given phase of the course and bring it up.
#
#   ./tools/phase.sh 0        # go to Phase 0 and start it
#   ./tools/phase.sh 3
#   ./tools/phase.sh main     # back to the finished state
#   ./tools/phase.sh status   # where am I, what is running
#   ./tools/phase.sh list
#
# Each phase tag is a SELF-CONSISTENT snapshot: the code, the compose file and
# the docs all come from that moment. At phase-0 there is genuinely no Tempo,
# because the point of Phase 0 is reading raw OTLP before any UI exists.
set -euo pipefail

# ---------------------------------------------------------------------------
# This script only exists on `main`, so checking out a phase tag DELETES it
# mid-execution. A shell reading a script that vanishes underneath it is asking
# for trouble, so re-exec from a temporary copy before touching the tree, then
# reinstall at the end.
# ---------------------------------------------------------------------------
if [[ "${PHASE_SH_REEXEC:-}" != "1" ]]; then
  _root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "phase.sh must be run from inside the repository" >&2; exit 1; }
  _tmp="$(mktemp -t phase)"
  cp "${BASH_SOURCE[0]}" "$_tmp"
  chmod +x "$_tmp"
  PHASE_SH_REEXEC=1 PHASE_SH_ROOT="$_root" exec "$_tmp" "$@"
fi

ROOT="${PHASE_SH_ROOT:?}"
LAB="$ROOT/lab"
COMPOSE=(docker compose -f "$LAB/docker-compose.yml")
cd "$ROOT"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
dim()  { printf '\033[2m%s\033[0m\n' "$*"; }

# Keep the reinstalled copy invisible to git while sitting on a phase tag,
# where it is an untracked file. .git/info/exclude is local-only and survives
# checkouts, so it never becomes part of any commit.
_exclude="$ROOT/.git/info/exclude"
grep -qxF 'tools/phase.sh' "$_exclude" 2>/dev/null || echo 'tools/phase.sh' >> "$_exclude"

reinstall_self() {
  mkdir -p "$ROOT/tools"
  cp "$0" "$ROOT/tools/phase.sh"
  chmod +x "$ROOT/tools/phase.sh"
}

case "${1:-}" in
  status)
    bold "current: $(git describe --tags --exact-match 2>/dev/null || git rev-parse --abbrev-ref HEAD)"
    "${COMPOSE[@]}" ps --format '  {{.Service}}	{{.Status}}' 2>/dev/null || echo "  (nothing running)"
    exit 0
    ;;
  list|"" |-h|--help)
    bold "usage: ./tools/phase.sh <0-7|main|status|list>"
    git tag -n1 'phase-*' | sed 's/^/  /'
    exit 0
    ;;
esac

TARGET="$1"
if [[ "$TARGET" =~ ^[0-7]$ ]]; then REF="phase-$TARGET"; else REF="$TARGET"; fi

git rev-parse --verify --quiet "$REF" >/dev/null || {
  echo "unknown phase or ref: $REF" >&2; exit 1; }

# If a previous run reinstalled this script onto a phase tag, it is untracked
# there and would block `git checkout main`, which wants to create it. Remove
# it first — we are already running from the temp copy, so this is safe.
if ! git ls-files --error-unmatch tools/phase.sh >/dev/null 2>&1; then
  rm -f "$ROOT/tools/phase.sh"
fi

# lab/.env is a scratch file — you are MEANT to edit it during a phase, since
# the chaos knobs live there. Stash those edits rather than carrying them
# across, so a knob left on from one phase cannot distort the next one.
if ! git diff --quiet -- lab/.env 2>/dev/null; then
  git stash push -q -m "phase.sh: lab/.env" -- lab/.env
  dim "    stashed your lab/.env edits (recover with: git stash pop)"
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "You have uncommitted changes. Commit, stash, or discard them first:" >&2
  git status --short >&2
  reinstall_self
  exit 1
fi

bold "==> tearing down the current stack (containers AND volumes)"
dim  "    a fresh start per phase: Tempo or Prometheus data left over from a"
dim  "    later phase would otherwise appear as history that cannot exist yet"
"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true

bold "==> checking out $REF"
git checkout -q "$REF"
git --no-pager log -1 --format='    %h  %s' HEAD
reinstall_self

# Phase 0's lesson IS the flip: run with telemetry off, see nothing, turn it
# on, see everything. The committed .env has it already ON, because the file
# was captured after that demo had been run — so set the honest start here.
if [[ "$REF" == "phase-0" ]]; then
  sed -i.bak 's/^OTEL_ENABLED=true/OTEL_ENABLED=false/' "$LAB/.env"
  rm -f "$LAB/.env.bak"
fi

bold "==> building and starting"
"${COMPOSE[@]}" up -d --build

echo
bold "==> services now running"
"${COMPOSE[@]}" ps --format '    {{.Service}}	{{.Status}}'

echo
if [[ "$REF" == phase-* ]]; then
  N="${REF#phase-}"
  DOC="$(ls "$ROOT/docs/" 2>/dev/null | grep -E "^0${N}-" || true)"
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
  dim "    You are on a detached HEAD. That is fine for reading and running."
  dim "    To experiment:  git switch -c my-phase-$N"
  if [[ "$N" -lt 7 ]]; then
    dim "    When done:      ./tools/phase.sh $((N + 1))"
  else
    dim "    When done:      ./tools/phase.sh main"
  fi
fi
