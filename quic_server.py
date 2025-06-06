#!/usr/bin/env python3
import os
import argparse
import asyncio
import ssl
import pathlib
import subprocess
import time
import sys
import tempfile

from aioquic.asyncio import QuicConnectionProtocol, serve
from aioquic.quic.configuration import QuicConfiguration

from protocol import (
    HEADER_SIZE,
    TYPE_OPEN_STREAM, TYPE_OPEN_ACK, TYPE_OPEN_NACK,
    TYPE_STREAM_CHUNK, TYPE_PAUSE_STREAM, TYPE_RESUME_STREAM,
    TYPE_CLOSE_STREAM, TYPE_HEARTBEAT,
    ERR_PROTOCOL_VIOLATION,
    build_open_ack_body, build_open_nack_body,
    parse_open_stream_body, build_stream_chunk_body,
    build_simple_header, build_empty_body_header,
    STATE_IDLE, STATE_STREAMING, STATE_PAUSED, STATE_CLOSING, STATE_CLOSED,
    SESSION_ID, PDUHeader
)


#
# ─── quic_server.py (buffer‐based parsing, no private _reader) ───────────────────
#
#   • Listens on UDP port 9000 (override with --port).
#   • Generates a self-signed certificate with SANs for localhost & 127.0.0.1
#     (using a temporary OpenSSL config file, suitable for macOS).
#   • Maintains a per‐stream buffer to accumulate QUIC StreamDataReceived bytes,
#     then parses full PDUs out of that buffer (no more `._reader` usage).
#   • Implements server‐side DFA: OPEN_STREAM→OPEN_ACK→STREAMING→PAUSE→RESUME→CLOSE.
#   • Blocks forever after binding so it does not exit immediately.
#


# ─── DEFAULT PARAMETERS ────────────────────────────────────────────────────────
DEFAULT_PORT         = 9000
DEFAULT_MEDIA_PATH   = "sample_media.bin"
DEFAULT_BITRATE      = 500_000     # in bps
DEFAULT_CHUNK_SIZE   = 64 * 1024   # 64 KiB
DEFAULT_KEEPALIVE    = 5           # seconds
DEFAULT_SESSION_TO   = 30          # seconds


