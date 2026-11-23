"""Known-answer tests for image validation and the physical control contract."""

from dataclasses import replace
import unittest

from replay_model import ReplayModel, add_tick, bounded_flush, load_commands
from replay_program import (
    Beat, Command, Cycle, Offer, Operation, Packet, ReplayError, Response, Statistics,
    Status, Tape, U16, U32, U48, U64, mask_data, prefix_keep, lanes_from_keep,
)


class ProtocolTests(unittest.TestCase):
    """Validate the software representations at their host-side input boundaries."""

    def test_keep_prefix_round_trip(self):
        """All eight nonempty prefixes decode to the corresponding byte count."""
        for count in range(1, 9):
            with self.subTest(count=count):
                self.assertEqual(lanes_from_keep(prefix_keep(count)), count)
                self.assertEqual(prefix_keep(count), (1 << count) - 1)

    def test_rejects_every_nonprefix_mask(self):
        """Holes and empty masks cannot create ambiguous byte order."""
        canonical = {1, 3, 7, 15, 31, 63, 127, 255}
        for keep in set(range(256)) - canonical:
            with self.subTest(keep=keep):
                with self.assertRaises(ReplayError):
                    lanes_from_keep(keep)

    def test_tail_and_nonfinal_rules(self):
        """Partial final beats are normalized and partial nonfinal beats rejected."""
        tail = Beat(21, 0xFFEEDDCCBBAA9988, 0x07, True, 0x1234)
        self.assertEqual(tail.data, 0xAA9988)
        self.assertEqual(tail.payload, bytes.fromhex("8899aa"))
        with self.assertRaises(ReplayError):
            replace(tail, last=False)
        with self.assertRaises(ReplayError):
            Beat.from_payload(2, b"", 1, True)
        with self.assertRaises(ReplayError):
            Beat.from_payload(2, b"ninebytes", 1, True)

    def test_mask_every_lane(self):
        """Verify little-endian lane position for every one-hot mask."""
        raw = 0xEFCDAB8967452301
        for position, byte in enumerate(raw.to_bytes(8, "little")):
            with self.subTest(position=position):
                self.assertEqual(mask_data(raw, 1 << position), byte << (position * 8))

    def test_packet_ticks_and_width(self):
        """Check a two-beat packet where the second beat is scheduled later."""
        pkt = Packet.paced(65535, b"01234567H", 23, 5)
        self.assertEqual(pkt.ticks, (23, 28))
        self.assertEqual(pkt.start, 23)
        self.assertEqual(pkt.end, 28)
        self.assertEqual(pkt.beats(), (
            Beat(23, 0x3736353433323130, 255, False, 65535),
            Beat(28, ord("H"), 1, True, 65535),
        ))

    def test_packet_uses_only_explicit_axis_bytes(self):
        """Reject wrong timestamps and field types before converting to bus words."""
        for options in [
            (1, b"", (0,)),
            (1, b"012345670", (0,)),
            (1, b"012345670", (8, 3)),
            (1, b"bytes", [0]),
            (1, bytearray(b"mutable"), (0,)),
            (U16+1, b"bytes", (0,)),
            (1, b"bytes", (U32+1,)),
        ]:
            with self.subTest(options=options):
                with self.assertRaises(ReplayError):
                    Packet(*options)

    def test_tape_complete_crop_and_capacity(self):
        """Crop whole packets without changing tags or keeping trailing fragments."""
        a = Packet.paced(4, b"abcde0123", 1, 8)
        b = Packet.paced(4, b"r", 10, 8)
        c = Packet.paced(7, b"z", 11, 8)
        tape = Tape.from_packets((a, b, c))
        self.assertEqual(tape.duration, 11)
        self.assertEqual(tape.payload_bytes, 11)
        self.assertEqual(tuple(tape.packets()), (a, b, c))
        self.assertEqual(tuple(tape.between(9, 10).packets()), (b,))
        self.assertIs(tape.checked_depth(4), tape)
        with self.assertRaisesRegex(ReplayError, "4 beats"):
            tape.checked_depth(3)
        with self.assertRaisesRegex(ReplayError, "precedes"):
            tape.between(10, 9)

    def test_tape_refuses_invalid_chronology_and_tags(self):
        """Reject interleaving, backwards time, and missing packet boundaries."""
        a = Beat(10, 0xFF, 255, False, 8)
        b = Beat(12, 0xAA, 1, True, 9)
        for controls in [(a,), (a, b), (b, a), (b, replace(b, tick=1))]:
            with self.subTest(controls=controls):
                with self.assertRaises(ReplayError):
                    Tape(controls)

    def test_raw_requests_preserve_malformed_wire_values(self):
        """Model stays capable of negative testing without accepting out-of-width values."""
        self.assertEqual(Command(7).op, 7)
        self.assertEqual(Command(0, data=U64, keep=0xFE).data, U64)
        for fields in ({"op": 8}, {"tick": -1}, {"data": U64+1}, {"last": 2},
                       {"tag": U16+1}, {"gap": U32+1}, {"repeats": -1}):
            with self.subTest(fields=fields):
                values = {"op": 0}
                values.update(fields)
                with self.assertRaises(ReplayError):
                    Command(**values)

    def test_counters_saturate_at_distinct_boundaries(self):
        """Do not roll over a high count when adjacent unsaturated fields increment."""
        stats = Statistics(U32, U32-1, U32, U32-2)
        item = Offer(Beat.from_payload(1, b"A", 3, True), 8, 11, 2)
        self.assertEqual(stats.receive(item, True), Statistics(U32, U32, U32, U32-1))
        self.assertEqual(stats.receive(item, False), Statistics(U32, U32, U32, U32-2))
        self.assertEqual(add_tick(U48-3, 8), U48)
        self.assertEqual(add_tick(U48-3, 2), U48-1)

    def test_reset_control_rejects_nonboolean_markers(self):
        """Readiness and reset flags must remain physical single-bit values."""
        for args in ({"reset": 1}, {"ready": 2}, {"rsp_ready": "true"}, {"cfg": 0}):
            with self.subTest(args=args):
                with self.assertRaises(ReplayError):
                    Cycle(**args)


