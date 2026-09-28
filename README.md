# Atlas · LightRAG + Qdrant 知识工作台

本地知识工作台：搜索公开资料、读取并预览正文、上传文件，调用 LightRAG 构建实体关系图谱和三类 Qdrant 向量，再用图谱与检索观察台检查结果。

默认地址：<http://127.0.0.1:8501>。只监听本机。

## 2026-09-21 新增：完整前台流程

| 页面 | 地址 | 能力 |
|---|---|---|
| 资料工作台 | <http://127.0.0.1:8501/?view=collect> | 输入研究主题搜索网页、勾选候选、读取正文、预览并提交构建 |
| 知识图谱 | <http://127.0.0.1:8501/?view=graph> | 展示真实 LightRAG 实体关系；搜索实体、展开邻居、类型筛选、点选节点与关系、追溯正文 |
| 向量数据库 | <http://127.0.0.1:8501/?view=vectors> | 浏览 Qdrant 文本块、实体、关系三个集合的真实向量投影与 payload |
| 检索观察台 | <http://127.0.0.1:8501/?view=retrieve> | 保留此前的相似度检索、分数、上下文与回放 |
| 模型设置 | <http://127.0.0.1:8501/?view=settings> | 设置模型 API、测试连接，保存后立即用于图谱抽取 |

**使用你已有的模型 API：** 打开“模型设置”，选 `Chat Completions 兼容 API`，填完整请求地址（通常以 `/v1/chat/completions` 结尾）、模型名称、API Key，点击“测试连接并保存”。不要把 Key 发到聊天里。连接成功后，在资料工作台选择资料并点“用 LightRAG 构建所选资料”。Ollama 作为可选接入方式保留。

API 采用 `{model, messages, stream:false, temperature:0}` 请求，读取 `choices[0].message.content`。目前适配的是该协议，其他厂商专用协议需增加适配。仅展示调用状态、模型名、耗时、返回长度和服务返回的 token 用量，不展示隐藏思维链。

模型配置保存在 `data/workbench/<workspace>/llm-settings.json`，文件权限为 `600`。切换接口地址时不会自动复用旧接口的 Key。搜索与预览不调用抽取模型；点击构建才把选中的正文交给所配置服务。模型测试只证明 API 可用，实际抽取格式与质量仍需检验。

### 从资料到真实索引

1. 搜索输入主题，返回 3 / 6 / 10 条候选。搜索通过 DDGS 使用公开搜索引擎，无需搜索 Key；遇到限流会明确报错，可改用直接 URL 或上传。
2. 选择候选后读取正文并保存原 URL、采集时间、原文与内容 ID。搜索摘要不参与建库。支持直接上传 UTF-8 TXT、Markdown、文本型 PDF（最多 60 页，无 OCR），或粘贴正文。
3. 选择正文，后台调用 **LightRAG 原生 `ainsert`**，切块（380 tokens / 60 tokens 重叠）、模型抽取、实体关系合并、GraphML 持久化，并写入 Qdrant 的 chunks / entities / relationships 集合。
4. 按 LightRAG 的真实文档状态确认成功；不能仅因 API 返回而标记完成。后台任务在页面切换后继续，事件落盘，完成后提供图谱、向量与检索地址。
5. 图谱点选实体或关系中点可查看描述和来源；默认最多画 160 个实体，搜索全库实体后查看 1–3 层邻居。图谱存储复用 LightRAG 的 NetworkX / GraphML，Qdrant 只存向量，不充当图数据库。
6. 向量页从 Qdrant 读取三个集合，分别拟合 PCA，显示向量点、实际 ID、内容和 payload；三个空间坐标不能直接比较。原有检索页继续展示文本向量相似度查询。

每批最多 10 份资料、总正文 100,000 字符；下载/上传单份最多 4 MB，单份正文超过 60,000 字符会明确提示截断。公网页面采集逐跳校验目标 IP，并固定到已验证的公网地址；不能抓取本机/内网管理服务。只提取页面可直接返回的正文，不绕过登录、付费或反爬限制。

相同正文按内容去重，重复构建已成功文档不会再次抽取；不提供同一文档编辑覆盖或删除。失败文档使用“失败与中断恢复”中的 **重试工作区未完成文档**，这是 LightRAG 原生的工作区级重试，可能处理该工作区所有失败、等待和中断文档，每次给失败文档一次重试机会。进程重启不自动重复提交构建。失败可能留下部分索引，恢复后应刷新并核对文档状态。

