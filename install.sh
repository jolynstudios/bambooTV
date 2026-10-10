#!/usr/bin/env bash
# bambooTV installer: puts a 3D printer's camera on your home network and TVs.
#
#   sudo ./install.sh                                   # asks for IP and code
#   sudo ./install.sh --printer-ip 192.168.1.50 --with-dlna --radio on
#   sudo ./install.sh --uninstall [--purge]
#
# Installs go2rtc (pinned release, SHA-256 verified) as the systemd service
# "bambu-tv" together with the TV page and a locked-down configuration, the
# print status service "bambu-tv-status" (progress, temperatures and AMS on the
# TV page), optionally the built-in DLNA server "bambu-tv-dlna" so TVs list the
# stream under their connected devices, and an optional background radio station.
# Re-running it upgrades the files and keeps the saved settings. It only touches its own units, /opt/bambu-tv and
# /etc/bambu-tv.
set -euo pipefail

GO2RTC_VERSION=1.9.14
declare -A GO2RTC_SHA256=(
  [linux_amd64]=32d616af226bd731678ffde328b94cfb94e30339bfefc469cfb76323144615a6
  [linux_arm64]=359fabade8a7a51e81a55fe6df6b0ef81764a5e1d63179577534eaaa71904b50
  [linux_arm]=4d7e1639af5a2722a28e864468fd8099b3c1682565446c798bf9e3b38fde12e4
  [linux_armv6]=4dc20370556b29f3a90f4c7a09dcd95472c8f74cca56d4d1fb91f32bdd15174c
  [linux_i386]=12a114d19fc9fba1b3541cf7c6bb9b01896a6845f31285ec77269e2e7c613885
)

PREFIX=/opt/bambu-tv
CONF_DIR=/etc/bambu-tv
ENV_FILE=$CONF_DIR/bambu-tv.env
DLNA_ENV_FILE=$CONF_DIR/dlna.env
UNIT_FILE=/etc/systemd/system/bambu-tv.service
DLNA_UNIT_FILE=/etc/systemd/system/bambu-tv-dlna.service
STATUS_UNIT_FILE=/etc/systemd/system/bambu-tv-status.service
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DEFAULT_RADIO_URL=https://listen.181fm.com/181-power_128k.mp3  # 181.FM Power 181 (US Top 40)
PAGE_MARKER='<meta name="application-name" content="bambooTV">'

PRINTER_IP=${PRINTER_IP:-}
ACCESS_CODE=${ACCESS_CODE:-}
HTTP_PORT=${HTTP_PORT:-}
RTSP_PORT=${RTSP_PORT:-}
DLNA_PORT=${DLNA_PORT:-}
STATUS_PORT=${STATUS_PORT:-}
DLNA_IP=${DLNA_LAN_IP:-}
RADIO_URL=${RADIO_URL:-}
SKIP_FFMPEG=false
UNINSTALL=false
PURGE=false
WITH_DLNA=""     # "", true, false; "" = keep the current choice
RADIO=""         # "", on, off;      "" = keep the current choice
STATUS=""        # "", on, off;      "" = keep the current choice (on by default)
OVERLAY=""       # "", on, off;      "" = keep the current choice (on by default)

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'NOTE: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: sudo ./install.sh [options]

  --printer-ip IP        the printer's IP address
  --access-code CODE     the printer's LAN access code (leave it out and the
                         installer asks for it, so it stays out of your shell
                         history)
  --http-port PORT       port of the TV page (default 1984)
  --rtsp-port PORT       RTSP port (default 8554)
  --skip-ffmpeg          don't install ffmpeg (no MJPEG, snapshots, DLNA or radio)
  --with-dlna            also list the stream under TVs' connected devices (DLNA)
  --without-dlna         switch DLNA off again
  --lan-ip IP            advertise a fixed LAN IP for DLNA (default: automatic)
  --radio on|off         background radio on or off (default station:
                         181.FM Power 181, US Top 40)
  --radio-url URL        your own station (MP3/AAC stream URL); turns the radio on
  --status on|off        print progress, temperatures and AMS on the TV page
                         (default on)
  --status-port PORT     port of the print status service (default 1985)
  --overlay on|off       burn the print progress into the DLNA picture
                         (default on; off saves CPU on a Raspberry Pi)
  --uninstall            remove bambooTV
  --purge                with --uninstall: also remove the settings
  -h, --help             this help
