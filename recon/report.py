"""Presentation layer: JSON and markdown for humans and downstream systems."""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path

from .engine import Reconciliation
from .balance import Interval


def _jsonable(value):
    if isinstance(value, Interval):
        return value.as_dict()
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float):
        return round(value, 6)
    return value


def payload(bundle: Reconciliation) -> dict:
    return {
        "as_of": bundle.as_of.isoformat() if bundle.as_of else "latest",
        "scales_used": bundle.selection.provenance(),
        "scale_warnings": bundle.selection.warnings,
        "waybills": {
            wb: {
                "buyer": att.buyer,
                "shortage_kg": att.shortage.as_dict(),
                "farms": {f: {"low_kg": round(iv.low, 4), "high_kg": round(iv.high, 4)}
                          for f, iv in att.farms.items()},
                "confirmed_farm_kg": round(att.confirmed_kg(), 4),
                "unresolved_kg": round(att.unresolved_kg(), 4),
                "conflict": att.conflict,
            }
            for wb, att in bundle.attributions.items()
        },
        "joint_maxima": {k: v.as_dict() for k, v in bundle.joint.items()},
        "aggregate": bundle.aggregate,
        "balance_detail": bundle.balance_detail(),
        "buyers": bundle.buyer_table(),
        "orchards": bundle.orchard_table(),
        "conflicts": [c.as_dict() for c in bundle.conflicts],
        "corrections": [c.as_dict() for c in bundle.corrections],
        "credit": bundle.credit,
        "close": _jsonable(bundle.close_summary()),
        "adjustments": [a.as_dict() for a in bundle.adjustments.applied],
        "next_run": bundle.schedule(),
        "latest_correction_impact": bundle.latest_correction_impact(),
    }


def to_json(bundle: Reconciliation, path: Path | None = None) -> str:
    text = json.dumps(_jsonable(payload(bundle)), ensure_ascii=False, indent=2)
    if path:
        path.write_text(text + "\n", encoding="utf-8")
    return text


def _money(value: int) -> str:
    return f"{value / 100:,.2f}"


