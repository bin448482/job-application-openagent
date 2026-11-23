# app/cli 目录指南

## 一、目录用途

提供 M0/M1 的命令行组合根：严格解析全局 `--config`、`doctor` 与 `store init` 参数，校验项目内相对路径，加载最小配置与合成夹具，注入只读探针或 SQLite 初始化，并只向 stdout 输出一个 UTF-8 JSON 对象。

## 二、当前文件

- `__init__.py`：声明命令行包。
- `main.py`：`main(argv, process_log=...)` 入口、严格 argparse、配置校验/夹具读取、共享 JSON 重复键边界、fixture 与 ADB 组合、`store init --db` 接线、唯一安全 JSON 输出、退出码与过程日志阶段记录。

## 三、行为契约

1. 全局 `--config` 必须放在子命令前；`doctor` 的 `--fixture` 与 `--device` 互斥且必须选其一。
2. `--serial` 只能与 `--device` 同用且此时必填；空白 serial 视为缺失。
3. 未知选项、选项缩写、重复选项、缺参数、额外位置参数统一输出 `invalid_input` 并退出 `2`。
4. 配置省略或内容为 `{}` 时保持 `{"schema_version":1,"mode":"read_only"}` 契约；`_validate_config` 只做校验，不返回未使用的对象，只接受这两个键与固定值。
5. 配置、夹具与 `--db` 路径只接受项目内相对路径；拒绝绝对路径、盘符、`~` 和解析链接/`..` 后越界。`store init --db` 省略时为 `data/runtime/runtime.sqlite3`。
6. 参数/配置/夹具/仓储错误只输出三键安全对象，不回显输入、异常、绝对路径或命令参数。共享 JSON 边界拒绝重复对象键（含嵌套）；夹具还拒绝不可 UTF-8 编码的 alias/selected_alias。
7. fixture 模式不得实例化或调用 ADB 适配器，不得启动子进程；CLI 导入适配器模块本身必须零副作用。`store init` 只调用 SQLite 初始化/核验，不启动子进程、不调用网络。
8. JSON 输出统一经过 `_finalize_output`：安全 `emit`、记录 `output` 成功/失败、再记录 `exit`；输出失败时安全退出 `2`、不打印 traceback，日志明确为 `output=failed`，不能假称 JSON 已输出。成功或业务停止正常时 stderr 为空；日志写入失败时 stdout/退出码不变，stderr 仅写固定 `m0_process_log_degraded`。`--help` 保持 argparse 直接输出并退出 `0`，只记录其 output/exit 路径，不伪造业务 JSON。其余失败退出 `2`，不使用退出 `1`。
9. `store init` 成功只输出 `{"schema_version":1,"status":"ok","reason_code":null}`；失败原因取 `invalid_input/store_busy/store_io_error/db_version_unsupported/migration_failed` 等仓储词表，不新增业务成功字段。
10. 过程日志阶段覆盖 `start/arguments/config/mode/fixture/probe/adb_devices/adb_window/decision/store/output/exit`；store 调用先记 `started` 再记 `ok/failed`，error_category 固定为 `store`；只写固定原因码/耗时，不写原值或数据库路径。stdout 七键/三键契约不含 `correlation_id`。

## 四、测试

```powershell
python -m unittest tests.test_doctor tests.test_store tests.test_migrations
```

`tests/test_doctor.py` 覆盖 M0 配置/夹具负例、重复 JSON 键、孤立代理字符、模式冲突、重复/未知选项、帮助 output/exit、业务失败、设备失败七键映射、窗口字段边界/解析、stdout 写失败、`output=failed`、白名单拒绝降级提示、日志 started→结束顺序与 fixture 零子进程断言。`tests/test_migrations.py` 覆盖 `store init` 的固定 JSON、幂等、默认路径、路径越界、损坏/篡改/未知版本库与失败日志。

## 五、维护要求

1. 修改参数、配置、路径或错误出口时，同步更新本文件、根 `AGENTS.md` 和 `docs/operator.md`。
2. 新增子命令时先对照实施计划的里程碑契约；不得把 M2+ 子命令提前接入。
3. 新增文件/子目录时同步更新本目录索引；不另建重复全局规则的文档。
4. 保持组合根是唯一可导入适配器的 CLI 模块，不让 usecase 或 port 反向依赖具体通道。

## 六、已知限制

- 当前只有 `doctor` 与 `store init`；`sense`、读取阶梯、动作与调度未实现。
- 真实设备模式只做离线 mock 验证，尚未在真机执行；M1 只用合成库与注入检查器。
- 输出键顺序固定，但调用方应按键读取而非依赖顺序。
- 过程日志为 best-effort；调用端忽略单次 `record` 的 `False`，只通过降级标记暴露不完整，不能当作审计保证。
