"""Exercise causal audit behavior on small hand-written timelines."""
import unittest

from causal_trace import check


class CausalityTest(unittest.TestCase):
    def test_confirmed_parent_and_child(self):
        trace = """I 1 0 1 1 21 0 0
I 2 0 1 2 22 1 1
T 3 0 1 1 21 1
A 4 0 0 1 1
A 5 0 1 1 1
T 6 0 1 2 22 1
D 7 0 1 1 21 0
A 8 0 1 2 0
D 9 0 1 2 22 1"""
        self.assertEqual(check(trace.splitlines()), (2, 2, 0))

    def test_same_edge_ack_cannot_enable_child(self):
        trace = """I 1 0 1 1 21 0 0
I 2 0 1 2 22 1 1
T 3 0 1 1 21 1
T 5 0 1 2 22 1
A 5 0 1 1 1"""
        with self.assertRaisesRegex(ValueError, "earlier confirmed success"):
            check(trace.splitlines())

    def test_revoked_output_may_drain_after_terminal_diagnostic(self):
        trace = """I 1 0 1 1 21 0 0
D 3 0 1 1 21 4
T 4 0 1 1 21 0"""
        self.assertEqual(check(trace.splitlines()), (1, 0, 1))

    def test_wrong_epoch_ack_is_not_execution_evidence(self):
        trace = """I 1 0 2 1 21 0 0
T 2 0 2 1 21 1
A 3 0 1 1 1
D 5 0 2 1 21 0"""
        with self.assertRaisesRegex(ValueError, "lacks matching evidence"):
            check(trace.splitlines())
