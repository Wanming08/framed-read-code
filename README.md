<p align="center">
  <a href="LICENSE"><img alt="License MIT" src="https://img.shields.io/badge/License-MIT-365a68?style=flat-square"></a>
  <img alt="Python 3.12" src="https://img.shields.io/badge/Python-3.12-365a68?style=flat-square&logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/API-FastAPI-365a68?style=flat-square&logo=fastapi&logoColor=white">
  <img alt="Vue 3" src="https://img.shields.io/badge/Frontend-Vue_3-365a68?style=flat-square&logo=vuedotjs&logoColor=white">
  <img alt="RocketMQ 5.3.4" src="https://img.shields.io/badge/Queue-RocketMQ_5.3.4-365a68?style=flat-square">
</p>

<p align="center">
  <img alt="SQLAlchemy 2" src="https://img.shields.io/badge/ORM-SQLAlchemy_2-365a68?style=flat-square">
  <img alt="MySQL 8.0" src="https://img.shields.io/badge/Database-MySQL_8.0-365a68?style=flat-square&logo=mysql&logoColor=white">
  <img alt="Redis 7.4" src="https://img.shields.io/badge/Cache-Redis_7.4-365a68?style=flat-square&logo=redis&logoColor=white">
  <img alt="MinIO" src="https://img.shields.io/badge/Storage-MinIO-365a68?style=flat-square">
  <img alt="Qdrant 1.18.2" src="https://img.shields.io/badge/Vector-Qdrant_1.18.2-365a68?style=flat-square">
</p>

<p align="center">
  <img alt="Vite 7" src="https://img.shields.io/badge/Build-Vite_7-365a68?style=flat-square&logo=vite&logoColor=white">
  <img alt="Docker Compose" src="https://img.shields.io/badge/Infra-Docker_Compose-365a68?style=flat-square&logo=docker&logoColor=white">
  <img alt="FFmpeg" src="https://img.shields.io/badge/Media-FFmpeg-365a68?style=flat-square&logo=ffmpeg&logoColor=white">
  <img alt="Tesseract" src="https://img.shields.io/badge/OCR-Tesseract-365a68?style=flat-square">
  <img alt="SiliconFlow" src="https://img.shields.io/badge/Models-SiliconFlow-365a68?style=flat-square">
</p>

<p align="center">
  <a href="#界面演示">🖼️ 界面实测</a> ·
  <a href="#快速开始">⚡ 快速开始</a> ·
  <a href="#架构概览">🧩 架构概览</a> ·
  <a href="#api-文档">📡 API 文档</a> ·
  <a href="#来源与许可">⚖️ 许可</a>
</p>

# 帧读

**Python 后端的Agent视频内容分析与证据工作台。** 导入视频，选择目标，整理可核对的结论；点击报告中的时间引用，回到原片。

帧读使用 Python API、独立任务 worker 和 Vue 工作台，将语音转写、关键帧 OCR、分块检索与 Planner / Executor / Critic 循环组织成一个可追踪的分析流程。适合课程笔记、会议内容整理、带字幕的讲解和内容审查。

## 界面演示

### 📚 素材库

![实际登录账号的素材库与上传视频](assets/readme/03_library.jpg)

### 🎬 视频工作台

![真实比赛视频的原片、转写、分析报告与时间引用](assets/readme/07_report.jpg)

<details>
<summary>🔐 登录与注册</summary>

| 🔑 登录 | 👤 注册 |
| --- | --- |
| ![实际登录入口](assets/readme/01_login.jpg) | ![实际注册入口](assets/readme/02_register.jpg) |

</details>

<details>
<summary>📤 上传与分析流程</summary>

### 📤 视频上传

![真实视频的分片上传进度](assets/readme/04_upload.jpg)

### 🎯 模式与目标

![为实际上传的视频选择模式并设置分析目标](assets/readme/05_goal.jpg)

### ⏳ 后台分析

![实际模型调用、阶段状态与生成的任务计划](assets/readme/06_processing.jpg)

</details>

<details>
<summary>🔎 证据、转写与执行详情</summary>

### 🔎 证据检索

![从实际视频上下文检索时间戳证据](assets/readme/08_evidence.jpg)

### ⏱️ 原片定位

![点击报告时间引用后定位实际视频](assets/readme/09_reference.jpg)

### 📄 转写全文

![实际语音识别生成的转写全文](assets/readme/10_transcript.jpg)

### 🧭 执行详情

![实际生成的计划、阶段耗时与复核状态](assets/readme/11_trace.jpg)

</details>

<details>
<summary>💬 计划、追问与产物</summary>

### 📝 调整计划

![编辑真实分析任务生成的计划](assets/readme/12_plan.jpg)

### 💬 视频追问

![基于实际视频的模型追问结果](assets/readme/13_followup.jpg)

### 👍 结果反馈

![真实分析结果的反馈入口与已保存状态](assets/readme/16_feedback.jpg)

### 🎛️ 产物选择

![实际视频的分析产物选择界面](assets/readme/14_modes.jpg)

