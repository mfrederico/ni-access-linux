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
WIRE_AUTH0_LOGIN_REQUEST = 32
WIRE_AUTH0_ACCESS_TOKEN_REQUEST = 33
WIRE_AUTH0_ACCESS_TOKEN_RESPONSE = 34
WIRE_GET_PREFERENCES_REQUEST = 49
WIRE_GET_PREFERENCES_RESPONSE = 50
WIRE_KNOWN_PRODUCTS_REQUEST = 47
WIRE_KNOWN_PRODUCTS_RESPONSE = 48

def identify_request(fields):
    """Identify what type of request this is based on field numbers."""
    field_nums = {f[0] for f in fields}

    if FIELD_DAEMON_PING_REQUEST in field_nums:
        return "daemonPingRequest"
    if FIELD_DAEMON_VERSION_REQUEST in field_nums or WIRE_DAEMON_VERSION_REQUEST in field_nums:
        return "daemonVersionRequest"
    if WIRE_ACTIVE_DEPLOYMENTS_REQUEST in field_nums:
        return "activeDeploymentsRequest"
    if WIRE_AUTH0_LOGIN_REQUEST in field_nums:
        return "auth0LoginRequest"
    if WIRE_AUTH0_ACCESS_TOKEN_REQUEST in field_nums:
        return "auth0AccessTokenRequest"
    if WIRE_GET_PREFERENCES_REQUEST in field_nums:
        return "getPreferencesRequest"
    if WIRE_KNOWN_PRODUCTS_REQUEST in field_nums:
        return "knownProductsRequest"
    if 42 in field_nums:
        return "userInfoRequest"
    if 51 in field_nums:
        return "setPreferencesRequest"
    if 77 in field_nums:
        return "subscriptionsRequest"
    if 87 in field_nums:
        return "refreshProductListRequest"
    if 67 in field_nums:
        return "currentKompleteHddsRequest"
    if FIELD_LOGIN_REQUEST in field_nums:
        return "loginRequest"
    if FIELD_LOGOUT_REQUEST in field_nums:
        return "logoutRequest"
    if FIELD_IS_LOGGED_IN_REQUEST in field_nums:
        return "isLoggedInRequest"

    return f"unknown (fields: {field_nums})"


def build_header(request_id=b""):
    """Build a response header with protocol version."""
    # Header: field 1 = Version message, field 2 = taskId
    # Version: field 1 = major (8), field 3 = micro (1) — matches request protocol
    version = encode_field(1, 0, 8) + encode_field(3, 0, 1)
    return encode_field(1, 2, version)


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