class StreamingServerProtocol(QuicConnectionProtocol):
    """
    Handles one QUIC connection. We keep a dict `self.buffers` mapping stream_id→bytearray(),
    where incoming StreamDataReceived bytes are appended. Whenever a full PDU (HEADER+body)
    is present, we parse and dispatch it according to our DFA.

    States:
      STATE_IDLE → (OPEN_STREAM) → STATE_STREAMING
      STATE_STREAMING → (PAUSE_STREAM) → STATE_PAUSED
      STATE_PAUSED → (RESUME_STREAM) → STATE_STREAMING
      STATE_STREAMING or PAUSED → (CLOSE_STREAM) → STATE_CLOSED

    Any out‐of‐order OPEN_STREAM → send OPEN_NACK(ERR_PROTOCOL_VIOLATION).
    Any unexpected PDU at the wrong state is ignored (warning).
    Idle for > session_timeout → force close.
    """

    def __init__(self, *args, media_path: str = DEFAULT_MEDIA_PATH, **kwargs):
        super().__init__(*args, **kwargs)
        self.media_path = media_path
        self.state      = STATE_IDLE
        self.seq_num    = 1
        self.paused     = False
        self._last_recv = time.time()
        self.keepalive_interval = DEFAULT_KEEPALIVE
        self.session_timeout    = DEFAULT_SESSION_TO

        # Map stream_id → bytearray of buffered incoming data
        self.buffers = {}

    def quic_event_received(self, event):
        """
        Called whenever a QUIC-level event arrives. We only care about:
          - StreamDataReceived: get event.stream_id, event.data, event.end_stream
          - StreamReset: client reset → close
        """
        from aioquic.quic.events import StreamDataReceived, StreamReset

        if isinstance(event, StreamDataReceived):
            # Schedule an async task to append data to buffer and parse PDUs
            asyncio.ensure_future(
                self.data_received(event.stream_id, event.data, event.end_stream)
            )
        elif isinstance(event, StreamReset):
            # Client reset the QUIC stream: mark session closed
            print(f"[Server] ⚠ Stream {event.stream_id} was reset by client; marking session CLOSED.")
            self.state = STATE_CLOSED

    async def data_received(self, stream_id: int, data: bytes, end_stream: bool):
        """
        Append `data` to the per‐stream buffer, then parse out any complete PDUs.
        If `end_stream` is True and the buffer is empty, we simply mark CLOSED.
        """
        # 1) Append incoming bytes to the buffer for this stream
        buf = self.buffers.setdefault(stream_id, bytearray())
        buf.extend(data)
        self._last_recv = time.time()

        # 2) While we can parse a full PDU (header+body), do so
        while True:
            if len(buf) < HEADER_SIZE:
                # Not enough data for even a header
                break

            # Peek at header
            try:
                hdr = PDUHeader.unpack(bytes(buf[:HEADER_SIZE]))
            except Exception as e:
                print(f"[Server] ❌ Failed to unpack PDU header: {e!r}. Closing connection.")
                self._quic.close()
                return

            total_len = HEADER_SIZE + hdr.body_length
            if len(buf) < total_len:
                # Wait for the full body
                break

            # We have a complete PDU in buf[0:total_len]
            pdu = bytes(buf[:total_len])
            del buf[:total_len]
            body = pdu[HEADER_SIZE:]

            # Dispatch based on hdr.type and current state
            await self.handle_pdu(stream_id, hdr, body)

        # 3) If client closed the QUIC stream (end_stream=True) and buffer is now empty,
        #    then mark state CLOSED if not already closed.
        if end_stream and not buf:
            if self.state not in (STATE_CLOSING, STATE_CLOSED):
                print(f"[Server] ⚠ Client closed stream {stream_id} with no leftover data; marking session CLOSED.")
                self.state = STATE_CLOSED

    async def handle_pdu(self, stream_id: int, hdr: PDUHeader, body: bytes):
        """
        Given a parsed hdr+body from stream_id, dispatch according to our DFA.
        """
        # 1) OPEN_STREAM
        if hdr.type == TYPE_OPEN_STREAM:
            if self.state != STATE_IDLE:
                print(f"[Server] ⚠ Received OPEN_STREAM in state={self.state}. Sending NACK.")
                nack_body = build_open_nack_body(ERR_PROTOCOL_VIOLATION)
                pdu_nack = build_simple_header(TYPE_OPEN_NACK, SESSION_ID, nack_body)
                self._quic.send_stream_data(stream_id, pdu_nack, end_stream=False)
                await asyncio.sleep(0)
                return

            # Try to parse the OPEN_STREAM body
            try:
                codec_name, codec_profile, bitrate_list, enc_flag, mode = parse_open_stream_body(body)
            except Exception as e:
                print(f"[Server] ⚠ Malformed OPEN_STREAM body: {e!r}. Sending NACK.")
                nack_body = build_open_nack_body(ERR_PROTOCOL_VIOLATION)
                pdu_nack = build_simple_header(TYPE_OPEN_NACK, SESSION_ID, nack_body)
                self._quic.send_stream_data(stream_id, pdu_nack, end_stream=False)
                await asyncio.sleep(0)
                return

            print(f"[Server] ▶ Received valid OPEN_STREAM (codec={codec_name}, profile={codec_profile}).")
            self.state = STATE_STREAMING

            # Send OPEN_ACK immediately
            ack_body = build_open_ack_body(
                DEFAULT_BITRATE,
                DEFAULT_CHUNK_SIZE,
                DEFAULT_KEEPALIVE,
                DEFAULT_SESSION_TO
            )
            pdu_ack = build_simple_header(TYPE_OPEN_ACK, SESSION_ID, ack_body)
            self._quic.send_stream_data(stream_id, pdu_ack, end_stream=False)
            print("[Server] ▶ Sent OPEN_ACK.")
            await asyncio.sleep(0)

            # Launch background media streaming
            asyncio.ensure_future(self._stream_media(stream_id))
            return

        # 2) PAUSE_STREAM
        if hdr.type == TYPE_PAUSE_STREAM:
            if self.state != STATE_STREAMING:
                print(f"[Server] ⚠ Received PAUSE_STREAM in state={self.state}. Ignoring.")
                return
            print("[Server] ▶ Received PAUSE_STREAM; transitioning to PAUSED.")
            self.state = STATE_PAUSED
            self.paused = True
            return

        # 3) RESUME_STREAM
        if hdr.type == TYPE_RESUME_STREAM:
            if self.state != STATE_PAUSED:
                print(f"[Server] ⚠ Received RESUME_STREAM in state={self.state}. Ignoring.")
                return
            print("[Server] ▶ Received RESUME_STREAM; transitioning back to STREAMING.")
            self.state = STATE_STREAMING
            self.paused = False
            return

        # 4) CLOSE_STREAM
        if hdr.type == TYPE_CLOSE_STREAM:
            if self.state not in (STATE_STREAMING, STATE_PAUSED):
                print(f"[Server] ⚠ Received CLOSE_STREAM in state={self.state}. Ignoring.")
                return
            print("[Server] ▶ Received CLOSE_STREAM; sending CLOSE_STREAM back and closing.")
            pdu_close = build_empty_body_header(TYPE_CLOSE_STREAM, SESSION_ID)
            self._quic.send_stream_data(stream_id, pdu_close, end_stream=True)
            await asyncio.sleep(0)
            self.state = STATE_CLOSED
            return

        # 5) HEARTBEAT
        if hdr.type == TYPE_HEARTBEAT:
            print("[Server] ▶ Received HEARTBEAT; updating timeout timer.")
            return

        # 6) Unexpected PDU
        print(f"[Server] ⚠ Unexpected PDU type={hdr.type} in state={self.state}. Ignoring.")

        # Always check idle-timeout
        if time.time() - self._last_recv > self.session_timeout:
            print("[Server] ⏱ Session timed out due to inactivity; closing.")
            self.state = STATE_CLOSED
            self._quic.close()

    async def _stream_media(self, stream_id: int):
        """
        While in STATE_STREAMING, read from media_path in chunks and send STREAM_CHUNK PDUs.
        When EOF, send CLOSE_STREAM and stop.
        """
        try:
            with open(self.media_path, "rb") as f:
                while True:
                    if self.state != STATE_STREAMING:
                        # If paused or out of state, sleep briefly and check again
                        await asyncio.sleep(0.1)
                        continue

                    chunk = f.read(DEFAULT_CHUNK_SIZE)
                    if not chunk:
                        # End of file reached → send CLOSE_STREAM
                        pdu_close = build_empty_body_header(TYPE_CLOSE_STREAM, SESSION_ID)
                        self._quic.send_stream_data(stream_id, pdu_close, end_stream=True)
                        print("[Server] ▶ EOF reached; sent CLOSE_STREAM and closed stream.")
                        await asyncio.sleep(0)
                        self.state = STATE_CLOSED
                        break

                    body = build_stream_chunk_body(self.seq_num, chunk)
                    pdu   = build_simple_header(TYPE_STREAM_CHUNK, SESSION_ID, body)
                    self._quic.send_stream_data(stream_id, pdu, end_stream=False)
                    print(f"[Server] ▶ Sent STREAM_CHUNK seq={self.seq_num}, {len(chunk)} bytes.")
                    await asyncio.sleep(0)

                    self.seq_num += 1
                    # Slight pacing
                    await asyncio.sleep(0.05)

        except FileNotFoundError:
            print(f"[Server] ⚠ Media file '{self.media_path}' not found; closing session.")
            self.state = STATE_CLOSED


