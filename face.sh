#!/bin/bash
# Show Jansky's face full screen on the Pi's panel (Chromium kiosk on Wayland).
# Way out: Alt+F4 on a keyboard, Ctrl+Alt+F2 for a console, or over SSH: pkill -x chromium
export XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0
exec 9> /tmp/jansky-face.lock
flock -n 9 || exit 0  # the face is already up
for i in $(seq 1 90); do curl -fs -o /dev/null http://localhost:8765/ && break; sleep 1; done
exec chromium --kiosk --ozone-platform=wayland --noerrdialogs --disable-infobars \
  --disable-session-crashed-bubble --no-first-run --password-store=basic \
  --check-for-update-interval=31536000 --user-data-dir="$HOME/.config/jansky-face" \
  --app=http://localhost:8765/
