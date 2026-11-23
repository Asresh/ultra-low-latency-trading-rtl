# Day 07 · OUCH Order Session

**SystemVerilog RTL · OUCH 5.0 order entry · SoupBinTCP 4.10 framing · replay checkpoint**

This core accepts normalized orders and emits exact SoupBinTCP frames for a single logical OUCH account. It validates and reconciles replies, tracks partially filled order chains, and retains the session cursor and unresolved command journal when the TCP adapter drops a socket. The network boundary is a ready/valid byte stream after TCP. The simulations use a synthetic peer.

Wire layouts follow Nasdaq's [OUCH 5.0 Order Entry Specification](https://www.nasdaqtrader.com/content/technicalsupport/specifications/TradingProducts/Ouch5.0.pdf), sections 1–3, and [SoupBinTCP 4.10](https://www.nasdaqtrader.com/content/technicalsupport/specifications/tradingproducts/SoupBinTCP41.pdf), sections 1–2.

## Architecture

```mermaid
flowchart LR
    App["Normalized<br/>commands and queries"] --> Table["Indexed order slots<br/>associative response lookup"]
    Table --> TX["Soup prefix<br/>64-bit byte serializer"]
    TX --> Adapter["TCP byte adapter"]
    Adapter --> RX["16-byte reservoir<br/>bounded frame assembly"]
    RX --> Wire["Type, length and<br/>appendage fences"]
    Wire --> Session["Session cursor and<br/>replay decision"]
    Session --> Table
    Session <-.-> Login["Iterative 64-bit decimal<br/>login conversion"]
    Table --> Notice["Ordered normalized<br/>notices and metrics"]
    Session --> Notice
    classDef io fill:#e0f2fe,stroke:#0369a1,color:#082f49
    classDef state fill:#ede9fe,stroke:#6d28d9,color:#2e1065
    classDef codec fill:#dbeafe,stroke:#1d4ed8,color:#172554
    classDef audit fill:#dcfce7,stroke:#15803d,color:#052e16
    class App,Adapter io
    class Table,Session state
    class TX,RX,Wire,Login codec
    class Notice audit
```

The RX reservoir keeps up to 16 bytes and consumes up to eight bytes on a body assembly clock. Multiple Soup packets may share an input beat or span any number of short beats. Each input lane with `keep[lane]` high contributes `data[8*lane +: 8]`; selected lanes must be nonempty and contiguous from lane zero. The serialized TX stream starts each frame in lane zero, drives `m_tx_last` on the final beat, and holds the beat under backpressure. A frame's big-endian 16-bit length counts its Soup type byte plus payload. The prefix itself is outside that count.

A complete validated peer packet gets priority over local management and commands. A decoded order update commits atomically with its normalized notice and sequence change. A stalled notice blocks further commits. No header, body fragment, or partial decimal field can mutate order state. Slot occupancy, command journal, and replay checkpoint remain in registers when the transport flushes.

### Login and replay

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Offline
    Offline --> LoginTx: valid local login
    LoginTx --> LoginRx: final L byte transfers
    LoginRx --> Replay: A starts below checkpoint
    LoginRx --> Active: A starts at checkpoint
    Replay --> Replay: count S but drop old position
    Replay --> Active: socket cursor catches up
    Active --> Active: apply next sequenced event
    Active --> Ended: peer Z
    LoginRx --> Fault: bad login or forward gap
    Active --> Fault: malformed or invalid event
    Fault --> Offline: socket down
    Active --> Offline: socket down
    Replay --> Offline: socket down
    Ended --> Offline: session reset
    classDef good fill:#dcfce7,stroke:#15803d,color:#052e16
    classDef transmit fill:#dbeafe,stroke:#1d4ed8,color:#172554
    classDef caution fill:#fef3c7,stroke:#a16207,color:#422006
    classDef fail fill:#fee2e2,stroke:#b91c1c,color:#450a0a
    class Active good
    class Offline,LoginTx,LoginRx transmit
    class Replay caution
    class Fault,Ended fail
