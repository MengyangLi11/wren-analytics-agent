from copy import deepcopy
from typing import TypedDict
import json
import os
import uuid

import httpx
from dotenv import load_dotenv
from langgraph.graph import StateGraph, START, END

from .common import ROOT, LABELS, save_json, file_hash
from .policy import guard_patch
from .statistics import invoice_ci
from .store import RetailStore, validate_scope


def empty_state():
    return {"analysis": "summary", "metrics": None, "time_range": None, "order_id": None, "country": None}


def merge_patch(state, patch):
    if not isinstance(patch, dict) or set(patch) - {"supported", *state}:
        raise ValueError("条件中包含未知字段。")
    if patch.get("supported") is not True:
        raise ValueError("此请求不属于支持的分析任务。")
    updated = deepcopy(state)
    for key, value in patch.items():
        if key != "supported":
            updated[key] = deepcopy(value)
    if updated["analysis"] not in ("summary", "invoice_ci"):
        raise ValueError("未知分析类型。")
    metrics = updated["metrics"]
    if metrics is not None and (not isinstance(metrics, list) or not metrics or
           any(not isinstance(key, str) or key not in LABELS for key in metrics) or len(set(metrics)) != len(metrics)):
        raise ValueError("指标列表不合法。")
    if updated["time_range"] is not None:
        if not isinstance(updated["time_range"], dict) or set(updated["time_range"]) - {"all_dates", "start_date", "end_date"}:
            raise ValueError("时间对象只能包含日期字段，发票与国家必须单独显示。")
        validate_scope(updated["time_range"])
    for key in ("order_id", "country"):
        value = updated[key]
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 80):
            raise ValueError("发票号和国家必须是非空文本。")
    return updated


def missing(state):
    fields = []
    if state["analysis"] == "summary" and state["metrics"] is None:
        fields.append("metrics")
    if state["time_range"] is None:
        fields.append("time_range")
    return fields


def scope_of(state):
    if state["time_range"] is None:
        raise ValueError("缺少时间范围。")
    scope = dict(state["time_range"])
    for key in ("order_id", "country"):
        if state[key] is not None:
            scope[key] = state[key]
    validate_scope(scope)
    return scope


def show_state(state):
    period = state["time_range"]
    time_text = "待确认" if period is None else ("全部现有记录" if period["all_dates"] else period["start_date"] + "至" + period["end_date"])
    metric_text = ("正向商品发票金额均值" if state["analysis"] == "invoice_ci" else
                   ("、".join(LABELS[key] for key in state["metrics"]) if state["metrics"] else "待确认"))
    lines = ["当前查询条件：", "分析：" + ("金额汇总" if state["analysis"] == "summary" else "订单金额均值与客户聚类Bootstrap区间"),
             "指标：" + metric_text,
             "日期：" + time_text, "发票：" + (state["order_id"] or "不筛选"), "国家：" + (state["country"] or "不筛选")]
    if "metrics" in missing(state):
        lines.append("请明确选择正向开票金额、取消冲减金额或净开票金额。")
    if "time_range" in missing(state):
        lines.append("请提供完整起止日期，或明确选择全部现有交易事件。")
    if not missing(state):
        lines.append("条件已齐全，请核对后执行查询。")
    return "\n".join(lines)


def render(result):
    lines = ["单位：GBP（英镑）；口径版本：" + result["proof"]["definition_version"]]
    if result["kind"] == "summary":
        lines.append("计入发票行数：" + str(result["event_count"]))
        if result["status"] == "no_records":
            lines.append("筛选范围内没有记录，不能据此认定真实业务金额为0。")
        else:
            lines.extend(LABELS[key] + "：" + value + " GBP" for key, value in result["metrics"].items())
        lines.append(result["proof"]["definition_snapshot"]["amount_basis"])
    else:
        lines += [f"纳入发票数：{result['invoice_count']}；客户簇数：{result['cluster_count']}",
                  f"客户信息缺失或不一致而排除的发票：{result['excluded_unidentified_or_inconsistent_invoices']}"]
        if result["mean"] is not None:
            lines.append(f"正向发票金额均值：{result['mean']:.2f} GBP")
        if result["ci"] is not None:
            lines.append(f"{result['confidence']:.0%}客户聚类Bootstrap区间：[{result['ci'][0]:.2f}, {result['ci'][1]:.2f}] GBP")
        else:
            lines.append("客户簇不足30或无有效样本，未输出置信区间。")
        lines.extend(result["assumptions"])
    lines.append("本结果由程序计算，SQL、口径和数据指纹已随证据保存。")
    return "\n".join(lines)


