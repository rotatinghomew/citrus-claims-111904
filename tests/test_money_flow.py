"""Buyer cash and orchard cash must never be conflated."""
import unittest

from recon.engine import reconcile


class CleanBuyerCash(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = reconcile()

    def test_entitlement_is_separate_from_attribution(self):
        # W1 has no attributable farm at all, yet the buyer is owed money.
        self.assertEqual(self.bundle.attributions["W1"].confirmed_kg(), 0.0)
        self.assertEqual(self.bundle.buyers["W1"].entitlement_cents, 10620)
        # W4's orchard is pinned, but the buyer has not been paid yet.
        self.assertEqual(self.bundle.attributions["W4"].confirmed_kg(), 23.0)
        self.assertEqual(self.bundle.buyers["W4"].paid_cents, 0)

    def test_one_shortage_not_paid_to_two_customers(self):
        # Each waybill's shortage is counted once, for its own buyer only.
        for wb, ent in self.bundle.entitlements.items():
            self.assertEqual(ent.buyer, self.bundle.attributions[wb].buyer)
        totals = {}
        for ent in self.bundle.entitlements.values():
            totals[ent.buyer] = totals.get(ent.buyer, 0) + ent.entitlement_cents
        self.assertEqual(sum(totals.values()),
                         sum(e.entitlement_cents for e in self.bundle.entitlements.values()))

    def test_limitation_per_waybill_not_per_notice(self):
        w1 = self.bundle.entitlements["W1"]
        self.assertEqual(len(w1.claims), 2)
        self.assertEqual(w1.deductible_kg, 4.0)
        # 22 confirmed - 4 deductible = 18 kg, not 22 - 2*4.
        self.assertEqual(w1.payable_kg, 18.0)

    def test_cap_cumulative(self):
        ent = self.bundle.entitlements["W4"]
        self.assertLessEqual(ent.entitlement_cents, ent.cap_cents)

    def test_orchard_deduction_not_reduced_by_buyer_payment(self):
        w3 = self.bundle.entitlements["W3"]
        orchard = next(o for o in self.bundle.orchards if o.waybill == "W3")
        self.assertEqual(orchard.amount_cents, w3.entitlement_cents)


class ReviewedAdjustment(unittest.TestCase):
    def test_revision_that_reduces_entitlement_does_not_take_cash_back(self):
        bundle = reconcile(overrides={"SD1": 1})
        w1 = bundle.buyers["W1"]
        self.assertEqual(w1.paid_cents, 8260)          # untouched
        self.assertLess(w1.balance_cents, 0)           # overpaid, not clawed back
        review = [p for p in bundle.payments if p.kind == "追回"]
        self.assertTrue(review)
        item = review[0]
        self.assertEqual(item.status, "待复核")
        self.assertEqual(item.cents, 590)
        self.assertNotIn(item.entry.replace("REVIEW-", "TOPUP-"),
                         {p.entry for p in bundle.payments})

    def test_next_run_lists_review_items_separately(self):
        bundle = reconcile(overrides={"SD1": 1})
        kinds = {item["kind"] for item in bundle.schedule()}
        self.assertIn("追回", kinds)
        self.assertIn("补付", kinds)
        self.assertIn("返还", kinds)

    def test_revision_that_raises_entitlement_creates_top_up(self):
        bundle = reconcile()
        w1 = bundle.buyers["W1"]
        self.assertEqual(w1.executable_next_cents, 2360)
        self.assertEqual(w1.entitlement_cents - w1.paid_cents, 2360)


class ReturnHandling(unittest.TestCase):
    def test_return_is_an_item_not_a_negative_deduction(self):
        bundle = reconcile()
        refund = bundle.credit["refund_items"][0]
        self.assertEqual(refund["posted_return_cents"], 2000)
        self.assertEqual(refund["outstanding_return_cents"], 600)
        # The original closed week is unchanged.
        closed = next(s for s in bundle.credit["statements"]
                      if s["statement"] == "F-A46")
        self.assertEqual(closed["existing_deductions_cents"], 2600)
        self.assertEqual(closed["returns_posted_cents"], 0)


class PartialAttribution(unittest.TestCase):
    def test_unpinned_part_does_not_load_onto_one_orchard(self):
        from recon.engine import _farm_share_cents
        # Only 10 of the confirmed 20 kg are pinned; the orchard bears half.
        self.assertEqual(_farm_share_cents(1000, {"园甲": 10.0},
                                           confirmed_total=20.0), {"园甲": 500})
        # Cents always add up exactly.
        parts = _farm_share_cents(1000, {"园甲": 3.0, "园乙": 3.0},
                                  confirmed_total=20.0)
        self.assertEqual(sum(parts.values()), 300)


if __name__ == "__main__":
    unittest.main()
