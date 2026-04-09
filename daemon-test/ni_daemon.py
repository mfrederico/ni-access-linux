#!/usr/bin/env python3
"""
NTKDaemon replacement — Python ZMQ server that speaks the NI protobuf protocol.
Listens on the same ports as the real NTKDaemon so NI plugins can communicate.

Ports:
  tcp://127.0.0.1:5146 — REQ/REP (plugins send requests, daemon replies)
  tcp://127.0.0.1:5563 — PUB (daemon publishes events to plugins)

Protocol: protobuf-encoded messages (ni.ntk.daemon.proto.*)

This is a work-in-progress — start with basic handshake/ping responses and
build up from there based on what the plugins actually request.
"""
import json
import os
import sys
import time
import signal
import logging
import struct
import threading

try:
    import zmq
except ImportError:
    print("ERROR: pyzmq not installed. Run: pip install pyzmq")
    sys.exit(1)

# ============================================================================
# Config
# ============================================================================
REQ_PORT = 5146   # Request/Reply port
PUB_PORT = 5563   # Publish port
LOG_FILE = os.path.expanduser("~/NI-Downloads/ni-daemon.log")
TOKEN_FILE = os.path.expanduser("~/.ni-access-token.json")

logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] [daemon] [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE),
    ]
)
log = logging.getLogger("ni-daemon")

# ============================================================================
# Minimal Protobuf Helpers
# We implement just enough protobuf wire format to understand and respond to
# messages without needing the full protobuf library or .proto files.
# ============================================================================

def encode_varint(value):
    """Encode an integer as a protobuf varint."""
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)

def decode_varint(data, offset=0):
    """Decode a protobuf varint, return (value, new_offset)."""
    result = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        result |= (byte & 0x7F) << shift
        offset += 1
        if not (byte & 0x80):
            break
        shift += 7
    return result, offset

def encode_field(field_number, wire_type, value):
    """Encode a single protobuf field."""
    tag = encode_varint((field_number << 3) | wire_type)
    if wire_type == 0:  # varint
        return tag + encode_varint(value)
    elif wire_type == 2:  # length-delimited
        if isinstance(value, str):
            value = value.encode('utf-8')
        return tag + encode_varint(len(value)) + value
    return tag

def decode_fields(data):
    """Decode all fields from a protobuf message. Returns list of (field_num, wire_type, value)."""
    fields = []
    offset = 0
    while offset < len(data):
        if offset >= len(data):
            break
        tag, offset = decode_varint(data, offset)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:  # varint
            value, offset = decode_varint(data, offset)
        elif wire_type == 1:  # 64-bit
            value = struct.unpack_from('<Q', data, offset)[0]
            offset += 8
        elif wire_type == 2:  # length-delimited
            length, offset = decode_varint(data, offset)
            value = data[offset:offset+length]
            offset += length
        elif wire_type == 5:  # 32-bit
            value = struct.unpack_from('<I', data, offset)[0]
            offset += 4
        else:
            log.warning(f"Unknown wire type {wire_type} for field {field_num}")
            break
        fields.append((field_num, wire_type, value))
    return fields


# ============================================================================
# Message types from the NI protocol
# Based on the protobuf field numbers observed in the JS client code:
#
# The outer "DaemonMessage" wrapper uses these field numbers:
#   1: header
#   2: errorResponse
#   3: successResponse
#   4: pingResponse
#   5: loginRequest
#   6: logoutRequest
#   7: daemonPingRequest
#   8: isLoggedInRequest
#   9: statusResponse
#  10: getLoginUiCommandRequest
#  11: getLoginUiCommandResponse
#  12: downloadProductInfoXmlRequest
#  13: downloadProductInfoXmlResponse
#  14: shutdownRequest
#  15: pauseDownloadRequest
#  16: resumeDownloadRequest
#  17: cancelDownloadRequest
#  18: registerSerialRequest
#  19: registerSerialResponse
#  20: downloadStartedEvent
#  ...etc
#  48: auth0LoginRequest
#  49: auth0AccessTokenRequest
#  50: auth0AccessTokenResponse
#  51: auth0LogoutRequest
#  52: deviceCodeLoginRequest
#  53: deviceCodeLoginResponse
#  55: daemonVersionRequest
#  56: daemonVersionResponse
# ============================================================================

# Field numbers for the outer DaemonMessage
FIELD_HEADER = 1
FIELD_ERROR_RESPONSE = 2
FIELD_SUCCESS_RESPONSE = 3
FIELD_PING_RESPONSE = 4
FIELD_LOGIN_REQUEST = 5
FIELD_LOGOUT_REQUEST = 6
FIELD_DAEMON_PING_REQUEST = 7
FIELD_IS_LOGGED_IN_REQUEST = 8
FIELD_STATUS_RESPONSE = 9
FIELD_DAEMON_VERSION_REQUEST = 55
FIELD_DAEMON_VERSION_RESPONSE = 56
# Wire format uses different field numbers than the code constants
# Field 71 on the wire = daemonVersionRequest (ba 04 tag)
WIRE_DAEMON_VERSION_REQUEST = 71
WIRE_DAEMON_VERSION_RESPONSE = 72
WIRE_ACTIVE_DEPLOYMENTS_REQUEST = 73
WIRE_ACTIVE_DEPLOYMENTS_RESPONSE = 74