EOF
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --printer-ip)   PRINTER_IP=${2:?}; shift 2 ;;
    --access-code)  ACCESS_CODE=${2:?}; shift 2 ;;
    --http-port)    HTTP_PORT=${2:?}; shift 2 ;;
    --rtsp-port)    RTSP_PORT=${2:?}; shift 2 ;;
    --skip-ffmpeg)  SKIP_FFMPEG=true; shift ;;
    --with-dlna)    WITH_DLNA=true; shift ;;
    --without-dlna) WITH_DLNA=false; shift ;;
    --lan-ip)       DLNA_IP=${2:?}; shift 2 ;;
    --radio)        RADIO=${2:?}; shift 2 ;;
    --radio-url)    RADIO_URL=${2:?}; RADIO=on; shift 2 ;;
    --status)        STATUS=${2:?}; shift 2 ;;
    --status-port)   STATUS_PORT=${2:?}; shift 2 ;;
    --overlay)       OVERLAY=${2:?}; shift 2 ;;
    --uninstall)    UNINSTALL=true; shift ;;
    --purge)        PURGE=true; shift ;;
    -h|--help)      usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run this script with sudo: sudo $0"
[[ -d /run/systemd/system ]] || die "systemd not found. Use the Docker setup instead (see README.md)."

# ------------------------------------------------------------------ uninstall

if $UNINSTALL; then
  step "Removing bambooTV"
  systemctl disable --now bambu-tv-dlna.service 2>/dev/null || true
  systemctl disable --now bambu-tv-status.service 2>/dev/null || true
  systemctl disable --now bambu-tv.service 2>/dev/null || true
  rm -f "$UNIT_FILE" "$DLNA_UNIT_FILE" "$STATUS_UNIT_FILE"
  systemctl daemon-reload
  rm -rf "$PREFIX"
  if $PURGE; then
    rm -rf "$CONF_DIR"
    say "Removed, including the settings."
  else
    say "Removed. Your settings are still in $CONF_DIR (remove them with --uninstall --purge)."
  fi
  exit 0
fi

# ------------------------------------------------------------------- settings

