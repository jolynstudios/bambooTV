#!/usr/bin/env bash
# Checks a running bambooTV end to end: TV page, snapshot, MJPEG, HLS, RTSP,
# the optional DLNA server (found the way a TV finds it), and that go2rtc's
# admin endpoints are not reachable.
#
#   dev/smoke-test.sh [http://HOST:1984] [RTSP_PORT] [DLNA_PORT]
#
# Needs curl, ffprobe and python3. Exits non-zero when a check fails.
set -uo pipefail

BASE=${1:-http://127.0.0.1:1984}
BASE=${BASE%/}
HOST=$(sed -E 's#^https?://([^/:]+).*#\1#' <<<"$BASE")
RTSP_PORT=${2:-8554}
DLNA_PORT=${3:-49499}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
FAILED=0

ok()   { printf 'OK    %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILED=1; }

# TV page (marker meta tag, so another web server on the port can't pass)
page=$(curl -fsS --max-time 5 "$BASE/")
if [[ $page == *'<meta name="application-name" content="bambooTV">'* ]]; then
  ok "tv page"
else
  fail "tv page ($BASE/)"
fi

# Snapshot (go2rtc decodes a keyframe with ffmpeg)
code=$(curl -s --max-time 25 -o "$TMP/snap.jpg" -w '%{http_code}' "$BASE/api/frame.jpeg?src=x2d")
if [[ $code == 200 ]] && [[ $(head -c 2 "$TMP/snap.jpg" | od -An -tx1 | tr -d ' \n') == ffd8 ]]; then
  ok "snapshot ($(wc -c <"$TMP/snap.jpg" | tr -d ' ') bytes)"
else
  fail "snapshot (HTTP $code: $(head -c 200 "$TMP/snap.jpg" 2>/dev/null))"
fi

# MJPEG: expect several frames within a few seconds
curl -s --max-time 8 -o "$TMP/stream.mjpeg" "$BASE/api/stream.mjpeg?src=x2d_mjpeg"
frames=$(grep -ac -- '--frame' "$TMP/stream.mjpeg" || true)
if ((frames >= 3)); then
  ok "mjpeg ($frames frames in 8 s)"
else
  fail "mjpeg (only $frames frames)"
fi

# HLS (fMP4 variant, as advertised to apps): let ffprobe follow the playlists
# like a player would
info=$(ffprobe -v error -rw_timeout 15000000 -select_streams v:0 -show_entries stream=codec_name,width,height \
  -of csv=p=0 "$BASE/api/stream.m3u8?src=x2d&mp4" 2>/dev/null | head -n 1)
if [[ $info =~ ,[1-9][0-9]*,[1-9] ]]; then
  ok "hls ($info)"
else
  fail "hls ($BASE/api/stream.m3u8?src=x2d&mp4: '$info')"
fi

# RTSP restream (also tells whether the optional radio is on)
streams=$(ffprobe -v error -rtsp_transport tcp -analyzeduration 10M -probesize 20M \
  -show_entries stream=codec_type,codec_name,width,height \
  -of csv=p=0 "rtsp://$HOST:$RTSP_PORT/x2d" 2>/dev/null)
info=$(grep -m 1 -E '^[a-z0-9]+,video,[1-9]' <<<"$streams" | cut -d, -f1,3,4)
if [[ -n $info ]]; then
  ok "rtsp ($info)"
else
  fail "rtsp (rtsp://$HOST:$RTSP_PORT/x2d)"
fi
if audio=$(grep -m 1 ',audio' <<<"$streams"); then
  ok "radio: on (${audio%%,*} audio in the stream)"
else
  ok "radio: off"
fi

# DLNA (optional). Discovered like a TV does it: multicast M-SEARCH, then the
# device description, a Browse of the root, and playing the item's own URL.
dlna=$(python3 -I - "$HOST" "$DLNA_PORT" <<'PY'
import re, socket, sys, time, urllib.request
host, port = sys.argv[1], int(sys.argv[2])
try:
    target = socket.gethostbyname(host)
except OSError:
    target = host
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(0.5)
s.sendto(b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\n'
         b'MX: 2\r\nST: urn:schemas-upnp-org:device:MediaServer:1\r\n\r\n', ("239.255.255.250", 1900))
location, end = "", time.time() + 4
while time.time() < end and not location:
    try:
        data, (ip, _) = s.recvfrom(4096)
    except socket.timeout:
        continue
    text = data.decode("utf-8", "replace")
    if ip == target or (target in ("127.0.0.1", "localhost") and ":%d/" % port in text):
        m = re.search(r"(?im)^location:\s*(\S+)", text)
        if m and "bambooTV" in urllib.request.urlopen(m.group(1), timeout=5).read().decode("utf-8", "replace"):
            location = m.group(1)
if not location:
    print("MISSING"); sys.exit()
base = location.rsplit("/", 1)[0]
body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        '<u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1"><ObjectID>0</ObjectID>'
        '<BrowseFlag>BrowseDirectChildren</BrowseFlag><Filter>*</Filter><StartingIndex>0</StartingIndex>'
        '<RequestedCount>0</RequestedCount><SortCriteria></SortCriteria></u:Browse></s:Body></s:Envelope>').encode()
req = urllib.request.Request(base + "/ctl/cds", data=body, headers={
    "Content-Type": 'text/xml; charset="utf-8"',
    "SOAPACTION": '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"'})
result = urllib.request.urlopen(req, timeout=5).read().decode("utf-8", "replace")
result = result.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
klass = re.search(r"<upnp:class>([^<]+)</upnp:class>", result)
res = re.search(r'<res protocolInfo="http-get:\*:video/mp2t:[^"]*">([^<]+)</res>', result)
print(location, klass.group(1) if klass else "-", res.group(1) if res else "-")
PY
)
if [[ $dlna == MISSING ]]; then
  if curl -s --max-time 2 -o /dev/null "http://$HOST:$DLNA_PORT/description.xml"; then
    fail "dlna: server on port $DLNA_PORT does not answer the multicast search (TVs won't find it)"
  else
    ok "dlna: not installed (skipped)"
  fi
else
  read -r dlna_loc dlna_class dlna_res <<<"$dlna"
  ok "dlna: found by multicast search ($dlna_loc)"
  if [[ $dlna_class == object.item.videoItem* && $dlna_res == http* ]]; then
    ok "dlna: one video item ($dlna_class)"
    info=$(ffprobe -v error -rw_timeout 15000000 -select_streams v:0 -show_entries stream=codec_name,width,height \
      -of csv=p=0 "$dlna_res" 2>/dev/null | head -n 1)
    if [[ $info =~ ,[1-9][0-9]*,[1-9] ]]; then ok "dlna: item plays ($info)"; else fail "dlna: item does not play ($dlna_res)"; fi
  else
    fail "dlna: no playable video item ($dlna_class $dlna_res)"
  fi
fi

# Admin API and config must not exist on this server
for path in /api /api/config /api/streams /api/restart /api/exit /api/log /api/ffmpeg /api/streams.dot /api/schemes; do
  code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "$BASE$path")
  if [[ $code == 404 ]]; then ok "closed: $path"; else fail "open: $path (HTTP $code)"; fi
done

# Scripted sources must be refused even when requested ad hoc
code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "$BASE/api/frame.jpeg?src=exec:id")
if [[ $code == 404 ]]; then ok "exec source refused"; else fail "exec source (HTTP $code)"; fi

exit $FAILED
