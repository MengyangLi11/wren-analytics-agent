import argparse

from wren_analysis.common import read_json, fingerprint
from wren_analysis.store import RetailStore
from wren_analysis.statistics import invoice_ci
from wren_analysis.agent import show_state, render, scope_of


def verify(trace):
    store = RetailStore()
    checked = 0
    for turn in trace.get("turns", []):
        if "result" not in turn:
            continue
        saved = turn["result"]
        snapshot = store.snapshot()
        for name in ("source_sha256", "database_sha256", "definitions_sha256", "mdl_sha256"):
            if snapshot[name] != saved["proof"][name]:
                raise ValueError("当前数据/口径与证据快照不一致：" + name)
        if saved["kind"] == "summary":
            actual = store.summary(saved["scope"], saved["requested_metrics"])
        elif saved["kind"] == "invoice_ci":
            actual = invoice_ci(store, saved["scope"], saved["n_resamples"], saved["confidence"], saved["seed"])
        else:
            raise ValueError("未知的结果类型。")
        if fingerprint(actual) != fingerprint(saved):
            raise ValueError("重算结果与证据不同，可能存在数据、环境或证据改动。")
        if scope_of(turn["state_after"]) != actual["scope"]:
            raise ValueError("显示条件与实际计算范围不一致。")
        if turn["state_after"]["analysis"] != actual["kind"]:
            raise ValueError("显示任务与实际计算类型不一致。")
        if actual["kind"] == "summary" and set(turn["state_after"]["metrics"] or []) != set(actual["requested_metrics"]):
            raise ValueError("显示指标与实际计算指标不一致。")
        if turn["answer"] != show_state(turn["state_after"]) + "\n\n" + render(actual):
            raise ValueError("报告文本与重算结果不一致。")
        checked += 1
    if not checked:
        raise ValueError("证据中没有可重算的成功分析。")
    return checked


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="从类型化参数重建SQL并重算，检查证据完整性。")
    parser.add_argument("report")
    args = parser.parse_args()
    print(f"核验通过：{verify(read_json(args.report))} 次分析与当前数据快照一致。")
