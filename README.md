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
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `GET /api/warehouses`：备缆仓库台账，含各仓总长度、已预占和剩余可用长度。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `POST /api/warehouses`：注册备缆仓库，请求体为`{"name":"..."}`，需`warehouse_admin`角色。
- `POST /api/warehouses/{id}/stock`：登记或追加某光缆区段在仓内的可用长度，请求体为`{"data":{"cable":"SEA-1","segment":"S3","available_km":20}}`，需`warehouse_admin`角色。

## 备缆调拨占用

- 动员（`mobilize`）时必须在`data`中携带`warehouse_id`，系统按`required_spare_km`在该仓对应区段台账上原子预占；余量不足或该仓无此区段台账时拒绝，记录状态不变。
- 接续（`splice`）后按`spare_used_km`实际长度核销，预占中未用部分自动归还仓库余量。
- 取消（`cancel`）时若存在未核销的预占，全部释放回仓库余量。
- 预占与抢修单状态变更在同一事务内完成，并通过条件更新扣减余量，两个调度员同时动员同一段备缆时不会重复占用。
- 演示页底部实时展示各仓库的总长度、已预占和剩余可用长度。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
