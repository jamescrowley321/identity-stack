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
# script under `make`). A caller that took the lock with flock(1) itself
# (`flock ~/.cache/identity-stack-compose.lock make test-integration`) passes
# its descriptor on the lock file down; on Linux this script finds it in
# /dev/fd and takes the exclusive lock through it, which succeeds at once when
# that descriptor is the one holding the lock. An open descriptor alone is never
# trusted: whichever descriptor the lock is taken on, the command runs only
# after flock reports it held. Other systems cannot see which file a descriptor
# names: wrap with this script instead
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

# Candidate descriptors: every inherited descriptor open on the lock file
# (Linux only; see above), then a new open of it. Descriptors are listed before
# the new one is opened, on a number no inherited descriptor uses.
inherited=()
if [ "$(uname -s)" = Linux ]; then
  lock_path="$(readlink -f "$lock")"
  for fd_path in /dev/fd/*; do
    fd="${fd_path##*/}"
    if [ "$(readlink -f "$fd_path" 2>/dev/null)" = "$lock_path" ]; then
      inherited+=("$fd")
    fi
  done
fi
own_fd=""
for fd in 9 8 7 6 5 4 3; do
  if [ ! -e "/dev/fd/$fd" ]; then
    own_fd="$fd"
    break
  fi
done
if [ -z "$own_fd" ]; then
  echo "no free descriptor (3-9) to open $lock on" >&2
  exit 1
fi
eval "exec $own_fd>>\"\$lock\""

# Take the exclusive lock, without waiting, on each candidate in turn. On an
# inherited descriptor it succeeds only if that descriptor's open file already
# holds the lock (flock(2) converts its own lock) or no one holds it; on the new
# descriptor only if no one holds it. Either way the lock is then held, through
# a descriptor the command keeps open. Until one succeeds, someone else holds
# it: poll, rather than block on one descriptor, so a caller's lock on another
# candidate can never deadlock this run.
take() {
  local fd
  for fd in ${inherited[@]+"${inherited[@]}"} "$own_fd"; do
    if flock -n -x "$fd"; then
      held_fd="$fd"
      return 0
    fi
  done
  return 1
}

held_fd=""
if ! take; then
  echo "--- compose lock: waiting for $lock (held by another run or session)" >&2
  if [ "$(uname -s)" != Linux ]; then
    echo "    if you took this lock yourself with flock, this waits forever: stop it and run" >&2
    echo "    under scripts/with-compose-lock.sh, or set IDENTITY_STACK_COMPOSE_LOCK=$lock" >&2
  fi
  until take; do
    sleep 1
  done
fi
if [ "$held_fd" = "$own_fd" ]; then
  echo "--- compose lock: held" >&2
else
  eval "exec $own_fd>&-"
  echo "--- compose lock: held through inherited descriptor $held_fd" >&2
fi
exec "$@"