新增数据目录：`data/workbench/<workspace>/sources/` 保存正文和来源；`jobs/` 保存任务与事件。图谱和向量地址展示最新状态，不是不可变版本快照。构建期间显示最近一次读取的索引快照，并明确标识正在构建。

**验证边界：** 已真实验证公开搜索和网页正文采集；本地 Embedding、LightRAG 原生构建、图存储、Qdrant 三类向量、失败重试与浏览器交互已验证。构建自动测试使用固定的 LLM 测试响应，模型 HTTP 协议用临时测试服务验证，测试数据与用户工作区隔离；尚未配置或调用你的真实模型 API，不能据此承诺该模型的抽取质量。当前默认工作区 `silk_culture_demo` 包含 42 个丝绸文化示例向量，六份完整示例正文也已放入资料工作台，待配置 API 后可用于构图，不填充虚构图谱。此前的工作区与检索记录仍保存在本机，可通过 `WORKSPACE=retrieval_demo` 切回。

## 哪些复用，哪些新建

| 部分 | 实现 |
|---|---|
| 构建、检索与上下文组织 | 直接复用 LightRAG 1.5.7 Python SDK，`ainsert`、`naive` + `aquery_data` |
| 向量持久化与相似度查询 | 直接复用 qdrant-client 1.19.1，可选 local / server |
| 中文向量模型 | 本机 FastEmbed / ONNX，`BAAI/bge-small-zh-v1.5`，512 维 |
| 资料工作台、图谱、向量图与调用过程 | Streamlit + Plotly 页面，复用旧版 Atlas 视觉方向 |
| 管理界面 | 接入 Qdrant Server 后可打开其官方 `/dashboard`；原有 LightRAG WebUI 可以继续作为独立管理入口 |

**需要补这个轻量页面。** 两个开源组件负责存储和检索，本应用补齐“同一次查询从模型调用到命中点、分数、上下文”的联动观察。未另起完整的管理平台。

原有示例向量查询不需要 Docker、外部模型 API 或 API Key；新增的图谱构建需要在设置页连接 LLM。首次安装/下载 Embedding 模型需要网络，此后示例查询在本机执行。Qdrant local 是官方 Python 客户端内置的本地持久化引擎，执行真实向量检索；它不是独立 Qdrant Server，不提供 Dashboard，也不运行服务端 HNSW 索引。切换 server 模式可以使用独立服务。

## 启动

安装 Python 3.12 后，在终端克隆仓库并启动：

```bash
git clone https://github.com/guobinyu/atlas-lightrag-qdrant.git
cd atlas-lightrag-qdrant
./start.sh
```

脚本创建虚拟环境并按 `requirements.lock.txt` 安装完整锁定依赖；`requirements.txt` 列出直接依赖。模型首次加载下载约 90 MB，并自动导入丝绸文化示例，请等待初始化。已安装过的本地副本直接运行 `./start.sh`。窗口关闭前可用 Ctrl+C 停止。

仓库包含源码、测试、六份丝绸文化示例和配置模板。本机的 `.env`、模型 API Key、`data/` 数据库与模型缓存、虚拟环境及运行日志不提交；换机器后在模型设置页重新配置抽取 API。

如果 8501 已被占用，检查已有应用或运行 `./start.sh --server.port 8502`；不要同时对同一份本地 Qdrant 文件运行两个进程。

内置 6 份项目编写的中文丝绸文化示例资料，共 42 个文本块，涵盖蚕桑与缫丝、丝织工艺与缂丝、云锦宋锦蜀锦、丝绸之路、纹样服饰、保护与非遗传承。它们是入门演示文本，不冒充外部文献或博物馆资料。可试：

- `缂丝为什么称为通经断纬？`
- `云锦、宋锦和蜀锦有什么区别？`
- `丝绸之路怎样促进文化交流？`
- `丝绸为什么要避光保存？`

默认搜索主题也已改为丝绸文化。在资料工作台勾选示例正文即可提交原生 LightRAG 构建；构建前的“待构建”指实体关系抽取尚未完成，预置文本向量已经可以检索。首次启动空工作区且 `SEED_DEMO=true` 时会导入这些示例；连接自己的知识库时请设置 `SEED_DEMO=false`。

## 使用界面

