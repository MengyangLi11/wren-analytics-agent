from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
import json

import duckdb
import pandas as pd

from prepare_retail import obtain_workbook, audit_frame, XLSX_SHA256
from .common import (ROOT, RETAIL_DIR, DB_PATH, CONTRACT_PATH, MDL_PATH,
                     MANIFEST_PATH, LABELS, file_hash, save_json)


def line_cents(quantity, price):
    amount = (Decimal(str(price)) * abs(int(quantity))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(amount * 100)


def classify(frame):
    data = frame.copy()
    data.insert(0, "source_row", data.index + 2)
    quantity = pd.to_numeric(data["Quantity"], errors="coerce")
    price = pd.to_numeric(data["UnitPrice"], errors="coerce")
    dates = pd.to_datetime(data["InvoiceDate"], errors="coerce")
    cancelled = data["InvoiceNo"].astype("string").str.upper().str.startswith("C", na=False)
    sku = data["StockCode"].astype("string").str.fullmatch(r"\d{5}[A-Za-z]*", na=False)
    reason = pd.Series("kept", index=data.index)
    rules = [
        (data["InvoiceNo"].isna(), "missing_invoice"),
        (dates.isna(), "invalid_date"),
        (quantity.isna() | (quantity % 1 != 0) | (quantity == 0), "invalid_quantity"),
        (price.isna() | (price <= 0), "nonpositive_or_invalid_price"),
        (~sku, "nonstandard_stock_code"),
        ((quantity < 0) & ~cancelled, "negative_quantity_without_cancellation"),
        ((quantity > 0) & cancelled, "positive_quantity_with_cancellation"),
    ]
    for condition, label in rules:
        reason.loc[(reason == "kept") & condition] = label
    selected = reason == "kept"
    cents = pd.Series(0, index=data.index, dtype="int64")
    cents.loc[selected] = [line_cents(q, p) for q, p in zip(quantity[selected], price[selected])]
    reason.loc[selected & (cents == 0)] = "rounded_zero_amount"
    data["exclusion_reason"] = reason
    kept = reason == "kept"
    events = pd.DataFrame({
        "event_id": "XLSX-" + data.loc[kept, "source_row"].astype(str),
        "source_row": data.loc[kept, "source_row"],
        "order_id": data.loc[kept, "InvoiceNo"].astype("string"),
        "event_date": dates[kept].dt.date,
        "event_type": cancelled[kept].map({False: "sale", True: "cancellation"}),
        "amount_text": [f"{int(value) // 100}.{int(value) % 100:02d}" for value in cents[kept]],
        "customer_id": data.loc[kept, "CustomerID"].astype("string"),
        "country": data.loc[kept, "Country"].astype("string"),
        "stock_code": data.loc[kept, "StockCode"].astype("string"),
        "amount_cents": cents[kept],
    }).reset_index(drop=True)
    return data, events


def contract():
    expressions = {
        "gross_sales": "SUM(CASE WHEN event_type = 'sale' THEN amount ELSE 0 END)",
        "cancel_amount": "SUM(CASE WHEN event_type = 'cancellation' THEN amount ELSE 0 END)",
        "net_sales": "SUM(CASE WHEN event_type = 'sale' THEN amount ELSE -amount END)",
    }
    return {
        "version": "retail-invoice-v1", "dataset": "uci-online-retail-352", "currency": "GBP",
        "source_sha256": XLSX_SHA256,
        "time_basis": "按源文件InvoiceDate的日历日期统计，不推断时区；首尾日期均计入。",
        "amount_basis": "开票记录口径，不代表收款到账、利润或会计收入。C发票是取消记录，不能视为已到账退款。",
        "rounding": "先Quantity绝对值乘UnitPrice，再逐行ROUND_HALF_UP至0.01 GBP；之后求和。",
        "inclusion": "商品代码匹配五位数字后接可选字母；单价大于零。普通发票正数量为sale，C发票负数量为cancellation。",
        "duplicates": "保留完全相同的原始行；缺少行级业务唯一标识，不能证明这些记录是重复入库。",
        "missing_customer": "金额汇总保留缺少客户编号的记录；客户聚类统计另行报告排除数量。",
        "cancellation_link": "C发票保留原编号，不剥除C推断原订单；不提供原订单退款归因。",
        "metrics": {key: {"name": LABELS[key], "sql": sql} for key, sql in expressions.items()},
    }


def make_mdl(definitions):
    columns = [("event_id", "VARCHAR"), ("source_row", "INTEGER"), ("order_id", "VARCHAR"),
               ("event_date", "DATE"), ("event_type", "VARCHAR"), ("amount", "DECIMAL"),
               ("customer_id", "VARCHAR"), ("country", "VARCHAR"), ("stock_code", "VARCHAR")]
    formulas = ", ".join(f"{item['sql']} AS {key}" for key, item in definitions["metrics"].items())
    return {"catalog": "wren", "schema": "public", "models": [{
        "name": "retail_events", "primaryKey": "event_id",
        "tableReference": {"catalog": "retail", "schema": "main", "table": "transactions"},
        "columns": [{"name": name, "type": dtype, "isCalculated": False,
                     "notNull": name not in ("customer_id", "country")} for name, dtype in columns],
    }], "relationships": [], "views": [{"name": "retail_daily_metrics", "statement":
        f"SELECT event_date, {formulas} FROM retail_events GROUP BY event_date"}]}


def build():
    workbook = obtain_workbook()
    print("读取原始Excel，重新生成可追溯的数据集……", flush=True)
    text_columns = ["InvoiceNo", "StockCode", "Description", "CustomerID", "Country"]
    frame = pd.read_excel(workbook, engine="openpyxl", dtype={key: "string" for key in text_columns})
    audit = audit_frame(frame)
    raw, events = classify(frame)
    definitions = contract()
    mdl = make_mdl(definitions)
    RETAIL_DIR.mkdir(parents=True, exist_ok=True)
    temporary = RETAIL_DIR / "retail.build.duckdb"
    if temporary.exists():
        temporary.unlink()
    sales_cents = sum(int(value) for value in events.loc[events.event_type == "sale", "amount_cents"])
    cancel_cents = sum(int(value) for value in events.loc[events.event_type == "cancellation", "amount_cents"])
    with duckdb.connect(str(temporary)) as connection:
        connection.register("raw_frame", raw)
        connection.register("event_frame", events)
        connection.execute("CREATE TABLE raw_rows AS SELECT * FROM raw_frame")
        connection.execute("""CREATE TABLE transactions (
            event_id VARCHAR PRIMARY KEY, source_row INTEGER NOT NULL,
            order_id VARCHAR NOT NULL, event_date DATE NOT NULL,
            event_type VARCHAR CHECK(event_type IN ('sale','cancellation')),
            amount DECIMAL(18,2) CHECK(amount > 0), customer_id VARCHAR,
            country VARCHAR, stock_code VARCHAR)""")
        connection.execute("""INSERT INTO transactions SELECT event_id, source_row, order_id, event_date,
            event_type, CAST(amount_text AS DECIMAL(18,2)), customer_id, country, stock_code FROM event_frame""")
        totals = connection.execute("SELECT " + ", ".join(value["sql"] for value in definitions["metrics"].values()) + " FROM transactions").fetchone()
        expected = tuple(Decimal(value) / 100 for value in (sales_cents, cancel_cents, sales_cents - cancel_cents))
        if totals != expected:
            raise RuntimeError("Python逐行Decimal与数据库汇总不一致，构建已停止。")
        assert connection.execute("SELECT COUNT(*),COUNT(DISTINCT source_row) FROM raw_rows").fetchone() == (541909, 541909)
        dates = connection.execute("SELECT MIN(event_date),MAX(event_date) FROM transactions").fetchone()
    temporary.replace(DB_PATH)
    save_json(CONTRACT_PATH, definitions)
    save_json(MDL_PATH, mdl)
    manifest = {
        "dataset": "uci-online-retail-352", "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_url": "https://archive.ics.uci.edu/dataset/352/online+retail",
        "citation": "Chen, D. (2015). Online Retail. UCI. DOI:10.24432/C5BW33", "license": "CC BY 4.0",
        "source_sha256": XLSX_SHA256, "database_sha256": file_hash(DB_PATH),
        "definitions_sha256": file_hash(CONTRACT_PATH), "mdl_sha256": file_hash(MDL_PATH),
        "audit": audit, "classification": {str(key): int(value) for key, value in raw.exclusion_reason.value_counts().items()},
        "kept_rows": len(events), "date_min": str(dates[0]), "date_max": str(dates[1]),
        "totals": {key: str(value) for key, value in zip(definitions["metrics"], totals)},
        "currency": "GBP", "rounding": definitions["rounding"],
        "code_sha256": file_hash(__file__),
    }
    save_json(MANIFEST_PATH, manifest)
    print(json.dumps({key: manifest[key] for key in ("kept_rows", "classification", "totals", "currency")}, ensure_ascii=False, indent=2))
    print("真实数据构建完成，Python Decimal与DuckDB金额对账通过。")
    return manifest
