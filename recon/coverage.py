"""How much of the unattributed customer money open weekly credit can secure.

The tempting answer is to take each waybill's most favourable farm split and
add them up.  That is wrong twice over: it can exceed the credit that exists,
and the separately-taken maxima need not be jointly attainable in the material
balance.  This module solves the actual question as one linear programme.

Variables (all continuous, cents or kilograms):

* ``q[w,f]``  kilograms of waybill ``w``'s confirmed shortage attributed to
  farm ``f``; these are exactly the balance model's ``farm_unexplained``
  variables, so every weighing and loss constraint still applies.
* ``z[w,f]``  cents of ``w``'s liability actually charged to ``f``; bounded by
  the effective rate (entitlement / confirmed kg, deductible and cap already
  applied) times ``q[w,f]``, and by farm ``f``'s open credit.
* ``cov[w]``  cents of ``w``'s liability secured in total.

The program is solved once for all pending waybills; it is never the sum of
per-waybill optima.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import linprog

EPS = 1e-6


def max_cover(mb, pending: list[dict], credit_by_farm: dict[str, int]) -> dict:
    """Maximum cents securable across all pending waybills, jointly.

    ``pending`` rows: ``{"waybill", "entitlement_cents", "confirmed_kg",
    "operation"}``.  Returns the covered total plus the part that cannot be
    covered even under the most favourable *joint* attribution.
    """
    pending = [p for p in pending if p["entitlement_cents"] > 0]
    if not pending:
        return {"coverable_cents": 0, "not_coverable_even_best_case_cents": 0,
                "assignment": [], "jointly_feasible": True,
                "note": "no pending unattributed amount"}

    base_n = mb.n
    index = {}
    for wb_i, row in enumerate(pending):
        for farm in mb.farms:
            index[("z", wb_i, farm)] = base_n + len(index)
    for wb_i, row in enumerate(pending):
        index[("cov", wb_i)] = base_n + len(index)
    n = base_n + len(index)
    c = np.zeros(n)
    for wb_i in range(len(pending)):
        c[index[("cov", wb_i)]] = -1.0     # maximise coverage

    aeq = [np.pad(r, (0, n - base_n)) for r in mb._Aeq]
    beq = list(mb._beq)
    aub = [np.pad(r, (0, n - base_n)) for r in mb._Aub]
    bub = list(mb._bub)

    for wb_i, row in enumerate(pending):
        op = row["operation"]
        # Money per confirmed kilogram of this waybill.  The entitlement has
        # already applied the deductible and the cap, so the rate here is the
        # effective one; splitting it over ``q[w,f]`` gives each farm's share.
        confirmed = float(row["confirmed_kg"])
        value_per_kg = (float(row["entitlement_cents"]) / confirmed
                        if confirmed > EPS else 0.0)
        # cov <= sum_f z
        r = np.zeros(n)
        r[index[("cov", wb_i)]] = 1.0
        for farm in mb.farms:
            r[index[("z", wb_i, farm)]] = -1.0
        aub.append(r)
        bub.append(0.0)
        # cov <= entitlement
        r = np.zeros(n)
        r[index[("cov", wb_i)]] = 1.0
        aub.append(r)
        bub.append(float(row["entitlement_cents"]))
        for farm in mb.farms:
            # z[w,f] <= value_per_kg * q[w,f]; q is a material-balance variable.
            r = np.zeros(n)
            r[index[("z", wb_i, farm)]] = 1.0
            try:
                r[mb.idx(("farm_unexplained", op, farm))] = -value_per_kg
            except KeyError:
                continue
            aub.append(r)
            bub.append(0.0)
        # The farm split may not exceed the confirmed shortage itself, however
        # generously the material balance could otherwise split it.
        r = np.zeros(n)
        for farm in mb.farms:
            r[mb.idx(("farm_unexplained", op, farm))] = 1.0
        aub.append(r)
        bub.append(confirmed)
    for farm, credit in credit_by_farm.items():
        r = np.zeros(n)
        for wb_i in range(len(pending)):
            r[index[("z", wb_i, farm)]] = 1.0
        aub.append(r)
        bub.append(float(max(0, credit)))

    bounds = [(0, None)] * n
    result = linprog(c, A_ub=np.array(aub), b_ub=np.array(bub),
                     A_eq=np.array(aeq), b_eq=np.array(beq),
                     bounds=bounds, method="highs")
    if result.status != 0:
        return {"coverable_cents": 0,
                "not_coverable_even_best_case_cents":
                    sum(p["entitlement_cents"] for p in pending),
                "assignment": [], "jointly_feasible": False,
                "note": "coverage programme has no solution"}
    assignment = []
    covered = 0
    for wb_i, row in enumerate(pending):
        cov = result.x[index[("cov", wb_i)]]
        covered += int(round(cov))
        for farm in mb.farms:
            z = result.x[index[("z", wb_i, farm)]]
            if z > EPS:
                assignment.append({"waybill": row["waybill"], "farm": farm,
                                   "cents": int(round(z))})
    total = sum(p["entitlement_cents"] for p in pending)
    return {
        "coverable_cents": covered,
        "not_coverable_even_best_case_cents": max(0, total - covered),
        "assignment": assignment,
        "jointly_feasible": True,
        "note": "solved as one programme over the material balance and the "
                "shared weekly credit; per-claim upper bounds are not added",
    }
