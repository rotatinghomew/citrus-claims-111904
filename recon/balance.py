"""One material-balance system for all scales, splits and transports.

A row of ``movements.csv`` is one leg of a process:

* ``投入``  material enters a lot (parent is the source parcel);
* ``拆分``  a lot is divided into child parcels (no physical loss);
* ``余货``  the remainder of a split goes to another parcel;
* ``拨出``  a lot is dispatched to a child parcel, remaining behind;
* ``损耗``  explicitly measured loss, given as a range;
* ``运输``  a parcel moves to a destination.

For every operation ``inputs - outputs == declared loss + unexplained`` and,
per farm, ``inputs_f - outputs_f == declared loss_f + unexplained_f``.  The
unexplained remainder is the only place a shortage can live; it is not a new
receive node and it is never assigned to a farm just because the parcel number
can be traced to one orchard.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import linprog

EPS = 1e-6

FLOW_IN_KINDS = {"投入"}
FLOW_OUT_KINDS = {"拆分", "余货", "拨出"}
LOSS_KIND = {"损耗"}
TRANSPORT_KIND = {"运输"}


@dataclass(frozen=True)
class Node:
    operation: str
    kind: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    loss_min: float = 0.0
    loss_max: float = 0.0


@dataclass
class Interval:
    low: float
    high: float

    def __round__(self, ndigits: int = 2) -> "Interval":
        return Interval(round(self.low, ndigits), round(self.high, ndigits))

    def as_dict(self) -> dict:
        return {"low": round(self.low, 4), "high": round(self.high, 4)}

    def __str__(self) -> str:
        return f"[{self.low:.2f}, {self.high:.2f}]"


def build_nodes(movements) -> list[Node]:
    """Collapse the per-leg rows into one node per operation.

    Parent is the source lot, child the destination lot, for every physical
    kind (投入 / 拆分 / 余货 / 拨出 / 运输).  A 损耗 row adds an explicit loss
    range to the same node and has no child.  Repeated parent/child references
    (a split and its remainder share the parent) are collapsed once.
    """
    groups: dict[str, dict] = {}
    order: list[str] = []
    for m in movements:
        if m.operation not in groups:
            groups[m.operation] = {"inputs": [], "outputs": [], "loss": (0.0, 0.0),
                                   "kinds": []}
            order.append(m.operation)
        g = groups[m.operation]
        g["kinds"].append(m.kind)
        if m.kind in LOSS_KIND:
            g["loss"] = (m.observed_min_kg or 0.0, m.observed_max_kg or 0.0)
        else:
            if m.parent and m.parent not in g["inputs"]:
                g["inputs"].append(m.parent)
            if m.child and m.child not in g["outputs"]:
                g["outputs"].append(m.child)
    nodes = []
    for op in order:
        g = groups[op]
        nodes.append(Node(operation=op, kind=_op_kind(g), inputs=tuple(g["inputs"]),
                          outputs=tuple(g["outputs"]), loss_min=g["loss"][0],
                          loss_max=g["loss"][1]))
    return nodes


def _op_kind(group: dict) -> str:
    """Name an operation by its dominant physical behaviour."""
    kinds = group["kinds"]
    if any(k in TRANSPORT_KIND for k in kinds):
        return "transport"
    if any(k in FLOW_IN_KINDS for k in kinds):
        return "mix"
    if any(k in FLOW_OUT_KINDS for k in kinds):
        return "split"
    return "loss"


class MaterialBalance:
    """Linear program over parcel-farm quantities and unexplained amounts.

    ``scale_ranges`` maps a parcel to ``(low, high)``; a missing parcel is
    unconstrained, and a ``high`` of ``None`` means "no upper document yet".
    """

    def __init__(self, nodes: list[Node], parcels: list[str], farms: list[str],
                 fixed_origin: dict[str, str],
                 scale_ranges: dict[str, tuple[float, float | None]],
                 extra_caps: list[tuple[dict, float]] | None = None):
        self.nodes = nodes
        self.parcels = parcels
        self.farms = farms
        self.fixed_origin = fixed_origin
        self.scale_ranges = scale_ranges
        self._index: dict[tuple, int] = {}
        self._names: list[tuple] = []
        self._Aeq: list[np.ndarray] = []
        self._beq: list[float] = []
        self._Aub: list[np.ndarray] = []
        self._bub: list[float] = []
        self._build()
        if extra_caps:
            for weights, cap in extra_caps:
                row = np.zeros(self.n)
                for name, coefficient in weights.items():
                    row[self._index[name]] = coefficient
                self._Aub.append(row)
                self._bub.append(cap)

    # -- variable bookkeeping -------------------------------------------------
    def _var(self, name: tuple) -> int:
        if name not in self._index:
            self._index[name] = len(self._names)
            self._names.append(name)
        return self._index[name]

    @property
    def n(self) -> int:
        return len(self._names)

    @property
    def names(self) -> list[tuple]:
        return self._names

    def idx(self, name: tuple) -> int:
        return self._index[name]

    def _row(self) -> np.ndarray:
        return np.zeros(self.n)

    def _eq(self, row: np.ndarray, rhs: float) -> None:
        self._Aeq.append(row)
        self._beq.append(rhs)

    def _le(self, row: np.ndarray, rhs: float) -> None:
        self._Aub.append(row)
        self._bub.append(rhs)

    def _build(self) -> None:
        for p in self.parcels:
            for f in self.farms:
                self._var(("x", p, f))
        for node in self.nodes:
            self._var(("loss", node.operation))
            self._var(("unexplained", node.operation))
            for f in self.farms:
                self._var(("farm_loss", node.operation, f))
                self._var(("farm_unexplained", node.operation, f))
        for p in self.parcels:
            # A parcel without a live weighing is not assumed to be zero; it is
            # simply unconstrained until a document arrives.
            if p not in self.scale_ranges:
                continue
            low, high = self.scale_ranges[p]
            row = self._row()
            for f in self.farms:
                row[self._var(("x", p, f))] = 1.0
            if high is not None:
                self._le(row, high)
            self._le(-row, -low)
        for p, farm in self.fixed_origin.items():
            for f in self.farms:
                if f != farm:
                    row = self._row()
                    row[self._var(("x", p, f))] = 1.0
                    self._le(row, 0.0)
        for node in self.nodes:
            flow = self._row()
            for p in node.inputs:
                for f in self.farms:
                    flow[self._var(("x", p, f))] += 1.0
            for p in node.outputs:
                for f in self.farms:
                    flow[self._var(("x", p, f))] -= 1.0
            flow[self._var(("loss", node.operation))] = -1.0
            flow[self._var(("unexplained", node.operation))] = -1.0
            self._eq(flow, 0.0)
            row = self._row()
            row[self._var(("loss", node.operation))] = 1.0
            self._le(row, node.loss_max)
            self._le(-row, -node.loss_min)
            for f in self.farms:
                per = self._row()
                for p in node.inputs:
                    per[self._var(("x", p, f))] += 1.0
                for p in node.outputs:
                    per[self._var(("x", p, f))] -= 1.0
                per[self._var(("farm_loss", node.operation, f))] = -1.0
                per[self._var(("farm_unexplained", node.operation, f))] = -1.0
                self._eq(per, 0.0)
                # cannot lose more of a farm's material than entered the node
                cap = self._row()
                for p in node.inputs:
                    cap[self._var(("x", p, f))] += 1.0
                cap[self._var(("farm_loss", node.operation, f))] = -1.0
                cap[self._var(("farm_unexplained", node.operation, f))] = -1.0
                self._le(-cap, 0.0)
            row = self._row()
            row[self._var(("loss", node.operation))] = -1.0
            for f in self.farms:
                row[self._var(("farm_loss", node.operation, f))] = 1.0
            self._eq(row, 0.0)
            row = self._row()
            row[self._var(("unexplained", node.operation))] = -1.0
            for f in self.farms:
                row[self._var(("farm_unexplained", node.operation, f))] = 1.0
            self._eq(row, 0.0)

    # -- solving --------------------------------------------------------------
    def _objective(self, weights: dict[tuple, float]) -> np.ndarray:
        c = np.zeros(self.n)
        for name, coef in weights.items():
            c[self._index[name]] = coef
        return c

    def solve(self, weights: dict[tuple, float] | None = None,
              extra: tuple[list[np.ndarray], list[float]] | None = None):
        c = self._objective(weights or {})
        aeq, beq = self._Aeq, self._beq
        if extra:
            aeq = self._Aeq + extra[0]
            beq = self._beq + extra[1]
        return linprog(c, A_ub=np.array(self._Aub), b_ub=np.array(self._bub),
                       A_eq=np.array(aeq), b_eq=np.array(beq),
                       bounds=[(0, None)] * self.n, method="highs")

    def feasible(self) -> bool:
        result = self.solve()
        return result.status == 0

    def range(self, weights: dict[tuple, float]) -> Interval | None:
        low = self.solve(weights)
        if low.status != 0:
            return None
        high = self.solve({k: -v for k, v in weights.items()})
        if high.status != 0:
            return None
        # Numerical dust from HiGHS can make a zero bound read -0.0 or 1e-9.
        lo = max(0.0, float(low.fun)) if all(v >= 0 for v in weights.values()) else float(low.fun)
        hi = float(-high.fun)
        if all(v >= 0 for v in weights.values()):
            hi = max(0.0, hi)
        return Interval(lo, hi)

    def simultaneous(self, constraints: list[tuple[dict[tuple, float], float]]) -> bool:
        """Can every named quantity reach at least its stated minimum at once?"""
        extra_rows, extra_bounds = [], []
        for weights, minimum in constraints:
            row = self._objective(weights)
            extra_rows.append(-row)
            extra_bounds.append(-minimum)
        return self.solve(extra=(extra_rows, extra_bounds)).status == 0

    def witness(self, weights: dict[tuple, float]) -> dict[tuple, float] | None:
        """A feasible point that attains the objective, for inspection."""
        result = self.solve(weights)
        if result.status != 0:
            return None
        return {name: float(result.x[i]) for i, name in enumerate(self._names)}
