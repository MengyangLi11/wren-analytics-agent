"""独立从原始Excel生成金额参考值；不导入被测Wren查询工具。"""
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from datetime import date
import re

from openpyxl import load_workbook

from prepare_retail import obtain_workbook, XLSX_SHA256
from wren_analysis.common import ROOT, save_json, fingerprint


def reference_events():
    book = load_workbook(obtain_workbook(), read_only=True, data_only=True)
    events = []
    try:
        for row in book.active.iter_rows(min_row=2, values_only=True):
            invoice, sku, _, quantity, timestamp, price, customer, country = row
            if invoice is None or timestamp is None or quantity is None or price is None:
                continue
            if not re.fullmatch(r"[0-9]{5}[a-zA-Z]*", str(sku)) or price <= 0 or quantity == 0:
                continue
            cancelled = str(invoice).upper().startswith("C")
            if cancelled != (quantity < 0):
                continue
            amount = (Decimal(str(price)) * Decimal(abs(quantity))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if amount <= 0:
                continue
            customer_id = None if customer is None else str(int(customer))
            events.append({"order_id": str(invoice), "day": timestamp.date().isoformat(), "cancelled": cancelled,
                           "amount": amount, "customer_id": customer_id, "country": country})
    finally:
        book.close()
    return events


def selected(events, scope):
    return [row for row in events if
            (scope["all_dates"] or scope["start_date"] <= row["day"] <= scope["end_date"]) and
            ("order_id" not in scope or row["order_id"] == scope["order_id"]) and
            ("country" not in scope or row["country"] == scope["country"])]


def reference_summary(events, scope, metrics):
    rows = selected(events, scope)
    gross = sum((row["amount"] for row in rows if not row["cancelled"]), Decimal("0.00"))
    cancel = sum((row["amount"] for row in rows if row["cancelled"]), Decimal("0.00"))
    totals = {"gross_sales": str(gross), "cancel_amount": str(cancel), "net_sales": str(gross - cancel)}
    return {"kind": "summary", "status": "ok" if rows else "no_records", "scope": scope,
            "requested_metrics": metrics, "event_count": len(rows),
            "metrics": {key: totals[key] for key in metrics} if rows else None}


def reference_ci(events, scope):
    invoices = defaultdict(list)
    for row in selected(events, scope):
        if not row["cancelled"]:
            invoices[row["order_id"]].append(row)
    valid, excluded = [], 0
    for rows in invoices.values():
        customers = {row["customer_id"] for row in rows}
        if None in customers or len(customers) != 1:
            excluded += 1
        else:
            valid.append((next(iter(customers)), sum(row["amount"] for row in rows)))
    clusters = len({customer for customer, _ in valid})
    return {"kind": "invoice_ci", "scope": scope,
            "status": "ok" if clusters >= 30 else ("insufficient_clusters" if clusters else "no_records"),
            "invoice_count": len(valid), "cluster_count": clusters,
            "excluded_unidentified_or_inconsistent_invoices": excluded,
            "mean": float(sum((amount for _, amount in valid), Decimal(0)) / len(valid)) if valid else None}


def main():
    print("独立读取原始Excel生成参考金额（不调用Wren或模型）……", flush=True)
    events = reference_events()
    all_metrics = ["gross_sales", "cancel_amount", "net_sales"]
    cases = []
    def add_summary(question, scope, metrics=None):
        metrics = metrics or all_metrics
        cases.append({"id": f"R{len(cases)+1:02}", "question": question,
                      "patch": {"supported": True, "analysis": "summary", "metrics": metrics,
                                "time_range": {key: value for key, value in scope.items() if key in ("all_dates", "start_date", "end_date")},
                                **{key: value for key, value in scope.items() if key in ("order_id", "country")}},
                      "expected": reference_summary(events, scope, metrics)})
    periods = [(2010, 12)] + [(2011, month) for month in range(1, 12)]
    from calendar import monthrange
    for year, month in periods:
        scope = {"all_dates": False, "start_date": date(year, month, 1).isoformat(), "end_date": date(year, month, monthrange(year, month)[1]).isoformat()}
        add_summary(f"统计{year}年{month}月的正向开票金额、取消冲减金额和净开票金额。", scope)
    add_summary("全期的正向开票金额、取消冲减金额和净开票金额。", {"all_dates": True})
    for day in ("2010-12-01", "2011-12-09", "2012-01-01"):
        add_summary(f"{day}当天的净销售额是多少？", {"all_dates": False, "start_date": day, "end_date": day}, ["net_sales"])
    for country in ("United Kingdom", "Germany", "France", "EIRE", "Spain", "Japan"):
        add_summary(f"查询全期{country}的净开票金额。", {"all_dates": True, "country": country}, ["net_sales"])
    for invoice in ("536365", "536367", "C536379", "999999999"):
        add_summary(f"查询发票号为{invoice}的全部交易事件，汇总正向开票金额、取消冲减金额和净开票金额。", {"all_dates": True, "order_id": invoice})
    for month in (1, 5, 11):
        add_summary(f"2011年{month}月United Kingdom的取消金额是多少？", {"all_dates": False, "start_date": f"2011-{month:02}-01",
                    "end_date": f"2011-{month:02}-{monthrange(2011, month)[1]}", "country": "United Kingdom"}, ["cancel_amount"])
    for invoice in ("536365", "C536379", "999999999"):
        add_summary(f"发票号为{invoice}在2010-12-01的净销售额。", {"all_dates": False, "start_date": "2010-12-01", "end_date": "2010-12-01", "order_id": invoice}, ["net_sales"])
    assert len(cases) == 32
    clarifications = [
        ("收入多少？", ["metrics", "time_range"]), ("2011年11月的收入是多少？", ["metrics"]),
        ("净销售额多少？", ["time_range"]), ("全部现有交易事件的收入是多少？", ["metrics"]),
        ("统计全部订单的净销售额。", ["time_range"]), ("2011年2月30日的净开票金额。", ["time_range"]),
        ("昨天销售额多少？", ["time_range"]), ("2011-12-09至2011-12-01的净销售额。", ["time_range"]),
        ("2011年13月销售额多少？", ["time_range"]), ("订单均值的置信区间是多少？", ["time_range"]),
    ]
    for index, (question, fields) in enumerate(clarifications, 1):
        cases.append({"id": f"C{index:02}", "question": question, "patch": {"supported": True}, "expected": {"kind": "clarification", "fields": fields}})
    for index, question in enumerate(("全期到账退款多少？", "2011年11月利润多少？", "预测2012年的销售额。", "2011年11月销售变化是否显著？"), 1):
        cases.append({"id": f"U{index:02}", "question": question, "patch": {"supported": False}, "expected": {"kind": "unsupported"}})
    statistical = [
        ("2011年11月订单金额均值的置信区间。", {"all_dates": False, "start_date": "2011-11-01", "end_date": "2011-11-30"}),
        ("全期发票号为536365的平均订单金额置信区间。", {"all_dates": True, "order_id": "536365"}),
        ("全期发票号为999999999的平均订单金额置信区间。", {"all_dates": True, "order_id": "999999999"}),
        ("2011年11月Germany订单均值置信区间。", {"all_dates": False, "start_date": "2011-11-01", "end_date": "2011-11-30", "country": "Germany"}),
    ]
    for index, (question, scope) in enumerate(statistical, 1):
        cases.append({"id": f"B{index:02}", "question": question,
                      "patch": {"supported": True, "analysis": "invoice_ci", "time_range": {key: value for key, value in scope.items() if key in ("all_dates", "start_date", "end_date")},
                                **{key: value for key, value in scope.items() if key in ("order_id", "country")}},
                      "expected": reference_ci(events, scope)})
    for index, case in enumerate(cases):
        case["split"] = "development" if index % 5 < 3 else "validation"
    assert len(cases) == 50
    save_json(ROOT / "benchmarks" / "real_cases.json", {"suite": "retail-v1", "source_sha256": XLSX_SHA256,
        "reference_method": "independent openpyxl + Python Decimal, no Wren/store functions", "reference_event_count": len(events),
        "cases_sha256": fingerprint(cases), "cases": cases})
    print("已生成50条固定回归用例（开发30/验证20）；参考记录数：", len(events))


if __name__ == "__main__":
    main()
