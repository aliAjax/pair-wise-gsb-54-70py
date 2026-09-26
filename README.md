# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、海况、船机许可、备缆窗口和接续质量和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8330
```

默认端口为`8330`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面（含各仓库备缆剩余长度）。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情（含该单的备缆预占流水）。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `GET /api/spare-stock`：备缆台账列表，含各仓`available_km`剩余长度，可带`cable`和`segment`参数。
- `POST /api/spare-stock`：登记备缆台账，请求体为`{"warehouse":"...","cable":"...","segment":"...","total_km":20}`，仅`warehouse_keeper`角色。
- `POST /api/spare-stock/{id}/actions/adjust`：按`{"delta_km":5}`增减台账总量，调整后不得低于已占用长度。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 备缆调拨占用

- 仓库管理员（`warehouse_keeper`）按仓库+光缆+区段登记可用备缆长度，剩余长度 = 总量 − 预占中 − 已核销。
- 动员（`mobilize`，`vessel_master`或`dispatcher`）必须在`data`中携带`stock_id`选仓，系统按`required_spare_km`在同一事务内预占；备缆区段与故障区段不一致或余量不足时拒绝，工单状态不变。
- 接续（`splice`）按`spare_used_km`实际核销：少用部分自动归还，超出预占部分需仓库余量兜底，不足则拒绝。
- 取消（`cancel`）自动释放该单未用的预占长度。
- 预占、核销、释放与工单状态变更在同一事务中完成，并发动员不会重复占用同一段余量。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及备缆台账登记、动员预占、接续核销、取消释放和并发动员不重复占用。
