"""按目录契约解析资料行：空值是未知不是零，数量一律 Decimal。"""
from decimal import Decimal
import datetime as dt
import hashlib
import json
from pathlib import Path


def load_catalog(root):
    return json.loads((Path(root) / "data" / "catalog.json").read_text(encoding="utf-8"))


def table_def(catalog, name):
    for table in catalog["tables"]:
        if table["name"] == name:
            return table
    raise KeyError(f"目录中无此表：{name}")


def parse_value(kind, raw):
    if raw is None or raw == "":
        return None
    if kind == "number":
        return Decimal(raw)
    if kind == "integer":
        return int(raw)
    if kind == "date":
        return dt.date.fromisoformat(raw)
    if kind == "datetime":
        return dt.datetime.fromisoformat(raw)
    return raw


def parse_rows(tdef, raws):
    return [{c["name"]: parse_value(c["type"], r[c["name"]]) for c in tdef["columns"]} for r in raws]


def key_of(tdef, row):
    return ":".join(str(row[k]) for k in tdef.get("key", []))


def canon(value):
    """把解析后的值转成可 JSON、可哈希的稳定文本。"""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def row_fingerprint(tdef, row):
    body = {c["name"]: canon(row[c["name"]]) for c in tdef["columns"]}
    text = json.dumps(body, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
