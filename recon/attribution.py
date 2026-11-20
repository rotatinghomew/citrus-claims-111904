"""What can and cannot be said about a transport shortage.

For each waybill the transport node carries an ``unexplained`` quantity.  The
total is one range; the farm split is another.  A farm share can be zero while
the total is confirmed, and a parcel that can be traced to an orchard does not
mean every kilogram of the shortage came from that orchard.

``joint_maxima`` answers the reviewer's question: can the largest defensible
amount for several customers hold at the same time?  Taking each range's upper
bound separately and adding them is not a proof of anything; the joint test
asks the LP whether all those lower bounds are simultaneously satisfiable.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from .balance import Interval, MaterialBalance


@dataclass
class Allocation:
    """One party's confirmed and still-unconfirmed quantity."""

    party: str
    confirmed_kg: float
    possible_kg: float  # total upper bound
    note: str = ""

    @property
    def unconfirmed_kg(self) -> float:
        return max(0.0, self.possible_kg - self.confirmed_kg)

    def as_dict(self) -> dict:
        return {
            "party": self.party,
            "confirmed_kg": round(self.confirmed_kg, 4),
            "possible_kg": round(self.possible_kg, 4),
            "unconfirmed_kg": round(self.unconfirmed_kg, 4),
            "note": self.note,
        }


@dataclass
class WaybillAttribution:
    waybill: str
    buyer: str
    contract: str
    shortage: Interval
    farms: dict[str, Interval]
    confirmed_farms: list[str] = field(default_factory=list)
    unresolved_farms: list[str] = field(default_factory=list)
    conflict: bool = False
    destination: str = ""

    def confirmed_kg(self) -> float:
        """Only amounts whose farm is pinned can be charged to an orchard."""
        return round(sum(self.farms[f].low for f in self.confirmed_farms), 4)

    def unresolved_kg(self) -> float:
        return round(self.shortage.high - self.confirmed_kg(), 4)

    def as_dict(self) -> dict:
        return {
            "waybill": self.waybill,
            "buyer": self.buyer,
            "contract": self.contract,
            "shortage_low_kg": round(self.shortage.low, 4),
            "shortage_high_kg": round(self.shortage.high, 4),
            "farms": {f: {"low_kg": round(iv.low, 4), "high_kg": round(iv.high, 4)}
                      for f, iv in self.farms.items()},
            "confirmed_farms": self.confirmed_farms,
            "unresolved_farms": self.unresolved_farms,
            "confirmed_kg": self.confirmed_kg(),
            "unresolved_kg": self.unresolved_kg(),
            "conflict": self.conflict,
        }


def transport_node_for(waybill: str, nodes, waybill_rows=None) -> str:
    """Find the transport operation that feeds this waybill's destination.

    Waybill numbers need not be consecutive (W4 goes to D5 because P5 was the
    split remainder), so match on the destination parcel, not on the number.
    """
    destination = None
    if waybill_rows:
        destination = next((w.destination for w in waybill_rows if w.waybill == waybill), None)
    for node in nodes:
        if node.kind == "transport" and node.outputs and (
                destination in node.outputs if destination
                else any(o.startswith("D") for o in node.outputs)):
            return node.operation
    raise KeyError(f"no transport node for {waybill}")


def attributable(mb: MaterialBalance, operation: str, farms: list[str],
                 material: str = "") -> dict[str, Interval]:
    out = {}
    for farm in farms:
        iv = mb.range({("farm_unexplained", operation, farm): 1})
        if iv is not None:
            out[farm] = iv
    return out


def attribute_waybills(mb: MaterialBalance, waybills, farms: list[str], nodes) -> dict[str, WaybillAttribution]:
    result: dict[str, WaybillAttribution] = {}
    for wb in waybills:
        op = transport_node_for(wb.waybill, nodes, waybills)
        total = mb.range({("unexplained", op): 1})
        if total is None:
            result[wb.waybill] = WaybillAttribution(
                wb.waybill, wb.buyer, wb.contract, Interval(0, 0), {},
                conflict=True, destination=wb.destination)
            continue
        splits = attributable(mb, op, farms)
        confirmed = [f for f, iv in splits.items() if iv.low > 1e-6]
        unresolved = [f for f, iv in splits.items() if iv.low <= 1e-6 < iv.high]
        result[wb.waybill] = WaybillAttribution(
            waybill=wb.waybill, buyer=wb.buyer, contract=wb.contract,
            shortage=total, farms=splits, confirmed_farms=confirmed,
            unresolved_farms=unresolved, destination=wb.destination)
    return result


@dataclass
class JointCheck:
    parties: list[str]
    constraints: list[tuple[str, float]]
    feasible: bool
    minimal_conflict: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "parties": list(self.parties),
            "constraints": [{"party": p, "at_least_kg": round(v, 4)}
                            for p, v in self.constraints],
            "feasible_together": self.feasible,
            "minimal_conflict": list(self.minimal_conflict),
        }


