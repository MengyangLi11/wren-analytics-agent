from calendar import monthrange
from datetime import date
import re
import unicodedata

from .common import LABELS
from .filters import anchor_filters

ALIASES = {
    "gross_sales": ("正向开票金额", "销售金额", "销售额", "gross_sales"),
    "cancel_amount": ("取消冲减金额", "取消金额", "冲减金额", "取消额", "cancel_amount"),
    "net_sales": ("净开票金额", "净销售金额", "净销售额", "销售净额", "净额", "net_sales"),
}


def normalize(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).casefold())


def mask_ids(text):
    text = normalize(text)
    for pattern in [r"(?:订单号?|发票号?|order_id)(?:为|是|=|:)?[\"'“‘].*?[\"'”’]",
                    r"(?:订单号?|发票号?|order_id)(?:为|是|=|:)?[a-z0-9_/-]+"]:
        text = re.sub(pattern, lambda match: " " * len(match.group()), text)
    return text


def metric_signals(text):
    work = mask_ids(text)
    for term in sorted((term for values in ALIASES.values() for term in values), key=len, reverse=True):
        work = re.sub(r"(?:不要|不看|不用)" + re.escape(term), " ", work)
    metric_pattern = "(?:" + "|".join(re.escape(term) for values in ALIASES.values() for term in values) + ")"
    bound = re.sub(r"(?:收入|营收)(?:按照|按|指|是|就是|定义为|口径为)" + metric_pattern, "", work)
    bound = re.sub(r"(?:收入|营收)\(" + metric_pattern + r"\)", "", bound)
    remaining = bound
    recognized = []
    for term, key in sorted(((term, key) for key, values in ALIASES.items() for term in values), key=lambda item: -len(item[0])):
        if term in work:
            if key not in recognized:
                recognized.append(key)
            work = work.replace(term, " ")
        remaining = remaining.replace(term, " ")
    return recognized, bool(re.search(r"收入|营收", remaining))


def explicit_time(text):
    """返回(period, touched)。不能解析或日期无效时period=None，要求用户补充。"""
    text = mask_ids(text)
    if re.search(r"今天|昨天|明天|本月|上月|下月|今年|去年|本周|上周|下周|这周|本季度|最近|近\d+[天日周月]", text):
        return None, True
    short = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})[日号]?[至到~—–-](?:(\d{1,2})月)?(\d{1,2})[日号]", text)
    try:
        if short:
            year, month, day, end_month, end_day = map(lambda item: int(item) if item else None, short.groups())
            start, end = date(year, month, day), date(year, end_month or month, end_day)
        else:
            matches = list(re.finditer(r"(?<![a-z0-9])(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})[日号]?(?!\d)", text))
            if matches:
                values = [date(*map(int, match.groups())) for match in matches]
                if len(values) > 2:
                    return None, True
                start, end = values[0], values[-1]
            else:
                month = re.search(r"(?<![a-z0-9])(\d{4})(?:年(\d{1,2})月|[-/](\d{1,2})(?![-/.\da-z]))", text)
                year = re.search(r"(?<![a-z0-9])(\d{4})年(?:全年)?(?!\d)", text)
                if month:
                    y, m = int(month[1]), int(month[2] or month[3])
                    start, end = date(y, m, 1), date(y, m, monthrange(y, m)[1])
                elif year:
                    if re.search(r"[一二三四五六七八九十零〇两\d]+[月日]|季度|[上下]半年|第[一二三四五六七八九十\d]+周", text[year.end():]):
                        return None, True
                    start, end = date(int(year[1]), 1, 1), date(int(year[1]), 12, 31)
                else:
                    full_pattern = r"全期|全部(?:现有)?(?:交易)?(?:事件|数据|日期|记录)|所有(?:现有|历史)?(?:交易)?(?:事件|数据|记录)|不限日期|不限制日期"
                    full = re.sub(r"(?:不要|不是|不)(?:查询|统计)?(?:" + full_pattern + ")", "", text)
                    if re.search(full_pattern, full):
                        return {"all_dates": True}, True
                    return None, bool(re.search(r"\d+[年月日号]|\d+[-/]\d+|日期|时间范围", text))
        if start > end:
            return None, True
        return {"all_dates": False, "start_date": start.isoformat(), "end_date": end.isoformat()}, True
    except (ValueError, OverflowError):
        return None, True


