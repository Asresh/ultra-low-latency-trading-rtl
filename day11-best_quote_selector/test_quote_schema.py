"""Exercise wire widths and exact field ordering independently of the RTL."""

from dataclasses import replace
from itertools import product
from random import Random
import unittest

from quote_schema import (
    Ack, Code, Command, Drive, Op, PROFILES, Profile, Query, Result, Sample,
    State, Winner, bounded, decimal_row, distance, mask, row_text,
    serial_is_forward, signed, unsigned, validate_fields, values,
)


class Widths(unittest.TestCase):
    def test_profile_preserves_unusable_addresses(self):
        """Ensure every supported instrument and venue count leaves an invalid wire ID."""
        for count in range(1, 25):
            profile = Profile(count, count)
            self.assertGreater(1 << profile.inst_w, count)
            self.assertGreater(1 << profile.venue_w, count)
            self.assertGreaterEqual(count, 1 << (profile.inst_w - 1))

    def test_profile_rejects_unsupported_geometry(self):
        """Reject Boolean, zero, and overly wide values at the profile boundary."""
        for key, invalid in (
            ("instruments", 0), ("venues", 25), ("price_w", 29),
            ("qty_w", 1), ("time_w", "8"), ("seq_w", 0), ("tag_w", True),
        ):
            with self.subTest(field=key), self.assertRaises(ValueError):
                Profile(**{key: invalid})

    def test_profile_parameters_are_reproducible(self):
        """Check the hardware elaboration keys represent exactly the declared profile."""
        self.assertEqual(
            PROFILES["odd"].sv_parameters(),
            {"INSTRUMENTS": 3, "VENUES": 5, "PRICE_W": 12, "QTY_W": 9,
             "TIME_W": 11, "SEQ_W": 8, "TAG_W": 12},
        )

    def test_each_counter_distance_exhaustively_in_small_profiles(self):
        """Exhaust serial wrap and ambiguous boundary points in three small moduli."""
        for width in (3, 4, 5):
            modulus = 2**width
            for new, old in product(range(modulus), repeat=2):
                want = (new - old) % modulus
                self.assertEqual(distance(new, old, width), want)
                self.assertEqual(serial_is_forward(new, old, width), 0 < want < modulus // 2)

    def test_modular_truncation_matches_independent_division(self):
        """Reduce many positive and negative mathematical values modulo the field."""
        rng = Random(216)
        for width in (1, 5, 12, 21, 28):
            modulus = 2**width
            self.assertEqual(mask(width), modulus - 1)
            for _ in range(128):
                number = rng.randint(-modulus * 11, modulus * 11)
                self.assertEqual(bounded(number, width), divmod(number, modulus)[1])

    def test_signed_spread_cannot_silently_truncate(self):
        """Validate both extrema of the sign-extended price-difference field."""
        for name, profile in PROFILES.items():
            limit = 1 << profile.price_w
            for value in (-limit, 0, limit - 1):
                self.assertEqual(signed(value, profile.price_w + 1, name), value)
            for value in (-limit - 1, limit):
                with self.assertRaises(ValueError):
                    signed(value, profile.price_w + 1, name)

    def test_unsigned_validation_is_not_a_wrap_operation(self):
        """Reject numbers an external user cannot present at the configured width."""
        self.assertEqual(unsigned(7, 3, "side"), 7)
        for value in (-1, 8, "2", 2.5):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "side"):
                unsigned(value, 3, "side")

    def test_dynamic_validator_rejects_bad_declaration(self):
        """Reject accidental drift between typed records and their width layout."""
        with self.assertRaisesRegex(ValueError, "record shape"):
            validate_fields(Query(), (3,))


