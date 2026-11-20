"""Weekly credit: closure, return states, and shared statements."""
import unittest
from datetime import datetime

from recon.data import FarmStatement, Recovery
from recon.farmcredit import OpenClaim, apply_credit, build_statements

JST = datetime.fromisoformat


def st(name, farm, week, gross, closed=None):
    """A statement that is open unless a close time is given."""
    return FarmStatement(name, farm, week, JST("2026-11-16T06:00:00+08:00"),
                         gross, JST(closed) if closed else None)


def rec(entry, statement, direction, state, cents, at, wb=None, original=None):
    return Recovery(entry, statement, direction, state, cents, JST(at), wb, original)


class StatementBuilding(unittest.TestCase):
    def test_duplicate_states_of_one_entry_are_one_item(self):
        from recon.engine import effective_recoveries
        rows = [
            rec("H", "S1", "返还", "待付", 2000, "2026-11-16T09:00:00+08:00"),
            rec("H", "S1", "返还", "入账", 2000, "2026-11-17T09:00:00+08:00"),
        ]
        effective = effective_recoveries(rows)
        self.assertEqual(len(effective), 1)
        self.assertEqual(effective[0].state, "入账")

    def test_effective_recovery_keeps_latest_state(self):
        from recon.engine import effective_recoveries
        rows = [
            rec("H", "S1", "返还", "入账", 2000, "2026-11-17T09:00:00+08:00"),
            rec("H", "S1", "返还", "待付", 2000, "2026-11-16T09:00:00+08:00"),
        ]
        # The later timestamp wins regardless of row order in the CSV.
        self.assertEqual(effective_recoveries(rows)[0].state, "入账")


class Closure(unittest.TestCase):
    def test_closed_statement_keeps_cash_but_offers_no_credit(self):
        statements = build_statements(
            [st("S1", "园甲", "2026-W46", 3500, closed="2026-11-15T18:00:00+08:00")],
            [rec("K", "S1", "扣抵", "入账", 2600, "2026-11-13T17:00:00+08:00", "W1")])
        self.assertTrue(statements[0].closed)
        self.assertEqual(statements[0].existing_deductions, 2600)
        self.assertEqual(statements[0].available_for_new, 0)

    def test_return_posted_into_later_week_frees_later_credit_only(self):
        statements = build_statements(
            [st("S46", "园甲", "2026-W46", 3500, closed="2026-11-15T18:00:00+08:00"),
             st("S47", "园甲", "2026-W47", 2700)],
            [rec("H", "S47", "返还", "入账", 2000, "2026-11-17T09:00:00+08:00",
                 "W1", "K")])
        self.assertEqual(statements[0].available_for_new, 0)
        self.assertEqual(statements[1].available_for_new, 4700)


class Ordering(unittest.TestCase):
    def test_earlier_confirmation_takes_credit_first(self):
        statements = build_statements([st("S47", "园甲", "2026-W47", 1000)], [])
        claims = [
            OpenClaim("b", "W4", "园甲", 800, "2026-11-14T10:00:00+08:00"),
            OpenClaim("a", "W1", "园甲", 800, "2026-11-13T10:00:00+08:00"),
        ]
        apply_credit(claims, statements)
        by_key = {c.key: c for c in claims}
        self.assertEqual(by_key["a"].applied, 800)
        self.assertEqual(by_key["b"].applied, 200)
        self.assertEqual(by_key["b"].remaining, 600)

    def test_one_claim_can_span_two_open_weeks_of_same_farm(self):
        statements = build_statements(
            [st("S47", "园甲", "2026-W47", 1000),
             st("S48", "园甲", "2026-W48", 1000)], [])
        claims = [OpenClaim("a", "W1", "园甲", 1500, "2026-11-13T10:00:00+08:00")]
        apply_credit(claims, statements)
        self.assertEqual(claims[0].applied, 1500)
        self.assertEqual(claims[0].remaining, 0)

    def test_credit_is_per_farm_not_shared_across_farms(self):
        statements = build_statements(
            [st("A", "园甲", "2026-W47", 1000),
             st("B", "园乙", "2026-W47", 1000)], [])
        claims = [OpenClaim("a", "W1", "园甲", 2000, "2026-11-13T10:00:00+08:00")]
        apply_credit(claims, statements)
        self.assertEqual(claims[0].applied, 1000)
        self.assertEqual(claims[0].remaining, 1000)


if __name__ == "__main__":
    unittest.main()
