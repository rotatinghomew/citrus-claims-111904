"""Conflict quarantine must not become a new shortage or a blocked ledger."""
import unittest
from datetime import datetime

from recon.engine import reconcile

LATE = datetime.fromisoformat("2026-11-20T09:00:00+08:00")


class LiveConflictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Force the contradictory S2 rev 2 to be the live reading.
        cls.bundle = reconcile(overrides={"S2": 2})

    def test_conflict_names_only_downstream_waybills(self):
        live = [c for c in self.bundle.conflicts if c.label == "live readings"]
        self.assertEqual(len(live), 2)
        self.assertEqual({c.parcel for c in live}, {"P1", "P2"})
        self.assertEqual({wb for c in live for wb in c.affected_waybills},
                         {"W1", "W2"})
        for c in live:
            self.assertIn("不是新增短少", c.basis)

    def test_unrelated_waybills_still_settle(self):
        self.assertFalse(self.bundle.attributions["W3"].conflict)
        self.assertFalse(self.bundle.attributions["W4"].conflict)
        self.assertEqual(self.bundle.buyers["W4"].entitlement_cents, 11610)
        self.assertEqual(self.bundle.buyers["W4"].executable_next_cents, 11610)

    def test_conflicted_waybill_gets_no_new_cash(self):
        for wb in ("W1", "W2"):
            buyer = self.bundle.buyers[wb]
            self.assertTrue(buyer.held)
            self.assertEqual(buyer.pending_cents, 0)
            self.assertEqual(buyer.executable_next_cents, 0)
            self.assertEqual(buyer.entitlement_cents, 0)
            # The money already paid is preserved, not reversed.
            self.assertGreater(buyer.paid_cents, 0)

    def test_conflict_is_not_counted_as_shortage(self):
        for wb in ("W1", "W2"):
            self.assertEqual(self.bundle.attributions[wb].shortage.low, 0)
            self.assertEqual(self.bundle.attributions[wb].shortage.high, 0)

    def test_closing_identity_covers_only_settled_waybills(self):
        close = self.bundle.close_summary()
        self.assertTrue(close["identity"]["check_balances"])
        self.assertEqual(set(close["identity"]["settled_waybills"]), {"W3", "W4"})
        self.assertEqual(close["quarantined"]["frozen_paid_cents"], 14260)

    def test_no_duplicate_payment_created_for_conflict(self):
        entries = [a for b in self.bundle.buyers.values() for a in b.adjustments]
        self.assertTrue(all(a.cents > 0 for a in entries))
        wb_entries = {(a.waybill) for a in entries}
        self.assertNotIn("W1", wb_entries)
        self.assertNotIn("W2", wb_entries)


class CleanRecordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = reconcile()

    def test_superseded_bad_revision_is_reported_not_live(self):
        labels = {c.label for c in self.bundle.conflicts}
        self.assertIn("S2:2", labels)
        self.assertFalse(any(c.label == "live readings" for c in self.bundle.conflicts))
        live = next(c for c in self.bundle.conflicts if c.label == "S2:2")
        self.assertEqual(live.affected_waybills, ["W2"])
        self.assertEqual(live.unaffected_waybills, ["W1", "W3", "W4"])

    def test_bad_revision_superseded_by_good_one(self):
        labels = [c.label for c in self.bundle.conflicts]
        self.assertNotIn("S2:3", labels)
        self.assertEqual(self.bundle.selection.chosen["S2"].rev, 3)

    def test_revision_chain_corrections_recorded(self):
        tickets = [(c.ticket, c.from_rev, c.to_rev, c.to_feasible)
                   for c in self.bundle.corrections]
        self.assertIn(("S2", 1, 2, False), tickets)
        self.assertIn(("S2", 2, 3, True), tickets)
        self.assertIn(("SD1", 1, 2, True), tickets)

    def test_good_revision_moves_buyer_balance_by_new_item(self):
        sd1 = next(c for c in self.bundle.corrections if c.ticket == "SD1")
        change = next(x for x in sd1.changes if x["waybill"] == "W1")
        self.assertGreater(change["entitlement_cents"][1], change["entitlement_cents"][0])
        w1 = self.bundle.buyers["W1"]
        self.assertEqual(w1.paid_cents, 8260)       # historical cash untouched
        self.assertEqual(w1.executable_next_cents, 2360)


if __name__ == "__main__":
    unittest.main()


class OverlappingObservationTests(unittest.TestCase):
    def test_held_node_does_not_block_other_waybills(self):
        from datetime import timedelta
        from recon.data import load_all, ScaleTicket
        from recon.engine import reconcile
        data = load_all()
        data["scales"].append(ScaleTicket(
            "SX", 1, "P3", 300, 4, 1,
            datetime.fromisoformat("2026-11-16T09:00:00+08:00"), None))
        bundle = reconcile(data=data)
        self.assertEqual([c.parcel for c in bundle.conflicts], ["P3"])
        self.assertTrue(bundle.buyers["W3"].held)
        self.assertFalse(bundle.buyers["W1"].held)
        self.assertFalse(bundle.buyers["W2"].held)
        self.assertFalse(bundle.buyers["W4"].held)
        self.assertEqual(bundle.buyers["W4"].executable_next_cents, 11610)
        self.assertTrue(bundle.close_summary()["identity"]["check_balances"])
