r'''Separation of buyer settlement from orchard deduction.

Two questions are deliberately kept apart:

* **Buyer balance** - what the buyer contract still owes for a *confirmed
  receiving shortfall*.  It does not depend on which orchard caused it.
* **Orchard recovery** - what can be withheld from an orchard's weekly goods
  payment.  It exists only where the responsible quantity is pinned to a farm.

An adjustment can move one without the other:

* ``farm_transfer`` - the same shortage is reassigned between orchards; the
  buyer balance is untouched.
* ``buyer_reprice`` - confirmed quantity changes, so the buyer balance moves;
  the orchard lines are recomputed but are not automatically paid.

Historical ledger rows are executed facts.  A revised weighing never rewrites
them; a new ``补付`` (top-up) or ``追回`` (recovery) is proposed instead.  A
proposal stays ``待办`` until cash actually moves, so it never silently changes
the amount already paid.
'''
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Payment:
    """One executed or proposed movement of money."""

    entry: str
    waybill: str
    party: str
    kind: str                 # 已付 / 补付 / 追回 / 已扣 / 返还
    cents: int
    authorized_at: datetime
    origin_refs: tuple[str, ...] = ()
    status: str = "已入账"     # 已入账 / 待办 / 待付 / 争议
    source_entry: str | None = None

    def as_dict(self) -> dict:
        return {
            "entry": self.entry,
            "waybill": self.waybill,
            "party": self.party,
            "kind": self.kind,
            "cents": self.cents,
            "authorized_at": self.authorized_at.isoformat(),
            "origin_refs": list(self.origin_refs),
            "status": self.status,
            "source_entry": self.source_entry,
        }


@dataclass
class BuyerPosition:
    waybill: str
    buyer: str
    contract: str
    version: str
    entitlement_cents: int
    paid_cents: int
    confirmed_kg: float
    unresolved_kg: float
    pending_cents: int = 0       # proposed, not executed
    status: str = "可结算"
    held: bool = False           # quarantined: no entitlement is asserted
    adjustments: list[Payment] = field(default_factory=list)

    @property
    def balance_cents(self) -> int:
        """Positive: still owed to the buyer.  Negative: overpaid, for review.

        A held waybill has no asserted entitlement, so its balance is not a
        claim in either direction; the cash already paid is shown separately.
        """
        if self.held:
            return 0
        return self.entitlement_cents - self.paid_cents

    @property
    def executable_next_cents(self) -> int:
        """What the next payment run may actually pay."""
        return 0 if self.held else max(0, self.balance_cents)

    @property
    def frozen_paid_cents(self) -> int:
        return self.paid_cents if self.held else 0

    def as_dict(self) -> dict:
        return {
            "waybill": self.waybill,
            "buyer": self.buyer,
            "contract": self.contract,
            "version": self.version,
            "entitlement_cents": self.entitlement_cents,
            "paid_cents": self.paid_cents,
            "pending_cents": self.pending_cents,
            "balance_cents": self.balance_cents,
            "executable_next_cents": self.executable_next_cents,
            "confirmed_kg": round(self.confirmed_kg, 4),
            "unresolved_kg": round(self.unresolved_kg, 4),
            "status": self.status,
            "held": self.held,
            "frozen_paid_cents": self.frozen_paid_cents,
            "adjustments": [a.as_dict() for a in self.adjustments],
        }


