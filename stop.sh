#!/bin/bash
# Stop Jansky. On SIGTERM it stops its HAT process and closes its face itself; the pkill below is a backstop.
# Same as "Close Jansky" on the screen.
pkill -TERM -f "jansky\.main"
sleep 3
pkill -f -- "--user-data-dir=.*jansky-face"
exit 0
