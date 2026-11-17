"""Prepare a local Git index for the accepted project; never pushes remotely."""
from pathlib import Path
from hashlib import sha256
import json
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
ROOT_FILES = ['.env.example', '.gitignore', '.gitattributes', 'README.md', 'requirements.txt', 'requirements.lock.txt', 'pytest.ini', 'app.py', 'build_data.py', 'prepare_retail.py', 'run_chat.py', 'evaluate_real.py', 'generate_benchmark.py', 'verify_report.py', 'diagnose_api.py', 'accept_release.py', 'prepare_git_repo.py']
DIRECTORIES = ["wren_analysis", "tests", "benchmarks", "docs"]

def git(*args, check=True):
    result = subprocess.run(["git", *args], cwd=ROOT, text=True, encoding="utf-8",
                            errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result

def fail(message):
    raise RuntimeError(message)

def main():
    if not shutil.which("git"):
        fail("Git is not on PATH. Reopen VS Code after installing Git.")
    for name in ROOT_FILES + DIRECTORIES:
        if not (ROOT / name).exists():
            fail("Missing required project path: " + name)
    frozen = json.loads((ROOT / "benchmarks/release_check_v102.json").read_text(encoding="utf-8"))
    expected = {"wren_analysis/" + name: digest
                for name, digest in frozen["frozen_code_sha256"].items()}
    expected["evaluate_real.py"] = frozen["evaluator_sha256"]
    expected["benchmarks/real_cases.json"] = frozen["reference_benchmark_sha256"]
    for name, digest in expected.items():
        if sha256((ROOT / name).read_bytes()).hexdigest() != digest:
            fail("Frozen bytes differ: " + name + ". Keep the working copy; do not overwrite it blindly.")
    parent = git("rev-parse", "--show-toplevel", check=False)
    if parent.returncode == 0 and Path(parent.stdout.strip()).resolve() != ROOT:
        fail("This folder is inside a different Git repository. No index was changed.")
    if parent.returncode == 0:
        committed = git("rev-parse", "--verify", "HEAD", check=False)
        if committed.returncode == 0:
            fail("A commit already exists. Review git status before changing the existing repository.")
        staged = git("diff", "--cached", "--name-only", "-z").stdout.split("\0")
        for name in filter(None, staged):
            if name not in ROOT_FILES and name.split("/", 1)[0] not in DIRECTORIES:
                fail("Previously staged path is outside the publication list: " + name)
    else:
        git("init", "-b", "main")
    git("config", "--local", "user.name", "MengyangLi11")
    git("config", "--local", "user.email", "150634079+MengyangLi11@users.noreply.github.com")
    git("config", "--local", "core.quotepath", "false")
    git("config", "--local", "core.autocrlf", "false")
    sensitive = [".env", ".venv/Scripts/python.exe", "data/raw.xlsx", "reports/private.json", "target/model.json"]
    for name in sensitive:
        if git("check-ignore", "--no-index", "-q", "--", name, check=False).returncode != 0:
            fail("Ignore rule is missing for: " + name)
    git("add", "--", *(ROOT_FILES + DIRECTORIES))
    tracked = git("ls-files", "-z").stdout.split("\0")
    blocked = {"data", "reports", "target", ".venv", "venv", "env", "backups", "__pycache__"}
    for name in filter(None, tracked):
        path = Path(name)
        if set(path.parts) & blocked or (path.name.startswith(".env") and path.name != ".env.example"):
            fail("Unexpected sensitive/generated tracked path: " + name + ". Nothing was committed or pushed.")
    print("Frozen source: PASS")
    print("Credential and generated-path exclusions: PASS")
    print(git("diff", "--cached", "--stat").stdout)
    print("Local index prepared. No commit or remote upload was performed.")
    print('Next: git commit -m "Release verifiable retail analytics agent v1.0.2"')

if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("STOP:", error, file=sys.stderr)
        sys.exit(1)