def guard_patch(text, patch, state):
    if not isinstance(patch, dict):
        raise ValueError("模型未返回条件对象。")
    patch = dict(patch)
    if type(patch.get("supported")) is not bool:
        raise ValueError("模型supported字段必须是布尔值。")
    vocabulary = mask_ids(text)
    if re.search(r"退款|退货金额|利润|成本|库存|预测|因果|原因|为什么|显著|a/b|同比|环比|转化率|相关系数|中位数|标准差|方差", vocabulary):
        raise ValueError("该数据只能核验开票/取消记录与描述性统计，不提供到账退款、利润、预测或实验因果结论。")
    if re.search(r"定义|含义|什么意思|公式|如何计算|怎么算|解释", vocabulary) and not re.search(r"多少|合计|汇总|统计", vocabulary):
        raise ValueError("当前入口执行金额或均值分析；口径定义与公式请在查询与统计证据页查看。")
    recognized, ambiguous = metric_signals(text)
    invoice_mean = bool(re.search(r"(?:订单|发票)(?:商品)?(?:金额)?(?:均值|平均)|平均(?:订单|发票)(?:金额)?|客单价", vocabulary))
    interval_request = bool(re.search(r"置信区间|bootstrap|区间估计", vocabulary))
    if interval_request and not invoice_mean:
        raise ValueError("当前区间工具仅支持正向商品发票金额均值，请明确统计对象。")
    if invoice_mean and set(recognized) - {"gross_sales"}:
        raise ValueError("区间工具分析正向发票均值，暂不支持取消金额或净额均值。")
    # 缺口径、缺/无效日期仍是支持的业务请求；能力边界由程序定义。
    known_request = bool(recognized or ambiguous or invoice_mean or any(term in text for term in ("都要", "三个指标", "三项指标")))
    if not patch["supported"] and not known_request:
        raise ValueError("模型未识别可执行的条件修改，请明确分析任务或使用手动表单。")
    patch["supported"] = True
    patch = anchor_filters(text, patch)
    if ambiguous:
        patch["metrics"] = None
        patch["analysis"] = "summary"
    elif recognized:
        patch["metrics"] = recognized
        if not invoice_mean:
            patch["analysis"] = "summary"
    elif any(term in text for term in ("都要", "三个指标", "三项指标")):
        patch["metrics"] = list(LABELS)
        patch["analysis"] = "summary"
    elif any(re.search(r"(?:不要|不看|不用)" + re.escape(term), normalize(text))
             for values in ALIASES.values() for term in values):
        patch["metrics"] = None
    else:
        patch.pop("metrics", None)
    if invoice_mean:
        patch["analysis"] = "invoice_ci"
    elif "analysis" in patch and not recognized and not ambiguous:
        patch.pop("analysis")
    period, touched = explicit_time(text)
    if touched:
        patch["time_range"] = period
    else:
        patch.pop("time_range", None)
    if "order_id" in patch:
        order = patch["order_id"]
        if order is not None:
            if not isinstance(order, str) or normalize(order) not in normalize(text):
                raise ValueError("新增发票号必须出现在本轮输入中。")
        elif state["order_id"] is not None and not re.search(r"全部订单|所有订单|全部发票|所有发票|取消订单筛选|取消发票筛选|不筛选订单|不限订单|去掉订单", text):
            raise ValueError("未明确取消发票筛选，已保留原条件。")
    if "country" in patch and patch["country"] is None and state["country"] is not None:
        if not re.search(r"全部国家|所有国家|取消国家|不限国家|不筛选国家", text):
            raise ValueError("未明确取消国家筛选，已保留原条件。")
    return patch
