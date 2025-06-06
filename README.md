# QUIC Streaming Assignment

## Overview

This project implements a simple stateful streaming protocol over QUIC using Python and the `aioquic` library. It consists of:

1. **protocol.py**
   Defines PDU formats, DFA states, and helper functions to build/parse PDUs.

2. **quic\_server.py**
   A QUIC-based server that:

   * Listens on a hardcoded UDP port (default 9000, configurable via `--port`).
   * Generates a self-signed certificate (with SAN for localhost/127.0.0.1) if none exists.
   * Implements a server-side DFA:
     • `STATE_IDLE` → (OPEN\_STREAM) → `STATE_STREAMING` → (PAUSE\_STREAM) → `STATE_PAUSED` → (RESUME\_STREAM) → `STATE_STREAMING` → (CLOSE\_STREAM) → `STATE_CLOSED`.
   * Streams a dummy media file (`sample_media.bin`) in 64 KiB chunks once an OPEN\_STREAM is accepted.
   * Pauses, resumes, and closes sessions according to client PDUs.
   * Times out after 30 seconds of inactivity.
   * Blocks indefinitely after binding so it doesn’t exit until you press Ctrl +C.

3. **quic\_client.py**
   A QUIC-based client that:

   * Connects to the server (hostname/IP and port configurable via command-line).
   * Disables TLS verification (for simplicity).
   * Implements a client-side DFA:
     • `CLIENT_STATE_IDLE` → (OPEN\_STREAM) → `CLIENT_STATE_OPEN_SENT` → `CLIENT_STATE_STREAMING` → (PAUSE\_STREAM at seq = 3) → `CLIENT_STATE_PAUSED` → (RESUME\_STREAM after 2 s) → `CLIENT_STATE_STREAMING` → (CLOSE\_STREAM) → `CLIENT_STATE_CLOSED`.
   * Prints each received STREAM\_CHUNK PDU (with sequence number and payload size).
   * After 2 × keepalive interval of inactivity, assumes session ended.
   * Sends HEARTBEAT PDUs every keepalive interval while streaming.
   * Cleans up by sending CLOSE\_STREAM if needed and closes the QUIC connection.

---

## Prerequisites

1. **Python 3.8+** (tested with 3.9 and 3.10).
2. **aioquic** library. Install via:

   ```
   pip install aioquic
   ```
3. **OpenSSL** (for certificate generation). On macOS, the system’s `openssl` is used with a temporary config (no `-addext` needed).

---

## Files in Directory

* **protocol.py**
  Contains all PDU constants (type codes, error codes), `PDUHeader` class, functions to build/parse PDU bodies, and DFA state constants.

* **quic\_server.py**
  The server implementation.
  **Usage:**

  ```
  python quic_server.py [--port <port>] [--media <media_file>]
  ```

  Defaults:

  * `--port 9000`
  * `--media sample_media.bin`

  The server creates a self-signed `quic_cert.pem` and `quic_key.pem` (if missing), with SANs for `localhost` and `127.0.0.1`. It also creates a dummy `sample_media.bin` (512 KiB of random data) if not present.

* **quic\_client.py**
  The client implementation.
  **Usage:**

  ```
  python quic_client.py [--host <hostname_or_ip>] [--port <port>]
  ```

  Defaults:

  * `--host 127.0.0.1`
  * `--port 9000`

  The client disables TLS verification, opens a bidirectional QUIC stream, sends OPEN\_STREAM, prints any received PDUs, implements automatic PAUSE/RESUME, and cleans up.

---

## How to Run

1. **Open a terminal** and navigate to the project directory:

   ```
   cd ~/Downloads/cs544_final
   ```

2. **Ensure no old certificates** (so the server generates a fresh SAN-correct cert):

   ```
   rm quic_cert.pem quic_key.pem 2>/dev/null
   ```

3. **Start the server** in Terminal A:

   ```
   python quic_server.py
   ```

   You should see:

   ```
   [Server] 🔐 No certificate found; generating self-signed certificate with SANs...
   [Server] 🔐 Certificate generated: quic_cert.pem & quic_key.pem
   [Server] 📁 Creating dummy media file: sample_media.bin
   [Server] 🎯 Binding to UDP port 9000 and listening for QUIC connections…
   [Server] ✅ QUIC server is up and running on port 9000. Press Ctrl+C to stop.
   ```

   Leave this terminal open.

