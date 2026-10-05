# 药物警戒案例处理系统

使用 Python 标准库实现的独立原型，覆盖多渠道案例接入、去重、随访更正、严重性医学裁定、分国家报告、逾期升级、跨区域权限和案例合并审计。

## 运行

要求 Python 3.11+。

```bash
python3 app.py --db pharmacovigilance.db
```

默认监听 `127.0.0.1:8201`。首页为 `http://127.0.0.1:8201/`，健康检查为 `/health`。

所有接口使用请求头 `X-User-Id`、`X-Role` 和区域角色必需的 `X-Region`。角色为 `reporter`、`regional_lead`、`medical_reviewer`、`global_admin`。

## 主要接口

- `POST /api/cases`：录入案例，`dedupe_key` 相同则返回已存在案例。
- `GET /api/cases`、`GET /api/cases/{id}`：按权限查询。
- `POST /api/cases/{id}/followups`：用 `expected_revision` 防止覆盖随访。
- `POST /api/cases/{id}/medical-review`：医学审核员更新严重性、死亡和关联性。
- `POST /api/cases/{id}/reports`、`POST /api/reports/{id}/submit`：生成并提交分国家报告。
- `POST /api/cases/{id}/merge`：全局管理员把重复案例合并到主案例（改挂来源登记与随访）。
- `GET /api/merges`、`GET /api/merges/{id}`：查看合并记录及其改挂明细。
- `POST /api/merges/{id}/undo`：全局管理员撤销合并，按改挂记录退回来源登记与随访。
- `POST /api/escalate-overdue`、`GET /api/overdue`：逾期检查与升级。

## 案例合并

全局管理员通过 `POST /api/cases/{id}/merge`（请求体 `target_case_id` 指定主案例）把重复案例并成一条主案例：

- 来源案例的**来源登记（intakes）与随访（followups）改挂到主案例**，随访按主案例的版本序列重新编号，避免唯一键冲突。
- 每条改挂都会写入 `merge_items`，记录其原来属于哪条案例；`GET /api/merges/{id}` 可查看明细。
- 来源案例在合并期间置为 `merging`、完成后置为 `merged`，期间及之后都不接受随访写入（`case_merged` / `case_merging` 冲突）。
- 并发随访通过 `expected_revision` 乐观锁串行：先到的保留，后到的收到 `revision_conflict`；合并本身会抬升主案例版本，合并后旧版本随访同样冲突。
- 合并分两阶段：先置 `merging` 锁，再在一个事务内完成改挂。若中途失败，来源案例停留在 `merging`，再次调用同一合并会从断点整体重试，不会留下只改一半的案例。
- 来源案例若存在**已提交的国家报告**，整条合并标记为 `irreversible` 并写明原因，`undo` 会被拒绝（`merge_irreversible`）；否则标记为 `completed`，可随时 `undo` 按记录退回。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

该实现使用请求头模拟身份，不含生产级登录、签名和密钥管理；SQLite 与标准库 HTTP 服务适合单机原型。分国家规则采用内置严重 15 天、死亡 7 天、非严重 90 天规则，接入真实监管网关前需按当地法规扩展。
