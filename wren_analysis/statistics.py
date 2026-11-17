from decimal import Decimal
from hashlib import sha256
import numpy as np

from .common import fingerprint


ASSUMPTIONS = [
    "目标量是筛选范围内、客户编号完整且一致的正向商品发票平均金额，不是净收入或客户平均消费。",
    "同一客户可能有多张发票，因此以客户为簇重采样；不把发票行当独立样本。",
    "区间依赖客户之间近似独立、样本可代表目标交易过程等假设；本数据不能验证这些假设，也不能消除共同时间冲击。",
    "历史样本均值本身是确定的；区间是在上述过程假设下的推断，不表示已记录历史金额存在计算误差。",
    "至少30个客户是本项目的工程门槛，不保证区间覆盖率；本工具不输出因果结论或A/B实验显著性。",
]


def cluster_bootstrap(clusters, n_resamples=1000, confidence=0.95, seed=42):
    if type(n_resamples) is not int or not 200 <= n_resamples <= 5000:
        raise ValueError("重采样次数须为200至5000的整数。")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.8 <= confidence <= 0.99:
        raise ValueError("置信水平须在0.8至0.99之间。")
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("随机种子须为0至2**32-1的整数。")
    if not clusters:
        return {"status": "no_records", "mean": None, "ci": None}
    if any(type(row["amount_cents"]) is not int or row["amount_cents"] <= 0 or
           type(row["invoice_count"]) is not int or row["invoice_count"] < 1 for row in clusters):
        raise ValueError("客户簇金额与发票计数不合法。")
    total_cents = sum(row["amount_cents"] for row in clusters)
    invoices = sum(row["invoice_count"] for row in clusters)
    mean = total_cents / invoices / 100
    if len(clusters) < 30:
        return {"status": "insufficient_clusters", "mean": mean, "ci": None}
    sums = np.asarray([row["amount_cents"] for row in clusters], dtype=np.int64)
    counts = np.asarray([row["invoice_count"] for row in clusters], dtype=np.int64)
    rng = np.random.Generator(np.random.PCG64(seed))
    simulated = []
    for start in range(0, n_resamples, 64):
        indices = rng.integers(0, len(clusters), size=(min(64, n_resamples - start), len(clusters)))
        simulated.extend((sums[indices].sum(axis=1) / counts[indices].sum(axis=1) / 100).tolist())
    tail = (1 - confidence) / 2
    low, high = np.quantile(simulated, [tail, 1 - tail], method="linear").tolist()
    return {"status": "ok", "mean": mean, "ci": [low, high]}


def invoice_ci(store, scope, n_resamples=1000, confidence=0.95, seed=42):
    proof = store.invoice_rows(scope)
    included, excluded, by_customer = [], 0, {}
    for row in proof["rows"]:
        if row["customer_count"] != 1 or row["identified_line_count"] != row["line_count"] or not row["customer_id"]:
            excluded += 1
            continue
        included.append({"order_id": row["order_id"], "customer_id": row["customer_id"], "amount": row["invoice_amount"]})
        amount_cents = int(Decimal(row["invoice_amount"]) * 100)
        cluster = by_customer.setdefault(row["customer_id"], {"customer_id": row["customer_id"], "amount_cents": 0, "invoice_count": 0})
        cluster["amount_cents"] += amount_cents
        cluster["invoice_count"] += 1
    clusters = [by_customer[key] for key in sorted(by_customer)]
    result = cluster_bootstrap(clusters, n_resamples, confidence, seed)
    return {"kind": "invoice_ci", **result, "scope": dict(scope), "currency": "GBP",
            "invoice_count": len(included), "cluster_count": len(clusters),
            "excluded_unidentified_or_inconsistent_invoices": excluded,
            "method": "customer-cluster percentile bootstrap", "n_resamples": n_resamples,
            "confidence": confidence, "seed": seed, "rng": "numpy.default_rng/PCG64",
            "numpy_version": np.__version__, "clusters": clusters, "sample_sha256": fingerprint(included),
            "included_invoices": included, "assumptions": ASSUMPTIONS, "proof": proof}
