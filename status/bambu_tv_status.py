#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""bambooTV status: what the printer is doing, for the TV page.

The printer reports its state through its own MQTT broker (TLS, port 8883),
the feed Bambu Studio uses on a LAN, with the same user "bblp" and LAN access
code as the camera. This service subscribes to that report, keeps the latest
state and serves a small summary as /status.json for the TV page: progress,
layers, time left, temperatures and the AMS.

Read-only: it subscribes and never publishes, so it cannot start, stop or
change anything on the printer. Like go2rtc with the camera, it only keeps
the connection while someone looks, and lets go a minute after the last
request.

Standard library only (Python 3.9+). Settings come from the environment:
  PRINTER_IP      the printer's IP address                  (required)
  ACCESS_CODE     the printer's LAN access code             (required)
  PRINTER_SERIAL  the printer's serial number   (default: read from its certificate)
  STATUS_PORT     HTTP port of this service                 (default 1985)
"""

import ipaddress
import json
import os
import re
import signal
import socket
import ssl
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PRINTER_IP = os.environ.get("PRINTER_IP") or ""
ACCESS_CODE = os.environ.get("ACCESS_CODE") or ""
SERIAL = os.environ.get("PRINTER_SERIAL") or ""
STATUS_PORT = int(os.environ.get("STATUS_PORT") or 1985)
MQTT_PORT = int(os.environ.get("MQTT_PORT") or 8883)
DEBUG = os.environ.get("STATUS_DEBUG") == "1"

LINGER = 60          # keep the printer connection this long after the last request
KEEPALIVE = 30       # MQTT keep-alive, seconds
RETRY_DELAYS = [2, 5, 10, 30]
MAX_PACKET = 1 << 20
NO_TRAY = 255
EXTERNAL = 254


def log(msg):
    print(msg, flush=True)


class FeedError(Exception):
    def __init__(self, kind, text):
        super().__init__(text)
        self.kind = kind  # auth | unreachable | refused | serial | protocol


# ------------------------------------------------------------------ MQTT

def varint(n):
    out = bytearray()
    while True:
        n, digit = divmod(n, 128)
        out.append(digit | (0x80 if n else 0))
        if not n:
            return bytes(out)


def utf8(text):
    data = text.encode()
    return struct.pack("!H", len(data)) + data


def send(sock, kind, body=b""):
    sock.sendall(bytes([kind]) + varint(len(body)) + body)


def recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise FeedError("protocol", "the printer closed the connection")
        data += chunk
    return data


def read_packet(sock, idle_timeout):
    """One MQTT packet. Only the wait for its first byte may time out."""
    sock.settimeout(idle_timeout)
    first = sock.recv(1)
    if not first:
        raise FeedError("protocol", "the printer closed the connection")
    sock.settimeout(30)  # a packet that has started must arrive completely
    length, shift = 0, 0
    while True:
        byte = recv_exact(sock, 1)[0]
        length |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
        if shift > 21:
            raise FeedError("protocol", "malformed packet")
    if length > MAX_PACKET:
        raise FeedError("protocol", "packet too large")
    return first[0], recv_exact(sock, length)


def serial_from_cert(der):
    """The printer's serial number: the common name of its own certificate."""
    at = der.rfind(b"\x06\x03\x55\x04\x03")  # commonName; the subject comes after the issuer
    if at < 0 or len(der) < at + 7:
        return ""
    length = der[at + 6]
    value = der[at + 7:at + 7 + length].decode("ascii", "replace")
    return value if re.fullmatch(r"[A-Za-z0-9]{6,32}", value) else ""


def merge(into, update):
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            merge(into[key], value)
        else:
            into[key] = value


