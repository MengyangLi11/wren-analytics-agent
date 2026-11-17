"""有限数据域的筛选锚定；无法确认的显式筛选不能被静默忽略。"""
import re
import unicodedata

COUNTRIES = {
    "Australia": ("澳大利亚", "澳洲"), "Austria": ("奥地利",), "Bahrain": ("巴林",),
    "Belgium": ("比利时",), "Brazil": ("巴西",), "Canada": ("加拿大",),
    "Channel Islands": ("海峡群岛",), "Cyprus": ("塞浦路斯",), "Czech Republic": ("捷克",),
    "Denmark": ("丹麦",), "EIRE": ("爱尔兰", "Ireland"), "European Community": ("欧洲共同体",),
    "Finland": ("芬兰",), "France": ("法国",), "Germany": ("德国",), "Greece": ("希腊",),
    "Hong Kong": ("香港",), "Iceland": ("冰岛",), "Israel": ("以色列",), "Italy": ("意大利",),
    "Japan": ("日本",), "Lebanon": ("黎巴嫩",), "Lithuania": ("立陶宛",), "Malta": ("马耳他",),
    "Netherlands": ("荷兰",), "Norway": ("挪威",), "Poland": ("波兰",), "Portugal": ("葡萄牙",),
    "RSA": ("南非", "South Africa"), "Saudi Arabia": ("沙特阿拉伯", "沙特"),
    "Singapore": ("新加坡",), "Spain": ("西班牙",), "Sweden": ("瑞典",),
    "Switzerland": ("瑞士",), "USA": ("美国", "United States"),
    "United Arab Emirates": ("阿联酋",), "United Kingdom": ("英国", "UK"), "Unspecified": ("未指定国家", "国家未指定"),
}
ORDER_CLEAR = r"全部订单|所有订单|全部发票|所有发票|取消订单筛选|取消发票筛选|不筛选订单|不限订单|去掉订单"
COUNTRY_CLEAR = r"全部国家|所有国家|取消国家|不限国家|不筛选国家"


def anchor_filters(text, patch):
    raw = unicodedata.normalize("NFKC", text)
    compact = re.sub(r"\s+", "", raw)
    prefix = r"(?:订单号|发票号|订单编号|发票编号|order_id)\s*(?:为|是|=|:)?\s*[\"'“‘]?"
    orders = set(re.findall(prefix + r"([a-zA-Z0-9_/-]+)", raw))
    orders.update(re.findall(r"(?<![a-zA-Z0-9])([a-zA-Z]?\d{6,})\s*(?:订单|发票)", raw))
    orders.update(re.findall(r"(?:订单|发票)\s*(?:为|是|=|:)?\s*[\"'“‘]?([a-zA-Z]?\d{6,})", raw))
    orders.update(re.findall(r"[\"'“‘]([a-zA-Z]?\d{6,})[\"'”’]\s*(?:这张|这笔|该)?\s*(?:订单|发票)", raw))
    if len(orders) > 1:
        raise ValueError("当前每次只支持一个发票号；请明确筛选条件。")
    clear_order = bool(re.search(ORDER_CLEAR, compact))
    if orders and clear_order:
        raise ValueError("同时出现发票号和取消筛选，请明确选择。")
    if orders:
        patch["order_id"] = next(iter(orders))
    elif clear_order:
        patch["order_id"] = None
    elif re.search(r"订单号|发票号|订单编号|发票编号|order_id", raw):
        value = patch.get("order_id")
        if not isinstance(value, str) or not value or value.casefold() not in raw.casefold():
            raise ValueError("显式发票筛选尚未确认，请提供发票号或使用手动条件表单。")

    country_text = raw.casefold()
    country_text = re.sub(r"(?:订单号?|发票号?|order_id)\s*(?:为|是|=|:)?\s*[\"'“‘].*?[\"'”’]", " ", country_text)
    country_text = re.sub(r"(?:订单号?|发票号?|order_id)\s*(?:为|是|=|:)?\s*[a-z0-9_/-]+", " ", country_text)
    found = set()
    for country, aliases in COUNTRIES.items():
        if country == "Unspecified" and not re.search(r"国家|country", country_text):
            continue
        for alias in (country, *aliases):
            pattern = re.escape(alias.casefold()).replace(r"\ ", r"\s*")
            if alias.isascii():
                pattern = r"(?<![a-z])" + pattern + r"(?![a-z])"
            if re.search(r"(?:不要|不看|排除)\s*" + pattern, country_text) or re.search(pattern + r"(?:以外|之外|除外)", country_text):
                raise ValueError("当前仅支持单个国家的包含筛选；排除条件请先改为明确的国家筛选。")
            if re.search(pattern, country_text):
                found.add(country)
    if len(found) > 1:
        raise ValueError("当前每次只支持一个国家筛选；多国比较暂不支持。")
    clear_country = bool(re.search(COUNTRY_CLEAR, compact))
    if found and clear_country:
        raise ValueError("同时出现国家名称和取消筛选，请明确选择。")
    if found:
        patch["country"] = next(iter(found))
    elif clear_country:
        patch["country"] = None
    elif re.search(r"国家(?:为|是|=|:)|只看.*国家", compact):
        value = patch.get("country")
        if not isinstance(value, str) or not value or value.casefold() not in raw.casefold():
            raise ValueError("显式国家筛选尚未确认，请使用手动条件表单。")
    return patch
