# 数仓分层建模与治理平台

面向原始数据落地、清洗转换、分层建模、维度事实设计、分区治理、质量校验、血缘追踪与历史补数的数据仓库工程管理平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 原始落地 | `landing` | 落地批次 | 批次编号、来源系统、落地路径 |
| 贴源层 | `ods` | 贴源表 | 表编号、表名称、来源系统 |
| 明细层 | `dwd` | 明细表 | 表编号、表名称、业务域 |
| 维度层 | `dim` | 维度表 | 维度编号、维度名称、自然键列 |
| 汇总层 | `dws` | 汇总表 | 表编号、表名称、统计粒度 |
| 应用层 | `ads` | 应用表 | 表编号、表名称、业务场景 |
| 分区治理 | `partition` | 分区方案 | 方案编号、数据表名、分区字段 |
| 质量校验 | `quality` | 质量规则 | 规则编号、校验表名、目标字段 |
| 数据血缘 | `lineage` | 血缘关系 | 关系编号、上游表名、下游表名 |
| 历史补数 | `backfill` | 补数任务 | 任务编号、目标表名、开始日期 |
| 清洗规则 | `cleanrule` | 清洗规则 | 规则编号、输入表名、空值策略 |
| 转换编排 | `transform` | 转换节点 | 节点编号、输入表名、输出表名 |
| 数据字典 | `dictionary` | 字典条目 | 条目编号、表名、字段名称 |
| 元数据管理 | `metamodel` | 元数据项 | 元数据编号、对象类型、对象名称 |
| 数据生命周期 | `lifecycle` | 生命周期策略 | 策略编号、数据表名、保留天数 |
| 存储成本 | `cost` | 成本账单 | 账单编号、数据表名、存储大小GB |
| 同步通道 | `sync` | 同步通道 | 通道编号、来源表名、目标表名 |
| 变更审计 | `auditlog` | 变更记录 | 记录编号、操作表名、变更类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 可恢复补数批次

历史补数不再只是登记起止日期，`app/services/backfill_batch.py` 提供了一套可中断、可续做、
成本可追溯的批次能力，通过「扫描批次」把补数任务与存储成本账单贯通。

### 执行语义

1. **有序切片**：启动时按天把日期范围切成有序区间，写入 `backfill_slice` 表，每条记录包含
   `slice_id`、`start_date`、`end_date`、`scan_bytes`、`failure_code`、`checkpoint_seq`；
   跨月切片按目标表 `生效时区` 解释统计月份（如 `America/Los_Angeles`）。
2. **断点续做**：区间按 `checkpoint_seq` 顺序执行，遇到失败即中断，后续区间保持「待扫描」；
   续做从第一个失败区间继续，已成功区间不重复扫描、不重复记账。
3. **水位推进**：完成水位只在从首个区间起连续成功时推进，断裂时停在断点前一个区间；
   任务字段 `失败区间` 始终指向第一个失败区间。
4. **幂等启动**：同一任务重复启动只生效一次，已切片任务再次调用启动接口直接返回提示。
5. **水位冲突拒绝**：覆盖写范围若早于目标表已持久化的最晚完整完成水位，拒绝执行并返回原因；
   追加写不回退水位，不受此限制。拒绝动作同样写变更审计。

### 账单与审计

- 每次真实扫描（含失败重试）都写一条不可变的 `backfill_scan_batch` 扫描批次，记录计划/实际
  扫描量、轮次与 `failure_code`。
- 成本账单落在 `cost` 表，按 **补数任务 × 目标表 × 统计月份** 归集（例如
  `COST_ORDER_202608`），包含 `计划扫描量字节`、`实际扫描量字节`、`重试扫描量字节` 与
  逐轮 `扫描明细`；重试产生的额外扫描只追加明细、累加聚合，绝不覆盖历史账单。
- 每个区间成功或失败、水位推进、启动拒绝都同步写一条 `auditlog` 变更审计。

### 接口

| 接口 | 说明 |
| --- | --- |
| `POST /api/backfill/{id}/batches/start` | 首次启动（幂等），body 可带 `fail_dates` 演练确定性失败 |
| `POST /api/backfill/{id}/batches/resume` | 从第一个失败区间断点续做 |
| `GET /api/backfill/{id}/slices` | 区间切片明细 |
| `GET /api/backfill/{id}/scan-batches` | 不可变扫描批次台账 |
| `GET /api/backfill/{id}/audits` | 该任务的变更审计流 |
| `GET /api/backfill/bills` | 补数驱动的成本账单（可按 task_id/目标表/月份过滤） |
| `GET /api/cost?backfill_task_id=` | 成本页按补数任务过滤，支持展开逐轮扫描明细 |

前端「历史补数」页提供登记、启动、断点续做、批次详情抽屉（切片/扫描台账/账单/审计），
并可一键跳转「存储成本」页核对账单，两个页面通过 `backfill_task_id` 双向打通处置链路。

领域：数据工程
子方向：数仓与数据湖
