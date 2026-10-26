#!/usr/bin/env bash
# Jansky v2: one-script install for a Raspberry Pi 5 + AI HAT+ 2 + reSpeaker Flex XVF3800.
#
#   curl -fsSL https://raw.githubusercontent.com/mayukh4/jansky/main/install.sh | bash
#
# What it does, in order:
#   1. AI HAT+ 2 (Hailo-10H): HailoRT 5.3.0 runtime + PCIe driver from Hailo's public server. The driver
#      is patched to build on kernel 6.15+ (del_timer_sync was renamed). Packages are held so apt leaves them.
#   2. reSpeaker Flex XVF3800: flashes Seeed's USB firmware c48k2ch (pinned, sha256-checked), then records
#      a few seconds to prove the mics deliver sound.
#   3. The app: this repo in ~/jansky-v2/app, a Python venv, the models (~3.5 GB) in ~/jansky-v2/models,
#      a Jansky icon on the desktop, and autostart at login.
#
# Safe to run again: anything already done is detected and skipped.
#
# Options (environment variables, e.g. `curl ... | JANSKY_USER=Alex bash`):
#   JANSKY_USER=Alex     what Jansky calls you (written to ~/jansky-v2/.env on the first run)
#   AUTOSTART=0          don't start Jansky at login
#   SKIP_HAILO=1  SKIP_RESPEAKER=1  SKIP_APP=1   skip a step
#   SAFE_MODE_WAIT=180   seconds to wait for you to put the reSpeaker in Safe Mode
#
# Tested on: Raspberry Pi 5 8 GB, Raspberry Pi OS (Debian 13 trixie) 64-bit desktop, kernel 6.18 rpi-v8,
# reSpeaker Flex XVF3800 Circular-4 with XIAO ESP32S3.

set -euo pipefail

REPO_URL="https://github.com/mayukh4/jansky.git"
JANSKY_HOME="$HOME/jansky-v2"
APP_DIR="$JANSKY_HOME/app"
VENV="$JANSKY_HOME/venv"
MODELS="$JANSKY_HOME/models"
AUTOSTART="${AUTOSTART:-1}"
SAFE_MODE_WAIT="${SAFE_MODE_WAIT:-180}"

# Hailo's public files for HailoRT 5.3.0 (Hailo-10H).
HAILO_VER="5.3.0"
HAILO_DL="https://dev-public.hailo.ai/2026_04/Hailo10"
HAILO_DEBS=("hailort_${HAILO_VER}_arm64.deb" "hailort-pcie-driver_${HAILO_VER}_all.deb")
HAILO_WHEEL="hailort-${HAILO_VER}-cp313-cp313-linux_aarch64.whl"
HAILO_HEF="https://dev-public.hailo.ai/v${HAILO_VER}/blob"

# The app needs 48 kHz stereo from the reSpeaker (config.AUDIO_DEVICE = "C48K2Ch").
RESPEAKER_FW="c48k2ch"
RESPEAKER_CARD="C48K2Ch"
RESPEAKER_REPO_RAW="https://raw.githubusercontent.com/respeaker/reSpeaker_Flex/99a5047129c158beb655daf5f38b325ebef0974a"
RESPEAKER_FW_VERSION="v1.0.4"
RESPEAKER_SHA256="59d601a8848d479ef381c9c59d58e55de8933f9beb2b52b8e8ca3db743239b17"

# name|url|size in bytes. Downloads resume, and a file already at its full size is skipped.
MODEL_FILES=(
  "Qwen3-1.7B-Instruct.hef|$HAILO_HEF/Qwen3-1.7B-Instruct.hef|2880748478"
  "Whisper-Small.hef|$HAILO_HEF/Whisper-Small.hef|405920082"
  "kokoro-v1.0.int8.onnx|https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx|92361271"
  "voices-v1.0.bin|https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin|28214398"
  "silero_vad.onnx|https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/silero_vad.onnx|1807522"
)
KWS_MODEL="sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
KWS_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/$KWS_MODEL.tar.bz2"

HAILO10H_PCI_ID="1e60:45c4"   # Hailo-10H, the chip on the AI HAT+ 2
HAILO8_PCI_ID="1e60:2864"     # Hailo-8 / 8L, the older AI Kit and AI HAT+
SEEED_VID="2886"
RESPEAKER_SAFE_MODE_PID="801c"

TOOLS_DIR="$JANSKY_HOME/respeaker"
DL="$JANSKY_HOME/dl"
NEEDS_REBOOT=0
HAILO_RESULT="skipped"
RESPEAKER_RESULT="skipped"
APP_RESULT="skipped"