```

The initial request starts at sequence 1 and can specify a blank session ID. The first accepted login pins the peer's session. A reconnect specifies that same ID and a sequence no greater than the committed next sequence. The accept packet's decimal sequence sets the new socket cursor. Earlier same-session positions receive a replay notice and advance only the socket cursor. The decoder checks their framing and type before skipping them. The implementation trusts the authenticated peer to supply the same history for that session and position.

A forward jump, wrong session ID, invalid decimal text, or 64-bit cursor overflow closes the transport and retains the committed state. When caught up, the next valid sequenced event advances both cursors by one. Debug and heartbeat packets do not change either cursor. A peer end-of-session packet stops admission until reset. An operator logout emits a complete `O` frame before raising `close_transport` and retains eligibility to reconnect to the same session.

The separate decimal converter takes 64 clocks for binary-to-ASCII and 20 clocks for checked ASCII-to-binary. Login traffic is off the active order update path. A valid `time_tick` advances client and server idle counters, with one unit selected by the integration. With millisecond ticks, the defaults request a client heartbeat after 1,000 idle ticks and declare peer inactivity after 5,000. A zero parameter disables its timer. Client idle time counts when the serializer can take work, and RX holds off timeout while input bytes or a completed frame are pending.

## Wire subset

Lengths below are the Soup length field. OUCH integers are network-endian. Fixed-width text is transmitted in its specified byte order. Prices use four implied decimal places. The supported positive limit range ends at 1,999,999,900 integer units.

| Direction | Soup | OUCH or control | Length | Supported encoding |
| --- | --- | --- | ---: | --- |
| Client to peer | `L` | Login | 52 | User 6 bytes, password 10, requested session 10, decimal cursor 20, timeout milliseconds 5 |
| Client to peer | `U` | Enter `O` | 48 | Buy/sell, positive limit, fixed principal capacity, non-ISO, continuous market, zero appendage |
| Client to peer | `U` | Replace `U` | 41 | Original and replacement references, whole-chain liability, inherited side and symbol, zero appendage |
| Client to peer | `U` | Cancel `X` | 10 | Current reference and intended total shares, optional appendage absent |
| Client to peer | `R`, `O` | Heartbeat, logout | 1 | No payload |
| Peer to client | `A`, `J` | Login accept, reject | 31, 2 | Session and starting cursor, or one-byte reason |
| Peer to client | `H`, `Z`, `+` | Heartbeat, end, debug | 1, 1, variable | Debug bytes ignored within the 72-byte body cap |
| Peer to client | `S` | System `S`, accepted `A`, replaced `U` | 11, 65, 69 | Start/end of day and exact base order acknowledgments |
| Peer to client | `S` | Executed `E`, canceled `C` | 37, 19 or 21 | Incremental execution or cancellation |
| Peer to client | `S` | Rejected `J`, cancel pending/reject `P`/`I` | 30 or 32, 14 or 16 | Ref-specific rejects and cancel status |

Nonempty OUCH appendages fail closed. The decoder handles the exact mandatory fixed fields and the explicitly allowed omitted or zero-length forms. Other OUCH responses, additional options, multiplexed UserRefIdx, broken trades, short-sale modes, and unsequenced peer application data lie outside this subset. Peer input is bounded to 72 body bytes and any framing error closes the connection without scanning for a new header inside the same byte stream.

## Lifecycle and application contract

An upstream strategy chooses a free slot. The default table has eight entries. `ORDER_SLOTS` can be set from 1 to 255, with 255 reserved as the no-slot encoding. A newly queued enter or replace consumes its strictly increasing 32-bit `order_ref` even if a later peer outcome rejects it. A cancel targets the slot's current `order_ref`. A replace also supplies `previous_order_ref`, so a stale producer cannot accidentally replace a reused slot.

| Command | Operands and result |
| --- | --- |
| `CMD_ENTER` (0) | Supply a new ref, legal symbol, B/S side, price, and 1–999,999 shares. Tags echo in notices. |
| `CMD_REPLACE` (1) | Supply current ref as `previous_order_ref`, a higher new ref, price, and desired total quantity across the whole chain. The outstanding amount is distinct from chain fills. |
| `CMD_CANCEL` (2) | Supply current ref and zero for all remaining shares, or the reduced intended total for a partial cancel. |
| `CMD_RETRY` (3) | Supply the pending command's ref. If accepted, re-emit its original bytes without allocating a new ref or changing slot state. |
| `CMD_QUERY` (4) | Return the slot's state without wire traffic. The query is available offline. |

Order acknowledgments validate slot phase, reference, client ID where present, and echo fields. Buy acknowledgments may improve the price down and sell acknowledgments may improve it up. A live acknowledgment has positive leaves. A dead acknowledgment or a full fill/cancel leaves a tombstone view with zero leaves. The engine allows fills on the old reference while replacement is pending. A replacement ack supplies remaining shares, while `filled` records prior execution over the chain. A replace reject clears the journal entry while preserving the old live order. A terminal fill or cancel also retires any in-flight request on that chain. Cancel-pending leaves its journal live and cancel-reject clears it.

A successful local notice means the packet was queued for serialization. An exchange event is a separate notice. A transport cut may invalidate a partially serialized packet while retaining its journal entry. After replay catches up, the upstream adapter chooses which unresolved slots to retry. The last match number on each chain catches a repeated execution presented at a new position. Replayed older Soup positions are suppressed independently before lifecycle mutation.

`kill_req` is sticky until reset and blocks new enters, replaces, and their retries. Cancels, cancel retries, and sequenced replies still run. End of day applies the same admission fence until the next session. An in-flight TX packet can finish while a kill or fault is raised. Transport loss is the byte-stream flush boundary. Reset clears orders, references, counters, and the pinned cursor and is reserved for a reconciled new hardware session.

## Top-level channels

The top is `ouch_order_session` in [ouch_order_session.sv](ouch_order_session.sv), with type definitions in [ouch_wire_pkg.sv](ouch_wire_pkg.sv) and dependency order in [rtl.f](rtl.f). All ports use `clk`. A channel transfers on a rising edge when valid and ready are high. Keep valid and payload stable while stalled. `rst` is synchronous active high. The transport must assert `transport_up` only on the current socket and drop it for at least one sampled edge before signaling a new one.

| Port group | Contract |
| --- | --- |
| `s_login_valid/ready`, `s_login` | Typed login request. Credentials are fixed-width right-space-padded alphanumerics, session is left-space-padded, heartbeat timeout is five nonzero decimal digits. |
| `s_logout_valid/ready`, `transport_up`, `close_transport`, `kill_req`, `kill_latched`, `time_tick` | Socket and operational controls. Close stays asserted until the transport drops. |
| `s_rx_valid/ready`, `s_rx_data`, `s_rx_keep` | TCP receive stream. Beat and frame boundaries are independent. |
| `m_tx_valid/ready`, `m_tx_data`, `m_tx_keep`, `m_tx_last` | TX stream with packet boundary marker. Each packet begins on a fresh output beat. |
| `s_cmd_valid/ready`, `s_cmd` | Typed command with slot, kind, tag, order refs, quantity, price, and applicable text/order attributes. |
| `m_notice_valid/ready`, `m_notice` | Held notice with kind, status, sequence, reference(s), tag, slot, time, delta, leaves, filled, price, venue identifier and reason. |
| `query_slot`, `query_view`, `metrics` | Same-clock combinational table observation and saturating counters. Out-of-range query returns zero. |
| `session_state`, `session_id`, `next_sequence`, `replaying`, `market_open`, `highest_order_ref`, `live_count`, `pending_count`, `last_fault` | Control state and checkpoints for recovery and monitoring. |

`status_t` names are the wire codes for the normalized notice. Errors from command validation have kinds `N_COMMAND` or `N_LOGIN_REJECT` and do not alter the order table. A peer inconsistency raises `N_FAULT` with the offending sequence retained. Transport errors and login failures carry the current checkpoint. Successful notices distinguish every supported response, replay skip, and session transition.

## Run the project

Use Python 3, Icarus Verilog, Verilator, and Yosys with Slang. OSS CAD Suite includes the HDL tools. The ECP5 timing target additionally uses nextpnr.

```sh
make test
make lint synth
make timing
python3 ouch_audit.py audit fixtures/audit_example.jsonl --details --fail-on-fault
```

- [The Python codec](ouch_protocol.py) uses structured binary layouts. [The lifecycle oracle](ouch_model.py) tracks orders in keyed objects and computes output expectations independently of the RTL.
- [The integrated scoreboard](tb_ouch_order_session.sv) verifies full TX bytes, parsed lifecycle results, every status/slot/cursor checkpoint, output holds, and exact abandoned TX prefixes on a cut. The default target uses three seeds and table depths 4, 8, and 12.
- [The framing bench](tb_ouch_framing.sv) checks 649 complete messages in each direction, 73 truncated socket lengths, and all 256 `keep` masks. [The decimal bench](tb_ouch_decimal64.sv) checks 649 conversions and six partial conversion flushes.
- [The offline auditor](ouch_audit.py) reads local timestamped JSONL `link`, `rx`, `tx`, and `mark` entries. It reports replay, pending refs, cut fragments, latency between captured attempts and replies, and credential-free decoded records. It also independently decodes and compares the hardware test's TX capture against the oracle.
- `make synth` records a generic structural check and gate netlist. `make timing` maps the default gateway to ECP5 LFE5U-85F / CABGA381 / speed 8 with out-of-context routing and a 100 MHz constraint. The routing command preserves the measured result if it misses the target.

## Integration and performance boundaries

The default ECP5 run routed at **25.99 MHz**, below the 100 MHz constraint, with 39,075 combinational cells and 10,378 flip-flops. Its critical path runs from assembled RX bytes through decode and order-state update into the order table. A registered decode boundary and separately pipelined slot matching are the next steps to raise the clock at the cost of more event latency. See `build/session-route.json` for the reproducible out-of-context result.

The 64-bit ready/valid ports accept eight selected bytes in a handshake. Header and frame retirement consume additional clocks. Worst-case packet rate depends on message size, fragmentation, notice backpressure, and the CAM and barrel-shift paths. The raw port width is therefore separate from an achieved payload throughput number. The order table scans for peer refs and should use a banked or indexed lookup when scaled. The timing target excludes TCP, packet I/O, and board routes.

The core operates on synthetic bytes or an adapter supplied by the integrator. It does not open external connections or provision accounts. A production integration would need a durable checkpoint and journal, venue certification, system-level safety limits, and a separately validated TCP/network path. Reset ends this model's durable session in memory.