1. 输入问题，设置 Top-K（1–20）和 Cosine 最低相似度（-1–1），点击“开始检索”。参数改变本身不会触发查询。
2. 上方显示真实向量化、Qdrant 调用、返回数量和上下文整理耗时。调用很快时直接完成；可通过回放逐步查看。
3. 图中菱形是本次真实问题向量的投影，绿色圆点和编号对应匹配排名。点击排名或圆点，联动查看原文与来源。
4. “最终上下文”显示 LightRAG 实际采用的片段；“实际请求与向量”显示 collection、workspace filter、Top-K、阈值、向量维度、前 8 个分量和向量摘要。
5. 打开“逐步回放”，拖动步骤观察历史状态；侧栏可以加载最近 50 次记录。回放只读 JSON，不调用模型或 Qdrant。
6. “事件记录”可以导出 JSON。查询失败、没有匹配、绘图补读失败分别显示，不编造结果。

图是经过 L2 归一化后的固定 PCA 二维投影。查询使用同一变换，不重新拟合背景。每次最多读取当前 workspace 的 2,000 个背景点，命中点不在样本中时按其真实 ID 补读。二维距离不能代替高维 Cosine；灰点仅表示未在本次响应中返回。可选连线表示匹配关系，**不是 HNSW 内部遍历轨迹**。

## 旧版 CLI：只导入文本向量

需要生成图谱时请使用上方的前台构建流程。下面保留原来的 chunks-only 命令，适合只查看文本向量的情况。

先停止页面进程，避免本地数据库锁冲突；为业务资料使用独立数据目录和 workspace。复制 `.env.example` 为 `.env` 并调整：

```dotenv
LAB_DATA_DIR=/absolute/path/to/my-retrieval-data
WORKSPACE=my_documents
SEED_DEMO=false
```

```bash
.venv/bin/python cli.py ingest /absolute/path/to/documents
./start.sh
```

支持 UTF-8 `.txt` / `.md` 文件或目录（目录只读下一层文件）。按章节切块，长段落以约 460 字符、80 字符重叠切分。默认中文模型会截断超过 512 tokens 的输入；需要处理复杂长文时应调整切块与模型，并重新建库。

导入使用 LightRAG 官方 `ainsert_custom_kg` 的 **chunks-only** 形式，不调用实体抽取模型、不创建实体关系。这符合本版仅观察文本向量检索的范围，不能把这个示例称为完成了知识图谱抽取。该上游接口没有完整的文档级崩溃恢复保证；这里适合少量本地资料。正式批量建库/建图请使用 LightRAG 原生文档导入与 LLM 配置。

文本块按内容生成 ID，相同文本会复用同一 ID；不要将重复导入解释为多个独立文档副本。本导入功能用于增量添加，不提供文档编辑、删除或自动同步。

CLI 也支持初始化与查询：

```bash
.venv/bin/python cli.py init
.venv/bin/python cli.py query '缂丝为什么称为通经断纬？' --top-k 5 --threshold 0.2
```

## 接入已有 LightRAG + Qdrant Server

无需部署新的 LightRAG API 服务，本应用直接加载 SDK 与已有工作区。停止相关写入程序后，在 `.env` 中填写：

```dotenv
QDRANT_MODE=server
QDRANT_URL=http://127.0.0.1:6333
QDRANT_API_KEY=
LIGHTRAG_WORKING_DIR=/absolute/path/to/lightrag-working-dir
WORKSPACE=your_workspace
SEED_DEMO=false
EMBEDDING_PROVIDER=http
EMBEDDING_URL=http://127.0.0.1:11434/v1/embeddings
EMBEDDING_MODEL=the-exact-model-used-for-indexing
EMBEDDING_DIM=1024
EMBEDDING_API_KEY=
QUERY_PREFIX=
DOCUMENT_PREFIX=
```

`EMBEDDING_URL` 是完整 embeddings 接口地址，遵循 `{model,input}` 请求和 `{data:[{index,embedding}]}` 响应格式。本应用不发送 `dimensions` 参数，接口实际输出必须与配置一致。Key 只在本机 `.env` 配置，不输入聊天窗口。

