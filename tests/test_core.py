from copy import deepcopy
from decimal import Decimal
import json

import httpx
import numpy as np
import pandas as pd
import pytest

from wren_analysis.agent import AnalysisSession, empty_state, extract_patch, merge_patch, missing
from wren_analysis.dataset import classify, line_cents
from wren_analysis.policy import guard_patch, explicit_time
from wren_analysis.statistics import cluster_bootstrap
from wren_analysis.store import validate_scope


@pytest.mark.parametrize("text,expected", [
    ("2011年11月1日至3日净销售额", {"all_dates": False, "start_date": "2011-11-01", "end_date": "2011-11-03"}),
    ("2011年2月", {"all_dates": False, "start_date": "2011-02-01", "end_date": "2011-02-28"}),
    ("2012年2月", {"all_dates": False, "start_date": "2012-02-01", "end_date": "2012-02-29"}),
    ("2011年全年", {"all_dates": False, "start_date": "2011-01-01", "end_date": "2011-12-31"}),
    ("全期", {"all_dates": True}), ("2011年2月30日", None), ("2011年13月", None),
    ("2011-12-03至2011-12-01", None), ("昨天", None), ("11月2日", None),
])
def test_explicit_calendar_and_rejection(text, expected):
    assert explicit_time(text) == (expected, True)


def test_all_orders_and_invoice_literals_are_not_time():
    assert explicit_time("全部订单") == (None, False)
    assert explicit_time("查询订单号为“2011-11-01”的净销售额") == (None, False)


@pytest.mark.parametrize("patch", [
    {"supported": True, "metrics": ["profit"]},
    {"supported": True, "metrics": ["net_sales", "net_sales"]},
    {"supported": True, "sql": "DELETE FROM transactions"},
    {"supported": True, "time_range": {"all_dates": 1}},
    {"supported": True, "time_range": {"all_dates": False, "start_date": "2011-11-01"}},
    {"supported": True, "time_range": {"all_dates": True, "order_id": "hidden_filter"}},
    {"supported": True, "country": ""},
])
def test_invalid_patch_is_atomic(patch):
    state = empty_state()
    before = deepcopy(state)
    with pytest.raises(ValueError):
        merge_patch(state, patch)
    assert state == before


def test_scope_quote_escaping_and_conflicting_dates():
    assert "O''001" in validate_scope({"all_dates": True, "order_id": "O'001"})
    with pytest.raises(ValueError):
        validate_scope({"all_dates": True, "start_date": "2011-11-01"})


def test_round_after_quantity_and_keep_lineage():
    assert line_cents(3, "0.005") == 2
    frame = pd.DataFrame([
        ["1", "12345", "a", 3, pd.Timestamp("2011-11-01"), .005, None, "United Kingdom"],
        ["C2", "12345", "a", -2, pd.Timestamp("2011-11-02"), 5, "11", "United Kingdom"],
        ["3", "12345", "a", -1, pd.Timestamp("2011-11-02"), 5, "11", "United Kingdom"],
        ["4", "POST", "fee", 1, pd.Timestamp("2011-11-02"), 5, "11", "United Kingdom"],
        ["1", "12345", "a", 3, pd.Timestamp("2011-11-01"), .005, None, "United Kingdom"],
    ], columns=["InvoiceNo", "StockCode", "Description", "Quantity", "InvoiceDate", "UnitPrice", "CustomerID", "Country"])
    raw, kept = classify(frame)
    assert kept.source_row.tolist() == [2, 3, 6]
    assert kept.amount_cents.tolist() == [2, 1000, 2]
    assert kept.event_type.tolist() == ["sale", "cancellation", "sale"]
    assert raw.exclusion_reason.tolist()[2:4] == ["negative_quantity_without_cancellation", "nonstandard_stock_code"]


