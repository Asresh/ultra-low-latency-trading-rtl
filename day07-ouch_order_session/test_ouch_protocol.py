"""Wire-level fixtures and adversarial checks independent from the RTL layouts."""

from __future__ import annotations

import io
import random
import unittest
from dataclasses import replace

from ouch_protocol import (
    BLANK_SESSION,
    MAX_BODY,
    MAX_PRICE,
    MAX_SEQUENCE,
    MAX_SHARES,
    ClientOrder,
    DecodeError,
    Envelope,
    Login,
    SoupStream,
    Status,
    StreamDecodeError,
    VenueEvent,
    control,
    decode_client,
    decode_login_request,
    decode_outbound,
    decode_peer,
    decode_venue,
    login_accepted,
    multiplex,
    parse_decimal,
    print_decimal,
    take_frames,
    unframe,
    valid_client,
    valid_session,
    valid_symbol,
)


ID = b"TRACE1        "
SYMBOL = b"MOCK.A  "
SESSION = b"   TEST001"
AUTH = Login(b"DEMO  ", b"wiretest  ", BLANK_SESSION, 1)


class MessageChecks(unittest.TestCase):
    def rejects(self, code: Status, operation, *args) -> DecodeError:
        with self.assertRaises(DecodeError) as captured:
            operation(*args)
        self.assertEqual(captured.exception.status, code)
        return captured.exception


class GoldenWireTests(MessageChecks):
    def test_login_wire_fields(self):
        wire = bytes.fromhex("00344c") + (
            b"DEMO  wiretest  " + (b" " * 29) + b"1" + b"05000"
        )
        self.assertEqual(AUTH.encode(), wire)
        self.assertEqual(len(wire), 54)
        self.assertEqual(decode_client(unframe(wire)), ("L", AUTH))
        accept = b"\x00\x1fA" + SESSION + b"                  24"
        self.assertEqual(login_accepted(SESSION, 24), accept)
        self.assertEqual(decode_peer(unframe(accept)), ("A", accept[3:]))

    def test_enter_wire_fixed_value_and_field_sentinels(self):
        frame = bytes.fromhex(
            "0030 554f 01020304 42 0000002b 4d4f434b2e412020 0000000001020304"
            " 3059504e4e 5452414345312020202020202020 0000"
        )
        enter = ClientOrder("O", 0x1020304, 43, 0x1020304,
                            side="B", symbol=SYMBOL, tif="0", display="Y", client_id=ID)
        self.assertEqual(enter.encode(), frame)
        self.assertEqual(len(frame), 50)
        self.assertEqual(decode_client(unframe(frame)), ("U", enter))

    def test_replace_wire_fixed_value_and_replacement_identity(self):
        frame = bytes.fromhex(
            "0029 5555 fabcde10 fabcde11 00010008 0000000030112233 334e4e"
            " 5452414345312020202020202020 0000"
        )
        order = ClientOrder("U", 0xFABCDE11, 65544, 0x30112233, 0xFABCDE10,
                            tif="3", display="N", client_id=ID)
        self.assertEqual(order.encode(), frame)
        self.assertEqual(decode_client(unframe(frame)), ("U", order))

    def test_cancel_wire_full_and_partial_sizes(self):
        full = ClientOrder("X", 0xABCDEF12, 0)
        reduce = replace(full, quantity=0x4321)
        self.assertEqual(full.encode(), b"\x00\x0aUX\xab\xcd\xef\x12\0\0\0\0")
        self.assertEqual(reduce.encode(), b"\x00\x0aUX\xab\xcd\xef\x12\0\0\x43\x21")
        body = unframe(reduce.encode()).payload
        self.assertEqual(decode_outbound(body + b"\0\0"), reduce)
        self.rejects(Status.OUCH_APPENDAGE, decode_outbound, body + b"\0\x01")

    def test_big_endian_execution_and_reject_reason(self):
        exec_hex = bytes.fromhex(
            "0025 5345 010204080f102040 89abcdef 0000c331 0000000055727374"
            " 41 1122334455667788 0000"
        )
        event = VenueEvent("E", 0x010204080F102040, 0x89ABCDEF, quantity=0xC331,
                           price=0x55727374, venue=0x1122334455667788, reason=ord("A"))
        self.assertEqual(event.frame(), exec_hex)
        self.assertEqual(decode_peer(unframe(exec_hex)), ("S", event))
        explicit = VenueEvent("J", 0x00ABCDEF01234567, 98, client_id=ID, reason=0x8022,
                              explicit_zero_appendage=True)
        self.assertEqual(explicit.payload()[13:15], b"\x80\x22")
        self.assertEqual(decode_venue(explicit.payload()), explicit)

    def test_replaced_ack_addresses_previous_before_current_key(self):
        ack = VenueEvent("U", 11, 0x110001EF, 0xF189B1FE, 71, 120000, SYMBOL, ID, "S", "L", 234567)
        raw = ack.payload()
        self.assertEqual(len(raw), 68)
        self.assertEqual(raw[9:18], b"\xf1\x89\xb1\xfe\x11\x00\x01\xefS")
        self.assertEqual(raw[40:48], (234567).to_bytes(8, "big"))
        self.assertEqual(decode_venue(raw), ack)


