"""耗用仓业务测试：双计量、知识截点、待核、交接与三方核对。"""
from decimal import Decimal
from pathlib import Path
import datetime as dt
import shutil
import tempfile
import unittest

from consume.constants import TOLERANCE_G
from consume.facts import build_facts
from consume.handover import (build_handover_doc, store_handover, diff_handovers,
                              load_index)
from consume.landing import receive_file, receive_dir, load_log
from consume.marts import build_marts
from consume.__main__ import main as cli

REPO = Path(__file__).resolve().parents[1]
AT = dt.datetime.fromisoformat
BASE_AT = AT("2026-10-27T18:00:00+08:00")   # 基础资料到手（10 月业务已发生完）
CORR_AT = AT("2026-10-28T08:00:00+08:00")   # 密度补正到手
PRE = AT("2026-10-27T23:59:59+08:00")       # 截点：补正尚不可知
POST = AT("2026-10-31T23:59:59+08:00")      # 截点：月末，补正可知
NOV1 = AT("2026-11-01T09:00:00+08:00")
NOV4 = AT("2026-11-04T09:00:00+08:00")


class WarehouseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copytree(REPO / "data", self.root / "data")
        receive_dir(self.root, self.root / "data", BASE_AT, skip=("density_corrections",))
        receive_file(self.root, "density_corrections",
                     self.root / "data" / "density_corrections.csv", CORR_AT)

    def facts(self, cutoff):
        return build_facts(self.root, cutoff)

    def marts(self, cutoff):
        return build_marts(self.facts(cutoff))

    def ledger(self, cutoff):
        return {r["pack_id"]: r for r in self.marts(cutoff)["container_ledger"]}


class ConversionTests(WarehouseCase):
    def test_spec_applies_by_packaged_at(self):
        """规格按封装日适用：P1 用旧规格 SP1，不用当前规格解释旧包装。"""
        facts = self.facts(POST)
        self.assertEqual(facts.containers["P1"].spec_id, "SP1")
        self.assertEqual(facts.containers["P1"].nominal_g, Decimal("100"))
        self.assertEqual(facts.containers["P2"].spec_id, "SP2")

    def test_density_requires_material_date_temperature(self):
        """无适用密度即未知：M2 没有密度行，不能拿 M1 的密度换算。"""
        facts = self.facts(POST)
        dc3 = [o for o in facts.containers["P3"].outs if o.kind == "decant"][0]
        self.assertIsNone(dc3.grams)
        u5 = next(u for u in facts.uses if u.use_id == "U5")
        self.assertIsNone(u5.grams)
        self.assertIsNone(u5.yuan_exact)  # 未知质量不能伪造成本

    def test_correction_respects_knowledge_time(self):
        """补正按知识时刻生效：截点前 1.08，截点后 1.06。"""
        u2_pre = next(u for u in self.facts(PRE).uses if u.use_id == "U2")
        u2_post = next(u for u in self.facts(POST).uses if u.use_id == "U2")
        self.assertEqual(u2_pre.grams, Decimal("10.80"))
        self.assertEqual(u2_post.grams, Decimal("10.60"))
        self.assertIn("density_corrections:DX1", u2_post.deps)
        self.assertNotIn("density_corrections:DX1", u2_pre.deps)


class ContainerTests(WarehouseCase):
    def test_book_after_confirmed_events(self):
        rows = self.ledger(POST)
        self.assertEqual(rows["P1"]["book_g"], Decimal("30"))      # 100-20-30-20
        self.assertEqual(rows["P1A"]["book_g"], Decimal("19.4"))   # 30-10.6
        self.assertEqual(rows["P1B"]["book_g"], Decimal("15"))     # 20-5
        self.assertEqual(rows["P2"]["book_g"], Decimal("119.9"))   # 120-0.1

    def test_pending_destroy_not_double_counted(self):
        """pending 处置既不计确认损失也不计可用余量。"""
        row = self.ledger(POST)["P2"]
        self.assertEqual(row["pending_out_g"], Decimal("10"))
        self.assertEqual(row["expected_g"], Decimal("109.9"))
        self.assertEqual(row["measured_g"], Decimal("109.9"))
        self.assertEqual(row["diff_g"], Decimal("0"))

    def test_weighing_tolerance_boundary(self):
        """容差 0.2 g：补正后 P1A 差 -0.2 g，未超差，不自动生成损耗。"""
        row = self.ledger(POST)["P1A"]
        self.assertEqual(row["diff_g"], Decimal("-0.2"))
        self.assertEqual(abs(row["diff_g"]) <= TOLERANCE_G, True)
        self.assertEqual(row["recon_status"], "ok")

    def test_issue_and_transfer_do_not_change_zone_total(self):
        """领用/转交只变保管方：全区实物量不变，在手余额随保管方移动。"""
        marts = self.marts(POST)
        holders = {(h["holder"], h["material_id"]): h for h in marts["holder_balance"]}
        self.assertEqual(holders[("store", "M1")]["expected_g"], Decimal("30"))
        self.assertEqual(holders[("A", "M1")]["expected_g"], Decimal("19.4"))
        self.assertEqual(holders[("B", "M1")]["expected_g"], Decimal("124.9"))
        self.assertEqual(holders[("B", "M1")]["pending_g"], Decimal("10"))
        zone = sum(h["expected_g"] for h in marts["holder_balance"])
        self.assertEqual(zone, Decimal("174.3"))  # 220 - 35.7，与是否领用无关

    def test_child_inherits_holder_and_cost_basis(self):
        facts = self.facts(POST)
        self.assertEqual(facts.containers["P1A"].holder_at(AT("2026-10-14T09:00:00+08:00")), "A")
        self.assertEqual(facts.containers["P1B"].holder_at(POST), "B")  # MV2 转交后
        u2 = next(u for u in facts.uses if u.use_id == "U2")
        self.assertEqual(u2.yuan_per_g, Decimal("4"))  # 与父管同物质同单价


