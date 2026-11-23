# tests 目录指南

## 一、目录用途

存放 Python 标准库 `unittest` 测试与纯合成夹具。当前覆盖 M0 `doctor`、M1 状态库/租约、M2 合成传感器/落库与 M3 离线读取/像素/跨屏身份/ADB 采集模拟，不连接真实设备、不调用模型或网络、不读取根 `config.json`；M1/M2 测试只使用临时合成 SQLite 库与合成 XML，M3 还使用合成 RGBA 与 mock subprocess。

## 二、当前文件与子目录

- `__init__.py`：声明测试包，支持按模块运行。
- `test_doctor.py`：M0 CLI 正反例、配置/夹具校验、重复 JSON 键与孤立代理字符、业务状态映射、keyguard token、`mCurrentFocus` 字段边界/整条 focus 完整 Window/单 Window 多组件/前后附加内容解析、设备失败七键映射、stdout 写失败与 `output=failed`、正常帮助 output/exit、过程日志白名单/降级故障注入、CLI 运行窗口内新建的同期日志哨兵不被清理、ADB `started→ok/failed` 与中断仅留 started、调用协议与 fixture 零子进程断言。
- `test_store.py`：M1 合成 ReadResult/Evidence 入库、未知行跳过、同指纹重放静默、queued 取消/延迟、kind 升级、优先级与 kind 过滤、竞争与 TTL、start/renew/finish/recover、blocked 设备原身份隔离、旧 nonce/截止时间、clock_changed、三类事务故障注入、伪造会话键原子回滚、只读查询写锁 `store_busy`、非法 host 的 open_store 失败日志且不建库、仓储打开/操作日志 started/ok/failed 与不回显非白名单元数据、默认不注入 open 成功/失败文件隔离、默认构造无实例降级告警、默认/注入成功操作降级的一次告警与业务隔离、严格输入与 Windows ctypes 停止判定 mock。
- `test_migrations.py`：v1 STRICT 五表/索引、幂等不改行、建表中途与版本写入后回滚、未知版本/非空无版本/结构篡改拒绝、损坏文件、schema 约束、初始化/打开写锁 `store_busy` 与释放后幂等、`store init` 固定 JSON/默认路径/路径越界/损坏与篡改库/失败日志。
- `test_process_log_concurrency.py`：两个独立 spawn 进程与 Barrier 同时写默认生产目录与显式基础路径，断言各自独立 UUID 文件、按 correlation_id 的完整事件序列、JSON 有效、互不覆盖；清理只删子进程明确返回的路径，并以合成哨兵断言不误删同期文件。
- `test_sensor.py`：M2 单页解析、区域角标/TextView/ASCII 整数与巨大整数边界、坏数字节点几何优先、超大行坐标不逃异常、四源正/零/未知冲突、全局标签语义、预览基线、身份缺失/重复、分页最长重叠、成功页 null snapshot、覆盖率、过期/未来快照、未标定 profile、非法 XML/DTD/ENTITY/Unicode/嵌套 hierarchy、manifest/profile 重复 JSON 键、路径越界/链接、独立键算法、领域聚合与有序 stop_reason。
- `test_sensor_store.py`：`sense` 正例两次重放、发现数 1 且不追加事件、终态同指纹不重开、终态旧键与不同 queued 新键、四类停止原因、未标定/坏输入/非法 Unicode/巨大区域不建库、parse 三态日志、聚合/停因成功与内部失败轨迹、日志降级、超大坐标 CLI 固定 JSON、仓储错误三键安全出口、严格参数、同一日志/ correlation 与零设备/网络/进程断言。
- `test_read.py`：M3 五类 `read` CLI fixture、页面分类/unknown 门禁、职位卡规范化与重复身份、职位详情正文/截断、未读行身份与像素后备、阈值/分辨率/损坏/未知格式/显式零角标/像素 count、字段阶梯来源槽位/前级优先/冲突/failed/视觉默认关闭/人工/跨上下文自校验/派生结果篡改、会话身份与方向/重复正文与方向控件/稳定消息键/重复 ID 会话未知/跨屏重叠/新增消息同会话核对/过期、公开解析入口标定/context/合并值类型/profile 校验、定向核对重放/陈旧/错 profile、ReadResult 分派、M1 仓储拒绝非 UnreadSnapshot、undetermined 日志与门禁。
- `test_screen.py`：M3 mock `subprocess.run` 验证 dump/cat/screencap 参数、`shell=False`、5 秒超时、单次 attempt、证据保存成功与失败 `adb_evidence` 日志、serial 前置拒绝、共享门禁先于采集与生产解析接线；不运行真实 ADB。
- `m1_helpers.py`：M1 测试专用的假时钟、合成快照/证据、固定合成 UUID owner/本机 host 摘要、临时库打开与只读行/事件查询辅助；不连接设备。
- `fixtures/`：M0/M2/M3 纯合成输入夹具；见 `fixtures/AGENTS.md`。

## 三、范围边界

- 独立场景协调端维护 `tests/scenarios/` 与 `tests/test_offline_scenarios.py`，私有回归在 `tests/private_regression/`；本目录指南不为这些位置虚构内容，也不得由本 Task 创建或修改。
- 负例由测试进程捕获预期退出码；验收命令本身必须退出 `0`。
- 真实适配器只在测试中 mock `subprocess.run` 与 ctypes；不得实际运行 ADB 或检查真实 PID。`sense`/`read` 测试额外 mock/断言零设备、网络、子进程与任务租约写入口。
- 测试不把被测输出反推成预期，也不使用真实姓名、序列号、聊天、数据库或绝对路径；`fixtures/sensor/expected.json` 与 `fixtures/read/expected.json` 只由测试读取。
- 日志测试覆盖正常追加、白名单拒绝、IO 故障注入与 CLI 固定 `m0_process_log_degraded` 标记；fixture 模式只允许写被忽略日志，M1/M2 只写临时库。每次运行日志必须独立，显式路径也不得退回共享追加；CLI 默认生产日志保留在被忽略目录，测试不得按目录差集删除文件。
- M1/M2/M3 反例必须显式断言失败原因与“任务/设备/事件全部回滚”“未创建业务库”或“未生成可发送身份/未通过门禁”，不能只断言抛出异常。

## 四、运行方式

在项目根执行：

```powershell
python -m unittest tests.test_doctor tests.test_store tests.test_migrations tests.test_sensor tests.test_sensor_store tests.test_process_log_concurrency tests.test_read tests.test_screen
npm test
```

M0–M3 测试不依赖 Node；`npm test` 验证既有评测判据未被破坏。

## 五、维护要求

1. 新增/改名/移动测试或夹具目录时同步更新本文件和根 `AGENTS.md`。
2. 新增测试模块时保持标准库优先，失败信息不回显敏感原文。
3. 每个测试方法写 docstring，说明覆盖的契约与预期。
4. 新增真实设备测试必须另行取得当轮确认，不能混入离线测试。
5. 修改 M1/M2 契约时同步更新 `m1_helpers.py` 或合成夹具，但辅助函数不得替被测代码生成期望结果之外的状态。

## 六、已知限制

- 当前覆盖 M0–M3；M4+ 的决策、动作与调度测试尚未实现。
- 真实 ADB、真实机型、窗口/截图输出与真实消息稳定标识未验证；M1 停止检查器只用注入值或 mock ctypes；M2 只用合成 XML/profile；M3 只用合成 XML/RGBA 与 mock subprocess。
- 独立离线场景与私有回归测试不在本 Task 范围。
- 日志测试验证的是本机 best-effort 行为，不代表磁盘故障时仍能保留完整日志。
