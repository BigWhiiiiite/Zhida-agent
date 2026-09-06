# OfferPilot — 求职投递 Agent MVP

一个本地优先的简历资料库：上传 PDF、DOCX 或 TXT 简历，自动拆分姓名、性别、年龄、联系方式、教育、技能、实习和项目经历，并允许逐项修改和保存。

## 技术选择

- 前端：React + TypeScript + Vite
- API：FastAPI + Pydantic
- 存储：SQLite（结构化资料）+ 本地 uploads（原始简历）
- Agent：PydanticAI 适配层。默认使用无需 API Key 的规则解析器；配置模型后切换为大模型结构化解析。

PydanticAI 适合这里的原因是：输出直接受 Pydantic schema 约束，和 FastAPI 共用数据模型；以后增加职位分析、表单映射、投递前检查时，也能逐步扩展成工具调用或工作流，而不必现在就引入很重的图编排。

## 启动

需要 Node.js 20+ 和 Python 3.11+。

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

另开一个终端：

```bash
cd frontend
npm install
npm run dev
```

打开 <http://localhost:5173>。API 文档位于 <http://localhost:8000/docs>。

## 使用大模型解析（可选）

默认 `APP_AGENT_MODE=rules`，所有数据只在本机处理。要启用 PydanticAI：

```bash
export APP_AGENT_MODE=pydantic_ai
export APP_AGENT_MODEL=openai:gpt-5-mini
export OPENAI_API_KEY=你的密钥
```

然后重启后端。模型只负责把已抽取的简历文本转成 schema；原始文件仍由本地应用保存。

## 当前边界

- 规则解析器是可运行的保底版本，对中文常见标题和联系方式效果较好，但复杂双栏 PDF、扫描件和非常规排版仍需要后续 OCR/LLM 增强。
- MVP 不包含账户系统、云端对象存储和真实投递；后续做自动投递时，应始终在最终提交前让用户确认。

