"""事实层：同一知识截点下的容器台账、保管流转、双计量换算与称量核对。

库存余额、项目成本、核销交接都从本层取数，保证三方同源：
- 计数与质量分别守恒，互不掩盖；
- ml→g 只在物质、日期、温度均适用时换算，无适用密度即未知，不强行换算；
- 规格按封装日适用，不用当前规格解释旧包装；
- issue/return/transfer 只变保管方；use/evaporate/destroy 才是实物出账；
- pending 处置不计确认损失，也不计可用余量；
- 称量是独立核对，超差待核，不自动生成损耗配平。
"""
from dataclasses import dataclass, field
from decimal import Decimal
import datetime as dt

from .constants import TOLERANCE_G, STORE_HOLDER, ELIGIBLE, PENDING_EVIDENCE
from .landing import merged_rows, load_log
from .model import table_def, key_of, row_fingerprint

FAR = dt.datetime(9999, 12, 31, tzinfo=dt.timezone.utc)


def ref(table, key):
    return f"{table}:{key}"


@dataclass
class Out:
    """容器的一次实物出账（分装出、消耗、挥发、销毁）。"""
    at: dt.datetime
    grams: object  # Decimal 或 None（未知）
    confirmed: bool  # 非 pending 的实物出账
    kind: str  # decant / use / evaporate / destroy
    ref: str
    deps: tuple


@dataclass
class Container:
    pack_id: str
    material: str
    kind: str  # root / child
    parent: object
    born_at: dt.datetime
    start_g: object  # Decimal 或 None（未知）
    deps: tuple
    origin_ref: str = ""  # 容器自身的来源行：根为 packs:键，子管为 decants:键
    spec_id: object = None
    nominal_g: object = None
    holders: list = field(default_factory=list)  # [(时刻, 保管方)]
    outs: list = field(default_factory=list)

    def holder_at(self, moment):
        current = None
        for at, holder in sorted(self.holders, key=lambda h: h[0]):
            if at <= moment:
                current = holder
            else:
                break
        return current

    def book_at(self, moment):
        """确认实物出账后的账面余量；任一环节未知则整体未知。"""
        if self.start_g is None:
            return None
        total = self.start_g
        for out in sorted(self.outs, key=lambda o: o.at):
            if out.at > moment:
                break
            if not out.confirmed:
                continue
            if out.grams is None:
                return None
            total -= out.grams
        return total

    def pending_at(self, moment):
        """申报未确证的实物量，单列，不进账面也不进可用余量。"""
        total = Decimal(0)
        for out in self.outs:
            if out.at <= moment and not out.confirmed and out.grams is not None:
                total += out.grams
        return total

    def expected_at(self, moment):
        """若 pending 申报在物理上成立，称量应对上的量。"""
        book = self.book_at(moment)
        if book is None:
            return None
        return book - self.pending_at(moment)


@dataclass
class UseFact:
    use_id: str
    pack_id: str
    project: str
    kind: str
    evidence: str
    at: dt.datetime
    handover_period: str
    quantity: object
    unit: str
    grams: object
    deps: tuple
    eligible: bool  # 核销确认资格
    confirmed_physical: bool  # 实物上已出账
    cost_period: str  # 实际消耗时点决定项目成本期
    yuan_per_g: object
    yuan_exact: object


@dataclass
class WeighFact:
    weigh_id: str
    pack_id: str
    at: dt.datetime
    remaining_g: object
    expected_g: object
    diff_g: object
    status: str  # ok / over / unknown
    deps: tuple


class Facts:
    cutoff = None
    materials = None
    containers = None
    uses = None
    weighings = None
    densities = None
    pendings = None
    inputs = None  # "表:键" -> 行指纹
    packs_count = None  # 根包装实收瓶数
    destroyed = None  # 已明确销毁的容器


