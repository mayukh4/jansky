#!/bin/bash
# Start Jansky and its full-screen face. The desktop icon runs this; pressing it twice is harmless
# (run.sh and face.sh each hold a lock, so a second copy exits straight away).
# To get the normal desktop back: swipe down on the face and press "Close Jansky" (or run stop.sh).
APP="$(cd "$(dirname "$0")" && pwd)"
LOGS="$HOME/jansky-v2"
setsid nohup "$APP/run.sh" >> "$LOGS/jansky.log" 2>&1 < /dev/null &
setsid nohup "$APP/face.sh" >> "$LOGS/face.log" 2>&1 < /dev/null &
