"""Known-answer tests for permissioned replacements and owner withdrawals."""

from dataclasses import replace
import unittest

from reserve_model import (
    Command, Geometry, ImportPart, Kind, Management, Operation, Reject, ReserveBook,
    Result, Scope, TimeInForce,
)
from reserve_workload import manage


PROFILE = Geometry(1, 8, 9, 6, 3, 2, owner_width=3)


def maker(id_value, tick, qty, *, owner=1, side=1, display=0):
    return Command(order_id=id_value, price=tick, quantity=qty, side=side, owner=owner, display=display)


class ReplaceSemantics(unittest.TestCase):
    def setUp(self):
        self.book = ReserveBook(PROFILE)

    def execute(self, *commands):
        for command in commands:
            result = self.book.execute(command)
        return result

    def replace(self, id_value, price, amount, display=0, **kwargs):
        return manage(Management.REPLACE, order_id=id_value, side=1, price=price,
                      quantity=amount, display=display, **kwargs)

    def test_decrease_consumes_hidden_without_reopening_a_partial_slice(self):
        self.execute(
            maker(0, 4, 12, display=5), maker(1, 4, 8, owner=2),
            Command(order_id=2, quantity=3, price=4),
        )
        age = self.book.orders[0].time_priority
        [amend] = self.execute(self.replace(0, 4, 7, display=5))
        self.assertEqual(amend, Result(Kind.REPLACE, side=1, maker=0, price=4, quantity=9,
                                       remaining=2, total=7, visible=2, peak=5, owner=1))
        self.assertEqual(self.book.orders[0].time_priority, age)
        fills = self.execute(Command(order_id=2, quantity=4, price=4))
        self.assertEqual([(x.maker, x.quantity) for x in fills], [(0, 2), (1, 2)])

    def test_increase_is_youngest_and_resets_display(self):
        self.execute(maker(0, 4, 9, display=3), maker(1, 4, 9))
        [changed] = self.execute(self.replace(0, 4, 10, display=3))
        self.assertTrue(changed.refreshed)
        self.assertEqual((changed.level_count, changed.visible), (0, 3))
        [hit] = self.execute(Command(order_id=2, quantity=1, price=4))
        self.assertEqual(hit.maker, 1)

    def test_price_move_and_peak_change_each_renew_priority(self):
        self.execute(maker(0, 4, 9), maker(1, 5, 8))
        [moved] = self.execute(self.replace(0, 5, 8))
        self.assertTrue(moved.refreshed)
        [reshaped] = self.execute(self.replace(1, 5, 8, display=2))
        self.assertTrue(reshaped.refreshed)
        self.assertEqual(reshaped.visible, 2)
        [hit] = self.execute(Command(order_id=2, quantity=1, price=5))
        self.assertEqual(hit.maker, 0)

    def test_unchanged_existing_peak_may_exceed_target_remainder(self):
        self.execute(maker(0, 4, 9, display=7))
        [changed] = self.execute(self.replace(0, 4, 3, display=7))
        self.assertFalse(changed.refreshed)
        self.assertEqual((changed.total, changed.visible, changed.peak), (3, 3, 7))

    def test_crossing_reprice_rejects_without_losing_existing_priority(self):
        self.execute(maker(0, 2, 6, side=0), maker(1, 2, 4, side=0), maker(2, 4, 9, owner=2))
        before = self.book.orders.copy()
        request = replace(self.replace(0, 4, 8), side=0)
        self.assertEqual(self.execute(request), [Result.reject(Reject.POST)])
        self.assertEqual(self.book.orders, before)
        [fill] = self.execute(Command(side=1, price=2, quantity=1, order_id=3, tif=TimeInForce.IOC))
        self.assertEqual(fill.maker, 0)

    def test_reject_priorities_do_not_relocate_or_modify_any_order(self):
        self.execute(maker(0, 3, 6, display=4))
        good = self.replace(0, 3, 5, display=4)
        before = self.book.orders.copy()
        cases = [
            (replace(good, order_id=9, quantity=0), Reject.ID),
            (replace(good, order_id=1, owner=2), Reject.ABSENT),
            (replace(good, owner=2, side=0), Reject.OWNER),
            (replace(good, side=0, price=8), Reject.POLICY),
            (replace(good, stp=1), Reject.POLICY),
            (replace(good, import_visible=1), Reject.POLICY),
            (replace(good, price=8, quantity=0), Reject.PRICE),
            (replace(good, quantity=0), Reject.QUANTITY),
            (replace(good, display=6), Reject.DISPLAY),
        ]
        for command, code in cases:
            with self.subTest(command=command):
                self.assertEqual(self.execute(command), [Result.reject(code)])
                self.assertEqual(self.book.orders, before)


