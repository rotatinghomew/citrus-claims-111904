import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

import unittest

from recon.balance import Interval, MaterialBalance, build_nodes
from recon.contracts import contract_for, entitlements
from recon.data import load_all
from recon.engine import reconcile
from recon.farmcredit import (OpenClaim, WeekStatement, apply_credit,
                              best_case_cover, build_statements)
from recon.money import Adjustment, AdjustmentLedger, compare_positions
from recon.scales import ScaleConflict, resolve_scales

ROOT = Path(__file__).resolve().parents[1]
JST = lambda s: datetime.fromisoformat(s)


class ScaleRevisionTests(unittest.TestCase):
    def setUp(self):
        self.data = load_all()

    def test_latest_revision_per_ticket_only(self):
        sel = resolve_scales(self.data["scales"])
        self.assertEqual(sel.chosen["S2"].rev, 3)
        self.assertEqual(sel.chosen["S2"].parcel, "P2")
        # SA names no predecessor and is not replaced by anything else.
        self.assertEqual(sel.chosen["SA"].rev, 1)

    def test_revision_replaces_only_named_version(self):
        sel = resolve_scales(self.data["scales"])
        self.assertEqual(sel.chosen["SD1"].rev, 2)
        self.assertEqual(sel.chosen["SD1"].replaces, 1)
        self.assertEqual(sel.chosen["SD5"].rev, 1)

    def test_net_uses_that_tickets_own_tare_and_tolerance(self):
        sel = resolve_scales(self.data["scales"])
        p2 = sel.chosen["S2"]
        self.assertEqual(p2.net_kg, 315.0)
        self.assertEqual(p2.low, 314.0)
        self.assertEqual(p2.high, 316.0)

    def test_as_of_hides_later_revisions(self):
        early = resolve_scales(self.data["scales"], as_of=JST("2026-11-14T14:30:00+08:00"))
        self.assertEqual(early.chosen["SD1"].rev, 2)
        self.assertEqual(early.chosen["S2"].rev, 1)

    def test_disjoint_observations_of_same_parcel_conflict(self):
        from recon.data import ScaleTicket
        a = ScaleTicket("X", 1, "P", 100, 0, 1, JST("2026-11-12T00:00:00+08:00"), None)
        b = ScaleTicket("Y", 1, "P", 120, 0, 1, JST("2026-11-12T01:00:00+08:00"), None)
        sel = resolve_scales([a, b])
        with self.assertRaises(ScaleConflict):
            sel.parcel_ranges()


