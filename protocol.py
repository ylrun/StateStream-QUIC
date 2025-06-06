import struct
import time

#
# ─── protocol.py ────────────────────────────────────────────────────────────────
#
#   Defines:
#   • PDU type codes
#   • HEADER_FORMAT, HEADER_SIZE
#   • build/parse helpers for each PDU
#   • DFA state constants for server & client
#   • A fixed SESSION_ID for simplicity
#

# ─── CONSTANTS ─────────────────────────────────────────────────────────────────

# PDU type codes (1 byte each):
TYPE_OPEN_STREAM   = 1
TYPE_OPEN_ACK      = 2
TYPE_OPEN_NACK     = 3
TYPE_STREAM_CHUNK  = 4
TYPE_PAUSE_STREAM  = 5
TYPE_RESUME_STREAM = 6
TYPE_CLOSE_STREAM  = 7
TYPE_HEARTBEAT     = 8

# Header layout: version(1), type(1), flags(1), reserved(1), session_id(4), body_length(4)
HEADER_FORMAT     = "!BBBBII"
HEADER_SIZE       = struct.calcsize(HEADER_FORMAT)  # should be 12
PROTOCOL_VERSION  = 1

# Error codes for OPEN_NACK:
ERR_UNSUPPORTED_CODEC      = 1
ERR_UNSUPPORTED_ENCRYPTION = 2
ERR_MULTICAST_UNSUPPORTED  = 3
ERR_PROTOCOL_VIOLATION     = 99    # used when a client sends OPEN_STREAM out of order

# DFA states (server-side)
STATE_IDLE      = 0
STATE_STREAMING = 1
STATE_PAUSED    = 2
STATE_CLOSING   = 3
STATE_CLOSED    = 4

# DFA states (client-side)
CLIENT_STATE_IDLE       = 0
CLIENT_STATE_OPEN_SENT  = 1
CLIENT_STATE_STREAMING  = 2
CLIENT_STATE_PAUSED     = 3
CLIENT_STATE_CLOSING    = 4
CLIENT_STATE_CLOSED     = 5

# A fixed SESSION_ID for demo (in a real design you'd pick this in your DFA spec)
SESSION_ID = 12345


# ─── LOW-LEVEL I/O HELPERS ───────────────────────────────────────────────────────

def recv_exact(sock, n):
    """
    Read exactly n bytes from a blocking socket. Raise ConnectionError if socket closes early.
    """
    data = b""
    while len(data) < n:
        more = sock.recv(n - len(data))
        if not more:
            raise ConnectionError("Socket closed while trying to read")
        data += more
    return data


def send_exact(sock, data):
    """
    Send all bytes in data to a blocking socket. Raise ConnectionError if socket closes early.
    """
    total_sent = 0
    while total_sent < len(data):
        sent = sock.send(data[total_sent:])
        if sent == 0:
            raise ConnectionError("Socket closed while trying to write")
        total_sent += sent


# ─── PDU HEADER CLASS ───────────────────────────────────────────────────────────

class PDUHeader:
    """
    Represents a 12‐byte header:
      version   (1 byte)
      type      (1 byte)
      flags     (1 byte)
      reserved  (1 byte)
      session_id(4 bytes, uint32)
      body_length(4 bytes, uint32)
    """

    def __init__(self, pdu_type: int, flags: int, session_id: int, body_length: int):
        self.version     = PROTOCOL_VERSION
        self.type        = pdu_type & 0xFF
        self.flags       = flags & 0xFF
        self.reserved    = 0
        self.session_id  = session_id
        self.body_length = body_length

    def pack(self) -> bytes:
        """
        Pack header fields into 12 bytes (network byte order).
        """
        return struct.pack(
            HEADER_FORMAT,
            self.version,
            self.type,
            self.flags,
            self.reserved,
            self.session_id,
            self.body_length
        )

    @classmethod
    def unpack(cls, data: bytes):
        """
        Unpack 12 bytes into a PDUHeader instance.
        """
        if len(data) != HEADER_SIZE:
            raise ValueError(f"Expected {HEADER_SIZE} bytes, got {len(data)}")
        version, pdu_type, flags, reserved, session_id, body_length = struct.unpack(HEADER_FORMAT, data)
        hdr = cls(pdu_type, flags, session_id, body_length)
        hdr.version  = version
        hdr.reserved = reserved
        return hdr


# ─── BUILD / PARSE BODIES FOR EACH PDU TYPE ───────────────────────────────────────

