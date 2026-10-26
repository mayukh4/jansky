#!/bin/bash
# Start Jansky v2 on the Pi. Extra arguments go to jansky.main (e.g. --inject clip.wav).
cd "$(dirname "$0")"
# One Jansky at a time: the HAT and the mic can only have one owner. The lock goes when Jansky exits.
exec 9> /tmp/jansky.lock
flock -n 9 || { echo '{"event": "already_running"}'; exit 0; }
export XDG_RUNTIME_DIR=/run/user/$(id -u)
exec ~/jansky-v2/venv/bin/python -u -m jansky.main "$@" 2> >(grep -v --line-buffered '\[warning\]' >&2)
