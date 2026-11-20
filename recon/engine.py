"""Top-level reconciliation: documents in, separated positions out.

The engine keeps four questions visibly separate:

1. **What does the weighing record admit?**  Every scale ticket is a range; the
   movements are one material-balance system.  We never pick an arbitrary farm
   split, and we never turn a record conflict into a new shortage.
2. **What may the buyer be paid?**  Contract version at first shipment,
   cumulative deductible and cap per waybill, confirmed shortage only.
3. **What may the orchard repay?**  Only quantities pinned to that orchard,
   taken from its shared weekly goods payment in first-confirmation order.
4. **What changed, and what is still open?**  Historical cash is untouched;
   corrections are new items with their own basis.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import attribution as attribution_mod
from . import contracts as contracts_mod
from . import coverage as coverage_mod
from . import farmcredit as farmcredit_mod
from . import money as money_mod
from .balance import Interval, MaterialBalance, build_nodes
from .data import BEIJING, load_all
from .scales import ScaleConflict, ScaleSelection, resolve_scales

EPS = 1e-6


def evaluation_moment(data: dict, as_of: datetime | None) -> datetime:
    """The moment a review is made.

    Without an explicit cut-off we use the newest timestamp in the documents,
    so results are reproducible and never depend on the wall clock.  A future
    ``closed_at`` therefore means "still open".
    """
    if as_of is not None:
        return as_of
    stamps: list[datetime] = []
    stamps += [t.reported_at for t in data["scales"]]
    stamps += [m.occurred_at for m in data["movements"]]
    stamps += [c.noticed_at for c in data["claims"]]
    stamps += [e.authorized_at for e in data["ledger"]]
    stamps += [s.posted_at for s in data["farm_credit"]]
    stamps += [r.known_at for r in data["recoveries"]]
    return max(stamps) if stamps else datetime.now(BEIJING)


def effective_recoveries(recoveries):
    """Collapse repeated reports of one entry to its latest state.

    ``入账`` supersedes an earlier ``待付`` for the same entry; a later report
    of the same entry does not create a second cash movement.
    """
    latest = {}
    for r in recoveries:
        key = (r.entry, r.direction)
        prior = latest.get(key)
        if prior is None or r.known_at >= prior.known_at:
            latest[key] = r
    return sorted(latest.values(), key=lambda r: (r.known_at, r.entry))


def visible(data: dict, moment: datetime) -> dict:
    """Only documents known by ``moment`` participate in a historical review."""
    out = dict(data)
    out["claims"] = [c for c in data["claims"] if c.noticed_at <= moment]
    out["ledger"] = [e for e in data["ledger"] if e.authorized_at <= moment]
    out["farm_credit"] = [s for s in data["farm_credit"] if s.posted_at <= moment]
    out["recoveries"] = [r for r in data["recoveries"] if r.known_at <= moment]
    return out


@dataclass
class Correction:
    ticket: str
    parcel: str
    from_rev: int | None
    to_rev: int
    reported_at: datetime
    from_feasible: bool
    to_feasible: bool
    affected_waybills: list[str] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "ticket": self.ticket,
            "parcel": self.parcel,
            "from_rev": self.from_rev,
            "to_rev": self.to_rev,
            "reported_at": self.reported_at.isoformat(),
            "from_feasible": self.from_feasible,
            "to_feasible": self.to_feasible,
            "affected_waybills": self.affected_waybills,
            "changes": self.changes,
            "note": self.note,
        }


@dataclass
class Conflict:
    label: str
    parcel: str
    basis: str
    affected_parcels: list[str]
    affected_waybills: list[str]
    unaffected_waybills: list[str]
    readings: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "parcel": self.parcel,
            "basis": self.basis,
            "affected_parcels": self.affected_parcels,
            "affected_waybills": self.affected_waybills,
            "unaffected_waybills": self.unaffected_waybills,
            "readings": self.readings,
        }


@dataclass
class Reconciliation:
    moment: datetime
    as_of: datetime | None
    selection: ScaleSelection
    nodes: list
    waybills: list
    farms: list
    parcels: list
    attributions: dict
    joint: dict
    aggregate: dict
    entitlements: dict
    buyers: dict
    orchards: list
    farm_cash: list
    conflicts: list[Conflict]
    corrections: list[Correction]
    credit: dict
    adjustments: money_mod.AdjustmentLedger
    payments: list
    mb: MaterialBalance
    data: dict

    # -- views ---------------------------------------------------------------
    def buyer_table(self) -> list[dict]:
        return [p.as_dict() for p in self.buyers.values()]

    def orchard_table(self) -> list[dict]:
        return [o.as_dict() for o in self.orchards]

    def farm_table(self) -> list[dict]:
        return [f.as_dict() for f in self.farm_cash]

    def close_summary(self) -> dict:
        settled = [wb for wb, a in self.attributions.items() if not a.conflict]
        quarantined = [wb for wb, a in self.attributions.items() if a.conflict]
        ent = sum(self.entitlements[wb].entitlement_cents for wb in settled)
        paid = sum(self.entitlements[wb].paid_cents for wb in settled)
        next_due = sum(self.buyers[wb].executable_next_cents for wb in settled)
        overpaid = sum(-self.buyers[wb].balance_cents for wb in settled
                       if self.buyers[wb].balance_cents < 0)
        confirmed_farm = sum(o.amount_cents for o in self.orchards)
        unallocated = sum(
            self.entitlements[wb].entitlement_cents
            for wb, att in self.attributions.items()
            if att.confirmed_kg() <= EPS and not att.conflict)
        return {
            "identity": {
                "entitlement_cents": ent,
                "paid_cents": paid,
                "next_due_cents": next_due,
                "overpaid_cents": overpaid,
                "paid_plus_next_cents": paid + next_due,
                "check_balances": abs(paid + next_due - overpaid - ent) < 0.5,
                "settled_waybills": settled,
            },
            "quarantined": {
                "waybills": quarantined,
                "frozen_paid_cents": sum(self.buyers[wb].paid_cents
                                         for wb in quarantined),
                "note": "these waybills are outside the closing identity until the "
                        "record conflict is resolved; no new cash is proposed",
            },
            "split_of_entitlement": {
                "confirmed_to_named_orchard_cents": confirmed_farm,
                "not_yet_attributable_cents": unallocated,
                "reconciles": abs(confirmed_farm + unallocated - ent) < 0.5,
            },
            "by_buyer": self._by_buyer(),
            "by_farm": self._by_farm(),
            "suspense": self.suspense(),
            "orchard_cash": {
                "posted_deductions_cents": sum(f.deducted_cents for f in self.farm_cash),
                "posted_returns_cents": sum(f.refund_posted_cents for f in self.farm_cash),
                "pending_returns_cents": sum(f.refund_pending_cents for f in self.farm_cash),
                "refund_due_cents": sum(f.refund_due_cents for f in self.farm_cash),
                "confirmed_but_unsecured_cents": sum(o.open_cents for o in self.orchards),
            },
        }

    def latest_correction_impact(self) -> dict | None:
        """Which pending item and whose money the most recent correction moved.

        Only waybills whose numbers actually changed are listed; a correction
        that touches no shared orchard and no shared credit leaves the other
        waybills out of the review queue.
        """
        if not self.corrections:
            return None
        latest = max(self.corrections, key=lambda c: (c.reported_at, c.ticket, c.to_rev))
        material = [c for c in self.corrections if c.changes]
        last_material = (max(material, key=lambda c: (c.reported_at, c.ticket, c.to_rev))
                         if material else None)
        subject = last_material or latest
        affected = []
        for change in subject.changes:
            wb = change["waybill"]
            buyer = self.buyers.get(wb)
            ent = self.entitlements.get(wb)
            rows = [o for o in self.orchards if o.waybill == wb]
            before_next = change.get("next_cents", [None, None])[0]
            after_next = change.get("next_cents", [None, None])[1]
            quarantined = after_next is None
            affected.append({
                "waybill": wb,
                "party": ent.buyer if ent else "",
                "before_cents": before_next,
                "after_cents": after_next,
                "delta_cents": (None if before_next is None or after_next is None
                                else after_next - before_next),
                "current_executable_cents":
                    buyer.executable_next_cents if buyer else 0,
                "quarantined_by_this_revision": quarantined,
                "effect": ("该版被隔离，不产生新短少；可办金额维持原值"
                           if quarantined else
                           "可办金额按更正中列示的差额变化"),
                "farms_touched": [o.farm for o in rows],
                "shared_credit": bool(rows),
                "pending_item": (f"TOPUP-{wb}"
                                 if buyer and buyer.executable_next_cents > 0
                                 else f"REVIEW-{wb}" if buyer and buyer.held
                                 else "无"),
            })
        return {
            "correction": subject.as_dict(),
            "latest_by_time": latest.as_dict(),
            "latest_with_effect": last_material.as_dict() if last_material else None,
            "affected": affected,
            "untouched_waybills": [wb for wb in self.entitlements
                                   if wb not in {a["waybill"] for a in affected}],
            "note": "只列实际变动的运单；无共同果园、无共用货款的运单不重新质疑",
            "scope_note": ("最近一次更正在时间上最新，但未改变任何金额"
                           if subject is not latest
                           else "最近一次更正即最近一次有实质影响的更正"),
        }

    def schedule(self, ledger: money_mod.AdjustmentLedger | None = None) -> list[dict]:
        """The cash items the next processing run may execute, once each.

        Buyer top-ups and recoveries are computed from the current entitlement
        and the cash already paid, so re-sending a reviewed adjustment cannot
        create a second item: the adjustment only documents *why* the balance
        moved.  ``ledger`` is accepted so a reviewer can hand the same reviewed
        changes in again and see that nothing is duplicated.
        """
        ledger = ledger or self.adjustments
        items: dict[str, dict] = {}
        for payment in self.payments:
            if payment.status not in ("待办", "待复核"):
                continue
            items[f"waybill:{payment.waybill}:{payment.kind}"] = payment.as_dict()
        for adj in ledger.applied:
            key = f"adjustment:{adj.key}"
            if key in items:
                continue
            items.setdefault(key, {
                "entry": f"ADJ-{adj.key}",
                "waybill": adj.waybill,
                "kind": adj.kind,
                "cents": adj.buyer_delta_cents,
                "farm_deltas": adj.farm_deltas,
                "status": "已并入本次余额",
                "description": adj.description,
            })
        for refund in self.credit["refund_items"]:
            if refund["outstanding_return_cents"] <= 0:
                continue
            items[f"refund:{refund['farm']}:{refund['waybill']}"] = {
                "entry": f"RETURN-{refund['waybill']}-{refund['farm']}",
                "waybill": refund["waybill"],
                "party": refund["farm"],
                "kind": "返还",
                "cents": refund["outstanding_return_cents"],
                "farm_deltas": {refund["farm"]: -refund["outstanding_return_cents"]},
                "status": "待办",
                "description": refund["basis"],
            }
        return list(items.values())

    def suspense(self) -> list[dict]:
        """Amounts that cannot yet be paid or deducted, kept off the cash totals.

        Three kinds, never mixed: an unresolved farm attribution still owed to
        a buyer, a waybill held because its record conflicts, and a confirmed
        orchard liability with no weekly credit left to secure it.
        """
        items = []
        for wb, att in self.attributions.items():
            ent = self.entitlements[wb]
            buyer = self.buyers[wb]
            if att.conflict:
                items.append({
                    "waybill": wb, "party": ent.buyer, "kind": "资料冲突待核",
                    "cents": buyer.paid_cents,
                    "basis": "该运单有效磅单与其余记录不相容；已付现金冻结在账上，"
                             "不新增付款也不追回，待复核后另行办理",
                })
                continue
            if att.confirmed_kg() <= EPS and ent.entitlement_cents > 0:
                items.append({
                    "waybill": wb, "party": ent.buyer, "kind": "买方应赔待分责",
                    "cents": ent.entitlement_cents,
                    "basis": "短少总量已确认，但没有任何果园的可确认份额；"
                             "买方应赔照常计算，果园扣款暂挂",
                })
        for o in self.orchards:
            if o.open_cents > 0:
                items.append({
                    "waybill": o.waybill, "party": o.farm,
                    "kind": "果园责任已确认但货款不足",
                    "cents": o.open_cents,
                    "basis": f"可在 {o.statement or '现有周结'} 抵扣后仍有未落地部分，"
                             f"不跨周挪用已关账余额",
                })
        return items

    def _by_buyer(self) -> list[dict]:
        """Customer-level totals that keep paid / still-due / not-yet-split apart."""
        out = {}
        for wb, ent in self.entitlements.items():
            att = self.attributions[wb]
            buyer = self.buyers[wb]
            row = out.setdefault(buyer.buyer, {
                "buyer": buyer.buyer, "waybills": [],
                "entitlement_cents": 0, "paid_cents": 0, "next_due_cents": 0,
                "confirmed_farm_cents": 0, "not_attributable_cents": 0,
                "held_paid_cents": 0})
            row["waybills"].append(wb)
            row["entitlement_cents"] += ent.entitlement_cents
            row["paid_cents"] += ent.paid_cents
            row["next_due_cents"] += buyer.executable_next_cents
            row["confirmed_farm_cents"] += sum(
                o.amount_cents for o in self.orchards if o.waybill == wb)
            if att.confirmed_kg() <= EPS:
                row["not_attributable_cents"] += ent.entitlement_cents
            if buyer.held:
                row["held_paid_cents"] += ent.paid_cents
        return sorted(out.values(), key=lambda r: r["buyer"])

    def _by_farm(self) -> list[dict]:
        """Orchard-level totals: confirmed liability, cash taken, still open."""
        out = {}
        for o in self.orchards:
            row = out.setdefault(o.farm, {
                "farm": o.farm, "waybills": [],
                "amount_cents": 0, "deducted_cents": 0,
                "newly_applied_cents": 0, "open_cents": 0})
            row["waybills"].append({"waybill": o.waybill, "amount_cents": o.amount_cents})
            row["amount_cents"] += o.amount_cents
            row["deducted_cents"] += o.deducted_cents
            row["newly_applied_cents"] += o.newly_applied_cents
            row["open_cents"] += o.open_cents
        for f in self.farm_cash:
            row = out.setdefault(f.farm, {
                "farm": f.farm, "waybills": [], "amount_cents": 0,
                "deducted_cents": 0, "newly_applied_cents": 0, "open_cents": 0})
            # Executed deductions without a live claim (refund items) are still
            # cash the orchard paid.  They are shown here, not hidden in a total.
            row["deducted_cents"] = f.deducted_cents
            row["refund_due_cents"] = f.refund_due_cents
            row["refund_posted_cents"] = f.refund_posted_cents
            row["refund_pending_cents"] = f.refund_pending_cents
            row["refund_outstanding_cents"] = f.refund_outstanding_cents
        return sorted(out.values(), key=lambda r: r["farm"])

    def audit(self, waybill: str) -> dict:
        """What was used for each payment, and what differs now.

        For every buyer payment on this waybill we replay the review at the
        moment the payment was authorised: only documents visible then, and
        the contract version in force at first shipment.  The current view is
        shown next to it with the executable next amount, so a reviewer can see
        both the basis of the original payment and what has changed since.
        """
        if waybill not in self.attributions:
            raise KeyError(waybill)
        att = self.attributions[waybill]
        ent = self.entitlements[waybill]
        buyer = self.buyers[waybill]
        upstream = _ancestor_parcels(att.destination, self.nodes)
        claims_now = [c.as_dict() for c in ent.claims]

        payments_then = []
        buyer_payments = [e for e in self.data["ledger"]
                          if e.waybill == waybill and e.kind == "已付"]
        for payment in sorted(buyer_payments, key=lambda e: e.authorized_at):
            moment = payment.authorized_at
            sel_then = resolve_scales(self.data["scales"], as_of=moment)
            state_then = _state(sel_then, self.data, self.nodes, self.parcels,
                                self.farms, self.waybills, moment)
            docs_then = []
            for parcel in sorted(upstream):
                for versions in sel_then.by_ticket.values():
                    for reading in versions:
                        if reading.parcel == parcel and reading.rev == sel_then.chosen[reading.ticket].rev:
                            docs_then.append(reading.as_dict())
            docs_then = {(d["ticket"], d["rev"]): d for d in docs_then}.values()
            if state_then is None:
                payments_then.append({
                    "entry": payment.entry, "paid_cents": payment.cents,
                    "authorized_at": moment.isoformat(),
                    "documents_used": sorted(docs_then, key=lambda d: d["reported_at"]),
                    "entitlement_then_cents": None,
                    "note": "the record visible then had no feasible material balance",
                })
                continue
            ent_then = state_then.entitlements[waybill]
            payments_then.append({
                "entry": payment.entry,
                "paid_cents": payment.cents,
                "authorized_at": moment.isoformat(),
                "documents_used": sorted(docs_then, key=lambda d: d["reported_at"]),
                "contract": {"contract": ent_then.contract,
                             "version": ent_then.version,
                             "deductible_kg": ent_then.deductible_kg,
                             "cents_per_kg": ent_then.cents_per_kg,
                             "cap_cents": ent_then.cap_cents},
                "claims_visible_then": [c.as_dict() for c in ent_then.claims],
                "confirmed_kg_then": round(ent_then.confirmed_shortage_kg, 4),
                "entitlement_then_cents": ent_then.entitlement_cents,
                "difference_to_now_cents": ent.entitlement_cents - ent_then.entitlement_cents,
            })

        corrections = []
        for c in self.corrections:
            touches = waybill in c.affected_waybills or c.parcel in upstream
            if not touches:
                continue
            change = next((x for x in c.changes if x["waybill"] == waybill), None)
            corrections.append({
                "ticket": c.ticket, "from_rev": c.from_rev, "to_rev": c.to_rev,
                "reported_at": c.reported_at.isoformat(),
                "feasible": [c.from_feasible, c.to_feasible],
                "change": change, "note": c.note,
            })

        return {
            "waybill": waybill,
            "buyer": ent.buyer,
            "contract_now": {
                "contract": ent.contract, "version": ent.version,
                "in_force_at_first_shipment": ent.first_shipment_at.isoformat(),
                "deductible_kg": ent.deductible_kg,
                "cents_per_kg": ent.cents_per_kg, "cap_cents": ent.cap_cents,
            },
            "payments_then": payments_then,
            "documents_now": sorted(
                {(t.ticket, t.rev): t.as_dict()
                 for versions in self.selection.by_ticket.values()
                 for t in versions
                 if t.parcel in upstream
                 and t.rev == self.selection.chosen[t.ticket].rev}.values(),
                key=lambda d: (d["reported_at"], d["ticket"])),
            "now": {
                "shortage_kg": att.shortage.as_dict(),
                "confirmed_farm_kg": round(att.confirmed_kg(), 4),
                "unresolved_kg": round(att.unresolved_kg(), 4),
                "farms": att.as_dict()["farms"],
                "claims": claims_now,
                "entitlement_cents": ent.entitlement_cents,
                "paid_cents": ent.paid_cents,
                "balance_cents": buyer.balance_cents,
                "pending_cents": buyer.pending_cents,
                "next_cents": buyer.executable_next_cents,
                "status": buyer.status,
                "quarantined": att.conflict,
            },
            "orchard": [o.as_dict() for o in self.orchards if o.waybill == waybill],
            "corrections": corrections,
        }

    def balance_detail(self) -> list[dict]:
        """Per operation: explicit loss, transfers and still-unexplained weight.

        Kept separate on purpose.  Normal loss is measured and may be inside
        its range; transfers are physical (余货/拨出/运输); the unexplained
        remainder is the only quantity that can turn into a shortage, and it is
        never attributed to a farm just because a parcel number is traceable.
        """
        rows = []
        for node in self.nodes:
            loss = self.mb.range({("loss", node.operation): 1})
            key = ("unexplained", node.operation)
            unexplained = self.mb.range({key: 1}) if key in self.mb._index else None
            if loss is None:
                loss = Interval(0, 0)
            if unexplained is None:
                unexplained = Interval(0, 0)
            rows.append({
                "operation": node.operation,
                "kind": node.kind,
                "inputs": list(node.inputs),
                "outputs": list(node.outputs),
                "loss_kg": {"declared_min": node.loss_min,
                            "declared_max": node.loss_max,
                            "admitted_low": round(loss.low, 4),
                            "admitted_high": round(loss.high, 4)},
                "unexplained_kg": {"low": round(unexplained.low, 4),
                                   "high": round(unexplained.high, 4)},
            })
        return rows

    def compare(self, other: "Reconciliation") -> list[money_mod.Adjustment]:
        """Reviewed differences between two reviews (this one is the baseline).

        The farm figures are the same confirmed-kilogram splits that the
        orchard lines use, priced at each waybill's contract rate, so a pure
        re-attribution shows as an orchard transfer with no buyer movement.
        """
        def snapshot(bundle):
            out = {}
            for wb, ent in bundle.entitlements.items():
                att = bundle.attributions[wb]
                confirmed = [iv.low for iv in att.farms.values()
                             if iv.low > EPS]
                total = sum(confirmed)
                farms = {}
                if total > EPS and ent.entitlement_cents > 0:
                    for farm, iv in att.farms.items():
                        if iv.low > EPS:
                            farms[farm] = int(round(
                                ent.entitlement_cents * iv.low / total))
                out[wb] = {"entitlement_cents": ent.entitlement_cents,
                           "farms": farms}
            return out
        return money_mod.compare_positions(snapshot(self), snapshot(other))


def _ancestor_parcels(parcel: str, nodes) -> set[str]:
    seen = {parcel}
    frontier = {parcel}
    while frontier:
        nxt = set()
        for node in nodes:
            if frontier.intersection(node.outputs):
                nxt.update(node.inputs)
        nxt -= seen
        seen |= nxt
        frontier = nxt
    return seen


def _transport_operation(waybill: str, destination: str, nodes) -> str:
    """The transport node that delivers a waybill's destination."""

    for node in nodes:
        if node.kind == "transport" and destination in node.outputs:
            return node.operation
    raise KeyError(f"no transport operation for {waybill}")