@dataclass
class OrchardPosition:
    """One waybill's confirmed liability against one orchard.

    ``amount_cents`` is the confirmed share of the buyer entitlement that may
    be withheld from this orchard.  ``deducted_cents`` is what earlier weekly
    statements actually took; ``newly_applied_cents`` is what this review has
    room to take from still-open credit; ``open_cents`` is what neither covers.
    Returns live on the weekly statement, not on each waybill, so a return is
    never duplicated across the claims that share the farm's credit.
    """

    waybill: str
    farm: str
    confirmed_kg: float
    unresolved_kg: float
    amount_cents: int = 0
    deducted_cents: int = 0
    newly_applied_cents: int = 0
    open_cents: int = 0
    week: str = ""
    statement: str = ""

    @property
    def secured_cents(self) -> int:
        return self.deducted_cents + self.newly_applied_cents

    def as_dict(self) -> dict:
        return {
            "waybill": self.waybill,
            "farm": self.farm,
            "confirmed_kg": round(self.confirmed_kg, 4),
            "unresolved_kg": round(self.unresolved_kg, 4),
            "amount_cents": self.amount_cents,
            "deducted_cents": self.deducted_cents,
            "newly_applied_cents": self.newly_applied_cents,
            "secured_cents": self.secured_cents,
            "open_cents": self.open_cents,
            "week": self.week,
            "statement": self.statement,
        }


@dataclass
class FarmCash:
    """Farm-level cash view.

    ``refund_due_cents`` covers deductions that lost their basis; a return is
    always its own item, so the original deduction and the closed week remain
    unchanged on the books.  Only a posted return reduces what is still owed
    back, and only a posted return frees credit for a later claim.
    """

    farm: str
    amount_cents: int = 0
    deducted_cents: int = 0
    newly_applied_cents: int = 0
    open_cents: int = 0
    refund_due_cents: int = 0
    refund_posted_cents: int = 0
    refund_pending_cents: int = 0

    @property
    def refund_outstanding_cents(self) -> int:
        return max(0, self.refund_due_cents - self.refund_posted_cents)

    @property
    def net_cash_cents(self) -> int:
        """Cash retained after posted returns; pending returns are not cash."""
        return self.deducted_cents + self.newly_applied_cents - self.refund_posted_cents

    def as_dict(self) -> dict:
        return {
            "farm": self.farm,
            "amount_cents": self.amount_cents,
            "deducted_cents": self.deducted_cents,
            "newly_applied_cents": self.newly_applied_cents,
            "open_cents": self.open_cents,
            "refund_due_cents": self.refund_due_cents,
            "refund_posted_cents": self.refund_posted_cents,
            "refund_pending_cents": self.refund_pending_cents,
            "refund_outstanding_cents": self.refund_outstanding_cents,
            "net_cash_cents": self.net_cash_cents,
        }


def buyer_positions(entitlements: dict, payments: list[Payment],
                    attributions: dict) -> dict[str, BuyerPosition]:
    """Build per-waybill cash positions; never include orchard amounts here."""
    result = {}
    for waybill, ent in entitlements.items():
        att = attributions.get(waybill)
        confirmed = att.confirmed_kg() if att else ent.confirmed_shortage_kg
        unresolved = att.unresolved_kg() if att else 0.0
        pending = sum(p.cents for p in payments
                      if p.waybill == waybill and p.party == ent.buyer
                      and p.kind in ("补付", "追回") and p.status == "待办")
        result[waybill] = BuyerPosition(
            waybill=waybill, buyer=ent.buyer, contract=ent.contract,
            version=ent.version, entitlement_cents=ent.entitlement_cents,
            paid_cents=ent.paid_cents, confirmed_kg=confirmed,
            unresolved_kg=unresolved, pending_cents=pending,
            adjustments=[p for p in payments if p.waybill == waybill
                         and p.party == ent.buyer and p.kind in ("补付", "追回")],
        )
    return result


@dataclass
class Adjustment:
    """One reviewed change, with an idempotency key.

    ``key`` is the review voucher, not the payload.  Re-sending the same
    reviewed adjustment must not create a second cash item.
    """

    key: str
    kind: str                     # farm_transfer / buyer_reprice / return
    waybill: str
    affects_buyer: bool
    affects_orchard: bool
    buyer_delta_cents: int
    farm_deltas: dict[str, int]
    description: str
    applied: bool = True

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "kind": self.kind,
            "waybill": self.waybill,
            "affects_buyer": self.affects_buyer,
            "affects_orchard": self.affects_orchard,
            "buyer_delta_cents": self.buyer_delta_cents,
            "farm_deltas": {k: v for k, v in self.farm_deltas.items() if v},
            "description": self.description,
            "applied": self.applied,
        }


