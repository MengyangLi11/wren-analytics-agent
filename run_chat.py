from wren_analysis.agent import AnalysisSession

if __name__ == "__main__":
    session = AnalysisSession()
    print("真实数据会话：/run 查询；/state 条件；/new 清空；/quit 退出。")
    print("证据文件：", session.path)
    while True:
        try:
            text = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text == "/quit":
            break
        print(session.handle(text))