class Feed:
    """A read-only subscription to the printer's report, kept while wanted."""

    def __init__(self):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.wanted_until = 0.0
        self.serial = SERIAL
        self.report = {}
        self.updated = 0.0
        self.state = "idle"   # idle | connecting | live | error
        self.error = ""

    def want(self):
        self.wanted_until = time.time() + LINGER
        self.wake.set()

    def wanted(self):
        return time.time() < self.wanted_until

    def set_state(self, state, error=""):
        with self.lock:
            if state != "live":
                self.report = {}
            self.state, self.error = state, error

    def run(self):
        failures, last_error = 0, ""
        while True:
            if not self.wanted():
                self.set_state("idle")
                self.wake.clear()
                if not self.wanted():  # a request may have come in just now
                    self.wake.wait()
                continue
            try:
                self.session()
                failures, last_error = 0, ""
                continue
            except FeedError as err:
                kind, text = err.kind, str(err)
            except ssl.SSLError as err:
                kind, text = "protocol", "TLS: %s" % err
            except ConnectionRefusedError:
                kind, text = "refused", "the printer refused the connection on port %d" % MQTT_PORT
            except OSError as err:  # timeouts, no route, host down
                kind, text = "unreachable", "the printer is not reachable (%s)" % (err.strerror or err)
            self.set_state("error", kind)
            if text != last_error:
                log("status: %s" % text)
                last_error = text
            time.sleep(RETRY_DELAYS[min(failures, len(RETRY_DELAYS) - 1)])
            failures += 1

    def session(self):
        self.set_state("connecting")
        context = ssl.create_default_context()
        # Each printer has its own certificate, signed by Bambu Lab's device CA,
        # which is not in any trust store. Like go2rtc for the camera, accept it.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((PRINTER_IP, MQTT_PORT), timeout=10)
        try:
            sock = context.wrap_socket(raw)
        except Exception:
            raw.close()
            raise
        with sock:
            if not self.serial:
                self.serial = serial_from_cert(sock.getpeercert(binary_form=True) or b"")
                if not self.serial:
                    raise FeedError("serial", "the printer's serial number is unknown; set PRINTER_SERIAL")
            send(sock, 0x10, utf8("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", KEEPALIVE)
                 + utf8("bambootv-%d" % os.getpid()) + utf8("bblp") + utf8(ACCESS_CODE))
            kind, body = read_packet(sock, 10)
            if kind >> 4 != 2 or len(body) < 2:
                raise FeedError("protocol", "unexpected answer to the login")
            if body[1] in (4, 5):
                raise FeedError("auth", "the printer rejected the access code")
            if body[1]:
                raise FeedError("refused", "the printer refused the login (code %d)" % body[1])
            send(sock, 0x82, struct.pack("!H", 1) + utf8("device/%s/report" % self.serial) + b"\x00")
            log("status: connected to the printer")
            last_sent = last_heard = time.time()
            while self.wanted():
                if time.time() - last_sent > KEEPALIVE / 2:
                    send(sock, 0xC0)  # PINGREQ
                    last_sent = time.time()
                try:
                    kind, body = read_packet(sock, 5)
                except socket.timeout:
                    if time.time() - last_heard > 2 * KEEPALIVE:
                        raise FeedError("unreachable", "the printer stopped answering")
                    continue
                last_heard = time.time()
                if kind >> 4 == 9 and body[2:3] == b"\x80":
                    raise FeedError("refused", "the printer refused the status subscription")
                if kind >> 4 == 3:
                    self.on_publish(kind, body)
            send(sock, 0xE0)  # DISCONNECT
            log("status: nobody watching, disconnected from the printer")

    def on_publish(self, kind, body):
        if len(body) < 2:
            return
        start = 2 + struct.unpack("!H", body[:2])[0] + (2 if kind & 0x06 else 0)
        try:
            message = json.loads(body[start:].decode("utf-8", "replace"))
        except ValueError:
            return
        report = message.get("print") if isinstance(message, dict) else None
        if not isinstance(report, dict):
            return
        with self.lock:
            merge(self.report, report)
            self.updated = time.time()
            self.state, self.error = "live", ""


FEED = Feed()


# ------------------------------------------------------------------ summary

def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def whole(value, default=0):
    return int(round(number(value, default)))


def packed(value):
    """Dual-nozzle printers pack a temperature as (target << 16) | current."""
    value = whole(value)
    return value & 0xFFFF, value >> 16


def temp(current, target):
    return {"temp": whole(current), "target": whole(target)}


def colors(tray):
    found = [c for c in (tray.get("cols") or []) if isinstance(c, str) and re.fullmatch(r"[0-9A-Fa-f]{8}", c)]
    if not found and re.fullmatch(r"[0-9A-Fa-f]{8}", str(tray.get("tray_color") or "")):
        found = [tray["tray_color"]]
    # Fully transparent means "unknown colour" (a spool without an RFID tag, or no spool).
    return ["#" + c[:6].upper() for c in found if c[6:].upper() != "00"]


def tray_summary(tray, active):
    kind = str(tray.get("tray_type") or "").strip()
    if not kind:
        return {"empty": True, "active": False}
    remain = whole(tray.get("remain"), -1)
    return {
        "type": kind,
        "name": str(tray.get("tray_sub_brands") or "").strip() or kind,
        "colors": colors(tray),
        "remain": remain if 0 <= remain <= 100 else None,
        "active": active,
    }


