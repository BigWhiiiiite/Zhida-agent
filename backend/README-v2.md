# 职达后端 V2 本地运行

V2 使用独立 PostgreSQL、Redis cache/control 实例和 RabbitMQ。基础 API `app.backend_v2.api` 不读取原来的 `backend/.env`，不会自动导入用户 SQLite、附件或简历。前端切换使用下文的 PostgreSQL 工作台网关；仅启动基础 API 不代表旧前端已经切换。

**2026-10-12 当前状态：**本机业务库已升级至 `0009_assisted_runtime`，网关与 Worker 已统一启用 D3。工作台使用持久任务授权、本机 LangGraph 答案规划、受控浏览器执行与回读；不再依赖一个长时间 POST 的返回判断是否成功。通用默认配置仍关闭图开关，必须显式迁移并启用。以下历史 D1/D2 验收记录不代表当前功能缺失，也不代表真实官网 D3 填写已验收。

## 当前总体架构

```mermaid
flowchart TD
  UI[React 工作台：选择简历和填写页 URL] --> API[本机 FastAPI 网关]
  API --> TX[PostgreSQL：任务和 Outbox 同事务]
  TX --> PUB[Outbox 发布器]
  PUB --> MQ[RabbitMQ]
  MQ --> WK[workflow.advance Worker / LangGraph 控制图]
  WK --> AUTH[持久化授权和检查点]
  AUTH --> BR[本机 BrowserRunner：只操作明确绑定的窗口]
  BR --> OBS[采集完整题干、栏目、主体、控件、真实选项]
  OBS --> PLAN[LangGraph 答案规划 / 严格证据校验]
  FACT[已确认事实与选中简历] --> PLAN
  RAG[pgvector HNSW + GIN / RRF 检索] --> PLAN
  PLAN --> MODEL[必要时：配置模型分析歧义和定向补读]
  MODEL --> VALID[独立校验计划]
  PLAN --> VALID
  VALID --> WRITE[文本 / 下拉 / 单多选 / 日期等类型化动作]
  WRITE --> READ[实际值和选中状态回读 / 持久回执]
  READ -->|新字段或未完成| OBS
  READ -->|缺少真实资料| USER[前端完整问题确认表]
  USER --> MEM[保存答案与简历、站点、经历作用域]
  MEM --> FACT
  READ -->|系统检查通过| REVIEW[用户最终检查，不自动提交]
```

这是**两层图加有界执行循环**，不是让多个大模型自由对话。Worker 图管理任务和执行权；本机规划图处理字段答案，既有有界循环负责补读、控件依赖、填写和再观察。模型不持有原始浏览器控制权，也不能绕过校验直接执行 JavaScript。D3 复用 D2 的 `adapt_browser_snapshot → normalize_observation`；未将真实页面先上传至原生 D2 Worker 报告链再执行，不应混淆这两条路径。

| 部分 | 技术与职责 | 持久数据/边界 |
| --- | --- | --- |
| 前端 | React、TypeScript、Vite；简历与 URL 入口、确认表、进度和开发检查 | 只缓存当前用户的任务引用，不在浏览器恢复旧填写指令 |
| API | FastAPI、Pydantic、兼容工作台网关 | 工作台是单一资料写入口；回环地址、Host、Origin 及用户校验；不是公网浏览器服务 |
| 业务库 | PostgreSQL、SQLAlchemy、Alembic | `workbench` 保留业务数据；`zhida` 保存版本事实、任务、检查点、回执；RLS 隔离用户 |
| 向量知识库 | PostgreSQL pgvector 与全文 GIN | `rag` 保存知识源、源版本、索引代次、文本片段、512 维向量；不是 Python 逐条扫描所有向量 |
| Redis cache | 可淘汰的 allkeys-lfu 实例 | 查询向量等可重建缓存；用户、内容哈希和模型版本进入 key，不保存最终事实真值 |
| Redis control | noeviction 独立实例、Lua 原子操作 | 令牌桶、缓存重建锁、共享模型调用预算；协调不可用时禁止新受控写入/模型调用 |
| RabbitMQ / Celery | 持久消息、发布确认、4 类队列、Outbox / Inbox | 至少一次投递加幂等事务；不宣称外部网页恰好一次执行 |
| 本机 Runner | 受控 Safari / Chromium 驱动、持久动作账本、租约与回读 | 只操作指定页面；用户登录、声明与最终提交仍由本人完成 |
| 模型 | 已配置 OpenAI-compatible Responses API、Agents SDK | 确定项先由规则处理；难题使用字段及来源证据，可按授权定向观察/截图；关闭第三方追踪 |

### 数据和记忆如何分开

- 多简历：`resumes / resume_revisions` 固定本轮版本。联系资料取自主档案，教育、实习、项目和技能取自所选简历；不把另一个岗位版本的项目偷偷补进来。
- 经历：教育用学校、学历等锚点，实习用公司/角色，项目用名称，再结合时间及记录边界匹配。重复或冲突锚点不按数组第几条猜测，本科与硕士学院分别归属。
- 长期事实：`profile_facts / fact_revisions / answer_memories` 保留语义键、主体、版本、确认与撤销状态。现有工作台确认记忆在同一资料事务中投影到版本事实；重复相同值不新建版本，冲突不采用“最后一个答案获胜”。
- 复用范围：确认答案至少核对用户、简历、公司/站点、完整问题、选项与经历主体。地址中的籍贯、生源地、户口和现居地不是同一个字段。条件改变、选项改变或记忆撤销，需要重新核对，不能保证所有公司同名问题永远不再问。
- 短期任务：`application_runs / workflow_checkpoints / assisted_rounds / assisted_actions` 保存本轮绑定、状态和回执，不把临时模型推断升级成长期事实。附件与完整结果在本机私有范围内保存，队列只传任务引用和版本，不传简历正文、cookies 或密钥。

### RAG 索引与检索

```text
资料/规则变更与版本保存
  → 同事务 Outbox → rag.index Worker
  → 按事实、句子/分句、token 上限切分（保留父经历和原文位置）
  → 本地 BGE 512 维 embedding + 中文字/二元词及技术词索引
  → 新索引代次构建完成后激活

当前题目 + 用户/简历版本/公司/经历过滤
  → 已确认的精确答案优先
  → HNSW 向量候选 20 条 + GIN 全文候选 20 条
  → 加权 RRF：0.6/(60+向量排名) + 0.4/(60+词法排名)
  → 批量回取证据，重新检查权限、有效版本与撤销状态
  → 给规划器或模型作为依据，不把检索相关性当真实性/填写权限
```

