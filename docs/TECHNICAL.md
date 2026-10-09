# bambooTV — technical overview

## Architecture

```
printer camera ──RTSPS :322──▶ go2rtc (service bambu-tv) ──┬─ :1984 TV page, MSE (WebSocket), HLS, MJPEG, snapshot, MP4
 (one connection, only                 ▲                    └─ :8554 RTSP restream
  while someone watches)               │
radio station ──HTTP(S)──▶ ffmpeg ─────┘ (optional second source, AAC)

TV "connected devices" ◀── SSDP :1900 / HTTP :49499 ── bambu_tv_dlna.py (service bambu-tv-dlna)
                                                        └─ per viewer: ffmpeg remuxes go2rtc's MP4 → MPEG-TS
```

- **go2rtc 1.9.14** does all streaming. The installer pins the release and
  checks its SHA-256 per architecture (amd64, arm64, arm, armv6, i386).
- **On demand.** go2rtc connects to the printer when the first viewer arrives
  and disconnects shortly after the last one leaves. Every viewer shares that
  single connection. If the printer drops, go2rtc reconnects (1 s → 5 s → 10 s →
  every minute) while viewers stay attached.
- **The TV page** (`web/index.html`) is served by go2rtc from its static
  directory and replaces go2rtc's admin UI.
- **The DLNA server** (`dlna/bambu_tv_dlna.py`) is a small, standard-library
  Python UPnP MediaServer with exactly one item.

## Printer camera protocol (X2D, firmware 01.02.00.00)

Verified against a real X2D:

- The camera is RTSP over TLS on **port 322**, served by a LIVE555 server with a
  self-signed certificate (issuer `BBL Device CA N6-V2`). Login is digest auth,
  user `bblp`, password = the LAN access code.
- **Path `/streaming/live/1`** answers `200 OK` with an H.264 SDP after
  authentication. Requests whose URI scheme is not `rtsps://` get
  `301 Moved Permanently` to `rtsps://<ip>/1`, and `/1` answers
  `404 Stream Not Found` after authentication. go2rtc's `rtspx://`
  pseudo-scheme triggers exactly that, so the config uses `rtsps://`.
- With `rtsps://`, go2rtc skips certificate verification **only for IP
  addresses**, so `PRINTER_IP` must be an IP. The installer resolves a hostname
  to its IPv4 address.
- Port 322 only opens when **LAN Only Liveview** is on (*Settings → General*).
  With it off, port 322 refuses connections; port 6000 (the JPEG protocol of
  the A1/P1 series) does not deliver JPEG frames on the X2D.
- The stream is H.264, 1920×1440 or 1920×1080 at 30 fps, without audio.

## TV page

| Mode | Technique | For |
|---|---|---|
| MSE | fragmented MP4 over go2rtc's WebSocket into MediaSource | Samsung/Tizen, Chrome, Edge, Firefox, Android |
| HLS | native HLS, fMP4 variant (`&mp4`) | Safari / iOS |
| MJPEG | multipart JPEG in an `<img>` (720p, 10 fps, ffmpeg) | anything else |

- **Fallback** to the next mode only when the server delivers video that the
  browser cannot show: unsupported codec, decode error, or video arriving
  without a picture after 20 s. For HLS errors before any metadata the page
  asks the server whether it can deliver; if so, the browser is the problem.
  When the server delivers nothing (printer off, wrong code), the page retries
  the same mode after 2, 5, 10, 20, then every 30 s.
- **Watchdog:** decoded-frame counters (not `currentTime`, which the
  live-edge seeking moves) detect stalls after 12 s.
- **Messages:** go2rtc's error text is mapped to one concrete fix: connection
  refused → LAN Only Liveview, timeout → printer/IP, `wrong user/pass` →
  access code, codec mismatch → choose MJPEG, WebSocket loss → service down.
- **Sound:** the stream starts muted (browser autoplay rules). The menu's Sound
  button unmutes inside a user gesture; the choice is kept in `localStorage`
  and re-applied on the next remote press.
- **OLED care:** the channel bug fades after 5 s; the offline slate drifts
  every 60 s and dims after 10 minutes.
- **Compatibility:** plain ES2015, no modules or build step, so older Tizen
  browsers run it. Fonts are bundled; nothing loads from the internet.

## DLNA server

- **Discovery:** binds UDP 1900 and joins the SSDP group **on every network
  interface** (Linux `ip_mreqn` per interface index), skipping container
  bridges and tunnels. Machines with two NICs on one LAN (Wi-Fi + Ethernet)
  are the common case where single-interface servers stay invisible. Answers
  M-SEARCH with a `LOCATION` routable to the asking TV; announces itself every
  10 minutes and says goodbye on shutdown. Only answers private addresses.
