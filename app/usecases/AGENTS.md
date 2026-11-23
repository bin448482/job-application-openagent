# app/usecases 目录指南

## 一、目录用途

组织单次业务用例的输入校验与领域判定，不直接访问属性、文件、数据库或网络。当前包含 M0 `doctor`、M2 `sense` 的持久化/聚合结果编排与 M3 `read` 的决策门禁/定向核对/JSON 组装。

## 二、当前文件

- `__init__.py`：声明用例包。
- `doctor.py`：严格校验合成探针输入（含 UTF-8 可编码性），按“清单 → 选择 → 状态 → 解锁 → 前台”的顺序判定；`build_result` 是唯一 doctor 业务结果组装点，CLI 与设备失败共用它。
- `sense.py`：`persist_and_aggregate(store, result, process_log=None)` 先 upsert 快照、再查未关闭会话，调用领域 `aggregate_unread` 与 `stop_reason_for`；两步分别以 `sense/aggregate`、`sense/stop_reason` 记 started→ok/failed（内部异常保留 `error_category=internal` 并继续公共安全出口），不记录正文/身份/路径/异常消息。`build_sense_payload`/`result_payload` 是 M2 唯一 JSON 结果组装点，不读取 XML 或数据库。SQLite 初始化与打开仍由 CLI 在解析/合并 ok 后执行。
- `read.py`：`is_actionable(result)`/`require_actionable` 按值类型检查页面、职位、未读与会话；结构 ok 但字段未知/身份未决仍拒绝决策。`build_read_payload` 是 M3 唯一 JSON 组装点；`targeted_verification_outcome(initial,targeted,queued_keys,*,now)` 要求新 snapshot、同 app/profile、observed_at 在 0–5 秒且不老于 initial，重放/陈旧/跨 profile 分别返回 `unread_unverified/stale_snapshot/identity_ambiguous`；`read_from_device` 在任何门禁或采集前拒绝不可调用 parser，再要求共享设备门禁通过。不调用仓储、模型或动作。
- `payload.py`：M2/M3 共用的 `ReadResult` 七键与 `UnreadSnapshot` 深层序列化唯一实现；用显式字段投影而不 `asdict`，保持 M2 `SIGNAL_KEYS` 顺序与全部字段，`sense`/`read` 均复用。

## 三、行为契约

- 输入必须是恰好包含 `devices/selected_alias/unlocked/foreground_app` 的对象；设备项恰好包含 `alias/state`。
- `alias` 唯一、非空且可按 UTF-8 编码；`state` 仅 `device/offline/unauthorized`；`selected_alias` 为已有别名或 `null`，且同样拒绝不可 UTF-8 编码的孤立代理字符；`unlocked` 为布尔或 `null`；`foreground_app` 仅 `boss/other/unknown`。
- 零设备报 `device_disconnected`；多设备未选择报 `device_ambiguous`；单设备未选择时自动选择。
- 选择后依次判定离线/未授权、锁定或解锁未知、前台不是 BOSS 或未知。
- 失败保留已知字段，未知字段保持 `null`/`unknown`；不伪造业务成功。
- 本模块不导入具体适配器，不启动子进程。CLI 的设备失败只调用 `build_result`，不得在 CLI 重复组装七键对象。
- `sense` 不允许伪造成功的 ReadResult：只有解析器已产出 `status=ok` 且合并有效的结果才可调用仓储；仓储错误由 CLI 映射为三键安全失败对象。
- `sense` 不 claim/start/finish、不消耗发现、不推进已读基线；停因来自纯聚合结果，不在这里重写信号或候选。
- 重复 JSON 对象键在 CLI 共享输入边界或解析器输入边界拒绝，不在本模块另造解析层。
- M3 `read` 不让 `undetermined` 或字段未知进入决策；门禁原因固定 `not_ok/field_unknown/identity_ambiguous/incomplete/no_target`。`read` 不写库、不关闭发现、不推进已验证基线；合成 `actionable=true` 不提供发送授权。
- `read_from_device` 必须在任何采集前调用 `DeviceGate.require_available()`；M4 共享门禁未交付时抛出 `DeviceGateUnavailable`，不得回退到 profile 内任务锁或真实子进程。

## 四、测试

```powershell
python -m unittest tests.test_doctor tests.test_sensor tests.test_sensor_store tests.test_read tests.test_screen
```

## 五、维护要求

1. 修改选择顺序、原因码、门禁或输出字段时，同步更新根 `AGENTS.md`、`app/cli/AGENTS.md`、`docs/operator.md`、M3 契约文档与测试。
2. 新增用例文件时必须有对应里程碑和测试，不预建空模块。
3. 每个函数写 docstring，说明输入、输出与业务含义。

## 六、已知限制

- 当前只有 `doctor`、`sense` 与 `read`；M4+ 的 `round.py`、`message.py`、`job.py` 等尚未创建。M1 `store init` 由 CLI 直接调用 SQLite 适配器，不经本层。
- 真实设备模式只复用 `doctor` 判定；M2 `sense` 只读合成夹具，M3 `read` 只读合成输入且真实采集在共享门禁前拒绝。M3 真实导航/进入/滚动/视觉仍未实现。