def test_cluster_bootstrap_uses_customer_weighted_invoice_mean():
    clusters = [{"customer_id": str(i), "amount_cents": (i + 1) * 1000, "invoice_count": (i % 3) + 1} for i in range(30)]
    result = cluster_bootstrap(clusters, 200, seed=7)
    assert result["mean"] == sum(row["amount_cents"] for row in clusters) / sum(row["invoice_count"] for row in clusters) / 100
    rng = np.random.Generator(np.random.PCG64(7))
    means = []
    for _ in range(200):
        sampled = [clusters[index] for index in rng.integers(0, 30, size=30)]
        means.append(sum(row["amount_cents"] for row in sampled) / sum(row["invoice_count"] for row in sampled) / 100)
    assert np.allclose(result["ci"], np.quantile(means, [.025, .975]), atol=1e-10)
    assert result == cluster_bootstrap(clusters, 200, seed=7)
    assert cluster_bootstrap(clusters[:29], 200)["ci"] is None
    assert cluster_bootstrap([], 200)["status"] == "no_records"
    constant = [{"amount_cents": 1000, "invoice_count": 1} for _ in range(30)]
    assert cluster_bootstrap(constant, 200)["ci"] == [10.0, 10.0]


def test_multi_turn_guard_preserves_unchanged_filters():
    state = {"analysis": "summary", "metrics": ["net_sales"], "time_range": {"all_dates": True}, "order_id": "536365", "country": "Germany"}
    patch = guard_patch("改成2011年11月2日", {"supported": True, "metrics": None}, state)
    updated = merge_patch(state, patch)
    assert updated["order_id"] == "536365" and updated["country"] == "Germany"
    assert updated["metrics"] == ["net_sales"]
    assert updated["time_range"]["start_date"] == "2011-11-02"
    cleared = merge_patch(updated, guard_patch("取消订单筛选", {"supported": True, "order_id": None}, updated))
    assert cleared["order_id"] is None and cleared["country"] == "Germany"
    assert cleared["time_range"] == updated["time_range"]
    ambiguous = merge_patch(cleared, guard_patch("收入多少？", {"supported": True, "metrics": ["gross_sales"]}, cleared))
    assert missing(ambiguous) == ["metrics"]
    narrowed = guard_patch("不要净销售额，只看销售额", {"supported": True, "metrics": ["net_sales", "gross_sales"]}, state)
    assert narrowed["metrics"] == ["gross_sales"]
    assert guard_patch("不要净销售额", {"supported": True}, state)["metrics"] is None


def test_model_protocol_and_api_errors_never_query(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fake-test-key-never-persist")
    from wren_analysis import agent
    monkeypatch.setattr(agent, "load_dotenv", lambda *args, **kwargs: None)
    captured = []
    def handler(request):
        body = json.loads(request.content)
        captured.append(body)
        return httpx.Response(200, json={"model": "mock-only", "usage": {"total_tokens": 15}, "choices": [{
            "finish_reason": "tool_calls", "message": {"tool_calls": [{"function": {
                "name": "update_request", "arguments": '{"supported":true,"metrics":["net_sales"]}'}}]}}]})
    calls = []
    assert extract_patch("净销售额", empty_state(), calls, httpx.MockTransport(handler))["metrics"] == ["net_sales"]
    assert calls[0]["usage"]["total_tokens"] == 15 and captured[0]["tool_choice"] == "required"
    for code in [401, 429, 500]:
        with pytest.raises(ValueError):
            extract_patch("净销售额", empty_state(), [], httpx.MockTransport(lambda request: httpx.Response(code)))
    class NoQuery:
        def summary(self, *args):
            raise AssertionError("Should not query")
    session = AnalysisSession(parser=lambda text, state, calls: {"supported": True}, store=NoQuery())
    session.handle("收入多少？")
    session.handle("/run")
    assert not any("result" in turn for turn in session.trace["turns"])
    assert "fake-test-key-never-persist" not in session.path.read_text(encoding="utf-8")