本地模型为 `BAAI/bge-small-zh-v1.5`，固定离线 snapshot；不在运行中联网下载。词法排序为 PostgreSQL `ts_rank_cd`，**不是 BM25**。HNSW 在过滤后召回不足时，仅对规模受限的候选集合允许精确补齐，并标明模式；不将补齐结果冒充 ANN 原始召回。公司规则索引保存题目/字段对照/规则文本，不把私人答案直接当公共规则。资料新版本构建索引期间旧代次仍有明确版本，不允许越过当前有效源读取已撤销证据。

### 缓存、锁、限流和 Worker

- 击穿：进程内 singleflight + Redis `SET NX PX` 重建锁，只由持有随机 token 的进程续期/解锁；竞争者短暂等待，不能无限阻塞。
- 穿透：先校验归属，确实不存在的结果短暂负缓存；索引未完成、异常不能伪装成“不存在”。
- 雪崩：TTL 使用 0.8–1.2 倍抖动；缓存不可用时只允许有界并发回源，不让请求无限打到数据库。控制 Redis 不可用与普通缓存不可用区别处理，前者不能授予写权限。
- 任务并发：PostgreSQL 版本比较、任务/Runner 租约和 fencing 校验与 Redis 速率控制配合；单用户同一时刻只有一个本机辅助填写轮次。取消、窗口换绑、简历/档案变化、租约失效都会阻止旧结果继续写入。
- 模型预算：D3 每次实际请求先预占共享 RPM、TPM 与并发槽，返回后以实际 usage 结算。当前本机保护配置为 20 RPM、1,000,000 TPM、并发 2，是应用保护上限，不是服务商承诺。SDK 自动重试禁用，未知 usage 保守占用并等待核对，不因超时直接释放并重发。
- 本机初始部署：`workflow.advance`、`document.parse`、`rag.index`、`maintenance` 各 1 个 Celery Worker、并发 1，加 1 个 Outbox 发布器、1 个恢复扫描器、1 个网关，共 7 个托管进程。等待用户和浏览器时释放队列 Worker，本机 Runner 另行等待授权；扩容前需实测，不无限增加 BrowserRunner。
- 重复和失败：业务与 Outbox 同事务；发布器等待 Broker 确认，消费用 Inbox 与版本去重。重试有次数/延迟上限，失败进入终态记录。浏览器结果未明不是普通消息重试，必须先只读对账。

## D3：当前辅助填写执行链

前端从服务器读取 `assistance_async_version=1` 后，调用 `POST /api/browser/{session_id}/assist/start?run_id=...`，返回 202；后续轮询持久进度。不会因长请求断开重发开始，也不在 D3 错误时悄悄回退到旧写入接口。

**Worker 控制图：**`bind_context → validate_context → dispatch_local_round / reconcile / wait_for_browser / audit_coverage / fail_closed`。授权与任务检查点、Inbox 完成同事务提交；Worker 不持有桌面浏览器，也不等待慢模型。

**本机答案规划图：**`read_and_understand → retrieve_confirmed_facts → validate_answer_plan`；难题进入 `read_evidence → model_ambiguities → validate_answer_plan`。执行前始终再次核验题干与真实控件：不存在的 selector、题干冲突、缺选项、错误经历归属、声明及密码等不可被模型强行放行。

有界执行循环按以下顺序推进：

1. 固定用户、选中简历版本与明确连接的官网窗口；被动采集，必要时定向打开/关闭已有控件读取真实选项，不选择答案。
2. 匹配明确事实和同范围确认记忆，必要时检索规则及让模型分析；只将有依据、通过独立校验的答案交给执行器。
3. 对文本、选择、日期分别执行；先写持久 dispatch 记录，再触碰控件，等控件稳定后回读实际值/选中状态。网页显示近似值但提交状态未通过，不计成功。
4. 条件字段出现后重新观察；空教育记录可先选择并核验学历，再读取学校/学院等字段。已填教育记录不按序号重新分配。仅注册适配器允许新增经历，不盲点任意“添加”。
5. 检查必填、记录覆盖与未读区域；需要真实资料的题目显示完整题干和真实选项，让用户一次确认并保存，再开启新轮次。无法识别的问题是系统缺口，不装成已完整填写。
6. 检查通过只能到 `ready_for_review`，交给用户最终检查，不点击保存/最终提交/本人声明。官网可能在字段变化时自行保存草稿，应用不承诺无站点侧副作用。

截图/视觉补证沿用已有工具和本轮用户授权；D3 不是无限截屏或全桌面扫描。附件上传须另行明确允许；没有通用证件照/附件自动生成或猜选功能。旧 D1/D2 任务保留图版本，不自动升级。

### 断线和恢复边界

结果未知的动作保持 `unknown`，新轮次被阻止。开发检查中的 `POST /api/browser/{session_id}/assist/reconcile` 只读当前明确绑定的申请页：题目、记录及签名唯一一致才核对文本/选项/日期/勾选值，**不打开菜单、不重写、不扫描其他窗口**。未知新增记录/上传或看不清的控件仍隔离，需人工核对；尚无一键强制清除未知动作的接口。

后端重启能读回任务、事实和回执，但本机窗口绑定是进程内资源，要重新明确连接；不会从旧写入中途自动重放。持久化是应用层事务检查点，不是 LangGraph `PostgresSaver` 逐节点恢复，没有时间旅行或外部动作“恰好一次”的承诺。模型超时后的未知 usage 也不自动恢复配额，需要按服务商实际结果运维核对。

### 本轮验证与尚未验收项（2026-10-12）

- 完整隔离后端回归 **512 passed，0 skipped**（54.63 秒）；真实合成 PostgreSQL/RLS、pgvector、本地 embedding、RabbitMQ/Celery D3 授权及 LangGraph 检查点均包含在内。
- 合成完整填写循环验证：确定字段不调用模型即填写并回读；动态学历选择后学校/学院正确归属，学历选中失败不继续填学校；未知动作、取消、断线、旧版本、确认记忆冲突和索引代次均有覆盖。控件模拟不等于官网运行验收。
- 前端 Node 测试 **32 passed**；TypeScript、Vite、Python 编译与 `pip check` 通过。仅有第三方 Starlette/AnyIO 弃用警告；没有调用付费模型，没有操作/提交真实招聘表。
- 本机升级前生成私有完整 PG 备份，并逐表核对业务数据 SHA-256 与数量；升级前后 **681 条原有业务记录不变**，附件/旧 SQLite 未删除。本机已启用三项图开关，健康接口不再报告旧阶段。
- 最终重启后从运行角色按当前用户作用域只读核对：6 份简历、1 份主档案、19 条有效确认答案事实、7 个有效向量索引头、616 个向量片段，7 个既有索引任务均完成。本机暂无公司规则源；规则投影/检索路径已由合成规则回归验证，不声称已构建全面公司知识库。前端页面及同源健康代理均返回 200，7 个托管应用进程正常运行。
- **待用户回来验收：**光大已登录填写页的完整识别、模型返回质量、真实下拉/日期/经历/附件控件。不同 ATS、跨域 iframe 或未注册组件仍可能出现缺口；目前不能宣称所有公司可一键填完。Docker 镜像仍仅静态校验，宿主 Safari Runner 不由容器代替。