class AdjustmentLedger:
    """Apply each reviewed adjustment once, keyed by its review voucher."""

    def __init__(self) -> None:
        self._seen: dict[str, Adjustment] = {}
        self._duplicates: list[str] = []
        self._history: list[Adjustment] = []

    def apply(self, adjustment: Adjustment) -> bool:
        if adjustment.key in self._seen:
            self._duplicates.append(adjustment.key)
            return False
        self._seen[adjustment.key] = adjustment
        self._history.append(adjustment)
        return True

    @property
    def applied(self) -> list[Adjustment]:
        return list(self._history)

    @property
    def duplicates(self) -> list[str]:
        return list(self._duplicates)

    def buyer_delta(self, waybill: str | None = None) -> int:
        return sum(a.buyer_delta_cents for a in self._history
                   if a.affects_buyer and (waybill is None or a.waybill == waybill))

    def farm_delta(self, farm: str, waybill: str | None = None) -> int:
        return sum(a.farm_deltas.get(farm, 0) for a in self._history
                   if a.affects_orchard and (waybill is None or a.waybill == waybill))


def compare_positions(before: dict, after: dict) -> list[Adjustment]:
    """Turn two reviews into reviewed adjustments, one per changed waybill.

    The split between buyer and orchard impact is explicit:

    * if the entitlement (confirmed weight x rate, after deductible/cap)
      changed, the buyer balance moved -> ``buyer_reprice``;
    * if only the farm split changed, orchard lines moved -> ``farm_transfer``;
    * both can be true at once, in which case both flags are set and the two
      deltas are reported separately.

    The key is derived from the waybill and the *reviewed* change, so feeding
    the same comparison twice cannot produce a second cash item.
    """
    adjustments: list[Adjustment] = []
    for wb in sorted(set(before) & set(after)):
        b, a = before[wb], after[wb]
        buyer_delta = a["entitlement_cents"] - b["entitlement_cents"]
        farm_deltas = {}
        for farm in set(b.get("farms", {})) | set(a.get("farms", {})):
            farm_deltas[farm] = (a.get("farms", {}).get(farm, 0)
                                 - b.get("farms", {}).get(farm, 0))
        farm_deltas = {f: v for f, v in farm_deltas.items() if v}
        affects_buyer = buyer_delta != 0
        affects_orchard = bool(farm_deltas)
        if not affects_buyer and not affects_orchard:
            continue
        kind = ("buyer_reprice" if affects_buyer and not affects_orchard
                else "farm_transfer" if affects_orchard and not affects_buyer
                else "both")
        key = f"{wb}:{kind}:{buyer_delta}:{','.join(f'{f}={v}' for f, v in sorted(farm_deltas.items()))}"
        adjustments.append(Adjustment(
            key=key, kind=kind, waybill=wb,
            affects_buyer=affects_buyer, affects_orchard=affects_orchard,
            buyer_delta_cents=buyer_delta, farm_deltas=farm_deltas,
            description=(f"{wb}: 买方余额 {buyer_delta:+d} 分；"
                         + ("果园间转移 " + ", ".join(f"{f} {v:+d}" for f, v in farm_deltas.items())
                            if farm_deltas else "果园扣款口径不变"))))
    return adjustments


def return_adjustment(farm: str, waybill: str, cents: int, voucher: str,
                      posted: bool) -> Adjustment:
    """A return is its own item; it never edits the original deduction."""
    return Adjustment(
        key=voucher, kind="return", waybill=waybill,
        affects_buyer=False, affects_orchard=True, buyer_delta_cents=0,
        farm_deltas={farm: -cents},
        description=(f"{farm} 返还 {cents} 分（{'已入账' if posted else '待付'}）；"
                     f"原扣款与关账周不改写，返还到账后才释放可用额度"),
        applied=posted,
    )