</details>

<details>
<summary>📡 API 界面</summary>

![实际后端生成的 Swagger 接口文档](assets/readme/15_api.jpg)

</details>

## 功能

- 🎯 **目标驱动的分析**：通用、学习、审查、创作四种产物模式；自动选项将目标路由到具体模式。
- 🎙️ **语音与屏幕文字融合**：ASR 和 OCR 两个分支并行构建 VideoContext，按时间窗口合并证据。
- 🔎 **长视频检索**：五分钟分块，结合向量、关键词与时间线索筛选上下文；外部检索失败时有本地降级路径。
- 🧠 **有限 Agent 循环**：Planner 拆解任务、Executor 生成产物、Critic 校验；受轮次、时间、Token 和可配置费用预算限制。
- ♻️ **可恢复的任务处理**：API 与 worker 分离，RocketMQ 投递、租约适配、内容锁、阶段检查点和失败台账共同约束重复执行。
- ⏱️ **可核对的阅读体验**：原片、转写、报告同屏；底部时间引用直接来自当前报告，不额外调用模型。
- 🖥️ **完整交互入口**：分片上传与断点续传、链接导入、证据检索、追问、计划调整、反馈、音频和 Markdown 导出。
- 📊 **运行可观测性**：SSE 展示阶段状态；trace 区分阶段耗时、请求次数、实际 usage 与估算成本。

## 快速开始

### 🖥️ A. 只看界面

需要 Node.js 22.12+，无需后端或模型密钥：

```bash
git clone https://github.com/Wanming08/framed-read-code.git
cd framed-read-code/frontend
npm ci
npm run dev -- --host 127.0.0.1
```

打开终端显示的地址，并加上 `/?demo`，通常是 <http://127.0.0.1:5173/?demo>。默认进入工作台，点击「素材库」可查看首页。

### 🚀 B. 运行完整项目

需要 Docker Compose；在仓库根目录运行：

```bash
git clone https://github.com/Wanming08/framed-read-code.git
cd framed-read-code
```

```bash
./scripts/dev-up.sh
curl --fail http://127.0.0.1:9090/health
```

脚本首次生成私有 `.env` 和基础设施密码，构建镜像、启动服务、执行数据库迁移并初始化 RocketMQ。没有模型密钥时，可先注册、登录、上传和播放视频。

在生成的 `.env` 中填写 `SILICONFLOW_API_KEY`，再次运行启动脚本即可开启分析 worker。默认组合为 DeepSeek-V3.2、TeleSpeechASR 和 bge-m3，分别承担文本生成、语音识别与向量生成。供应商替换需要核对接口和响应格式兼容。

另开终端运行前端：

```bash
cd frontend
npm ci
npm run dev -- --host 127.0.0.1
```

| 入口 | 默认地址 |
| --- | --- |
| 前端 | `http://127.0.0.1:5173`，端口占用时以终端输出为准 |
| API 健康检查 | `http://127.0.0.1:9090/health` |
| Swagger 文档 | `http://127.0.0.1:9090/docs` |
| OpenAPI | `http://127.0.0.1:9090/openapi.json` |

## 架构概览

```mermaid
flowchart LR
    UI["Vue 3 工作台"] --> API["FastAPI API"]
    API --> MQ["RocketMQ Proxy"]
    MQ --> Worker["Python worker"]
    Worker --> Context["ASR + OCR<br/>VideoContext"]
    Context --> Retrieval["分块摘要与混合检索"]
    Retrieval --> Agent["Planner → Executor → Critic"]
    Agent --> SQL[("MySQL 检查点与结果")]
    API --> SQL
    API --> Storage[("MinIO 媒体对象")]
    Worker --> Storage
    Retrieval --> Vector[("Qdrant 向量")]
    Worker --> Redis[("Redis 锁 / 状态 / 事件")]
    Redis --> API
    API -. SSE 阶段事件 .-> UI

    classDef interface fill:#e8f0f3,stroke:#365a68,color:#243d47
    classDef processing fill:#edf3ed,stroke:#637b68,color:#344c39
    classDef persistence fill:#f6efe3,stroke:#a08048,color:#68512b
    class UI,API interface
    class MQ,Worker,Context,Retrieval,Agent processing
    class SQL,Storage,Vector,Redis persistence
```

<details>
<summary>🧠 Agent · Critic</summary>

```mermaid
flowchart TD
    Planner["Planner<br/>目标与任务"] --> Executor["Executor<br/>结构化草稿"]
    Executor --> Critic["模型 Critic<br/>目标覆盖 · 内容 · 证据"]
    Critic --> Local["Python 校验<br/>结构 · 时间 · ASR/OCR 原文 · 结论绑定"]
    Local --> Passed{"passed"}
    Passed -->|true| Report["报告"]
    Passed -->|false| Retry{"剩余轮次<br/>默认最多 2 轮"}
    Retry -->|有| Repair["按反馈重写<br/>定向补证 / 缺失要求时重规划"]
    Repair --> Executor
    Retry -->|无| Structure{"产物结构有效"}
    Structure -->|是| Warning["Critic 警告"]
    Warning --> Report
    Structure -->|否| Failed["FAILED"]

    classDef generation fill:#e8f0f3,stroke:#365a68,color:#243d47
    classDef review fill:#edf3ed,stroke:#637b68,color:#344c39
    classDef caution fill:#f6efe3,stroke:#a08048,color:#68512b
    classDef failure fill:#f6e8e7,stroke:#a5635d,color:#713e39
    class Planner,Executor,Report generation
    class Critic,Local,Passed review
    class Retry,Repair,Structure,Warning caution
    class Failed failure
```