class CodecRejectionTests(MessageChecks):
    def setUp(self):
        self.timeout = AUTH.timeout
        self.enter = ClientOrder("O", 3, 20, 114_000, side="B",
                                 symbol=SYMBOL, tif="0", display="Y", client_id=ID)
        self.fixed = VenueEvent("A", 22, 3, quantity=20, price=112_000,
                                symbol=SYMBOL, client_id=ID, side="B", state="L", venue=13)

    def test_every_login_format_error_excludes_secrets(self):
        for field, value in [
            ("username", b" D    "), ("username", b"\xffA    "), ("username", b"abc"),
            ("password", b"DE MO     "), ("password", b"\0A        "),
            ("timeout", b"02e03"), ("timeout", b"00000"), ("timeout", b"0050"),
            ("sequence", MAX_SEQUENCE + 1), ("sequence", -1),
            ("session", b"TEST      "), ("session", b"    TE ST "), ("session", b"x" * 11),
        ]:
            with self.subTest(field=field, value=value):
                req = replace(AUTH, **{field: value})
                error = self.rejects(Status.LOGIN_TEXT, req.encode)
                self.assertNotIn(AUTH.password.decode("ascii"), str(error))
        self.rejects(Status.FRAME_LENGTH, decode_login_request, b"X" * 50)

    def test_nonnegative_decimal_and_overflow_properties(self):
        generator = random.Random(0x1563)
        for number in [0, 1, 10, 99, 10 ** 19, MAX_SEQUENCE, MAX_SEQUENCE - 1] + \
                      [generator.randrange(MAX_SEQUENCE + 1) for _ in range(450)]:
            padded = print_decimal(number)
            self.assertEqual(len(padded), 20)
            self.assertEqual(padded.strip(), str(number).encode("ascii"))
            self.assertEqual(parse_decimal(padded), number)
        self.assertEqual(parse_decimal(b"00000000000000000002"), 2)
        for invalid in [
            b"18446744073709551616", b"99999999999999999999", b" " * 20, b"-" * 20,
            b"                  \x001", b"                 1 2", b"                  1 ",
            b"                  \t1", b"                  +1", b"01", b"1" * 21,
        ]:
            self.rejects(Status.LOGIN_TEXT, parse_decimal, invalid)

    def test_exact_length_fences_for_required_appendages(self):
        for sample in [
            self.fixed,
            replace(self.fixed, kind="U", prev=2),
            VenueEvent("E", 22, 3, quantity=3, price=114_000, venue=28, reason=ord("A")),
        ]:
            payload = sample.payload()
            for k in range(1, len(payload)):
                self.rejects(Status.OUCH_LENGTH, decode_venue, payload[:k])
            for k in range(1, 4):
                self.rejects(Status.OUCH_LENGTH, decode_venue, payload + bytes(k))
            self.rejects(Status.OUCH_APPENDAGE, decode_venue, payload[:-1] + b"\x01")
            self.rejects(Status.OUCH_APPENDAGE, decode_venue, payload[:-2] + b"\x01\0")
            self.assertEqual(decode_venue(payload), sample)

    def test_optional_length_requires_whole_short_or_explicit_zero(self):
        for sample in [
            VenueEvent("C", 10, 3, quantity=5, reason=0x56),
            VenueEvent("P", 11, 3),
            VenueEvent("I", 12, 3),
            VenueEvent("J", 13, 3, reason=0xFE01, client_id=ID),
        ]:
            payload = sample.payload()
            self.assertEqual(decode_venue(payload), sample)
            self.assertEqual(decode_venue(payload + b"\0\0"),
                             replace(sample, explicit_zero_appendage=True))
            self.rejects(Status.OUCH_LENGTH, decode_venue, payload + b"\0")
            self.rejects(Status.OUCH_LENGTH, decode_venue, payload[:-1])
            self.rejects(Status.OUCH_APPENDAGE, decode_venue, payload + b"\0\x02")

    def test_unsupported_client_settings_and_packet_type(self):
        raw = unframe(self.enter.encode()).payload
        self.rejects(Status.BAD_FIELDS, decode_outbound, raw[:28] + b"R" + raw[29:])
        self.rejects(Status.BAD_FIELDS, decode_outbound, raw[:29] + b"Y" + raw[30:])
        self.rejects(Status.BAD_FIELDS, decode_outbound, raw[:30] + b"C" + raw[31:])
        self.rejects(Status.OUCH_TYPE, decode_outbound, b"Q")
        self.rejects(Status.SOUP_TYPE, decode_client, Envelope("A", b""))
        self.rejects(Status.SOUP_TYPE, decode_peer, Envelope("\u00e1", b""))
        self.rejects(Status.OUCH_TYPE, decode_venue, b"B")
        self.rejects(Status.FRAME_LENGTH, decode_peer, Envelope("H", b"1"))
        self.rejects(Status.FRAME_LENGTH, decode_peer, Envelope("A", b" " * 29))
        self.rejects(Status.FRAME_LENGTH, decode_client, Envelope("R", b"1"))

    def test_text_fields_have_explicit_boundaries(self):
        for symbol in (b"ALFA    ", b"Z09.A   ", b"A1234567"):
            self.assertTrue(valid_symbol(symbol))
        for symbol in (b" ALFA   ", b"AL FA   ", b"alfa    ", b"A\xff      ", b"        "):
            self.assertFalse(valid_symbol(symbol))
        for ident in (b"abc def       ", b"00112233445566"):
            self.assertTrue(valid_client(ident))
        for ident in (b"              ", b"ABCD\x00         ", b"\t0000000000000"):
            self.assertFalse(valid_client(ident))
        for session in (b"  001ABCD9", b"0123456789"):
            self.assertTrue(valid_session(session))
        self.assertFalse(valid_session(BLANK_SESSION))
        self.assertTrue(valid_session(BLANK_SESSION, allow_blank=True))