class CostTests(WarehouseCase):
    def cost_lines(self, cutoff):
        return {l["use_id"]: l for l in self.marts(cutoff)["project_cost_lines"]}

    def test_project_cost_by_actual_consumption(self):
        lines = self.cost_lines(POST)
        self.assertEqual(lines["U1"]["yuan"], Decimal("80.00"))
        self.assertEqual(lines["U2"]["yuan"], Decimal("42.40"))
        self.assertEqual(lines["U3"]["yuan"], Decimal("20.00"))
        self.assertEqual(lines["U4"]["yuan"], Decimal("0.40"))
        self.assertNotIn("U6", lines)  # pending 不进成本

    def test_correction_changes_cost_not_counts(self):
        """补正改变可折算量与项目分摊，但不改已领出瓶数与实收事实。"""
        pre, post = self.cost_lines(PRE), self.cost_lines(POST)
        self.assertEqual(pre["U2"]["yuan"], Decimal("43.20"))
        self.assertEqual(post["U2"]["yuan"], Decimal("42.40"))
        count_pre = self.marts(PRE)["recon"]["count"]
        count_post = self.marts(POST)["recon"]["count"]
        self.assertEqual(count_pre, count_post)
        mass_pre = next(m for m in self.marts(PRE)["recon"]["mass"] if m["material_id"] == "M1")
        mass_post = next(m for m in self.marts(POST)["recon"]["mass"] if m["material_id"] == "M1")
        self.assertEqual(mass_pre["received_g"], mass_post["received_g"])  # 付款事实不动

    def test_cost_period_is_actual_month_not_handover_period(self):
        """跨月实验：U3 成本期 2026-10，核销期 2026-11。"""
        lines = self.cost_lines(POST)
        self.assertEqual(lines["U3"]["cost_period"], "2026-10")
        self.assertEqual(lines["U3"]["handover_period"], "2026-11")

    def test_unknown_mass_stays_pending(self):
        lines = self.cost_lines(POST)
        self.assertEqual(lines["U5"]["status"], "待核")
        self.assertIsNone(lines["U5"]["yuan"])


class RoundingTests(unittest.TestCase):
    """结算舍入到分：尾差归分摊基数最大项，并列按业务键升序。"""

    def test_tail_goes_to_largest_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(REPO / "data", root / "data")
            (root / "data" / "materials.csv").write_text(
                "material_id,yuan_per_g\nM9,0.335\n", encoding="utf-8")
            (root / "data" / "packs.csv").write_text(
                "pack_id,material_id,packaged_at,received_at,count,net_g\n"
                "P9,M9,2026-10-01,2026-10-01T08:00:00+08:00,1,9\n", encoding="utf-8")
            uses = ["use_id,pack_id,project,kind,quantity,unit,temperature_c,at,evidence,handover_period"]
            for i in ("1", "2", "3"):
                uses.append(f"UX{i},P9,X,use,1,g,20,2026-10-0{i}T08:00:00+08:00,confirmed,2026-10")
            (root / "data" / "uses.csv").write_text("\n".join(uses) + "\n", encoding="utf-8")
            for table, name in (("materials", "materials.csv"), ("packs", "packs.csv"),
                                ("uses", "uses.csv")):
                receive_file(root, table, root / "data" / name, BASE_AT)
            marts = build_marts(build_facts(root, POST))
            bill = marts["project_cost_bills"][0]
            # 每行 0.335 → 0.34；账单 1.005 → 1.01；尾差 -0.01 归基数并列最小键 UX1
            self.assertEqual(bill["yuan"], Decimal("1.01"))
            self.assertEqual(bill["tail_to"], "UX1")
            lines = {l["use_id"]: l for l in marts["project_cost_lines"]}
            self.assertEqual(lines["UX1"]["yuan"], Decimal("0.33"))
            self.assertEqual(lines["UX2"]["yuan"], Decimal("0.34"))