def _downstream_waybills(parcel: str, nodes, waybills) -> list[str]:
    frontier = {parcel}
    seen = set(frontier)
    while frontier:
        nxt = set()
        for node in nodes:
            if frontier.intersection(node.inputs):
                nxt.update(node.outputs)
        nxt -= seen
        seen |= nxt
        frontier = nxt
    dest = {w.destination: w.waybill for w in waybills}
    return sorted(dest[p] for p in seen if p in dest)


def _build_mb(nodes, parcels, farms, origins, ranges):
    return MaterialBalance(nodes, parcels, farms, origins, ranges)


def _admitted_range(nodes, parcels, farms, origins, ranges, parcel) -> Interval | None:
    """What the other documents admit for ``parcel`` when its own ticket is out."""
    relaxed = dict(ranges)
    relaxed.pop(parcel, None)
    mb = _build_mb(nodes, parcels, farms, origins, relaxed)
    if not mb.feasible():
        return None
    return mb.range({("x", parcel, f): 1 for f in farms})


def _first_shipments(waybills, movements) -> dict[str, datetime]:
    by_output: dict[str, datetime] = {}
    for m in movements:
        if m.kind == "运输" and m.child:
            current = by_output.get(m.child)
            by_output[m.child] = m.occurred_at if current is None else min(current, m.occurred_at)
    return {w.waybill: by_output[w.destination] for w in waybills}


