from streamlit.testing.v1 import AppTest
from wren_analysis.common import ROOT, MANIFEST_PATH
import pytest


@pytest.mark.skipif(not MANIFEST_PATH.exists(), reason="先构建真实数据")
def test_manual_web_flow_without_api():
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=40).run()
    assert not app.exception
    next(button for button in app.button if button.label == "应用条件").click().run()
    assert not app.exception
    next(button for button in app.button if button.label == "执行查询").click().run()
    assert not app.exception
    session = app.session_state["analysis_session"]
    result = session.trace["turns"][-1]["result"]
    assert result["kind"] == "summary" and result["proof"]["currency"] == "GBP"
    assert not session.trace["turns"][-1]["model_calls"]
    app.selectbox[0].select("订单均值与区间估计")
    next(button for button in app.button if button.label == "应用条件").click().run()
    next(button for button in app.button if button.label == "执行查询").click().run()
    assert not app.exception
    result = app.session_state["analysis_session"].trace["turns"][-1]["result"]
    assert result["kind"] == "invoice_ci" and result["cluster_count"] == 1660
    assert result["ci"] is not None


@pytest.mark.skipif(not MANIFEST_PATH.exists(), reason="先构建真实数据")
def test_failed_update_disables_execution_and_manual_form_recovers():
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=40).run()
    next(button for button in app.button if button.label == "应用条件").click().run()
    session = app.session_state["analysis_session"]
    assert session.can_execute()
    def failure(text, state, calls):
        raise ValueError("simulated API connection failure")
    session.parser = failure
    app.chat_input[0].set_value("估计2011年11月订单金额均值置信区间").run()
    assert not app.exception
    assert next(button for button in app.button if button.label == "执行查询").disabled
    assert session.update_failed
    assert not any("result" in turn for turn in session.trace["turns"])
    next(button for button in app.button if button.label == "应用条件").click().run()
    assert not next(button for button in app.button if button.label == "执行查询").disabled
    assert not session.update_failed