class WriteoffTests(WarehouseCase):
    def writeoff(self, cutoff, period):
        return {l["use_id"]: l for l in self.marts(cutoff)["writeoff_lines"]
                if l["handover_period"] == period}

    def test_eligibility_by_kind_and_evidence(self):
        lines = self.writeoff(POST, "2026-10")
        self.assertEqual(lines["U1"]["status"], "确认")   # confirmed use
        self.assertEqual(lines["U4"]["status"], "确认")   # documented evaporate
        self.assertEqual(lines["U6"]["status"], "待核")   # pending 不自动核销
        self.assertIn("未确证", lines["U6"]["reason"])

    def test_confirmed_use_with_unknown_mass_is_pending(self):
        lines = self.writeoff(POST, "2026-10")
        self.assertEqual(lines["U5"]["status"], "待核")
        self.assertIn("质量未知", lines["U5"]["reason"])

    def test_cross_month_line_lands_in_handover_period(self):
        self.assertIn("U3", self.writeoff(POST, "2026-11"))
        self.assertNotIn("U3", self.writeoff(POST, "2026-10"))


class ReconTests(WarehouseCase):
    def test_mass_conservation_with_pending_and_tolerance(self):
        """M1：220 = 35.7 确认 + 10 待核 + 174.1 实测 + 0.2 容差内余差。"""
        recon = self.marts(POST)["recon"]
        m1 = next(m for m in recon["mass"] if m["material_id"] == "M1")
        self.assertEqual(m1["received_g"], Decimal("220"))
        self.assertEqual(m1["confirmed_out_g"], Decimal("35.70"))
        self.assertEqual(m1["pending_out_g"], Decimal("10"))
        self.assertEqual(m1["measured_g"], Decimal("174.1"))
        self.assertEqual(m1["residual_g"], Decimal("0.20"))
        self.assertLessEqual(abs(m1["residual_g"]), TOLERANCE_G * 4)  # 各容器差均在容差内

    def test_unknown_mass_not_hidden_by_other_items(self):
        """M2 质量未知单列，不用别项对上掩盖。"""
        recon = self.marts(POST)["recon"]
        m2 = next(m for m in recon["mass"] if m["material_id"] == "M2")
        self.assertIsNone(m2["residual_g"])
        self.assertEqual(sorted(m2["unknown_containers"]), ["P3", "P3A"])

    def test_count_conservation_and_empty_packages(self):
        count = self.marts(POST)["recon"]["count"]
        self.assertEqual(count["received_bottles"], 3)
        self.assertEqual(count["destroyed_bottles"], 0)  # U6 未确证，瓶数不减
        self.assertEqual(count["current_bottles"], 3)
        self.assertEqual(count["empty_packages"], 3)  # 分装不增加进口数量

    def test_cost_writeoff_same_source(self):
        cw = self.marts(POST)["recon"]["cost_writeoff"]
        self.assertEqual(cw["cost_total_yuan"], Decimal("142.80"))
        self.assertEqual(cw["writeoff_confirmed_yuan"], Decimal("142.80"))
        self.assertEqual(cw["unknown_cost_lines"], ["U5"])
        bridge = {b["use_id"] for b in cw["bridge"]}
        self.assertEqual(bridge, {"U1", "U2", "U3", "U4"})


class LandingTests(WarehouseCase):
    def test_same_digest_adds_no_facts(self):
        before = len(load_log(self.root))
        result = receive_file(self.root, "uses", self.root / "data" / "uses.csv", CORR_AT)
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(len(load_log(self.root)), before)

    def test_whole_row_revision_by_key(self):
        revised = self.root / "uses_v2.csv"
        text = (self.root / "data" / "uses.csv").read_text(encoding="utf-8")
        revised.write_text(text.replace("U3,P1B,B,use,5,g", "U3,P1B,B,use,6,g"),
                           encoding="utf-8")
        receive_file(self.root, "uses", revised, CORR_AT)
        u3 = next(u for u in build_facts(self.root, POST).uses if u.use_id == "U3")
        self.assertEqual(u3.grams, Decimal("6"))
        # 原始文件与接收历史都保留
        landing = list((self.root / "warehouse" / "landing").glob("*.csv"))
        self.assertEqual(len(landing), len(load_log(self.root)))