@dataclass
class _State:
    mb: MaterialBalance
    attributions: dict
    entitlements: dict


def _state(selection: ScaleSelection, data: dict, nodes, parcels, farms,
           waybills, moment: datetime) -> _State | None:
    try:
        ranges = selection.parcel_ranges()
    except ScaleConflict:
        return None
    docs = visible(data, moment)
    mb = _build_mb(nodes, parcels, farms, data["origins"], ranges)
    if not mb.feasible():
        return None
    att = attribution_mod.attribute_waybills(mb, waybills, farms, nodes)
    first = _first_shipments(waybills, data["movements"])
    low = {wb: a.shortage.low for wb, a in att.items()}
    ent = contracts_mod.entitlements(
        waybills, low, first, docs["claims"], data["contracts"], docs["ledger"])
    return _State(mb, att, ent)


def _diff(before: _State | None, after: _State | None) -> list[dict]:
    if before is None or after is None:
        return []
    changes = []
    for wb in sorted(before.entitlements):
        b, a = before.entitlements[wb], after.entitlements[wb]
        ba, aa = before.attributions[wb], after.attributions[wb]
        rows = {
            "shortage_low_kg": [round(ba.shortage.low, 4), round(aa.shortage.low, 4)],
            "shortage_high_kg": [round(ba.shortage.high, 4), round(aa.shortage.high, 4)],
            "confirmed_farm_kg": [round(ba.confirmed_kg(), 4), round(aa.confirmed_kg(), 4)],
            "entitlement_cents": [b.entitlement_cents, a.entitlement_cents],
            "next_cents": [b.next_cents, a.next_cents],
        }
        if any(v[0] != v[1] for v in rows.values()):
            changes.append({"waybill": wb, **rows})
    return changes