declare -A SAVED=()
if [[ -f $ENV_FILE ]]; then
  while IFS= read -r line || [[ -n $line ]]; do
    [[ $line =~ ^([A-Z_][A-Z0-9_]*)=(.*)$ ]] || continue
    value=${BASH_REMATCH[2]}
    value=${value#\"}; value=${value%\"}
    SAVED[${BASH_REMATCH[1]}]=$value
  done <"$ENV_FILE"
  PRINTER_IP=${PRINTER_IP:-${SAVED[PRINTER_IP]:-}}
  ACCESS_CODE=${ACCESS_CODE:-${SAVED[ACCESS_CODE]:-}}
  HTTP_PORT=${HTTP_PORT:-${SAVED[HTTP_PORT]:-}}
  RTSP_PORT=${RTSP_PORT:-${SAVED[RTSP_PORT]:-}}
  DLNA_PORT=${DLNA_PORT:-${SAVED[DLNA_PORT]:-}}
  STATUS_PORT=${STATUS_PORT:-${SAVED[STATUS_PORT]:-}}
  DLNA_IP=${DLNA_IP:-${SAVED[DLNA_LAN_IP]:-}}
  RADIO_URL=${RADIO_URL:-${SAVED[RADIO_URL]:-}}
fi

# A reinstall keeps the current DLNA choice unless one is given.
if [[ -z $WITH_DLNA ]]; then
  if [[ -f $DLNA_UNIT_FILE ]]; then WITH_DLNA=true; else WITH_DLNA=false; fi
fi
if [[ -z $RADIO ]]; then
  if [[ -f $PREFIX/radio.yaml ]]; then RADIO=on; else RADIO=off; fi
fi
[[ $RADIO == on || $RADIO == off ]] || die "--radio expects on or off"
# Print status is on unless it was switched off (saved as STATUS=off).
[[ -n $STATUS ]] || { [[ ${SAVED[STATUS]:-} == off ]] && STATUS=off || STATUS=on; }
[[ $STATUS == on || $STATUS == off ]] || die "--status expects on or off"
# The overlay is on unless it was switched off (saved as OVERLAY=off).
[[ -n $OVERLAY ]] || { [[ ${SAVED[OVERLAY]:-} == off ]] && OVERLAY=off || OVERLAY=on; }
[[ $OVERLAY == on || $OVERLAY == off ]] || die "--overlay expects on or off"
[[ $RADIO == off ]] || RADIO_URL=${RADIO_URL:-$DEFAULT_RADIO_URL}
# No spaces, quotes, '#' or '$': the URL ends up inside a go2rtc source string.
URL_RE='^https?://[A-Za-z0-9._~:/?&=%+,;@!-]+$'
[[ -z $RADIO_URL || $RADIO_URL =~ $URL_RE ]] || die "invalid --radio-url: $RADIO_URL"

if [[ -z $PRINTER_IP && -t 0 ]]; then
  read -rp "Printer IP address (printer: Settings → Network): " PRINTER_IP
fi
if [[ -z $ACCESS_CODE && -t 0 ]]; then
  read -rp "Printer access code (printer: Settings → Network): " ACCESS_CODE
fi

[[ -n $PRINTER_IP ]] || die "no printer IP given (--printer-ip)"
[[ -n $ACCESS_CODE ]] || die "no access code given (--access-code)"
[[ $PRINTER_IP =~ ^[A-Za-z0-9.-]+$ ]] || die "invalid printer address: $PRINTER_IP"
# The printer's certificate is self-signed; go2rtc only skips verification for
# IP addresses, so a hostname is turned into its IPv4 address here.
if [[ ! $PRINTER_IP =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
  resolved=$(getent ahostsv4 "$PRINTER_IP" 2>/dev/null | awk 'NR == 1 {print $1}' || true)
  [[ -n $resolved ]] || die "printer address $PRINTER_IP not found; use the IP address shown on the printer"
  warn "using $PRINTER_IP as IP address $resolved (bambooTV needs an IP address; give the printer a fixed IP)"
  PRINTER_IP=$resolved
fi
[[ $ACCESS_CODE =~ ^[A-Za-z0-9]{4,32}$ ]] || die "the access code may only contain letters and digits"
[[ -z $DLNA_IP || $DLNA_IP =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "invalid --lan-ip: $DLNA_IP"
# The services run without privileges, so ports below 1024 are not possible.
for port in "$HTTP_PORT" "$RTSP_PORT" "$DLNA_PORT" "$STATUS_PORT"; do
  [[ -z $port ]] || { [[ $port =~ ^[0-9]+$ ]] && ((port >= 1024 && port <= 65535)); } ||
    die "invalid port: $port (choose 1024 to 65535)"
done
PORT=${HTTP_PORT:-1984}
RPORT=${RTSP_PORT:-8554}
DPORT=${DLNA_PORT:-49499}
SPORT=${STATUS_PORT:-1985}

# --------------------------------------------------------------------- checks

step "Checking the printer at $PRINTER_IP"
if timeout 3 bash -c "exec 3<>/dev/tcp/$PRINTER_IP/322" 2>/dev/null; then
  say "Camera port 322 is reachable."
else
  warn "camera port 322 on $PRINTER_IP does not answer. Is the printer on, and is"
  warn "'LAN Only Liveview' switched on (printer: Settings → General)? Installing anyway;"
  warn "bambooTV keeps retrying automatically."
fi

case $(uname -m) in
  x86_64|amd64)          ASSET=linux_amd64 ;;
  aarch64|arm64)         ASSET=linux_arm64 ;;
  armv7*|armv8l)         ASSET=linux_arm ;;
  armv6*)                ASSET=linux_armv6 ;;
  i386|i486|i586|i686)   ASSET=linux_i386 ;;
  *) die "CPU architecture $(uname -m) is not supported" ;;
esac

fetch() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$2" "$1"
  else
    die "curl or wget is needed to download go2rtc"
  fi
}