- **Content:** one `object.item.videoItem`, "X2D live", with two resources
  (`video/mp2t` and `video/mpeg`, same stream), DLNA flags for live streaming,
  and a live thumbnail (160 px snapshot via go2rtc, logo as fallback). Supports
  Browse, Search, Samsung's `X_GetFeatureList`, ConnectionManager and the
  MediaReceiverRegistrar; unknown actions get UPnP error 401.
- **Playback:** per viewer, ffmpeg remuxes go2rtc's MP4 output to MPEG-TS
  (`-c copy`). MP4 rather than RTSP because go2rtc's MP4 muxer keeps camera
  video and radio audio on one timeline; at most 6 concurrent viewers.
- **Icon:** PNG/JPEG 48/120/256 px in `dlna/icons/`, rendered from
  `web/icon.svg`.

## Radio

- `config/radio.yaml` is a second go2rtc config file that redefines the `x2d`
  stream with two sources: the printer and `ffmpeg:${RADIO_URL}#audio=aac`.
  The service always passes it; go2rtc skips a missing file, so the file's
  presence is the on/off switch.
- Consumers that only need video (snapshot, MJPEG) never start the radio. If
  the station is unreachable, consumers get video without audio.
- go2rtc's MPEG-TS **HLS** variant does not repeat the codec setup when an
  audio track is present, so the app-facing HLS URL is the fMP4 variant
  (`&mp4`), which works with and without radio.
- Free stations insert their own ads, which may be geo-targeted.

## Security

Open on the home network by design: the TV page, the streams and the DLNA
server (a TV cannot log in). Everything else is closed:

- **API allow-list:** only the static page, `/api/ws`, HLS, MP4, MJPEG and
  snapshot endpoints are registered. go2rtc's config editor, stream management,
  restart/exit and logs endpoints don't exist on this server (404; the smoke
  test checks them).
- **Modules:** only `api ws rtsp mp4 hls mjpeg ffmpeg exec` load. `echo`/`expr`
  scripting, WebRTC, HomeKit and tunnels are off. `exec` may only start
  `ffmpeg`, and go2rtc refuses `exec:`/`echo:`/`expr:` sources requested through
  the API.
- **go2rtc 1.9.14** contains the fixes for the 2026 command-injection
  advisories (ZDI-26-560/561).
- **Secrets:** the access code lives in `/etc/bambu-tv/bambu-tv.env` (root,
  0600). go2rtc masks substituted values in its logs and strips credentials
  from errors sent to browsers. The DLNA service gets a separate environment
  file without the code.
- **Sandbox:** both services run as `DynamicUser` with `ProtectSystem=strict`,
  `ProtectHome`, no capabilities, `@system-service` syscalls and restricted
  address families; `systemd-analyze security` rates both 1.7 (OK).
- **Accepted on a home LAN:** go2rtc's WebSocket, MP4 and snapshot endpoints
  accept ad-hoc `?src=` sources (RTSP/HTTP/ffmpeg URLs), and the MJPEG and
  RTSP servers accept incoming streams. Someone on your network could make
  go2rtc open another URL or replace the picture. The sandbox keeps ffmpeg away
  from `/home` and system files. Set `RTSP_USER`/`RTSP_PASS` to protect RTSP,
  and never forward these ports to the internet.
- **go2rtc log format** is JSON: its text formatter drops any line containing
  a masked value, which would hide connection errors.

## Testing

`dev/fake-printer.sh` simulates the X2D camera (RTSPS on 322, self-signed
certificate, `/streaming/live/1`, digest login). `dev/smoke-test.sh` checks
the page, snapshot, MJPEG, HLS, RTSP, radio audio, the DLNA chain as a TV sees
it (multicast M-SEARCH → description → Browse → playing the item URL), and that
admin endpoints and scripted sources are refused.

Verified on 9 October 2026:

| Where | Result |
|---|---|
| Real X2D (fw 01.02.00.00) + Ubuntu 24.04 home server, two NICs | smoke test 19/19 with radio on; DLNA visible on both interfaces |
| Samsung QE77S93C (Tizen) | discovered the DLNA server, fetched its icon and thumbnail, played the MPEG-TS item |
| Debian 12 systemd container (arm64) | install, reinstall, migration from the former Gerbera add-on, DLNA on/off, radio on/off, uninstall, purge; smoke test green |
| Chrome, 1920×1080 | MSE live; fallback MSE → HLS → MJPEG; printer off → hint → recovery; sound on/off |

## Files

| Path | Contents |
|---|---|
| `install.sh` | installer, updater, uninstaller (systemd) |
| `config/go2rtc.yaml` | go2rtc configuration (settings via environment) |
| `config/radio.yaml` | optional radio source (second config file) |
| `web/` | TV page, logo, icon, bundled font |
| `dlna/` | DLNA server and its icons |
| `systemd/` | `bambu-tv` and `bambu-tv-dlna` units |
| `compose.yaml`, `bambu-tv.env.example` | Docker setup |
| `dev/` | simulated printer and end-to-end smoke test |
