import json
from datetime import date

import pandas as pd
import streamlit as st

from wren_analysis.agent import AnalysisSession, missing, show_state, scope_of
from wren_analysis.common import MANIFEST_PATH, read_json, LABELS


st.set_page_config(page_title="可核验数据分析 Agent", page_icon="📊", layout="wide")
st.title("可核验数据分析 Agent")
st.caption("Wren 语义模型 · 指标口径追溯 · 多轮条件管理 · 客户聚类 Bootstrap")

if not MANIFEST_PATH.exists():
    st.info("先在项目终端运行 python build_data.py，再刷新页面。")
    st.stop()

manifest = read_json(MANIFEST_PATH)
if "analysis_session" not in st.session_state:
    st.session_state.analysis_session = AnalysisSession()
    st.session_state.messages = []
session = st.session_state.analysis_session


def push(text):
    answer = session.handle(text)
    st.session_state.messages.extend([{"role": "user", "content": text}, {"role": "assistant", "content": answer}])


with st.sidebar:
    st.subheader("数据与口径")
    st.write(f"UCI Online Retail · {manifest['kept_rows']:,} 条纳入记录")
    st.write(f"{manifest['date_min']} 至 {manifest['date_max']} · GBP")
    st.caption("金额为开票口径。取消记录不等于退款到账；C发票不能直接关联回原订单。")
    if st.button("清空会话条件", use_container_width=True):
        push("/new")
        st.rerun()

    with st.expander("手动填写条件（无需API）"):
        with st.form("conditions"):
            mode = st.selectbox("分析任务", ["金额汇总", "订单均值与区间估计"])
            selected = st.multiselect("金额汇总指标", list(LABELS), default=list(LABELS), format_func=LABELS.get)
            all_dates = st.checkbox("全部现有记录", value=False)
            start = st.date_input("开始日期", date(2011, 11, 1))
            end = st.date_input("结束日期", date(2011, 11, 30))
            order_id = st.text_input("发票号（留空则不筛选）")
            country = st.text_input("国家英文值（留空则不筛选）", placeholder="United Kingdom")
            submitted = st.form_submit_button("应用条件")
        if submitted:
            try:
                period = {"all_dates": True} if all_dates else {"all_dates": False, "start_date": start.isoformat(), "end_date": end.isoformat()}
                session.set_conditions({"analysis": "summary" if mode == "金额汇总" else "invoice_ci",
                    "metrics": selected or None, "time_range": period,
                    "order_id": order_id.strip() or None, "country": country.strip() or None})
                st.success("条件已应用，请核对后执行。")
            except ValueError as error:
                st.error(str(error))

    st.subheader("当前查询")
    st.text(session.describe_state())
    if session.update_failed:
        st.warning("新条件尚未更新成功。请重试输入，或在手动表单应用条件后再执行。")
    if st.button("执行查询", type="primary", disabled=not session.can_execute(), use_container_width=True):
        with st.spinner("由数据库及统计工具计算……"):
            push("/run")
        st.rerun()
    calls = [call for turn in session.trace["turns"] for call in turn.get("model_calls", [])]
    st.caption(f"本会话模型请求 {len(calls)} 次；Token {sum((call.get('usage') or {}).get('total_tokens', 0) for call in calls)}")

chat_tab, evidence_tab, lineage_tab = st.tabs(["分析对话", "查询与统计证据", "原始记录追溯"])

with chat_tab:
    st.info("可输入：统计2011年11月的销售额、取消金额和净销售额。补齐条件后点击左侧“执行查询”。")
    st.caption("继续补充：只看536365发票 / 取消订单筛选 / 改成2011年12月1日。统计分析：估计2011年11月订单金额均值的置信区间。")
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.text(message["content"])
    text = st.chat_input("输入问题或补充条件")
    if text:
        with st.spinner("提取并校验本轮条件……"):
            push(text)
        st.rerun()

with evidence_tab:
    successful = [turn for turn in session.trace["turns"] if "result" in turn]
    if not successful:
        st.write("执行查询后，这里显示口径、SQL、数据指纹和计算参数。")
    else:
        result = successful[-1]["result"]
        proof = result["proof"]
        st.subheader("最近一次成功查询")
        st.json({"分析": result["kind"], "范围": result["scope"], "状态": result["status"]}, expanded=True)
        st.code(proof["sql"], language="sql")
        with st.expander("Wren执行计划"):
            st.code(proof["planned_sql"], language="sql")
        with st.expander("业务口径与数据指纹"):
            st.json({key: value for key, value in proof.items() if key not in ("rows", "sql", "planned_sql")})
        if result["kind"] == "invoice_ci":
            st.json({key: result[key] for key in ("method", "invoice_count", "cluster_count", "mean", "ci", "n_resamples", "confidence", "seed", "sample_sha256", "assumptions")})
        if st.button("显示同范围每日净开票趋势"):
            with st.spinner("按日汇总完整筛选记录……"):
                rows = session.store.daily(result["scope"])
            if rows:
                chart = pd.DataFrame(rows)
                chart["event_date"] = pd.to_datetime(chart["event_date"])
                chart["net_sales"] = chart["net_sales"].astype(float)
                st.line_chart(chart.set_index("event_date"))
            else:
                st.write("该范围没有记录。")
    st.download_button("下载完整会话证据 JSON", json.dumps(session.trace, ensure_ascii=False, indent=2, default=str).encode("utf-8"),
                       file_name=f"session_{session.id}.json", mime="application/json")

with lineage_tab:
    st.write("输入原始Excel行号，查看该行是否纳入、排除理由和对应事件。第1行为表头。")
    with st.form("lineage"):
        row_number = st.number_input("Excel行号", min_value=2, max_value=541910, value=2, step=1)
        lookup = st.form_submit_button("查看原始记录")
    if lookup:
        import duckdb
        from wren_analysis.common import DB_PATH
        session.store.snapshot()
        with duckdb.connect(str(DB_PATH), read_only=True) as connection:
            frame = connection.execute("SELECT * FROM raw_rows WHERE source_row = ?", [int(row_number)]).fetchdf()
            event = connection.execute("SELECT * FROM transactions WHERE source_row = ?", [int(row_number)]).fetchdf()
        st.dataframe(frame, hide_index=True)
        st.dataframe(event, hide_index=True)
