# bambooTV — technical overview

## Architecture

```
printer camera ──RTSPS :322──▶ go2rtc stream "x2d" ──┬─ :8554 RTSP restream, snapshot, MJPEG
 (one connection, only            │                   │
  while someone watches)          ▼                   │
radio station ──▶ ffmpeg ──▶ go2rtc stream "tv" ──────┴─ :1984 TV page (MSE/HLS), MP4 → DLNA
                 (optional, AAC)   (camera + radio)

TV "connected devices" ◀── SSDP :1900 / HTTP :49499 ── bambu_tv_dlna.py (service bambu-tv-dlna)
                                                        ├─ per viewer: ffmpeg remuxes go2rtc's MP4 → MPEG-TS
                                                        └─ and burns the print status into the picture

printer status ──MQTT/TLS :8883──▶ bambu_tv_status.py ──HTTP :1985 /status.json──▶ TV page card
 (read-only subscription,           (service bambu-tv-status)
  only while someone watches)
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
- **The status service** (`status/bambu_tv_status.py`) is a small,
  standard-library Python MQTT subscriber and HTTP server that turns the
  printer's status report into the TV page's print card.

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
  M-SEARCH with a `LOCATION` routable to the asking TV; says goodbye and
  hello around every restart, and repeats the hello **every minute**. Only
  answers private addresses.
- **Presence on the TV:** entries live on the TV for `max-age` (900 s) after
  the last announcement. A TV that misses announcements — it was off, or the
  router's IGMP snooping swallowed them — would keep a dead entry for half an
  hour and show bambooTV as "gone". The per-minute hello (with a per-minute
  multicast re-join, see below) re-introduces the server within two minutes.
  At startup the server first says goodbye for the previous boot, pauses two
  seconds so the goodbye cannot arrive after the hello, then says hello.
- **IGMP snooping:** the server leaves and re-joins the SSDP group every
  minute. The kernel reports a multicast membership only when it joins, and
  routers or access points with IGMP snooping but no IGMP querier forget that
  report after a few minutes. From then on the TVs' searches no longer reach
  the server, and it disappears from the TV until it restarts. The re-join
  sends a fresh report every minute.
- **Content:** one `object.item.videoItem`, "X2D live", with two resources
  (`video/mp2t` and `video/mpeg`, same stream), DLNA flags for live streaming,
  and a live thumbnail (160 px snapshot via go2rtc, logo as fallback; one
  retry, because go2rtc's snapshot producer races the stream producer for the
  printer's single camera session and can lose exactly once). Supports
  Browse, Search, Samsung's `X_GetFeatureList`, ConnectionManager and the
  MediaReceiverRegistrar; unknown actions get UPnP error 401.
- **Warm start:** every Browse fires one discarded snapshot in the
  background, which makes go2rtc open the camera before the TV presses play.
  After a long quiet period the first dial to the printer can take seconds —
  longer than a TV waits — so the play request itself attaches to a warm
  producer and starts in about a second.
- **Playback:** per viewer, ffmpeg remuxes go2rtc's MP4 output to MPEG-TS.
  MP4 rather than RTSP because go2rtc's MP4 muxer keeps camera video and
  radio audio on one timeline; at most 6 concurrent viewers.
  - **Burned-in status:** while the status service has something to report,
    the player re-encodes (`libx264 veryfast`) with the print card over the
    bottom right of the picture — the same panel as the TV page, with the
    mark, job, percent, a block-glyph progress bar, layer and temperatures
    (`dlna/card.png` background, translucent 62 %, texts re-read from the
    status files every frame (`reload=1`), drifting sideways every minute
    for OLED panels). The status is polled (every 3 s) only while someone
    watches, so the printer connection still follows the "on demand" rule.
    `expansion=none` is required — the default text expansion swallows the
    `%` sign and with it the whole line. With no status text (status service
    off, printer offline) the stream stays a zero-CPU remux (`-c copy`).
    `--overlay off` (or `OVERLAY=off` in Docker) forces the remux, e.g. on a
    Raspberry Pi.
  - **Stall recovery:** the printer dropping mid-stream (Wi-Fi power save,
    firmware reboot) would freeze a copy pipe forever. Instead the player
    rebuilds itself up to three times — a new `PAT`/`PMT` in the same
    response lets the TV resync — and the start itself is tried twice, so a
    slow first dial is not reported to the TV as "cannot play". ffmpeg's
    stderr is kept and its last line logged whenever a player fails, which
    turns "the picture is gone" into a one-line reason in the journal.
- **Icon:** PNG/JPEG 48/120/256 px in `dlna/icons/`, rendered from
  `web/icon.svg`.

## Radio

- Two streams: **`x2d`** is the plain camera (RTSP, snapshot, MJPEG);
  **`tv`** is what the TV page and DLNA play, defined as the camera via
  go2rtc's own RTSP loopback. `config/radio.yaml` is a second go2rtc config
  file that redefines `tv` with a second source, `ffmpeg:${RADIO_URL}#audio=aac`.
  The service always passes it; go2rtc skips a missing file, so the file's
  presence is the on/off switch.
