"""Buyer claim entitlement per waybill.

The contract version is the one in force at the waybill's first outbound
shipment; later top-ups follow that version, not whatever contract is newest.
Deductible and cap are per waybill and cumulative: two notices about the same
waybill share one deductible and one cap.  A customer notice records a demand,
not proof -- the payable weight is the *confirmed* shortage (the lower bound
that survives every weighing tolerance), and never more than what was claimed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .data import Claim, ContractVersion, LedgerEntry, Waybill


def contract_for(waybill: Waybill, first_shipment: datetime,
                 contracts: list[ContractVersion]) -> ContractVersion:
    candidates = [c for c in contracts if c.contract == waybill.contract
                  and c.active_from <= first_shipment]
    if not candidates:
        raise ValueError(f"no contract version for {waybill.waybill} at {first_shipment}")
    return max(candidates, key=lambda c: c.active_from)


def cents(kg: float, rate_cents: int) -> int:
    """Money in cents, rounded to the nearest cent (banker's rounding)."""
    return int(round(kg * rate_cents))


@dataclass
class ClaimLine:
    notice: str
    noticed_at: datetime
    amount_kg: float

    def as_dict(self) -> dict:
        return {"notice": self.notice, "noticed_at": self.noticed_at.isoformat(),
                "amount_kg": self.amount_kg}


@dataclass
class Entitlement:
    waybill: str
    buyer: str
    contract: str
    version: str
    deductible_kg: float
    cents_per_kg: int
    cap_cents: int
    confirmed_shortage_kg: float
    claimed_kg: float
    eligible_kg: float
    payable_kg: float
    entitlement_cents: int
    paid_cents: int
    first_shipment_at: datetime
    claims: list[ClaimLine] = field(default_factory=list)
    settle: bool = True

    @property
    def next_cents(self) -> int:
        """Cash still owed after payments already made."""
        return max(0, self.entitlement_cents - self.paid_cents)

    @property
    def excess_paid_cents(self) -> int:
        """Paid above the current entitlement; held for review, not clawed back."""
        return max(0, self.paid_cents - self.entitlement_cents)

    def as_dict(self) -> dict:
        return {
            "waybill": self.waybill,
            "buyer": self.buyer,
            "contract": self.contract,
            "version": self.version,
            "first_shipment_at": self.first_shipment_at.isoformat(),
            "deductible_kg": self.deductible_kg,
            "cents_per_kg": self.cents_per_kg,
            "cap_cents": self.cap_cents,
            "confirmed_shortage_kg": round(self.confirmed_shortage_kg, 4),
            "claimed_kg": round(self.claimed_kg, 4),
            "eligible_kg": round(self.eligible_kg, 4),
            "payable_kg": round(self.payable_kg, 4),
            "entitlement_cents": self.entitlement_cents,
            "paid_cents": self.paid_cents,
            "next_cents": self.next_cents,
            "excess_paid_cents": self.excess_paid_cents,
            "claims": [c.as_dict() for c in self.claims],
            "settle": self.settle,
        }


def entitlements(waybills: list[Waybill],
                 shortage_low: dict[str, float],
                 first_shipment: dict[str, datetime],
                 claims: list[Claim],
                 contracts: list[ContractVersion],
                 ledger: list[LedgerEntry],
                 include_waybills: set[str] | None = None) -> dict[str, Entitlement]:
    claims_by_waybill: dict[str, list[Claim]] = {}
    for claim in claims:
        claims_by_waybill.setdefault(claim.waybill, []).append(claim)
    paid_by_waybill: dict[str, int] = {}
    for entry in ledger:
        if entry.kind == "已付":
            paid_by_waybill[entry.waybill] = paid_by_waybill.get(entry.waybill, 0) + entry.cents

    result: dict[str, Entitlement] = {}
    for wb in waybills:
        if include_waybills is not None and wb.waybill not in include_waybills:
            continue
        version = contract_for(wb, first_shipment[wb.waybill], contracts)
        lines = sorted(claims_by_waybill.get(wb.waybill, []), key=lambda c: c.noticed_at)
        claimed = sum(c.amount_kg for c in lines)
        confirmed = shortage_low.get(wb.waybill, 0.0)
        eligible = min(confirmed, claimed) if lines else 0.0
        payable = max(0.0, eligible - version.deductible_kg)
        amount = min(version.cap_cents, cents(payable, version.cents_per_kg))
        result[wb.waybill] = Entitlement(
            waybill=wb.waybill, buyer=wb.buyer, contract=wb.contract,
            version=version.version, deductible_kg=version.deductible_kg,
            cents_per_kg=version.cents_per_kg, cap_cents=version.cap_cents,
            confirmed_shortage_kg=confirmed, claimed_kg=claimed,
            eligible_kg=eligible, payable_kg=payable, entitlement_cents=amount,
            paid_cents=paid_by_waybill.get(wb.waybill, 0),
            first_shipment_at=first_shipment[wb.waybill],
            claims=[ClaimLine(c.notice, c.noticed_at, c.amount_kg) for c in lines],
        )
    return result