def build_facts(root, cutoff=None):
    """在统一知识截点上构建全部事实；截点之后才知道的资料不参与。"""
    tables, catalog = merged_rows(root, cutoff)
    if cutoff is None:
        stamps = [dt.datetime.fromisoformat(e["received_at"]) for e in load_log(root)]
        cutoff = max(stamps) if stamps else None
    facts = Facts()
    facts.cutoff = cutoff
    facts.pendings = []
    facts.inputs = {}
    for name, rows in tables.items():
        tdef = table_def(catalog, name)
        for row in rows:
            facts.inputs[ref(name, key_of(tdef, row))] = row_fingerprint(tdef, row)

    def add_pending(pid, topic, reason, needed, affects, refs):
        facts.pendings.append({
            "pending_id": pid,
            "topic": topic,
            "reason": reason,
            "needed": needed,
            "affects": sorted(set(affects)),
            "refs": sorted(set(refs)),
        })

    facts.materials = {r["material_id"]: r for r in tables.get("materials", [])}
    specs = list(tables.get("specifications", []))

    # 密度：整行补正按知识时刻生效，原始值保留备查
    corrections = {}
    for c in tables.get("density_corrections", []):
        corrections.setdefault(c["density_id"], []).append(c)
    densities = []
    for d in tables.get("density", []):
        deps = [ref("density", d["density_id"])]
        g_per_ml = d["g_per_ml"]
        applied = None
        for c in sorted(corrections.get(d["density_id"], []),
                        key=lambda c: (c["received_at"], c["correction_id"])):
            applied = c  # 同一密度行的多次补正，取知识时刻最新者
        if applied is not None:
            g_per_ml = applied["g_per_ml"]
            deps.append(ref("density_corrections", applied["correction_id"]))
        densities.append({
            **d,
            "g_per_ml": g_per_ml,
            "base_g_per_ml": d["g_per_ml"],
            "corrected": applied is not None,
            "deps": deps,
        })
    facts.densities = densities

    def to_grams(quantity, unit, material, at, temperature):
        """ml→g 只在物质、日期、温度均适用时换算；无适用密度即未知。"""
        if unit == "g":
            return quantity, []
        if unit == "ml":
            for d in densities:
                if (d["material_id"] == material
                        and d["start"] <= at.date() < d["end"]
                        and d["temperature_c"] == temperature):
                    return quantity * d["g_per_ml"], list(d["deps"])
            return None, []
        raise ValueError(f"未知单位：{unit}")

    containers = {}
    facts.packs_count = {}

    # 根包装：规格按封装日适用，实收净克是付款事实，不随后续补正改变
    for p in tables.get("packs", []):
        spec = next((s for s in specs
                     if s["material_id"] == p["material_id"]
                     and s["start"] <= p["packaged_at"] < s["end"]), None)
        deps = [ref("packs", p["pack_id"]), ref("materials", p["material_id"])]
        if spec is not None:
            deps.append(ref("specifications", spec["spec_id"]))
        container = Container(
            pack_id=p["pack_id"],
            material=p["material_id"],
            kind="root",
            parent=None,
            born_at=p["received_at"],
            start_g=p["net_g"],
            deps=tuple(deps),
            origin_ref=ref("packs", p["pack_id"]),
            spec_id=spec["spec_id"] if spec else None,
            nominal_g=spec["nominal_g"] if spec else None,
            holders=[(p["received_at"], STORE_HOLDER)],
        )
        containers[p["pack_id"]] = container
        facts.packs_count[p["pack_id"]] = p["count"]
        if spec is None:
            add_pending(
                f"PEND-{p['pack_id']}-SPEC", "包装规格缺失",
                f"{p['material_id']} 在封装日 {p['packaged_at']} 无适用规格",
                "补历史包装规格", [f"容器{p['pack_id']}规格"], deps)

    # 分装与保管转移按业务时点交错处理：子管在分装时刻继承父管保管方，
    # 新管是空包装，不是新入区材料；转移只变保管方，不减全区实物总量。
    events = []
    for dc in tables.get("decants", []):
        events.append((dc["at"], 0, dc["decant_id"], dc))
    for mv in tables.get("movements", []):
        events.append((mv["at"], 1, mv["move_id"], mv))
    for at, kind, _, row in sorted(events, key=lambda e: (e[0], e[1], e[2])):
        if kind == 0:
            dc = row
            parent = containers.get(dc["parent_pack"])
            if parent is None:
                add_pending(f"PEND-{dc['decant_id']}", "分装父容器缺失",
                            f"父容器 {dc['parent_pack']} 不在已收资料中",
                            "补父包装资料", [f"分装{dc['decant_id']}"],
                            [ref("decants", dc["decant_id"])])
                continue
            if dc["child_pack"] in containers:
                add_pending(f"PEND-{dc['decant_id']}", "分装子容器重复",
                            f"子容器 {dc['child_pack']} 已存在",
                            "核对分装记录", [f"分装{dc['decant_id']}"],
                            [ref("decants", dc["decant_id"])])
                continue
            grams, ddeps = to_grams(dc["quantity"], dc["unit"], parent.material,
                                    dc["at"], dc["temperature_c"])
            deps = [ref("decants", dc["decant_id"]), parent.origin_ref] + ddeps
            if grams is not None:
                book = parent.book_at(dc["at"])
                if book is not None and book - grams < 0:
                    add_pending(
                        f"PEND-{dc['decant_id']}-OVER", "超耗",
                        f"{parent.pack_id} 账面 {book} g 不足分装 {grams} g",
                        "核对分装记录", [f"容器{parent.pack_id}余量"], deps)
            child = Container(
                pack_id=dc["child_pack"],
                material=parent.material,
                kind="child",
                parent=parent.pack_id,
                born_at=dc["at"],
                start_g=grams,
                deps=tuple(deps),
                origin_ref=ref("decants", dc["decant_id"]),
                holders=[(dc["at"], parent.holder_at(dc["at"]))],
            )
            parent.outs.append(Out(dc["at"], grams, True, "decant",
                                   ref("decants", dc["decant_id"]), tuple(deps)))
            containers[child.pack_id] = child
            if grams is None:
                add_pending(
                    f"PEND-{dc['decant_id']}", "分装规格不明",
                    f"{parent.material} 在 {dc['at'].date()}、{dc['temperature_c']}°C 无适用密度，"
                    f"{dc['quantity']} ml 不能折算为克",
                    "补适用密度资料",
                    [f"容器{parent.pack_id}余量", f"容器{child.pack_id}期初",
                     f"{parent.material}质量守恒"], deps)
        else:
            mv = row
            container = containers.get(mv["pack_id"])
            if container is None:
                add_pending(f"PEND-{mv['move_id']}", "动作容器缺失",
                            f"容器 {mv['pack_id']} 不在已收资料中",
                            "补包装或分装资料", [f"动作{mv['move_id']}"],
                            [ref("movements", mv["move_id"])])
                continue
            current = container.holder_at(mv["at"])
            if current != mv["from_holder"]:
                add_pending(
                    f"PEND-{mv['move_id']}", "保管方矛盾",
                    f"{mv['pack_id']} 动作前保管方为 {current}，来源栏为 {mv['from_holder']}",
                    "核对保管记录", [f"容器{mv['pack_id']}保管链"],
                    [ref("movements", mv["move_id"])])
                continue
            container.holders.append((mv["at"], mv["to_holder"]))

    # 消耗及处置：use 是实际实验消耗，evaporate/destroy 单列；pending 只申报不出账
    uses = []
    for u in sorted(tables.get("uses", []), key=lambda r: (r["at"], r["use_id"])):
        container = containers.get(u["pack_id"])
        if container is None:
            add_pending(f"PEND-{u['use_id']}", "去向容器缺失",
                        f"容器 {u['pack_id']} 不在已收资料中",
                        "补包装或分装资料", [f"去向{u['use_id']}"],
                        [ref("uses", u["use_id"])])
            continue
        grams, ddeps = to_grams(u["quantity"], u["unit"], container.material,
                                u["at"], u["temperature_c"])
        deps = ([ref("uses", u["use_id"]), container.origin_ref,
                 ref("materials", container.material)] + ddeps)
        holder = container.holder_at(u["at"])
        if holder != u["project"]:
            add_pending(
                f"PEND-{u['use_id']}-HOLDER", "保管方与项目不符",
                f"{container.pack_id} 在 {u['at']} 的保管方为 {holder}，去向项目为 {u['project']}",
                "核对领用与消耗记录",
                [f"项目{u['project']}成本", f"容器{container.pack_id}保管链"], deps)
        confirmed_physical = u["evidence"] != PENDING_EVIDENCE
        if confirmed_physical and grams is not None:
            book = container.book_at(u["at"])
            if book is not None and book - grams < 0:
                add_pending(
                    f"PEND-{u['use_id']}-OVER", "超耗",
                    f"{container.pack_id} 账面 {book} g 不足出账 {grams} g",
                    "核对消耗与分装记录", [f"容器{container.pack_id}余量"], deps)
        container.outs.append(Out(u["at"], grams, confirmed_physical, u["kind"],
                                  ref("uses", u["use_id"]), tuple(deps)))
        price = facts.materials[container.material]["yuan_per_g"] \
            if container.material in facts.materials else None
        uses.append(UseFact(
            use_id=u["use_id"],
            pack_id=container.pack_id,
            project=u["project"],
            kind=u["kind"],
            evidence=u["evidence"],
            at=u["at"],
            handover_period=u["handover_period"],
            quantity=u["quantity"],
            unit=u["unit"],
            grams=grams,
            deps=tuple(deps),
            eligible=(u["kind"], u["evidence"]) in ELIGIBLE,
            confirmed_physical=confirmed_physical,
            cost_period=f"{u['at'].year:04d}-{u['at'].month:02d}",
            yuan_per_g=price,
            yuan_exact=(grams * price) if (grams is not None and price is not None) else None,
        ))
        if u["evidence"] == PENDING_EVIDENCE:
            add_pending(
                f"PEND-{u['use_id']}", "处置申报未确证",
                f"{container.pack_id} 申报 {u['kind']} {u['quantity']} {u['unit']}，"
                f"证据状态 pending，不具确认资格；该量既不计确认损耗也不计可用余量",
                "补见证或确证资料",
                [f"容器{container.pack_id}余量", f"项目{u['project']}成本",
                 f"核销期{u['handover_period']}"], deps)
        if grams is None:
            add_pending(
                f"PEND-{u['use_id']}-G", "消耗质量未知",
                f"{container.material} 无适用密度，{u['quantity']} ml 不能折算为克，"
                f"成本与核销金额未定",
                "补适用密度资料",
                [f"项目{u['project']}成本", f"核销期{u['handover_period']}",
                 f"{container.material}质量守恒"], deps)
    facts.uses = uses

    # 称量：独立核对，不是配平；超容差待核，不自动生成损耗
    weighings = []
    for w in sorted(tables.get("weighings", []), key=lambda r: (r["at"], r["weigh_id"])):
        container = containers.get(w["pack_id"])
        if container is None:
            add_pending(f"PEND-{w['weigh_id']}", "称量容器缺失",
                        f"容器 {w['pack_id']} 不在已收资料中",
                        "补包装或分装资料", [f"称量{w['weigh_id']}"],
                        [ref("weighings", w["weigh_id"])])
            continue
        expected = container.expected_at(w["at"])
        deps = [ref("weighings", w["weigh_id"]), container.origin_ref]
        if expected is None:
            status, diff = "unknown", None
        else:
            diff = w["remaining_g"] - expected
            status = "over" if abs(diff) > TOLERANCE_G else "ok"
        weighings.append(WeighFact(
            weigh_id=w["weigh_id"], pack_id=container.pack_id, at=w["at"],
            remaining_g=w["remaining_g"], expected_g=expected, diff_g=diff,
            status=status, deps=tuple(deps)))
        if status == "over":
            add_pending(
                f"PEND-{w['weigh_id']}", "称量超差",
                f"{container.pack_id} 实测 {w['remaining_g']} g，应有 {expected} g，"
                f"差 {diff} g，超容差 {TOLERANCE_G} g",
                "复核称量或补处置凭证", [f"容器{container.pack_id}余量"], deps)
    facts.weighings = weighings

    # 容器余量未知：单列待核，不用别项对上掩盖
    moment = cutoff if cutoff is not None else FAR
    for pack_id in sorted(containers):
        container = containers[pack_id]
        if container.book_at(moment) is None:
            add_pending(
                f"PEND-{pack_id}-REM", "容器余量未知",
                f"{container.material} 存在未知环节，{pack_id} 账面余量不能确定",
                "补适用密度或规格资料",
                [f"容器{pack_id}余量", f"{container.material}质量守恒"],
                list(container.deps))

    # 明确销毁：仅当有资格的销毁出账覆盖当时全部账面余量时，容器计数才减少
    facts.destroyed = []
    for pack_id in sorted(containers):
        container = containers[pack_id]
        book = container.start_g
        for out in sorted(container.outs, key=lambda o: o.at):
            if not out.confirmed:
                continue
            if out.grams is None or book is None:
                book = None
                continue
            if out.kind == "destroy" and out.grams == book:
                facts.destroyed.append(pack_id)
                break
            book -= out.grams
    facts.containers = containers
    return facts


def container_deps(container, weighings):
    """容器台账行的全部来源依赖，用于差异分类。"""
    deps = set(container.deps)
    for out in container.outs:
        deps.update(out.deps)
    for w in weighings:
        if w.pack_id == container.pack_id:
            deps.update(w.deps)
    return sorted(deps)
