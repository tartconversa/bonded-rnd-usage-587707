"""集市层：库存余额、项目成本、核销交接三张可读仓表与三方核对，全部同源。"""
from decimal import Decimal, ROUND_HALF_UP
import csv
import json
from pathlib import Path

from .constants import PENDING_EVIDENCE
from .facts import container_deps
from .model import canon

FEN = Decimal("0.01")


def quant2(value):
    return value.quantize(FEN, rounding=ROUND_HALF_UP)


def latest_weighing(facts, pack_id):
    items = [w for w in facts.weighings if w.pack_id == pack_id]
    return max(items, key=lambda w: w.at) if items else None


def build_marts(facts):
    """从事实层产出全部仓表（内存结构）；写文件由 write_marts 完成。"""
    moment = facts.cutoff
    containers = facts.containers

    # 容器台账：包装明细可追到来源行
    ledger = []
    for pack_id in sorted(containers):
        c = containers[pack_id]
        book = c.book_at(moment)
        pending = c.pending_at(moment)
        expected = book - pending if book is not None else None
        weigh = latest_weighing(facts, pack_id)
        ledger.append({
            "pack_id": pack_id,
            "material_id": c.material,
            "container_kind": c.kind,
            "parent_pack": c.parent,
            "spec_id": c.spec_id,
            "nominal_g": c.nominal_g,
            "holder": c.holder_at(moment),
            "start_g": c.start_g,
            "confirmed_out_g": sum((o.grams for o in c.outs
                                    if o.confirmed and o.grams is not None), Decimal(0)),
            "pending_out_g": pending,
            "book_g": book,
            "expected_g": expected,
            "measured_g": weigh.remaining_g if weigh else None,
            "diff_g": weigh.diff_g if weigh else None,
            "recon_status": weigh.status if weigh else ("unknown" if book is None else "no_weighing"),
            "deps": container_deps(c, facts.weighings),
        })

    # 在手余额：按保管方汇总，领用不减少全区总量，只移动归属
    holders = {}
    for row in ledger:
        key = (row["holder"], row["material_id"])
        slot = holders.setdefault(key, {"holder": row["holder"], "material_id": row["material_id"],
                                        "containers": 0, "expected_g": Decimal(0),
                                        "pending_g": Decimal(0), "unknown_containers": 0})
        slot["containers"] += 1
        slot["pending_g"] += row["pending_out_g"]
        if row["expected_g"] is None:
            slot["unknown_containers"] += 1
        else:
            slot["expected_g"] += row["expected_g"]
    holder_rows = [holders[k] for k in sorted(holders)]

    # 项目成本行：按实际耗用归属，未知质量不伪造成本
    cost_lines = []
    for u in facts.uses:
        if not u.confirmed_physical:
            continue
        cost_lines.append({
            "project": u.project,
            "cost_period": u.cost_period,
            "use_id": u.use_id,
            "pack_id": u.pack_id,
            "kind": u.kind,
            "grams": u.grams,
            "yuan_per_g": u.yuan_per_g,
            "yuan_exact": u.yuan_exact,
            "yuan": quant2(u.yuan_exact) if u.yuan_exact is not None else None,
            "handover_period": u.handover_period,
            "status": "确认" if u.grams is not None else "待核",
            "deps": sorted(u.deps),
        })

    # 账单：舍入到分，尾差归分摊基数最大项，并列按业务键升序
    bills = {}
    for line in cost_lines:
        bills.setdefault((line["project"], line["cost_period"]), []).append(line)
    bill_rows = []
    for (project, period), lines in sorted(bills.items()):
        known = [l for l in lines if l["yuan_exact"] is not None]
        if not known:
            # 全部行质量未知：账单留空，不把未知伪造成零成本
            bill_rows.append({
                "project": project, "cost_period": period, "lines": len(lines),
                "grams": None, "unknown_lines": len(lines),
                "yuan_exact": None, "yuan": None, "tail_to": "",
            })
            continue
        exact_total = sum((l["yuan_exact"] for l in known), Decimal(0))
        bill_total = quant2(exact_total)
        tail = bill_total - sum((l["yuan"] for l in known), Decimal(0))
        tail_to = ""
        if tail != 0:
            target = sorted(known, key=lambda l: (-l["grams"], l["use_id"]))[0]
            target["yuan"] += tail
            tail_to = target["use_id"]
        bill_rows.append({
            "project": project,
            "cost_period": period,
            "lines": len(lines),
            "grams": sum((l["grams"] for l in known), Decimal(0)),
            "unknown_lines": len(lines) - len(known),
            "yuan_exact": exact_total,
            "yuan": bill_total,
            "tail_to": tail_to,
        })

    # 核销交接行：确认资格 + 质量已知才可确认；pending 不自动核销
    writeoff = []
    for u in facts.uses:
        reasons = []
        if u.evidence == PENDING_EVIDENCE:
            reasons.append("处置申报未确证")
        elif not u.eligible:
            reasons.append("证据状态不具确认资格")
        if u.grams is None:
            reasons.append("无适用密度，质量未知")
        status = "确认" if (u.eligible and u.grams is not None) else "待核"
        writeoff.append({
            "handover_period": u.handover_period,
            "use_id": u.use_id,
            "project": u.project,
            "kind": u.kind,
            "evidence": u.evidence,
            "grams": u.grams,
            "yuan": quant2(u.yuan_exact) if u.yuan_exact is not None else None,
            "cost_period": u.cost_period,
            "status": status,
            "reason": "；".join(reasons),
            "deps": sorted(u.deps),
        })
    writeoff.sort(key=lambda r: (r["handover_period"], r["use_id"]))

    marts = {
        "container_ledger": ledger,
        "holder_balance": holder_rows,
        "project_cost_lines": cost_lines,
        "project_cost_bills": bill_rows,
        "writeoff_lines": writeoff,
        "pending_queue": [dict(p) for p in facts.pendings],
        "recon": build_recon(facts, ledger, cost_lines, writeoff),
    }
    return marts