class Records(unittest.TestCase):
    def setUp(self):
        """Use the default geometry and asymmetric values to catch field swaps."""
        self.p = Profile()
        self.query = Query(2, 47, 311)
        self.command = Command.replace(1, 2, 11, (510, 4), (600, 9), 521)
        self.bid = Winner(1, 2, 850, 7, 9, 703)
        self.ask = Winner(1, 1, 820, 3, 6, 783)
        self.result = Result(311, Code.OK, State.CROSSED, 97, 6, 8, self.bid, self.ask, -30)

    def test_command_constructors_preserve_order(self):
        """Exercise publish, removal, and venue state controls as raw wire fields."""
        self.assertEqual(values(self.command), (0, 1, 2, 11, 510, 600, 4, 9, 521))
        self.assertEqual(values(Command.withdraw(2, 3, 72, 16)), (1, 2, 3, 72, 0, 0, 0, 0, 16))
        self.assertEqual(Command.venue_enabled(1, 0, True).op, Op.ENABLE)
        self.assertEqual(Command.venue_enabled(1, 0, False).op, Op.DISABLE)

    def test_driver_layout_is_independent(self):
        """Check numeric driver ordering against a hand-written known answer."""
        source = Drive(25, 0, 1, 0, self.command, self.query)
        want = (25, 0, 1, 0, 1, 0, 1, 2, 11, 510, 600, 4, 9, 521, 1, 2, 47, 311)
        self.assertEqual(source.encode(), want)
        self.assertEqual(Drive.decode(want, self.p), source)

    def test_reply_layout_preserves_signed_spread_and_forwarded_metadata(self):
        """Check the exact observed field order for a crossed consolidated result."""
        source = Sample(1, 0, Ack(Code.BAD_VENUE, 521), self.result)
        want = (1, 0, 1, 2, 521, 1, 311, 0, 5, 97, 6, 8, 1, 2, 850, 7, 9, 703,
                1, 1, 820, 3, 6, 783, -30)
        self.assertEqual(source.encode(), want)
        self.assertEqual(Sample.decode(want, self.p), source)

    def test_inactive_payload_decodes_to_none(self):
        """Decode idle driver and sink records without fabricating a transaction."""
        self.assertEqual(Drive.decode(Drive().encode(), self.p), Drive())
        self.assertEqual(Sample.decode(Sample(0, 1, None, None).encode(), self.p),
                         Sample(0, 1, None, None))

    def test_controls_reject_non_binary_values(self):
        """Validate ready, valid, and reset signals separately from data fields."""
        source = list(Drive(command=self.command, query=self.query).encode())
        for index in (1, 2, 3, 4, 14):
            malformed = list(source)
            malformed[index] = 2
            with self.subTest(index=index), self.assertRaises(ValueError):
                Drive.decode(malformed, self.p)
        observed = list(Sample(1, 1, Ack(Code.OK, 521), self.result).encode())
        for index in (0, 1, 2, 5):
            malformed = list(observed)
            malformed[index] = 2
            with self.subTest(index=index), self.assertRaises(ValueError):
                Sample.decode(malformed, self.p)

    def test_every_observed_result_width(self):
        """Reject values one bit wider than their published per-field ranges."""
        base = self.result
        for name, wrong in (
            ("tag", 1 << self.p.tag_w), ("code", 16), ("state", 8),
            ("tick", 1 << self.p.time_w), ("stale", 1 << self.p.venues),
            ("active", 1 << self.p.venues), ("spread", 1 << self.p.price_w),
        ):
            with self.subTest(field=name), self.assertRaises(ValueError):
                replace(base, **{name: wrong}).check(self.p)
        for field, width in (
            ("valid", 1), ("venue", self.p.venue_w), ("price", self.p.price_w),
            ("qty", self.p.qty_w), ("age", self.p.time_w), ("sequence", self.p.seq_w),
        ):
            with self.subTest(side_field=field), self.assertRaises(ValueError):
                replace(base, bid=replace(self.bid, **{field: 1 << width})).check(self.p)

    def test_differing_bad_addresses_are_still_representable(self):
        """Preserve malformed commands for hardware error-priority verification."""
        raw = Command(7, self.p.instruments, self.p.venues, tag=11)
        self.assertEqual(Drive.decode(Drive(command=raw).encode(), self.p).command, raw)
        with self.assertRaises(ValueError):
            replace(raw, op=8).check(self.p)

    def test_edge_helpers_do_not_transfer_during_reset(self):
        """Treat every visible bus value as void on the reset edge."""
        pulse = Drive(0, 1, 1, 1, self.command, self.query)
        sample = Sample(1, 1, Ack(Code.OK, 521), self.result)
        self.assertFalse(sample.accepted_command(pulse))
        self.assertFalse(sample.accepted_query(pulse))
        self.assertIsNone(sample.transferred_ack(pulse))
        self.assertIsNone(sample.transferred_result(pulse))

    def test_ready_bits_are_independent(self):
        """Check individual results and inputs are governed by their own ready/valid pair."""
        pulse = Drive(0, 0, 0, 1, self.command, self.query)
        sample = Sample(1, 0, Ack(Code.OK, 521), self.result)
        self.assertTrue(sample.accepted_command(pulse))
        self.assertFalse(sample.accepted_query(pulse))
        self.assertIsNone(sample.transferred_ack(pulse))
        self.assertEqual(sample.transferred_result(pulse), self.result)

    def test_decimal_codec_rejects_partial_rows(self):
        """Use an exact row size so truncated or merged lines fail loudly."""
        self.assertEqual(decimal_row(" -2 13 4 ", 3), (-2, 13, 4))
        self.assertEqual(row_text((-2, 13, 4)), "-2 13 4")
        for line in ("", "1 2", "1 2 3 4"):
            with self.subTest(line=line), self.assertRaisesRegex(ValueError, "columns"):
                decimal_row(line, 3)
        with self.assertRaisesRegex(ValueError, "non-decimal"):
            decimal_row("1 2 0x3", 3)


if __name__ == "__main__":
    unittest.main()
