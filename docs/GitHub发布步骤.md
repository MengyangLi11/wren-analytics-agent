# GitHub 发布步骤

账号：MengyangLi11。建议仓库名：wren-analytics-agent。

## 本地准备

将发布准备包解压到已经验收的项目根目录。运行：

```powershell
.\.venv\Scripts\python.exe .\prepare_git_repo.py
git diff --cached --stat
git commit -m "Release verifiable retail analytics agent v1.0.2"
git status --short
```

脚本先核对冻结的源码与评测器字节指纹，确认通过后才初始化仓库并暂存明确列出的源码、测试和文档。它使用本地 Git 配置，不更改其他项目配置，也不联网或推送。

提交身份使用 GitHub 公开 API 查询所得用户 ID 150634079，对应隐私地址 `150634079+MengyangLi11@users.noreply.github.com`；可在 GitHub 邮箱设置中核对。它不会读取账号密码或 Token。

`.env`、虚拟环境、原始数据、数据库、本机证据、下载 ZIP 与旧原型脚本被排除。现有旧脚本仍留在工作目录中，可继续用于历史复现。`.gitattributes` 保留文件原始换行，防止克隆后冻结校验因 CRLF/LF 转换失败。

如果目录已有其他 Git 提交或已暂存清单外文件，脚本会停止并说明情况；不重写历史，不强制清理。

## 远程仓库

在本人 GitHub 账号创建空仓库，名称为 `wren-analytics-agent`。可见性由本人选择；公开仓库会让源码与文档对所有人可见。创建时不自动添加 README、许可证或 .gitignore，以免与本地首个提交产生分叉。

仓库创建并确认可见性后，在网页展示的 HTTPS 地址与下列地址一致时推送：

```powershell
git remote add origin https://github.com/MengyangLi11/wren-analytics-agent.git
git push -u origin main
```

若远程 origin 已存在，先 `git remote -v` 查看，不直接覆盖。Git for Windows 可能打开浏览器登录 GitHub；普通账号密码不能作为 HTTPS Git 密码使用。优先使用浏览器授权，不在聊天中发送凭证。

## 全新目录复现

远程仓库可访问后，在独立目录克隆并按 README 创建新虚拟环境、安装 requirements、构建数据、运行本地检查和网页。需要自然语言时只在新目录本机配置 `.env`。这项验证与已有工作目录启动成功分开记录。

正式开放分享前，检查 README 链接、复现记录、演示截图和许可证选择。依赖及 UCI 数据许可仍由各自权利人规定，不能因本项目自行改变。
