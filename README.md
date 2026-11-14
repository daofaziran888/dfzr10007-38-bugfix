# 数据洞察与自助分析平台

面向业务分析师的自助式数据洞察平台。覆盖数据集市构建、自助SQL查询、可视化拖拽分析、分析看板协作、指标异动归因、分析模型沉淀、查询优化建议与数据故事编排的完整洞察链路。

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
| 数据集市构建 | `mart` | 集市定义 | 集市编号、集市名称、数据源表 |
| 自助SQL查询 | `sqlpad` | 查询脚本 | 脚本编号、脚本名称、SQL语句 |
| 拖拽可视化 | `vizdrag` | 可视化方案 | 方案编号、方案名称、数据源绑定 |
| 看板协作管理 | `collab` | 协作看板 | 看板编号、看板名称、协作成员 |
| 指标异动归因 | `anomaly` | 异动记录 | 记录编号、指标名称、异动方向 |
| 分析模型沉淀 | `model` | 模型注册 | 模型编号、模型名称、模型类型 |
| 查询优化建议 | `opt` | 优化建议 | 建议编号、目标查询、优化类型 |
| 数据故事编排 | `story` | 故事模板 | 模板编号、故事标题、分析主题 |
| 数据准备流水线 | `etl` | 准备任务 | 任务编号、任务名称、输入源 |
| 数据目录检索 | `catalog` | 目录条目 | 条目编号、数据集名称、业务域 |
| 数据采样管理 | `sample` | 采样配置 | 配置编号、数据源名称、采样算法 |
| 同环比分析 | `compare` | 对比方案 | 方案编号、对比指标、基准周期 |
| 分析导出中心 | `export` | 导出任务 | 任务编号、数据源名称、导出格式 |
| 分析告警规则 | `alarm` | 告警规则 | 规则编号、监控指标、告警条件 |
| 数据定时刷新 | `refresh` | 刷新策略 | 策略编号、目标数据集、刷新周期 |
| 查询审计日志 | `audit` | 审计记录 | 记录编号、查询语句、执行人 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 数据采样链路（sample / etl）

采样登记、样本输出表与数据准备读取三段链路的约定：

- **可选字段随配置落库**：样本比例、随机种子、分层字段、输出表名与三个必填字段
  一起持久化，不再静默丢弃；随机种子统一为整数，同配置 + 同种子 + 同比例的采样
  结果可复现。
- **同种子幂等**：`POST /api/sample/{id}/generate` 对同一配置同一随机种子只保留
  一个采样结果，重复请求直接复用既有结果；结果清单按（配置、种子）去重。
- **输出表并发互斥**：两个任务写同一张输出表时按表名互斥，只允许一个成功，另一个
  立刻被拦截，不排队、不覆盖。
- **写入失败整体回滚**：修改/生成在写入前保存完整快照，任何写入报错（可用
  `values.force_fail=true` 模拟）都回滚到旧配置，例如旧 `随机种子=41` 仍可回读。
- **暂停不产出**：已暂停/已关闭的配置（如 SAMP-0003）不生成新输出表，且不影响
  其他配置（如 SAMP-0001 的 seed=42）。
- **流水线读取分层输入**：ETL 输入源按样本输出表名关联采样配置，
  `GET /api/etl/inputs/{id}` 返回关联配置、随机种子、采样结果与非空分层输入。
- **存量补齐**：`app/services/sample.py` 的 `BACKFILL_CATALOG` 维护
  SAMP-0001～SAMP-0003 的数据源名称、采样算法、样本比例、随机种子口径，服务启动时
  幂等补齐缺失字段（占位的「样例N」文本视为缺失），已填字段不覆盖。

采样相关接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/sample` | 登记采样配置，可选字段一起落库 |
| PUT | `/api/sample/{id}` | 修改配置；`force_fail=true` 模拟写入失败并回滚 |
| POST | `/api/sample/{id}/generate` | 生成/复用采样结果（幂等、输出表互斥） |
| GET | `/api/sample/results` | 样本输出表清单（同配置同种子去重） |
| GET | `/api/etl/inputs/{id}` | 流水线输入详情（关联采样配置与分层输入） |

回归验证：`cd backend && .venv/bin/python -I tests/test_sample_flow.py`
与 `.venv/bin/python -I tests/http_smoke.py`。

领域：数据工程
子方向：数仓与数据湖