- **模型、版本、维度、query/document 前缀及预处理必须与建库一致**，仅维度一致不够。如果原系统有自定义预处理，需要在 `lab/embedding.py` 实现同样逻辑。
- LightRAG 本地 KV / 文本块文件也应匹配 `WORKSPACE`；只有一个任意 Qdrant collection 不能当成完整 LightRAG 知识库。
- 按 LightRAG 1.5.7 自动生成模型隔离的集合名，不提供任意 collection 选择。现有集合必须是单个 dense vector、Cosine 度量且维度相同。
- 使用旧版 LightRAG 集合时，先在原系统按官方流程升级迁移。本工具拒绝触发旧集合自动迁移。
- 首次接入外部库时，模型身份依赖操作者提供的正确配置，无法从向量反推模型。随后会保存 `retrieval_lab_embedding.json`，防止意外更换配置。
- 本应用初始化 SDK 时可能创建缺失的空集合和 payload 索引，不是数据库权限意义上的纯只读工具。请指向明确用于观察的工作区；首版保持单应用实例、单任务执行，查询期间暂停其他写入。
- 保留独立 Qdrant Server 的原生 Dashboard；侧栏提供入口。LightRAG 原生 WebUI 不包含在本项目启动进程中。

当前机器没有独立 Qdrant Server 或外部模型凭证。**已经验证的是本地模式的真实集成；外部服务需填写自己的配置后连接验证。**

## 数据、源码与可观察范围

```text
app.py                  单页界面、实时事件消费与回放
lab/engine.py           固定 asyncio worker，持有 LightRAG 与 Qdrant
lab/trace.py            实例级 query_points 委托包装与事件记录
lab/embedding.py        本地模型 / HTTP Embedding
lab/projection.py       归一化、PCA 与异常向量处理
lab/charts.py           Plotly 图表与点选数据
lab/workbench_ui.py     资料、图谱、向量、模型设置页面
lab/workspace.py        采集与原生构建任务、文档状态、工作区重试
lab/sources.py          公网搜索、正文/PDF 提取与来源存储
lab/llm.py              模型 API / Ollama 适配与调用事件
lab/graph.py            图谱过滤、布局与实体/关系检查
examples/               6 份可修改的示例资料
data/models/            本地 ONNX 模型缓存
data/qdrant/            Qdrant 本地持久化数据
data/lightrag/          LightRAG KV、工作区及模型身份记录
data/runs/<workspace>/  每次查询的 JSON 结果与 JSONL 事件
tests/                  契约、投影、真实集成测试
```

事件包含 `query.started`、`embedding.started/completed`、`vector_search.started/completed`、`context.completed`、`query.failed`。记录保存真实问题、参数、原始分数、point ID、chunk ID、原文、最终采用状态和二维坐标快照；不保存完整查询向量或 API Key。

客户端包装器只委托调用一次 `query_points`，异常原样传播到任务边界。它依赖锁定版本中的 `_client` 私有接入点，没有全局 monkeypatch；升级 LightRAG 时必须重新验证契约。完整查询向量仅保留在当前服务端任务内，用于同一次绘图。背景读取、补读与 PCA 处理单独计算，不计入数据库查询耗时。

记录时间是客户端事件时间，数据库耗时是客户端调用耗时；不等于 Qdrant 内部 CPU 时间。标准 API 不提供 HNSW 每层访问节点、完整内部算法轨迹。本版也不接入 Codex 或展示模型隐藏思维链。

## 验证

2026-09-21：35 项测试通过，依赖检查通过。浏览器已验证公开搜索与正文采集、模型设置、图谱节点/关系溯源，以及三类向量集合的切换；非空图谱的界面验证使用隔离的测试工作区。

```bash
.venv/bin/python -m pytest -q tests
.venv/bin/python -m pip check
```

集成测试运行真正的 LightRAG、中文 ONNX 模型和临时 Qdrant 本地库，覆盖一次 embedding / 一次搜索、原始 ID 与 chunk ID 映射、工作区隔离、Top-K 和阈值、无结果、embedding 失败、样本外命中补读、固定 PCA 与纯回放。首次测试会下载或复用模型缓存。测试数据使用临时目录，不改页面中的示例库。

模型：`BAAI/bge-small-zh-v1.5`（MIT），由 FastEmbed 支持的 `Qdrant/bge-small-zh-v1.5` ONNX 工件加载。LightRAG 和 Qdrant 的版权与许可证属于对应项目；`examples/` 内容由本项目编写。

参考：

- <https://github.com/HKUDS/LightRAG/tree/v1.5.7>
- <https://github.com/HKUDS/LightRAG/blob/v1.5.7/lightrag/kg/qdrant_impl.py>
- <https://github.com/qdrant/qdrant-client>
- <https://qdrant.tech/documentation/web-ui/>