def build_open_stream_body(codec_name: str, codec_profile: str, bitrate_list: list[int],
                           encryption_flag: int, delivery_mode: int) -> bytes:
    """
    Build the body for OPEN_STREAM (type = 1):
      codec_len      (2 bytes) + codec_name      (UTF-8)
      profile_len    (2 bytes) + codec_profile   (UTF-8)
      bitrate_count  (1 byte)  + (bitrate_count × 4 bytes uint32)
      encryption_flag(1 byte)
      delivery_mode  (1 byte)
    """
    codec_bytes   = codec_name.encode("utf-8")
    profile_bytes = codec_profile.encode("utf-8")

    body = struct.pack("!H", len(codec_bytes)) + codec_bytes
    body += struct.pack("!H", len(profile_bytes)) + profile_bytes
    body += struct.pack("!B", len(bitrate_list))
    for br in bitrate_list:
        body += struct.pack("!I", br)
    body += struct.pack("!B", encryption_flag & 0xFF)
    body += struct.pack("!B", delivery_mode  & 0xFF)
    return body


def parse_open_stream_body(body: bytes):
    """
    Parse an OPEN_STREAM body, returning:
      (codec_name:str, codec_profile:str, bitrate_list:list[int], encryption_flag:int, delivery_mode:int)
    """
    idx = 0
    (codec_len,) = struct.unpack_from("!H", body, idx)
    idx += 2
    codec_name = body[idx:idx+codec_len].decode("utf-8")
    idx += codec_len

    (profile_len,) = struct.unpack_from("!H", body, idx)
    idx += 2
    codec_profile = body[idx:idx+profile_len].decode("utf-8")
    idx += profile_len

    (bitrate_count,) = struct.unpack_from("!B", body, idx)
    idx += 1
    bitrate_list = []
    for _ in range(bitrate_count):
        (br,) = struct.unpack_from("!I", body, idx)
        idx += 4
        bitrate_list.append(br)

    (encryption_flag,) = struct.unpack_from("!B", body, idx)
    idx += 1
    (delivery_mode,)    = struct.unpack_from("!B", body, idx)
    idx += 1

    return codec_name, codec_profile, bitrate_list, encryption_flag, delivery_mode


def build_open_ack_body(chosen_bitrate: int, max_chunk_size: int,
                        keepalive_interval: int, session_timeout: int) -> bytes:
    """
    Build the body for OPEN_ACK (type = 2):
      chosen_bitrate   (4 bytes),
      max_chunk_size   (4 bytes),
      keepalive_interval (4 bytes),
      session_timeout  (4 bytes)
    """
    return struct.pack("!IIII", chosen_bitrate, max_chunk_size, keepalive_interval, session_timeout)


def parse_open_ack_body(body: bytes):
    """
    Parse OPEN_ACK body, returning (chosen_bitrate:int, max_chunk_size:int, keepalive_interval:int, session_timeout:int).
    """
    return struct.unpack("!IIII", body)


def build_open_nack_body(error_code: int) -> bytes:
    """
    Build the body for OPEN_NACK (type = 3):
      error_code (2 bytes, uint16)
    """
    return struct.pack("!H", error_code & 0xFFFF)


def parse_open_nack_body(body: bytes):
    """
    Parse OPEN_NACK body and return (error_code:int,)
    """
    (error_code,) = struct.unpack("!H", body)
    return error_code


def build_stream_chunk_body(seq_num: int, payload: bytes) -> bytes:
    """
    Build STREAM_CHUNK (type = 4) body:
      seq_num        (4 bytes),
      payload_length (4 bytes),
      payload_bytes  (payload_length)
    """
    payload_length = len(payload)
    return struct.pack("!II", seq_num, payload_length) + payload


def parse_stream_chunk_body(body: bytes):
    """
    Parse STREAM_CHUNK, returning (seq_num:int, payload_bytes:bytes).
    """
    (seq_num, payload_length) = struct.unpack_from("!II", body, 0)
    payload = body[8:8+payload_length]
    return seq_num, payload


def build_heartbeat_body() -> bytes:
    """
    Build HEARTBEAT (type = 8) body: an 8-byte timestamp (ms since Unix epoch).
    """
    ts_ms = int(time.time() * 1000)
    return struct.pack("!Q", ts_ms)


def parse_heartbeat_body(body: bytes):
    """
    Parse HEARTBEAT body and return timestamp in ms (int).
    """
    (ts_ms,) = struct.unpack("!Q", body)
    return ts_ms


def build_simple_header(pdu_type: int, session_id: int, body: bytes, flags: int=0) -> bytes:
    """
    Build a full PDU (12-byte header + body), given:
      - pdu_type: TYPE_*
      - session_id: uint32
      - body: raw bytes
      - flags: (1 byte)
    """
    hdr = PDUHeader(pdu_type, flags, session_id, len(body))
    return hdr.pack() + body


def build_empty_body_header(pdu_type: int, session_id: int, flags: int=0) -> bytes:
    """
    Build just the 12-byte header for PDUs with no body (PAUSE, RESUME, CLOSE).
    """
    hdr = PDUHeader(pdu_type, flags, session_id, 0)
    return hdr.pack()
