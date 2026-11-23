# app/cli 目录指南

## 一、目录用途

提供 M0 的命令行组合根：严格解析全局 `--config` 与 `doctor` 参数，校验项目内相对路径，加载最小配置与合成夹具，注入只读探针，并只向 stdout 输出一个 UTF-8 JSON 对象。

## 二、当前文件

- `__init__.py`：声明命令行包。
- `main.py`：`main(argv)` 入口、严格 argparse、配置/夹具读取、fixture 与 ADB 组合、安全 JSON 输出与退出码。

## 三、行为契约

1. 全局 `--config` 必须放在子命令前；`doctor` 的 `--fixture` 与 `--device` 互斥且必须选其一。
2. `--serial` 只能与 `--device` 同用且此时必填；空白 serial 视为缺失。
3. 未知选项、选项缩写、重复选项、缺参数、额外位置参数统一输出 `invalid_input` 并退出 `2`。
4. 配置省略或内容为 `{}` 时使用 `{"schema_version":1,"mode":"read_only"}`；只接受这两个键与固定值。
5. 配置、夹具路径只接受项目内相对路径；拒绝绝对路径、盘符、`~` 和解析后越界。
6. 参数/配置/夹具错误只输出三键安全对象，不回显输入、异常、绝对路径或命令参数。
7. fixture 模式不得实例化或调用 ADB 适配器，不得启动子进程。
8. 成功或业务停止只向 stdout 写一个 JSON 对象；stderr 保持为空。帮助退出 `0`，其余失败退出 `2`，不使用退出 `1`。

## 四、测试

```powershell
python -m unittest tests.test_doctor
```

`tests/test_doctor.py` 覆盖配置/夹具负例、模式冲突、重复/未知选项、帮助、业务失败、窗口解析与 fixture 零子进程断言。

## 五、维护要求

1. 修改参数、配置、路径或错误出口时，同步更新本文件、根 `AGENTS.md` 和 `docs/operator.md`。
2. 新增子命令时先对照实施计划的里程碑契约；不得把 M1+ 子命令提前接入。
3. 新增文件/子目录时同步更新本目录索引；不另建重复全局规则的文档。
4. 保持组合根是唯一可导入适配器的 CLI 模块，不让 usecase 或 port 反向依赖具体通道。

## 六、已知限制

- 当前只有 `doctor`；`store init`、`sense` 等未实现。
- 真实设备模式只做离线 mock 验证，尚未在真机执行。
- 输出键顺序固定为 `schema_version/status/reason_code/device_count/selected_alias/unlocked/foreground_app`，但调用方应按键读取而非依赖顺序。