class ControlBehaviorTests(unittest.TestCase):
    """Check observable runs independently of the generated RTL campaign."""

    def setUp(self):
        """Start each test with an empty three-record reference model."""
        self.model = ReplayModel(3)

    def command(self, command, wanted=Status.OK, *, ready=True):
        """Accept one request and compare its status and returned length."""
        noticed = self.model.step(Cycle(cfg=command, ready=ready))
        self.assertEqual(noticed.request, command)
        self.assertEqual(self.model.response.status, wanted)
        return self.model.response.length

    def load(self, packets):
        """Program a complete fixture using only the documented control channel."""
        tape = Tape.from_packets(packets)
        for command in load_commands(tape):
            self.command(command)
        return tape

    def drain(self):
        """Collect output transfers to completion with a finite cycle watchdog."""
        retired = []
        for _ in range(128):
            if not self.model.active:
                return retired
            event = self.model.step(Cycle()).output
            if event is not None:
                retired.append(event)
        self.fail("run never completed")

    def test_empty_start_bad_opcode_and_response_hold(self):
        """A held status blocks configuration while untouched payload remains stable."""
        self.assertEqual(self.command(Command.start(), Status.EMPTY), 0)
        previous = self.model.response
        invalid = Command(7)
        stalled = self.model.step(Cycle(cfg=invalid, rsp_ready=False))
        self.assertIsNone(stalled.request)
        self.assertEqual(stalled.observation.response, previous)
        next_edge = self.model.step(Cycle(cfg=invalid))
        self.assertEqual(next_edge.request, invalid)
        self.assertEqual(next_edge.response, Response(Status.EMPTY, 0))
        self.assertEqual(self.model.response, Response(Status.BAD_OP, 0))

    def test_rejected_bad_fields_do_not_corrupt_image(self):
        """Validation rejects malformed fields before any state change becomes visible."""
        head = Beat.from_payload(10, b"abcdefgh", 1, False)
        self.command(Command.append(head))
        self.command(Command.start(), Status.OPEN_PACKET)
        bad_tag = Command.append(Beat.from_payload(11, b"!", 2, True))
        backward = Command.append(Beat.from_payload(9, b"!", 1, True))
        hole = Command(Operation.APPEND, 11, 255, 0x81, True, 1)
        self.command(bad_tag, Status.BAD_TAG)
        self.command(backward, Status.BAD_TIME)
        self.command(hole, Status.BAD_KEEP)
        self.assertEqual(self.model.image, [head])
        by = Beat.from_payload(11, b"?", 1, True)
        self.command(Command.append(by))
        self.command(Command.start())
        self.assertEqual([x.event for x in self.drain()], [head, by])

    def test_no_stall_repeat_schedule(self):
        """Equal deadlines serialize and each pass starts after the accepted tail."""
        a = Packet.paced(2, b"abcdefghQ", 0, 0)
        b = Packet.paced(3, b"T", 4)
        self.load([a, b])
        self.command(Command.start(2, 2))
        result = self.drain()
        self.assertEqual([offer.due for offer in result], [0, 0, 4, 8, 8, 12])
        self.assertEqual([offer.offered for offer in result], [0, 1, 4, 8, 9, 12])
        self.assertEqual([offer.pass_number for offer in result], [1, 1, 1, 2, 2, 2])
        self.assertEqual(self.model.statistics, Statistics(6, 4, 2, 2))
        self.assertTrue(self.model.done)
        self.model.step(Cycle())
        self.assertFalse(self.model.done)

    def test_pause_drains_held_output_without_advancing_tick(self):
        """Verify that a legal transfer can retire while future scheduling stays frozen."""
        a = Packet.paced(5, b"abcdefghf", 0)
        self.load([a])
        self.command(Command.start(), ready=False)
        self.model.step(Cycle(ready=False))
        self.command(Command(Operation.PAUSE), ready=False)
        tick = self.model.now
        held = self.model.output
        self.assertEqual(self.model.step(Cycle(ready=False)).observation.offer, held)
        self.assertEqual(self.model.step(Cycle()).output, held)
        self.assertEqual(self.model.now, tick)
        self.assertIsNone(self.model.output)
        self.command(Command(Operation.RESUME), ready=False)
        self.assertEqual(self.model.now, tick)
        self.assertEqual([item.event.payload for item in self.drain()], [b"f"])

    def test_stop_preserves_held_payload_then_abandons_rest(self):
        """Stop is deferred until a visible beat transfers and permits a fresh start."""
        packet = Packet.paced(9, b"A" * 12, 0)
        self.load([packet])
        self.command(Command.start(), ready=False)
        self.model.step(Cycle(ready=False))
        saved = self.model.output
        self.command(Command(Operation.STOP), ready=False)
        self.command(Command(Operation.STOP), Status.BAD_STATE, ready=False)
        self.assertEqual(self.model.output, saved)
        delivered = self.model.step(Cycle()).output
        self.assertEqual(delivered, saved)
        self.assertTrue(self.model.done)
        self.assertEqual(self.model.statistics, Statistics(1, 0, 0, 0))
        self.command(Command.start())
        self.assertEqual([item.event.payload for item in self.drain()], [b"A"*8, b"A"*4])
        self.assertEqual(self.model.statistics, Statistics(2, 1, 0, 1))

    def test_reset_abandons_any_held_outputs(self):
        """Synchronous reset invalidates the tape, output slot, response, and counters."""
        self.load([Packet.paced(7, b"data", 0)])
        self.command(Command.start(), ready=False)
        self.model.step(Cycle(ready=False, rsp_ready=False))
        edge = self.model.step(Cycle(reset=True, cfg=Command.start()))
        self.assertIsNone(edge.request)
        self.assertIsNone(edge.output)
        self.assertFalse(self.model.active)
        self.assertFalse(self.model.paused)
        self.assertFalse(self.model.done)
        self.assertEqual(self.model.now, 0)
        self.assertEqual(self.model.statistics, Statistics())
        self.assertEqual(self.command(Command.start(), Status.EMPTY), 0)

    def test_busy_requests_cannot_write_or_clear(self):
        """Keep the run's original image despite interleaved invalid reprogramming."""
        a = Packet.paced(1, b"x", 8)
        self.load([a])
        self.command(Command.start(2))
        self.assertEqual(self.command(Command(Operation.CLEAR), Status.BUSY), 1)
        self.assertEqual(self.command(Command.append(a.beats()[0]), Status.BUSY), 1)
        self.assertEqual(self.command(Command.start(), Status.BUSY), 1)
        self.assertEqual([value.event.tag for value in self.drain()], [1, 1])

    def test_bounded_flush_warns_on_paused_run(self):
        """A finite offline wait reports that the script failed to resume the engine."""
        a = Packet.paced(1, b"later", 21)
        self.load([a])
        self.command(Command.start())
        self.command(Command(Operation.PAUSE))
        with self.assertRaisesRegex(ReplayError, "failed to drain"):
            bounded_flush(self.model, [Cycle()], 7)

    def test_at_last_pass_stop_counts_accepted_final(self):
        """A held final beat still contributes a pass count when stop is asserted."""
        a = Packet.paced(12, b"h", 0)
        self.load([a])
        self.command(Command.start(3), ready=False)
        self.model.step(Cycle(ready=False))
        self.command(Command(Operation.STOP), ready=False)
        retired = self.model.step(Cycle()).output
        self.assertEqual(retired.event.payload, b"h")
        self.assertEqual(self.model.statistics.passes, 1)
        self.assertFalse(self.model.active)


    def test_pause_on_final_edge_does_not_suppress_pass_credit(self):
        """Check that a final handshake wins normal retirement in the same pause edge."""
        self.load([Packet.paced(83,b"last",0)])
        self.command(Command.start(repeats=2,gap=0), ready=False)
        self.model.step(Cycle(ready=False))
        held = self.model.output
        step = self.model.step(Cycle(cfg=Command(Operation.PAUSE)))
        self.assertEqual(step.output, held)
        self.assertEqual(self.model.statistics.passes, 1)
        self.assertTrue(self.model.active)
        self.assertTrue(self.model.paused)
        self.assertIsNone(self.model.output)
        paused_tick = self.model.now
        for _ in range(10):
            self.model.step(Cycle())
        self.assertEqual(self.model.now, paused_tick)
        self.command(Command(Operation.RESUME))
        remaining, = self.drain()
        self.assertEqual(remaining.pass_number, 2)
        self.assertEqual(remaining.due, paused_tick+1)
        self.assertEqual(self.model.statistics.passes, 2)

    def test_stop_without_held_offer_prioritizes_control(self):
        """A due input cannot publish an unoffered beat in the stop cycle."""
        self.load([Packet.paced(33,b"queued",4)])
        self.command(Command.start())
        for _ in range(4):
            self.model.step(Cycle(ready=False))
        self.assertEqual(self.model.now, 4)
        self.assertIsNone(self.model.output)
        self.command(Command(Operation.STOP), ready=False)
        self.assertTrue(self.model.done)
        self.assertFalse(self.model.active)
        self.assertIsNone(self.model.output)
        self.assertEqual(self.model.statistics, Statistics())
        self.assertEqual(len(self.model.image), 1)

    def test_full_tape_with_open_packet_cannot_be_started(self):
        """A full but incomplete image needs CLEAR before it can be used again."""
        for offset in range(3):
            part = Beat.from_payload(offset, bytes([offset])*8, 18, False)
            self.assertEqual(self.command(Command.append(part)), offset+1)
        invalid_tail = Command(Operation.APPEND, 0, 1, 0, True, 19)
        self.assertEqual(self.command(invalid_tail, Status.FULL), 3)
        self.assertEqual(self.command(Command.start(), Status.OPEN_PACKET), 3)
        self.assertFalse(self.model.active)
        self.assertIsNone(self.model.output)
        self.assertEqual(self.command(Command(Operation.CLEAR)), 0)
        self.assertEqual(self.command(Command.start(), Status.EMPTY), 0)
        self.assertEqual(self.model.image, [])

if __name__ == "__main__":
    unittest.main()