def identify_request(fields):
    """Identify what type of request this is based on field numbers."""
    field_nums = {f[0] for f in fields}

    if FIELD_DAEMON_PING_REQUEST in field_nums:
        return "daemonPingRequest"
    if FIELD_DAEMON_VERSION_REQUEST in field_nums or WIRE_DAEMON_VERSION_REQUEST in field_nums:
        return "daemonVersionRequest"
    if WIRE_ACTIVE_DEPLOYMENTS_REQUEST in field_nums:
        return "activeDeploymentsRequest"
    if FIELD_LOGIN_REQUEST in field_nums:
        return "loginRequest"
    if FIELD_LOGOUT_REQUEST in field_nums:
        return "logoutRequest"
    if FIELD_IS_LOGGED_IN_REQUEST in field_nums:
        return "isLoggedInRequest"

    return f"unknown (fields: {field_nums})"


def build_header(request_id=b""):
    """Build a response header."""
    # Header has field 1 = request_id (string)
    return encode_field(1, 2, request_id)


def handle_ping():
    """Respond to daemon ping."""
    header = build_header()
    ping_resp = b""  # Empty ping response body
    return encode_field(FIELD_HEADER, 2, header) + encode_field(FIELD_PING_RESPONSE, 2, ping_resp)


def handle_version_request():
    """Respond to daemon version request."""
    header = build_header()
    # Version response has individual fields:
    #   field 1 = major (uint32)
    #   field 2 = minor (uint32)
    #   field 3 = micro (uint32)
    #   field 4 = build (string)
    version_body = (
        encode_field(1, 0, 1) +    # major = 1
        encode_field(2, 0, 30) +   # minor = 30
        encode_field(3, 0, 0) +    # micro = 0
        encode_field(4, 2, "0")    # build = "0"
    )
    return encode_field(FIELD_HEADER, 2, header) + encode_field(WIRE_DAEMON_VERSION_RESPONSE, 2, version_body)


def handle_is_logged_in():
    """Respond to is-logged-in check."""
    header = build_header()
    # Status response with logged_in = true
    # This is approximate — we'll refine based on actual requests
    status_body = encode_field(1, 0, 1)  # logged_in = true
    return encode_field(FIELD_HEADER, 2, header) + encode_field(FIELD_STATUS_RESPONSE, 2, status_body)


def handle_active_deployments():
    """Respond to active deployments check — return empty (no active installs)."""
    header = build_header()
    # Active deployments response: empty list = no active deployments
    deployments_body = b""  # Empty = no active installations in progress
    return encode_field(FIELD_HEADER, 2, header) + encode_field(WIRE_ACTIVE_DEPLOYMENTS_RESPONSE, 2, deployments_body)


def handle_unknown(request_type, raw_data):
    """Handle unknown requests with a success response."""
    log.info(f"Unknown request: {request_type}, data hex: {raw_data[:100].hex()}")
    header = build_header()
    success_body = b""
    return encode_field(FIELD_HEADER, 2, header) + encode_field(FIELD_SUCCESS_RESPONSE, 2, success_body)


# ============================================================================
# ZMQ Server
# ============================================================================
def run_req_server(context):
    """REQ/REP server — handles requests from plugins."""
    socket = context.socket(zmq.REP)
    socket.bind(f"tcp://127.0.0.1:{REQ_PORT}")
    log.info(f"REQ/REP server listening on tcp://127.0.0.1:{REQ_PORT}")

    while True:
        try:
            raw = socket.recv()
            log.debug(f"Received {len(raw)} bytes: {raw[:50].hex()}...")

            try:
                fields = decode_fields(raw)
                request_type = identify_request(fields)
                log.info(f"Request type: {request_type}")

                if request_type == "daemonPingRequest":
                    response = handle_ping()
                elif request_type == "daemonVersionRequest":
                    response = handle_version_request()
                elif request_type == "activeDeploymentsRequest":
                    response = handle_active_deployments()
                elif request_type == "isLoggedInRequest":
                    response = handle_is_logged_in()
                else:
                    response = handle_unknown(request_type, raw)

            except Exception as e:
                log.error(f"Error processing request: {e}")
                response = handle_unknown("error", raw)

            socket.send(response)
            log.debug(f"Sent {len(response)} bytes response")

        except zmq.ZMQError as e:
            if e.errno == zmq.ETERM:
                break
            log.error(f"ZMQ error: {e}")
        except Exception as e:
            log.error(f"Server error: {e}")


def run_pub_server(context):
    """PUB server — publishes events to plugins."""
    socket = context.socket(zmq.PUB)
    socket.bind(f"tcp://127.0.0.1:{PUB_PORT}")
    log.info(f"PUB server listening on tcp://127.0.0.1:{PUB_PORT}")

    # Periodically publish heartbeat events
    while True:
        try:
            time.sleep(30)
            # Heartbeat event (field 42 in the outer message based on JS code)
            heartbeat = encode_field(1, 2, build_header())
            socket.send(heartbeat)
            log.debug("Published heartbeat")
        except zmq.ZMQError as e:
            if e.errno == zmq.ETERM:
                break
        except Exception as e:
            log.error(f"PUB error: {e}")


# ============================================================================
# Main
# ============================================================================
def main():
    log.info("=" * 60)
    log.info("NI Daemon (Python replacement) starting...")
    log.info(f"REQ/REP port: {REQ_PORT}")
    log.info(f"PUB port: {PUB_PORT}")
    log.info("=" * 60)

    context = zmq.Context()

    # Start both servers in threads
    req_thread = threading.Thread(target=run_req_server, args=(context,), daemon=True)
    pub_thread = threading.Thread(target=run_pub_server, args=(context,), daemon=True)

    req_thread.start()
    pub_thread.start()

    log.info("Daemon is running. Press Ctrl+C to stop.")

    def signal_handler(sig, frame):
        log.info("Shutting down...")
        context.term()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        signal_handler(None, None)


if __name__ == "__main__":
    main()
