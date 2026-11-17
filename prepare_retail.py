from pathlib import Path
from hashlib import sha256
from zipfile import ZipFile
from datetime import datetime, timezone
from importlib.metadata import version
import json
import uuid

import httpx
import pandas as pd


ROOT = Path(__file__).resolve().parent
DATASET_DIR = ROOT / "data" / "online_retail"
RAW_DIR = DATASET_DIR / "raw"
STAGING_DIR = DATASET_DIR / "staging"

URL = (
    "https://archive.ics.uci.edu/static/public/"
    "352/online+retail.zip"
)

XLSX_SHA256 = (
    "43465a06f2ccf7c8b5bd2892bc7defb52"
    "f97487934fe93b16ae4c3936424676d"
)

COLUMNS = [
    "InvoiceNo",
    "StockCode",
    "Description",
    "Quantity",
    "InvoiceDate",
    "UnitPrice",
    "CustomerID",
    "Country",
]


def obtain_workbook():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    workbook = RAW_DIR / "Online Retail.xlsx"

    if workbook.exists():
        actual_hash = sha256(workbook.read_bytes()).hexdigest()

        if actual_hash != XLSX_SHA256:
            raise ValueError(
                "现有Excel与固定源文件指纹不一致；"
                "已停止，请保留文件并核对来源。"
            )

        print("复用本地原始Excel，文件指纹一致。", flush=True)
        return workbook

    archive = RAW_DIR / "online_retail.zip"

    if not archive.exists():
        temporary = archive.with_suffix(".part")
        print("从UCI下载约23MB原始压缩包……", flush=True)

        with httpx.stream(
            "GET",
            URL,
            follow_redirects=True,
            timeout=60.0,
        ) as response:
            response.raise_for_status()

            with temporary.open("wb") as file:
                for block in response.iter_bytes(
                    chunk_size=1024 * 1024
                ):
                    file.write(block)

        temporary.replace(archive)

    with ZipFile(archive) as bundle:
        content = bundle.read("Online Retail.xlsx")

    if sha256(content).hexdigest() != XLSX_SHA256:
        raise ValueError(
            "下载文件与已核验版本不一致；"
            "已停止，尚未生成分析数据。"
        )

    workbook.write_bytes(content)

    print(
        "原始Excel下载完成，文件指纹校验通过。",
        flush=True,
    )
    return workbook


def audit_frame(frame):
    if list(frame.columns) != COLUMNS or len(frame) != 541909:
        raise ValueError("源数据列名或行数与已核验版本不一致。")

    quantity = pd.to_numeric(
        frame["Quantity"], errors="coerce"
    )
    price = pd.to_numeric(
        frame["UnitPrice"], errors="coerce"
    )
    dates = pd.to_datetime(
        frame["InvoiceDate"], errors="coerce"
    )

    cancelled = (
        frame["InvoiceNo"]
        .str.upper()
        .str.startswith("C", na=False)
    )

    standard_sku = frame["StockCode"].str.fullmatch(
        r"\d{5}[A-Za-z]*",
        na=False,
    )

    special_codes = (
        frame.loc[~standard_sku, "StockCode"]
        .value_counts()
        .head(20)
    )

    return {
        "raw_rows": len(frame),
        "columns": list(frame.columns),
        "date_min": str(dates.min()),
        "date_max": str(dates.max()),
        "distinct_invoice_count": int(
            frame["InvoiceNo"].nunique()
        ),
        "missing_by_column": {
            key: int(value)
            for key, value in frame.isna().sum().items()
        },
        "identical_extra_row_candidates": int(
            frame.duplicated(
                subset=COLUMNS,
                keep="first",
            ).sum()
        ),
        "cancel_invoice_rows": int(cancelled.sum()),
        "negative_quantity_rows": int(
            (quantity < 0).sum()
        ),
        "negative_quantity_without_cancel_prefix": int(
            ((quantity < 0) & ~cancelled).sum()
        ),
        "cancel_prefix_with_nonnegative_quantity": int(
            (cancelled & (quantity >= 0)).sum()
        ),
        "zero_quantity_rows": int(
            (quantity == 0).sum()
        ),
        "zero_unit_price_rows": int(
            (price == 0).sum()
        ),
        "negative_unit_price_rows": int(
            (price < 0).sum()
        ),
        "unparseable_quantity_rows": int(
            quantity.isna().sum()
        ),
        "unparseable_unit_price_rows": int(
            price.isna().sum()
        ),
        "unparseable_date_rows": int(
            dates.isna().sum()
        ),
        "nonstandard_stock_code_top20": {
            str(key): int(value)
            for key, value in special_codes.items()
        },
    }