def _corrections(data, nodes, parcels, farms, waybills) -> list[Correction]:
    groups: dict[str, list] = {}
    for t in data["scales"]:
        groups.setdefault(t.ticket, []).append(t)
    out: list[Correction] = []
    for ticket, versions in groups.items():
        versions = sorted(versions, key=lambda t: t.rev)
        if len(versions) < 2:
            continue
        for previous, current in zip(versions, versions[1:]):
            moment = current.reported_at
            before_sel = resolve_scales(data["scales"], overrides={ticket: previous.rev})
            after_sel = resolve_scales(data["scales"], overrides={ticket: current.rev})
            before = _state(before_sel, data, nodes, parcels, farms, waybills, moment)
            after = _state(after_sel, data, nodes, parcels, farms, waybills, moment)
            affected: list[str] = []
            note = ""
            changes = _diff(before, after)
            if before is not None and after is None:
                note = "新版与其余记录不相容，已隔离；不按新增短少处理"
                admitted = _admitted_range(nodes, parcels, farms, data["origins"],
                                           before_sel.parcel_ranges(), current.parcel)
                if admitted is not None:
                    note += (f"（其余记录只容许 {current.parcel} "
                             f"{admitted.low:.2f}–{admitted.high:.2f} kg，"
                             f"新版为 {current.low:.2f}–{current.high:.2f} kg）")
                affected = _downstream_waybills(current.parcel, nodes, waybills)
                for wb in affected:
                    changes.append({
                        "waybill": wb,
                        "shortage_low_kg": [round(before.attributions[wb].shortage.low, 4), None],
                        "shortage_high_kg": [round(before.attributions[wb].shortage.high, 4), None],
                        "confirmed_farm_kg": [round(before.attributions[wb].confirmed_kg(), 4), None],
                        "entitlement_cents": [before.entitlements[wb].entitlement_cents, None],
                        "next_cents": [before.entitlements[wb].next_cents, None],
                    })
            elif before is None and after is not None:
                note = "新版本使记录重新自洽"
            out.append(Correction(
                ticket=ticket, parcel=current.parcel, from_rev=previous.rev,
                to_rev=current.rev, reported_at=moment,
                from_feasible=before is not None, to_feasible=after is not None,
                affected_waybills=affected, changes=changes, note=note))
    return sorted(out, key=lambda c: (c.reported_at, c.ticket, c.to_rev))