首次从旧本机阶段升级（已升级不要重复执行导入）：先停止项目托管应用，`scripts/upgrade_assisted_runtime.py --apply --confirm-app-stopped` 会先备份、显式迁移、投影确认事实与规则索引任务，再检查业务数据未变；默认仅预检。新启动使用 `workbench_v2_app.py start --enable-assisted`，否则不显式开启 D3。回退必须冻结写入并对账，不能直接启动旧 SQLite 丢弃升级后的修改。

## LangGraph 分步落地计划（2026-10-12）

| 阶段 | 范围 | 验收门槛 |
| --- | --- | --- |
| D1：可恢复的任务骨架（已完成） | 固定任务/简历/图版本，等待用户或浏览器，版本检查、持久化、取消与继续 | 进程重建能读到已提交进度；重复消息、旧版本、过期执行权不能提交新进度；不操作网页 |
| D2：只读识别与答案计划（结构/隔离验收完成） | 快照适配、题干/控件核验、严格事实匹配、索引检索及确认记忆；独立只读预览与原生只读图保留，D3 复用同一归一化 | 按题核验题干、选项、经历归属、覆盖遗漏；看不清的问题先补观察；有来源的答案才能进入计划 |
| D3：受控执行与实际回读（编码/隔离验收完成，待官网验收） | Worker 授权、本机规划图及单一 Runner，类型化动作、持久回执、动态字段补读与只读对账 | 合成完整循环与实际基础设施通过；真实招聘页需重新明确连接验收；停在最终提交前 |

D1 已提供的执行路径：`POST /api/v2/runs → PostgreSQL/Outbox → RabbitMQ → workflow.advance Worker → LangGraph → 事务保存进度 → GET run/events`。每次唤醒只运行一个有上限的纯决策图：`bind_context → validate_context → wait_for_user / wait_for_browser / reconcile / fail_closed`。`reconcile` 在 D1 仅表示隔离结果不明的动作、等待后续对账，**不是已经实现自动对账或填写恢复**。没有模型调用、浏览器操作或隐式上传。

