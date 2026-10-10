#!/usr/bin/env bash
# Simulates an X2D so bambooTV can be tested without a printer: the camera
# (RTSP over TLS on port 322 with a self-signed certificate, path
# /streaming/live/1, user "bblp", a 1080p30 H.264 test pattern) and the status
# feed (MQTT over TLS on port 8883, see dev/fake-mqtt.py).
#
#   dev/fake-printer.sh [ACCESS_CODE] [fake-mqtt.py options]   # default a1b2c3d4, Ctrl-C stops
#   dev/fake-printer.sh a1b2c3d4 --state PAUSE --percent 62
#
# Needs mediamtx, ffmpeg, openssl and python3 on PATH. Port 322 is privileged
# on Linux: run with sudo there (macOS allows it unprivileged).
set -euo pipefail

CODE=${1:-a1b2c3d4}
shift || true
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORK=$(mktemp -d)
trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$WORK"' EXIT

# Like a real printer, the certificate's common name is the serial number.
openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=FAKEX2D00000001" \
  -keyout "$WORK/server.key" -out "$WORK/server.crt" 2>/dev/null

python3 -I "$HERE/fake-mqtt.py" --cert "$WORK/server.crt" --key "$WORK/server.key" --code "$CODE" "$@" &

cat >"$WORK/mediamtx.yml" <<EOF
logLevel: warn
authMethod: internal
authInternalUsers:
  - user: any
    ips: ["127.0.0.1", "::1"]
    permissions: [{action: publish}]
  - user: bblp
    pass: "$CODE"
    permissions: [{action: read}]
rtsp: true
rtspTransports: [tcp]
rtspEncryption: "optional"
rtspAddress: 127.0.0.1:18554
rtspsAddress: :322
rtspServerKey: $WORK/server.key
rtspServerCert: $WORK/server.crt
rtspAuthMethods: [basic, digest]
rtmp: false
hls: false
webrtc: false
srt: false
moq: false
paths:
  streaming/live/1:
    source: publisher
EOF

(cd "$WORK" && exec mediamtx mediamtx.yml) &
sleep 1

echo "fake printer: rtsps://bblp:$CODE@<this-host>:322/streaming/live/1, status feed on port 8883"
ffmpeg -hide_banner -loglevel error -re \
  -f lavfi -i "testsrc2=size=1920x1080:rate=30" \
  -c:v libx264 -preset veryfast -tune zerolatency -profile:v high -pix_fmt yuv420p -g 60 \
  -f rtsp -rtsp_transport tcp "rtsp://127.0.0.1:18554/streaming/live/1"