PATCH_TOOL = {"type": "function", "function": {"name": "update_request", "description": "只提交本轮明确修改的字段，省略字段会保留。",
    "parameters": {"type": "object", "additionalProperties": False, "required": ["supported"], "properties": {
        "supported": {"type": "boolean", "description": "业务能力是否支持；缺口径、缺日期、无效/相对日期时仍为true，用null请求澄清。"}, "analysis": {"type": "string", "enum": ["summary", "invoice_ci"]},
        "metrics": {"anyOf": [{"type": "array", "items": {"type": "string", "enum": list(LABELS)}, "minItems": 1, "uniqueItems": True}, {"type": "null"}]},
        "time_range": {"anyOf": [{"type": "object", "properties": {"all_dates": {"type": "boolean"},
            "start_date": {"type": "string"}, "end_date": {"type": "string"}}, "required": ["all_dates"], "additionalProperties": False}, {"type": "null"}]},
        "order_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "country": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    }}}}

PROMPT = """调用一次update_request提取本轮明确修改的条件，省略字段继续保留。不要计算金额或生成SQL。
supported表示业务能力，不表示参数是否齐全。缺少口径、缺少日期、无效日期、相对日期都是supported=true，分别用metrics=null或time_range=null请求澄清。
正向商品发票/订单金额均值及其客户聚类Bootstrap置信区间属于支持任务，analysis=invoice_ci、supported=true。样本不足由工具判断，不要预先拒绝。
数据是UCI Online Retail，2010-12-01至2011-12-09，单位GBP。
正向开票金额/销售额=gross_sales，取消冲减金额/取消金额=cancel_amount，净开票金额/净销售额=net_sales。
收入、营收未绑定口径时metrics=null；不要默认销售额或净额。退款到账、利润、预测、因果、A/B显著性不受支持，supported=false。
金额汇总analysis=summary；订单均值、客单价或Bootstrap置信区间analysis=invoice_ci。
只输出本轮明确提到的字段。metrics整体替换。时间范围整体替换，不借旧日期补新日期。
指定日期用all_dates=false和完整ISO起止日期；全期用all_dates=true且不带日期。
相对日期或缺少年份不能解析为日期，但任务本身仍支持；supported=true、time_range=null要求补充。全部订单不等于全期。
订单就是InvoiceNo发票号，保留原大小写和前导零。C开头为取消记录，不能推断原订单。
明确取消发票/国家筛选才将order_id/country置null，否则省略。
country使用源数据英文值，例如英国United Kingdom、德国Germany、法国France、爱尔兰EIRE。
例如本轮只说“只看536365发票”，只传supported=true,order_id=536365，不重复时间或指标。
"""


def model_request(messages, calls, tools=None, transport=None, max_tokens=500):
    load_dotenv(ROOT / ".env", override=True, encoding="utf-8-sig")
    key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not key or not key.isascii():
        raise ValueError("请在项目.env配置DASHSCOPE_API_KEY。网页的手动条件表单可在无API时使用。")
    url = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").strip().rstrip("/") + "/chat/completions"
    body = {"model": os.getenv("LLM_MODEL", "qwen-plus").strip(), "messages": messages,
            "enable_thinking": False, "temperature": 0, "max_tokens": max_tokens}
    if tools is not None:
        body.update({"tools": tools, "tool_choice": "required", "parallel_tool_calls": False})
    timeout = httpx.Timeout(60.0, connect=10.0, write=30.0, pool=10.0)
    # 只重试连接建立失败。等待响应超时、HTTP错误或输出错误均不自动重发。
    with httpx.Client(timeout=timeout, transport=transport) as client:
        for attempt in range(1, 3):
            info = {"usage": {}, "connection_attempt": attempt}
            calls.append(info)
            try:
                response = client.post(url, headers={"Authorization": "Bearer " + key}, json=body)
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                info.update({"error_type": type(error).__name__, "phase": "connect"})
                if attempt == 2:
                    raise ValueError("连接大模型API失败，两次连接尝试均未成功。请运行 diagnose_api.py --api 检查网络，或在左侧手动应用条件继续。") from error
            except httpx.ReadTimeout as error:
                info.update({"error_type": type(error).__name__, "phase": "read"})
                raise ValueError("等待模型响应超时，本次未自动重发请求。可稍后重新输入，或手动应用条件继续。") from error
            except httpx.RequestError as error:
                info.update({"error_type": type(error).__name__, "phase": "network"})
                raise ValueError("模型网络请求失败（" + type(error).__name__ + "）。请运行 diagnose_api.py --api 检查连接或代理设置。") from error
    info["http_status"] = response.status_code
    if response.status_code != 200:
        raise ValueError(f"模型接口返回HTTP {response.status_code}。")
    try:
        payload = response.json()
    except ValueError as error:
        raise ValueError("模型接口未返回JSON，请检查LLM_BASE_URL和代理设置。") from error
    if not isinstance(payload, dict):
        raise ValueError("模型接口返回结构不合法。")
    info["usage"], info["response_model"] = payload.get("usage") or {}, payload.get("model")
    return payload


def extract_patch(text, current, calls, transport=None):
    payload = model_request(
        [{"role": "system", "content": PROMPT}, {"role": "user", "content":
          json.dumps({"已保存条件": current, "本轮输入": text}, ensure_ascii=False)}],
        calls, tools=[PATCH_TOOL], transport=transport)
    if not isinstance(payload.get("choices"), list) or not payload["choices"]:
        raise ValueError("模型接口未返回可用choices。")
    choice = payload["choices"][0]
    tools = choice["message"].get("tool_calls") or []
    if choice.get("finish_reason") == "length" or len(tools) != 1 or tools[0]["function"]["name"] != "update_request":
        raise ValueError("模型没有返回一个完整的条件修改。")
    arguments = json.loads(tools[0]["function"]["arguments"])
    calls[-1]["model_tool_arguments"] = deepcopy(arguments)
    return arguments


class Turn(TypedDict, total=False):
    text: str
    current: dict
    patch: dict
    calls: list
    answer: str
    result: dict


class AnalysisSession:
    def __init__(self, parser=None, store=None):
        self.state = empty_state()
        self.update_failed = False
        self.parser, self.store = parser or extract_patch, store or RetailStore()
        self.id = uuid.uuid4().hex
        self.path = ROOT / "reports" / "sessions" / f"session_{self.id}.json"
        self.trace = {"version": "real-analysis-v1.0.2", "session_id": self.id, "turns": [],
                      "code_sha256": {name: file_hash(ROOT / "wren_analysis" / name) for name in
                                      ("agent.py", "store.py", "statistics.py", "dataset.py", "policy.py", "filters.py")}}
        graph = StateGraph(Turn)
        graph.add_node("extract", self._extract)
        graph.add_node("validate", self._validate)
        graph.add_edge(START, "extract")
        graph.add_edge("extract", "validate")
        graph.add_edge("validate", END)
        self.graph = graph.compile()

    def can_execute(self):
        return not self.update_failed and not missing(self.state)

    def describe_state(self):
        description = show_state(self.state)
        if self.update_failed:
            description = description.replace("条件已齐全，请核对后执行查询。", "")
            return "上轮条件更新失败，已暂停执行。以下是之前保存的条件；请重新输入或手动应用条件。\n" + description.rstrip()
        return description

    def _extract(self, turn):
        return {"patch": self.parser(turn["text"], deepcopy(turn["current"]), turn["calls"])}

    def _validate(self, turn):
        patch = guard_patch(turn["text"], turn["patch"], turn["current"])
        updated = merge_patch(turn["current"], patch)
        return {"patch": patch, "current": updated, "answer": show_state(updated)}

    def handle(self, text):
        text = text.strip()
        if not text:
            return "请输入问题。"
        event = {"user": text, "state_before": deepcopy(self.state), "model_calls": [],
                 "execution_blocked_before": self.update_failed}
        try:
            if text == "/new":
                self.state = empty_state()
                self.update_failed = False
                answer = "已清空条件。\n" + show_state(self.state)
            elif text == "/state":
                answer = self.describe_state()
            elif text == "/run":
                if self.update_failed:
                    raise ValueError("上轮条件未成功更新，查询仍暂停；请重新输入或在手动表单中应用条件。")
                elif missing(self.state):
                    answer = "条件未齐全，没有查询数据库。\n" + show_state(self.state)
                else:
                    scope = scope_of(self.state)
                    result = (self.store.summary(scope, self.state["metrics"]) if self.state["analysis"] == "summary"
                              else invoice_ci(self.store, scope))
                    event["result"] = result
                    answer = show_state(self.state) + "\n\n" + render(result)
            elif text.startswith("/"):
                answer = "命令：/run 查询；/state 条件；/new 清空；终端中/quit退出。"
            else:
                output = self.graph.invoke({"text": text, "current": deepcopy(self.state), "calls": event["model_calls"]})
                if self.update_failed and set(output["patch"]) <= {"supported"}:
                    raise ValueError("尚未收到明确的新条件，查询继续暂停；可手动应用条件。")
                self.state = output["current"]
                self.update_failed = False
                event["applied_patch"] = output["patch"]
                answer = output["answer"]
            event["status"] = "ok"
        except Exception as error:
            if not text.startswith("/"):
                self.update_failed = True
            event["status"], event["error"] = "error", str(error)
            answer = "本轮没有产生可用分析结果：" + str(error) + "\n" + self.describe_state()
        event.update({"state_after": deepcopy(self.state), "answer": answer,
                      "execution_blocked_after": self.update_failed})
        self.trace["turns"].append(event)
        save_json(self.path, self.trace)
        return answer

    def set_conditions(self, patch):
        before = deepcopy(self.state)
        blocked_before = self.update_failed
        self.state = merge_patch(self.state, {"supported": True, **patch})
        self.update_failed = False
        self.trace["turns"].append({"user": "[手动条件表单]", "source": "manual_form", "model_calls": [],
                                   "state_before": before, "state_after": deepcopy(self.state), "status": "ok",
                                   "execution_blocked_before": blocked_before, "execution_blocked_after": False})
        save_json(self.path, self.trace)
