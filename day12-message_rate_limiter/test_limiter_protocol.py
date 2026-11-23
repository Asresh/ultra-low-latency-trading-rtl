"""Exercise wire compatibility at field boundaries and trace error locations."""

import json
import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from limiter_protocol import (
    COMMAND_COLUMNS, RESPONSE_COLUMNS, PROFILES, Command, Drive, Geometry, Manifest,
    Observation, Operation, Response, Status, ZERO_COMMAND, ZERO_RESPONSE,
    as_operation, as_status, bool_bit, checked_keys, hexadecimal, ints, maximum,
    read_rows, unsigned, write_rows,
)


class GeometryTest(unittest.TestCase):
    """Check all allowed key encodings and the signed modular tick convention."""

    def test_key_widths_include_unimplemented_encodings(self):
        """Retain every needed selector bit, even for a one-key instance."""
        expected = {1: 1, 2: 1, 3: 2, 4: 2, 5: 3, 7: 3, 8: 3, 9: 4, 32: 5, 64: 6}
        for keys, bits in expected.items():
            self.assertEqual(Geometry(keys=keys).key_w, bits)
        for count in range(1, 65):
            current = Geometry(keys=count)
            self.assertGreaterEqual(1 << current.key_w, count)
            if current.key_w != 1:
                self.assertLess(1 << (current.key_w - 1), count)

    def test_reject_geometry_defaults_that_cannot_be_serialized(self):
        """Identify the exact geometry field which an invalid manifest supplied."""
        for name, value in (
            ("keys", 0), ("keys", 65), ("keys", True), ("tick_w", 1),
            ("count_w", 0), ("token_w", 25), ("tag_w", "8"),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaisesRegex(ValueError, name):
                    Geometry(**{name: value})

    def test_forward_distance_order_on_every_small_tick_pair(self):
        """Compare the modular convention with a forward-only explicit walk."""
        for width in (2, 3, 4, 5, 6):
            cfg = Geometry(tick_w=width)
            for anchor in range(1 << width):
                reachable = {
                    (anchor + offset) % (1 << width): offset
                    for offset in range(1 << (width - 1))
                }
                for target in range(1 << width):
                    self.assertEqual(cfg.delta(target, anchor), reachable.get(target))
                    self.assertEqual(cfg.wrapped(target + 4 * (1 << width)), target)

    def test_time_helpers_bound_ports_and_absolute_domain(self):
        """Reject negative time and unrepresentable tick values at host ingress."""
        geometry = Geometry(tick_w=8)
        with self.assertRaisesRegex(ValueError, "tick"):
            geometry.delta(256, 0)
        with self.assertRaisesRegex(ValueError, "anchor"):
            geometry.delta(0, -1)
        with self.assertRaisesRegex(ValueError, "absolute"):
            geometry.wrapped(-1)
        with self.assertRaisesRegex(ValueError, "absolute"):
            geometry.wrapped(True)
        self.assertEqual(geometry.wrapped(256), 0)
        self.assertEqual(geometry.delta(0, 255), 1)

    def test_geometry_record_contains_exact_hardware_parameters(self):
        """Round-trip every geometry without adding derived fields to the manifest."""
        for profile in PROFILES.values():
            record = profile.to_record()
            self.assertEqual(Geometry.from_record(record), profile)
            self.assertEqual(set(record), {"keys", "tick_w", "token_w", "count_w", "tag_w"})
        with self.assertRaisesRegex(ValueError, "unexpected"):
            Geometry.from_record({**Geometry().to_record(), "key_w": 1})
        with self.assertRaisesRegex(ValueError, "missing"):
            Geometry.from_record({"keys": 5})


class EncodingTest(unittest.TestCase):
    """Golden wire rows and randomized packed field round trips."""

    def setUp(self):
        """Use a small geometry that contains invalid-yet-representable keys."""
        self.geometry = PROFILES["small"]

    def test_known_vector_field_order(self):
        """Check a literal driver row and the endpoint sideband interpretation."""
        drive = Drive(
            reset=0, valid=1, ready=1,
            command=Command.configure(2, 14, 37, 12, 0x26),
        )
        self.assertEqual(hexadecimal(drive.words()), "0 1 1 0 2 e 25 c 1 26")
        encoded = ints("0 1 1 0 2 e 25 c 1 26", COMMAND_COLUMNS + 3, "fixture")
        self.assertEqual(Drive.from_words(encoded, self.geometry), drive)

    def test_response_vector_keeps_diagnostics_in_pin_order(self):
        """Check result identity, token counts, and status columns in a literal row."""
        result = Response(Operation.OFFER, 2, 42, Status.LIMITED, 1, 35, 8, 5, 4, 4, 2, 1, 8, 0)
        sample = Observation(0, 1, result)
        self.assertEqual(
            hexadecimal(sample.words()), "0 1 1 2 2a 2 1 23 8 5 4 4 2 1 8 0"
        )
        decoded = Observation.from_words(sample.words(), self.geometry)
        self.assertEqual(decoded, sample)

    def test_values_in_invalid_samples_are_normalized(self):
        """Treat unused payload as unobservable while retaining the ready pin."""
        sample = Observation(1, 0, None)
        self.assertEqual(sample.words(), (1, 0, *([0] * RESPONSE_COLUMNS)))
        self.assertEqual(Observation.from_words(sample.words(), self.geometry), sample)
        with self.assertRaisesRegex(ValueError, "zero payload"):
            Observation.from_words((1, 0, *([0] * (RESPONSE_COLUMNS - 1)), 1), self.geometry)
        with self.assertRaisesRegex(ValueError, "output valid"):
            Observation.from_words((1, 2, *([0] * RESPONSE_COLUMNS)), self.geometry)

    def test_command_rejects_width_errors_in_each_position(self):
        """Reject invalid host values before any simulator vector is written."""
        valid = Command.offer(2, 1, 1)
        for field, value in (
            ("op", 4), ("key", 4), ("tick", 64), ("amount", 64),
            ("rate", 64), ("enable", 2), ("tag", 4096),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field if field != "op" else "operation"):
                    replace(valid, **{field: value}).validate(self.geometry)
        self.assertEqual(replace(valid, key=3).validate(self.geometry).key, 3)

    def test_response_rejects_each_overwide_signal(self):
        """A broken capture should be diagnosed rather than implicitly narrowed."""
        for name, value in (
            ("op", 8), ("key", 4), ("tag", 4096), ("status", 7), ("enabled", 2),
            ("anchor", 64), ("capacity", 64), ("rate", 64), ("before", 64),
            ("balance", 64), ("passed", 16), ("limited", 16), ("charged", 16), ("saturated", 8),
        ):
            with self.subTest(field=name):
                with self.assertRaises(ValueError):
                    replace(ZERO_RESPONSE, **{name: value}).validate(self.geometry)

    def test_response_diff_reports_only_changed_fields(self):
        """Keep comparison diagnostics precise for failed simulator seeds."""
        altered = replace(ZERO_RESPONSE, key=3, capacity=7, charged=2)
        self.assertEqual(ZERO_RESPONSE.differences(altered), {
            "key": (0, 3), "capacity": (0, 7), "charged": (0, 2),
        })

    def test_enums_and_unsigned_fields_reject_booleans(self):
        """Validate exact file fields rather than Python's truthy numeric aliases."""
        for fn, value in ((as_operation, True), (as_operation, -1), (as_status, False), (as_status, 9)):
            with self.assertRaises(ValueError):
                fn(value)
        self.assertEqual(as_operation(2), Operation.INSPECT)
        self.assertEqual(as_status(6), Status.BAD_TICK)
        self.assertEqual(maximum(11), 2047)
        for value, bits in ((-1, 3), (8, 3), (True, 1), (1, 0), (2.0, 4)):
            with self.assertRaises(ValueError):
                unsigned(value, bits, "checked")
        self.assertEqual(bool_bit(1, "enabled"), 1)
        with self.assertRaises(ValueError):
            bool_bit(4, "enabled")

    def test_randomized_command_and_reply_wire_round_trip(self):
        """Exercise arbitrary content on every profile without semantic restrictions."""
        rng = random.Random(9813)
        for cfg in PROFILES.values():
            for _ in range(80):
                token = [rng.randrange(cfg.token_max + 1) for _ in range(4)]
                count = [rng.randrange(cfg.count_max + 1) for _ in range(3)]
                request = Command(
                    Operation(rng.randrange(4)), rng.randrange(1 << cfg.key_w),
                    rng.randrange(cfg.tick_mask + 1), *token[:2], rng.randrange(2),
                    rng.randrange(cfg.tag_max + 1),
                )
                answer = Response(
                    request.op, request.key, request.tag, Status(rng.randrange(7)), request.enable,
                    request.tick, *token, *count, rng.randrange(8),
                )
                self.assertEqual(Command.from_words(request.words(), cfg), request)
                self.assertEqual(Response.from_words(answer.words(), cfg), answer)

    def test_reset_valid_and_ready_bits_are_independent(self):
        """Even invalid command cycles preserve their optionally helpful payload."""
        for reset in range(2):
            for valid in range(2):
                for ready in range(2):
                    drive = Drive(reset, valid, ready, ZERO_COMMAND)
                    self.assertEqual(Drive.from_words(drive.words(), self.geometry), drive)
        for name in ("reset", "valid", "ready"):
            with self.assertRaisesRegex(ValueError, name):
                replace(Drive(), **{name: -1}).validate(self.geometry)


class TraceFilesTest(unittest.TestCase):
    """Prevent incomplete or corrupted trace sets from being treated as valid."""

    def setUp(self):
        """Allocate an isolated location for temporary traces and manifests."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)

    def test_rows_round_trip_columns_unchanged(self):
        """Keep zero, leading-width, and full-width hexadecimal words exact."""
        path = self.directory / "vectors.hex"
        cases = ((0, 0, 0xFFFFFFFF), (0xA1, 0x1000, 0xF0F), (0, 2, 7))
        write_rows(path, iter(cases))
        self.assertEqual(tuple(read_rows(path, 3)), cases)
        self.assertEqual(path.read_text(), "0 0 ffffffff\na1 1000 f0f\n0 2 7\n")

    def test_malformed_rows_keep_the_failed_file_line_in_the_error(self):
        """Expose the second row when the first parsed successfully."""
        path = self.directory / "vectors.hex"
        for malformed, pattern in (("1 2", "columns"), ("z 8 5", "hexadecimal"), ("", "columns")):
            path.write_text(f"0 0 0\n{malformed}\n", encoding="utf-8")
            rows = read_rows(path, 3)
            self.assertEqual(next(rows), (0, 0, 0))
            with self.assertRaisesRegex(ValueError, f"vectors.hex:2:.*{pattern}"):
                next(rows)

    def test_manifest_rejects_shape_issues_explicitly(self):
        """Report invalid schema, name, count, and unknown configuration fields."""
        example = Manifest("small", "random", 7, 10, PROFILES["small"])
        base = example.to_record()
        for name, value in (
            ("schema", 9), ("schema", True), ("profile", ""), ("scenario", 64),
            ("seed", -2), ("cycles", False), ("geometry", 32),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    Manifest.from_record({**base, name: value})
        with self.assertRaisesRegex(ValueError, "unexpected"):
            Manifest.from_record({**base, "cycle": 4})
        with self.assertRaisesRegex(ValueError, "expected an object"):
            checked_keys([], set(), "bad")

    def test_manifest_canonical_file_round_trip(self):
        """Restore the exact geometry and identity observed by the pin audit."""
        example = Manifest("wide_tick", "random", 727, 1024, PROFILES["wide_tick"])
        path = self.directory / "manifest.json"
        example.write(path)
        data = path.read_text(encoding="utf-8")
        self.assertEqual(json.loads(data), example.to_record())
        self.assertTrue(data.endswith("\n"))
        self.assertEqual(Manifest.read(path), example)


if __name__ == "__main__":
    unittest.main()
