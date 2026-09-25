# 生物样本库知情同意与撤回

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8302`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8302
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `participant`：参与者；`consent`：同意版本；`sample`：样本；`withdrawal`：撤回申请。

## 撤回的按参与者批量执行

撤回不再由人工逐个改样本。委员会批准（`withdrawal/approve`）时，系统按
`participant_id` 生成并冻结清单快照（写入撤回单的 `data.manifest`）：

- `sample_ids` 不得重复，不得包含不存在或属于其他参与者的样本；
- 不得夹带已终结样本（`anonymized`/`destroyed`/`withdrawn`）；
- 必须穷尽该参与者当前所有未终结样本（`collected`/`stored`/`on_loan`），遗漏即拒绝；
- 快照记录每个样本与每个生效中同意的 `id`、`status`、`version`。

执行（`withdrawal/execute`）由服务层一次性核对并处理：

- 以批准快照为唯一清单，重新核对归属、覆盖率、版本与实时状态；
- 任一样本处于 `on_loan`，或清单中任何样本/同意在批准后发生过版本或状态变化
  （包括出现了清单外的新未终结样本、批准后又有同意生效），**整批不生效**，
  返回 HTTP 409 `BatchConflictError`，`reasons` 列出全部原因；
- 核对通过时，在**单个数据库事务**内统一把生效同意置为 `withdrawn`、
  所有清单样本置为 `withdrawn`、撤回单置为 `executed`，条件更新保证并发下
  要么全部生效、要么全部回滚；
- 被拦截的执行也会写入 `execute_blocked` 审计记录并附全部原因；
- 成功执行对每个被终结对象各写一条 `withdraw` 审计，撤回单上的 `execute`
  审计包含样本/同意数量与完整 ID 清单。

`withdrawn` 是样本终态，没有任何转出迁移：处理后的样本不能借出、归还、
重新入库、销毁或匿名。撤回执行后，该参与者也不能再激活新的同意。
状态变化后需重新批准（刷新清单快照）才能再次执行。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

样本销毁和外部机构调用是流程演示，不会自动删除真实存储中的样本。