class BalanceTests(unittest.TestCase):
    def setUp(self):
        self.data = load_all()
        self.nodes = build_nodes(self.data["movements"])
        self.parcels = sorted({p for n in self.nodes for p in n.inputs + n.outputs})
        self.farms = sorted(set(self.data["origins"].values()))
        self.sel = resolve_scales(self.data["scales"])

    def _mb(self, ranges=None):
        return MaterialBalance(self.nodes, self.parcels, self.farms,
                               self.data["origins"],
                               ranges if ranges is not None else self.sel.parcel_ranges())

    def test_balance_is_feasible_on_clean_record(self):
        self.assertTrue(self._mb().feasible())

    def test_shortage_lives_in_unexplained_not_in_a_receive_node(self):
        mb = self._mb()
        for wb, op in [("W1", "T1"), ("W2", "T2"), ("W3", "T3"), ("W4", "T4")]:
            iv = mb.range({("unexplained", op): 1})
            self.assertIsNotNone(iv)
            self.assertGreater(iv.low, 0)
        # The split at M2 explains part of its own loss; its unexplained piece
        # must not be added to any customer's receiving shortage.
        m2 = mb.range({("unexplained", "M2"): 1})
        self.assertGreaterEqual(m2.low, 0)

    def test_single_farm_shortage_is_pinned(self):
        mb = self._mb()
        w4 = mb.range({("farm_unexplained", "T4", "园甲"): 1})
        other = mb.range({("farm_unexplained", "T4", "园乙"): 1})
        self.assertGreater(w4.low, 0)
        self.assertAlmostEqual(other.high, 0)

    def test_traceable_parcel_does_not_pin_mixed_waybill(self):
        mb = self._mb()
        farms = {f: mb.range({("farm_unexplained", "T1", f): 1}) for f in self.farms}
        self.assertEqual(farms["园甲"].low, 0)   # W1 is the whole farm-agnostic case
        self.assertEqual(farms["园乙"].low, 0)

    def test_incompatible_revision_makes_whole_system_infeasible(self):
        ranges = dict(self.sel.parcel_ranges())
        ranges["P2"] = (343.0, 345.0)   # S2 rev 2
        self.assertFalse(self._mb(ranges).feasible())


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.data = load_all()
        self.w1 = next(w for w in self.data["waybills"] if w.waybill == "W1")

    def test_uses_version_in_force_at_first_shipment(self):
        version = contract_for(self.w1, JST("2026-11-12T08:00:00+08:00"),
                               self.data["contracts"])
        self.assertEqual(version.version, "2026秋")

    def test_later_contract_does_not_retroactively_apply(self):
        version = contract_for(self.w1, JST("2026-11-12T08:00:00+08:00"),
                               [c for c in self.data["contracts"] if c.contract == "AG1"])
        self.assertEqual(version.deductible_kg, 4)

    def test_notices_on_same_waybill_share_one_deductible_and_cap(self):
        ents = entitlements(
            [self.w1], {"W1": 22.0},
            {"W1": JST("2026-11-12T08:00:00+08:00")},
            [c for c in self.data["claims"] if c.waybill == "W1"],
            self.data["contracts"], self.data["ledger"])
        w1 = ents["W1"]
        self.assertEqual(w1.claimed_kg, 29.0)
        self.assertEqual(w1.eligible_kg, 22.0)      # notices cannot inflate proof
        self.assertEqual(w1.payable_kg, 18.0)       # one deductible, not two
        self.assertEqual(w1.entitlement_cents, 10620)

    def test_cap_applies_per_waybill(self):
        from recon.data import Claim
        big = Claim("N99", "W1", JST("2026-11-13T09:00:00+08:00"), 100.0)
        ents = entitlements(
            [self.w1], {"W1": 100.0},
            {"W1": JST("2026-11-12T08:00:00+08:00")},
            [big], self.data["contracts"], self.data["ledger"])
        # 96 payable kg x 590 would be 56,640 but the AG1 cap is 15,000.
        self.assertEqual(ents["W1"].entitlement_cents, 15000)


class CreditTests(unittest.TestCase):
    def _statement(self, name, farm, week, gross, closed=False):
        return WeekStatement(statement=name, farm=farm, week=week,
                             gross_available_cents=gross, posted_at="2026-11-16T06:00:00+08:00",
                             closed=closed)

    def test_closed_week_offers_no_new_credit(self):
        st = [self._statement("F1", "园甲", "2026-W46", 3500, closed=True)]
        claims = [OpenClaim("c", "W1", "园甲", 2000, "2026-11-13T17:00:00+08:00",
                            already_deducted=2600)]
        apply_credit(claims, st)
        self.assertEqual(claims[0].applied, 0)
        self.assertEqual(claims[0].remaining, 0)   # already deducted, not new

    def test_first_confirmed_consumes_credit_before_later_claim(self):
        st = [self._statement("F1", "园甲", "2026-W47", 3000)]
        claims = [OpenClaim("late", "W4", "园甲", 2000, "2026-11-14T10:00:00+08:00"),
                  OpenClaim("early", "W1", "园甲", 2000, "2026-11-13T10:00:00+08:00")]
        apply_credit(claims, st)
        early = next(c for c in claims if c.key == "early")
        late = next(c for c in claims if c.key == "late")
        self.assertEqual(early.applied, 2000)
        self.assertEqual(late.applied, 1000)
        self.assertEqual(late.remaining, 1000)

    def test_pending_return_frees_nothing(self):
        st = [self._statement("F1", "园甲", "2026-W47", 1000)]
        st[0].returns_pending = 2000
        claims = [OpenClaim("c", "W4", "园甲", 1500, "2026-11-14T00:00:00+08:00")]
        apply_credit(claims, st)
        self.assertEqual(claims[0].applied, 1000)

    def test_posted_return_frees_credit(self):
        st = [self._statement("F1", "园甲", "2026-W47", 1000)]
        st[0].returns_posted = 2000
        claims = [OpenClaim("c", "W4", "园甲", 2500, "2026-11-14T00:00:00+08:00")]
        apply_credit(claims, st)
        self.assertEqual(claims[0].applied, 2500)

    def test_best_case_never_exceeds_real_credit(self):
        rows = [
            {"waybill": "W1", "entitlement_cents": 5000,
             "farm_caps_cents": {"园甲": 5000, "园乙": 5000}},
            {"waybill": "W2", "entitlement_cents": 5000,
             "farm_caps_cents": {"园甲": 5000, "园乙": 5000}},
        ]
        cover = best_case_cover(rows, {"园甲": 1500, "园乙": 1000})
        self.assertEqual(cover["coverable_cents"], 2500)
        self.assertEqual(cover["not_coverable_even_best_case_cents"], 7500)
        self.assertEqual(sum(cover["credit_after_best_case"].values()), 0)


