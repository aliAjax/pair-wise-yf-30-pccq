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
- `POST /api/cases/{id}/merge`：全局管理员合并重复案例（`case_ids` 为参与方数组，主案例默认取 URL 上的 id，也可用 `master_case_id` 指定；兼容旧版 `{"target_case_id": id}`）。
- `POST /api/merges/{id}/retry`：对 `in_progress` 的失败合并做整体重试（`{"refresh_snapshots": true}` 可接受期间先到的写入）。
- `POST /api/merges/{id}/revert`：撤销已完成且可撤销的合并，按流向记录退回来源登记与随访。
- `POST /api/merges/{id}/abort`：中止卡住的合并作业，解除随访写入锁。
- `GET /api/merges`、`GET /api/merges/{id}`：合并作业列表/详情（含参与方版本快照与逐条流向记录）。
- `POST /api/escalate-overdue`、`GET /api/overdue`：逾期检查与升级。

## 合并语义

- **两阶段提交**：阶段一写入合并锁（`case_merges = in_progress` 及参与方版本快照）后提交；阶段二在单个事务内完成 intakes/followups 改挂、案例置 `merged`、作业置 `completed`。阶段二失败整体回滚，只留下可整笔重试的作业，不会出现改了一半的案例。
- **留痕与撤销**：每条改挂记录在 `merge_movements` 中保存原属案例、主案例和随访原始版本号；撤销时按记录退回。随访改挂到主案例时会在主案例版本序列内重新编号（原号存 `original_revision`），撤销时恢复原号。
- **不可撤销**：任一参与案例存在已提交的国家报告时，合并完成即标记 `irreversible=1` 并在 `irreversible_reason` 中写明具体报告、国家和提交时间；撤销接口拒绝。撤销时还会实时复核，合并后新提交的报告同样阻止退回（监管报送无法拆分）。
- **合并期间并发**：作业进行中，所有参与案例（含主案例）的随访与新建报告写入返回 `409 merge_in_progress`；写操作经进程内写锁串行化，先拿到锁的请求先落库。若阶段二发现参与案例版本相对快照已变化（如随访先于合并落库），返回 `409 merge_revision_conflict`，已提交的随访保留，管理员可整体重试合并。
- **合并完成后**：被合并案例拒绝随访写入（`409 case_merged`，提示去主案例提交），读取被合并案例会聚合展示主案例的登记、随访和合并记录。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

该实现使用请求头模拟身份，不含生产级登录、签名和密钥管理；SQLite 与标准库 HTTP 服务适合单机原型。分国家规则采用内置严重 15 天、死亡 7 天、非严重 90 天规则，接入真实监管网关前需按当地法规扩展。
