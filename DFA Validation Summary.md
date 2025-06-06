**DFA Validation Summary**


### Server-Side DFA

* **States**

  1. `STATE_IDLE` (0)
  2. `STATE_STREAMING` (1)
  3. `STATE_PAUSED` (2)
  4. `STATE_CLOSED` (4)

* **Valid Transitions**

  1. **`STATE_IDLE` → (OPEN\_STREAM) → `STATE_STREAMING`**

     * On `OPEN_STREAM` PDU, parse its body. If valid, send `OPEN_ACK` and set `state = STREAMING`.
     * If an `OPEN_STREAM` arrives in any state ≠ `IDLE`, send `OPEN_NACK(ERR_PROTOCOL_VIOLATION)` and remain in `IDLE`.
  2. **`STATE_STREAMING` → (PAUSE\_STREAM) → `STATE_PAUSED`**

     * On `PAUSE_STREAM` PDU, if currently `STREAMING`, set `state = PAUSED`. Otherwise ignore.
  3. **`STATE_PAUSED` → (RESUME\_STREAM) → `STATE_STREAMING`**

     * On `RESUME_STREAM` PDU, if currently `PAUSED`, set `state = STREAMING`. Otherwise ignore.
  4. **`STATE_STREAMING` or `STATE_PAUSED` → (CLOSE\_STREAM) → `STATE_CLOSED`**

     * On `CLOSE_STREAM` PDU, if in `STREAMING` or `PAUSED`, send back a `CLOSE_STREAM` and set `state = CLOSED`. Otherwise ignore.
  5. **`STATE_STREAMING` → (STREAM\_CHUNK sending)**

     * Once in `STREAMING`, the server’s `_stream_media` loop repeatedly sends `STREAM_CHUNK` PDUs. Pausing is enforced by checking the `state` flag before each chunk.
  6. **`STATE_CLOSED`**

     * No further PDUs are processed once in `CLOSED`. Any additional data or resets simply keep it closed.

* **Idle-Timeout Check**

  * After every received PDU, update a “last‐receive” timestamp. If no PDU arrives within `session_timeout` seconds, forcibly set `state = CLOSED`.

* **Malformed / Unexpected PDU Handling**

  * If a PDU arrives that doesn’t match the current state (e.g. `PAUSE_STREAM` in `IDLE`), it’s logged and ignored.
  * If an `OPEN_STREAM` is malformed or arrives out of order, send `OPEN_NACK(ERR_PROTOCOL_VIOLATION)` and do not change state.

---

### Client-Side DFA

* **States**

  1. `CLIENT_STATE_IDLE` (0)
  2. `CLIENT_STATE_OPEN_SENT` (1)
  3. `CLIENT_STATE_STREAMING` (2)
  4. `CLIENT_STATE_PAUSED` (3)
  5. `CLIENT_STATE_CLOSED` (5)

* **Valid Transitions**

  1. **`CLIENT_STATE_IDLE` → (send OPEN\_STREAM) → `CLIENT_STATE_OPEN_SENT`**

     * Immediately upon connection, build and send an `OPEN_STREAM` PDU and set `state = OPEN_SENT`.
  2. **`CLIENT_STATE_OPEN_SENT` → (receive OPEN\_ACK) → `CLIENT_STATE_STREAMING`**

     * On receiving a valid `OPEN_ACK` PDU (and only if in `OPEN_SENT`), record parameters (bitrate, keepalive, etc.), set `state = STREAMING`.
  3. **`CLIENT_STATE_OPEN_SENT` → (receive OPEN\_NACK) → `CLIENT_STATE_CLOSED`**

     * If an `OPEN_NACK` arrives in `OPEN_SENT`, set `state = CLOSED` and terminate.
  4. **`CLIENT_STATE_STREAMING` → (receive STREAM\_CHUNK) → stay in `STREAMING`**

     * Each `STREAM_CHUNK` is printed.
     * If `seq == 3`, the client sends `PAUSE_STREAM` and sets `state = PAUSED`.
  5. **`CLIENT_STATE_PAUSED` → (after 2 s delay, send RESUME\_STREAM) → `CLIENT_STATE_STREAMING`**

     * The client automatically waits 2 seconds, sends `RESUME_STREAM`, and sets `state = STREAMING`. Any chunks that arrived while paused are discarded.
  6. **Any State in (`STREAMING`, `PAUSED`) → (receive CLOSE\_STREAM) → `CLIENT_STATE_CLOSED`**

     * On `CLOSE_STREAM`, print message and set `state = CLOSED`.
  7. **`CLIENT_STATE_STREAMING` → (2×keepalive without chunk) → assume session end**

     * If no `STREAM_CHUNK` arrives within `2 × keepalive_interval`, assume the server closed abruptly; send `CLOSE_STREAM` for cleanup and set `state = CLOSED`.

* **Heartbeat Task**

  * While in `STREAMING`, a background coroutine sends a `HEARTBEAT` PDU every `keepalive_interval` seconds.

* **Malformed / Unexpected PDU Handling**

  * PDUs that do not match the current state (e.g. `STREAM_CHUNK` when in `OPEN_SENT`) are ignored.
  * Once in `CLOSED`, no further PDUs are processed.

---

### “DFA Validation” in Code

1. **State Variables**

   * The server uses `self.state` (an integer) to represent its DFA state.
   * The client uses `self.state` in `StreamingClientProtocol` to track its DFA state.

2. **Conditional Dispatch**

   * On each received PDU, code checks:

     ```python
     if hdr.type == TYPE_OPEN_STREAM and self.state == STATE_IDLE:
         …     # valid
     elif hdr.type == TYPE_OPEN_STREAM:
         …     # out of order → NACK or ignore
     ```

     and similarly for each PDU type.

3. **State Transitions Only When Correct**

   * Each handler changes state only when an expected PDU arrives in the correct source state.
   * If a PDU arrives in the wrong state, it logs a warning and does not transition.

4. **Streaming Loop & Pause Logic**

   * The server’s `_stream_media()` checks `self.state == STATE_STREAMING` before sending each chunk. If `self.state` becomes `STATE_PAUSED`, it stalls until `STATE_STREAMING` returns.
   * The client’s handler for `STREAM_CHUNK` forcibly pauses when `seq == 3`, setting `state = PAUSED` and scheduling a resume in 2 seconds.

5. **Timeouts & Closure**

   * Both sides track “last‐receive” time. If the session remains idle beyond the timeout, they set state to CLOSED and tear down.
   * On closing, they send/acknowledge `CLOSE_STREAM` and refuse any further PDUs.

---

#### In Short

Every PDU handler begins by checking “Am I in the right state for this PDU?” If yes, it processes and transitions. If not, it either NACKs (`OPEN_STREAM` out of order on server) or ignores. This strict checking—combined with state variables and timed heartbeats/timeouts—ensures the implementation faithfully validates the DFA.