- Why two streams: the printer's SDP carries no codec parameters
  (`sprop-parameter-sets`), and go2rtc's RTSP output does not align the
  camera's and the radio's timestamps. An RTSP client joining a running
  camera+radio stream then finds no picture parameters. go2rtc's MP4/HLS
  muxers do align both tracks, so the radio only goes into `tv`, and RTSP apps
  get the camera alone.
- Consumers that only need video (snapshot, MJPEG) never start the radio. If
  the station is unreachable, consumers get video without audio.
- go2rtc's MPEG-TS **HLS** variant does not repeat the codec setup when an
  audio track is present, so the app-facing HLS URL is the fMP4 variant
  (`?src=tv&mp4`), which works with and without radio.
- Free stations insert their own ads, which may be geo-targeted.

## Print status

- **Feed:** the printer runs an MQTT broker on **port 8883** (TLS, user
  `bblp`, password = the LAN access code) and publishes its state on
  `device/<serial>/report`; Bambu Studio uses the same feed in LAN mode. The
  X2D (firmware 01.02.00.00) sends a complete report about once a second with
  LAN Only Liveview on; models that send partial updates are merged into one
  state.
- **Read-only:** the service only subscribes. It never publishes, so it cannot
  start, pause, stop or change anything on the printer.
- **Serial number:** the topic needs the printer's serial number, which is the
  common name of the printer's own TLS certificate, so nothing has to be
  configured. `PRINTER_SERIAL` overrides it.
- **On demand:** the service connects when the TV page first asks for the
  status and disconnects a minute after the last request. Errors are retried
  after 2, 5, 10, then every 30 s. MQTT keep-alive is 30 s.
- **Summary:** `/status.json` holds the job (state, name, percent, minutes
  left, layer, current stage, number of alerts), the temperatures (each
  nozzle, bed, chamber) and the AMS (per unit: humidity in %, temperature and
  each slot's material, name, colours and the active slot; external spools).
  Dual-nozzle printers (H2D, X2D) pack a temperature as
  `(target << 16) | current`; the active nozzle is the one with the highest
  target. `tray_now` counts four slots per AMS; an AMS HT (id 128 and up) has
  one. The serial number and the access code are not in the summary.
- **TV page:** polls every 2 s while visible (not when the TV shows another
  app). *Auto* shows the card for 15 s when the page opens, every 10 %, on a
  state change and on a remote press, and otherwise a small tag with the
  progress and the ready time; *Always* and *Off* are in the menu and
  remembered per TV. The card's mark is the bambooTV logo, its four layers
  filled to the print's progress with the hotend on the current one. OLED
  care: the card and tag drift slightly every minute and dim after ten
  minutes without a remote press.
- **CORS:** the page and the status service run on different ports, so the
  service answers `Access-Control-Allow-Origin: *` (it serves no credentials)
  and only to private addresses.

## Security

Open on the home network by design: the TV page, the streams, the print status
and the DLNA server (a TV cannot log in). Everything else is closed:

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
  from errors sent to browsers. The status service needs the code for its
  login and never logs or serves it. The DLNA service gets a separate
  environment file without the code.
- **Printer certificate:** each printer has its own certificate, signed by
  Bambu Lab's device CA, which is in no trust store. Like go2rtc for the
  camera (`rtsps://` to an IP address), the status service accepts it without
  verification. Someone who can impersonate the printer on your LAN could
  therefore learn the access code; this is inherent to the printer's LAN
  protocols.
- **Sandbox:** all three services run as `DynamicUser` with
  `ProtectSystem=strict`, `ProtectHome`, no capabilities, `@system-service`
  syscalls and restricted address families; `systemd-analyze security` rates
  each 1.7 (OK).
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
the page, snapshot, MJPEG, HLS, RTSP, radio audio, the print status (live feed
and CORS header), the DLNA chain as a TV sees it (multicast M-SEARCH →
description → Browse → playing the item URL), and that admin endpoints and
scripted sources are refused. The simulated printer includes the status feed
(`dev/fake-mqtt.py`: an X2D-style report every second, with options for
paused, finished, stopped and idle prints).

Verified on 9 and 10 October 2026:

| Where | Result |
|---|---|
| Real X2D (fw 01.02.00.00) + Ubuntu 24.04 home server, two NICs | smoke test 19/19 with radio on; DLNA visible on both interfaces |
| Same network: router with IGMP snooping and no IGMP querier | hosts stop receiving SSDP about 4 minutes after joining; with the per-minute re-join the server kept receiving and answered every search for 11 minutes |
| Real X2D, printing (10 October) | status feed live within 3 s; progress, layer, time left, both nozzles, bed, chamber and the AMS (four slots, active slot, humidity) match the printer |
| Samsung QE77S93C (Tizen) | discovered the DLNA server, fetched its icon and thumbnail, played the MPEG-TS item |
| Real X2D printing, 10 October (after the overlay release) | smoke test 19/19 against the home server; DLNA item with the burned-in print card verified frame-accurate (panel, mark, percent, block bar, layer, temperatures, live-updating); play after 95 s of quiet started in 1.2 s; compact translucent card variant approved on the same TV |
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
| `status/` | print status service |
| `systemd/` | `bambu-tv`, `bambu-tv-status` and `bambu-tv-dlna` units |
| `compose.yaml`, `bambu-tv.env.example` | Docker setup |
| `dev/` | simulated printer (camera and status feed) and end-to-end smoke test |
