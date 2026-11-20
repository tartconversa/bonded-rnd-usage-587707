"""交接层：月度核销交接的生成、确认、版本保存与差异比较。

交接内容确认后可回看，后来的修订另行比较；差异按来源分类，
分清新增实物动作与历史资料补正各改了哪些项目、哪些期间。
"""
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from .constants import EVENT_TABLES, CORRECTION_TABLES, COST_TABLES
from .facts import build_facts
from .marts import build_marts
from .model import canon

SECTION_KEYS = {
    "confirmed_lines": "use_id",
    "pending_lines": "use_id",
    "mass_discrepancies": "pack_id",
    "open_pendings": "pending_id",
}

CAUSE_LABELS = (
    (CORRECTION_TABLES, "历史资料补正"),
    (EVENT_TABLES, "新增实物动作"),
    (COST_TABLES, "成本资料变化"),
)


def handover_dir(root, period):
    return Path(root) / "warehouse" / "handover" / period


def index_path(root, period):
    return handover_dir(root, period) / "index.json"


def load_index(root, period):
    path = index_path(root, period)
    if not path.exists():
        return {"period": period, "versions": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_index(root, index):
    path = index_path(root, index["period"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_handover_doc(root, period, cutoff=None):
    """生成一个交接期的交接内容；确认行与未决部分都保留，不为月末总数删待核。"""
    facts = build_facts(root, cutoff)
    marts = build_marts(facts)
    confirmed, pending_lines = [], []
    for line in marts["writeoff_lines"]:
        if line["handover_period"] != period:
            continue
        row = {k: v for k, v in line.items() if k != "handover_period"}
        (confirmed if line["status"] == "确认" else pending_lines).append(row)
    discrepancies = []
    for row in marts["container_ledger"]:
        if row["recon_status"] != "ok" or row["pending_out_g"] > 0 or row["book_g"] is None:
            discrepancies.append({
                "pack_id": row["pack_id"],
                "material_id": row["material_id"],
                "holder": row["holder"],
                "book_g": row["book_g"],
                "pending_out_g": row["pending_out_g"],
                "expected_g": row["expected_g"],
                "measured_g": row["measured_g"],
                "diff_g": row["diff_g"],
                "recon_status": row["recon_status"],
                "deps": row["deps"],
            })
    by_project = {}
    for line in confirmed:
        slot = by_project.setdefault(line["project"], {"grams": Decimal(0), "yuan": Decimal(0)})
        slot["grams"] += line["grams"]
        slot["yuan"] += line["yuan"]
    content = {
        "confirmed_lines": confirmed,
        "pending_lines": pending_lines,
        "mass_discrepancies": discrepancies,
        "open_pendings": marts["pending_queue"],
        "totals": {
            "grams": sum((l["grams"] for l in confirmed), Decimal(0)),
            "yuan": sum((l["yuan"] for l in confirmed), Decimal(0)),
            "by_project": by_project,
        },
    }
    content_hash = hashlib.sha256(
        json.dumps(content, ensure_ascii=False, sort_keys=True, default=canon).encode("utf-8")
    ).hexdigest()
    return {
        "period": period,
        "cutoff": facts.cutoff.isoformat() if facts.cutoff else None,
        "content": json.loads(json.dumps(content, default=canon)),
        "inputs": facts.inputs,
        "content_hash": content_hash,
    }


def store_handover(root, doc, confirm=False):
    """内容相同不新增版本；确认只翻转标记，不改已存内容。"""
    index = load_index(root, doc["period"])
    versions = index["versions"]
    if versions and versions[-1]["hash"] == doc["content_hash"]:
        entry = versions[-1]
        if confirm and not entry["confirmed"]:
            entry["confirmed"] = True
            save_index(root, index)
        return entry["version"], False
    version = f"v{len(versions) + 1}"
    folder = handover_dir(root, doc["period"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{version}.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2, default=canon) + "\n", encoding="utf-8")
    from .report import render_handover_md
    (folder / f"{version}.md").write_text(render_handover_md(doc, version), encoding="utf-8")
    entry = {"version": version, "hash": doc["content_hash"],
             "confirmed": bool(confirm), "cutoff": doc["cutoff"]}
    versions.append(entry)
    save_index(root, index)
    return version, True


def load_handover(root, period, version):
    path = handover_dir(root, period) / f"{version}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def classify(ref_name):
    table = ref_name.split(":", 1)[0]
    for tables, label in CAUSE_LABELS:
        if table in tables:
            return label
    return "其他资料"


def diff_handovers(root, period, version_a, version_b):
    """比较两个版本：输入差集 → 行级差异 → 按来源分类到项目与期间。"""
    doc_a = load_handover(root, period, version_a)
    doc_b = load_handover(root, period, version_b)
    inputs_a, inputs_b = doc_a["inputs"], doc_b["inputs"]
    delta_refs = sorted(k for k in set(inputs_a) | set(inputs_b)
                        if inputs_a.get(k) != inputs_b.get(k))
    input_changes = [{
        "ref": k,
        "cause": classify(k),
        "change": ("新增" if k not in inputs_a else
                   "移除" if k not in inputs_b else "修订"),
    } for k in delta_refs]
    delta_set = set(delta_refs)

    rows_changed = []
    for section, key_field in SECTION_KEYS.items():
        rows_a = {r[key_field]: r for r in doc_a["content"][section]}
        rows_b = {r[key_field]: r for r in doc_b["content"][section]}
        for key in sorted(set(rows_a) | set(rows_b)):
            old, new = rows_a.get(key), rows_b.get(key)
            if old == new:
                continue
            deps = set((new or old).get("deps", [])) | set((new or old).get("refs", []))
            causes = sorted({classify(d) for d in deps & delta_set}) or ["级联影响"]
            rows_changed.append({
                "section": section,
                "key": key,
                "change": ("新增" if old is None else "移除" if new is None else "变更"),
                "causes": causes,
                "project": (new or old).get("project"),
                "before": old,
                "after": new,
            })
    totals_changed = doc_a["content"]["totals"] != doc_b["content"]["totals"]
    summary = {}
    for row in rows_changed:
        for cause in row["causes"]:
            slot = summary.setdefault(cause, {"projects": set(), "rows": 0})
            slot["rows"] += 1
            if row["project"]:
                slot["projects"].add(row["project"])
    summary = {cause: {"projects": sorted(v["projects"]), "rows": v["rows"]}
               for cause, v in sorted(summary.items())}
    return {
        "period": period,
        "from": version_a,
        "to": version_b,
        "cutoff_from": doc_a["cutoff"],
        "cutoff_to": doc_b["cutoff"],
        "input_changes": input_changes,
        "rows_changed": rows_changed,
        "totals_before": doc_a["content"]["totals"],
        "totals_after": doc_b["content"]["totals"],
        "totals_changed": totals_changed,
        "summary": summary,
    }