http_get() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 2 "$1" 2>/dev/null
  else
    wget -q -O - --timeout=2 "$1" 2>/dev/null
  fi
}

# True when our own TV page answers (not some other server on that port).
tv_page_ok() {
  local page
  page=$(http_get "$1") || return 1
  [[ $page == *"$PAGE_MARKER"* ]]
}

# True when the print status service answers with its JSON.
status_ok() {
  local reply
  reply=$(http_get "http://127.0.0.1:$SPORT/status.json") || return 1
  [[ $reply == *'"feed":'* ]]
}

# True when the DLNA server answers and introduces itself as bambooTV.
dlna_ok() {
  local desc
  desc=$(http_get "http://127.0.0.1:$DPORT/description.xml") || return 1
  [[ $desc == *'<modelName>bambooTV</modelName>'* ]]
}

verified() { echo "${GO2RTC_SHA256[$ASSET]}  $1" | sha256sum -c --status - 2>/dev/null; }

install_pkg() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$1" >/dev/null
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q "$1"
  elif command -v pacman >/dev/null 2>&1; then
    pacman -S --noconfirm --needed "$1"
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install "$1"
  else
    return 1
  fi
}

# --------------------------------------------------------------- dependencies

if ! $SKIP_FFMPEG && ! command -v ffmpeg >/dev/null 2>&1; then
  step "Installing ffmpeg (for MJPEG, snapshots, DLNA and radio)"
  install_pkg ffmpeg || install_pkg ffmpeg-free || true
  command -v ffmpeg >/dev/null 2>&1 ||
    warn "ffmpeg is missing: the live stream works, but MJPEG, snapshots, DLNA and radio do not."
fi

if [[ $RADIO == on ]] && ! command -v ffmpeg >/dev/null 2>&1; then
  die "the radio needs ffmpeg; run the installer without --skip-ffmpeg"
fi

if [[ $WITH_DLNA == true || $STATUS == on ]] && ! command -v python3 >/dev/null 2>&1; then
  step "Installing python3 (for print status and DLNA)"
  install_pkg python3 || true
fi
if [[ $STATUS == on ]] && ! command -v python3 >/dev/null 2>&1; then
  warn "python3 is missing: no print status on the TV page (switch it off with --status off)."
  STATUS=off
fi
if [[ $WITH_DLNA == true ]]; then
  command -v python3 >/dev/null 2>&1 || die "DLNA needs python3, which could not be installed"
  command -v ffmpeg >/dev/null 2>&1 || die "DLNA needs ffmpeg; run the installer without --skip-ffmpeg"
fi

# --------------------------------------------------------------------- go2rtc

step "Installing go2rtc $GO2RTC_VERSION ($ASSET)"
install -d -m 0755 "$PREFIX"
if [[ -x $PREFIX/go2rtc ]] && verified "$PREFIX/go2rtc"; then
  say "Already present and verified."
else
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  fetch "https://github.com/AlexxIT/go2rtc/releases/download/v$GO2RTC_VERSION/go2rtc_$ASSET" "$TMP/go2rtc"
  verified "$TMP/go2rtc" || die "the go2rtc download failed its checksum; installation stopped"
  install -m 0755 "$TMP/go2rtc" "$PREFIX/go2rtc"
  say "Downloaded and checksum verified."
fi

step "Installing configuration and TV page"
install -m 0644 "$HERE/config/go2rtc.yaml" "$PREFIX/go2rtc.yaml"
rm -rf "$PREFIX/web"
cp -R "$HERE/web" "$PREFIX/web"
chmod -R u=rwX,go=rX "$PREFIX/web"
# The radio is a second go2rtc config file: present = on, absent = off.
if [[ $RADIO == on ]]; then
  install -m 0644 "$HERE/config/radio.yaml" "$PREFIX/radio.yaml"
else
  rm -f "$PREFIX/radio.yaml"
fi
# Leftovers of the former Gerbera-based DLNA add-on (our own files only).
rm -f "$PREFIX/gerbera.xml"
rm -rf "$PREFIX/import"

