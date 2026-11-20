"""Behaviour under alternative document states, not just the shipped CSV."""
import unittest
from datetime import datetime

from recon.data import Claim, ContractVersion, FarmStatement, LedgerEntry, Recovery
from recon.engine import reconcile

JST = datetime.fromisoformat


def entry(eid, wb, party, kind, cents, at, refs="AG1"):
    return LedgerEntry(eid, wb, party, kind, cents, JST(at), (refs,))


class HistoricalReplayTests(unittest.TestCase):
    def test_payment_before_revision_cannot_be_clawed_back_retroactively(self):
        at_payment = reconcile(as_of=JST("2026-11-13T11:00:00+08:00"))
        now = reconcile()
        self.assertEqual(at_payment.buyers["W1"].paid_cents, 8260)
        self.assertEqual(now.buyers["W1"].paid_cents, 8260)
        # Later evidence raises the entitlement; the difference is a new item.
        self.assertLess(at_payment.buyers["W1"].entitlement_cents,
                        now.buyers["W1"].entitlement_cents)
        self.assertEqual(now.buyers["W1"].paid_cents
                         + now.buyers["W1"].executable_next_cents,
                         now.buyers["W1"].entitlement_cents)

    def test_overpayment_at_payment_time_is_not_clawed_back(self):
        # On 11-13 11:00 only S2 rev1 was visible: 17 kg -> 7670, but 8260 paid.
        at_payment = reconcile(as_of=JST("2026-11-13T11:00:00+08:00"))
        w1 = at_payment.buyers["W1"]
        self.assertEqual(w1.entitlement_cents, 7670)
        self.assertEqual(w1.executable_next_cents, 0)
        self.assertLess(w1.balance_cents, 0)   # negative = overpaid, for review

    def test_claims_not_yet_noticed_do_not_pay(self):
        early = reconcile(as_of=JST("2026-11-13T08:00:00+08:00"))
        # N12 arrives 09:00; at 08:00 only N11 (13 kg) was on file.
        self.assertEqual(early.entitlements["W1"].claimed_kg, 13.0)


class ConflictingReplacementTests(unittest.TestCase):
    def test_replacement_that_cannot_hold_is_quarantined(self):
        bundle = reconcile(overrides={"SD1": 1, "S2": 2})
        live = [c for c in bundle.conflicts if c.label == "live readings"]
        self.assertEqual({c.parcel for c in live}, {"P2"})
        self.assertEqual({wb for c in live for wb in c.affected_waybills}, {"W2"})
        self.assertFalse(bundle.buyers["W1"].held)
        self.assertTrue(bundle.buyers["W2"].held)
        self.assertFalse(bundle.buyers["W3"].held)
        self.assertFalse(bundle.buyers["W4"].held)
        self.assertEqual(bundle.buyers["W4"].executable_next_cents, 11610)

    def test_quarantine_does_not_rewrite_paid_cash(self):
        bundle = reconcile(overrides={"S2": 2})
        paid_entries = [e for e in bundle.data["ledger"] if e.kind == "已付"]
        self.assertEqual(sum(e.cents for e in paid_entries), 16840)
        self.assertEqual(
            sum(b.paid_cents for b in bundle.buyers.values()), 16840)


class CreditOrderingTests(unittest.TestCase):
    def test_same_farm_two_claims_first_confirmed_gets_paid_first(self):
        bundle = reconcile()
        w4 = next(o for o in bundle.orchards if o.waybill == "W4" and o.farm == "园甲")
        # F-A47 has 2700 gross + 2000 posted return = 4700 available.
        self.assertEqual(w4.newly_applied_cents, 4700)
        self.assertEqual(w4.open_cents, 6910)
        self.assertEqual(w4.amount_cents, 11610)


class SourceIndependenceTests(unittest.TestCase):
    def test_last_week_paid_is_not_erased_by_return(self):
        bundle = reconcile()
        refund = bundle.credit["refund_items"]
        self.assertEqual(len(refund), 1)
        item = refund[0]
        self.assertEqual(item["unsupported_deduction_cents"], 2600)
        # The closed week still shows the original 2600 deduction.
        f_a46 = next(s for s in bundle.credit["statements"]
                     if s["statement"] == "F-A46")
        self.assertEqual(f_a46["existing_deductions_cents"], 2600)
        self.assertTrue(f_a46["closed"])
        self.assertEqual(f_a46["available_for_new_cents"], 0)

    def test_unposted_return_releases_no_credit(self):
        bundle = reconcile(as_of=JST("2026-11-16T12:00:00+08:00"))
        f_a47 = next(s for s in bundle.credit["statements"]
                     if s["statement"] == "F-A47")
        self.assertEqual(f_a47["returns_pending_cents"], 2000)
        self.assertEqual(f_a47["returns_posted_cents"], 0)
        # Only the gross weekly payment is available; the pending return is not.
        self.assertEqual(f_a47["newly_applied_cents"], 2700)

    def test_posted_return_releases_credit(self):
        bundle = reconcile(as_of=JST("2026-11-17T12:00:00+08:00"))
        f_a47 = next(s for s in bundle.credit["statements"]
                     if s["statement"] == "F-A47")
        self.assertEqual(f_a47["returns_pending_cents"], 0)
        self.assertEqual(f_a47["returns_posted_cents"], 2000)
        # The posted return frees 2000 more for the same week's claim.
        self.assertEqual(f_a47["newly_applied_cents"], 4700)


if __name__ == "__main__":
    unittest.main()
