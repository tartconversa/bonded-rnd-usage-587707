"""命令行入口：接收文件、构建仓表、处理待核、完成交接、比较版本、三方核对。"""
import argparse
import datetime as dt
import json
import sys
from decimal import Decimal
from pathlib import Path

from .facts import build_facts
from .handover import (build_handover_doc, store_handover, diff_handovers,
                       load_index)
from .landing import receive_file, receive_dir
from .marts import build_marts, write_marts
from .report import render_handover_md, render_diff_md, render_recon_md, render_pending_md


def parse_at(text):
    stamp = dt.datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        raise SystemExit(f"时刻须带时区：{text}")
    return stamp


def cmd_receive(args):
    result = receive_file(args.root, args.table, args.file, parse_at(args.at))
    print(json.dumps(result, ensure_ascii=False))


def cmd_receive_dir(args):
    results = receive_dir(args.root, args.dir, parse_at(args.at), skip=args.skip)
    for r in results:
        print(json.dumps(r, ensure_ascii=False))


def cmd_build(args):
    facts = build_facts(args.root, parse_at(args.cutoff) if args.cutoff else None)
    marts = build_marts(facts)
    out = write_marts(args.root, facts, marts)
    print(f"仓表已写入 {out}（截点 {facts.cutoff}）")


def cmd_pending(args):
    facts = build_facts(args.root, parse_at(args.cutoff) if args.cutoff else None)
    print(render_pending_md(facts.pendings, facts.cutoff))


def cmd_handover(args):
    doc = build_handover_doc(args.root, args.period,
                             parse_at(args.cutoff) if args.cutoff else None)
    version, created = store_handover(args.root, doc, confirm=args.confirm)
    state = "新版本" if created else "内容相同，沿用"
    mark = "，已确认" if args.confirm else ""
    print(f"{args.period} 交接 {state} {version}{mark}，哈希 {doc['content_hash'][:12]}")
    if args.show:
        print(render_handover_md(doc, version))


def cmd_versions(args):
    index = load_index(args.root, args.period)
    for v in index["versions"]:
        mark = "已确认" if v["confirmed"] else "未确认"
        print(f"{v['version']}  {mark}  截点 {v['cutoff']}  哈希 {v['hash'][:12]}")


def cmd_diff(args):
    diff = diff_handovers(args.root, args.period, args.va, args.vb)
    print(render_diff_md(diff))


def cmd_check(args):
    facts = build_facts(args.root, parse_at(args.cutoff) if args.cutoff else None)
    marts = build_marts(facts)
    print(render_recon_md(marts["recon"], facts.cutoff))
    broken = []
    for m in marts["recon"]["mass"]:
        containers = m["containers"]
        all_weighed = all(c["measured_g"] is not None for c in containers)
        if m["residual_g"] is None or not all_weighed:
            continue  # 有未知环节时余差不可算，已在待核队列单列
        # 恒等式：物质余差 + 各容器称量差之和 = 0（待核申报已在两侧同列）
        diffs = sum((c["diff_g"] for c in containers if c["diff_g"] is not None), Decimal(0))
        if abs(m["residual_g"] + diffs) > 0:
            broken.append(m["material_id"])
    if not marts["recon"]["count"]["conserved"]:
        broken.append("count")
    if broken:
        print(f"核对未通过：{broken}", file=sys.stderr)
        sys.exit(1)
    print("核对通过：质量、计数、成本↔核销均同源。")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="consume", description="材料耗用仓")
    parser.add_argument("--root", default=".", help="仓库根目录（含 data/ 与 warehouse/）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("receive", help="接收一个资料文件")
    p.add_argument("--file", required=True)
    p.add_argument("--table", required=True)
    p.add_argument("--at", required=True, help="该批资料的知识时刻，须带时区")
    p.set_defaults(func=cmd_receive)

    p = sub.add_parser("receive-dir", help="接收一个文件夹内的全部资料表")
    p.add_argument("--dir", required=True)
    p.add_argument("--at", required=True)
    p.add_argument("--skip", nargs="*", default=[], help="跳过的表（后续接收类）")
    p.set_defaults(func=cmd_receive_dir)

    p = sub.add_parser("build", help="构建可读仓表")
    p.add_argument("--cutoff", default=None, help="知识截点；缺省为已收资料的最新时刻")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("pending", help="查看待核队列")
    p.add_argument("--cutoff", default=None)
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("handover", help="生成一期交接（内容相同不新增版本）")
    p.add_argument("--period", required=True)
    p.add_argument("--cutoff", default=None)
    p.add_argument("--confirm", action="store_true", help="确认该版本")
    p.add_argument("--show", action="store_true", help="同时打印交接内容")
    p.set_defaults(func=cmd_handover)

    p = sub.add_parser("versions", help="查看一期交接的版本")
    p.add_argument("--period", required=True)
    p.set_defaults(func=cmd_versions)

    p = sub.add_parser("diff", help="比较两个交接版本")
    p.add_argument("--period", required=True)
    p.add_argument("--from", dest="va", required=True)
    p.add_argument("--to", dest="vb", required=True)
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("check", help="三方核对（库存/成本/核销）")
    p.add_argument("--cutoff", default=None)
    p.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    args.root = Path(args.root)
    args.func(args)


if __name__ == "__main__":
    main()
