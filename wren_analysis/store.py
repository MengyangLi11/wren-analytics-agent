from datetime import date
import base64
import json

from wren.engine import WrenEngine
from wren.config import WrenConfig

from .common import (DB_PATH, RETAIL_DIR, CONTRACT_PATH, MDL_PATH, MANIFEST_PATH,
                     file_hash, read_json, fingerprint)
from .dataset import make_mdl


def validate_scope(scope):
    allowed = {"all_dates", "start_date", "end_date", "order_id", "country"}
    if not isinstance(scope, dict) or set(scope) - allowed:
        raise ValueError("筛选参数包含未知字段。")
    if type(scope.get("all_dates")) is not bool:
        raise ValueError("请明确时间范围。")
    predicates = []
    if scope["all_dates"]:
        if "start_date" in scope or "end_date" in scope:
            raise ValueError("全期与起止日期不能同时出现。")
    else:
        for name in ("start_date", "end_date"):
            value = scope.get(name)
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError("请提供有效的YYYY-MM-DD起止日期。")
        if scope["start_date"] > scope["end_date"]:
            raise ValueError("起始日期不能晚于结束日期。")
        predicates += [f"event_date >= DATE '{scope['start_date']}'", f"event_date <= DATE '{scope['end_date']}'"]
    for name in ("order_id", "country"):
        if name in scope:
            value = scope[name]
            if not isinstance(value, str) or not value.strip() or len(value) > 80:
                raise ValueError(f"{name}必须是1至80个字符的非空文本。")
            predicates.append(name + " = '" + value.replace("'", "''") + "'")
    return " AND ".join(predicates) or "TRUE"


class RetailStore:
    def snapshot(self):
        if not MANIFEST_PATH.exists():
            raise ValueError("请先运行 python build_data.py 构建真实数据。")
        manifest = read_json(MANIFEST_PATH)
        for name, path in (("database", DB_PATH), ("definitions", CONTRACT_PATH), ("mdl", MDL_PATH)):
            if file_hash(path) != manifest[name + "_sha256"]:
                raise ValueError(f"{name}与构建清单不一致，请核对后重新构建数据。")
        definitions, mdl = read_json(CONTRACT_PATH), read_json(MDL_PATH)
        if mdl != make_mdl(definitions):
            raise ValueError("Wren语义模型与指标口径不一致。")
        return {"dataset": manifest["dataset"], "currency": definitions["currency"],
                "definition_version": definitions["version"], "definition_snapshot": definitions,
                "source_sha256": manifest["source_sha256"], "database_sha256": manifest["database_sha256"],
                "definitions_sha256": manifest["definitions_sha256"], "mdl_sha256": manifest["mdl_sha256"],
                "manifest_sha256": file_hash(MANIFEST_PATH)}

    def query(self, sql, limit=1):
        snapshot = self.snapshot()
        with WrenEngine(
            manifest_str=base64.b64encode(json.dumps(read_json(MDL_PATH)).encode()).decode(),
            data_source="duckdb", connection_info={"url": RETAIL_DIR.as_posix(), "format": "duckdb"},
            fallback=False, config=WrenConfig(strict_mode=True),
        ) as engine:
            plan = engine.dry_plan(sql)
            rows = engine.query(sql, limit=limit, read_only=True).to_pylist()
        rows = json.loads(json.dumps(rows, default=str))
        return {**snapshot, "sql": sql, "planned_sql": plan, "rows": rows,
                "rows_sha256": fingerprint(rows), "result_row_limit": limit}

    def summary(self, scope, metrics):
        where = validate_scope(scope)
        definitions = read_json(CONTRACT_PATH)
        if (not isinstance(metrics, list) or not metrics or
                any(not isinstance(item, str) or item not in definitions["metrics"] for item in metrics) or
                len(set(metrics)) != len(metrics)):
            raise ValueError("请选择有效且不重复的指标。")
        formulas = ", ".join(f"{definitions['metrics'][key]['sql']} AS {key}" for key in metrics)
        sql = f"SELECT COUNT(*) AS event_count, {formulas} FROM retail_events WHERE {where}"
        proof = self.query(sql)
        row = proof["rows"][0]
        return {"kind": "summary", "status": "ok" if row["event_count"] else "no_records",
                "scope": dict(scope), "requested_metrics": list(metrics), "event_count": row["event_count"],
                "metrics": {key: row[key] for key in metrics} if row["event_count"] else None,
                "proof": proof}

    def invoice_rows(self, scope):
        where = validate_scope(scope)
        sql = f"""SELECT order_id, MIN(customer_id) AS customer_id,
            COUNT(DISTINCT customer_id) AS customer_count,
            COUNT(*) AS line_count, COUNT(customer_id) AS identified_line_count,
            SUM(amount) AS invoice_amount
            FROM retail_events WHERE ({where}) AND event_type = 'sale'
            GROUP BY order_id ORDER BY order_id"""
        proof = self.query(sql, limit=200001)
        if len(proof["rows"]) > 200000:
            raise ValueError("发票数超过统计工具上限，已停止；请缩小时间范围。")
        return proof

    def daily(self, scope):
        where = validate_scope(scope)
        proof = self.query(f"SELECT event_date, SUM(CASE WHEN event_type='sale' THEN amount ELSE -amount END) AS net_sales "
                           f"FROM retail_events WHERE {where} GROUP BY event_date ORDER BY event_date", limit=4001)
        if len(proof["rows"]) > 4000:
            raise ValueError("每日趋势超过4000天，请缩小范围。")
        return proof["rows"]
