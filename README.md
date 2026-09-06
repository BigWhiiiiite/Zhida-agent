# 职达 Zhida

> 上传一次简历，建立可复用的求职档案。

职达是一个本地优先、human-in-the-loop 的求职资料 Agent。它将多份 PDF、DOCX 或 TXT 简历拆成结构化信息，合并为候选人主档案，并保留字段置信度、原文证据和冲突记录。用户始终拥有最终决定权。

## 第一阶段能力

- 候选人主档案：联系方式、求职偏好、教育、实习、项目、技能、语言、证书和奖项
- 多简历资料库：语言、默认版本、文件大小、解析方式和更新时间
- 本地文件解析：PDF、DOCX、TXT，单份最大 10MB
- 文件 SHA-256 去重与安全文件名
- 本地规则解析器，以及可选的 PydanticAI 结构化解析器
- 字段级置信度、来源原文和确认/修改/拒绝状态
- 多简历自动合并；冲突信息必须由用户选择，不静默覆盖
- 原始文件下载、重新解析、删除及 JSON 数据导出
- SQLite 本地持久化，默认不将简历发送到云端模型

扫描版 PDF 的 OCR、账户系统、职位匹配和浏览器投递不属于第一阶段。

## 技术栈

- React 19 + TypeScript + Vite
- FastAPI + Pydantic
- SQLite + 本地文件存储
- PydanticAI Agent 适配层

## 本地启动

需要 Python 3.9+ 和 Node.js 20+。

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

## 可选：启用大模型解析

默认 `APP_AGENT_MODE=rules`，解析过程完全在本地进行。启用 PydanticAI 前，请先理解简历文本会发送给配置的模型服务商：

```bash
export APP_AGENT_MODE=pydantic_ai
export APP_AGENT_MODEL=openai:gpt-5-mini
export OPENAI_API_KEY=你的密钥
```

敏感字段缺失时，Agent 必须保持空白，不得根据姓名、毕业年份等信息推测。

## 验证

```bash
cd backend && .venv/bin/python smoke_test.py
cd frontend && pnpm run build
```

冒烟测试使用临时数据库和临时上传目录，不会污染用户资料。

## 隐私

`backend/data/`、`backend/uploads/`、`.env`、虚拟环境与前端构建产物均被 Git 忽略。删除一份简历时，数据库记录、相关冲突和原始文件会一并删除。

