#!/usr/bin/env bash
# Run a command while holding the shared compose lock,
# ${XDG_CACHE_HOME:-~/.cache}/identity-stack-compose.lock.
#
# Every Makefile target that starts, uses or stops the docker-compose.test.yml
# stack, and infra/keycloak/scripts/check-against-compose.sh, runs through this
# script, so two of them (or two sessions on one Docker host) never bring the
# stack up or down under each other.
#
# Re-entrant. The command runs with IDENTITY_STACK_COMPOSE_LOCK set to the lock
# path, and a nested call that sees it runs its command at once: the lock is
# already held by an ancestor (a target that calls another target, or the check
# script under `make`). On Linux a caller that took the lock with flock(1)
# itself (`flock ~/.cache/identity-stack-compose.lock make test-integration`) is
# also recognised, by the descriptor on the lock file it passes down. Other
# systems cannot see that descriptor: wrap with this script instead
# (`scripts/with-compose-lock.sh make test-integration`), or set
# IDENTITY_STACK_COMPOSE_LOCK to the lock path under your own flock.
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "usage: $0 <command> [argument ...]" >&2
  exit 2
fi
if ! command -v flock >/dev/null; then
  echo "missing prerequisite: flock (util-linux; on macOS: brew install flock)" >&2
  exit 1
fi

cache="${XDG_CACHE_HOME:-$HOME/.cache}"
lock="$cache/identity-stack-compose.lock"

if [ "${IDENTITY_STACK_COMPOSE_LOCK:-}" = "$lock" ]; then
  exec "$@"
fi

mkdir -p "$cache"
touch "$lock"
export IDENTITY_STACK_COMPOSE_LOCK="$lock"

# flock(1) passes its open descriptor on the lock file to its command; a new
# open of the same file would wait on that caller forever. Linux names the file
# behind each descriptor in /dev/fd, so a descriptor already open on the lock
# file means the caller holds it: run the command with that descriptor still
# open (it keeps the lock) and without taking it again.
if [ "$(uname -s)" = Linux ]; then
  lock_path="$(readlink -f "$lock")"
  for fd_path in /dev/fd/*; do
    if [ "$(readlink -f "$fd_path" 2>/dev/null)" = "$lock_path" ]; then
      echo "--- compose lock: held by the caller (descriptor ${fd_path##*/})" >&2
      exec "$@"
    fi
  done
fi

exec 9>>"$lock"
if ! flock -n 9; then
  echo "--- compose lock: waiting for $lock (held by another run or session)" >&2
  if [ "$(uname -s)" != Linux ]; then
    echo "    if you took this lock yourself with flock, this waits forever: stop it and run" >&2
    echo "    under scripts/with-compose-lock.sh, or set IDENTITY_STACK_COMPOSE_LOCK=$lock" >&2
  fi
  flock 9
fi
echo "--- compose lock: held" >&2
exec "$@"