def to_markdown(bundle: Reconciliation) -> str:
    lines: list[str] = []
    add = lines.append
    add("# 混装短少：买方应赔与果园扣款对账")
    add("")
    add(f"- 资料截止：{bundle.as_of.isoformat() if bundle.as_of else '最新可见记录'}")
    add(f"- 重量区间均为当票毛重、皮重与容差算出的可行范围；金额单位分")
    add("")
    add("## 一、运单可确认与未分责重量")
    add("")
    add("| 运单 | 买方 | 短少总量(kg) | 可确认果园 | 可确认量(kg) | 未分责(kg) | 状态 |")
    add("| --- | --- | --- | --- | --- | --- | --- |")
    for wb, att in bundle.attributions.items():
        status = "资料冲突，暂停结算" if att.conflict else "可结算"
        farms = "、".join(att.confirmed_farms) or "—"
        add(f"| {wb} | {att.buyer} | {att.shortage.low:.2f}–{att.shortage.high:.2f} "
            f"| {farms} | {att.confirmed_kg():.2f} | {att.unresolved_kg():.2f} | {status} |")
    add("")
    add("## 一之二、各环节正常损耗、转出与仍未说明")
    add("")
    add("| 操作 | 性质 | 投入 | 转出 | 正常损耗(申报) | 正常损耗(可行) | 仍未说明(kg) |")
    add("| --- | --- | --- | --- | --- | --- | --- |")
    kinds = {"mix": "合批", "split": "拆分/拨出", "transport": "运输", "loss": "损耗"}
    for row in bundle.balance_detail():
        loss = row["loss_kg"]
        declared = (f"{loss['declared_min']:.0f}–{loss['declared_max']:.0f}"
                    if loss["declared_max"] else "—")
        add(f"| {row['operation']} | {kinds.get(row['kind'], row['kind'])} "
            f"| {'、'.join(row['inputs']) or '—'} | {'、'.join(row['outputs']) or '—'} "
            f"| {declared} | {loss['admitted_low']:.2f}–{loss['admitted_high']:.2f} "
            f"| {row['unexplained_kg']['low']:.2f}–{row['unexplained_kg']['high']:.2f} |")
    add("")
    add("> 只有“仍未说明”的重量可能成为短少；正常损耗与转出不另算一份赔付。")
    add("")
    add("## 二、最大归属量能否同时成立")
    add("")
    for name, check in bundle.joint.items():
        verdict = "可以同时成立" if check.feasible else "不能同时成立"
        parties = "、".join(check.parties) or "—"
        add(f"- **{name}**（{parties}）：{verdict}"
            + (f"；最小冲突组合：{'、'.join(check.minimal_conflict)}"
               if check.minimal_conflict else ""))
    add("")
    add("### 客户与果园汇总（真实合计区间，不是各单上限相加）")
    add("")
    add("| 客户 | 涉及运单 | 短少合计(kg) | 已明确到果园(kg) | 未分责(kg) | 各单上限相加(仅对照) |")
    add("| --- | --- | --- | --- | --- | --- |")
    for buyer, row in bundle.aggregate["buyers"].items():
        add(f"| {buyer} | {'、'.join(row['waybills'])} "
            f"| {row['shortage_kg']['low']:.2f}–{row['shortage_kg']['high']:.2f} "
            f"| {row['confirmed_farm_kg']:.2f} | {row['unresolved_kg']:.2f} "
            f"| {row['sum_of_individual_highs_kg']:.2f} |")
    add("")
    add("| 果园 | 涉及运单 | 可确认(kg) | 可能上限(kg) |")
    add("| --- | --- | --- | --- |")
    for farm, row in bundle.aggregate["farms"].items():
        add(f"| {farm} | {'、'.join(row['waybills'])} "
            f"| {row['confirmed_kg']:.2f} | {row['possible_kg']:.2f} |")
    add("")
    add("## 三、买方应赔（与果园归属分离）")
    add("")
    add("| 运单 | 买方 | 合同 | 已确认短少 | 申报合计 | 免赔 | 可赔重量 | 应赔 | 已付 | 待办 | 下次可办 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for wb, ent in bundle.entitlements.items():
        buyer = bundle.buyers[wb]
        add(f"| {wb} | {ent.buyer} | {ent.contract}/{ent.version} | {ent.confirmed_shortage_kg:.2f} "
            f"| {ent.claimed_kg:.2f} | {ent.deductible_kg:.2f} | {ent.payable_kg:.2f} "
            f"| {_money(ent.entitlement_cents)} | {_money(ent.paid_cents)} "
            f"| {_money(buyer.pending_cents)} | {_money(buyer.balance_cents)} |")
    add("")
    add("## 四、果园应扣与周结货款")
    add("")
    add("| 运单 | 果园 | 已确认量(kg) | 应扣 | 已扣(历史) | 本次开账抵扣 | 仍未落地 | 周结单 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for o in bundle.orchards:
        if o.amount_cents or o.deducted_cents or o.open_cents:
            add(f"| {o.waybill} | {o.farm} | {o.confirmed_kg:.2f} | {_money(o.amount_cents)} "
                f"| {_money(o.deducted_cents)} | {_money(o.newly_applied_cents)} "
                f"| {_money(o.open_cents)} | {o.statement or '—'} |")
    add("")
    add("### 周结货款余额")
    add("")
    add("| 结算单 | 果园 | 周 | 总可用 | 已扣 | 已入账返还 | 待付返还 | 本次抵扣 | 尚可新扣 | 关账 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for st in bundle.credit["statements"]:
        add(f"| {st['statement']} | {st['farm']} | {st['week']} "
            f"| {_money(st['gross_available_cents'])} | {_money(st['existing_deductions_cents'])} "
            f"| {_money(st['returns_posted_cents'])} | {_money(st['returns_pending_cents'])} "
            f"| {_money(st['newly_applied_cents'])} "
            f"| {_money(st['available_for_new_cents'])} | {'是' if st['closed'] else '否'} |")
    add("")
    add("### 果园现金（返还单列，不改写原扣款）")
    add("")
    add("| 果园 | 已确认应扣 | 已入账扣抵 | 本次抵扣 | 仍未落地 | 应返还(无归属扣款) | 已入账返还 | 待付返还 | 净留现金 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for f in bundle.farm_table():
        add(f"| {f['farm']} | {_money(f['amount_cents'])} | {_money(f['deducted_cents'])} "
            f"| {_money(f['newly_applied_cents'])} | {_money(f['open_cents'])} "
            f"| {_money(f['refund_due_cents'])} | {_money(f['refund_posted_cents'])} "
            f"| {_money(f['refund_pending_cents'])} | {_money(f['net_cash_cents'])} |")
    add("")
    add("## 五、资料冲突与新磅单更正")
    add("")
    if not bundle.conflicts:
        add("无。")
    for c in bundle.conflicts:
        add(f"- **{c.label}**（{c.parcel}）不可与其余记录同时成立；"
            f"受影响运单：{'、'.join(c.affected_waybills) or '无'}；"
            f"未受影响：{'、'.join(c.unaffected_waybills) or '无'}。")
        add(f"  - 依据：{c.basis}")
    add("")
    if bundle.corrections:
        add("| 磅单 | 版本 | 报送 | 前版可行 | 新版可行 | 受影响运单 | 说明 |")
        add("| --- | --- | --- | --- | --- | --- | --- |")
        for c in bundle.corrections:
            add(f"| {c.ticket} | {c.from_rev}→{c.to_rev} | {c.reported_at:%Y-%m-%d %H:%M} "
                f"| {'是' if c.from_feasible else '否'} | {'是' if c.to_feasible else '否'} "
                f"| {'、'.join(c.affected_waybills) or '—'} | {c.note or '—'} |")
        add("")
    add("## 六、未分责客户款与周结空间")
    add("")
    ua = bundle.credit["unallocated_buyer"]
    add("| 运单 | 买方 | 应赔 | 在办 | 短少(kg) | 可能涉及果园及最大归属(kg) |")
    add("| --- | --- | --- | --- | --- | --- |")
    for c in ua["claims"]:
        farms = "、".join(f"{f} {kg:.2f}" for f, kg in c["farm_max_kg"].items()) or "—"
        add(f"| {c['waybill']} | {c['buyer']} | {_money(c['entitlement_cents'])} "
            f"| {_money(c['next_cents'])} | {c['shortage_kg']['low']:.2f}–"
            f"{c['shortage_kg']['high']:.2f} | {farms} |")
    add("")
    add(f"- 待分责应赔合计：{_money(ua['claim_gross_total_cents'])}"
        f"（其中尚未支付 {_money(ua['claim_next_total_cents'])}）")
    add(f"- 尚未关账周的可用货款：{_money(ua['open_credit_total_cents'])}"
        + ("（" + "，".join(f"{f} {_money(v)}" for f, v in
                           ua["open_credit_by_farm_cents"].items() if v) + "）"
           if any(ua["open_credit_by_farm_cents"].values()) else ""))
    add(f"- 联合最优归属下可覆盖：{_money(ua['coverable_cents'])}")
    add(f"- 即使在最有利归属下仍不能覆盖：{_money(ua['not_coverable_even_best_case_cents'])}")
    if ua.get("assignment"):
        add("- 最优归属试算（仅用于说明空间，不构成扣款）："
            + "，".join(f"{a['waybill']}→{a['farm']} {_money(a['cents'])}"
                        for a in ua["assignment"]))
    add(f"- 口径：{ua['note']}；各运单区间上限未相加")
    add("")
    if bundle.credit["refund_items"]:
        add("### 需单独办理的返还")
        add("")
        for r in bundle.credit["refund_items"]:
            add(f"- {r['farm']} / {r['waybill']}：无归属扣款 "
                f"{_money(r['unsupported_deduction_cents'])}，已入账返还 "
                f"{_money(r['posted_return_cents'])}，仍应返还 "
                f"{_money(r['outstanding_return_cents'])}。{r['basis']}")
        add("")
    impact = bundle.latest_correction_impact()
    add("## 六之二、最近一次更正影响了谁")
    add("")
    if not impact:
        add("无更正记录。")
    else:
        c = impact["correction"]
        add(f"- 最近一次有实质影响的更正：{c['ticket']} {c['from_rev']}→{c['to_rev']}"
            f"（{c['reported_at']}）；可行 {c['from_feasible']}→{c['to_feasible']}。"
            f"{c['note'] or ''}")
        latest = impact["latest_by_time"]
        if impact.get("latest_with_effect") is not None and latest["ticket"] != c["ticket"]:
            add(f"- 时间上更晚的 {latest['ticket']} {latest['from_rev']}→{latest['to_rev']}"
                f"（{latest['reported_at']}）没有改变任何金额。")
        elif impact.get("latest_with_effect") is None:
            add(f"- 时间上最新的 {latest['ticket']} {latest['from_rev']}→{latest['to_rev']} "
                f"（{latest['reported_at']}）没有改变任何金额。")
        if impact["affected"]:
            add("")
            add("| 运单 | 客户 | 更正前在办 | 更正后在办 | 差额 | 涉及果园 | 待办事项 |")
            add("| --- | --- | --- | --- | --- | --- | --- |")
            # quarantined revisions keep the previous amount and create no new cash
            for row in impact["affected"]:
                before = ("—" if row["before_cents"] is None
                          else _money(row["before_cents"]))
                after = ("维持 " + _money(row["current_executable_cents"])
                         if row["quarantined_by_this_revision"]
                         else ("—" if row["after_cents"] is None
                               else _money(row["after_cents"])))
                delta = ("被隔离" if row["quarantined_by_this_revision"]
                         else ("—" if row["delta_cents"] is None
                               else _money(row["delta_cents"])))
                add(f"| {row['waybill']} | {row['party']} | {before} | {after} "
                    f"| {delta} | {'、'.join(row['farms_touched']) or '—'} "
                    f"| {row['pending_item']} |")
            add("")
        else:
            add("- 本次更正没有改变任何运单的可办金额或果园归属。")
        add(f"- 不重新质疑：{'、'.join(impact['untouched_waybills']) or '无'}"
            f"（{impact['note']}）")
    add("")
    add("## 六之三、下一笔可办理")
    add("")
    add("| 事项 | 运单 | 对象 | 类型 | 金额 | 状态 |")
    add("| --- | --- | --- | --- | --- | --- |")
    for item in bundle.schedule():
        add(f"| {item['entry']} | {item['waybill']} | {item.get('party', '')} "
            f"| {item['kind']} | {_money(item['cents'])} | {item['status']} |")
    add("")
    add("> “待办”可直接办理；“待复核”是修订后出现的差额，须复核员确认后才生成追回或补付。")
    add("> 同一办理凭证重复送达不生成第二笔；金额由当前应赔与已付现金重新计算。")
    add("")
    add("## 六之四、单独挂起、不并入现金总额的事项")
    add("")
    suspense = bundle.close_summary()["suspense"]
    if not suspense:
        add("无。")
    else:
        add("| 运单 | 对象 | 类型 | 金额 | 依据 |")
        add("| --- | --- | --- | --- | --- |")
        for row in suspense:
            add(f"| {row['waybill']} | {row['party']} | {row['kind']} "
                f"| {_money(row['cents'])} | {row['basis']} |")
    add("")
    add("## 七、关账恒等式")
    add("")
    add("### 按客户")
    add("")
    add("| 客户 | 运单 | 应赔 | 已付 | 下次应付 | 已明确到果园 | 未能分责 | 冻结已付(资料冲突) |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in bundle.close_summary()["by_buyer"]:
        add(f"| {row['buyer']} | {'、'.join(row['waybills'])} "
            f"| {_money(row['entitlement_cents'])} | {_money(row['paid_cents'])} "
            f"| {_money(row['next_due_cents'])} | {_money(row['confirmed_farm_cents'])} "
            f"| {_money(row['not_attributable_cents'])} | {_money(row['held_paid_cents'])} |")
    add("")
    add("### 按果园")
    add("")
    add("| 果园 | 应扣 | 已扣 | 本次抵扣 | 仍未落地 | 已入账返还 | 待付返还 | 仍应返还 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in bundle.close_summary()["by_farm"]:
        add(f"| {row['farm']} | {_money(row['amount_cents'])} "
            f"| {_money(row['deducted_cents'])} | {_money(row['newly_applied_cents'])} "
            f"| {_money(row['open_cents'])} | {_money(row.get('refund_posted_cents', 0))} "
            f"| {_money(row.get('refund_pending_cents', 0))} "
            f"| {_money(row.get('refund_outstanding_cents', 0))} |")
    add("")
    close = bundle.close_summary()
    ident = close["identity"]
    add(f"- 已付 {_money(ident['paid_cents'])} + 下次应付 {_money(ident['next_due_cents'])} "
        f"= 应赔 {_money(ident['entitlement_cents'])} + 待复核多付 {_money(ident['overpaid_cents'])}；"
        f"校验 {'通过' if ident['check_balances'] else '不通过'}")
    split = close["split_of_entitlement"]
    add(f"- 应赔额中：已明确到果园 {_money(split['confirmed_to_named_orchard_cents'])} "
        f"+ 未能分责 {_money(split['not_yet_attributable_cents'])}；"
        f"校验 {'通过' if split['reconciles'] else '不通过'}")
    cash = close["orchard_cash"]
    add(f"- 果园现金：已入账扣抵 {_money(cash['posted_deductions_cents'])}，"
        f"已入账返还 {_money(cash['posted_returns_cents'])}，"
        f"待付返还 {_money(cash['pending_returns_cents'])}，"
        f"已确认但仍未落地 {_money(cash['confirmed_but_unsecured_cents'])}")
    add("")
    return "\n".join(lines)


def write(bundle: Reconciliation, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": out_dir / "reconciliation.json",
        "markdown": out_dir / "reconciliation.md",
        "audit": out_dir / "audit.json",
    }
    to_json(bundle, paths["json"])
    paths["markdown"].write_text(to_markdown(bundle) + "\n", encoding="utf-8")
    audit = {wb: bundle.audit(wb) for wb in bundle.attributions}
    paths["audit"].write_text(
        json.dumps(_jsonable(audit), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    for wb in bundle.attributions:
        paths[f"audit_{wb}"] = out_dir / f"audit-{wb}.md"
        paths[f"audit_{wb}"].write_text(
            audit_markdown(bundle, wb) + "\n", encoding="utf-8")
    return paths


def audit_markdown(bundle: Reconciliation, waybill: str) -> str:
    data = bundle.audit(waybill)
    lines: list[str] = []
    add = lines.append
    now = data["now"]
    add(f"# 运单 {waybill} 赔付查账")
    add("")
    add(f"- 买方：{data['buyer']}")
    add(f"- 合同：{data['contract_now']['contract']} / {data['contract_now']['version']}"
        f"（首次出运 {data['contract_now']['in_force_at_first_shipment']} 时生效）")
    add(f"- 免赔 {data['contract_now']['deductible_kg']} kg，"
        f"{data['contract_now']['cents_per_kg']} 分/kg，"
        f"上限 {_money(data['contract_now']['cap_cents'])}")
    add("")
    add("## 当时用了哪些单、哪版合同")
    add("")
    if not data["payments_then"]:
        add("尚无已付记录；以下列出当前采用的有效磅单，供首次办理核对。")
        add("")
        upstream = {d["parcel"] for d in data["documents_now"]}
        add("- 当前采用磅单：" + "、".join(
            f"{d['ticket']}:{d['rev']}（{d['parcel']} "
            f"{d['low_kg']:.0f}–{d['high_kg']:.0f} kg）"
            for d in data["documents_now"]))
    for pay in data["payments_then"]:
        add(f"### {pay['entry']}（{pay['authorized_at']}，{_money(pay['paid_cents'])}）")
        add("")
        if pay.get("contract"):
            c = pay["contract"]
            add(f"- 合同版本：{c['contract']} / {c['version']}；"
                f"免赔 {c['deductible_kg']} kg；{c['cents_per_kg']} 分/kg；"
                f"上限 {_money(c['cap_cents'])}")
            add(f"- 当时可见申报："
                + ("、".join(f"{x['notice']} {x['amount_kg']} kg"
                            for x in pay.get("claims_visible_then", [])) or "无"))
            add(f"- 当时可确认短少 {pay['confirmed_kg_then']:.2f} kg，"
                f"应赔 {_money(pay['entitlement_then_cents'])}")
        add(f"- 当时采用的磅单："
            + "、".join(f"{d['ticket']}:{d['rev']}（{d['parcel']} "
                        f"{d['low_kg']:.0f}–{d['high_kg']:.0f} kg）"
                        for d in pay["documents_used"]))
        add("")
    add("## 现在有什么不同")
    add("")
    if now["quarantined"]:
        add("- 该运单目前因资料冲突暂缓结算：不新增付款，也不把冲突当作新增短少。")
    add(f"- 短少范围：{now['shortage_kg']['low']:.2f}–{now['shortage_kg']['high']:.2f} kg")
    add(f"- 可明确到果园：{now['confirmed_farm_kg']:.2f} kg；未分责 "
        f"{now['unresolved_kg']:.2f} kg")
    for farm, iv in now["farms"].items():
        if iv["high_kg"]:
            add(f"  - {farm}：{iv['low_kg']:.2f}–{iv['high_kg']:.2f} kg")
    add(f"- 应赔 {_money(now['entitlement_cents'])}；已付 {_money(now['paid_cents'])}；"
        f"余额 {_money(now['balance_cents'])}")
    add(f"- **下一笔可办理：{_money(now['next_cents'])}**")
    add("")
    if data["corrections"]:
        add("## 更正经过")
        add("")
        for c in data["corrections"]:
            add(f"- {c['ticket']} {c['from_rev']}→{c['to_rev']}（{c['reported_at']}）："
                f"可行 {c['feasible'][0]}→{c['feasible'][1]}。{c['note'] or ''}")
            if c.get("change"):
                ch = c["change"]
                add(f"  - 该运单应赔 {ch['entitlement_cents'][0]} → "
                    f"{ch['entitlement_cents'][1]} 分")
        add("")
    return "\n".join(lines)
