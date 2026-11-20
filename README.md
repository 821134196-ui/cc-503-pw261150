# Pointwise 报表构建器（Report Builder）

一个带**指标版本管理、来源血缘、权限范围、快照导出与审计**的报表构建服务。
后端 FastAPI + SQLAlchemy（SQLite/WAL），前端为零构建原生 SPA。

## 核心保证

| 需求 | 实现机制 |
| --- | --- |
| 组合指标 / 维度 / 筛选 / 权限范围 | 报表只存 *引用*（指标 key + 固定版本 id、维度 key、字段名、scope key），执行时由目录服务统一解析 |
| 结果带指标版本与来源血缘 | 每次保存生成**不可变快照**，内嵌完整版本固定血缘（指标版本→字段→物理列→数据源、维度、权限范围），同时写入关系表 `lineage_edges` |
| 指标改版后已保存报表提示影响 | 快照按版本 id 固定，新版本不会静默改变旧报表；`GET /impact` 扫描当前目录，给出 `ok / impacted / broken` 健康度与可切换版本清单 |
| 字段下线 | `active → deprecated → retired` 生命周期；retired 字段使关联报表变 `broken` 并拒绝执行（409），新构建在保存时即被拒绝（422） |
| 允许切换版本 | `POST /switch-version` 基于乐观锁生成新快照，旧快照保留可审计、可回滚 |
| 导出与页面一致 | 预览与导出调用**同一个引擎函数**；导出时把结果与 sha256 校验和冻结到 `export_records`，下载永远返回冻结字节，支持 `/verify` 校验 |
| 并发修改 | 原子条件更新 `UPDATE ... WHERE revision=?`，失败返回 409 `revision_conflict` 并写审计（`result=conflict`） |
| 权限撤销 | 权限在**执行时**按库中实时记录解析，撤销立即生效；行级过滤自动收窄结果，列级允许清单对越权指标/维度脱敏，拒绝访问写 `access.denied` 审计 |

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python -m app.seed                     # 初始化并灌入演示数据（可重复执行）
.venv/bin/python -m uvicorn app.main:app --reload
# 应用界面： http://127.0.0.1:8000/app/
# OpenAPI 文档：http://127.0.0.1:8000/docs
```

演示用户（请求头 `X-Username` 切换）：

- `alice`：全量 + 华东范围
- `bob`：仅华东范围
- `carol`：脱敏范围（不可见成本字段）+ 一个已撤销范围
- `admin`：管理员

## 页面（五个标签页）

1. **构建**：勾选指标（默认 active 版本）/维度、添加筛选、选择权限范围，临时预览或保存生成快照
2. **预览**：按快照执行，展示数据、脱敏/弃用告警与 sha256 校验和
3. **血缘**：快照冻结的版本固定血缘图（指标版本、字段→物理列、数据源、权限范围）
4. **版本切换**：影响分析（broken/impacted 问题清单 + 可切换版本）、版本切换、权限范围切换、不可变快照历史
5. **导出审计**：冻结导出 CSV、下载/校验、导出记录与全量审计日志

## 主要 API

```
# 目录
GET  /api/catalog/metrics | /dimensions | /fields | /scopes

# 报表
POST /api/reports                       创建（生成快照 #1）
PUT  /api/reports/{key}                 更新（expected_revision 乐观锁，追加快照）
POST /api/reports/preview               快照预览或画布临时预览（同一引擎）
GET  /api/reports/{key}/impact          影响分析（健康度/问题/可切换版本）
GET  /api/reports/{key}/lineage         版本固定血缘
GET  /api/reports/{key}/snapshots       不可变快照历史
POST /api/reports/{key}/switch-version  指标版本切换
POST /api/reports/{key}/switch-scope    权限范围切换

# 导出
POST /api/reports/{key}/exports                 冻结一次执行
GET  /api/reports/{key}/exports/{id}/download  下载冻结 CSV（带 X-Export-Checksum）
GET  /api/reports/{key}/exports/{id}/verify    页面/文件一致性校验

# 管理与审计
POST /api/admin/metrics/{key}/versions    发布指标新版本（旧 active 自动 deprecated）
POST /api/admin/fields/{name}/status      active|deprecated|retired
POST /api/admin/scopes/{key}/revoke       撤销权限范围（实时生效）
POST /api/admin/grants                    授予范围
GET  /api/admin/audit                     审计日志（可按 action/target_id 过滤）
```

## 一次完整演示流程

```bash
# 1. alice 构建依赖 cost_usd（已 deprecated）的毛利报表 -> 健康度 impacted
# 2. admin 发布 gross_margin v3，前端「版本切换」页可看到切换选项
# 3. 切换版本 -> revision+1、生成新快照，血缘记录精确版本
# 4. 预览后导出，对比两次 checksum；/verify 证明页面与文件一致
# 5. admin 将 cost_usd 置为 retired -> 报表变 broken，预览/新导出被 409 拒绝，
#    旧导出仍可字节一致地下载
# 6. 两个用户同时保存同一 revision -> 一个 200，另一个 409 且审计留 conflict
# 7. admin 撤销 scope_masked -> carol 立即 403，审计出现 access.denied
```

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
# 27 passed：构建校验、并发（含 6 线程竞速）、版本/字段下线、
# 行级/列级权限与撤销、血缘固定、导出一致性、审计完整性
```

## 代码结构

```
app/
  models.py              目录/权限/报表/快照/导出/审计表模型
  schemas.py             API 契约
  errors.py              领域错误（404/422/403/409）
  audit.py               审计写入
  deps.py                基于 X-Username 的当前用户依赖
  seed.py                可重复执行的演示数据
  services/
    catalog.py           解析、血缘构建、影响分析
    permissions.py       行级/列级范围解析与实时撤销执行
    engine.py            唯一查询引擎（预览与导出共用）、校验和、CSV
    reports.py           快照、乐观锁、版本/范围切换、冻结导出
  routers/               catalog / reports / admin 路由
  static/                零构建 SPA（index.html + app.js + styles.css）
tests/                   27 个 pytest 用例
```
