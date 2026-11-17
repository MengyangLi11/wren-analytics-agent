from accept_release import run_acceptance


def test_acceptance_stops_before_new_checks_when_regression_fails(tmp_path):
    calls = []
    def evaluate(**options):
        calls.append(options)
        count = options["limit"]
        return {"summary": {"completed": count, "passed": count if count == 4 else count - 1}}
    report = run_acceptance(evaluator=evaluate, unit_check=lambda: True, report_path=tmp_path / "acceptance.json")
    assert report["status"] == "failed" and report["failed_phase"] == "regression"
    assert [options["limit"] for options in calls] == [4, 50]


def test_acceptance_runs_three_separate_phases_on_success(tmp_path):
    calls = []
    def evaluate(**options):
        calls.append(options)
        return {"summary": {"completed": options["limit"], "passed": options["limit"]}}
    report = run_acceptance(evaluator=evaluate, unit_check=lambda: True, report_path=tmp_path / "acceptance.json")
    assert report["status"] == "passed"
    assert [options["limit"] for options in calls] == [4, 50, 20]
    assert calls[-1]["suite"] == "release_check"
