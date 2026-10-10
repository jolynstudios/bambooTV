#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Simulates the printer's status feed so bambooTV can be tested without a printer.

MQTT over TLS (user "bblp", password = access code), publishing an X2D-style
report every second to whoever subscribes, the way the printer does. Without
--percent the simulated print runs from 0 to 100 % in ten minutes and starts
over. dev/fake-printer.sh starts it next to the simulated camera.

  dev/fake-mqtt.py --cert server.crt --key server.key [--code a1b2c3d4] [--port 8883]
                   [--state RUNNING|PREPARE|PAUSE|FINISH|FAILED|IDLE] [--percent 49]
                   [--stage 2] [--alerts 1]
"""

import argparse
import json
import socketserver
import ssl
import struct
import threading
import time

ARGS = None
START = time.time()


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


def recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def read_packet(sock):
    kind = recv_exact(sock, 1)[0]
    length, shift = 0, 0
    while True:
        byte = recv_exact(sock, 1)[0]
        length |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return kind, recv_exact(sock, length)


def read_string(body, at):
    n = struct.unpack("!H", body[at:at + 2])[0]
    return body[at + 2:at + 2 + n].decode("utf-8", "replace"), at + 2 + n


def packed(current, target):
    return (target << 16) | current


def report():
    elapsed = time.time() - START
    percent = ARGS.percent if ARGS.percent is not None else int(elapsed % 600 / 6)
    state = ARGS.state or "RUNNING"
    if state == "FINISH":
        percent = 100
    if state == "IDLE":
        percent = 0
    layers = 120
    printing = state in ("PREPARE", "RUNNING", "PAUSE")
    nozzle = 220 if printing else 36
    bed = 55 if printing else 28
    return {"print": {
        "command": "push_status", "msg": 0, "sequence_id": str(int(elapsed)),
        "gcode_state": state, "mc_percent": percent, "mc_remaining_time": round((100 - percent) * 2.3),
        "layer_num": max(1, round(layers * percent / 100)) if state != "IDLE" else 0,
        "total_layer_num": layers if state != "IDLE" else 0,
        "stg_cur": ARGS.stage if ARGS.stage is not None else (2 if state == "PREPARE" else 0),
        "subtask_name": "" if state == "IDLE" else "Benchy, plate 1",
        "print_error": 0, "hms": [{"attr": 0, "code": 0}] * ARGS.alerts,
        "nozzle_temper": float(nozzle), "nozzle_target_temper": float(nozzle if printing else 0),
        "bed_temper": float(bed), "bed_target_temper": float(bed if printing else 0),
        "device": {
            "extruder": {"state": 0x8112, "info": [
                {"id": 0, "temp": packed(38, 0)},
                {"id": 1, "temp": packed(nozzle, nozzle if printing else 0)}]},
            "bed": {"info": {"temp": packed(bed, bed if printing else 0)}},
            "ctc": {"info": {"temp": packed(31, 0)}},
        },
        "ams": {"tray_now": "2" if printing else "255", "ams": [{
            "id": "0", "humidity": "4", "humidity_raw": "21", "temp": "27.6", "tray": [
                {"id": "0", "tray_type": "PLA", "tray_sub_brands": "PLA Basic", "tray_color": "161616FF", "remain": 80},
                {"id": "1", "tray_type": "PLA", "tray_sub_brands": "PLA Matte", "tray_color": "C12E1FFF", "remain": 45},
                {"id": "2", "tray_type": "PETG", "tray_sub_brands": "PETG HF", "tray_color": "E8E4DCFF", "remain": 62},
                {"id": "3", "tray_type": "PLA", "tray_sub_brands": "PLA Silk Dual", "tray_color": "E4B45BFF",
                 "cols": ["E4B45BFF", "3A6EA5FF"], "remain": -1},
            ]}]},
        "vir_slot": [{"id": "254", "tray_type": "", "tray_color": "00000000"}],
    }}


class Client(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        lock = threading.Lock()
        stop = threading.Event()
        topic = []

        def publish():
            while not stop.wait(1):
                if topic:
                    payload = json.dumps(report()).encode()
                    body = utf8(topic[0]) + payload
                    with lock:
                        sock.sendall(b"\x30" + varint(len(body)) + body)

        try:
            kind, body = read_packet(sock)
            if kind >> 4 != 1:
                return
            at = 10  # protocol name, level, flags, keep-alive
            flags = body[7]
            _client, at = read_string(body, at)
            user, at = read_string(body, at) if flags & 0x80 else ("", at)
            password, at = read_string(body, at) if flags & 0x40 else ("", at)
            accepted = user == "bblp" and password == ARGS.code
            sock.sendall(b"\x20\x02\x00" + (b"\x00" if accepted else b"\x05"))
            if not accepted:
                return
            threading.Thread(target=publish, daemon=True).start()
            while True:
                kind, body = read_packet(sock)
                if kind >> 4 == 8:  # SUBSCRIBE
                    name, _ = read_string(body, 2)
                    topic[:] = [name]
                    with lock:
                        sock.sendall(b"\x90\x03" + body[:2] + b"\x00")
                elif kind >> 4 == 12:  # PINGREQ
                    with lock:
                        sock.sendall(b"\xd0\x00")
                elif kind >> 4 == 14:  # DISCONNECT
                    return
        except (EOFError, OSError):
            pass
        finally:
            stop.set()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, context):
        self.context = context
        super().__init__(address, Client)

    def get_request(self):
        sock, addr = self.socket.accept()
        return self.context.wrap_socket(sock, server_side=True), addr


def main():
    global ARGS
    parser = argparse.ArgumentParser(description="Simulated printer status feed (MQTT over TLS)")
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--code", default="a1b2c3d4")
    parser.add_argument("--port", type=int, default=8883)
    parser.add_argument("--state", choices=["RUNNING", "PREPARE", "PAUSE", "FINISH", "FAILED", "IDLE"])
    parser.add_argument("--percent", type=int)
    parser.add_argument("--stage", type=int)
    parser.add_argument("--alerts", type=int, default=0)
    ARGS = parser.parse_args()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ARGS.cert, ARGS.key)
    with Server(("", ARGS.port), context) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