def main():
    workbook = obtain_workbook()

    print(
        "正在读取并审计Excel；"
        "此步骤可能需要几十秒，请等待……",
        flush=True,
    )

    text_columns = [
        "InvoiceNo",
        "StockCode",
        "Description",
        "CustomerID",
        "Country",
    ]

    frame = pd.read_excel(
        workbook,
        engine="openpyxl",
        dtype={
            key: "string"
            for key in text_columns
        },
    )

    audit = audit_frame(frame)

    # 保留所有原始行，先识别候选问题。
    # source_row对应Excel原始行号，可追溯每条记录。
    frame.insert(0, "source_row", frame.index + 2)

    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    parquet = STAGING_DIR / "raw.parquet"
    temporary = STAGING_DIR / "raw.tmp.parquet"

    frame.to_parquet(
        temporary,
        index=False,
        engine="pyarrow",
    )
    temporary.replace(parquet)

    report = {
        "dataset_id": "uci-online-retail-352",
        "stage": "raw_audit_only",
        "generated_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "source_url": (
            "https://archive.ics.uci.edu/"
            "dataset/352/online+retail"
        ),
        "download_url": URL,
        "citation": (
            "Chen, D. (2015). Online Retail [Dataset]. "
            "UCI Machine Learning Repository. "
            "https://doi.org/10.24432/C5BW33"
        ),
        "license": "CC BY 4.0",
        "currency": "GBP",
        "raw_file_sha256": XLSX_SHA256,
        "staging_file_sha256": sha256(
            parquet.read_bytes()
        ).hexdigest(),
        "staging_path": parquet.relative_to(
            ROOT
        ).as_posix(),
        "source_row_definition": (
            "原始Excel工作表行号；"
            "表头在第1行，数据从第2行开始。"
        ),
        "row_policy": (
            "保留原始全部行；未去重、未删除缺失值、"
            "未将取消记录解释为已到账退款。"
        ),
        "versions": {
            key: version(key)
            for key in (
                "pandas",
                "openpyxl",
                "pyarrow",
            )
        },
        "audit": audit,
    }

    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)

    report_path = (
        reports / f"retail_audit_{uuid.uuid4().hex}.json"
    )

    report_path.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("\n真实数据审计完成；未调用模型API。")
    print("原始行数：", audit["raw_rows"])
    print(
        "日期范围：",
        audit["date_min"],
        "至",
        audit["date_max"],
    )
    print("金额单位：GBP（英镑）")
    print(
        "缺少CustomerID的行数：",
        audit["missing_by_column"]["CustomerID"],
    )
    print(
        "完全相同行的额外副本候选：",
        audit["identical_extra_row_candidates"],
    )
    print(
        "C开头的取消记录行数：",
        audit["cancel_invoice_rows"],
    )
    print(
        "负数量但非C开头的行数：",
        audit["negative_quantity_without_cancel_prefix"],
    )
    print(
        "零单价行数：",
        audit["zero_unit_price_rows"],
    )
    print(
        "负单价行数：",
        audit["negative_unit_price_rows"],
    )
    print("原始数据缓存：", parquet)
    print("审计报告：", report_path)
    print(
        "本阶段已保留所有原始行，"
        "尚未切换Agent的数据源。"
    )


if __name__ == "__main__":
    main()