class AdjustmentTests(unittest.TestCase):
    def test_same_review_twice_creates_one_item(self):
        ledger = AdjustmentLedger()
        adj = Adjustment("v1", "farm_transfer", "W1", False, True, 0, {"园甲": -500, "园乙": 500}, "x")
        self.assertTrue(ledger.apply(adj))
        self.assertFalse(ledger.apply(adj))
        self.assertEqual(ledger.duplicates, ["v1"])
        self.assertEqual(len(ledger.applied), 1)

    def test_buyer_and_orchard_effects_are_separate(self):
        before = {"W1": {"entitlement_cents": 1000, "farms": {"园甲": 1000}}}
        after = {"W1": {"entitlement_cents": 1200, "farms": {"园甲": 700, "园乙": 500}}}
        adj = compare_positions(before, after)[0]
        self.assertTrue(adj.affects_buyer)
        self.assertTrue(adj.affects_orchard)
        self.assertEqual(adj.buyer_delta_cents, 200)
        self.assertEqual(adj.farm_deltas, {"园甲": -300, "园乙": 500})

    def test_farm_only_transfer_leaves_buyer_alone(self):
        before = {"W1": {"entitlement_cents": 1000, "farms": {"园甲": 1000}}}
        after = {"W1": {"entitlement_cents": 1000, "farms": {"园乙": 1000}}}
        adj = compare_positions(before, after)[0]
        self.assertFalse(adj.affects_buyer)
        self.assertTrue(adj.affects_orchard)
        self.assertEqual(adj.buyer_delta_cents, 0)


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = reconcile()

    def test_conflict_names_waybills_but_does_not_block_others(self):
        conflict = next(c for c in self.bundle.conflicts if c.label == "S2:2")
        self.assertEqual(conflict.affected_waybills, ["W2"])
        self.assertIn("W1", conflict.unaffected_waybills)
        self.assertIn("W3", conflict.unaffected_waybills)
        self.assertIn("W4", conflict.unaffected_waybills)
        self.assertIn("313.00", conflict.basis)

    def test_conflict_does_not_create_new_shortage(self):
        w2 = self.bundle.attributions["W2"]
        self.assertFalse(w2.conflict)          # quarantined revision is not live
        self.assertGreater(w2.shortage.low, 0)
        self.assertEqual(self.bundle.buyers["W2"].balance_cents, 450)

    def test_buyer_payments_do_not_depend_on_farm_attribution(self):
        w1 = self.bundle.buyers["W1"]
        att = self.bundle.attributions["W1"]
        self.assertEqual(att.confirmed_kg(), 0)   # no farm can be pinned
        self.assertGreater(w1.entitlement_cents, 0)  # but the buyer is still owed

    def test_paid_amount_is_not_overwritten_by_revision(self):
        # L11 paid 8260 before the D1 revision; it stays on the books.
        paid = [p for p in self.bundle.buyers["W1"].adjustments]
        self.assertTrue(all(p.kind != "已付" for p in paid))
        self.assertEqual(self.bundle.buyers["W1"].paid_cents, 8260)

    def test_close_identity_holds(self):
        ident = self.bundle.close_summary()["identity"]
        self.assertTrue(ident["check_balances"])
        split = self.bundle.close_summary()["split_of_entitlement"]
        self.assertTrue(split["reconciles"])

    def test_pending_payment_does_not_reduce_paid(self):
        w1 = self.bundle.buyers["W1"]
        self.assertEqual(w1.pending_cents, 2360)
        self.assertEqual(w1.paid_cents, 8260)
        self.assertEqual(w1.executable_next_cents, 2360)

    def test_joint_maxima_reported_for_each_farm(self):
        for check in self.bundle.joint.values():
            self.assertTrue(check.feasible)

    def test_farm_credit_respects_confirmation_order(self):
        claims = [c for c in self.bundle.credit["orchard_claims"]]
        for claim in claims:
            self.assertLessEqual(claim["executed_cents"] + claim["applied_cents"],
                                 claim["amount_cents"])

    def test_return_is_separate_from_original_deduction(self):
        refund = self.bundle.credit["refund_items"][0]
        self.assertEqual(refund["unsupported_deduction_cents"], 2600)
        self.assertEqual(refund["posted_return_cents"], 2000)
        self.assertEqual(refund["outstanding_return_cents"], 600)

    def test_as_of_replay_is_reproducible(self):
        once = reconcile(as_of=JST("2026-11-13T11:00:00+08:00"))
        twice = reconcile(as_of=JST("2026-11-13T11:00:00+08:00"))
        self.assertEqual(once.buyers["W1"].paid_cents, twice.buyers["W1"].paid_cents)
        self.assertEqual(once.buyers["W1"].entitlement_cents, 7670)  # S2 rev1 in force

    def test_audit_shows_documents_and_current_difference(self):
        audit = self.bundle.audit("W1")
        self.assertEqual(audit["contract_now"]["version"], "2026秋")
        payment = audit["payments_then"][0]
        self.assertEqual(payment["contract"]["version"], "2026秋")
        self.assertTrue(payment["documents_used"])
        self.assertEqual(payment["entitlement_then_cents"], 7670)
        self.assertEqual(audit["now"]["paid_cents"], 8260)
        self.assertEqual(audit["now"]["next_cents"], 2360)


