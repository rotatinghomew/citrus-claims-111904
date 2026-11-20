"""Joint maxima and aggregates must be attainable, not summed intervals."""
import unittest

from recon.attribution import WaybillAttribution, aggregate, joint_maxima
from recon.balance import Interval, MaterialBalance, Node


class SyntheticNetwork(unittest.TestCase):
    """100 kg from farm 甲 split into P1/P2, each shipped with 30 kg received.

    The two deliveries total exactly 60 kg, so the two shortages total exactly
    40 kg.  Each waybill may individually carry all 40 kg -- but both cannot be
    40 kg at once.  The joint check must catch that, and the aggregate must
    report 40 kg rather than the 80 kg a sum of interval highs would give.
    """

    def setUp(self):
        self.nodes = [
            Node("T1", "transport", ("P1",), ("D1",)),
            Node("T2", "transport", ("P2",), ("D2",)),
            Node("M", "split", ("A",), ("P1", "P2"), 0.0, 0.0),
        ]
        self.parcels = ["A", "P1", "P2", "D1", "D2"]
        self.farms = ["园甲"]
        self.ranges = {
            "A": (100.0, 100.0),
            "P1": (0.0, 99.0),
            "P2": (0.0, 99.0),
            "D1": (30.0, 30.0),
            "D2": (30.0, 30.0),
        }
        self.mb = MaterialBalance(self.nodes, self.parcels, self.farms,
                                  {"A": "园甲"}, self.ranges)

    def attribution(self, waybill, op):
        total = self.mb.range({("unexplained", op): 1})
        farm = self.mb.range({("farm_unexplained", op, "园甲"): 1})
        return WaybillAttribution(waybill, "买方", "C", total,
                                  {"园甲": farm}, confirmed_farms=[],
                                  unresolved_farms=["园甲"])

    def test_model_is_feasible(self):
        self.assertTrue(self.mb.feasible())

    def test_individual_maxima_are_large(self):
        u1 = self.mb.range({("unexplained", "T1"): 1})
        u2 = self.mb.range({("unexplained", "T2"): 1})
        self.assertAlmostEqual(u1.high, 40.0, places=4)
        self.assertAlmostEqual(u2.high, 40.0, places=4)

    def test_joint_maxima_detected_infeasible(self):
        att = {"W1": self.attribution("W1", "T1"),
               "W2": self.attribution("W2", "T2")}
        checks = joint_maxima(self.mb, att, self.farms)
        self.assertFalse(checks["buyers"].feasible)
        self.assertEqual(sorted(checks["buyers"].minimal_conflict), ["W1", "W2"])

    def test_aggregate_is_not_sum_of_highs(self):
        att = {"W1": self.attribution("W1", "T1"),
               "W2": self.attribution("W2", "T2")}
        agg = aggregate(self.mb, att, self.farms)
        buyer = agg["buyers"]["买方"]
        self.assertAlmostEqual(buyer["shortage_kg"]["high"], 40.0, places=4)
        self.assertGreater(buyer["sum_of_individual_highs_kg"],
                           buyer["shortage_kg"]["high"])

    def test_aggregate_never_exceeds_source(self):
        att = {"W1": self.attribution("W1", "T1"),
               "W2": self.attribution("W2", "T2")}
        agg = aggregate(self.mb, att, self.farms)
        self.assertLessEqual(agg["buyers"]["买方"]["shortage_kg"]["high"], 100.0)
        self.assertLessEqual(agg["farms"]["园甲"]["possible_kg"], 100.0)


if __name__ == "__main__":
    unittest.main()