install -d -m 0700 "$CONF_DIR"
(
  umask 077
  {
    echo "# bambooTV settings. After editing: sudo systemctl restart bambu-tv bambu-tv-status bambu-tv-dlna"
    echo "PRINTER_IP=$PRINTER_IP"
    echo "ACCESS_CODE=$ACCESS_CODE"
    [[ -z $HTTP_PORT ]] || echo "HTTP_PORT=$HTTP_PORT"
    [[ -z $RTSP_PORT ]] || echo "RTSP_PORT=$RTSP_PORT"
    [[ -z $DLNA_PORT ]] || echo "DLNA_PORT=$DLNA_PORT"
    [[ -z $STATUS_PORT ]] || echo "STATUS_PORT=$STATUS_PORT"
    [[ $STATUS == on ]] || echo "STATUS=off"
    [[ -z $DLNA_IP ]] || echo "DLNA_LAN_IP=$DLNA_IP"
    [[ -z $RADIO_URL ]] || echo "RADIO_URL=$RADIO_URL"
    # keep anything else the user added (e.g. RTSP_USER / RTSP_PASS)
    if [[ -f $ENV_FILE ]]; then
      grep -Ev '^(#|PRINTER_IP=|ACCESS_CODE=|HTTP_PORT=|RTSP_PORT=|DLNA_PORT=|STATUS_PORT=|STATUS=|DLNA_LAN_IP=|RADIO_URL=)' "$ENV_FILE" |
        grep -E '^[A-Z_][A-Z0-9_]*=' || true
    fi
  } >"$ENV_FILE.new"
  mv "$ENV_FILE.new" "$ENV_FILE"
)
say "Settings saved in $ENV_FILE"

step "Starting the service"
install -m 0644 "$HERE/systemd/bambu-tv.service" "$UNIT_FILE"
systemctl daemon-reload
systemctl enable --quiet bambu-tv.service
systemctl restart bambu-tv.service

for _ in $(seq 1 20); do
  tv_page_ok "http://127.0.0.1:$PORT/" && break
  sleep 0.5
done
tv_page_ok "http://127.0.0.1:$PORT/" ||
  die "bambooTV does not answer on port $PORT. Is another program using it? Then pick another port with --http-port. Logs: journalctl -u bambu-tv -n 50"

HOST_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}' || true)
[[ -n $HOST_IP ]] || HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
[[ -n $HOST_IP ]] || HOST_IP="<this-computer-ip>"

# --------------------------------------------------------------------- status

if [[ $STATUS == on ]]; then
  step "Starting print status (progress, temperatures and AMS on the TV page)"
  install -d -m 0755 "$PREFIX/status"
  install -m 0644 "$HERE/status/bambu_tv_status.py" "$PREFIX/status/bambu_tv_status.py"
  install -m 0644 "$HERE/systemd/bambu-tv-status.service" "$STATUS_UNIT_FILE"
  systemctl daemon-reload
  systemctl enable --quiet bambu-tv-status.service
  systemctl restart bambu-tv-status.service
  for _ in $(seq 1 20); do
    status_ok && break
    sleep 0.5
  done
  status_ok || die "the print status service does not answer on port $SPORT. Is another program using it? Then pick another port with --status-port. Logs: journalctl -u bambu-tv-status -n 50"
elif [[ -f $STATUS_UNIT_FILE ]]; then
  step "Switching print status off"
  systemctl disable --now bambu-tv-status.service 2>/dev/null || true
  rm -f "$STATUS_UNIT_FILE"
  rm -rf "$PREFIX/status"
  systemctl daemon-reload
fi

# ----------------------------------------------------------------------- dlna