# ---------- output ------------------------------------------------------------

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m  %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m   %s\n' "$*"; }
die()  { printf '\n  \033[31mx\033[0m   %s\n\n' "$*" >&2; exit 1; }

if [[ $EUID -eq 0 ]]; then die "Run this as your normal user, not root. It uses sudo where it needs to."; fi

# ---------- preflight -----------------------------------------------------------

preflight() {
  bold "Checking this machine"

  local model
  model=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)
  [[ "$model" == *"Raspberry Pi 5"* ]] || die "This needs a Raspberry Pi 5 (found: $model). The AI HAT+ 2 only fits a Pi 5."
  ok "$model"

  [[ "$(uname -m)" == "aarch64" ]] || die "This needs 64-bit Raspberry Pi OS (found $(uname -m))."
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ "${VERSION_CODENAME:-}" == "trixie" ]] ||
    die "This needs Raspberry Pi OS trixie (Debian 13); found ${PRETTY_NAME:-unknown}. Hailo's 5.3 Python wheel is for Python 3.13."
  ok "$PRETTY_NAME, kernel $(uname -r)"

  local free_gb
  free_gb=$(df --output=avail -BG "$HOME" | tail -1 | tr -dc '0-9')
  if [[ "${SKIP_APP:-0}" != 1 && ! -f "$MODELS/Qwen3-1.7B-Instruct.hef" && "$free_gb" -lt 6 ]]; then
    die "Only ${free_gb} GB free. The models and Python packages need about 6 GB."
  fi
  ok "${free_gb} GB free"

  sudo true || die "This needs sudo."
  mkdir -p "$JANSKY_HOME" "$DL"

  bold "Installing base tools"
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    pciutils usbutils alsa-utils curl git dfu-util python3-venv python3-dev libportaudio2 libsndfile1 \
    >/dev/null </dev/null
  ok "base tools"
}

fetch() {  # fetch <url> <file> [size]: resumable, skipped when the file is already complete
  local url=$1 out=$2 size=${3:-}
  if [[ -f "$out" && ( -z "$size" || $(stat -c %s "$out") -eq "$size" ) ]]; then return 0; fi
  curl -fL --retry 3 -C - -o "$out.part" "$url" || { rm -f "$out.part"; curl -fL --retry 3 -o "$out.part" "$url"; }
  if [[ -n "$size" && $(stat -c %s "$out.part") -ne "$size" ]]; then die "Download of $(basename "$out") is incomplete. Run this again to resume."; fi
  mv "$out.part" "$out"
}

# ---------- 1. AI HAT+ 2 (Hailo-10H) --------------------------------------------

hailo_identify() { hailortcli fw-control identify 2>/dev/null | sed -n 's/^Device Architecture: //p' || true; }