class HandoverTests(WarehouseCase):
    def test_correction_revision_flow(self):
        """先确认旧版，补正后另行比较：v1 可回看，v2 差异分类为历史资料补正。"""
        doc1 = build_handover_doc(self.root, "2026-10", PRE)
        v1, created1 = store_handover(self.root, doc1, confirm=True)
        self.assertTrue(created1)
        doc2 = build_handover_doc(self.root, "2026-10", POST)
        v2, created2 = store_handover(self.root, doc2, confirm=True)
        self.assertTrue(created2)
        self.assertEqual((v1, v2), ("v1", "v2"))
        diff = diff_handovers(self.root, "2026-10", "v1", "v2")
        self.assertEqual({c["ref"] for c in diff["input_changes"]},
                         {"density_corrections:DX1"})
        changed = {(r["section"], r["key"]): r for r in diff["rows_changed"]}
        u2 = changed[("confirmed_lines", "U2")]
        self.assertEqual(u2["causes"], ["历史资料补正"])
        self.assertEqual(u2["before"]["grams"], "10.80")
        self.assertEqual(u2["after"]["grams"], "10.60")
        self.assertEqual(diff["totals_before"]["yuan"], "123.60")
        self.assertEqual(diff["totals_after"]["yuan"], "122.80")
        # 确认版本可回看，内容未被改写
        index = load_index(self.root, "2026-10")
        self.assertTrue(all(v["confirmed"] for v in index["versions"]))

    def test_same_content_adds_no_version(self):
        doc1 = build_handover_doc(self.root, "2026-10", POST)
        store_handover(self.root, doc1, confirm=True)
        doc2 = build_handover_doc(self.root, "2026-10", POST)
        v, created = store_handover(self.root, doc2, confirm=True)
        self.assertFalse(created)
        self.assertEqual(v, "v1")

    def test_new_events_classified_separately(self):
        """实验人员继续报数据：新增实物动作与历史补正分开归类。"""
        doc1 = build_handover_doc(self.root, "2026-11", NOV1)
        store_handover(self.root, doc1, confirm=True)
        receive_file(self.root, "uses", REPO / "examples" / "batch2" / "uses.csv",
                     AT("2026-11-03T18:00:00+08:00"))
        receive_file(self.root, "weighings", REPO / "examples" / "batch2" / "weighings.csv",
                     AT("2026-11-03T18:00:00+08:00"))
        doc2 = build_handover_doc(self.root, "2026-11", NOV4)
        store_handover(self.root, doc2, confirm=True)
        diff = diff_handovers(self.root, "2026-11", "v1", "v2")
        causes = {c["cause"] for c in diff["input_changes"]}
        self.assertEqual(causes, {"新增实物动作"})
        added = {(r["section"], r["key"]) for r in diff["rows_changed"]}
        self.assertIn(("confirmed_lines", "U7"), added)
        # 2026-10 交接不受 11 月新动作影响
        doc_oct = build_handover_doc(self.root, "2026-10", NOV4)
        doc_oct_ref = build_handover_doc(self.root, "2026-10", POST)
        self.assertEqual(doc_oct["content_hash"], doc_oct_ref["content_hash"])

    def test_handover_keeps_unresolved_parts(self):
        """交接包含未决质量差额与原因，不为月末总数删除。"""
        doc = build_handover_doc(self.root, "2026-10", POST)
        pending_ids = {p["pending_id"] for p in doc["content"]["open_pendings"]}
        self.assertIn("PEND-U6", pending_ids)
        self.assertIn("PEND-DC3", pending_ids)
        gaps = {d["pack_id"] for d in doc["content"]["mass_discrepancies"]}
        self.assertEqual(gaps, {"P2", "P3", "P3A"})
        pending_uses = {p["use_id"] for p in doc["content"]["pending_lines"]}
        self.assertEqual(pending_uses, {"U5", "U6"})


class CliTests(WarehouseCase):
    def test_end_to_end_cli(self):
        argv = ["--root", str(self.root)]
        cli(argv + ["build", "--cutoff", POST.isoformat()])
        self.assertTrue((self.root / "warehouse" / "mart" / "container_ledger.csv").exists())
        cli(argv + ["handover", "--period", "2026-10", "--cutoff", POST.isoformat(),
                    "--confirm"])
        cli(argv + ["check", "--cutoff", POST.isoformat()])
        cli(argv + ["pending", "--cutoff", POST.isoformat()])
        cli(argv + ["versions", "--period", "2026-10"])


if __name__ == "__main__":
    unittest.main()
