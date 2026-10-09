# Third-party notices

bambooTV is MIT-licensed (see [LICENSE](LICENSE)). It builds on the following
software and assets. Programs marked *installed separately* are downloaded or
installed on your machine by `install.sh` (or come from Docker images); they
are not part of this repository and keep their own licenses.

| Component | Used for | License | How |
|---|---|---|---|
| [go2rtc](https://github.com/AlexxIT/go2rtc) 1.9.14 | streaming engine | MIT | installed separately (pinned release, SHA-256 verified) or `alexxit/go2rtc` Docker image |
| go2rtc `video-rtc.js` | the MSE playback logic in `web/index.html` follows it | MIT, Copyright (c) 2022 Alexey Khit | adapted code, notice kept in the source |
| [Archivo](https://github.com/Omnibus-Type/Archivo) | display typeface of the TV page | SIL Open Font License 1.1 | bundled: `web/fonts/archivo-expanded.woff2`, license in `web/fonts/OFL.txt` |
| [FFmpeg](https://ffmpeg.org) | MJPEG, snapshots, radio transcoding, DLNA remux | LGPL 2.1+ / GPL 2+ (depends on the build) | installed separately from your Linux distribution |
| [Python](https://www.python.org) 3 | DLNA server (standard library only) | PSF License | installed separately |
| [MediaMTX](https://github.com/bluenviron/mediamtx) | `dev/fake-printer.sh` only (testing) | MIT | installed separately by developers |

## Radio stations

The optional background radio plays a public internet radio stream that you
choose (`RADIO_URL`). The music, programming and any advertising belong to the
station; bambooTV only plays the stream inside your home network. The default
suggestion is 181.FM Power 181; you can replace it with any station you are
allowed to listen to.

## Trademarks

bambooTV is an independent open-source project. It is not affiliated with,
endorsed by or sponsored by Bambu Lab. "Bambu Lab", "X2D" and related names are
trademarks of their respective owners and are used here only to describe
compatibility.