install_hailo() {
  bold "1/3  Raspberry Pi AI HAT+ 2 (Hailo-10H), HailoRT $HAILO_VER"

  if lspci -d "$HAILO8_PCI_ID" 2>/dev/null | grep -q .; then
    die "Found a Hailo-8/8L (AI Kit or AI HAT+), not the AI HAT+ 2. Jansky's models need the Hailo-10H."
  fi
  if ! lspci -d "$HAILO10H_PCI_ID" 2>/dev/null | grep -q .; then
    warn "No Hailo-10H on PCIe. Check, with the Pi powered off:"
    note "- the flat PCIe ribbon is seated at both ends, contacts the right way round"
    note "- the HAT is pressed fully onto the GPIO header"
    die "Then run this script again."
  fi
  ok "Hailo-10H found on PCIe"

  local have
  have=$(dpkg-query -W -f='${Version}' hailort 2>/dev/null || true)
  if [[ "$have" == "$HAILO_VER" ]] && dpkg -s hailort-pcie-driver >/dev/null 2>&1; then
    ok "HailoRT $HAILO_VER already installed"
  else
    # Raspberry Pi's own packages are 5.1.1 (no Qwen3) and the Hailo-8 runtime is a different chip. Both
    # are removed with dpkg, never apt: apt would pull the Hailo-8 runtime back in through rpicam-apps.
    local old=()
    for p in hailo-h10-all hailo-gen-ai-model-zoo python3-h10-hailort h10-hailort h10-hailort-pcie-driver; do
      dpkg -s "$p" >/dev/null 2>&1 && old+=("$p")
    done
    if [[ -n "$have" && "$have" != "$HAILO_VER" ]]; then old+=(hailort); fi
    if (( ${#old[@]} )); then
      note "Replacing older Hailo packages: ${old[*]}"
      sudo dpkg --purge --force-depends "${old[@]}" >/dev/null
    fi

    # DKMS builds the driver, so it needs headers for the kernel this Pi boots.
    local flavour headers=(dkms)
    flavour=$(uname -r | sed -n 's/.*-\(rpi-[a-z0-9]*\)$/\1/p')
    [[ -n "$flavour" ]] && headers+=("linux-headers-$flavour")
    note "Installing ${headers[*]}..."
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${headers[@]}" >/dev/null </dev/null

    for f in "${HAILO_DEBS[@]}"; do fetch "$HAILO_DL/$f" "$DL/$f"; done
    note "Installing HailoRT $HAILO_VER..."
    # The driver's first build fails on kernel 6.15+; it is patched and rebuilt just below.
    sudo dpkg -i "$DL/${HAILO_DEBS[0]}" "$DL/${HAILO_DEBS[1]}" >"$DL/hailo_dpkg.log" 2>&1 || true
    local src
    for src in /usr/src/hailo1x_pci-"$HAILO_VER" /usr/src/hailort-pcie-driver; do
      [[ -d "$src" ]] && sudo grep -rl 'del_timer_sync(' "$src" 2>/dev/null |
        xargs -r sudo sed -i 's/del_timer_sync(/timer_delete_sync(/g'
    done
    sudo dkms build "hailo1x_pci/$HAILO_VER" >>"$DL/hailo_dpkg.log" 2>&1 || true
    sudo dkms install --force "hailo1x_pci/$HAILO_VER" >>"$DL/hailo_dpkg.log" 2>&1 || true
    sudo dpkg --configure -a >>"$DL/hailo_dpkg.log" 2>&1 ||
      die "The Hailo driver did not install. Log: $DL/hailo_dpkg.log"
    sudo apt-mark hold hailort hailort-pcie-driver >/dev/null
    ok "HailoRT $HAILO_VER installed and held (apt will leave it alone)"
  fi

  [[ -e /dev/h1x-0 ]] || sudo modprobe hailo1x_pci 2>/dev/null || true
  if [[ -e /dev/h1x-0 && "$(hailo_identify)" == "HAILO10H" ]]; then
    ok "The chip answers: HAILO10H, firmware $(hailortcli fw-control identify 2>/dev/null | sed -n 's/^Firmware Version: //p')"
    HAILO_RESULT="working"
  else
    note "Driver installed; it loads on the next boot."
    HAILO_RESULT="installed; reboot needed"
    NEEDS_REBOOT=1
  fi
}

# ---------- 2. reSpeaker Flex XVF3800 --------------------------------------------

respeaker_usb_line() { lsusb -d "$SEEED_VID:" 2>/dev/null | grep -v "$SEEED_VID:$RESPEAKER_SAFE_MODE_PID" | grep -i respeaker || true; }
respeaker_safe_mode() { lsusb -d "$SEEED_VID:$RESPEAKER_SAFE_MODE_PID" 2>/dev/null | grep -q .; }
respeaker_card() { arecord -l 2>/dev/null | sed -n 's/^card [0-9]*: \([^ ]*\) \[reSpeaker.*/\1/p' | head -1; }

fetch_respeaker_files() {
  mkdir -p "$TOOLS_DIR"
  local fw="respeaker_flex_usb_${RESPEAKER_FW}_${RESPEAKER_FW_VERSION}.bin"
  FW_FILE="$TOOLS_DIR/$fw"
  if ! echo "$RESPEAKER_SHA256  $FW_FILE" | sha256sum -c --status 2>/dev/null; then
    curl -fsSL -o "$FW_FILE" "$RESPEAKER_REPO_RAW/xmos_firmwares/usb/$RESPEAKER_FW_VERSION/$fw"
    echo "$RESPEAKER_SHA256  $FW_FILE" | sha256sum -c --status ||
      die "Firmware checksum mismatch for $fw. Not flashing a file that does not match."
  fi
  # Seeed's control tool, for routing and direction of arrival.
  [[ -f "$TOOLS_DIR/xvf_host.py" ]] || curl -fsSL -o "$TOOLS_DIR/xvf_host.py" "$RESPEAKER_REPO_RAW/python_control/xvf_host.py"
  ok "firmware $fw (sha256 verified)"
}

# Lets dfu-util and Jansky (direction of arrival) talk to the board without sudo.
install_respeaker_udev() {
  local rule=/etc/udev/rules.d/99-respeaker-flex.rules
  if [[ ! -f "$rule" ]]; then
    echo "SUBSYSTEM==\"usb\", ATTR{idVendor}==\"$SEEED_VID\", MODE=\"0660\", GROUP=\"plugdev\"" |
      sudo tee "$rule" >/dev/null
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=usb
  fi
  id -nG | grep -qw plugdev || { sudo usermod -aG plugdev "$USER"; NEEDS_REBOOT=1; }
}

verify_respeaker_audio() {
  local card wav=/tmp/respeaker_check.wav
  for _ in $(seq 1 15); do card=$(respeaker_card); [[ -n "$card" ]] && break; sleep 1; done
  if [[ -z "$card" ]]; then
    warn "On USB, but no sound card appeared. Unplug it, plug it back in, run this again."
    RESPEAKER_RESULT="on USB, no sound card"
    return
  fi
  if [[ "$card" != "$RESPEAKER_CARD" ]]; then
    warn "The reSpeaker runs other USB firmware ($card). Jansky needs $RESPEAKER_CARD."
    note "Hold its Boot button while plugging it in (Safe Mode), then run this again to reflash."
    RESPEAKER_RESULT="wrong firmware ($card)"
    return
  fi
  if ! arecord -q -D "plughw:CARD=$card,DEV=0" -c 2 -r 48000 -f S16_LE -d 3 "$wav"; then
    warn "Could not record from $card (is Jansky already running? Close it and run this again)."
    RESPEAKER_RESULT="$card, recording failed"
    return
  fi
  local peak
  peak=$(python3 - "$wav" <<'PY'
import sys, wave, array
w = wave.open(sys.argv[1]); a = array.array("h", w.readframes(w.getnframes()))
print(max((abs(v) for v in a), default=0))
PY
) || peak=0
  if [[ "${peak:-0}" -eq 0 ]]; then
    warn "$card records exact zeros, so no mic data is reaching the chip."
    warn "Real mics always pick up some noise. This is almost always the mic ribbon cable:"
    note "unplug USB, open the latch on the connector marked MIC, push the ribbon fully in"
    note "(bare contacts toward the connector's contacts), close the latch, plug back in."
    RESPEAKER_RESULT="$card, but mics silent (check ribbon)"
  else
    ok "Recording works: $card, 48 kHz stereo, peak $peak"
    RESPEAKER_RESULT="working"
  fi
}

install_respeaker() {
  bold "2/3  reSpeaker Flex XVF3800 mic array"
  fetch_respeaker_files
  install_respeaker_udev

  local line
  line=$(respeaker_usb_line)
  if [[ -n "$line" && "$(respeaker_card)" == "$RESPEAKER_CARD" ]]; then
    ok "Already on USB firmware $RESPEAKER_FW"
    verify_respeaker_audio
    return
  fi

  if ! respeaker_safe_mode; then
    note "Put the reSpeaker in Safe Mode so its USB firmware can be flashed:"
    note ""
    note "  1. Its own USB-C port (next to the RST button) goes to a USB-A port on the Pi."
    note "  2. Unplug that cable."
    note "  3. Hold the Boot button, plug the cable back in, release after 2 seconds."
    note ""
    note "Waiting up to ${SAFE_MODE_WAIT}s (Ctrl+C to stop; run this again later)..."
    local waited=0
    while ! respeaker_safe_mode; do
      (( waited >= SAFE_MODE_WAIT )) && {
        warn "No reSpeaker in Safe Mode. Run this again when it is plugged in (or SKIP_RESPEAKER=1)."
        RESPEAKER_RESULT="not detected"
        return
      }
      sleep 2; waited=$((waited + 2))
    done
  fi
  ok "Safe Mode detected"

  # alt 1 is the Upgrade slot. alt 0 (Factory) is never touched, so Safe Mode always stays a way back.
  note "Flashing $RESPEAKER_FW to the Upgrade slot (about 30 seconds)..."
  sudo dfu-util -R -e -a 1 -D "$FW_FILE" >/tmp/respeaker_dfu.log 2>&1 ||
    die "Flash failed; log in /tmp/respeaker_dfu.log. Re-enter Safe Mode and run this again."
  ok "Flashed"

  for _ in $(seq 1 20); do [[ -n "$(respeaker_usb_line)" ]] && break; sleep 1; done
  [[ -n "$(respeaker_usb_line)" ]] || { warn "Did not come back on USB. Unplug and replug it, then run this again."; RESPEAKER_RESULT="flashed; replug needed"; return; }
  verify_respeaker_audio
}

# ---------- 3. The app -----------------------------------------------------------

install_app() {
  bold "3/3  Jansky"

  if [[ -d "$APP_DIR/.git" ]]; then
    git -C "$APP_DIR" pull --ff-only -q && ok "app updated in $APP_DIR"
  elif [[ -e "$APP_DIR" ]]; then
    ok "app already in $APP_DIR (not a git clone; left as it is)"
  else
    git clone -q "$REPO_URL" "$APP_DIR" && ok "app cloned to $APP_DIR"
  fi

  if [[ ! -x "$VENV/bin/python" ]]; then
    python3 -m venv --system-site-packages "$VENV"
  fi
  fetch "$HAILO_DL/$HAILO_WHEEL" "$DL/$HAILO_WHEEL"
  note "Installing Python packages (a few minutes)..."
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q "$DL/$HAILO_WHEEL" -r "$APP_DIR/requirements.txt"
  ok "Python packages in $VENV"

  mkdir -p "$MODELS/kws"
  local entry name url size
  for entry in "${MODEL_FILES[@]}"; do
    IFS='|' read -r name url size <<<"$entry"
    if [[ -f "$MODELS/$name" && $(stat -c %s "$MODELS/$name") -eq "$size" ]]; then continue; fi
    note "Downloading $name ($((size / 1000000)) MB)..."
    fetch "$url" "$MODELS/$name" "$size"
  done
  if [[ ! -d "$MODELS/kws/$KWS_MODEL" ]]; then
    fetch "$KWS_URL" "$DL/$KWS_MODEL.tar.bz2"
    tar -xjf "$DL/$KWS_MODEL.tar.bz2" -C "$MODELS/kws"
  fi
  ok "models in $MODELS"

  if [[ ! -f "$JANSKY_HOME/.env" ]]; then
    cat > "$JANSKY_HOME/.env" <<EOF
# Jansky settings. Edit, then restart Jansky.
JANSKY_USER=${JANSKY_USER:-friend}
# For the weather:
HOME_CITY=Kingston
HOME_LAT=44.2312
HOME_LON=-76.4860
# Home Assistant (optional): Profile -> Security -> Long-lived access tokens
# HA_URL=http://homeassistant.local:8123
# HA_TOKEN=
EOF
    chmod 600 "$JANSKY_HOME/.env"
    ok "settings in $JANSKY_HOME/.env (set your city there)"
  fi

  chmod +x "$APP_DIR"/*.sh
  local desktop="[Desktop Entry]
Type=Application
Name=Jansky
Comment=Local voice AI agent
Exec=$APP_DIR/jansky.sh
Icon=$APP_DIR/jansky/face/icon.svg
Terminal=false"
  mkdir -p "$HOME/Desktop" "$HOME/.local/share/applications"
  echo "$desktop" > "$HOME/Desktop/jansky.desktop"
  echo "$desktop" > "$HOME/.local/share/applications/jansky.desktop"
  chmod +x "$HOME/Desktop/jansky.desktop"
  ok "Jansky icon on the desktop and in the menu"

  if [[ "$AUTOSTART" == 1 ]]; then
    mkdir -p "$HOME/.config/autostart"
    echo "$desktop" > "$HOME/.config/autostart/jansky.desktop"
    ok "starts at login (AUTOSTART=0 to turn that off)"
  else
    rm -f "$HOME/.config/autostart/jansky.desktop"
  fi
  APP_RESULT="installed"
}

# ---------- run -----------------------------------------------------------------

# Everything runs from main, called on the last line, so that under `curl ... | bash`
# nothing executes until the whole file has arrived.
main() {
  preflight
  [[ "${SKIP_HAILO:-0}" == 1 ]] || install_hailo
  [[ "${SKIP_RESPEAKER:-0}" == 1 ]] || install_respeaker
  [[ "${SKIP_APP:-0}" == 1 ]] || install_app

  bold "Done"
  note "AI HAT+ 2:  $HAILO_RESULT"
  note "reSpeaker:  $RESPEAKER_RESULT"
  note "Jansky:     $APP_RESULT"
  [[ -f /var/run/reboot-required ]] && NEEDS_REBOOT=1
  if [[ $NEEDS_REBOOT == 1 ]]; then
    echo
    warn "Reboot to finish: sudo reboot. Jansky starts by itself after that."
  else
    note "Tap the Jansky icon, or reboot, and say \"Hey Jansky\"."
  fi
  echo
}

main "$@"