4. **Open a second terminal** (Terminal B) in the same directory and run the client:

   ```
   python quic_client.py
   ```

   You should see (example):

   ```
   [Client] 🔗 Connecting to 127.0.0.1:9000 over QUIC (no TLS verify)…
   [Client] ▶ Sent OPEN_STREAM; now in state OPEN_SENT.
   [Client] ◀ OPEN_ACK: bitrate=500000, chunk_size=65536, keepalive=5, timeout=30
   [Client] ◀ STREAM_CHUNK seq=1, 65536 bytes.
   [Client] ◀ STREAM_CHUNK seq=2, 65536 bytes.
   [Client] ◀ STREAM_CHUNK seq=3, 65536 bytes.
   [Client] ▶ Sending PAUSE_STREAM (seq==3).
   [Client] ▶ Sending RESUME_STREAM after 2 s pause.
   [Client] ◀ STREAM_CHUNK seq=4, 65536 bytes.
   …
   [Client] ◀ CLOSE_STREAM from server; transitioning to CLOSED.
   [Client] 🔌 Done; connection closed.
   ```

5. **Stop the server** by pressing **Ctrl + C** in Terminal A:

   ```
   [Server] 🔌 Interrupted; shutting down.
   ```

---

## Configuration

* **Server port:** Change via `--port <port>` (both server and client must use the same port).
* **Server media file:** Change via `--media <filepath>`. By default, a 512 KiB random file named `sample_media.bin` is created if missing.
* **Client host/port:** Change via `--host <hostname_or_ip> --port <port>` if the server is on a different machine or port.
* **Protocol DFA:**

  * **Server states:** `STATE_IDLE` (0), `STATE_STREAMING` (1), `STATE_PAUSED` (2), `STATE_CLOSING` (3), `STATE_CLOSED` (4).
  * **Client states:** `CLIENT_STATE_IDLE` (0), `CLIENT_STATE_OPEN_SENT` (1), `CLIENT_STATE_STREAMING` (2), `CLIENT_STATE_PAUSED` (3), `CLIENT_STATE_CLOSING` (4), `CLIENT_STATE_CLOSED` (5).

---

## Protocol Summary

1. **Client** builds and sends an `OPEN_STREAM` PDU (type 1).
2. **Server** (in `STATE_IDLE`) receives `OPEN_STREAM`, transitions to `STATE_STREAMING`, and replies with `OPEN_ACK` (type 2).
3. **Client** (in `STATE_OPEN_SENT`) receives `OPEN_ACK`, transitions to `STATE_STREAMING`.
4. **Server** begins sending `STREAM_CHUNK` PDUs (type 4) in sequence (seq 1, 2, 3, …).
5. **Client** receives each `STREAM_CHUNK` while in `STATE_STREAMING`. When it sees `seq == 3`, it sends `PAUSE_STREAM` (type 5), transitions to `STATE_PAUSED`, then sleeps 2 s, sends `RESUME_STREAM` (type 6), and returns to `STATE_STREAMING`.
6. **Server** pauses streaming while in `STATE_PAUSED` and resumes once it sees `RESUME_STREAM`.
7. Once the media file is exhausted, **Server** sends `CLOSE_STREAM` (type 7) and transitions to `STATE_CLOSED`.
8. **Client** (in `STATE_STREAMING` or `STATE_PAUSED`) receives `CLOSE_STREAM`, transitions to `CLIENT_STATE_CLOSED`, and closes.
9. **HEARTBEAT** PDUs (type 8) are exchanged:

   * **Client** sends a heartbeat every keepalive interval while streaming.
   * **Server** updates its last-receive time on any heartbeat.

---

## Error Handling

* If the server receives an `OPEN_STREAM` out of order (not in `STATE_IDLE`), it replies with `OPEN_NACK` (type 3, error 99) and remains in `STATE_IDLE`.
* Malformed PDU bodies lead to an immediate `OPEN_NACK(ERR_PROTOCOL_VIOLATION)`.
* Any unexpected PDU at the wrong state is ignored (with a warning).
* If either side is idle for more than the session timeout (30 s by default), the session is closed.

---

## Notes

* The client disables TLS verification for simplicity. In a production scenario, you would verify the server’s certificate chain.
* The server’s self-signed certificate includes SANs for `localhost` and `127.0.0.1` so that TLS verification would work if enabled.
* For testing on different machines, copy `quic_cert.pem` from the server directory to the client’s directory and remove `verify_mode=ssl.CERT_NONE` so the client can verify it.

---

## Support

If you encounter any issues, make sure:

* Both server and client are run from the same directory (so they can find `protocol.py` and `quic_cert.pem`).
* No other process is blocking or using UDP port 9000.
* You have installed `aioquic` (`pip install aioquic`).
* You allow UDP traffic through any local firewall on port 9000.


