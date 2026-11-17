"""依次验收：自动检查→已知故障复测→50题回归→新20题；失败立即停止。"""
import subprocess
import sys
import uuid

from evaluate_real import evaluate
from wren_analysis.common import ROOT, read_json, save_json, file_hash


def run_acceptance(evaluator=None, unit_check=None, report_path=None):
    injected = evaluator is not None
    evaluator = evaluator or evaluate
    unit_check = unit_check or (lambda: subprocess.run(
        [sys.executable, "-m", "pytest", "-q"], cwd=ROOT).returncode == 0)
    check = read_json(ROOT / "benchmarks/release_check_v102.json")
    for name, digest in check["frozen_code_sha256"].items():
        if file_hash(ROOT / "wren_analysis" / name) != digest:
            raise ValueError("运行代码与新增检查集冻结版本不同：" + name)
    if file_hash(ROOT / "evaluate_real.py") != check["evaluator_sha256"]:
        raise ValueError("评测器与新增检查集冻结版本不同。")
    report = {"version": "1.0.2", "status": "running", "phases": {},
              "execution_mode": "injected_test" if injected else "live",
              "note": "4题故障复测与50题回归有重叠，不合并为独立样本准确率。", "unit_tests_passed": False}
    path = report_path or ROOT / "reports" / f"acceptance_v102_{uuid.uuid4().hex}.json"
    save_json(path, report)
    if not unit_check():
        report.update(status="failed", failed_phase="unit_tests")
        save_json(path, report)
        print("自动检查失败，未调用真实API。报告：", path)
        return report
    report["unit_tests_passed"] = True
    phases = [
        ("known_failures", {"mode": "live", "split": "all", "limit": 4, "suite": "regression", "ids": ["C04", "C09", "C07", "B01"]}),
        ("regression", {"mode": "live", "split": "all", "limit": 50, "suite": "regression"}),
        ("new_wording_check", {"mode": "live", "split": "validation", "limit": 20, "suite": "release_check"}),
    ]
    try:
        for name, options in phases:
            print("\n开始：", name, flush=True)
            phase = evaluator(**options)
            report["phases"][name] = phase
            summary = phase["summary"]
            if summary["completed"] != options["limit"] or summary["passed"] != summary["completed"]:
                report.update(status="failed", failed_phase=name)
                save_json(path, report)
                print("本阶段有失败，已停止后续阶段。报告：", path)
                return report
            save_json(path, report)
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=str(error))
    except KeyboardInterrupt:
        report["status"] = "interrupted"
    finally:
        save_json(path, report)
    for name, phase in report["phases"].items():
        print(name, phase["summary"])
    print("验收状态：", report["status"])
    print("汇总报告：", path)
    return report


if __name__ == "__main__":
    report = run_acceptance()
    if report["status"] != "passed":
        raise SystemExit(1)
