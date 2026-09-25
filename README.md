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

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 撤回执行（按参与者一次核对、整批处理）

撤回申请经委员会`approve`后，不再允许人工逐个修改样本。执行分两步：

1. **核对**：`POST /api/entities/<withdrawal_id>/actions`，请求体`{"action":"reconcile"}`。
   系统按参与者返回权威清单：该参与者所有未终结样本（`collected`/`stored`/`on_loan`）与所有生效同意（`active`），不重复、不含他人样本，并写一条`reconcile`审计。
2. **执行**：`{"action":"execute","data":{"executed_at":"...","samples":[{"id","version"},...],"consents":[{"id","version"},...]}}`。
   规则引擎把回传清单与当前状态逐项核对。出现下列任一情况，**整批不生效**（HTTP 409 `BatchBlockedError`，`reasons`列出全部原因），撤回单和所有相关对象保持原状，并写`execute_blocked`审计：
   - `SAMPLE_DUPLICATE`/`CONSENT_DUPLICATE`：清单内重复；
   - `SAMPLE_FOREIGN_OR_TERMINAL`/`CONSENT_FOREIGN_OR_INACTIVE`：混入他人或已终结对象；
   - `SAMPLE_NOT_COVERED`/`CONSENT_NOT_COVERED`：未覆盖该参与者全部未终结样本或生效同意；
   - `SAMPLE_ON_LOAN`：任一样本处于借出状态；
   - `SAMPLE_VERSION_CHANGED`/`CONSENT_VERSION_CHANGED`/`WITHDRAWAL_VERSION_CHANGED`：核对后版本发生变化；
   - `MISSING_RECONCILIATION`：未先核对。

全部通过时，生效同意、全部未终结样本、撤回单在**单个数据库事务**内分别终结为`withdrawn`/`withdrawn`/`executed`，每个对象各留一条审计。`withdrawn`是样本终态，不允许再`loan`/`store`/`return`；样本的`withdraw`动作也不能通过通用 action 接口绕过批量编排。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

样本销毁和外部机构调用是流程演示，不会自动删除真实存储中的样本。
