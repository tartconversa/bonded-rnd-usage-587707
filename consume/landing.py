"""接收层：原始文件与接收历史都保留；相同摘要不增加事实；同键新文件是整行修订。"""
from pathlib import Path
import datetime as dt
import hashlib
import json

from sourcepack.catalog import read_table

from .model import load_catalog, table_def, parse_rows, key_of

LOG_NAME = "receive_log.jsonl"


def landing_dir(root):
    return Path(root) / "warehouse" / "landing"


def log_path(root):
    return landing_dir(root) / LOG_NAME


def load_log(root):
    path = log_path(root)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def receive_file(root, table, path, received_at):
    """接收一个文件到落地区。received_at 为该批资料的知识时刻（调用方明确接收次序）。"""
    catalog = load_catalog(root)
    tdef = table_def(catalog, table)
    path = Path(path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    log = load_log(root)
    for entry in log:
        if entry["sha256"] == digest:
            return {"status": "duplicate", "table": table, "sha256": digest}
    rows = parse_rows(tdef, read_table(path, tdef))  # 契约校验：字段、类型、记录键
    seq = max((e["seq"] for e in log), default=0) + 1
    dest = landing_dir(root) / f"{seq:04d}_{table}.csv"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    entry = {
        "seq": seq,
        "table": table,
        "sha256": digest,
        "received_at": received_at.isoformat(),
        "path": str(dest.relative_to(root)),
        "rows": len(rows),
        "source": str(path),
    }
    with log_path(root).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"status": "received", **entry}


def receive_dir(root, data_dir, received_at, skip=()):
    """按目录接收一个文件夹内的全部资料表，可跳过后续接收类表。"""
    catalog = load_catalog(root)
    results = []
    for table in catalog["tables"]:
        if table["name"] in skip:
            continue
        path = Path(data_dir) / Path(table["path"]).name
        if path.is_file():
            results.append(receive_file(root, table["name"], path, received_at))
    return results


def merged_rows(root, cutoff=None):
    """按接收次序合并整行修订；知识时刻晚于截点的文件与补正不参与。

    返回 (tables, catalog)：tables 为 {表名: [带类型的行]}，每行附带
    _seq（接收序号）与 _knowledge（文件知识时刻）。
    """
    catalog = load_catalog(root)
    tables = {}
    for entry in sorted(load_log(root), key=lambda e: e["seq"]):
        knowledge = dt.datetime.fromisoformat(entry["received_at"])
        if cutoff is not None and knowledge > cutoff:
            continue
        tdef = table_def(catalog, entry["table"])
        rows = parse_rows(tdef, read_table(Path(root) / entry["path"], tdef))
        bucket = tables.setdefault(entry["table"], {})
        for row in rows:
            if entry["table"] == "density_corrections" and cutoff is not None:
                if row["received_at"] is not None and row["received_at"] > cutoff:
                    continue  # 补正自身的收到时刻晚于截点，尚不可知
            row["_seq"] = entry["seq"]
            row["_knowledge"] = knowledge
            bucket[key_of(tdef, row)] = row
    ordered = {}
    for name, bucket in tables.items():
        tdef = table_def(catalog, name)
        ordered[name] = sorted(bucket.values(), key=lambda r: key_of(tdef, r))
    return ordered, catalog