</details>

<details>
<summary>📨 异步分析 · 消息与事件</summary>

```mermaid
sequenceDiagram
    participant UI as Vue 工作台
    participant API as FastAPI
    participant MQ as RocketMQ
    participant Worker as Python worker
    participant SQL as MySQL
    participant Redis as Redis

    UI->>API: POST /analysis/ai
    API->>Redis: 去重 / 限流 / 任务标记
    API->>MQ: 分析任务
    API-->>UI: 202 Accepted
    UI->>API: GET /analysis/analysis-events
    MQ->>Worker: 消费任务
    Worker->>Redis: 内容锁 / 任务租约

    loop VideoContext → 检索 → Planner → Executor → Critic
        Worker->>SQL: 阶段检查点
        Worker->>Redis: 状态 / Pub/Sub
        Redis-->>API: 阶段事件
        API-->>UI: SSE task-status
    end

    Worker->>SQL: 报告 / 转写 / 终态检查点
    Worker->>Redis: COMPLETED
    Redis-->>API: 终态事件
    API-->>UI: SSE COMPLETED / 报告
    Worker->>MQ: ACK
```

</details>

## API 文档

当前提供 **31 个业务方法与路径**，按 HTTP 方法 + 路径计数，不含自动文档端点和 OPTIONS。认证使用 Bearer 会话令牌；普通 JSON 使用 `{code,message,data}` 信封，SSE 和文件响应采用各自格式。

| 能力 | 代表接口 |
| --- | --- |
| 账号 | `POST /user/register`、`POST /user/login` |
| 上传 / 素材 | `POST /media/init-upload`、`POST /media/upload-chunk`、`GET /media/list` |
| 原片播放 | `GET /media/playback` |
| 分析与状态 | `POST /analysis/ai`、`GET /analysis/analysis-status` |
| 实时事件 | `GET /analysis/analysis-events` |
| 计划 / 轨迹 | `GET /analysis/agent-plan`、`GET /analysis/agent-trace` |
| 证据 / 追问 | `GET /analysis/evidence-search`、`POST /analysis/follow-up` |
| 转写 / 导出 | `POST /analysis/transcribe`、`GET /analysis/download` |
| 管理 | `GET /admin/failed-analysis` |

## 能力边界

- ASR 当前以约 60 秒为片段粒度；左侧全文是纯文本，不提供逐句字幕对齐。
- 摘要、ASR 分片与 OCR 帧循环目前各自串行；ASR / OCR 两个整体分支并行。长视频耗时不能直接从视频时长推算。
- 检查点复用已经完成并保存的阶段，不能恢复一半的供应商 HTTP 请求；部分阶段还不是逐片保存。
- Critic 未通过时可能返回带警告的报告，时间引用仍应结合原片核对。
- trace 的费用需要配置模型单价；未返回 usage 的请求不能保证被完整计量，统计不替代供应商账单。
- 当前 Compose 面向本地开发，未提供生产高可用或公网部署 SLA。Jev、动态视觉路由和直接视频视觉模型还未接入。

## 目录

```text
frontend/             Vue 工作台
  src/                页面、上传、SSE 与播放器
backend/              Python 3.12 后端
  app/api/            HTTP 接口与认证
  app/agent/          Planner / Executor / Critic
  app/services/       媒体、检索、检查点与任务
  app/integrations/   模型与基础设施适配
  app/workers/        消息消费与租约
  migrations/         Alembic 与基线 SQL
scripts/              启动与 RocketMQ 初始化
docker/               MinIO 镜像构建
rocketmq/             Broker 配置
docker-compose.yml    本地服务编排
.env.example          配置模板
LICENSE               MIT 许可证
```

## 来源与许可

帧读由 [Wanming08](https://github.com/Wanming08) 开发维护，采用 [MIT License](LICENSE)。Python 后端、任务 worker、运行适配及前端界面与交互改造为本项目的开发成果，新增和改编贡献署名 `Wanming08`。

业务迁移以 [Xiaoc7r/DOVideo-AI 的固定版本](https://github.com/Xiaoc7r/DOVideo-AI/tree/df23f36226495f9cd4e7f740ea8a3b8cac51fbf2) 为参考。沿用或改编的上游前端代码、提示词与数据库 SQL 保留原 MIT 许可及 `Copyright (c) 2026 Majst`；本项目贡献补充 `Copyright (c) 2026 Wanming08`。