def _live_conflicts(selection, data, nodes, parcels, farms, waybills) -> tuple[list[Conflict], set[str]]:
    def conflict_for(parcel: str, label: str, why: str) -> Conflict:
        affected = _downstream_waybills(parcel, nodes, waybills)
        readings = [t.as_dict() for ticket in selection.by_ticket.values()
                    for t in ticket if t.parcel == parcel]
        return Conflict(label=label, parcel=parcel,
                        basis=why, affected_parcels=[parcel],
                        affected_waybills=affected,
                        unaffected_waybills=[w.waybill for w in waybills
                                             if w.waybill not in affected],
                        readings=readings)

    try:
        ranges = selection.parcel_ranges()
    except ScaleConflict as exc:
        c = conflict_for(
            exc.parcel, "live readings",
            f"{exc}; 同一节点的有效读数互不重叠，不是新增短少")
        return [c], {exc.parcel}
    mb = _build_mb(nodes, parcels, farms, data["origins"], ranges)
    if mb.feasible():
        return [], set()
    occupied = [p for p in parcels if p in ranges]
    culprits = []
    for parcel in occupied:
        if _admitted_range(nodes, parcels, farms, data["origins"], ranges, parcel) is not None:
            culprits.append(parcel)
    conflicts = []
    for parcel in culprits:
        admitted = _admitted_range(nodes, parcels, farms, data["origins"], ranges, parcel)
        why = (f"全部有效磅单放在一起没有可行解；摘掉 {parcel} 的本票后其余记录只容许 "
               f"{admitted.low:.2f}–{admitted.high:.2f} kg，现票为 "
               f"{ranges[parcel][0]:.2f}–{ranges[parcel][1]:.2f} kg。"
               f"这是记录冲突，不是新增短少")
        conflicts.append(conflict_for(parcel, "live readings", why))
    affected = {wb for c in conflicts for wb in c.affected_waybills}
    for c in conflicts:
        c.unaffected_waybills = [w.waybill for w in waybills if w.waybill not in affected]
    return conflicts, set(culprits)