持久化采用**应用层事务检查点**：`workflow_checkpoints` 保存只含引用/版本/计数的 JSON，`workflow_heads` 指向已提交进度，`workflow_step_commits` 记录单步结果。它们与任务版本、事件和 Inbox 完成记录同一事务提交；未提交的计算不能作为恢复点。下一次始终从 `bind_context` 核对最新资料，不从旧填写动作中途重放。这不是 LangGraph `PostgresSaver` 的逐节点检查点，也没有承诺 `interrupt()/Command`、时间旅行或外部网页“恰好一次执行”。短期任务进度与长期用户事实仍分开保存，符合 [LangGraph 对检查点与长期存储的区分](https://docs.langchain.com/oss/python/langgraph/persistence)。

固定依赖：LangGraph 1.2.12（及 `requirements-v2.txt` 中固定的核心组件）。图调用显式关闭 LangSmith 跟踪，不需要托管 LangGraph 或 LangSmith 账号；D1/原生 D2 的确定性路径无需外部模型，D3 难题分析使用已有配置模型。任务有最大推进次数、节点步数和可选截止时间；用户/简历/浏览器连接版本变化会拒绝过期结果。

### D1 接口和开关

- `ZHIDA_V2_WORKFLOW_ENABLED=false` 是默认值；开启必须在 API 与 workflow Worker 同时配置，且先显式执行 `0007_workflow_runtime` 迁移。启动不会自动迁移。旧 `foundation-v1` 任务不会悄悄换成新图。
- `POST /api/v2/runs/{id}/resume` 携带 `expected_revision`；只有可暂停状态能继续。重复或过期请求返回 409；未回答完问题不允许继续；新唤醒重新观察，不复用旧执行计划。
- 原生 API 的确认答案携带任务版本、D1 题目版本 `expected_question_revision` 和事实版本。部分答案立即存入事实及记忆，仍然等待其余答案；全部答完才派发重新观察任务。索引异步，不依赖网页填写成功。
- 兼容网关的原生事实/简历/答案写接口仍阻止双写。当前工作台资料确认通过单一兼容写入口、同事务版本投影；D3 start 桥另行创建绑定本机窗口与简历版本的原生任务。
- 健康接口报告配置是否启用及 `workflow_stage=d1_control_only`，但 `workflow_worker_checked=false`，不能据此推断 Worker 已消费任务或具备自动填写能力。

D1 阶段仅在合成库验收，未在该阶段迁移业务库。当前 D3 已按文首记录备份、迁移并启用，不要把历史阶段边界当作当前运行状态。

### D2 第一部分：已实现的只读分析链路

任务固定为 `langgraph-d2-v1` 后，路径是：

```text
已绑定的页面快照 → 校验并入库 → Outbox / Worker
  → bind_context → validate_context → recognize_form
  → retrieve_evidence → plan_answers → audit_coverage
  → 保存只读报告 / 等待明确问题的用户确认 / 等待补充观察
```

- `observation_adapter.py` 接收现有 `BrowserSnapshot` 的 JSON，校验结构、拒绝超大快照，不静默截断。普通控件当前值、凭据值、图片及外来的知识映射不进入分析；仅在题干证据明确时保留学校等少量记录锚点。题干/上下文仍可能含个人资料，**不能把该快照称为匿名数据**。
- `form_recognition.py` 核验题干归属，只有分组证据充分的“是／否”才合并为完整选择题。保留文本、单选、多选、下拉、日期、附件、级联的差异；题干冲突、选项缺失、重复目标、未展开栏目和未读嵌入区域均明确记录。覆盖范围只能是当前可见文档，永远不声称整份申请已完成。
- `answer_planning.py` 先查当前有效的已确认事实。本人和紧急联系人、学历和学位、实习和正式工作分别处理；本科与硕士须通过学校/学历等实际锚点唯一对应，不按数组位置匹配。已确认的站点具体答案可覆盖通用偏好，已撤销答案不从旧偏好复活。
- 无确定事实时，按当前用户、简历版本、站点和经历主体调用已有 pgvector HNSW + GIN + RRF 检索，每轮最多 8 题、每题 3 条。检索结果只是证据引用，**不会自动升级成已确认事实**。本地索引不可用会标明原因，不联网下载、不调用付费模型。
- 单选、多选和日期确认使用对应类型校验，拒绝不存在的选项、无效日期或盲填日期。相同事实的重复题只问一次；冲突选项不会让用户反复覆盖同一答案。确认通过原生版本化事实服务保存，下一轮即能匹配，不等待网页填写成功或异步向量索引完成。
- 只读报告、待确认问题、任务检查点及 Inbox 结果在同一事务提交。检查点和队列事件只含引用/计数；详细题目和答案保存在 RLS 隔离的 `form_analysis_reports`。分析期间事实、任务、窗口连接变化或快照过期，旧结果不能提交；报告读取也会标记失效原因。

这条独立原生 D2 路径的所有建议仍保留 `executable_actions=[]`、`ready_to_execute=false`，不执行浏览器写入。它仅做确定性匹配；模型难题及实际填写位于文首 D3 本机路径，不由只读报告直接授权。选中的简历内容可作为证据及实体核对来源，但不会自动变成“用户已确认事实”。

#### 开关与接口边界

合成验证使用 `ZHIDA_V2_WORKFLOW_ENABLED=true` 和 `ZHIDA_V2_RECOGNITION_ENABLED=true`，要求显式迁移至 `0008_readonly_analysis`；默认均为 false。旧 D1 任务继续使用原图，不自动换版本。

- `POST /api/v2/runs/{id}/observations`：要求明确窗口会话、连接版本、任务版本、文档标识、采集标识/时间及快照；拒绝本机页、不同申请 URL、过期证据或跨用户会话。重复采集幂等，新的采集使旧待确认题失效，已确认事实保留。
- `GET /api/v2/runs/{id}/analysis`：返回识别报告、只读建议、补观察问题和报告过期原因。
- 原生确认接口按控件类型检查答案；兼容工作台网关仍禁止原生 observations/answers 写入，避免绕过尚未接好的浏览器桥及资料统一入口。

原生快照入口是受鉴权、绑定校验的内部数据契约，不是“网页内容由可信浏览器实时采集”的证明。D3 通过服务端既有会话绑定创建任务/简历/设备/窗口关联，不开放客户端上传 DOM 充当可信采集；原生 D2 入口没有取代它。不得把调用方提交的 `document_id`、`owned` 等标记直接当作真实浏览器认证。

现有扫描器的普通选项列表只保存文本；未伪造不存在的 option value。自定义下拉只读到部分选项、级联路径不完整或空白教育记录无可识别锚点时继续阻止匹配。D3 已接入有界补读和受控写入，视觉/模型及控件实际表现仍需官网验收，不能由合成测试或只读预览代替。

### D2 第二部分：当前工作台的实时只读预览

在已确认连接的填写页进入 **开发检查 → 只连接并读取页面 → 只读识别检查（D2）**。这是预览入口，不是辅助填写按钮，也不会调用旁边旧版的模型审阅流程。

- `POST /api/browser/{session_id}/recognition-preview` 只接受预期 URL 和本次简历 ID，不接受调用方上传的 DOM、窗口 ID 或文档认证标记。现有浏览器操作锁串行执行，并在采集前后核对用户、任务 epoch 和简历绑定。
- 固定已有 page/context/Safari window/tab；不扫描其他窗口，不使用会接管其他标签页的 fallback。连续两次被动采集，对题干、选项、当前值、上下文锚点及覆盖证据计算内部指纹；URL、文档或表单中途变化则丢弃。指纹及当前值不进入预览报告。
- 使用 `adapt_browser_snapshot → normalize_observation`，报告显示题干、栏目、控件类型、选项、必填依据、经历归属和缺口。缺口按题目 ID 关联题干，不按同名字段猜测。前端会丢弃旧会话/简历/网址的晚到结果。
- 不调用模型、不运行 LangGraph、不检索或匹配个人答案、不持久保存报告；不填值、不打开选项菜单、不上传、不保存或提交。旧扫描器会添加 `data-zhida-*` 定位元数据及临时句柄，**只读不是零 DOM 变更**。
- `document_id` 是本次采集生成的标识，不是 Safari 的持久标签页 ID，也不是下一轮授权。临时句柄清理须匹配原窗口、URL 和 document；页面跳转后不读取替换页面的标题或表单。
- 当前值、定位器和经历锚点被投影移除，但题干/选项自身仍可能含个人资料；报告是本机私有开发诊断，不能称为匿名数据。

2026-10-12 光大真实页只读首测：既有登录窗口未刷新，93 个控件归并为 68 道题，报告 40 项证据清晰、28 项待核对。抽查完整性别题、是非题、附件、实习字段及招聘来源 14 个复选项；仍有下拉选项、级联路径和日期精度缺口，学校/专业等条件字段尚未出现。发现生源地说明中的“院校”和父母学历被误归为本人教育记录，已纳入本轮修复。上述数量是**修复前的识别预览**，不是填写成功率，也不能证明动态字段全部读齐。

修复后在同一已登录窗口再次通过前端只读预览：仍为 **93 控件 / 68 题**，**28 项证据清晰 / 40 项待核对**。生源地现为个人基本信息中的级联选择，经历归属“不适用”；性别为同题“男、女”单选。亲属类字段在归一化中独立标记 `third_party`，不可借用本人事实，教育/语言等重复记录仍要求明确对象。更严格的主体隔离把之前过度乐观放行的家庭字段转为待核对，因此不能用“清晰项变多”代替正确率验收。前端缺口列表现在关联完整题干和栏目。后端重载后通过重新明确绑定保留的 Safari 窗口继续，未重新登录或刷新官网；一次窗口切换未赶上 10 秒倒计时而被拒绝，重试后成功，这个连接交互仍有改进空间。

上述只读现场发现的问题用于文首 D3 的改进与回归；只读预览本身仍不是完整填写通过的证明。

### 隔离回归测试

不要直接加载旧 `.v2-runtime/native/synthetic-tests.env` 运行全套测试：其中历史 Redis 地址可能仍指向正在使用的工作台。新的测试启动器会校验 `zhida_test_*` 数据库和 RabbitMQ vhost，只在专用 17379/17380 端口启动自己的临时 Redis，覆盖旧 Redis 配置，测试完仅关闭自己启动的进程：

```sh
# backend 目录；先准备专用测试 DB/vhost 与本地 Redis 二进制
.venv-v2/bin/python -m pip install -r requirements-v2-dev.txt
.venv-v2/bin/python scripts/test_v2_isolated.py -- -q tests
```

默认禁用实际 embedding 与测试建库；指定 `--real-embedding` 时仅允许本地已有模型离线运行，指定 `--allow-create-database` 才允许迁移测试创建合成数据库。不会读取真实 `.env`，不会调用付费模型、修改真实资料或打开招聘网站。端口占用和并行测试会明确退出，不接管未知进程。

整轮默认最长 300 秒，可用 `--timeout` 调整；超时只停止本轮测试子进程，返回 124。真实队列测试采用唯一命名的持久队列，启动最多等待 20 秒，兼容禁止非独占瞬态队列的 RabbitMQ 配置，不放宽 Broker 设置。

2026-10-12 D1 阶段验收记录：以下增强隔离测试 **187 passed，0 skipped**（42.74 秒）；覆盖本地真实向量索引、合成库迁移、真实 RabbitMQ/Celery 到 LangGraph、重复投递消息、新 Worker 恢复、取消/租约失效、部分确认及非超级用户 RLS。前端后端连接契约测试通过，`pip check` 无依赖冲突。

2026-10-12 D2 第一部分验收：增强隔离测试最终 **377 passed，0 skipped**（42.98 秒）。新增覆盖快照适配、题干与控件证据、经历归属、作用域事实匹配、单选/多选/日期确认、确认记忆复用、分析事务回滚、过期快照/事实/窗口连接及租户隔离。两项旧队列测试曾受历史合成任务影响，现已限定自身事件/恢复记录并使用独立队列；修复后连续两次完整回归通过（前一次 376 项，最后新增一项统一字段上限测试）。只有一条第三方 Starlette/AnyIO 弃用警告；编译检查、`git diff --check` 和 `pip check` 通过。本轮没有修改前端，当前 shell 无 `node`，未复跑前端契约测试，不能将 D1 的前端结果视为本轮结果。

2026-10-12 D2 实时预览及现场问题修复验收：最终增强隔离回归 **472 passed，0 skipped**（43.90 秒），前端 Node 测试 **29 passed**（其中预览测试 10 项），TypeScript 与 Vite 构建通过，`pip check` 无冲突。增加了真实采集绑定/文档重载/双快照漂移、只读 API、亲属与个人信息隔离及清理脚本测试；清理 JS 使用 Node VM 执行，未操作真实招聘页。测试启动器会清除 `ZHIDA_*` 环境变量，Node 不在默认 PATH 时应把已安装 Node 的 bin 目录加入 PATH，而不是仅设置 `ZHIDA_TEST_NODE`；首次因此有 4 项测试未找到 Node，补上 PATH 后完整通过。仍只有一条第三方 Starlette/AnyIO 弃用警告。

```sh
.venv-v2/bin/python scripts/test_v2_isolated.py --timeout 180 \
  --real-embedding --allow-create-database -- -q -rs -o faulthandler_timeout=60 tests
```

这些历史 D1/D2 结果不代表真实网申填写通过。当时真实库保持 `0006_workbench_projection`、图未开启；当前迁移/启用和 D3 验证请以文首记录为准。

## 本机已完成的切换（2026-10-11）

本机工作台已经从 SQLite 切到 PostgreSQL，前端网址仍是 **http://127.0.0.1:5173**。本次没有安装 Docker，使用下文的项目专用宿主服务。不要同时启动旧 `app.main:app` 和新网关来分别修改资料。

- 一致快照导入并逐表核验 **680 条业务记录**：6 份简历、1 份主档案、3 条简历确认事实、3 个聊天会话，以及原岗位/投递/审计记录。完整资料 JSON 和旧 ID 保留，不把未确认内容升级成已确认事实。
- 6 个附件独立复制，原文件与经新前端下载的文件 SHA-256 全部一致。原 `data/resumes.db`、`uploads/` 未删除；旧库写入已停止。
- 私有快照、附件清单和导入报告位于 `.v2-runtime/cutovers/20261011-workbench/`，被 Git 忽略。运行附件来自此目录的 `uploads/`，不是原附件目录。
- 7 个真实索引任务均完成，7 个索引头已激活，616 个文本片段及对应 512 维向量已生成；实际主档案查询返回证据，HNSW 与 GIN 索引均存在。使用本地模型，没有付费模型调用。
- 迁移前全量后端测试 **124 passed / 0 skipped**；切换后通过前端代理核对健康、身份、简历、主档案、投递队列、聊天与附件。新检索接口与原岗位推荐 RAG 接口均返回 200，后者确认使用 `evidence-rag-postgres-v2`。隔离无头浏览器验证资料库、主档案及投递工作台页面，无 JavaScript 异常；测试禁止外部请求及资料写入。前端构建及连接测试通过。未进行招聘网站填写测试，LangGraph 未启用。

机器重启后的恢复顺序（不要重复执行 `prepare` 或导入）：

```sh
# 终端一，工作目录 backend
.venv-v2/bin/python scripts/native_backend_v2.py start
.venv-v2/bin/python scripts/native_backend_v2.py health
.venv-v2/bin/python scripts/workbench_v2_app.py start \
  --uploads .v2-runtime/cutovers/20261011-workbench/uploads --enable-assisted
.venv-v2/bin/python scripts/workbench_v2_app.py status

# 终端二，工作目录 frontend
npm run dev -- --host 127.0.0.1
```

只有第一次冻结迁移才使用 `scripts/cutover_workbench.py stage/apply`；两步均要求 `--confirm-old-api-stopped`。它检测备份期间和导入前的源库变化，核验附件，并拒绝覆盖旧备份或非空业务目标。旧通用 `import-sqlite` 是 A–C 阶段的归档工具，不是本次完整工作台切换工具。

回退不是简单重新打开旧 SQLite：新后端启用后可能已有新修改，回退前必须冻结写入并对账，避免丢失切换后的数据。旧登录令牌和浏览器会话没有迁移；本机免登录保持不变，招聘网站连接需要用户重新确认。

## 现有前端连接 PostgreSQL 工作台网关

网关保留现有 `/api/*` 接口与页面功能，但所有工作台读写都经过 PostgreSQL 的 `workbench` schema；不会转发到旧的 SQLite 进程。简历与主档案的变更在同一个事务中投影到 V2 版本资料、知识源及 Outbox。原生 `/api/v2/*` 的资料写接口暂时返回 409，避免两套写入口造成资料不一致；检索和只读接口保留，身份共用工作台 cookie。

先完成备份、显式迁移、附件复制及数量核对，再启动网关。需要旧解析/浏览器模块的依赖，但安装依赖和启动服务本身不会调用付费模型：

```sh
# 工作目录 backend；先加载专用 PostgreSQL/Redis/RabbitMQ 配置。
.venv-v2/bin/python -m pip install -r requirements-v2-gateway.txt
export ZHIDA_STORAGE_BACKEND=postgresql
export ZHIDA_UPLOAD_DIR=/absolute/path/to/independent-v2-uploads
.venv-v2/bin/uvicorn app.backend_v2.gateway:app --host 127.0.0.1 --port 18000
```

`ZHIDA_UPLOAD_DIR` 必须是迁移后附件的独立目录，不能填旧 `backend/uploads`；网关启动时会检查。网关复用已有模型和 Safari 配置，因此旧 `backend/.env` 仍会作为默认配置读取，但不会覆盖进程中显式设置的环境变量。旧 `app.main:app` 入口仍保留用于显式回退，不会由网关自动切回；正式回退前先停止 PostgreSQL 业务写入，避免新旧资料各改一份。

这个过渡网关只服务本机桌面：同时检查客户端回环地址、Host 和写请求 Origin，禁止从局域网或远程反向代理操作本机浏览器；所有 API 响应禁用缓存。不要将它当作公网多租户生产服务。

使用原生基础设施并完成迁移后，可用本项目的宿主进程管理器统一启动网关、4 类 Worker、Outbox 发布器和恢复扫描器。它不自动迁移、不启动基础设施、不打开浏览器：

```sh
# 工作目录 backend；两个目录均须替换为已经核对的实际独立目录。
.venv-v2/bin/python scripts/workbench_v2_app.py start \
  --infra-root /absolute/path/to/native-runtime \
  --uploads /absolute/path/to/independent-v2-uploads --enable-assisted
.venv-v2/bin/python scripts/workbench_v2_app.py status
.venv-v2/bin/python scripts/workbench_v2_app.py stop
```

`runtime.env` 必须为 `0600`；只将运行凭据传给子进程，不传数据库管理员或迁移密码。进程身份、启动时间与私有日志保存在本仓库 `backend/.v2-runtime/workbench-app`，重复启动不会再生成一组 Worker；同一批运行进程对应的配置和附件路径也必须相同。端口占用、部分进程启动失败、健康检查失败会明确报错并保留现场，不会自动重试、清库或杀死未知进程。`stop` 只对身份一致的已记录 PID 请求退出，保留日志和全部数据；退出后再次 `status` 确认。这个本机网关保留免登录体验（`APP_AUTH_REQUIRED=false`），其回环和来源限制不能去掉。进程存活不等于已验证队列消费或招聘网站填写。

前端从 `frontend` 目录使用 `npm run dev -- --host 127.0.0.1`。`vite.config.mjs` 默认把同源 `/api` 转发到 `http://127.0.0.1:18000`，开发页为 `http://127.0.0.1:5173`；`npm run preview` 的 `4173` 端口也使用同一代理。不需要再把 `VITE_API_URL` 写成旧 `8000`。只有明确回退时才设置 `ZHIDA_BACKEND_URL=http://127.0.0.1:8000`；没有静默回退。

前端启动先读取 `/api/health`。新网关返回 `storage=postgresql`、`backend_generation=v2`、`compatibility=true`；本机 D3 启用时还报告 `langgraph_enabled=true`、`workflow_stage=d3_local_runner`。页面显示“已连接新后端 · PostgreSQL”。服务不可用时显示连接错误，不会误导用户重新注册；连到旧服务则明确显示旧后端。健康中的 `workflow_worker_checked=false` 不能当成队列消费已验证。正式静态部署还需由反向代理提供同源 `/api`，Vite 开发代理不是生产服务器。

## 容器方式

需要 Python 3.11 或 3.12，以及已启动的 Docker Engine 和 Docker Compose v2。在仓库根目录执行：

```sh
python3.12 backend/scripts/backend_v2_stack.py init
python3.12 backend/scripts/backend_v2_stack.py doctor
python3.12 backend/scripts/backend_v2_stack.py validate --application
python3.12 backend/scripts/backend_v2_stack.py start --application
python3.12 backend/scripts/backend_v2_stack.py status --application
```

`init` 生成权限为 `0600` 的 `backend/.env.v2.local`，包含随机开发凭据；若文件已存在，拒绝覆盖。该文件不应提交。`.env.v2.example` 只有配置示例。不要将已有生产数据库地址填入本地测试配置。

不带 `--application` 的 `start` 只启动基础设施。带该参数会构建 Python 3.12 镜像，先迁移专用数据库，再启动 API、四种队列 Worker、Outbox 发布器和恢复扫描器。创建容器时不会创建本机 Safari/Chrome 浏览器连接。

### 模型缓存与文件存储

检索使用本地 `BAAI/bge-small-zh-v1.5`，固定 snapshot 为 `46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59`。启动应用前，需要按 `app/backend_v2/embedding.py` 的格式预装模型；运行时设置 `local_files_only=True`，不会自动下载模型。预期目录为：

```text
backend/data/embedding_models/
└── models--Qdrant--bge-small-zh-v1.5/
    └── snapshots/46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59/
```

如果 snapshot 内文件是符号链接，必须同时保留缓存中的 `blobs` 等链接目标。Compose 只将 `backend/data/embedding_models` 只读挂载给 API 和 `rag.index` Worker；不会挂载整个 `backend/data`，也不会将旧 SQLite 或用户附件打包进镜像。宿主目录缺失时 Compose 应拒绝挂载；目录存在但 snapshot 不完整时检索仍会失败，不能把 API 健康检查通过当成模型就绪。

新文件存储使用独立 `document-files` 数据卷，仅由 API 和 `document.parse` Worker 共享，容器路径为 `/opt/zhida/backend/.v2-runtime/files`。镜像预建该目录并授予 UID 10001 的运行用户，目录权限 `0700`；不需要以 root 运行 Worker。宿主进程方式不使用此卷，必须为所有相关进程设置相同的 `ZHIDA_V2_FILE_ROOT`。这是新存储，旧附件不会自动出现其中。

| 服务 | 本机入口 | 说明 |
| --- | --- | --- |
| 新 API | `http://127.0.0.1:18000` | `/docs` 查看接口，`/api/v2/health` 检查 API 与数据库 |
| PostgreSQL | `127.0.0.1:15432` | 数据库 `zhida_v2`，含 pgvector |
| Redis cache | `127.0.0.1:16379` | allkeys-lfu，可淘汰、可重建 |
| Redis control | `127.0.0.1:16380` | noeviction，独立 AOF 数据卷 |
| RabbitMQ AMQP | `127.0.0.1:15674` | 独立 `zhida` vhost |
| RabbitMQ 管理页 | `http://127.0.0.1:15672` | 用户 `zhida`，密码在本地配置 |

所有发布端口仅监听回环地址。调整端口时，同时调整本地配置中的端口项和宿主进程 URL；容器内部使用服务名，不使用这些宿主端口。`doctor` 的 listening 只表示端口有监听者，不能证明认证、迁移或集成测试通过。

PostgreSQL 首次初始化创建两个角色：`zhida_migrator` 用于显式迁移；`zhida_runtime` 是非超级用户、不能绕过 RLS 的运行角色。API 和 Worker 只得到运行角色 URL。初始化脚本授予 `zhida`、`rag` schema 的业务读写权限和迁移角色未来创建表的默认权限；迁移包装脚本只额外授予运行角色读取 `public.alembic_version` 的权限，供健康检查使用。初始化脚本仅在空数据卷第一次启动时执行，已有数据卷不会通过修改环境变量自动重置密码。

V2 默认使用注册/登录接口，不默认免登录。`ZHIDA_V2_LOCAL_USER_ENABLED` 只用于受限本机调试；容器里的来源地址未必是回环，因此不要把它作为容器认证方案。本地 HTTP 开发配置使用 `ZHIDA_V2_COOKIE_SECURE=false`；HTTPS 部署需设为 true。

停止本项目服务并保留数据：

```sh
python3.12 backend/scripts/backend_v2_stack.py stop --application
```

脚本不提供删除数据卷、清空数据库或回退用户资料的命令。不要通过 `down -v` 处理启动或迁移失败。

## 宿主机进程方式

可仅让 Compose 运行基础设施，将 API/Worker 运行在 Python 3.12 虚拟环境中。以下命令从仓库根目录开始；只加载本次生成的 V2 开发配置：

```sh
python3.12 -m venv backend/.venv-v2
backend/.venv-v2/bin/python -m pip install -r backend/requirements-v2.txt
python3.12 backend/scripts/backend_v2_stack.py start
set -a
. backend/.env.v2.local
set +a
cd backend
ZHIDA_V2_DATABASE_URL="$ZHIDA_V2_MIGRATION_DATABASE_URL" .venv-v2/bin/python scripts/migrate_backend_v2.py
.venv-v2/bin/uvicorn app.backend_v2.api:app --host 127.0.0.1 --port 18000
```

每个下列命令在独立终端运行，工作目录为 `backend`，并加载同一份 V2 环境。工作队列按类别分开，等待用户或浏览器的任务应持久化等待状态并释放 Worker。

```sh
.venv-v2/bin/python -m app.backend_v2.cli worker --queue workflow.advance
.venv-v2/bin/python -m app.backend_v2.cli worker --queue document.parse
.venv-v2/bin/python -m app.backend_v2.cli worker --queue rag.index
.venv-v2/bin/python -m app.backend_v2.cli worker --queue maintenance
.venv-v2/bin/python -m app.backend_v2.cli publisher
.venv-v2/bin/python -m app.backend_v2.cli scheduler
```

RabbitMQ 的连接认证、消息投递、Celery 消费与数据库结果是不同验证层。端口就绪不代表 Worker 正常；排查应依次检查发布器、消费者及数据库任务状态，不重复提交业务操作。新版本 RabbitMQ 可能拒绝非持久非独占的辅助队列，应修正消费者配置，不能用无限重连掩盖不兼容。

### 没有 Docker 的 macOS：本项目专用持久服务

`scripts/native_backend_v2.py` 可管理完全独立的宿主基础设施，**不会安装或修改系统软件**，也不会使用其他项目的数据库。它只接收明确指定的 PostgreSQL（含 pgvector）、Redis、RabbitMQ、Erlang、OpenSSL 包路径，复制到被 Git 忽略的 `backend/.v2-runtime/native/bin`，将数据保存到同一私有目录，而不是 `/tmp`。这不是开机自启服务，机器重启后需执行 `start`。

首次准备（工作目录 `backend`；以下路径需要替换成真实已安装包的路径）：

```sh
.venv-v2/bin/python scripts/native_backend_v2.py prepare \
  --postgres-prefix /absolute/postgresql-prefix \
  --redis-server /absolute/redis-server \
  --rabbitmq-prefix /absolute/rabbitmq-prefix \
  --erlang-prefix /absolute/erlang-prefix \
  --openssl-prefix /absolute/openssl-prefix
.venv-v2/bin/python scripts/native_backend_v2.py start
.venv-v2/bin/python scripts/native_backend_v2.py status
.venv-v2/bin/python scripts/native_backend_v2.py health
```

`prepare` 拒绝覆盖已存在的目录，只复制二进制并写配置；`start` 才初始化新专用 PostgreSQL 集群、角色和空库，启动双 Redis、RabbitMQ。认证凭据随机生成，配置权限 `0600`，数据根目录 `0700`；全部监听本机回环，PostgreSQL 使用 SCRAM。端口与 Compose 基本一致，不能同时启动两套。

凭据分离：

- `.v2-runtime/native/runtime.env` 只提供运行角色及 Redis/Broker 连接，可给 API/Worker 使用。
- `.v2-runtime/native/bootstrap.env` 包含迁移 URL 和初始化管理员凭据，**只给迁移/运维命令使用，不要传给 API/Worker**。
- `zhida_runtime` 和 `zhida_migrator` 均不可绕过 RLS，只有独立初始化管理员可建角色及安装扩展。`zhida`、`rag`、`workbench` 的现有表与未来迁移表均有明确权限。

先迁移结构，再启动 API（应用进程和浏览器 Runner 仍留在宿主机；基础设施启动不代表填写功能运行）：

```sh
set -a
. .v2-runtime/native/runtime.env
set +a
(
  set -a
  . .v2-runtime/native/bootstrap.env
  set +a
  ZHIDA_V2_DATABASE_URL="$ZHIDA_V2_MIGRATION_DATABASE_URL" \
    .venv-v2/bin/python scripts/migrate_backend_v2.py
)
# 根据本轮前端接入方案启动对应 API；上面的迁移子进程不会污染父进程运行凭据。
```

停止时先停 API/Worker，再执行 `.venv-v2/bin/python scripts/native_backend_v2.py stop`。它只向 PID、启动时间和命令均匹配自身记录的进程发送停止信号，保留数据库、队列、附件与日志；不会清理其他服务或删除数据。若 PID 已复用、端口被占或启动部分失败，脚本会拒绝接管/覆盖并保留诊断信息。

`test-resources` 只用于正式数据导入**前**的基础设施验收：创建另外的 `zhida_test_native` 数据库及 RabbitMQ vhost，凭据保存到 `synthetic-tests.env`。其 Redis 指向此时仍空的专属实例，因此正式导入后不要再使用这份测试环境；后续回归应启用完全独立测试服务。真实账号资料的迁移必须另行明确执行，不属于 `prepare` 或 `start`。

## SQLite 离线迁移预检

V2 不在启动时读取旧 SQLite。以下是需要替换路径后显式执行的运维示例，不是应用启动步骤。先停止旧库的业务写入，再使用备份命令生成独立快照；不要只复制一个仍在写入、可能带 WAL 的数据库文件。原库应保留备份，先在测试环境核对。

```sh
# 工作目录 backend；路径仅为占位示例，不是实际用户数据库。
.venv-v2/bin/python -m app.backend_v2.cli backup-sqlite \
  --source /absolute/path/to/legacy.sqlite3 \
  --destination /absolute/path/to/frozen-snapshot.sqlite3
.venv-v2/bin/python -m app.backend_v2.cli import-sqlite \
  --snapshot /absolute/path/to/frozen-snapshot.sqlite3
```

`import-sqlite` 默认 dry-run，不写 PostgreSQL。先阅读报告中的表、记录数、归属和未支持内容，再决定是否切换；缺少用户归属时，只有核实后才指定 `--unowned-user`。真正导入必须另外显式传入 `--apply --confirm-frozen-snapshot`，配置迁移目标，并满足目标业务库为空等校验；不能对正在服务的业务库尝试覆盖导入。

此通用归档命令的范围不等于完整工作台迁移：不迁移登录令牌、文件内容或浏览器会话；原记录按租户隔离归档，缺少可靠确认依据的事实保持 `unconfirmed`，不支持的表明确报告。A–C 初始阶段只验证了合成快照。真实工作台迁移使用文首说明的 `cutover_workbench.py`，不能混用两种导入路径。

## 独立集成测试

测试只接受显式提供的专用资源。PostgreSQL 数据库和 RabbitMQ vhost 名必须以 `zhida_test` 开头；Redis 使用独立测试实例，测试只删除自己的随机前缀 key。不要将日常开发库或真实业务库改名以绕过检查。

从 `backend` 目录运行，先设置测试管理员 URL（用于迁移），以及独立测试 Broker、Redis URL。实际凭据留在终端环境或权限受限的临时文件，不写进文档或测试代码：

```sh
.venv-v2/bin/python -m pip install -r requirements-v2-dev.txt
export ZHIDA_TEST_DATABASE_URL='postgresql+psycopg://TEST_USER:TEST_PASSWORD@127.0.0.1:15432/zhida_test_v2'
export ZHIDA_TEST_BROKER_URL='amqp://TEST_USER:TEST_PASSWORD@127.0.0.1:15674/zhida_test_v2'
export ZHIDA_TEST_REDIS_URL='redis://:TEST_PASSWORD@127.0.0.1:16380/0'
export ZHIDA_TEST_CACHE_REDIS_URL='redis://:TEST_PASSWORD@127.0.0.1:16379/0'
export ZHIDA_TEST_REAL_EMBEDDING=1
export ZHIDA_TEST_ALLOW_CREATE_DATABASE=1
.venv-v2/bin/python -m pytest -q tests/test_v2_*.py
```

这些 URL 是需要自行替换的模板，不会自动创建对应测试数据库、账号和 vhost。测试中要以非超级用户验证租户隔离，不能只在迁移管理员下检查查询结果。缺少真实连接时用例会标记 skipped；skipped 不代表集成通过。

`ZHIDA_TEST_REAL_EMBEDDING=1` 启用已缓存本地模型的真实向量测试，不调用付费远程模型。`ZHIDA_TEST_ALLOW_CREATE_DATABASE=1` 明确允许迁移测试创建并删除自己随机命名的 `zhida_test_import_<uuid>` 数据库，要求测试管理员具有对应权限；不要对生产实例启用。

`native_rabbitmq_test.py` 是无 Docker 时的临时测试辅助工具，只接受调用者明确提供的 RabbitMQ/Erlang 二进制路径，生成独立目录、节点和随机凭据，绑定本机回环端口。它不安装软件、不读取已有 broker 数据；按 Ctrl-C 会结束它自己启动的子进程，保留诊断文件。PostgreSQL、pgvector 及 Redis 仍须提供真实服务，不能用内存替身宣称整套验收通过。

本机未安装 Docker，Compose 只做了静态结构检查，容器镜像构建和启动仍需在有 Docker 的环境执行。A–C 初始阶段使用仅含虚构数据的临时 PostgreSQL 17.6/pgvector 0.8.1、Redis 8.2.2 双实例及 RabbitMQ 4.3.6；之后工作台切换使用项目专用持久实例。两者都不能代替 Compose 镜像的运行验证。基础设施测试不调用付费远程模型，也不证明招聘网站填写成功。

## A–C 初始验收记录（2026-10-11，早于工作台切换）

- 新测试全量运行：77 passed，0 skipped；另有一条第三方 Starlette/AnyIO 兼容性警告。
- 真实端到端：API 创建合成简历 → 同事务写 Outbox → RabbitMQ → Celery Worker → 本地 BGE 向量 → PostgreSQL → API 混合检索；重复查询命中向量缓存，证据仍按当前权限和版本回查。
- 检索验证使用 240 条合成中文记录、4 条查询；强制 HNSW 的原始 ANN Recall@20 均为 1.0，与精确检索比较，未用补齐结果冒充 ANN 召回率。该小样本不代表真实招聘表单准确率或生产性能。
- 非超级用户、不可绕过 RLS 的运行角色通过 API 健康检查及文档 Worker 验证；相同题目按经历实体区分记忆，覆盖本科/硕士不串用案例。
- 旧环境的 `candidate_facts_test.py` 和 `bank_form_policy_test.py` 通过；新环境依赖检查与 Python 编译检查通过。
- 本阶段不包含 LangGraph 编排、真实浏览器 Runner、旧工作台切换或用户数据正式迁移；检索证据不等同于自动填写授权。
