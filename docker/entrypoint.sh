#!/bin/sh
# Runs Syncalist as PUID:PGID (Unraid convention: 99:100 = nobody:users) so files
# in /config are owned by the right user on the host.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$SYNCALIST_DATA"
    chown -R "$PUID:$PGID" "$SYNCALIST_DATA"
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups --inh-caps=-all \
        env HOME=/tmp "$@"
fi

# Already started as a non-root user (e.g. docker run --user): run as-is.
exec "$@"
