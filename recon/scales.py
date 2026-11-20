"""Revision resolution and the meaning of a weighing record.

Net weight is ``gross - tare``; the reading is only known within
``± tolerance``.  A revision only replaces the revision it names, so tickets
that do not name a predecessor stay at their own version.  ``as_of`` filters
by ``reported_at`` so that a review can be replayed using only what was visible
at that moment.

Several tickets may observe the same parcel node independently; those
observations are intersected.  Intersecting observations that cannot all be
true is a scale conflict, not an extra shortage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .data import ScaleTicket

EPS = 1e-9


class ScaleConflict(ValueError):
    """Two live observations of the same parcel node cannot both be true."""

    def __init__(self, parcel: str, observations: list[ScaleTicket]):
        self.parcel = parcel
        self.observations = observations
        low = max(t.low for t in observations)
        high = min(t.high for t in observations)
        detail = ", ".join(
            f"{t.label} {t.parcel} [{t.low:.2f}, {t.high:.2f}]" for t in observations)
        super().__init__(
            f"parcel {parcel}: live readings do not overlap "
            f"(intersection would be [{low:.2f}, {high:.2f}]); {detail}")


@dataclass
class ScaleSelection:
    chosen: dict[str, ScaleTicket]
    by_ticket: dict[str, list[ScaleTicket]]
    as_of: datetime | None = None
    overrides: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def active(self) -> list[ScaleTicket]:
        return sorted(self.chosen.values(), key=lambda t: (t.reported_at, t.ticket, t.rev))

    def by_parcel(self) -> dict[str, list[ScaleTicket]]:
        out: dict[str, list[ScaleTicket]] = {}
        for ticket in self.active():
            out.setdefault(ticket.parcel, []).append(ticket)
        return out

    def parcel_ranges(self) -> dict[str, tuple[float, float]]:
        """Intersect every live observation of each parcel node."""
        out: dict[str, tuple[float, float]] = {}
        for parcel, tickets in self.by_parcel().items():
            low = max(t.low for t in tickets)
            high = min(t.high for t in tickets)
            if low > high + EPS:
                raise ScaleConflict(parcel, tickets)
            out[parcel] = (low, high)
        return out

    def provenance(self) -> list[dict]:
        rows = []
        for t in self.active():
            rows.append({
                "parcel": t.parcel, "ticket": t.ticket, "rev": t.rev,
                "net_kg": t.net_kg, "low_kg": t.low, "high_kg": t.high,
                "reported_at": t.reported_at.isoformat(),
            })
        return rows


def resolve_scales(tickets: list[ScaleTicket], as_of: datetime | None = None,
                   overrides: dict[str, int] | None = None) -> ScaleSelection:
    visible = [t for t in tickets if as_of is None or t.reported_at <= as_of]
    by_ticket: dict[str, list[ScaleTicket]] = {}
    for t in visible:
        by_ticket.setdefault(t.ticket, []).append(t)
    warnings: list[str] = []
    chosen: dict[str, ScaleTicket] = {}
    for ticket, versions in by_ticket.items():
        for t in versions:
            if t.replaces is not None:
                if t.replaces >= t.rev:
                    warnings.append(f"{t.label} replaces a revision that is not older")
                if not any(v.rev == t.replaces for v in versions):
                    warnings.append(f"{t.label} replaces missing {ticket}:{t.replaces}")
        chosen[ticket] = max(versions, key=lambda t: t.rev)
    if overrides:
        for ticket, rev in overrides.items():
            match = [t for t in by_ticket.get(ticket, []) if t.rev == rev]
            if not match:
                raise ValueError(f"{ticket} rev {rev} not visible")
            chosen[ticket] = match[0]
    return ScaleSelection(chosen=chosen, by_ticket=by_ticket, as_of=as_of,
                          overrides=dict(overrides or {}), warnings=warnings)

