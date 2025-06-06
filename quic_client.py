import argparse
import asyncio
import ssl
import sys
import time

from aioquic.asyncio import connect, QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration

from protocol import (
    HEADER_SIZE,
    TYPE_OPEN_STREAM, TYPE_OPEN_ACK, TYPE_OPEN_NACK,
    TYPE_STREAM_CHUNK, TYPE_PAUSE_STREAM, TYPE_RESUME_STREAM,
    TYPE_CLOSE_STREAM, TYPE_HEARTBEAT,
    build_open_stream_body, build_simple_header,
    parse_open_ack_body, parse_open_nack_body,
    parse_stream_chunk_body, build_heartbeat_body,
    CLIENT_STATE_IDLE, CLIENT_STATE_OPEN_SENT, CLIENT_STATE_STREAMING,
    CLIENT_STATE_PAUSED, CLIENT_STATE_CLOSING, CLIENT_STATE_CLOSED,
    SESSION_ID, PDUHeader
)


class StreamingClientProtocol(QuicConnectionProtocol):
    """
    Implements the client-side DFA and handles QUIC events.  
    - On connection, opens a bidirectional stream and sends OPEN_STREAM.  
    - Processes incoming StreamDataReceived events on that stream.  
    - Automatically PAUSEs after seq == 3, then RESUME after 2 seconds.  
    - Sends HEARTBEATs in the background while in STREAMING.  
    - Signals completion via a Future (self._done).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.state = CLIENT_STATE_IDLE
        self.keepalive_interval = 0
        self.last_chunk_time = time.time()
        self.stream_id = None
        self.recv_buffer = b""
        self._done = asyncio.get_event_loop().create_future()

    def connection_made(self, transport):
        """
        Called when the QUIC connection is established.  
        We pick a new bidirectional stream ID, send OPEN_STREAM, and transition to OPEN_SENT.
        """
        super().connection_made(transport)

        # 1) Open a bidirectional stream
        self.stream_id = self._quic.get_next_available_stream_id(is_unidirectional=False)

        # 2) Build & send OPEN_STREAM PDU
        codec_name      = "h264"
        codec_profile   = "baseline"
        bitrate_list    = [240_000, 480_000]
        encryption_flag = 0
        delivery_mode   = 0

        body = build_open_stream_body(
            codec_name,
            codec_profile,
            bitrate_list,
            encryption_flag,
            delivery_mode
        )
        open_pdu = build_simple_header(TYPE_OPEN_STREAM, 0, body)
        self._quic.send_stream_data(self.stream_id, open_pdu, end_stream=False)
        # let QUIC process outgoing frames
        asyncio.ensure_future(self._flush())

        self.state = CLIENT_STATE_OPEN_SENT
        print(f"[Client] ▶ Sent OPEN_STREAM; now in state OPEN_SENT.")

        # 3) Spawn heartbeat task
        asyncio.ensure_future(self.send_heartbeats())

    async def _flush(self):
        """
        Allow the QUIC engine to process outgoing frames.
        """
        await asyncio.sleep(0)

    def quic_event_received(self, event):
        """
        Called by aioquic whenever QUIC-level events arrive.  
        We only care about StreamDataReceived (for our chosen stream) and StreamReset / ConnectionTerminated.
        """
        from aioquic.quic.events import StreamDataReceived, StreamReset, ConnectionTerminated

        if isinstance(event, StreamDataReceived) and event.stream_id == self.stream_id:
            # Accumulate incoming data in recv_buffer, then attempt to parse PDUs
            self.recv_buffer += event.data
            self._last_recv = time.time()
            self._process_pdus()

        elif isinstance(event, StreamReset) and event.stream_id == self.stream_id:
            # Server forcibly reset our stream → end immediately
            self.state = CLIENT_STATE_CLOSED
            if not self._done.done():
                self._done.set_result(None)

        elif isinstance(event, ConnectionTerminated):
            # Connection fully terminated → ensure we finish
            if not self._done.done():
                self._done.set_result(None)

    def _process_pdus(self):
        """
        Parse any complete PDUs in self.recv_buffer. Each PDU = HEADER_SIZE + hdr.body_length.
        Dispatch them according to our DFA.
        """
        while True:
            if len(self.recv_buffer) < HEADER_SIZE:
                return

            try:
                hdr = PDUHeader.unpack(self.recv_buffer[:HEADER_SIZE])
            except Exception:
                # Malformed header—abort
                self.state = CLIENT_STATE_CLOSED
                if not self._done.done():
                    self._done.set_result(None)
                return

            total_len = HEADER_SIZE + hdr.body_length
            if len(self.recv_buffer) < total_len:
                # Wait for full body
                return

            # We have a complete PDU
            pdu_bytes = self.recv_buffer[:total_len]
            self.recv_buffer = self.recv_buffer[total_len:]
            body = pdu_bytes[HEADER_SIZE:]

            # ─── DISPATCH BASED ON hdr.type ───────────────────────────

            if hdr.type == TYPE_OPEN_ACK and self.state == CLIENT_STATE_OPEN_SENT:
                chosen_bitrate, max_chunk_size, keepalive_interval, session_timeout = parse_open_ack_body(body)
                print(f"[Client] ◀ OPEN_ACK: bitrate={chosen_bitrate}, chunk_size={max_chunk_size}, keepalive={keepalive_interval}, timeout={session_timeout}")
                self.keepalive_interval = keepalive_interval
                self.last_chunk_time = time.time()
                self.state = CLIENT_STATE_STREAMING

            elif hdr.type == TYPE_OPEN_NACK and self.state == CLIENT_STATE_OPEN_SENT:
                err_code = parse_open_nack_body(body)
                print(f"[Client] ◀ OPEN_NACK (error={err_code}); exiting.")
                self.state = CLIENT_STATE_CLOSED
                if not self._done.done():
                    self._done.set_result(None)
                return

            elif hdr.type == TYPE_STREAM_CHUNK and self.state == CLIENT_STATE_STREAMING:
                seq_num, payload = parse_stream_chunk_body(body)
                print(f"[Client] ◀ STREAM_CHUNK seq={seq_num}, {len(payload)} bytes.")
                self.last_chunk_time = time.time()

                # Automatically PAUSE after seq == 3, then RESUME after 2s
                if seq_num == 3:
                    print("[Client] ▶ Sending PAUSE_STREAM (seq==3).")
                    pdu_pause = build_simple_header(TYPE_PAUSE_STREAM, SESSION_ID, b"")
                    self._quic.send_stream_data(self.stream_id, pdu_pause, end_stream=False)
                    asyncio.ensure_future(self._flush())
                    self.state = CLIENT_STATE_PAUSED

                    # Schedule RESUME in 2 seconds
                    asyncio.ensure_future(self._resume_after_delay(2))

            elif hdr.type == TYPE_CLOSE_STREAM and self.state in (CLIENT_STATE_STREAMING, CLIENT_STATE_PAUSED):
                print("[Client] ◀ CLOSE_STREAM from server; transitioning to CLOSED.")
                self.state = CLIENT_STATE_CLOSED
                if not self._done.done():
                    self._done.set_result(None)
                return

            elif hdr.type == TYPE_HEARTBEAT:
                print(f"[Client] ◀ HEARTBEAT from server.")

            else:
                # Unexpected or out‐of‐order PDU: ignore
                pass

            # Check if no STREAM_CHUNK within 2×keepalive_interval → assume done
            if self.state == CLIENT_STATE_STREAMING and (time.time() - self.last_chunk_time) > (self.keepalive_interval * 2):
                print("[Client] ⚠ No STREAM_CHUNK for a while; assuming session ended.")
                self.state = CLIENT_STATE_CLOSED
                if not self._done.done():
                    self._done.set_result(None)
                return

    async def _resume_after_delay(self, delay: float):
        """
        After `delay` seconds, send RESUME_STREAM PDU if still paused.
        """
        await asyncio.sleep(delay)
        if self.state == CLIENT_STATE_PAUSED:
            print("[Client] ▶ Sending RESUME_STREAM after 2s pause.")
            pdu_resume = build_simple_header(TYPE_RESUME_STREAM, SESSION_ID, b"")
            self._quic.send_stream_data(self.stream_id, pdu_resume, end_stream=False)
            await asyncio.sleep(0)
            self.state = CLIENT_STATE_STREAMING

    async def send_heartbeats(self):
        """
        While in CLIENT_STATE_STREAMING, send a HEARTBEAT every keepalive_interval seconds.
        If keepalive_interval is still 0, default to 5 seconds.
        """
        while True:
            if self.state == CLIENT_STATE_STREAMING:
                body = build_heartbeat_body()
                pdu = build_simple_header(TYPE_HEARTBEAT, SESSION_ID, body)
                self._quic.send_stream_data(self.stream_id, pdu, end_stream=False)
                await asyncio.sleep(0)
                print("[Client] ▶ Sent HEARTBEAT.")
            await asyncio.sleep(self.keepalive_interval or 5)

    def connection_lost(self, exc):
        """
        Called when the QUIC connection is fully closed. Ensure our _done future is set.
        """
        super().connection_lost(exc)
        if not self._done.done():
            self._done.set_result(None)


async def run_client(host: str, port: int):
    """
    1) Disable TLS verification (verify_mode=ssl.CERT_NONE).
    2) Connect with our StreamingClientProtocol.
    3) Await the protocol’s `_done` future, then close.
    """
    configuration = QuicConfiguration(
        is_client=True,
        alpn_protocols=["quic-stream-proto"],
        verify_mode=ssl.CERT_NONE
    )

    print(f"[Client] 🔗 Connecting to {host}:{port} over QUIC (no TLS verify)…")
    try:
        # Use create_protocol=StreamingClientProtocol so that our subclass is instantiated.
        async with connect(
            host,
            port,
            configuration=configuration,
            create_protocol=StreamingClientProtocol
        ) as client_proto:
            # Wait until the client protocol signals it’s done (either CLOSED or ConnectionTerminated)
            await client_proto._done

            # If we haven’t already closed the stream, send CLOSE_STREAM
            if client_proto.state not in (CLIENT_STATE_CLOSED, CLIENT_STATE_CLOSING):
                print("[Client] ▶ Sending CLOSE_STREAM to server for cleanup.")
                pdu_close = build_simple_header(TYPE_CLOSE_STREAM, SESSION_ID, b"")
                client_proto._quic.send_stream_data(client_proto.stream_id, pdu_close, end_stream=True)
                await asyncio.sleep(0)

            # Give QUIC a moment to flush anything outstanding
            await asyncio.sleep(0.5)
            client_proto._quic.close()
            print("[Client] 🔌 Done; connection closed.")

    except ConnectionError as ce:
        print(f"[Client] ❌ ConnectionError: {ce!r}. Is the server running on {host}:{port}?")
    except Exception as e:
        print(f"[Client] ❌ Unexpected error: {e!r}")


def main():
    parser = argparse.ArgumentParser(description="QUIC Streaming Client")
    parser.add_argument(
        "--host", "-H", type=str, default="127.0.0.1",
        help="Server hostname or IP (default 127.0.0.1)"
    )
    parser.add_argument(
        "--port", "-p", type=int, default=9000,
        help="Server UDP port (default 9000)"
    )
    args = parser.parse_args()

    try:
        asyncio.run(run_client(args.host, args.port))
    except KeyboardInterrupt:
        print("\n[Client] 🔌 Interrupted; exiting.")
        sys.exit(0)


if __name__ == "__main__":
    main()