if [[ $WITH_DLNA == true ]]; then
  step "Starting DLNA (on the TV: Source → Connected devices → bambooTV)"
  install -d -m 0755 "$PREFIX/dlna" "$PREFIX/dlna/icons"
  install -m 0644 "$HERE/dlna/bambu_tv_dlna.py" "$PREFIX/dlna/bambu_tv_dlna.py"
  install -m 0644 "$HERE/dlna/card.png" "$PREFIX/dlna/card.png"
  install -m 0644 "$HERE"/dlna/icons/icon-* "$PREFIX/dlna/icons/"
  {
    echo "# bambooTV DLNA, written by install.sh (no secrets in here)"
    echo "HTTP_PORT=$PORT"
    echo "DLNA_PORT=$DPORT"
    # The overlay polls the status service (same port), which install.sh starts
    # whenever print status is on; STATUS_PORT only needs writing when moved.
    [[ $STATUS == on && $SPORT != 1985 ]] && echo "STATUS_PORT=$SPORT" || true
    [[ $OVERLAY == off ]] && echo "OVERLAY=off" || true
    [[ -z $DLNA_IP ]] || echo "DLNA_LAN_IP=$DLNA_IP"
  } >"$DLNA_ENV_FILE"
  chmod 0644 "$DLNA_ENV_FILE"
  # Replaces the unit of the former Gerbera add-on as well (same name).
  install -m 0644 "$HERE/systemd/bambu-tv-dlna.service" "$DLNA_UNIT_FILE"
  systemctl daemon-reload
  systemctl enable --quiet bambu-tv-dlna.service
  systemctl restart bambu-tv-dlna.service
  for _ in $(seq 1 20); do
    dlna_ok && break
    sleep 0.5
  done
  dlna_ok || die "the DLNA service does not answer on port $DPORT. Logs: journalctl -u bambu-tv-dlna -n 50"
  if command -v gerbera >/dev/null 2>&1; then
    say "Tip: gerbera is no longer used. Installed only for bambooTV? Then it can go: sudo apt remove gerbera"
  fi
elif [[ -f $DLNA_UNIT_FILE ]]; then
  step "Switching DLNA off"
  systemctl disable --now bambu-tv-dlna.service 2>/dev/null || true
  rm -f "$DLNA_UNIT_FILE" "$DLNA_ENV_FILE"
  rm -rf "$PREFIX/dlna"
  systemctl daemon-reload
fi

PAGE_QUERY=""
[[ $STATUS == off || $SPORT == 1985 ]] || PAGE_QUERY="?statusport=$SPORT"

cat <<EOF

bambooTV is running.

  On the TV:   open the web browser (Samsung: "Internet") and go to
               http://$HOST_IP:$PORT/$PAGE_QUERY

  Other apps (VLC, IPTV apps, Home Assistant):
    HLS       http://$HOST_IP:$PORT/api/stream.m3u8?src=tv&mp4
    RTSP      rtsp://$HOST_IP:$RPORT/x2d
    MJPEG     http://$HOST_IP:$PORT/api/stream.mjpeg?src=x2d_mjpeg
    Snapshot  http://$HOST_IP:$PORT/api/frame.jpeg?src=x2d

  Logs:        journalctl -u bambu-tv -f
  Settings:    $ENV_FILE
EOF

if [[ $WITH_DLNA == true ]]; then
  say ''
  say '  DLNA:        TV → Source → Connected devices → "bambooTV" → "X2D live"'
fi
if [[ $STATUS == on ]]; then
  say "  Print info:  on (progress, temperatures and AMS on the TV page)"
else
  say "  Print info:  off (switch on: sudo ./install.sh --status on)"
fi
if [[ $RADIO == on ]]; then
  say "  Radio:       on ($RADIO_URL)"
  say "               switch off: sudo ./install.sh --radio off"
else
  say "  Radio:       off (switch on: sudo ./install.sh --radio on)"
fi

FW_UFW="sudo ufw allow $PORT/tcp && sudo ufw allow $RPORT/tcp"
FW_FWCMD="--add-port=$PORT/tcp --add-port=$RPORT/tcp"
if [[ $STATUS == on ]]; then
  FW_UFW+=" && sudo ufw allow $SPORT/tcp"
  FW_FWCMD+=" --add-port=$SPORT/tcp"
fi
if [[ $WITH_DLNA == true ]]; then
  FW_UFW+=" && sudo ufw allow 1900/udp && sudo ufw allow $DPORT/tcp"
  FW_FWCMD+=" --add-port=1900/udp --add-port=$DPORT/tcp"
fi
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
  printf '\nThe ufw firewall is active. Open the ports with:\n  %s\n' "$FW_UFW"
elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  printf '\nThe firewalld firewall is active. Open the ports with:\n  sudo firewall-cmd --permanent %s && sudo firewall-cmd --reload\n' "$FW_FWCMD"
fi
