# app/ports 目录指南

## 一、目录用途

声明业务用例依赖的能力端口与跨层领域快照，不包含具体平台 IO。当前只有 M0 设备探针相关类型。

## 二、当前文件

- `__init__.py`：声明端口包。
- `device.py`：`DeviceEntry`（逻辑别名与状态）、`ProbeSnapshot`（清单、选择、解锁、前台）以及 `DeviceProbe` 协议。

## 三、边界

- 端口只暴露能力与领域字段，不暴露真实序列号、窗口原文、控件坐标或 XML 节点。
- 不导入 `adapters/`；实现由具体适配器提供。
- M0 探针字段与原因码沿用实施计划；fixture 通道不启动子进程。
- 不预建 `store.py`、`decision.py`、`policy.py` 等未实现里程碑的空端口。

## 四、维护要求

1. 修改端口字段或协议时，同步更新根 `AGENTS.md`、对应 usecase/printer 指南和测试。
2. 新增端口必须有真实用例消费；不为未来可能性添加空接口。
3. 新增文件时同步本索引；每个文件顶部写用途，公开方法写 docstring。

## 五、已知限制

- 当前未定义 `ReadResult`、`UnreadSnapshot`、`Lease` 等 M1–M2 类型。
- 真实适配器只实现 `probe`，不包含 M3+ 的读屏、点击、输入、返回或截图能力。
