# 职达 Zhida

> 上传一次简历，建立可复用的求职档案。

职达是一个本地存储、human-in-the-loop 的求职资料 Agent。它将多份 PDF、DOCX 或 TXT 简历拆成结构化信息，合并为候选人主档案，并保留字段置信度、原文证据和冲突记录。用户始终拥有最终决定权。

## 第一阶段能力

- 候选人主档案：联系方式、求职偏好、教育、实习、项目、技能、语言、证书和奖项
- 多简历资料库：语言、默认版本、文件大小、解析方式和更新时间
- 本地文件解析：PDF、DOCX、TXT，单份最大 10MB
- 文件 SHA-256 去重与安全文件名
- OpenAI Agents SDK 结构化解析器，以及可选的本地规则保底模式
- 字段级置信度、来源原文和确认/修改/拒绝状态
- 多简历自动合并；冲突信息必须由用户选择，不静默覆盖
- 原始文件下载、重新解析、删除及 JSON 数据导出
- SQLite 本地持久化；只在解析简历或匹配表单时将所需内容发送到已配置的模型代理

扫描版 PDF 的 OCR、账户系统、职位匹配和浏览器投递不属于第一阶段。

## 技术栈

- React 19 + TypeScript + Vite
- FastAPI + Pydantic
- SQLite + 本地文件存储
- OpenAI Agents SDK + OpenAI-compatible Responses API
- Playwright 可视浏览器执行器

## 本地启动

需要 Python 3.9+ 和 Node.js 20+。

先配置模型代理（密钥不会进入 Git）：

```bash
cp .env.example backend/.env
# 编辑 backend/.env，填写 ISRC_API_KEY
```

后端：

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

前端：

```bash
cd frontend
pnpm install
pnpm run dev
```

打开 <http://localhost:5173>，API 文档位于 <http://localhost:8000/docs>。

## 模型代理配置

默认配置使用 OpenAI Agents SDK、Responses API 和自定义代理：

```bash
APP_AGENT_MODE=agents_sdk
APP_AGENT_MODEL=gpt-5.6-sol
APP_MODEL_BASE_URL=https://llmapi.isrc.ac.cn/v1
APP_MODEL_REASONING_EFFORT=max
APP_MODEL_API_KEY_ENV=ISRC_API_KEY
ISRC_API_KEY=你的密钥
```

Codex 配置中的 `ultra` 在 Responses API 上会由职达自动转换为当前支持的最高档 `max`。

使用模型解析简历或分析网页时，对应的简历文本、页面字段和候选人资料会发送到这个模型代理。敏感字段缺失时，Agent 必须保持空白，不得根据姓名、毕业年份等信息推测。

如果只想测试本地规则解析，可以设置 `APP_AGENT_MODE=rules`。

## 招聘网站填写 Demo

1. 先在“主档案”中保存真实资料。
2. 进入“网站 Demo”，粘贴公开招聘申请页面 URL。
3. 点击“打开浏览器”，职达会启动一个独立可见的 Chrome 窗口。
4. 必要时由用户在该窗口登录或切换到具体表单页。
5. 点击“分析当前页面”，Agents SDK 会生成结构化填写计划。
6. 检查计划后点击“填写安全字段”。

Demo 只会执行置信度不低于 85% 的非敏感 `fill`、`select` 和 `check` 操作。除模型判断外，执行层还会独立拦截签证、薪资、性别、工作许可、同意条款等字段。它不会填写密码、处理验证码、上传文件、点击下一步或点击最终提交。当前只扫描顶层页面 DOM，iframe、多页流程和各 ATS 专用适配器将在后续版本加入。

默认使用电脑已安装的 Google Chrome。如需使用 Playwright 自带 Chromium：

```bash
playwright install chromium
export APP_BROWSER_CHANNEL=chromium
```

## 验证

```bash
cd backend && .venv/bin/python smoke_test.py
cd frontend && pnpm run build
```

冒烟测试使用临时数据库和临时上传目录，不会污染用户资料。

## 隐私

`backend/data/`、`backend/uploads/`、`.env`、虚拟环境与前端构建产物均被 Git 忽略。删除一份简历时，数据库记录、相关冲突和原始文件会一并删除。
