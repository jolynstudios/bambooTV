#!/usr/bin/env bash
# Simulates the X2D camera so bambooTV can be tested without a printer:
# RTSP over TLS on port 322 with a self-signed certificate, path
# /streaming/live/1, user "bblp", serving a 1080p30 H.264 test pattern.
#
#   dev/fake-printer.sh [ACCESS_CODE]      # default a1b2c3d4, Ctrl-C stops
#
# Needs mediamtx, ffmpeg and openssl on PATH. Port 322 is privileged on Linux:
# run with sudo there (macOS allows it unprivileged).
set -euo pipefail

CODE=${1:-a1b2c3d4}
WORK=$(mktemp -d)
trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$WORK"' EXIT

openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=fake-x2d" \
  -keyout "$WORK/server.key" -out "$WORK/server.crt" 2>/dev/null

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

echo "fake printer: rtsps://bblp:$CODE@<this-host>:322/streaming/live/1"
ffmpeg -hide_banner -loglevel error -re \
  -f lavfi -i "testsrc2=size=1920x1080:rate=30" \
  -c:v libx264 -preset veryfast -tune zerolatency -profile:v high -pix_fmt yuv420p -g 60 \
  -f rtsp -rtsp_transport tcp "rtsp://127.0.0.1:18554/streaming/live/1"
