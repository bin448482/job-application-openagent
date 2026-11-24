# app/adapters 目录指南

## 一、目录用途

实现 `app/ports/` 声明的具体通道。当前包含 M0 的 ADB 只读探针、M1 的本机 SQLite 仓储、M2 的 BOSS 合成列表解析、M3 的 BOSS 页面/职位/未读/会话读取、像素纯解析与 ADB 层级/截图采集、M4a 的本机策略文件工作流、M4b A/B 本机子进程决策桥与评测文件存储，以及 M4c profile 独立事实库、固定 OS 共享设备门禁与 restore 活动检查；fixture 输入由 CLI 读取，不属于设备适配器。

## 二、当前文件与子目录

- `__init__.py`：声明适配器包。
- `path_state.py`：M4c 明确路径查询边界；只有 `FileNotFoundError` 是 missing，普通文件是 file，目录/非普通文件是 other，权限或其它 OSError 是 unknown；gate/activity/profile/facts 共用，不把不可判定吞成缺失。
- `adb/`：真实 ADB 探针、M3 层级/截图采集、M5 动作原语与恢复现场观察；见 `adb/AGENTS.md`。`command.py` 是 M3/M5 共用有界子进程边界。
- `boss/`：M2 合成 profile/manifest、XML、四源判定与分页合并，以及 M3 页面/职位/未读/会话读取、像素纯解析与共享夹具 IO；见 `boss/AGENTS.md`。
- `files/`：M4a 本机策略包/base/preview 读取、点前缀暂存、同卷发布与受控清理；M4b 套件读取、本机报告/凭证与留出登记；见 `files/AGENTS.md`。
- `llm/`：M4b A/B 子进程桥、固定三角色分区、20 秒有界超时与固定诊断；见 `llm/AGENTS.md`。
- `sqlite/`：M1 五表 v1、初始化/核验、事务仓储与租约，以及 M4c profile 事实库/固定 OS 共享门禁/restore 活动检查/真实 profile 会话租约、M5 profile v2 动作表/文本额度与受控对账；见 `sqlite/AGENTS.md`。
- `simulated/`：M5 脚本化设备动作执行器，只回放合成观察，不连接真实设备、不启动子进程；见 `simulated/AGENTS.md`。

## 三、边界

- 适配器只返回领域对象或携带安全原因码的失败，不改调度、不决定策略档位、不重试。
- 真实 ADB 适配器只能由显式 `--device --serial` 触发；fixture 模式不得实例化或调用其探针，适配器模块导入必须零副作用。M3 `read` 当前不暴露真实设备入口，真实采集在共享门禁交付前拒绝。
- BOSS 列表适配器只解析合成 XML/profile 并返回 `ReadResult`；M3 读取适配器同样只解析合成 XML/像素并返回登记值类型，不连接设备、不读数据库、不执行导航或动作；M2/M3 都不读取 `expected.json`。
- SQLite 适配器只写项目内本机库；不读取旧库、根 `config.json` 或个人事实；不连接设备或外部服务。
- 文件适配器只读写项目内 `prompts/`、固定 preview、公开/私有套件与被忽略的 `data/evals`，保留原始换行，拒绝越界/链接/BOM/NUL/大小越界；暂存路径不被活动包发现器接纳，发布不覆盖，清理只限本次自有路径。`list` 对链接/junction 候选保留相对路径并标 `valid=false`，不跟随；活动包与快照读取复用同一路径形状和指南读取核心。
- M4b 桥适配器只用显式参数数组、`shell=False`、20 秒超时与有界 stdout；固定 system/developer/user 三角色、无工具无历史，拒绝角色声明不符/坏 JSON/坏编码/超大输出；不重试、不读凭据、不写正文。桥自报角色/工具/历史只是协议声明，不代表第三方 provider 实际能力。
- M4c profile 库只按固定 profile 路径读写事实/元数据/审计并只读现有 M1 状态，不跨 profile 复制；共享门禁固定 Windows `LOCALAPPDATA` 唯一 primary，acquire/verify/release 比较完整 owner/profile 身份，未知版本/额外对象不改写，不做 TTL/PID 自动解锁，路径不写日志/报告；活动检查不释放占用、不改库，M5 起把 `pending/unknown/cancelled` 动作也视为未决。`recover` 只用于原 profile/原库的显式受控接管，要求原 PID 已停止。
- M5 ADB 动作适配器只在显式设备入口使用参数数组、`shell=False`、5 秒超时、每条命令前门禁复核与单次 attempt；不重发原命令，不把 captured 输出写入日志。`execute` 要求已绑定动作与非空命令账本，缺失/未绑定/错绑定在任何子进程前拒绝；动作绑定后的 IME 预读同样要求账本，未绑定动作的 `read_current_ime` 仅服务 reconcile 维护读取。当前 CLI 不暴露真实动作入口，测试只 mock subprocess 或用本机合成子进程验证账本/Job。
- `simulated/` 只服务 M5 离线 CLI/测试；不接受 Evidence 字段，不替代真实设备。
- ADB 命令过程日志只用固定 `adb_devices`/`adb_window`/`adb_hierarchy_dump`/`adb_hierarchy_cat`/`adb_screencap`、固定错误类别和耗时；SQLite 只记 `store` 阶段与固定错误码，不写 serial、窗口原文、SQL 参数、绝对路径或异常；BOSS 解析不写 XML、身份、正文、像素、原始稳定 ID 或夹具路径；文件适配器不写文件内容或绝对路径；桥适配器不写 argv、端点或凭据。
- 不预建猎聘等未实现适配器目录；M4b 桥只服务单次决策评测/运行绑定预留，不提供发送、设备或事实写入。

## 四、维护要求

1. 新增适配器目录时同时创建 `AGENTS.md` 与只含 `@AGENTS.md` 的 `CLAUDE.md`，并更新本索引与根 `AGENTS.md`。
2. 选择器、命令参数、DDL/索引和解析规则必须带合成夹具或 mock 测试；不得把真实设备或个人数据写入公开材料。
3. 每个源文件顶部写用途，每个函数/方法写 docstring。

## 五、已知限制

- 只有 M0 设备清单、解锁与前台组件读取，M1 本机 SQLite 五表/租约，M2 合成 XML 四源解析/分页合并，M3 合成页面/职位/未读/会话读取、像素纯解析与 ADB 层级/截图采集，M4a 策略文件读写，M4b A/B 本机子进程桥与评测文件，以及 M4c profile 事实库/共享门禁/活动检查；真实模型 provider、真实 OS 门禁目录、真机、真实 profile 租约运行、M5 动作对账与设备执行器未实现。
- 真实设备输出格式、机型差异与时序未验证；M1 的停止检查器只用注入值或 mock ctypes 验证；M2/M3 合成 profile 不代表真实版本已标定；M3 真实 ADB 未执行；M4a 只在合成项目根与临时目录验证文件事务；M4b 只用本机合成桥桩/模拟 transport，真实 provider 与私有留出未测。