def _revision_conflicts(selection, tickets, nodes, parcels, farms, origins,
                        waybills) -> list[Conflict]:
    """Superseded versions that cannot coexist with the rest of the record.

    Only versions that are *not* the live choice are reported here; a live
    incompatibility is covered by ``_live_conflicts``.  This keeps the two
    lists from double-counting the same bad document.
    """
    groups: dict[str, list] = {}
    for t in tickets:
        groups.setdefault(t.ticket, []).append(t)
    out: list[Conflict] = []
    for ticket, versions in groups.items():
        live_rev = selection.chosen[ticket].rev if ticket in selection.chosen else None
        for version in versions:
            if version.rev == live_rev:
                continue
            sel = resolve_scales(tickets, overrides={ticket: version.rev})
            try:
                ranges = sel.parcel_ranges()
            except ScaleConflict:
                continue
            mb = _build_mb(nodes, parcels, farms, origins, ranges)
            if mb.feasible():
                continue
            admitted = _admitted_range(nodes, parcels, farms, origins,
                                       ranges, version.parcel)
            if admitted is None:
                continue
            affected = _downstream_waybills(version.parcel, nodes, waybills)
            out.append(Conflict(
                label=f"{ticket}:{version.rev}", parcel=version.parcel,
                basis=(f"{ticket}:{version.rev} 取代 {version.replaces}；与其余记录放在一起无解。"
                       f"其余记录对该节点只容许 {admitted.low:.2f}–{admitted.high:.2f} kg，"
                       f"本版为 {version.low:.2f}–{version.high:.2f} kg。"
                       f"该版被隔离，不计为新增短少"),
                affected_parcels=[version.parcel],
                affected_waybills=affected,
                unaffected_waybills=[w.waybill for w in waybills if w.waybill not in affected],
                readings=[version.as_dict()]))
    return out


def _farm_share_cents(total_cents: int, shares: dict[str, float],
                      confirmed_total: float | None = None) -> dict[str, int]:
    """Split an entitlement over confirmed farm kilograms.

    Only confirmed (lower-bound) kilograms create an orchard liability.  The
    split is pro-rata over the *whole* confirmed shortage, so if only part of
    the shortage is pinned to orchards, only that part becomes an orchard
    liability and the rest stays unattributed.  Largest-remainder rounding
    keeps the cents exact and deterministic.
    """
    total = sum(shares.values())
    if total <= EPS or total_cents <= 0:
        return {f: 0 for f in shares}
    base = total if not confirmed_total else max(total, confirmed_total)
    allocatable = int(round(total_cents * total / base)) if base else 0
    raw = {f: allocatable * kg / total for f, kg in shares.items()}
    floored = {f: int(raw[f]) for f in raw}
    remainder = allocatable - sum(floored.values())
    for f in sorted(raw, key=lambda f: (raw[f] - floored[f], f), reverse=True)[:remainder]:
        floored[f] += 1
    return floored


def _confirmation_time(ent, moment: datetime) -> str:
    """When the liability became confirmable: the latest notice depended on."""
    times = [c.noticed_at for c in ent.claims]
    return max(times).isoformat() if times else moment.isoformat()


def _orchard_claims(attributions, entitlements, farms, moment):
    claims = []
    for wb, att in attributions.items():
        if att.conflict:
            continue
        shares = {f: att.farms[f].low for f in farms if f in att.farms and att.farms[f].low > EPS}
        amounts = _farm_share_cents(entitlements[wb].entitlement_cents, shares,
                                    confirmed_total=att.shortage.low)
        for farm, amount in amounts.items():
            if amount <= 0:
                continue
            claims.append(farmcredit_mod.OpenClaim(
                key=f"{wb}:{farm}", waybill=wb, farm=farm, amount_cents=amount,
                confirmed_at=_confirmation_time(entitlements[wb], moment)))
    return claims


def _executed_deductions(recoveries, statement_farm) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = {}
    for r in recoveries:
        if r.direction != "扣抵" or r.state != "入账" or not r.waybill:
            continue
        key = (r.waybill, statement_farm.get(r.statement, ""))
        out[key] = out.get(key, 0) + r.cents
    return out