if __name__ == "__main__":
    unittest.main()


class OutputTests(unittest.TestCase):
    def test_report_renders_and_outputs_written(self):
        import tempfile
        from pathlib import Path
        from recon.engine import reconcile
        from recon.report import to_markdown, write
        bundle = reconcile()
        text = to_markdown(bundle)
        for section in ("买方应赔", "果园应扣", "资料冲突", "关账恒等式", "挂起"):
            self.assertIn(section, text)
        with tempfile.TemporaryDirectory() as tmp:
            paths = write(bundle, Path(tmp))
            self.assertTrue(paths["json"].exists())
            self.assertTrue(paths["markdown"].exists())
            payload = json.loads(paths["json"].read_text(encoding="utf-8"))
            self.assertTrue(payload["close"]["identity"]["check_balances"])


class InvariantTests(unittest.TestCase):
    """Accounting identities must hold in every scenario we can produce."""

    SCENARIOS = [
        ("clean", {}),
        ("early-SD1", {"overrides": {"SD1": 1}}),
        ("bad-S2", {"overrides": {"S2": 1, "SD1": 1}}),
        ("as-of-payment", {"as_of": JST("2026-11-13T11:00:00+08:00")}),
        ("as-of-mid", {"as_of": JST("2026-11-15T12:00:00+08:00")}),
    ]

    def test_identities_hold_everywhere(self):
        for label, kwargs in self.SCENARIOS:
            with self.subTest(scenario=label):
                bundle = reconcile(**kwargs)
                close = bundle.close_summary()
                ident = close["identity"]
                self.assertEqual(
                    ident["paid_cents"] + ident["next_due_cents"],
                    ident["entitlement_cents"] + ident["overpaid_cents"])
                split = close["split_of_entitlement"]
                self.assertEqual(
                    split["confirmed_to_named_orchard_cents"]
                    + split["not_yet_attributable_cents"],
                    ident["entitlement_cents"])
                for o in bundle.orchards:
                    self.assertEqual(
                        o.deducted_cents + o.newly_applied_cents + o.open_cents,
                        o.amount_cents, (o.waybill, o.farm))
                    self.assertLessEqual(
                        o.amount_cents,
                        bundle.entitlements[o.waybill].entitlement_cents)
                self.assertEqual(
                    split["confirmed_to_named_orchard_cents"],
                    sum(o.amount_cents for o in bundle.orchards))