async def main():
    parser = argparse.ArgumentParser(description="QUIC Streaming Server")
    parser.add_argument(
        "--port", "-p", type=int, default=DEFAULT_PORT,
        help=f"UDP port to bind (default {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--media", "-m", type=str, default=DEFAULT_MEDIA_PATH,
        help="Path to the media file to stream (default 'sample_media.bin')"
    )
    args = parser.parse_args()

    # ─── 1) Generate a self‐signed cert if none exists, using a temporary OpenSSL config ───
    cert_path = pathlib.Path("quic_cert.pem")
    key_path  = pathlib.Path("quic_key.pem")

    if not cert_path.exists() or not key_path.exists():
        print("[Server] 🔐 No certificate found; generating self-signed certificate with SANs...")

        # Build a temporary OpenSSL config file to include SANs
        with tempfile.NamedTemporaryFile("w", delete=False) as tmp_conf:
            san_conf_path = tmp_conf.name
            tmp_conf.write(f"""\
[req]
distinguished_name = req_distinguished_name
x509_extensions    = v3_req
prompt             = no

[req_distinguished_name]
commonName = localhost

[v3_req]
keyUsage = keyEncipherment, dataEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = localhost
IP.1  = 127.0.0.1
""")

        try:
            subprocess.run([
                "openssl", "req",
                "-newkey", "rsa:2048",
                "-nodes",
                "-days", "365",
                "-x509",
                "-keyout", str(key_path),
                "-out", str(cert_path),
                "-config", san_conf_path,
                "-extensions", "v3_req"
            ], check=True)
            print(f"[Server] 🔐 Certificate generated: {cert_path} & {key_path}")
        except Exception as e:
            print(f"[Server] ❌ Failed to generate certificate: {e!r}", file=sys.stderr)
            os.unlink(san_conf_path)
            return
        finally:
            os.unlink(san_conf_path)

    # ─── 2) QUIC server configuration ──────────────────────────────────────────────
    configuration = QuicConfiguration(
        is_client=False,
        alpn_protocols=["quic-stream-proto"],
        max_datagram_frame_size=65536
    )
    try:
        configuration.load_cert_chain(str(cert_path), str(key_path))
    except Exception as e:
        print(f"[Server] ❌ Failed to load cert chain: {e!r}", file=sys.stderr)
        return

    # ─── 3) Ensure media file exists (create dummy if missing) ─────────────────────
    if not os.path.exists(args.media):
        print(f"[Server] 📁 Creating dummy media file: {args.media}")
        try:
            with open(args.media, "wb") as f:
                f.write(os.urandom(512 * 1024))  # 512 KiB of random data
        except Exception as e:
            print(f"[Server] ❌ Could not create media file: {e!r}", file=sys.stderr)
            return

    # ─── 4) Bind and serve ─────────────────────────────────────────────────────────
    try:
        print(f"[Server] 🎯 Binding to UDP port {args.port} and listening for QUIC connections…")
        server = await serve(
            host="0.0.0.0",
            port=args.port,
            configuration=configuration,
            create_protocol=lambda *a, **k: StreamingServerProtocol(*a, media_path=args.media, **k)
        )

        # ─── 5) BLOCK FOREVER (or until Ctrl+C) ───────────────────────────────────
        print(f"[Server] ✅ QUIC server is up and running on port {args.port}. Press Ctrl+C to stop.")
        await asyncio.Event().wait()

    except Exception as e:
        print(f"[Server] ❌ serve() threw an exception: {e!r}", file=sys.stderr)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Server] 🔌 Interrupted; shutting down.")
        sys.exit(0)












