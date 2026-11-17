"""本机网络诊断；不输出Key、代理地址或完整.env。--api发送一次简短API测试。"""
from urllib.parse import urlsplit
from urllib.request import getproxies
import argparse
import os
import socket

from dotenv import load_dotenv
from wren_analysis.common import ROOT
from wren_analysis.agent import model_request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", action="store_true", help="额外发送简短API请求，会产生少量Token用量")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=True, encoding="utf-8-sig")
    url = urlsplit(os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").strip())
    if url.scheme != "https" or not url.hostname:
        raise SystemExit("请将LLM_BASE_URL配置为有效HTTPS接口地址。")
    host, port = url.hostname, url.port or 443
    print("接口主机：", host)
    print("配置模型：", os.getenv("LLM_MODEL", "qwen-plus"))
    print("检测到的代理配置类型：", ", ".join(sorted(getproxies())) or "无")
    print("不会输出代理地址或API Key。")
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        print("DNS：通过，解析地址数", len(addresses))
    except OSError as error:
        print("DNS：失败，错误类型", type(error).__name__)
    try:
        with socket.create_connection((host, port), timeout=5):
            print("直连TCP 443：通过")
    except OSError as error:
        print("直连TCP 443：失败，错误类型", type(error).__name__)
    print("直连检查不经过HTTP代理；若使用代理，以后面的实际API结果为准。")
    if args.api:
        calls = []
        try:
            result = model_request([{"role": "user", "content": "请只回复：连接成功"}], calls, max_tokens=16)
            print("API HTTP：", calls[-1].get("http_status"))
            choices = result.get("choices") or []
            print("模型返回文本：", bool(choices and choices[0].get("message", {}).get("content")))
            print("Token用量：", result.get("usage") or {})
        except Exception as error:
            print("API测试失败：", str(error))
        print("请求尝试次数：", len(calls))
        for index, call in enumerate(calls, 1):
            print("尝试", index, {key: call[key] for key in ("http_status", "phase", "error_type") if key in call})


if __name__ == "__main__":
    main()
