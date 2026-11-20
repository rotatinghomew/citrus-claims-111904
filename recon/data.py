"""Typed views over the fictional CSV ledgers."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


BEIJING = timezone(timedelta(hours=8))


def _dt(value: str) -> datetime:
    """Every timestamp is business time in Beijing.

    Date-only values (contract activation) mean the start of that day in
    Beijing, so comparisons never mix naive and aware datetimes.
    """
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=BEIJING)


def read_rows(name: str, root: Path = DATA) -> list[dict[str, str]]:
    with (root / f"{name}.csv").open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


@dataclass(frozen=True)
class ScaleTicket:
    ticket: str
    rev: int
    parcel: str
    gross_kg: float
    tare_kg: float
    tolerance_kg: float
    reported_at: datetime
    replaces: int | None

    @property
    def net_kg(self) -> float:
        return self.gross_kg - self.tare_kg

    @property
    def low(self) -> float:
        return self.net_kg - self.tolerance_kg

    @property
    def high(self) -> float:
        return self.net_kg + self.tolerance_kg

    @property
    def label(self) -> str:
        return f"{self.ticket}:{self.rev}"

    def as_dict(self) -> dict:
        return {
            "ticket": self.ticket,
            "rev": self.rev,
            "parcel": self.parcel,
            "gross_kg": self.gross_kg,
            "tare_kg": self.tare_kg,
            "tolerance_kg": self.tolerance_kg,
            "net_kg": self.net_kg,
            "low_kg": self.low,
            "high_kg": self.high,
            "reported_at": self.reported_at.isoformat(),
            "replaces": self.replaces,
        }


@dataclass(frozen=True)
class Movement:
    operation: str
    kind: str
    parent: str
    child: str
    observed_min_kg: float | None
    observed_max_kg: float | None
    occurred_at: datetime


@dataclass(frozen=True)
class Waybill:
    waybill: str
    parcel: str
    destination: str
    buyer: str
    contract: str


@dataclass(frozen=True)
class ContractVersion:
    contract: str
    version: str
    active_from: datetime
    deductible_kg: float
    cents_per_kg: int
    cap_cents: int


@dataclass(frozen=True)
class Claim:
    notice: str
    waybill: str
    noticed_at: datetime
    amount_kg: float


@dataclass(frozen=True)
class LedgerEntry:
    entry: str
    waybill: str
    party: str
    kind: str
    cents: int
    authorized_at: datetime
    origin_refs: tuple[str, ...]


@dataclass(frozen=True)
class FarmStatement:
    statement: str
    farm: str
    week: str
    posted_at: datetime
    gross_available_cents: int
    closed_at: datetime | None


@dataclass(frozen=True)
class Recovery:
    entry: str
    statement: str
    direction: str
    state: str
    cents: int
    known_at: datetime
    waybill: str | None
    original_entry: str | None


def load_scales(root: Path = DATA) -> list[ScaleTicket]:
    out = []
    for r in read_rows("scales", root):
        out.append(ScaleTicket(
            ticket=r["ticket"], rev=int(r["rev"]), parcel=r["parcel"],
            gross_kg=float(r["gross_kg"]), tare_kg=float(r["tare_kg"]),
            tolerance_kg=float(r["tolerance_kg"]),
            reported_at=_dt(r["reported_at"]),
            replaces=int(r["replaces"]) if r["replaces"] else None,
        ))
    return out


def load_movements(root: Path = DATA) -> list[Movement]:
    return [Movement(
        operation=r["operation"], kind=r["kind"], parent=r["parent"], child=r["child"],
        observed_min_kg=float(r["observed_min_kg"]) if r["observed_min_kg"] else None,
        observed_max_kg=float(r["observed_max_kg"]) if r["observed_max_kg"] else None,
        occurred_at=_dt(r["occurred_at"]),
    ) for r in read_rows("movements", root)]


def load_origins(root: Path = DATA) -> dict[str, str]:
    return {r["parcel"]: r["farm"] for r in read_rows("origins", root)}


def load_waybills(root: Path = DATA) -> list[Waybill]:
    return [Waybill(waybill=r["waybill"], parcel=r["parcel"], destination=r["destination"],
                    buyer=r["buyer"], contract=r["contract"])
            for r in read_rows("waybills", root)]


def load_contracts(root: Path = DATA) -> list[ContractVersion]:
    return [ContractVersion(
        contract=r["contract"], version=r["version"], active_from=_dt(r["active_from"]),
        deductible_kg=float(r["deductible_kg"]), cents_per_kg=int(r["cents_per_kg"]),
        cap_cents=int(r["cap_cents"])) for r in read_rows("contracts", root)]


def load_claims(root: Path = DATA) -> list[Claim]:
    return [Claim(notice=r["notice"], waybill=r["waybill"],
                  noticed_at=_dt(r["noticed_at"]), amount_kg=float(r["amount_kg"]))
            for r in read_rows("claims", root)]


def load_ledger(root: Path = DATA) -> list[LedgerEntry]:
    return [LedgerEntry(
        entry=r["entry"], waybill=r["waybill"], party=r["party"], kind=r["kind"],
        cents=int(r["cents"]), authorized_at=_dt(r["authorized_at"]),
        origin_refs=tuple(x for x in r["origin_refs"].split(";") if x))
        for r in read_rows("ledger", root)]


def load_farm_credit(root: Path = DATA) -> list[FarmStatement]:
    return [FarmStatement(
        statement=r["statement"], farm=r["farm"], week=r["week"],
        posted_at=_dt(r["posted_at"]), gross_available_cents=int(r["gross_available_cents"]),
        closed_at=_dt(r["closed_at"]) if r["closed_at"] else None)
        for r in read_rows("farm_credit", root)]


def load_recoveries(root: Path = DATA) -> list[Recovery]:
    return [Recovery(
        entry=r["entry"], statement=r["statement"], direction=r["direction"],
        state=r["state"], cents=int(r["cents"]), known_at=_dt(r["known_at"]),
        waybill=r["related_waybill"] or None, original_entry=r["original_entry"] or None)
        for r in read_rows("recoveries", root)]


def load_all(root: Path = DATA) -> dict:
    return {
        "scales": load_scales(root),
        "movements": load_movements(root),
        "origins": load_origins(root),
        "waybills": load_waybills(root),
        "contracts": load_contracts(root),
        "claims": load_claims(root),
        "ledger": load_ledger(root),
        "farm_credit": load_farm_credit(root),
        "recoveries": load_recoveries(root),
    }
