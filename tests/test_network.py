from copy import deepcopy
import json

import httpx
import pytest

from wren_analysis.agent import AnalysisSession, extract_patch, model_request


@pytest.fixture(autouse=True)
def local_api_config(monkeypatch):
    from wren_analysis import agent
    monkeypatch.setattr(agent, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fake-network-test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")


def success():
    return httpx.Response(200, json={"model": "mock", "usage": {"total_tokens": 12},
        "choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{"function": {
            "name": "update_request", "arguments": json.dumps({"supported": True, "metrics": ["net_sales"]})}}]}}]})


@pytest.mark.parametrize("kind", [httpx.ConnectError, httpx.ConnectTimeout])
def test_connection_error_retried_once(kind):
    attempts, calls = [], []
    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise kind("simulated connect failure", request=request)
        return success()
    patch = extract_patch("净销售额", {}, calls, httpx.MockTransport(handler))
    assert patch["metrics"] == ["net_sales"]
    assert len(calls) == len(attempts) == 2
    assert calls[0]["phase"] == "connect" and calls[1]["http_status"] == 200
    assert calls[1]["usage"]["total_tokens"] == 12


def test_connection_retry_is_bounded():
    calls = []
    def handler(request):
        raise httpx.ConnectTimeout("simulated", request=request)
    with pytest.raises(ValueError, match="两次连接尝试"):
        model_request([], calls, transport=httpx.MockTransport(handler))
    assert len(calls) == 2
    assert [call["connection_attempt"] for call in calls] == [1, 2]


def test_read_timeout_is_not_retried():
    calls = []
    def handler(request):
        raise httpx.ReadTimeout("simulated response timeout", request=request)
    with pytest.raises(ValueError, match="未自动重发"):
        model_request([], calls, transport=httpx.MockTransport(handler))
    assert len(calls) == 1 and calls[0]["phase"] == "read"


@pytest.mark.parametrize("status", [401, 429, 503])
def test_http_errors_are_not_retried(status):
    calls = []
    with pytest.raises(ValueError, match=f"HTTP {status}"):
        model_request([], calls, transport=httpx.MockTransport(lambda request: httpx.Response(status)))
    assert len(calls) == 1


class NoQuery:
    def summary(self, *args):
        raise AssertionError("failed update must not execute saved scope")


def failed_parser(text, state, calls):
    raise ValueError("simulated connection failure")


def ready_session():
    session = AnalysisSession(parser=failed_parser, store=NoQuery())
    session.set_conditions({"metrics": ["net_sales"], "time_range": {"all_dates": True}})
    assert session.can_execute()
    return session


def test_failed_update_blocks_saved_query_until_valid_update():
    session = ready_session()
    original = deepcopy(session.state)
    answer = session.handle("估计2011年11月订单均值的置信区间")
    assert session.state == original and session.update_failed
    assert "暂停执行" in answer
    assert "条件已齐全，请核对后执行查询" not in answer
    assert not session.can_execute()
    session.handle("/state")
    assert session.update_failed
    session.handle("/run")
    assert not any("result" in turn for turn in session.trace["turns"])
    session.parser = lambda text, state, calls: {"supported": True}
    session.handle("谢谢")
    assert session.update_failed, "no-op patch must not restore old executable scope"
    session.parser = lambda text, state, calls: {"supported": True, "metrics": ["net_sales"]}
    session.handle("净销售额")
    assert session.can_execute() and session.state == original


def test_manual_recovery_and_reset():
    session = ready_session()
    session.handle("估计订单均值置信区间")
    with pytest.raises(ValueError):
        session.set_conditions({"time_range": {"all_dates": False}})
    assert session.update_failed
    session.set_conditions({"analysis": "invoice_ci", "time_range": {
        "all_dates": False, "start_date": "2011-11-01", "end_date": "2011-11-30"}})
    assert session.can_execute() and session.state["analysis"] == "invoice_ci"
    session.handle("再次修改条件")
    assert session.update_failed
    session.handle("/new")
    assert not session.update_failed and not session.can_execute()
