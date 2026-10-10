#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""bambooTV DLNA: the printer camera as a video in TV media browsers.

A deliberately small UPnP/DLNA MediaServer that offers exactly one item,
"X2D live". TVs find it through SSDP (Samsung: Source -> connected
devices) and play it as MPEG-TS. ffmpeg remuxes that per viewer from
go2rtc's local MP4 output (which keeps camera and optional radio audio in
sync), so go2rtc keeps its single connection to the printer.

Standard library only (Python 3.9+). Settings come from the environment:
  DLNA_NAME    name shown on the TV                   (default "bambooTV")
  DLNA_PORT    HTTP port of this server               (default 49499)
  DLNA_LAN_IP  advertise this address instead of auto-detecting one
  DLNA_TITLE   title of the video item                (default "X2D live")
  STREAM       go2rtc stream to play                  (default "tv": camera + radio)
  CAM_STREAM   go2rtc camera stream for thumbnails    (default "x2d")
  HTTP_PORT    bambooTV web port (stream, thumbnail)  (default 1984)
"""

import ipaddress
import os
import platform
import random
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

NAME = os.environ.get("DLNA_NAME") or "bambooTV"
DLNA_PORT = int(os.environ.get("DLNA_PORT") or 49499)
LAN_IP = os.environ.get("DLNA_LAN_IP") or ""
STREAM = os.environ.get("STREAM") or "tv"
CAM_STREAM = os.environ.get("CAM_STREAM") or "x2d"
TITLE = os.environ.get("DLNA_TITLE") or "X2D live"
HTTP_PORT = int(os.environ.get("HTTP_PORT") or 1984)

UDN = "uuid:" + str(uuid.uuid5(uuid.NAMESPACE_DNS, socket.gethostname() + "/bambu-tv/" + NAME))
SERVER = "Linux/%s UPnP/1.0 bambooTV/1.0" % platform.release()
ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
ICONS = [  # (file, mime, size) in the order TVs should prefer them
    ("icon-120.png", "image/png", 120),
    ("icon-48.png", "image/png", 48),
    ("icon-256.png", "image/png", 256),
    ("icon-120.jpg", "image/jpeg", 120),
    ("icon-48.jpg", "image/jpeg", 48),
]
SSDP_GROUP, SSDP_PORT = "239.255.255.250", 1900
DEVICE = "urn:schemas-upnp-org:device:MediaServer:1"
CDS = "urn:schemas-upnp-org:service:ContentDirectory:1"
CMS = "urn:schemas-upnp-org:service:ConnectionManager:1"
MRR = "urn:microsoft.com:service:X_MS_MediaReceiverRegistrar:1"
FEATURES = "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
DEBUG = os.environ.get("DLNA_DEBUG") == "1"
MAX_STREAMS = threading.BoundedSemaphore(6)
# Interfaces that never lead to a TV: container bridges and tunnels.
VIRTUAL = re.compile(r"^(docker|br-|veth|virbr|cni|flannel|cali|vxlan|tun|tap|wg|tailscale|zt)")
HOST_HEADER = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$")


def log(msg):
    print(msg, flush=True)


def present_icons():
    return [icon for icon in ICONS if os.path.isfile(os.path.join(ICON_DIR, icon[0]))]


def local_ip_for(peer):
    """The local address that routes to peer (no packet is sent)."""
    if LAN_IP:
        return LAN_IP
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect((peer, 9))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def interfaces():
    """(index, name, IPv4 or None) for every non-loopback interface."""
    result = []
    for index, name in socket.if_nameindex():
        if name.startswith("lo"):
            continue
        addr = None
        try:
            import fcntl  # Unix only; SIOCGIFADDR is Linux-specific
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                packed = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))
                addr = socket.inet_ntoa(packed[20:24])
        except (OSError, ImportError):
            pass
        result.append((index, name, addr))
    return result


def is_local_peer(addr):
    ip = ipaddress.ip_address(addr)
    return ip.is_private or ip.is_link_local or ip.is_loopback


# ------------------------------------------------------------------ XML / SOAP

def description(host):
    icons = "".join(
        "<icon><mimetype>%s</mimetype><width>%d</width><height>%d</height><depth>24</depth>"
        "<url>/icons/%s</url></icon>" % (mime, size, size, name)
        for name, mime, size in present_icons())
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">'
        "<specVersion><major>1</major><minor>0</minor></specVersion><device>"
        "<deviceType>%s</deviceType><friendlyName>%s</friendlyName>"
        "<manufacturer>bambooTV</manufacturer><modelDescription>Live 3D printer camera</modelDescription>"
        "<modelName>bambooTV</modelName><modelNumber>1</modelNumber><serialNumber>%s</serialNumber>"
        "<UDN>%s</UDN><dlna:X_DLNADOC>DMS-1.50</dlna:X_DLNADOC>"
        "<presentationURL>http://%s:%d/</presentationURL>%s<serviceList>%s%s%s</serviceList>"
        "</device></root>"
    ) % (DEVICE, escape(NAME), UDN[5:], UDN, host.split(":")[0], HTTP_PORT,
         "<iconList>%s</iconList>" % icons if icons else "",
         service_entry(CDS, "cds"), service_entry(CMS, "cms"), service_entry(MRR, "mrr"))


def service_entry(service_type, short):
    service_id = service_type.replace(":service:", ":serviceId:").rsplit(":", 1)[0]
    return ("<service><serviceType>%s</serviceType><serviceId>%s</serviceId>"
            "<SCPDURL>/%s.xml</SCPDURL><controlURL>/ctl/%s</controlURL>"
            "<eventSubURL>/evt/%s</eventSubURL></service>") % (service_type, service_id, short, short, short)


def scpd(actions, variables):
    """actions: {name: [(arg, direction, variable)]}, variables: {name: type}"""
    body = "".join(
        "<action><name>%s</name><argumentList>%s</argumentList></action>" % (name, "".join(
            "<argument><name>%s</name><direction>%s</direction>"
            "<relatedStateVariable>%s</relatedStateVariable></argument>" % arg for arg in args))
        for name, args in actions.items())
    state = "".join(
        '<stateVariable sendEvents="%s"><name>%s</name><dataType>%s</dataType></stateVariable>'
        % ("yes" if name == "SystemUpdateID" else "no", name, kind) for name, kind in variables.items())
    return ('<?xml version="1.0" encoding="utf-8"?><scpd xmlns="urn:schemas-upnp-org:service-1-0">'
            "<specVersion><major>1</major><minor>0</minor></specVersion>"
            "<actionList>%s</actionList><serviceStateTable>%s</serviceStateTable></scpd>") % (body, state)


BROWSE_ARGS = [("ObjectID", "in", "A_ARG_TYPE_ObjectID"), ("BrowseFlag", "in", "A_ARG_TYPE_BrowseFlag"),
               ("Filter", "in", "A_ARG_TYPE_Filter"), ("StartingIndex", "in", "A_ARG_TYPE_Index"),
               ("RequestedCount", "in", "A_ARG_TYPE_Count"), ("SortCriteria", "in", "A_ARG_TYPE_SortCriteria"),
               ("Result", "out", "A_ARG_TYPE_Result"), ("NumberReturned", "out", "A_ARG_TYPE_Count"),
               ("TotalMatches", "out", "A_ARG_TYPE_Count"), ("UpdateID", "out", "A_ARG_TYPE_UpdateID")]
SCPD = {
    "cds": scpd({
        "Browse": BROWSE_ARGS,
        "Search": [("ContainerID", "in", "A_ARG_TYPE_ObjectID"), ("SearchCriteria", "in", "A_ARG_TYPE_SearchCriteria")]
                  + BROWSE_ARGS[2:],
        "GetSearchCapabilities": [("SearchCaps", "out", "SearchCapabilities")],
        "GetSortCapabilities": [("SortCaps", "out", "SortCapabilities")],
        "GetSystemUpdateID": [("Id", "out", "SystemUpdateID")],
        "X_GetFeatureList": [("FeatureList", "out", "A_ARG_TYPE_Featurelist")],
    }, {
        "A_ARG_TYPE_ObjectID": "string", "A_ARG_TYPE_Result": "string", "A_ARG_TYPE_BrowseFlag": "string",
        "A_ARG_TYPE_Filter": "string", "A_ARG_TYPE_SortCriteria": "string", "A_ARG_TYPE_SearchCriteria": "string",
        "A_ARG_TYPE_Index": "ui4", "A_ARG_TYPE_Count": "ui4", "A_ARG_TYPE_UpdateID": "ui4",
        "A_ARG_TYPE_Featurelist": "string", "SearchCapabilities": "string", "SortCapabilities": "string",
        "SystemUpdateID": "ui4",
    }),
    "cms": scpd({
        "GetProtocolInfo": [("Source", "out", "SourceProtocolInfo"), ("Sink", "out", "SinkProtocolInfo")],
        "GetCurrentConnectionIDs": [("ConnectionIDs", "out", "CurrentConnectionIDs")],
        "GetCurrentConnectionInfo": [
            ("ConnectionID", "in", "A_ARG_TYPE_ConnectionID"), ("RcsID", "out", "A_ARG_TYPE_RcsID"),
            ("AVTransportID", "out", "A_ARG_TYPE_AVTransportID"),
            ("ProtocolInfo", "out", "A_ARG_TYPE_ProtocolInfo"),
            ("PeerConnectionManager", "out", "A_ARG_TYPE_ConnectionManager"),
            ("PeerConnectionID", "out", "A_ARG_TYPE_ConnectionID"),
            ("Direction", "out", "A_ARG_TYPE_Direction"), ("Status", "out", "A_ARG_TYPE_ConnectionStatus")],
    }, {
        "SourceProtocolInfo": "string", "SinkProtocolInfo": "string", "CurrentConnectionIDs": "string",
        "A_ARG_TYPE_ConnectionID": "i4", "A_ARG_TYPE_RcsID": "i4", "A_ARG_TYPE_AVTransportID": "i4",
        "A_ARG_TYPE_ProtocolInfo": "string", "A_ARG_TYPE_ConnectionManager": "string",
        "A_ARG_TYPE_Direction": "string", "A_ARG_TYPE_ConnectionStatus": "string",
    }),
    "mrr": scpd({
        "IsAuthorized": [("DeviceID", "in", "A_ARG_TYPE_DeviceID"), ("Result", "out", "A_ARG_TYPE_Result")],
        "IsValidated": [("DeviceID", "in", "A_ARG_TYPE_DeviceID"), ("Result", "out", "A_ARG_TYPE_Result")],
        "RegisterDevice": [("RegistrationReqMsg", "in", "A_ARG_TYPE_RegistrationReqMsg"),
                           ("RegistrationRespMsg", "out", "A_ARG_TYPE_RegistrationRespMsg")],
    }, {
        "A_ARG_TYPE_DeviceID": "string", "A_ARG_TYPE_Result": "int",
        "A_ARG_TYPE_RegistrationReqMsg": "bin.base64", "A_ARG_TYPE_RegistrationRespMsg": "bin.base64",
    }),
}
SERVICE_OF = {"cds": CDS, "cms": CMS, "mrr": MRR}


def soap_response(service, action, values):
    args = "".join("<%s>%s</%s>" % (k, escape(str(v)), k) for k, v in values)
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            '<u:%sResponse xmlns:u="%s">%s</u:%sResponse></s:Body></s:Envelope>') % (action, service, args, action)


def soap_fault(code, text):
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><s:Fault>'
            "<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>"
            '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0"><errorCode>%d</errorCode>'
            "<errorDescription>%s</errorDescription></UPnPError></detail></s:Fault></s:Body></s:Envelope>"
            ) % (code, escape(text))


def didl(objects):
    return ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">%s</DIDL-Lite>') % "".join(objects)


def root_container():
    return ('<container id="0" parentID="-1" restricted="1" searchable="1" childCount="1">'
            "<dc:title>%s</dc:title><upnp:class>object.container.storageFolder</upnp:class></container>"
            ) % escape(NAME)


def live_item(base):
    # Two equivalent resources: TVs pick the first MIME type they accept.
    return ('<item id="1" parentID="0" restricted="1"><dc:title>%s</dc:title>'
            "<upnp:class>object.item.videoItem</upnp:class>"
            '<upnp:albumArtURI dlna:profileID="JPEG_TN">%s/thumb.jpg</upnp:albumArtURI>'
            '<res protocolInfo="http-get:*:video/mp2t:%s">%s/stream.ts</res>'
            '<res protocolInfo="http-get:*:video/mpeg:%s">%s/stream.mpg</res></item>'
            ) % (escape(TITLE), base, FEATURES, base, FEATURES, base)


def tag(body, name):
    m = re.search(r"<(?:\w+:)?%s(?:\s[^>]*)?>([^<]*)</(?:\w+:)?%s>" % (name, name), body)
    return m.group(1).strip() if m else ""


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "bambooTV/1.0"
    sys_version = ""

    def version_string(self):
        return self.server_version
    protocol_version = "HTTP/1.1"
    timeout = 30

    def log_message(self, fmt, *args):  # quiet unless DLNA_DEBUG=1; streams are logged explicitly
        if DEBUG:
            log("http %s %s [%s]" % (self.client_address[0], fmt % args, self.headers.get("User-Agent", "")
                                     if hasattr(self, "headers") and self.headers else ""))

    def base_url(self):
        host = self.headers.get("Host", "")
        if not HOST_HEADER.match(host):
            host = "%s:%d" % (local_ip_for(self.client_address[0]), DLNA_PORT)
        return "http://" + host

    def reply(self, code, body=b"", mime="text/xml; charset=\"utf-8\"", extra=()):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("EXT", "")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/description.xml":
            return self.reply(200, description(self.base_url()[7:]))
        if path in ("/cds.xml", "/cms.xml", "/mrr.xml"):
            return self.reply(200, SCPD[path[1:4]])
        if path in ("/stream.ts", "/stream.mpg"):
            return self.stream("video/mp2t" if path.endswith(".ts") else "video/mpeg")
        if path == "/thumb.jpg":
            return self.thumbnail()
        if path.startswith("/icons/"):
            return self.icon(path[7:])
        if path == "/":
            host = self.base_url()[7:].split(":")[0]
            return self.reply(302, b"", "text/plain", [("Location", "http://%s:%d/" % (host, HTTP_PORT))])
        self.reply(404, b"not found", "text/plain")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        short = path[5:] if path.startswith("/ctl/") else ""
        if short not in SERVICE_OF:
            return self.reply(404, b"not found", "text/plain")
        length = int(self.headers.get("Content-Length") or 0)
        if length > 65536:
            return self.reply(413, b"too large", "text/plain")
        body = self.rfile.read(length).decode("utf-8", "replace")
        action = (self.headers.get("SOAPACTION") or "").strip().strip('"').split("#")[-1]
        try:
            values = self.soap_action(short, action, body)
        except LookupError as err:
            return self.reply(500, soap_fault(*err.args))
        self.reply(200, soap_response(SERVICE_OF[short], action, values))

    def do_SUBSCRIBE(self):  # events are never sent; TVs only need the handshake
        self.reply(200, b"", "text/plain", [("SID", "uuid:%s" % uuid.uuid4()), ("TIMEOUT", "Second-1800")])

    def do_UNSUBSCRIBE(self):
        self.reply(200, b"", "text/plain")

    def soap_action(self, short, action, body):
        if short == "cds":
            if action in ("Browse", "Search"):
                start = int(tag(body, "StartingIndex") or 0)
                if action == "Browse":
                    obj, flag = tag(body, "ObjectID"), tag(body, "BrowseFlag")
                    if obj == "0":
                        objects = [root_container()] if flag == "BrowseMetadata" else [live_item(self.base_url())]
                    elif obj == "1":
                        objects = [live_item(self.base_url())] if flag == "BrowseMetadata" else []
                    else:
                        raise LookupError(701, "No such object")
                else:
                    criteria = tag(body, "SearchCriteria")
                    wanted = criteria in ("", "*") or "videoItem" in criteria or "object.item" in criteria
                    objects = [live_item(self.base_url())] if wanted else []
                total = len(objects)
                objects = objects[start:] if start < total else []
                return [("Result", didl(objects)), ("NumberReturned", len(objects)),
                        ("TotalMatches", total), ("UpdateID", 1)]
            if action == "GetSearchCapabilities":
                return [("SearchCaps", "upnp:class")]
            if action == "GetSortCapabilities":
                return [("SortCaps", "")]
            if action == "GetSystemUpdateID":
                return [("Id", 1)]
            if action == "X_GetFeatureList":  # Samsung: where the videos live
                return [("FeatureList",
                         '<?xml version="1.0" encoding="UTF-8"?><Features xmlns="urn:schemas-upnp-org:av:avs">'
                         '<Feature name="samsung.com_BASICVIEW" version="1">'
                         '<container id="0" type="object.item.videoItem"/></Feature></Features>')]
        elif short == "cms":
            if action == "GetProtocolInfo":
                return [("Source", "http-get:*:video/mp2t:%s,http-get:*:video/mpeg:%s" % (FEATURES, FEATURES)),
                        ("Sink", "")]
            if action == "GetCurrentConnectionIDs":
                return [("ConnectionIDs", "0")]
            if action == "GetCurrentConnectionInfo":
                return [("RcsID", -1), ("AVTransportID", -1), ("ProtocolInfo", ""),
                        ("PeerConnectionManager", ""), ("PeerConnectionID", -1),
                        ("Direction", "Output"), ("Status", "OK")]
        elif short == "mrr":
            if action in ("IsAuthorized", "IsValidated"):
                return [("Result", 1)]
            if action == "RegisterDevice":
                return [("RegistrationRespMsg", "")]
        raise LookupError(401, "Invalid Action")

    def icon(self, name):
        if name not in [i[0] for i in ICONS]:
            return self.reply(404, b"not found", "text/plain")
        path = os.path.join(ICON_DIR, name)
        if not os.path.isfile(path):
            return self.reply(404, b"not found", "text/plain")
        with open(path, "rb") as f:
            data = f.read()
        self.reply(200, data, "image/png" if name.endswith(".png") else "image/jpeg",
                   [("Cache-Control", "max-age=86400")])

    def thumbnail(self):
        """A live frame of the print, or the logo when the printer is offline."""
        data = None
        url = "http://127.0.0.1:%d/api/frame.jpeg?src=%s&width=160&cache=15s" % (HTTP_PORT, CAM_STREAM)
        try:
            with urllib.request.urlopen(url, timeout=4) as r:
                if r.status == 200:
                    data = r.read(512 * 1024)
                    if not data.startswith(b"\xff\xd8"):
                        data = None
        except Exception:
            pass
        if data is None:
            path = os.path.join(ICON_DIR, "icon-120.jpg")
            if not os.path.isfile(path):
                return self.reply(404, b"not found", "text/plain")
            with open(path, "rb") as f:
                data = f.read()
        self.reply(200, data, "image/jpeg", [("Cache-Control", "no-cache")])

    def stream(self, mime):
        headers = [("transferMode.dlna.org", "Streaming"), ("contentFeatures.dlna.org", FEATURES)]
        if self.command == "HEAD":
            self.send_response(200)
            self.send_header("Content-Type", mime)
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            return
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            log("stream refused: ffmpeg is not installed")
            return self.reply(503, b"ffmpeg missing", "text/plain")
        if not MAX_STREAMS.acquire(blocking=False):
            return self.reply(503, b"too many viewers", "text/plain")
        viewer = self.client_address[0]
        proc = None
        try:
            proc = subprocess.Popen(
                [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-i", "http://127.0.0.1:%d/api/stream.mp4?src=%s" % (HTTP_PORT, STREAM),
                 "-map", "0:v:0", "-map", "0:a:0?",  # audio only when the radio is on
                 "-c", "copy", "-f", "mpegts",
                 "-mpegts_flags", "+resend_headers", "-muxdelay", "0", "-muxpreload", "0", "pipe:1"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            ready, _, _ = select.select([proc.stdout], [], [], 15)
            first = os.read(proc.stdout.fileno(), 65536) if ready else b""
            if not first:
                log("stream for %s: no video from the printer" % viewer)
                return self.reply(503, b"no video from the printer", "text/plain")
            log("stream started for %s (%s)" % (viewer, mime))
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            chunk = first
            while chunk:
                self.wfile.write(chunk)
                ready, _, _ = select.select([proc.stdout], [], [], 20)
                chunk = os.read(proc.stdout.fileno(), 65536) if ready else b""
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        finally:
            if proc:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                log("stream stopped for %s" % viewer)
            MAX_STREAMS.release()


# ------------------------------------------------------------------ SSDP

class SSDP:
    def __init__(self):
        self.boot_id = int(time.time())
        self.joined = {}  # interface -> its membership request, kept for rejoin()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        self.sock.bind(("", SSDP_PORT))
        self.join_all()

    def join_all(self):
        """Join the SSDP group on every interface, not just the default one."""
        group = socket.inet_aton(SSDP_GROUP)
        for index, name, addr in interfaces():
            if (index, name) in self.joined or VIRTUAL.match(name):
                continue
            for mreq in (struct.pack("4s4si", group, socket.inet_aton("0.0.0.0"), index),  # Linux ip_mreqn
                         struct.pack("4s4s", group, socket.inet_aton(addr)) if addr else None):
                if mreq is None:
                    continue
                try:
                    self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                    self.joined[(index, name)] = mreq
                    log("ssdp: listening on %s%s" % (name, " (%s)" % addr if addr else ""))
                    break
                except OSError:
                    continue
        if not self.joined:  # last resort: the kernel's default interface
            mreq = struct.pack("4s4s", group, socket.inet_aton("0.0.0.0"))
            try:
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                self.joined["default"] = mreq
                log("ssdp: listening on the default interface")
            except OSError as err:
                log("ssdp: cannot join multicast group: %s" % err)

    def rejoin(self):
        """Leave and re-join the SSDP group, so the network hears about it again.

        The kernel reports a multicast membership only when it joins. Routers
        and access points with IGMP snooping but no IGMP querier forget that
        report after a few minutes and stop passing the TVs' searches to this
        machine: bambooTV then drops off the TV until the service restarts.
        """
        for key, mreq in list(self.joined.items()):
            try:
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, mreq)
            except OSError:
                pass
            try:
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError:  # the interface is gone; join_all() picks it up if it returns
                del self.joined[key]
        self.join_all()

    def targets(self):
        return ([("upnp:rootdevice", UDN + "::upnp:rootdevice"), (UDN, UDN), (DEVICE, UDN + "::" + DEVICE)]
                + [(s, UDN + "::" + s) for s in (CDS, CMS, MRR)])

    def location(self, ip):
        return "http://%s:%d/description.xml" % (ip, DLNA_PORT)

    def serve(self):
        while True:
            try:
                data, (addr, port) = self.sock.recvfrom(4096)
            except OSError:
                return
            try:
                self.on_packet(data.decode("utf-8", "replace"), addr, port)
            except Exception as err:  # never let one odd packet stop discovery
                log("ssdp: ignored packet from %s: %s" % (addr, err))

    def on_packet(self, text, addr, port):
        lines = text.split("\r\n")
        if not lines[0].upper().startswith("M-SEARCH") or not is_local_peer(addr):
            return
        headers = {}
        for line in lines[1:]:
            key, sep, value = line.partition(":")
            if sep:
                headers[key.strip().upper()] = value.strip()
        if headers.get("MAN", "").strip('"') != "ssdp:discover":
            return
        st = headers.get("ST", "")
        if DEBUG:
            log("ssdp: search from %s for %s" % (addr, st))
        matches = [t for t in self.targets() if st == "ssdp:all" or st == t[0]]
        if not matches:
            return
        try:
            mx = max(1, min(int(headers.get("MX", "1")), 3))
        except ValueError:
            mx = 1
        location = self.location(local_ip_for(addr))
        timer = threading.Timer(random.uniform(0, mx * 0.5), self.respond, (matches, location, addr, port))
        timer.daemon = True
        timer.start()

    def respond(self, matches, location, addr, port):
        for nt, usn in matches:
            msg = ("HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\nDATE: %s\r\nEXT:\r\n"
                   "LOCATION: %s\r\nSERVER: %s\r\nST: %s\r\nUSN: %s\r\n"
                   "BOOTID.UPNP.ORG: %d\r\nCONFIGID.UPNP.ORG: 1\r\nContent-Length: 0\r\n\r\n"
                   ) % (formatdate(usegmt=True), location, SERVER, nt, usn, self.boot_id)
            try:
                self.sock.sendto(msg.encode(), (addr, port))
            except OSError:
                pass

    def notify(self, nts):
        """Announce on every real interface, each with its own address."""
        sent_on = []
        for _index, name, addr in interfaces():
            if not addr or VIRTUAL.match(name):
                continue
            sent_on.append(addr)
        if not sent_on:  # e.g. macOS during development: use the default route
            sent_on = [LAN_IP or local_ip_for("192.0.2.1")]
        for addr in sent_on:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as s:
                try:
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(addr))
                except OSError:
                    continue
                for nt, usn in self.targets():
                    head = "NOTIFY * HTTP/1.1\r\nHOST: %s:%d\r\nNT: %s\r\nNTS: %s\r\nUSN: %s\r\n" % (
                        SSDP_GROUP, SSDP_PORT, nt, nts, usn)
                    if nts == "ssdp:alive":
                        head += "CACHE-CONTROL: max-age=1800\r\nLOCATION: %s\r\nSERVER: %s\r\n" % (
                            self.location(LAN_IP or addr), SERVER)
                    head += "BOOTID.UPNP.ORG: %d\r\nCONFIGID.UPNP.ORG: 1\r\n\r\n" % self.boot_id
                    try:
                        s.sendto(head.encode(), (SSDP_GROUP, SSDP_PORT))
                    except OSError:
                        pass

    def announce_forever(self):
        for delay in (0, 1, 3):  # TVs sometimes miss the first burst
            time.sleep(delay)
            self.notify("ssdp:alive")
        minutes = 0
        while True:
            time.sleep(60)
            minutes += 1
            self.rejoin()  # also picks up interfaces that came up later (Wi-Fi, DHCP)
            if minutes % 3 == 0:  # a TV that switches on hears about us within 3 minutes
                self.notify("ssdp:alive")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # TVs drop idle keep-alive connections with a reset; that is not an error.
        if not isinstance(sys.exc_info()[1], (ConnectionError, socket.timeout)):
            super().handle_error(request, client_address)


def main():
    httpd = Server(("", DLNA_PORT), Handler)
    ssdp = SSDP()
    for target in (httpd.serve_forever, ssdp.serve, ssdp.announce_forever):
        threading.Thread(target=target, daemon=True).start()
    log("bambooTV DLNA: \"%s\" on port %d (%s), icons: %d" % (NAME, DLNA_PORT, UDN, len(present_icons())))

    stop = threading.Event()

    def shutdown(*_):
        ssdp.notify("ssdp:byebye")
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    stop.wait()
    httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