def nozzles(report):
    extruder = (report.get("device") or {}).get("extruder") or {}
    info = [e for e in (extruder.get("info") or []) if isinstance(e, dict)]
    if len(info) < 2:
        return [dict(temp(report.get("nozzle_temper"), report.get("nozzle_target_temper")), active=True)]
    # H2D/X2D: two nozzles; the one working is the one aimed highest.
    result = [dict(temp(*packed(e.get("temp"))), id=whole(e.get("id"))) for e in info]
    result.sort(key=lambda n: n["id"])
    hottest = max(result, key=lambda n: n["target"])
    for n in result:
        n["active"] = n is hottest and n["target"] > 0
        del n["id"]
    return result


def summary():
    with FEED.lock:
        state, error, report, updated = FEED.state, FEED.error, json.loads(json.dumps(FEED.report)), FEED.updated
    out = {"feed": state}
    if error:
        out["error"] = error
    if state != "live" or not report:
        return out
    device = report.get("device") or {}
    out["age"] = round(time.time() - updated, 1)
    out["job"] = {
        "state": str(report.get("gcode_state") or "IDLE").upper(),
        "name": str(report.get("subtask_name") or "").strip(),
        "percent": max(0, min(100, whole(report.get("mc_percent")))),
        "remaining": max(0, whole(report.get("mc_remaining_time"))),  # minutes
        "layer": whole(report.get("layer_num")),
        "layers": whole(report.get("total_layer_num")),
        "stage": whole(report.get("stg_cur"), -1),
        "alerts": len(report.get("hms") or []) + (1 if whole(report.get("print_error")) else 0),
    }
    temps = {"nozzles": nozzles(report)}
    if "bed_temper" in report:
        temps["bed"] = temp(report.get("bed_temper"), report.get("bed_target_temper"))
    elif isinstance(device.get("bed"), dict):
        temps["bed"] = temp(*packed((device["bed"].get("info") or {}).get("temp")))
    if isinstance(device.get("ctc"), dict):
        temps["chamber"] = temp(*packed((device["ctc"].get("info") or {}).get("temp")))
    elif number(report.get("chamber_temper")) > 0:
        temps["chamber"] = temp(report.get("chamber_temper"), 0)
    out["temps"] = temps

    ams = report.get("ams") or {}
    now = whole(ams.get("tray_now"), NO_TRAY)
    units = []
    for unit in ams.get("ams") or []:
        if not isinstance(unit, dict):
            continue
        uid = whole(unit.get("id"))
        trays = []
        for tray in unit.get("tray") or []:
            slot = whole(tray.get("id"))
            # tray_now counts 4 slots per AMS; an AMS HT (id 128 and up) has one slot.
            active = now == (uid if uid >= 128 else uid * 4 + slot)
            trays.append(tray_summary(tray, active))
        humidity = number(unit.get("humidity_raw"), -1)
        units.append({
            "id": uid,
            "humidity": whole(humidity) if humidity >= 0 else None,
            "temp": round(number(unit.get("temp")), 1) if unit.get("temp") not in (None, "") else None,
            "trays": trays,
        })
    external = []
    spools = report.get("vir_slot") if isinstance(report.get("vir_slot"), list) else [report.get("vt_tray")]
    for spool in spools:
        if isinstance(spool, dict) and str(spool.get("tray_type") or "").strip():
            external.append(tray_summary(spool, now == EXTERNAL))
    out["ams"] = {"units": units, "external": external}
    return out


# ------------------------------------------------------------------ HTTP

def is_local_peer(addr):
    ip = ipaddress.ip_address(addr)
    return ip.is_private or ip.is_link_local or ip.is_loopback


class Handler(BaseHTTPRequestHandler):
    server_version = "bambooTV-status/1.1"
    sys_version = ""

    def log_message(self, fmt, *args):  # quiet unless STATUS_DEBUG=1
        if DEBUG:
            log("http %s %s" % (self.client_address[0], fmt % args))

    def do_GET(self):
        if self.path.split("?", 1)[0] != "/status.json":
            self.send_error(404)
            return
        if not is_local_peer(self.client_address[0]):
            self.send_error(403)
            return
        FEED.want()
        body = json.dumps(summary(), separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")  # the TV page is served from another port
        self.end_headers()
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # TVs drop idle keep-alive connections with a reset; that is not an error.
        if not isinstance(sys.exc_info()[1], (ConnectionError, socket.timeout)):
            super().handle_error(request, client_address)


def main():
    if not PRINTER_IP or not ACCESS_CODE:
        log("bambooTV status: PRINTER_IP and ACCESS_CODE are required")
        return 1
    httpd = Server(("", STATUS_PORT), Handler)
    threading.Thread(target=FEED.run, daemon=True).start()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log("bambooTV status: port %d, printer %s" % (STATUS_PORT, PRINTER_IP))

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