def build_recon(facts, ledger, cost_lines, writeoff):
    """三方核对：质量守恒、计数守恒、成本↔核销同源，互不掩盖。"""
    moment = facts.cutoff
    mass = []
    for material in sorted(facts.materials):
        rows = [r for r in ledger if r["material_id"] == material]
        roots = [r for r in rows if r["container_kind"] == "root"]
        received = sum((r["start_g"] for r in roots if r["start_g"] is not None), Decimal(0))
        confirmed = sum((u.grams for u in facts.uses
                         if facts.containers[u.pack_id].material == material
                         and u.confirmed_physical and u.grams is not None), Decimal(0))
        pending = sum((r["pending_out_g"] for r in rows), Decimal(0))
        unknown = [r["pack_id"] for r in rows if r["book_g"] is None]
        measured_rows = [r for r in rows if r["measured_g"] is not None]
        measured = sum((r["measured_g"] for r in measured_rows), Decimal(0))
        residual = None
        if not unknown:
            residual = received - confirmed - pending - measured
        mass.append({
            "material_id": material,
            "received_g": received,
            "confirmed_out_g": confirmed,
            "pending_out_g": pending,
            "measured_g": measured,
            "residual_g": residual,
            "unknown_containers": unknown,
            "containers": [{
                "pack_id": r["pack_id"],
                "book_g": r["book_g"],
                "pending_g": r["pending_out_g"],
                "expected_g": r["expected_g"],
                "measured_g": r["measured_g"],
                "diff_g": r["diff_g"],
                "status": r["recon_status"],
            } for r in rows],
        })
    received_bottles = sum(facts.packs_count.values())
    count = {
        "received_bottles": received_bottles,
        "destroyed_bottles": len(facts.destroyed),
        "current_bottles": received_bottles - len(facts.destroyed),
        "empty_packages": sum(1 for c in facts.containers.values() if c.kind == "child"),
        "conserved": received_bottles == received_bottles - len(facts.destroyed) + len(facts.destroyed),
    }
    known_cost = [l for l in cost_lines if l["yuan_exact"] is not None]
    confirmed_writeoff = [w for w in writeoff if w["status"] == "确认"]
    bridge = [{
        "use_id": l["use_id"],
        "project": l["project"],
        "cost_period": l["cost_period"],
        "handover_period": l["handover_period"],
        "grams": l["grams"],
        "yuan": l["yuan"],
    } for l in known_cost]
    cost_writeoff = {
        "cost_total_yuan": sum((l["yuan_exact"] for l in known_cost), Decimal(0)),
        "writeoff_confirmed_yuan": sum((w["yuan"] for w in confirmed_writeoff), Decimal(0)),
        "unknown_cost_lines": [l["use_id"] for l in cost_lines if l["yuan_exact"] is None],
        "bridge": bridge,
    }
    holder_sum = {}
    for r in ledger:
        if r["expected_g"] is not None:
            holder_sum[r["material_id"]] = holder_sum.get(r["material_id"], Decimal(0)) + r["expected_g"]
    return {"mass": mass, "count": count, "cost_writeoff": cost_writeoff,
            "holder_expected_g": holder_sum}


MART_COLUMNS = {
    "container_ledger": ["pack_id", "material_id", "container_kind", "parent_pack", "spec_id",
                         "nominal_g", "holder", "start_g", "confirmed_out_g", "pending_out_g",
                         "book_g", "expected_g", "measured_g", "diff_g", "recon_status", "deps"],
    "holder_balance": ["holder", "material_id", "containers", "expected_g", "pending_g",
                       "unknown_containers"],
    "project_cost_lines": ["project", "cost_period", "use_id", "pack_id", "kind", "grams",
                           "yuan_per_g", "yuan_exact", "yuan", "handover_period", "status", "deps"],
    "project_cost_bills": ["project", "cost_period", "lines", "grams", "unknown_lines",
                           "yuan_exact", "yuan", "tail_to"],
    "writeoff_lines": ["handover_period", "use_id", "project", "kind", "evidence", "grams",
                       "yuan", "cost_period", "status", "reason", "deps"],
    "pending_queue": ["pending_id", "topic", "reason", "needed", "affects", "refs"],
}


def flatten(value):
    if isinstance(value, (list, tuple)):
        return ";".join(str(canon(v)) for v in value)
    return canon(value)


def write_marts(root, facts, marts):
    """把仓表写成可读 CSV，核对结果写成 JSON；同一次构建同一截点。"""
    out_dir = Path(root) / "warehouse" / "mart"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, columns in MART_COLUMNS.items():
        with (out_dir / f"{name}.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            for row in marts[name]:
                writer.writerow([flatten(row.get(c)) if row.get(c) is not None else "" for c in columns])
    recon = json.loads(json.dumps(marts["recon"], default=canon))
    (out_dir / "recon.json").write_text(
        json.dumps(recon, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    meta = {
        "cutoff": facts.cutoff.isoformat() if facts.cutoff else None,
        "inputs": facts.inputs,
        "tables": {name: len(marts[name]) for name in MART_COLUMNS},
    }
    (out_dir / "build_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out_dir
