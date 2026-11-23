# tests 目录指南

## 一、目录用途

存放 Python 标准库 `unittest` 测试与纯合成夹具。当前覆盖 M0 `doctor`，不连接真实设备、不调用模型或网络、不读取根 `config.json`、不写业务库。

## 二、当前文件与子目录

- `__init__.py`：声明测试包，支持 `python -m unittest tests.test_doctor`。
- `test_doctor.py`：M0 CLI 正反例、配置/夹具校验、重复 JSON 键与孤立代理字符、业务状态映射、keyguard token、`mCurrentFocus` 字段边界/整条 focus 完整 Window/单 Window 多组件/前后附加内容解析、设备失败七键映射、stdout 写失败与 `output=failed`、正常帮助 output/exit、过程日志白名单/降级故障注入、ADB `started→ok/failed` 与中断仅留 started、调用协议与 fixture 零子进程断言。
- `fixtures/`：纯合成输入夹具；见 `fixtures/AGENTS.md`。

## 三、范围边界

- 独立场景协调端维护 `tests/scenarios/` 与 `tests/test_offline_scenarios.py`；本目录指南不为这两处虚构内容，也不得由 M0 任务创建或修改。
- 负例由测试进程捕获预期退出码；验收命令本身必须退出 `0`。
- 真实适配器只在测试中 mock `subprocess.run`；不得实际运行 ADB。
- 测试不把被测输出反推成预期，也不使用真实姓名、序列号、聊天或绝对路径。
- 日志测试必须同时覆盖正常追加、白名单拒绝、IO 故障注入与 CLI 固定 `m0_process_log_degraded` 标记；fixture 模式只允许写被忽略日志，不得写业务库或启动子进程。

## 四、运行方式

在项目根执行：

```powershell
python -m unittest tests.test_doctor
npm test
```

M0 测试不依赖 Node；`npm test` 验证既有评测判据未被破坏。

## 五、维护要求

1. 新增/改名/移动测试或夹具目录时同步更新本文件和根 `AGENTS.md`。
2. 新增测试模块时保持标准库优先，失败信息不回显敏感原文。
3. 每个测试方法写 docstring，说明覆盖的契约与预期。
4. 新增真实设备测试必须另行取得当轮确认，不能混入离线测试。

## 六、已知限制

- 当前只有 M0 测试；M1–M2 的 store、migrations、sensor 测试尚未实现。
- 真实 ADB、真实机型与窗口输出未验证。
- 独立离线场景测试不在本 Task 范围。
- 日志测试验证的是本机 best-effort 行为，不代表磁盘故障时仍能保留完整日志。
