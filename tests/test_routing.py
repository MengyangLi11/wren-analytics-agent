from copy import deepcopy

import httpx
import json
import pytest

from wren_analysis.agent import AnalysisSession, empty_state, extract_patch, merge_patch, missing
from wren_analysis.common import ROOT, read_json
from wren_analysis.policy import guard_patch


@pytest.mark.parametrize("identifier", ["C04", "C07", "C09", "B01"])
def test_real_failure_supported_false_uses_capability_contract(identifier):
    from evaluate_real import grade
    case = next(case for case in read_json(ROOT / "benchmarks/real_cases.json")["cases"] if case["id"] == identifier)
    session = AnalysisSession(parser=lambda text, state, calls: {"supported": False})
    session.handle(case["question"])
    assert session.trace["turns"][0]["status"] == "ok"
    session.handle("/run")
    assert grade(case, session) == []


@pytest.mark.parametrize("text,fields", [
    ("请汇总所有历史数据的营收", ["metrics"]),
    ("统计2010年0月销售金额", ["time_range"]),
    ("我想查上周的净开票金额", ["time_range"]),
    ("给出销售额，期间还未确定", ["time_range"]),
])
def test_missing_information_is_not_unsupported(text, fields):
    updated = merge_patch(empty_state(), guard_patch(text, {"supported": False}, empty_state()))
    assert set(missing(updated)) == set(fields)


@pytest.mark.parametrize("text", [
    "估计2011年11月转化率的置信区间", "全期净销售额的置信区间", "预测明年销售额",
    "查询全期利润", "全期销售额和到账退款", "比较德国和法国销售额",
])
def test_capability_recovery_does_not_expand_supported_tasks(text):
    with pytest.raises(ValueError):
        guard_patch(text, {"supported": False}, empty_state())


def test_supported_false_cannot_drop_explicit_filters():
    state = empty_state()
    patch = guard_patch("2011年11月德国的销售额", {"supported": False}, state)
    assert patch["country"] == "Germany"
    assert patch["time_range"]["start_date"] == "2011-11-01"
    patch = guard_patch("全期536365发票净销售额", {"supported": False}, state)
    assert patch["order_id"] == "536365"
    with pytest.raises(ValueError, match="国家筛选尚未确认"):
        guard_patch("全期国家为毛里求斯的销售额", {"supported": False}, state)
    patch = guard_patch("全期发票号为Germany的净销售额", {"supported": False}, state)
    assert patch["order_id"] == "Germany" and "country" not in patch


def test_invoice_average_alias_and_noop_scope():
    state = empty_state()
    patch = guard_patch("2011年7月平均发票金额的区间估计", {"supported": False}, state)
    assert patch["analysis"] == "invoice_ci"
    updated = merge_patch(state, patch)
    assert not missing(updated)
    with pytest.raises(ValueError):
        guard_patch("你好", {"supported": False}, state)


def test_unresolved_subyear_is_not_coerced_to_whole_year():
    updated = merge_patch(empty_state(), guard_patch("2011年十一月净销售额", {"supported": False}, empty_state()))
    assert missing(updated) == ["time_range"]
    updated = merge_patch(empty_state(), guard_patch("2011年按发生日期统计净销售额", {"supported": True}, empty_state()))
    assert updated["time_range"] == {"all_dates": False, "start_date": "2011-01-01", "end_date": "2011-12-31"}
    updated = merge_patch(empty_state(), guard_patch("2011年上半年净销售额", {"supported": True}, empty_state()))
    assert missing(updated) == ["time_range"]


def test_excluding_country_is_not_coerced_to_including_it():
    with pytest.raises(ValueError, match="排除条件"):
        guard_patch("全期不要德国的净销售额", {"supported": False}, empty_state())


def test_quoted_invoice_anchor_preserves_filter():
    patch = guard_patch("针对'536365'这张发票，全期净销售额", {"supported": False}, empty_state())
    assert patch["order_id"] == "536365"


def test_unspecified_date_does_not_invent_country_filter():
    patch = guard_patch("日期未指定的净销售额", {"supported": False}, empty_state())
    assert "country" not in patch
    assert patch["time_range"] is None


@pytest.mark.parametrize("text", ["请解释全期净销售额的定义", "2011年11月销售额为什么变动", "我想知道正向开票金额的公式"])
def test_known_metric_does_not_turn_other_intents_into_amount_query(text):
    with pytest.raises(ValueError):
        guard_patch(text, {"supported": False}, empty_state())


@pytest.mark.parametrize("value", [None, "false", 0])
def test_malformed_supported_is_not_silently_coerced(value):
    with pytest.raises(ValueError, match="布尔值"):
        guard_patch("净销售额多少", {"supported": value}, empty_state())


def test_native_model_arguments_are_saved_before_guard(monkeypatch):
    from wren_analysis import agent
    monkeypatch.setattr(agent, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fake-routing-key")
    original = {"supported": False}
    def handler(request):
        return httpx.Response(200, json={"usage": {}, "choices": [{"finish_reason": "tool_calls", "message": {
            "tool_calls": [{"function": {"name": "update_request", "arguments": json.dumps(original)}}]}}]})
    calls = []
    native = extract_patch("收入多少", empty_state(), calls, httpx.MockTransport(handler))
    guarded = guard_patch("收入多少", native, empty_state())
    assert calls[-1]["model_tool_arguments"] == original
    assert guarded["supported"] is True
