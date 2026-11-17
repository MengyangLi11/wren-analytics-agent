import argparse
from copy import deepcopy
from decimal import Decimal
from math import isclose, isfinite
from time import perf_counter
import uuid

from wren_analysis.agent import AnalysisSession, missing, scope_of
from wren_analysis.common import ROOT, read_json, save_json, fingerprint, file_hash


def grade(case, session):
    expected = case["expected"]
    turns = session.trace["turns"]
    results = [turn["result"] for turn in turns if "result" in turn]
    if expected["kind"] == "unsupported":
        return [] if turns[0]["status"] == "error" and not results else ["不支持的任务未被阻止"]
    if turns[0]["status"] != "ok":
        return ["条件提取失败：" + str(turns[0].get("error"))]
    if expected["kind"] == "clarification":
        return [] if set(missing(session.state)) == set(expected["fields"]) and not results else ["澄清字段或查询行为错误"]
    if not results:
        return ["没有成功分析结果"]
    if turns[-1]["status"] != "ok":
        return ["执行或报告生成失败"]
    actual = results[-1]
    errors = []
    for key in ("kind", "status", "scope"):
        if actual.get(key) != expected[key]:
            errors.append(key + "不匹配")
    if actual["proof"]["currency"] != "GBP":
        errors.append("币种错误")
    if expected["kind"] == "summary":
        if actual["event_count"] != expected["event_count"]:
            errors.append("发票行数错误")
        if set(actual["requested_metrics"]) != set(expected["requested_metrics"]):
            errors.append("指标选择错误")
        if expected["metrics"] is None:
            if actual["metrics"] is not None:
                errors.append("无记录被错误当成零金额")
        elif actual["metrics"] is None or set(actual["metrics"]) != set(expected["metrics"]):
            errors.append("结果字段错误")
        elif any(Decimal(str(actual["metrics"][key])) != Decimal(value) for key, value in expected["metrics"].items()):
            errors.append("金额与独立参考不一致")
        if actual["metrics"] is not None:
            answer = turns[-1]["answer"]
            from wren_analysis.common import LABELS
            if any(LABELS[key] + "：" + str(value) + " GBP" not in answer for key, value in actual["metrics"].items()):
                errors.append("报告金额与工具不一致")
    else:
        for key in ("invoice_count", "cluster_count", "excluded_unidentified_or_inconsistent_invoices"):
            if actual[key] != expected[key]:
                errors.append(key + "不一致")
        if expected["mean"] is None:
            if actual["mean"] is not None:
                errors.append("无有效样本仍返回均值")
        elif actual["mean"] is None or not isclose(actual["mean"], expected["mean"], abs_tol=1e-9):
            errors.append("均值与独立参考不一致")
        if expected["status"] != "ok" and actual["ci"] is not None:
            errors.append("样本不足仍返回区间")
        if expected["status"] == "ok" and (actual["ci"] is None or not all(isfinite(value) for value in actual["ci"]) or actual["ci"][0] > actual["ci"][1]):
            errors.append("区间不合法")
    return errors


def evaluate(mode="offline", split="development", limit=None, suite="regression", ids=None):
    benchmark = ROOT / "benchmarks" / ("real_cases.json" if suite == "regression" else "release_check_v102.json")
    data = read_json(benchmark)
    cases = [case for case in data["cases"] if split == "all" or case["split"] == split]
    if ids:
        known = {case["id"] for case in data["cases"]}
        if set(ids) - known:
            raise ValueError("未知用例ID：" + ",".join(sorted(set(ids) - known)))
        cases = [case for case in cases if case["id"] in ids]
    if limit is not None:
        cases = cases[:limit]
    if not cases:
        raise ValueError("没有选中的用例，请检查split和ids。")
    report = {"suite": data["suite"], "mode": mode, "split": split,
              "evaluation_role": "已用于调试的固定回归集" if suite == "regression" else "修复后冻结的新措辞检查集；实际表现以首次真实API运行记录为准",
              "interpretation": "模拟模型条件修改，仅证明协议/工具回归，不代表真实模型准确率" if mode == "offline" else "真实API端到端评测；每题新会话",
              "benchmark_sha256": file_hash(benchmark),
              "code_sha256": {path.name: file_hash(path) for path in (ROOT / "wren_analysis").glob("*.py")}, "cases": []}
    path = ROOT / "reports" / f"eval_real_{mode}_{uuid.uuid4().hex}.json"
    for case in cases:
        parser = None if mode == "live" else lambda text, state, calls, patch=case["patch"]: deepcopy(patch)
        session = AnalysisSession(parser=parser)
        start = perf_counter()
        session.handle(case["question"])
        session.handle("/run")
        reasons = grade(case, session)
        calls = [call for turn in session.trace["turns"] for call in turn["model_calls"]]
        report["cases"].append({"id": case["id"], "passed": not reasons, "reasons": reasons,
            "question": case["question"], "actual_state": deepcopy(session.state),
            "first_turn_error": session.trace["turns"][0].get("error"),
            "model_tool_arguments": [call["model_tool_arguments"] for call in calls if "model_tool_arguments" in call],
            "seconds": round(perf_counter() - start, 3), "model_calls": len(calls),
            "tokens": sum((call.get("usage") or {}).get("total_tokens", 0) for call in calls),
            "expected": case["expected"], "session_report": session.path.relative_to(ROOT).as_posix()})
        save_json(path, report)
        print(f"[{'PASS' if not reasons else 'FAIL'}] {case['id']} " + "; ".join(reasons), flush=True)
    report["summary"] = {"completed": len(cases), "passed": sum(row["passed"] for row in report["cases"]),
                         "total_model_calls": sum(row["model_calls"] for row in report["cases"]),
                         "total_tokens": sum(row["tokens"] for row in report["cases"]),
                         "elapsed_seconds": sum(row["seconds"] for row in report["cases"])}
    save_json(path, report)
    print(report["summary"])
    print(report["interpretation"])
    print("报告：", path)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    parser.add_argument("--split", choices=["development", "validation", "all"], default="development")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--suite", choices=["regression", "release_check"], default="regression")
    parser.add_argument("--ids", nargs="+")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("limit须大于0")
    if args.mode == "live" and args.limit is None:
        args.limit = 3
    report = evaluate(args.mode, args.split, args.limit, args.suite, args.ids)
    if report["summary"]["passed"] != report["summary"]["completed"]:
        raise SystemExit(1)