class MassOwnerOperations(unittest.TestCase):
    def setUp(self):
        self.book = ReserveBook(PROFILE)
        for command in (
            maker(6, 4, 8, display=2), maker(1, 2, 9, side=0), maker(5, 5, 6),
            maker(2, 4, 7, owner=2), maker(3, 1, 15, side=0, display=3),
        ):
            self.book.execute(command)

    def ids(self, mode, **changes):
        events = self.book.execute(manage(mode, **changes))
        return [row.maker for row in events[:-1]], events[-1]

    def test_preview_conserves_book_and_cancel_reports_same_traversal(self):
        original = self.book.orders.copy()
        keys, terminal = self.ids(Management.PREVIEW)
        self.assertEqual(keys, [6, 1, 5, 3])
        self.assertEqual(terminal, Result(Kind.MASS_PREVIEW, owner=1, level_count=4,
                                          level_total=38, level_visible=20))
        self.assertEqual(self.book.orders, original)
        canceled, terminal_cancel = self.ids(Management.CANCEL)
        self.assertEqual(canceled, keys)
        self.assertEqual(terminal_cancel, replace(terminal, kind=Kind.MASS_CANCEL))
        self.assertEqual(self.book.orders, {2: original[2]})

    def test_side_exact_and_aggressive_filters(self):
        cases = [
            (Scope.SIDE, 1, 0, [6, 5]), (Scope.SIDE, 0, 0, [1, 3]),
            (Scope.LEVEL, 1, 4, [6]), (Scope.LEVEL, 0, 1, [3]),
            (Scope.THROUGH, 0, 2, [1]), (Scope.THROUGH, 1, 4, [6]),
            (Scope.THROUGH, 0, 1, [1, 3]), (Scope.THROUGH, 1, 5, [6, 5]),
        ]
        for scope, side, price, wanted in cases:
            with self.subTest(scope=scope, side=side, price=price):
                ids, terminal = self.ids(Management.PREVIEW, stp=scope, side=side, price=price)
                self.assertEqual(ids, wanted)
                self.assertEqual(terminal.level_count, len(wanted))

    def test_no_matches_and_illegal_policy(self):
        self.assertEqual(self.ids(Management.CANCEL, owner=7), ([], Result(Kind.MASS_CANCEL, owner=7)))
        original = self.book.orders.copy()
        cases = [
            (manage(Management.CANCEL, order_id=1), Reject.POLICY),
            (manage(Management.CANCEL, quantity=1), Reject.POLICY),
            (manage(Management.PREVIEW, side=1), Reject.POLICY),
            (manage(Management.PREVIEW, stp=Scope.LEVEL, price=8), Reject.PRICE),
            (manage(3), Reject.OPCODE),
        ]
        for command, code in cases:
            self.assertEqual(self.book.execute(command), [Result.reject(code)])
        self.assertEqual(self.book.orders, original)

    def test_open_import_blocks_management_then_abort_reenables(self):
        self.book.execute(Command(operation=Operation.IMPORT, tif=ImportPart.BEGIN, order_id=0))
        for command in (manage(Management.PREVIEW), manage(Management.CANCEL),
                        manage(Management.REPLACE, order_id=1, price=2, quantity=3)):
            self.assertEqual(self.book.execute(command), [Result.reject(Reject.IMPORT)])
        self.book.execute(Command(operation=Operation.IMPORT, tif=ImportPart.ABORT))
        self.assertEqual(self.ids(Management.PREVIEW)[0], [6, 1, 5, 3])


if __name__ == "__main__":
    unittest.main()
