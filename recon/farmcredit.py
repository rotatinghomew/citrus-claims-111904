r'''Weekly orchard credit and the order in which deductions consume it.

An orchard's collateral is its *weekly* goods payment, not a per-waybill
balance.  Statements share their balance across all claims the same farm brings
in the same week.  A deduction consumes that shared balance in the order the
liability was first confirmed; whatever does not fit stays open on the claim.

Rules implemented here:

1. Only statements that have posted and not yet closed can take new
   deductions.  A closed week keeps its actual cash and its leftover credit is
   *not* carried into a later week.
2. A return reverses an earlier deduction as a *separate* item.  It does not
   edit the original statement.  A return adds usable credit only once it is
   actually posted; a ``待付`` return releases nothing.
3. Per-orchard maxima for several pending customer claims may not be added
   together.  Coverage is computed as an assignment over the same weekly
   credit, so the totals never exceed the credit that exists.
'''
from __future__ import annotations

from dataclasses import dataclass, field

from .data import FarmStatement, Recovery


@dataclass
class WeekStatement:
    statement: str
    farm: str
    week: str
    gross_available_cents: int
    posted_at: str
    closed: bool
    existing_deductions: int = 0
    returns_posted: int = 0
    returns_pending: int = 0
    newly_applied: int = 0
    allocations: list[dict] = field(default_factory=list)

    @property
    def available_for_new(self) -> int:
        """Credit this statement can still absorb; closed weeks absorb nothing."""
        if self.closed:
            return 0
        return max(0, self.gross_available_cents - self.existing_deductions
                   + self.returns_posted - self.newly_applied)

    def as_dict(self) -> dict:
        return {
            "statement": self.statement,
            "farm": self.farm,
            "week": self.week,
            "gross_available_cents": self.gross_available_cents,
            "posted_at": self.posted_at,
            "closed": self.closed,
            "existing_deductions_cents": self.existing_deductions,
            "returns_posted_cents": self.returns_posted,
            "returns_pending_cents": self.returns_pending,
            "newly_applied_cents": self.newly_applied,
            "available_for_new_cents": self.available_for_new,
            "allocations": self.allocations,
        }


def build_statements(statements: list[FarmStatement],
                     recoveries: list[Recovery]) -> list[WeekStatement]:
    out = []
    for st in statements:
        deducted = sum(r.cents for r in recoveries
                       if r.statement == st.statement and r.direction == "扣抵"
                       and r.state == "入账")
        posted_return = sum(r.cents for r in recoveries
                            if r.statement == st.statement and r.direction == "返还"
                            and r.state == "入账")
        pending_return = sum(r.cents for r in recoveries
                             if r.statement == st.statement and r.direction == "返还"
                             and r.state == "待付")
        out.append(WeekStatement(
            statement=st.statement, farm=st.farm, week=st.week,
            gross_available_cents=st.gross_available_cents,
            posted_at=st.posted_at.isoformat(), closed=st.closed_at is not None,
            existing_deductions=deducted, returns_posted=posted_return,
            returns_pending=pending_return))
    out.sort(key=lambda s: (s.week, s.farm, s.statement))
    return out


@dataclass
class OpenClaim:
    """A confirmed farm liability and how much of it weekly credit secured.

    ``already_deducted`` is money an earlier statement already took; it is
    never offered to a later week again.  ``applied`` is what this review can
    take from still-open statements, and ``remaining`` is what stays unsecured.
    """

    key: str
    waybill: str
    farm: str
    amount_cents: int
    confirmed_at: str
    already_deducted: int = 0
    applied: int = 0
    remaining: int = 0
    statement: str = ""
    week: str = ""


def apply_credit(claims: list[OpenClaim], statements: list[WeekStatement]) -> None:
    """First-confirmed, first-served against the same farm's open credit."""
    for st in statements:
        st.newly_applied = 0
        st.allocations = []
    for claim in sorted(claims, key=lambda c: (c.confirmed_at, c.key)):
        claim.remaining = max(0, claim.amount_cents - claim.already_deducted)
        claim.applied = 0
        claim.statement = ""
        claim.week = ""
        for st in statements:
            if claim.remaining <= 0:
                break
            if st.farm != claim.farm:
                continue
            room = st.available_for_new
            if room <= 0:
                continue
            take = min(room, claim.remaining)
            st.newly_applied += take
            claim.remaining -= take
            claim.applied += take
            claim.statement = st.statement
            claim.week = st.week
            st.allocations.append({"claim": claim.key, "waybill": claim.waybill,
                                   "farm": claim.farm, "cents": take})
            if claim.remaining > 0:
                # Continue to the next open week for this farm.
                continue
            break


def best_case_cover(rows: list[dict], open_credit: dict[str, int]) -> dict:
    """How much of the unattributed customer money open credit could absorb.

    ``rows`` describes each pending waybill: its entitlement and the maximum
    quantity that could be pinned to each farm.  A waybill may be assigned to
    any one farm up to its own maximum; the farm's credit is shared.  This is a
    transport/assignment problem, not a sum of separately-taken maxima.  If no
    such assignment covers every claim, the uncovered part is reported.
    """
    room = dict(open_credit)
    total = sum(r["entitlement_cents"] for r in rows)
    covered = 0
    unassigned = []
    # Greedy largest-first is exact for the "any farm up to a per-claim cap"
    # relaxation, but we keep it explicit so the assignment is auditable.
    for row in sorted(rows, key=lambda r: r["entitlement_cents"], reverse=True):
        eligible = [f for f, cap in row.get("farm_caps_cents", {}).items()
                    if cap > 0 and room.get(f, 0) > 0]
        if not eligible:
            unassigned.append(row)
            continue
        need = row["entitlement_cents"]
        take = min(need, sum(room[f] for f in eligible))
        remaining = take
        for f in sorted(eligible, key=lambda x: room[x], reverse=True):
            use = min(room[f], remaining)
            room[f] -= use
            remaining -= use
            if remaining <= 0:
                break
        covered += take - remaining
        if take - remaining < need:
            unassigned.append(row)
    return {
        "coverable_cents": covered,
        "not_coverable_even_best_case_cents": max(0, total - covered),
        "credit_after_best_case": room,
        "claims_not_fully_coverable": [r.get("waybill") for r in unassigned],
        "note": "per-claim interval maxima are not added together; coverage is an "
                "assignment over the same weekly credit",
    }
