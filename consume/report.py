"""可读报告：交接、差异、三方核对、待核队列的 Markdown 呈现。"""
from .constants import TOLERANCE_G


def _g(value):
    return "" if value is None else str(value)


def render_handover_md(doc, version):
    c = doc["content"]
    lines = [
        f"# 核销交接 {doc['period']}（{version}）",
        "",
        f"- 知识截点：{doc['cutoff']}",
        f"- 内容哈希：{doc['content_hash']}",
        f"- 称量容差：{TOLERANCE_G} g；超差待核，不自动生成损耗",
        "- 确认资格：confirmed use / documented evaporate / witnessed destroy；pending 不自动核销",
        "",
        "## 确认核销行",
        "",
        "| 去向 | 项目 | 类别 | 证据 | 克 | 元 | 成本期 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in c["confirmed_lines"]:
        lines.append(f"| {row['use_id']} | {row['project']} | {row['kind']} | {row['evidence']} "
                     f"| {_g(row['grams'])} | {_g(row['yuan'])} | {row['cost_period']} |")
    if not c["confirmed_lines"]:
        lines.append("| （无） | | | | | | |")
    lines += ["", "## 待核去向", "",
              "| 去向 | 项目 | 类别 | 原因 |", "| --- | --- | --- | --- |"]
    for row in c["pending_lines"]:
        lines.append(f"| {row['use_id']} | {row['project']} | {row['kind']} | {row['reason']} |")
    if not c["pending_lines"]:
        lines.append("| （无） | | | |")
    lines += ["", "## 未决质量差额", "",
              "| 容器 | 物质 | 保管方 | 账面克 | 待核克 | 应有克 | 实测克 | 差 | 状态 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in c["mass_discrepancies"]:
        lines.append(f"| {row['pack_id']} | {row['material_id']} | {row['holder']} "
                     f"| {_g(row['book_g'])} | {_g(row['pending_out_g'])} | {_g(row['expected_g'])} "
                     f"| {_g(row['measured_g'])} | {_g(row['diff_g'])} | {row['recon_status']} |")
    if not c["mass_discrepancies"]:
        lines.append("| （无） | | | | | | | | |")
    lines += ["", "## 未决事项（不随月末总数删除）", "",
              "| 编号 | 主题 | 原因 | 需要 |", "| --- | --- | --- | --- |"]
    for row in c["open_pendings"]:
        lines.append(f"| {row['pending_id']} | {row['topic']} | {row['reason']} | {row['needed']} |")
    if not c["open_pendings"]:
        lines.append("| （无） | | | |")
    totals = c["totals"]
    lines += ["", "## 合计", "",
              f"- 确认耗用：{totals['grams']} g，{totals['yuan']} 元"]
    for project, slot in sorted(totals["by_project"].items()):
        lines.append(f"- 项目 {project}：{slot['grams']} g，{slot['yuan']} 元")
    lines.append("")
    return "\n".join(lines)


def render_diff_md(diff):
    lines = [
        f"# 交接差异 {diff['period']}：{diff['from']} → {diff['to']}",
        "",
        f"- 截点：{diff['cutoff_from']} → {diff['cutoff_to']}",
        "",
        "## 输入变化",
        "",
        "| 来源行 | 变化 | 分类 |", "| --- | --- | --- |",
    ]
    for row in diff["input_changes"]:
        lines.append(f"| {row['ref']} | {row['change']} | {row['cause']} |")
    if not diff["input_changes"]:
        lines.append("| （无） | | |")
    lines += ["", "## 行级差异", "",
              "| 部分 | 键 | 变化 | 原因分类 | 项目 |", "| --- | --- | --- | --- | --- |"]
    for row in diff["rows_changed"]:
        lines.append(f"| {row['section']} | {row['key']} | {row['change']} "
                     f"| {'、'.join(row['causes'])} | {row['project'] or ''} |")
    if not diff["rows_changed"]:
        lines.append("| （无） | | | | |")
    lines += ["", "## 影响汇总", ""]
    for cause, slot in diff["summary"].items():
        projects = "、".join(slot["projects"]) or "（非项目行）"
        lines.append(f"- {cause}：{slot['rows']} 行，涉及项目 {projects}")
    if not diff["summary"]:
        lines.append("- （无）")
    if diff["totals_changed"]:
        lines += ["", f"- 合计：{diff['totals_before']['grams']} g / {diff['totals_before']['yuan']} 元"
                      f" → {diff['totals_after']['grams']} g / {diff['totals_after']['yuan']} 元"]
    lines.append("")
    return "\n".join(lines)


def render_recon_md(recon, cutoff):
    lines = [
        "# 三方核对（库存 / 成本 / 核销同源）",
        "",
        f"- 知识截点：{cutoff}",
        "",
        "## 质量守恒（按物质）",
        "",
        "| 物质 | 实收克 | 确认出账克 | 待核克 | 实测克 | 余差克 | 未知容器 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for m in recon["mass"]:
        lines.append(f"| {m['material_id']} | {_g(m['received_g'])} | {_g(m['confirmed_out_g'])} "
                     f"| {_g(m['pending_out_g'])} | {_g(m['measured_g'])} | {_g(m['residual_g'])} "
                     f"| {'、'.join(m['unknown_containers']) or '无'} |")
    lines += ["", "## 计数守恒", ""]
    count = recon["count"]
    lines.append(f"- 实收瓶数 {count['received_bottles']} = 现存 {count['current_bottles']} "
                 f"+ 已销毁 {count['destroyed_bottles']}；分装空包装 {count['empty_packages']} 个另列")
    lines += ["", "## 成本 ↔ 核销（按去向行同源）", ""]
    cw = recon["cost_writeoff"]
    lines.append(f"- 项目成本合计（已知质量）：{cw['cost_total_yuan']} 元")
    lines.append(f"- 核销确认合计：{cw['writeoff_confirmed_yuan']} 元")
    if cw["unknown_cost_lines"]:
        lines.append(f"- 质量未知的成本行（未计入）：{'、'.join(cw['unknown_cost_lines'])}")
    lines += ["", "| 去向 | 项目 | 成本期 | 核销期 | 克 | 元 |", "| --- | --- | --- | --- | --- | --- |"]
    for row in cw["bridge"]:
        lines.append(f"| {row['use_id']} | {row['project']} | {row['cost_period']} "
                     f"| {row['handover_period']} | {_g(row['grams'])} | {_g(row['yuan'])} |")
    lines.append("")
    return "\n".join(lines)


def render_pending_md(pendings, cutoff):
    lines = [f"# 待核队列（截点 {cutoff}）", "",
             "| 编号 | 主题 | 原因 | 需要 | 影响 |", "| --- | --- | --- | --- | --- |"]
    for p in pendings:
        lines.append(f"| {p['pending_id']} | {p['topic']} | {p['reason']} | {p['needed']} "
                     f"| {'、'.join(p['affects'])} |")
    if not pendings:
        lines.append("| （无） | | | | |")
    lines.append("")
    return "\n".join(lines)