class StreamBoundaryTests(MessageChecks):
    def test_offset_monotonicity_every_simple_fragmentation(self):
        models = [
            Envelope("+", b"hello"),
            Envelope("H", b""),
            unframe(login_accepted(SESSION, 1)),
            Envelope("S", bytes(range(1, MAX_BODY))),
            Envelope("J", b"S"),
        ]
        wire = multiplex(models)
        offsets = []
        cursor = 0
        for message in models:
            offsets.append(cursor)
            cursor += message.length + 2
        expected = list(zip(offsets, models))
        for size in range(1, len(wire) + 1):
            with self.subTest(size=size):
                self.assertEqual(list(take_frames(io.BytesIO(wire), size)), expected)

    def test_randomized_coalescing_with_very_short_and_maximum_packets(self):
        rng = random.Random(9134)
        messages = [
            Envelope("+" if i % 2 else "S", bytes(rng.randrange(256) for _ in range(i % MAX_BODY)))
            for i in range(180)
        ]
        physical = multiplex(messages)
        for _ in range(25):
            cursor = 0
            parser = SoupStream()
            completed: list[Envelope] = []
            while cursor < len(physical):
                n = rng.randrange(1, 94)
                completed.extend(msg for _, msg in parser.ingest(physical[cursor:cursor + n]))
                cursor += n
            parser.finish()
            self.assertEqual(completed, messages)

    def test_late_bad_header_preserves_all_previous_frames_and_offset(self):
        parser = SoupStream()
        kept = control("H") + control("+", b"x")
        with self.assertRaises(StreamDecodeError) as captured:
            parser.ingest(kept + b"\x00\x00" + control("H"))
        err = captured.exception
        self.assertEqual(err.offset, len(kept))
        self.assertEqual([packet.kind for _, packet in err.completed], ["H", "+"])
        self.assertEqual(parser.offset, len(kept))
        with self.assertRaises(RuntimeError):
            parser.ingest(control("H"))
        iterator = take_frames(io.BytesIO(kept + b"\x00\x00"), 50)
        self.assertEqual(next(iterator)[1].kind, "H")
        self.assertEqual(next(iterator)[1].kind, "+")
        self.rejects(Status.FRAME_LENGTH, next, iterator)

    def test_truncated_eof_fences_every_byte_boundary(self):
        frame = Envelope("+", bytes(range(MAX_BODY - 1))).frame()
        for cut in range(1, len(frame)):
            reader = SoupStream()
            self.assertEqual(reader.ingest(frame[:cut]), [])
            error = self.rejects(Status.FRAME_LENGTH, reader.finish)
            self.assertEqual(error.offset, 0)
        self.assertEqual(unframe(frame), Envelope("+", bytes(range(MAX_BODY - 1))))
        self.rejects(Status.FRAME_LENGTH, unframe, frame + frame)
        self.rejects(Status.FRAME_LENGTH, unframe, b"")
        with self.assertRaises(ValueError):
            Envelope("+", bytes(MAX_BODY)).frame()
        with self.assertRaises(ValueError):
            SoupStream(65536)


if __name__ == "__main__":
    unittest.main()