def handle_auth0_login(fields):
    """Handle auth0 login — exchange PKCE code for tokens."""
    # Extract the auth code, redirect URI, and code verifier from field 32
    login_data = None
    for fnum, wtype, val in fields:
        if fnum == WIRE_AUTH0_LOGIN_REQUEST and isinstance(val, bytes):
            inner = decode_fields(val)
            auth_code = ""
            redirect_uri = ""
            code_verifier = ""
            for ifnum, iwtype, ival in inner:
                if ifnum == 1 and isinstance(ival, bytes):
                    auth_code = ival.decode('utf-8', errors='replace')
                elif ifnum == 2 and isinstance(ival, bytes):
                    redirect_uri = ival.decode('utf-8', errors='replace')
                elif ifnum == 3 and isinstance(ival, bytes):
                    code_verifier = ival.decode('utf-8', errors='replace')
            login_data = (auth_code, redirect_uri, code_verifier)

    if not login_data:
        log.error("Could not parse auth0LoginRequest")
        return handle_unknown("auth0LoginRequest", b"")

    auth_code, redirect_uri, code_verifier = login_data
    log.info(f"Auth0 login: code={auth_code[:20]}... redirect={redirect_uri} verifier={code_verifier[:20]}...")

    # Exchange the code for tokens
    try:
        import requests as http_requests
        resp = http_requests.post(
            f"https://auth.native-instruments.com/oauth/token",
            json={
                "grant_type": "authorization_code",
                "client_id": "GgcQZ2OCSvzqgVL7RSAoErQRNB9S59kh",
                "code": auth_code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
            timeout=15,
        )
        token_data = resp.json()
        if "access_token" in token_data:
            log.info("Auth0 token exchange successful!")
            access_token = token_data["access_token"]
            id_token = token_data.get("id_token", "")

            # Publish userLoggedInEvent on PUB socket (field 69, tag 554)
            if pub_socket:
                try:
                    event = encode_field(FIELD_HEADER, 2, build_header()) + encode_field(69, 2, b"")
                    pub_socket.send_multipart([b"daemon/event", event])
                    log.info("Published userLoggedInEvent")
                except Exception as pe:
                    log.error(f"Failed to publish login event: {pe}")

            # Store tokens for later and persist to disk
            global stored_tokens
            stored_tokens = token_data
            save_tokens()

            # Return auth0AccessTokenResponse (field 34)
            header = build_header()
            token_body = (
                encode_field(1, 2, access_token) +
                encode_field(2, 2, id_token)
            )
            return encode_field(FIELD_HEADER, 2, header) + encode_field(WIRE_AUTH0_ACCESS_TOKEN_RESPONSE, 2, token_body)
        else:
            log.error(f"Token exchange failed: {token_data}")
    except Exception as e:
        log.error(f"Token exchange error: {e}")

    # Return success anyway to not block the flow
    header = build_header()
    return encode_field(FIELD_HEADER, 2, header) + encode_field(3, 2, b"")  # successResponse


# Stored auth tokens — persist to disk
DAEMON_TOKEN_FILE = os.path.expanduser("~/.ni-daemon-tokens.json")
stored_tokens = {}

def save_tokens():
    try:
        with open(DAEMON_TOKEN_FILE, 'w') as f:
            json.dump(stored_tokens, f)
        log.info(f"Tokens saved to {DAEMON_TOKEN_FILE}")
    except Exception as e:
        log.error(f"Failed to save tokens: {e}")

def load_tokens():
    global stored_tokens
    try:
        if os.path.exists(DAEMON_TOKEN_FILE):
            with open(DAEMON_TOKEN_FILE) as f:
                stored_tokens = json.load(f)
            log.info(f"Loaded stored tokens (access_token: {len(stored_tokens.get('access_token',''))} chars)")
    except Exception as e:
        log.error(f"Failed to load tokens: {e}")


def handle_auth0_access_token():
    """Return stored access token."""
    header = build_header()
    access_token = stored_tokens.get("access_token", "")
    id_token = stored_tokens.get("id_token", "")

    if access_token:
        token_body = (
            encode_field(1, 2, access_token) +
            encode_field(2, 2, id_token)
        )
        return encode_field(FIELD_HEADER, 2, header) + encode_field(WIRE_AUTH0_ACCESS_TOKEN_RESPONSE, 2, token_body)

    # No tokens stored — return empty
    return encode_field(FIELD_HEADER, 2, header) + encode_field(3, 2, b"")


def handle_get_preferences():
    """Return default preferences with Linux paths."""
    header = build_header()
    # Preferences (G type):
    #   field 1 = downloadLocation (string)
    #   field 2 = contentLocation (string)
    #   field 3 = installLocation (string)
    #   field 4 = dataTrackingEnabled (bool)
    prefs_inner = (
        encode_field(1, 2, os.path.expanduser("~/NI-Downloads")) +
        encode_field(2, 2, os.path.expanduser("~/NI-Instruments")) +
        encode_field(3, 2, "/usr/lib/vst")
    )
    # PreferencesResponse has field 1 = Preferences message (nested)
    prefs_body = encode_field(1, 2, prefs_inner)
    return encode_field(FIELD_HEADER, 2, header) + encode_field(WIRE_GET_PREFERENCES_RESPONSE, 2, prefs_body)


def handle_user_info():
    """Return user info from the stored token."""
    header = build_header()
    # UserInfoResponse (field 43, tag 346): has fields for user details
    # Field 1 = nativeId (string), field 2 = email (string), field 3 = firstName, field 4 = lastName
    # Get user info from the stored id_token or from the API
    try:
        import requests as http_requests
        access_token = stored_tokens.get("access_token", "")
        if access_token:
            resp = http_requests.get(
                "https://auth.native-instruments.com/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            user = resp.json()
            native_id = user.get("native_id", user.get("sub", ""))
            email = user.get("email", "")
            nickname = user.get("nickname", "")
            log.info(f"User info: {email} (native_id: {native_id})")

            user_body = (
                encode_field(1, 2, native_id) +
                encode_field(2, 2, email) +
                encode_field(3, 2, nickname)
            )
            return encode_field(FIELD_HEADER, 2, header) + encode_field(43, 2, user_body)
    except Exception as e:
        log.error(f"Failed to get user info: {e}")

    return encode_field(FIELD_HEADER, 2, header) + encode_field(3, 2, b"")


def handle_known_products():
    """Return known products from NI API."""
    header = build_header()
    access_token = stored_tokens.get("access_token", "")
    if not access_token:
        return encode_field(FIELD_HEADER, 2, header) + encode_field(WIRE_KNOWN_PRODUCTS_RESPONSE, 2, b"")

    try:
        import requests as http_requests
        app_token = (
            "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9."
            "eyJpYXQiOjE2MzQ3MzA1MjYsInN1YiI6ImFwcGxpY2F0aW9uIiwiZGF0YSI6eyJuYW1lIjoiTmF0aXZlQWNjZXNzIiwidmVyc2lvbiI6IjIuMCJ9LCJleHAiOjI1MzQwMjMwMDc5OX0."
            "U6EQdp8WNcOyYFIHWw9tGUDUCEtxSuLmqEOfLB2UCZMYUkmsV5TItuKPbPCg5-_s7Ls3_4vbMDpisfGqXretddhVnBg-UoSJB4vj4RZtZq29_KaSly9cFA2A5lVbCDEM1bKNkKfNSyfDM6Whkdu2ub3aqt3LgAg7dfMVI3-_MY24txhZNW8xQ44M1nVsiUkpMk7nqrhIwcnb7EX-DPLbIQQ2NCLtoEGiA9eeCu19RvekxTxbttghDptkFBYqs_6CTiKmg98BkU8kQn2225LuzLIeD43vA6yHGyPwyvZloO1Pid5TcRH5qjqjLcfnCk65lSEGR39fZY_AnuDQAtF4tg"
        )

        # Get user's products
        resp = http_requests.get(
            "https://api.native-instruments.com/v1/users/me/products",
            headers={
                "Authorization": f"Bearer {access_token}",
                "X-NI-App-Token": app_token,
                "Accept": "application/json",
                "User-Agent": "NativeAccess/3.24.0",
            },
            timeout=15,
        )
        data = resp.json()
        products = data.get("response_body", {}).get("products", [])
        log.info(f"Fetched {len(products)} products from NI API")

        # Get download artifacts which have product titles
        artifacts_resp = http_requests.get(
            "https://api.native-instruments.com/v2/download/me/full-products",
            headers={
                "Authorization": f"Bearer {access_token}",
                "X-NI-App-Token": app_token,
                "Accept": "application/json",
                "User-Agent": "NativeAccess/3.24.0",
            },
            timeout=30,
        )
        all_artifacts = artifacts_resp.json().get("artifacts", [])
        # Build UPID -> title map from artifacts
        upid_titles = {}
        for a in all_artifacts:
            uid = a.get("upid", "")
            if uid and uid not in upid_titles:
                upid_titles[uid] = a.get("product_title", uid[:8])
        log.info(f"Resolved {len(upid_titles)} product titles from artifacts")

        product_entries = b""
        for p in products:
            upid = p.get("upid", "")
            title = upid_titles.get(upid, "")
            if not title:
                # Try product API as fallback
                try:
                    pr = http_requests.get(
                        f"https://api.native-instruments.com/v1/products/{upid}",
                        headers={"Authorization": f"Bearer {app_token}", "Accept": "application/json", "User-Agent": "NativeAccess/3.24.0"},
                        timeout=5,
                    )
                    if pr.status_code == 200:
                        resources = pr.json().get("response_body", {}).get("resources", [])
                        title = next((r["value"] for r in resources if r["key"] == "name"), "")
                except:
                    pass
            if not title:
                log.debug(f"Skipping product with no title: {upid}")
                continue

            # Build KnownProduct protobuf
            # field 1 = upid (string)
            # field 2 = isPlayer (bool)
            # field 3 = title (string)
            # field 7 = installed (bool)
            # field 8 = updateable (bool)
            # field 11 = activationState (int32, 1=ACTIVATED)
            # field 12 = isOwned (bool)
            # field 14 = isLocatable (bool)
            # field 15 = isInstallable (bool)
            # field 19 = type (int32)
            # field 20 = isUninstallable (bool)
            product_pb = (
                encode_field(1, 2, upid) +      # upid
                encode_field(3, 2, title) +     # title
                encode_field(7, 0, 0) +         # installed = false (not yet installed)
                encode_field(11, 0, 1) +        # activationState = ACTIVATED
                encode_field(12, 0, 1) +        # isOwned = true
                encode_field(15, 0, 1)          # isInstallable = true
            )
            product_entries += encode_field(1, 2, product_pb)

        log.info(f"Built knownProductsResponse with {len(products)} products")

        # Publish productListRefreshedEvent on PUB socket (field 85, tag 682)
        if pub_socket:
            try:
                event = encode_field(FIELD_HEADER, 2, build_header()) + encode_field(85, 2, b"")
                pub_socket.send(event)
                log.info("Published productListRefreshedEvent")
            except Exception as pe:
                log.error(f"Failed to publish product event: {pe}")

        return encode_field(FIELD_HEADER, 2, header) + encode_field(WIRE_KNOWN_PRODUCTS_RESPONSE, 2, product_entries)

    except Exception as e:
        log.error(f"Failed to fetch products: {e}")
        return encode_field(FIELD_HEADER, 2, header) + encode_field(WIRE_KNOWN_PRODUCTS_RESPONSE, 2, b"")


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
                elif request_type == "auth0LoginRequest":
                    response = handle_auth0_login(fields)
                elif request_type == "auth0AccessTokenRequest":
                    response = handle_auth0_access_token()
                elif request_type == "getPreferencesRequest":
                    response = handle_get_preferences()
                elif request_type == "userInfoRequest":
                    response = handle_user_info()
                elif request_type == "knownProductsRequest":
                    response = handle_known_products()
                elif request_type == "setPreferencesRequest":
                    header = build_header()
                    response = encode_field(FIELD_HEADER, 2, header) + encode_field(52, 2, b"")
                elif request_type == "refreshProductListRequest":
                    # Respond with success, then publish productListRefreshedEvent
                    header = build_header()
                    response = encode_field(FIELD_HEADER, 2, header) + encode_field(3, 2, b"")
                    # Publish event after a short delay
                    if pub_socket:
                        def _publish_refresh():
                            time.sleep(1)
                            try:
                                event = encode_field(FIELD_HEADER, 2, build_header()) + encode_field(85, 2, b"")
                                pub_socket.send_multipart([b"daemon/event", event])
                                log.info("Published productListRefreshedEvent")
                            except Exception as e:
                                log.error(f"Failed to publish: {e}")
                        threading.Thread(target=_publish_refresh, daemon=True).start()
                elif request_type == "subscriptionsRequest":
                    header = build_header()
                    response = encode_field(FIELD_HEADER, 2, header) + encode_field(78, 2, b"")
                elif request_type == "currentKompleteHddsRequest":
                    header = build_header()
                    response = encode_field(FIELD_HEADER, 2, header) + encode_field(68, 2, b"")
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


pub_socket = None

def run_pub_server(context):
    """PUB server — publishes events to plugins."""
    global pub_socket
    socket = context.socket(zmq.PUB)
    socket.bind(f"tcp://127.0.0.1:{PUB_PORT}")
    pub_socket = socket
    log.info(f"PUB server listening on tcp://127.0.0.1:{PUB_PORT}")

    # Periodically publish daemonStatusEvent (field 44, tag 354)
    # ZMQ PUB messages must be multipart: [channel, protobuf_data]
    while True:
        try:
            time.sleep(30)
            status_body = encode_field(1, 0, 1)  # status = 1 (ok)
            heartbeat = encode_field(FIELD_HEADER, 2, build_header()) + encode_field(44, 2, status_body)
            socket.send_multipart([b"daemon/event", heartbeat])
            log.debug("Published daemonStatusEvent")
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

    # Load previously stored tokens
    load_tokens()

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