def _returns_by_farm(recoveries, statement_farm) -> tuple[dict[str, int], dict[str, int]]:
    posted, pending = {}, {}
    for r in recoveries:
        if r.direction != "返还":
            continue
        farm = statement_farm.get(r.statement, "")
        if r.state == "入账":
            posted[farm] = posted.get(farm, 0) + r.cents
        elif r.state == "待付":
            pending[farm] = pending.get(farm, 0) + r.cents
    return posted, pending


def reconcile(as_of: datetime | None = None,
              overrides: dict[str, int] | None = None,
              data: dict | None = None) -> Reconciliation:
    data = data or load_all()
    moment = evaluation_moment(data, as_of)
    nodes = build_nodes(data["movements"])
    parcels = sorted({p for n in nodes for p in n.inputs + n.outputs})
    farms = sorted(set(data["origins"].values()))
    waybills = data["waybills"]
    docs = visible(data, moment)

    selection = resolve_scales(docs["scales"], as_of=moment, overrides=overrides)
    conflicts, affected_parcels = _live_conflicts(
        selection, data, nodes, parcels, farms, waybills)
    affected = {wb for c in conflicts for wb in c.affected_waybills}

    # Quarantined parcels are held out entirely; the rest of the record keeps
    # settling, and the affected waybills are reported as unresolved.
    try:
        live_ranges = selection.parcel_ranges()
    except ScaleConflict as exc:
        # Already reported as a conflict above; the overlapping node is held
        # out and everything else keeps settling.
        live_ranges = {p: (max(t.low for t in ts), min(t.high for t in ts))
                       for p, ts in selection.by_parcel().items()
                       if p != exc.parcel}
        affected_parcels = set(affected_parcels) | {exc.parcel}
    settle_ranges = {p: v for p, v in live_ranges.items() if p not in affected_parcels}
    mb = _build_mb(nodes, parcels, farms, data["origins"], settle_ranges)
    if not mb.feasible():
        # The culprit search found single parcels that each suffice; the union
        # can still be inconsistent.  Relax one more at a time until the rest
        # of the record settles, so no unrelated waybill is held back.
        held = set(affected_parcels)
        for parcel in [p for p in parcels if p in settle_ranges]:
            if mb.feasible():
                break
            held.add(parcel)
            settle_ranges.pop(parcel)
            mb = _build_mb(nodes, parcels, farms, data["origins"], settle_ranges)
        if not mb.feasible():
            settle_ranges = {}
            mb = _build_mb(nodes, parcels, farms, data["origins"], settle_ranges)
        affected = {wb for c in conflicts for wb in c.affected_waybills}
        for parcel in held - affected_parcels:
            affected |= set(_downstream_waybills(parcel, nodes, waybills))
    attributions = attribution_mod.attribute_waybills(mb, waybills, farms, nodes)
    for wb in affected:
        att = attributions[wb]
        att.conflict = True
        att.shortage = Interval(0.0, 0.0)
        att.farms = {}
        att.confirmed_farms = []
        att.unresolved_farms = list(farms)

    joint = attribution_mod.joint_maxima(mb, attributions, farms)
    aggregate = attribution_mod.aggregate(mb, attributions, farms)

    first = _first_shipments(waybills, data["movements"])
    low = {wb: a.shortage.low for wb, a in attributions.items()}
    ents = contracts_mod.entitlements(
        waybills, low, first, docs["claims"], data["contracts"], docs["ledger"])

    # ``待付`` and ``入账`` rows for one handling order are the same item in two
    # states; only the latest state counts, so a posted return does not also
    # leave a phantom pending return behind.
    recoveries = effective_recoveries(docs["recoveries"])
    statements = farmcredit_mod.build_statements(docs["farm_credit"], recoveries)
    by_statement = {s.statement: s for s in docs["farm_credit"]}
    for st in statements:
        raw = by_statement.get(st.statement)
        st.closed = bool(raw and raw.closed_at is not None and raw.closed_at <= moment)
        st.returns_pending = sum(
            r.cents for r in recoveries
            if r.statement == st.statement and r.direction == "返还" and r.state == "待付")
    # Closed weeks never take new deductions; their posted returns are still
    # real credit for the week the handling order names.
    posted_returns, pending_returns = _returns_by_farm(
        recoveries, {s.statement: s.farm for s in statements})

    claims = _orchard_claims(attributions, ents, farms, moment)
    executed = _executed_deductions(recoveries,
                                    {s.statement: s.farm for s in statements})
    excess_executed = {}
    for claim in claims:
        done = executed.get((claim.waybill, claim.farm), 0)
        claim.already_deducted = min(done, claim.amount_cents)
        if done > claim.amount_cents:
            excess_executed[(claim.waybill, claim.farm)] = done - claim.amount_cents
    # Executed deductions that have no confirmed liability at all, or that
    # exceed the now-confirmed amount, are refunded as separate items.  They
    # are never netted against another claim and never rewrite a closed week.
    orphan_executed = [{"waybill": wb, "farm": farm, "cents": cents}
                       for (wb, farm), cents in executed.items()
                       if not any(c.waybill == wb and c.farm == farm for c in claims)]
    for (wb, farm), cents in excess_executed.items():
        orphan_executed.append({"waybill": wb, "farm": farm, "cents": cents})

    farmcredit_mod.apply_credit(claims, statements)

    orchards = []
    for claim in sorted(claims, key=lambda c: (c.farm, c.waybill)):
        att = attributions[claim.waybill]
        orchards.append(money_mod.OrchardPosition(
            waybill=claim.waybill, farm=claim.farm,
            confirmed_kg=att.farms[claim.farm].low,
            unresolved_kg=att.unresolved_kg(),
            amount_cents=claim.amount_cents,
            deducted_cents=claim.already_deducted,
            newly_applied_cents=claim.applied,
            open_cents=claim.remaining,
            week=claim.week, statement=claim.statement))

    farm_cash = []
    for farm in farms:
        rows = [o for o in orchards if o.farm == farm]
        orphans = [x for x in orphan_executed if x["farm"] == farm]
        farm_cash.append(money_mod.FarmCash(
            farm=farm,
            amount_cents=sum(o.amount_cents for o in rows),
            deducted_cents=sum(o.deducted_cents for o in rows)
            + sum(x["cents"] for x in orphans),
            newly_applied_cents=sum(o.newly_applied_cents for o in rows),
            open_cents=sum(o.open_cents for o in rows),
            refund_due_cents=sum(x["cents"] for x in orphans),
            refund_posted_cents=posted_returns.get(farm, 0),
            refund_pending_cents=pending_returns.get(farm, 0)))

    # ---- buyer cash ------------------------------------------------------
    payments = []
    for entry in docs["ledger"]:
        payments.append(money_mod.Payment(
            entry=entry.entry, waybill=entry.waybill, party=entry.party,
            kind=entry.kind, cents=entry.cents,
            authorized_at=entry.authorized_at, origin_refs=entry.origin_refs,
            status="已入账"))
    for wb, ent in ents.items():
        if wb in affected:
            continue
        if ent.next_cents > 0:
            payments.append(money_mod.Payment(
                entry=f"TOPUP-{wb}", waybill=wb, party=ent.buyer, kind="补付",
                cents=ent.next_cents, authorized_at=moment,
                origin_refs=(ent.contract, f"claims:{','.join(c.notice for c in ent.claims)}"),
                status="待办"))
        elif ent.excess_paid_cents > 0:
            payments.append(money_mod.Payment(
                entry=f"REVIEW-{wb}", waybill=wb, party=ent.buyer, kind="追回",
                cents=ent.excess_paid_cents, authorized_at=moment,
                origin_refs=(ent.contract,), status="待复核"))
    buyers = money_mod.buyer_positions(ents, payments, attributions)
    for wb in affected:
        buyers[wb].pending_cents = 0
        buyers[wb].held = True
        buyers[wb].status = "资料冲突，暂缓结算"

    # ---- weekly space for unattributed customer money --------------------
    # Only statements that are still open can secure anything.  The question
    # "how much could even the best attribution cover?" is solved as one
    # programme over the material balance and the shared weekly credit, so
    # per-claim maxima are never added together.
    open_room: dict[str, int] = {}
    for st in statements:
        if not st.closed:
            open_room[st.farm] = open_room.get(st.farm, 0) + st.available_for_new
    pending_rows = []
    pending_lp = []
    for wb, att in attributions.items():
        if att.conflict or att.confirmed_kg() > EPS:
            continue
        ent = ents[wb]
        if ent.entitlement_cents <= 0:
            continue
        op = _transport_operation(wb, att.destination, nodes)
        pending_rows.append({
            "waybill": wb, "buyer": att.buyer,
            "entitlement_cents": ent.entitlement_cents,
            "next_cents": ent.next_cents,
            "shortage_kg": att.shortage.as_dict(),
            "farm_max_kg": {f: round(iv.high, 4) for f, iv in att.farms.items()
                            if iv.high > EPS},
        })
        pending_lp.append({
            "waybill": wb, "operation": op,
            "entitlement_cents": ent.entitlement_cents,
            "confirmed_kg": att.shortage.low,
        })
    cover = coverage_mod.max_cover(mb, pending_lp, open_room)

    credit = {
        "statements": [s.as_dict() for s in statements],
        "orchard_claims": [{
            "waybill": c.waybill, "farm": c.farm, "amount_cents": c.amount_cents,
            "executed_cents": c.already_deducted,
            "applied_cents": c.applied, "open_cents": c.remaining,
            "confirmed_at": c.confirmed_at, "statement": c.statement, "week": c.week,
        } for c in sorted(claims, key=lambda c: (c.farm, c.confirmed_at, c.waybill))],
        "unallocated_buyer": {
            "claims": pending_rows,
            "open_credit_by_farm_cents": open_room,
            "open_credit_total_cents": sum(open_room.values()),
            "claim_gross_total_cents": sum(p["entitlement_cents"] for p in pending_rows),
            "claim_next_total_cents": sum(p["next_cents"] for p in pending_rows),
            **cover,
        },
        "refund_items": [
            {"waybill": x["waybill"], "farm": x["farm"],
             "unsupported_deduction_cents": x["cents"],
             "posted_return_cents": min(posted_returns.get(x["farm"], 0), x["cents"]),
             "outstanding_return_cents": max(
                 0, x["cents"] - min(posted_returns.get(x["farm"], 0), x["cents"])),
             "basis": ("该运单没有可确认的果园归属，已执行的扣款按单独返还款项办理，"
                       "不改写原关账周")}
            for x in orphan_executed],
    }

    corrections = _corrections(data, nodes, parcels, farms, waybills)
    conflicts = conflicts + _revision_conflicts(
        selection, docs["scales"], nodes, parcels, farms, data["origins"], waybills)

    return Reconciliation(
        moment=moment, as_of=as_of, selection=selection, nodes=nodes,
        waybills=waybills, farms=farms, parcels=parcels,
        attributions=attributions, joint=joint, aggregate=aggregate,
        entitlements=ents, buyers=buyers, orchards=orchards, farm_cash=farm_cash,
        conflicts=conflicts, corrections=corrections, credit=credit,
        adjustments=money_mod.AdjustmentLedger(), payments=payments, mb=mb,
        data=data)
