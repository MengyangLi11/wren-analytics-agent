from copy import deepcopy
import pytest

from wren_analysis.agent import AnalysisSession
from wren_analysis.common import MANIFEST_PATH
from verify_report import verify


@pytest.mark.skipif(not MANIFEST_PATH.exists(), reason="先构建真实数据")
def test_evidence_replay_and_tamper_detection():
    session = AnalysisSession()
    session.set_conditions({"metrics": ["net_sales"], "time_range": {"all_dates": True}})
    session.handle("/run")
    assert verify(session.trace) == 1
    altered = deepcopy(session.trace)
    altered["turns"][-1]["result"]["metrics"]["net_sales"] = "0.00"
    with pytest.raises(ValueError):
        verify(altered)
    altered_text = deepcopy(session.trace)
    altered_text["turns"][-1]["answer"] = "净开票金额：0.00 GBP"
    with pytest.raises(ValueError):
        verify(altered_text)
    altered_task = deepcopy(session.trace)
    altered_task["turns"][-1]["state_after"]["analysis"] = "invoice_ci"
    with pytest.raises(ValueError):
        verify(altered_task)
    session.set_conditions({"analysis": "invoice_ci", "time_range": {
        "all_dates": False, "start_date": "2011-11-01", "end_date": "2011-11-30"}})
    session.handle("/run")
    assert session.trace["turns"][-1]["result"]["kind"] == "invoice_ci"
    assert verify(session.trace) == 2
