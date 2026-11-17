from pathlib import Path
from hashlib import sha256
import json

ROOT = Path(__file__).resolve().parents[1]
RETAIL_DIR = ROOT / "data" / "retail"
DB_PATH = RETAIL_DIR / "retail.duckdb"
MANIFEST_PATH = RETAIL_DIR / "manifest.json"
CONTRACT_PATH = ROOT / "configs" / "retail_metrics.json"
MDL_PATH = ROOT / "target" / "retail_mdl.json"
LABELS = {"gross_sales": "正向开票金额", "cancel_amount": "取消冲减金额", "net_sales": "净开票金额"}


def fingerprint(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), default=str).encode()).hexdigest()


def file_hash(path):
    digest = sha256()
    with Path(path).open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))
