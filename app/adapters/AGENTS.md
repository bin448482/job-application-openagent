# app/adapters 目录指南

## 一、目录用途

实现 `app/ports/` 声明的具体通道。当前包含 M0 的 ADB 只读探针、M1 的本机 SQLite 仓储与 M2 的 BOSS 合成列表解析；fixture 输入由 CLI 读取，不属于设备适配器。

## 二、当前文件与子目录

- `__init__.py`：声明适配器包。
- `adb/`：真实 ADB 探针实现；见 `adb/AGENTS.md`。
- `boss/`：M2 合成 profile/manifest、XML、四源判定与分页合并；见 `boss/AGENTS.md`。
- `sqlite/`：M1 五表 v1、初始化/核验、事务仓储与租约；见 `sqlite/AGENTS.md`。

## 三、边界

- 适配器只返回领域对象或携带安全原因码的失败，不改调度、不决定策略档位、不重试。
- 真实 ADB 适配器只能由显式 `--device --serial` 触发；fixture 模式不得实例化或调用其探针，适配器模块导入必须零副作用。
- BOSS 列表适配器只解析合成 XML/profile 并返回 `ReadResult`，不连接设备、不读数据库、不执行导航或动作；M2 不读取 `expected.json`。
- SQLite 适配器只写项目内本机库；不读取旧库、根 `config.json` 或个人事实；不连接设备或外部服务。
- ADB 命令过程日志只用固定 `adb_devices`/`adb_window`、固定错误类别和耗时；SQLite 只记 `store` 阶段与固定错误码，不写 serial、窗口原文、SQL 参数、绝对路径或异常；BOSS 解析不写 XML、身份、正文或夹具路径。
- 不预建模型桥、猎聘等未实现适配器目录。

## 四、维护要求

1. 新增适配器目录时同时创建 `AGENTS.md` 与只含 `@AGENTS.md` 的 `CLAUDE.md`，并更新本索引与根 `AGENTS.md`。
2. 选择器、命令参数、DDL/索引和解析规则必须带合成夹具或 mock 测试；不得把真实设备或个人数据写入公开材料。
3. 每个源文件顶部写用途，每个函数/方法写 docstring。

## 五、已知限制

- 只有 M0 设备清单、解锁与前台组件读取，M1 本机 SQLite 五表/租约，以及 M2 合成 XML 四源解析/分页合并。
- 真实设备输出格式、机型差异与时序未验证；M1 的停止检查器只用注入值或 mock ctypes 验证；M2 合成 profile 不代表真实版本已标定。