def joint_maxima(mb: MaterialBalance, attributions: dict[str, WaybillAttribution],
                 farms: list[str] | None = None) -> dict[str, JointCheck]:
    """Can every party reach its individual maximum at the same time?

    Run once for buyers (total shortage) and once per farm (that farm's share),
    because adding per-party upper bounds that are not jointly attainable would
    overstate what can be recovered.
    """
    checks: dict[str, JointCheck] = {}
    buyer_constraints = []
    for name, att in attributions.items():
        op = att.waybill
        buyer_constraints.append((name, att.shortage.high,
                                  {("unexplained", _op(att)): 1}))
    checks["buyers"] = _check(mb, "buyers", buyer_constraints)
    if farms:
        for farm in farms:
            cons = []
            for name, att in attributions.items():
                iv = att.farms.get(farm)
                if iv is None or iv.high <= 1e-6:
                    continue
                cons.append((name, iv.high, {("farm_unexplained", _op(att), farm): 1}))
            if cons:
                checks[farm] = _check(mb, farm, cons)
    return checks


def _op(att: WaybillAttribution) -> str:
    return "T" + att.waybill[1:]


def _check(mb: MaterialBalance, label: str, constraints) -> JointCheck:
    parties = [c[0] for c in constraints]
    simple = [(c[0], c[1]) for c in constraints]
    if not constraints:
        return JointCheck([label], [], True)
    feasible = mb.simultaneous([(c[2], c[1]) for c in constraints])
    if feasible:
        return JointCheck(parties, simple, True)
    conflict: list[str] = []
    for size in range(1, len(constraints) + 1):
        for subset in itertools.combinations(range(len(constraints)), size):
            if not mb.simultaneous([(constraints[i][2], constraints[i][1]) for i in subset]):
                conflict = [constraints[i][0] for i in subset]
                break
        if conflict:
            break
    return JointCheck(parties, simple, False, conflict)


def buyer_allocations(attributions: dict[str, WaybillAttribution]) -> dict[str, list[Allocation]]:
    """Per buyer: confirmed vs possible total, then per farm only what is pinned."""
    by_buyer: dict[str, list[Allocation]] = {}
    for att in attributions.values():
        confirmed = att.confirmed_kg()
        possible = att.shortage.high
        by_buyer.setdefault(att.buyer, []).append(Allocation(
            party=att.buyer,
            confirmed_kg=min(confirmed, possible),
            possible_kg=possible,
            note=f"{att.waybill} confirmed farm amount {confirmed:.2f} kg of "
                 f"{att.shortage.low:.2f}-{possible:.2f} kg",
        ))
    return by_buyer


def aggregate(mb: MaterialBalance, attributions: dict[str, WaybillAttribution],
              farms: list[str]) -> dict:
    """True aggregate ranges, not sums of per-waybill interval bounds.

    Summing each waybill's upper bound would overstate what can be recovered:
    two waybills can each reach 28 kg only if the material can be split that
    way at the same time.  Here the sum itself is optimised, so the reported
    aggregate is attainable.
    """
    buyers: dict[str, dict] = {}
    by_buyer: dict[str, list[str]] = {}
    for wb, att in attributions.items():
        if att.conflict:
            continue
        by_buyer.setdefault(att.buyer, []).append(wb)
    for buyer, wbs in by_buyer.items():
        weights = {("unexplained", _op(attributions[wb])): 1.0 for wb in wbs}
        total = mb.range(weights)
        buyers[buyer] = {
            "waybills": wbs,
            "shortage_kg": total.as_dict() if total else {"low": 0.0, "high": 0.0},
            "confirmed_farm_kg": round(sum(attributions[wb].confirmed_kg()
                                           for wb in wbs), 4),
            "unresolved_kg": round(sum(attributions[wb].unresolved_kg()
                                       for wb in wbs), 4),
            "sum_of_individual_highs_kg": round(
                sum(attributions[wb].shortage.high for wb in wbs), 4),
        }
    farm_totals: dict[str, dict] = {}
    for farm in farms:
        weights = {}
        for wb, att in attributions.items():
            if att.conflict:
                continue
            key = ("farm_unexplained", _op(att), farm)
            if key in mb._index:
                weights[key] = 1.0
        if not weights:
            continue
        total = mb.range(weights)
        if total is None or total.high <= 1e-6:
            continue
        farm_totals[farm] = {
            "confirmed_kg": round(total.low, 4),
            "possible_kg": round(total.high, 4),
            "waybills": [wb for wb, att in attributions.items()
                         if not att.conflict and farm in att.farms
                         and att.farms[farm].high > 1e-6],
        }
    return {"buyers": buyers, "farms": farm_totals}
