# app 目录指南

## 一、目录用途

承载 M0 起的分层业务程序。当前只实现 `python -m app` 入口与只读 `doctor` 探针，按“CLI 组合根 → usecase → port ← adapter”的依赖方向组织；M1–M8 的存储、传感器、决策、动作与调度尚未实现。

## 二、当前文件与子目录

- `__init__.py`：声明 `app` 包。
- `__main__.py`：`python -m app` 的进程入口，调用 `app.cli.main.main`。
- `cli/`：参数解析、配置加载、路径边界、JSON 输出与组合根；见 `cli/AGENTS.md`。
- `ports/`：能力端口与领域快照类型；见 `ports/AGENTS.md`。
- `usecases/`：单次用例的校验与业务判定；见 `usecases/AGENTS.md`。
- `adapters/`：具体平台通道；见 `adapters/AGENTS.md`。

## 三、依赖与边界

1. `ports/` 只依赖标准库与自身类型，不导入 `adapters/`。
2. `usecases/` 依赖 `ports/` 与领域对象，不导入具体适配器。
3. `adapters/` 实现端口，不写业务库、不改调度、不决定策略档位。
4. `cli/main.py` 是当前唯一组合根；只有它可以把 fixture 输入或 ADB 适配器注入 `doctor` 用例。
5. M0 的 fixture 模式必须零设备、零网络、零子进程；真实 ADB 适配器只能在显式 `--device --serial` 下调用，且只能在测试中 mock，不在离线验收中执行。
6. 不读取根 `config.json`、环境文件、凭据或个人事实；不引入 Python 第三方依赖。

## 四、测试与验证

在项目根执行：

```powershell
python -m unittest tests.test_doctor
python -m app doctor --fixture tests/fixtures/doctor/ready.json
```

测试覆盖 CLI 正反例、配置与夹具校验、设备状态映射、窗口解析与“fixture 零子进程”断言。真实 ADB 模式只通过 mock 的 subprocess 输出验证，不代表已在真机运行。

## 五、维护要求

1. 新增、删除、改名或移动本目录文件/子目录时，同步更新本文件和根 `AGENTS.md`。
2. 新增承载真实代码的子目录时，同目录创建 `AGENTS.md` 与只含 `@AGENTS.md` 的 `CLAUDE.md`；不创建空目录。
3. 每个源文件顶部说明用途，每个函数/方法写 docstring；修改模块时同步对应子目录指南。
4. 新增里程碑能力时先确认计划路径、`app/` 分层和测试入口，再按对应 M 的验收契约实现，不提前创建空模块。
5. 涉及真实设备、外部模型或网络时先取得本轮确认；M0 不提供这些调用的默认入口。

## 六、已知限制

- 当前只有 M0；`domain/models.py`、`ports/store.py`、`usecases/sense.py` 等 M1–M2 文件尚未创建。
- 真实 `adb devices` / `dumpsys window` 未在本机执行，真实机型、系统版本、序列号格式与窗口输出差异未验证。
- `doctor` 只做一次独立探针，不提供业务循环、导航、截图或动作。
