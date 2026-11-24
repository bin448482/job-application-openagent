# tests/scenarios 目录指南

## 一、目录用途

本目录存放 BOSS 直聘离线场景测试资产：按《BOSS直聘_求职自动化_场景测试方案_20261123》登记的 73 个场景及其全部变体、独立 expected、纯合成夹具、标准库执行辅助、使用说明与三轮作者自审记录。它服务于“测试资产/判据自检”和“对真实交付 app 的离线场景验收”两条不同链路，不实现未读解析、调度、动作执行或任何业务替代实现。

M0 `51333d8`、M1 `a600d0a`、M2 `cc103f8`、M3 `584b814`（普通 merge `92a0b68`）已接入并在隔离框架实际执行；M4+ 接口尚未交付。READY 表示入口、夹具和独立预期齐备，实际执行仍受里程碑门禁限制；ASSET_ONLY 接口未冻结；DEFERRED 与 MODULE_EVIDENCE 不计 PASS；T/O/W 实机门槛保持未验收。M0 29 PASS、M1 38 PASS、M2 54 PASS、M3 15 PASS，不等于产品整体通过。

## 二、文件与子目录索引

- `catalogue.json`：73 个场景、327 个变体的机器清单，含 milestone、源契约、前置、离线子场景、SUT 入口步骤、夹具引用、覆盖状态和 `expected_ref`。
- `expected/`：独立 expected 与目录指南。每个变体一条判据，只由比较器和审查者读取；夹具与 manifest 不引用该目录。
- `fixtures/`：纯合成输入与目录指南。包含 M0 doctor/config、M2 sensor、M3–M8 事件/动作/策略/清理/指标声明式输入；不含真实联系人、公司、简历、设备标识或本机路径。
- `framework/`：标准库辅助与目录指南。包含资产校验、严格比较器、虚拟时钟/事件重放、隔离验收入口。
- `tools/`：作者生成脚本与目录指南。`build_assets.py` 按人工判据机械展开清单、预期与夹具，可重复生成；不读取产品输出。
- `reviews/`：三轮作者自审记录与目录指南。明确是作者自审，不是独立第三方审查或实机验收。
- `README.md`：面向执行者的使用说明、命令、退出码、层间边界与已知限制。
- `AGENTS.md`：本文件，目录索引与维护要求。
- `CLAUDE.md`：只包含 `@AGENTS.md`。

## 三、当前规模与覆盖口径

最近一次生成与自检结果（以实际命令输出为准）：

- 场景 73 个：M0 3、M1 7、M2 15、M3 14、M4 11、M5 8、M6 8、M7 4、M8 3。
- 变体 327 个：F 305、T 9、O 12、W 1。
- 资产状态：READY_FOR_APP 136、ASSET_ONLY 155、DEFERRED 10、MODULE_EVIDENCE 4、DEVICE_GATE 22。
- M0 已执行：C01 9 PASS + 1 T 门槛、C02 12 PASS、C03 8 PASS；共 29 PASS、0 FAIL。C01 real_probe 仍是未验收 T 门槛。
- M1 已执行：38 PASS、0 FAIL；2 DEFERRED（M5/M6）、3 MODULE_EVIDENCE（纯约束，不重做模块单测）。M1 总体仍 BLOCKED，不冒充完整通过。
- M3 已执行：15 PASS、0 FAIL；8 DEFERRED（R01 三项、R02、R03/R04/R06/R07 原变体）、1 MODULE_EVIDENCE（R01 无确认的零子进程证据）、17 设备门槛。新增 10 个明确父场景的 M3 子变体（定向核对 API/边界、全局正计数 API、读侧不关闭发现、稳定身份新增键、页面/职位完整路径）；它们不计作原 R03/R04/R06/R07 完整通过。
- 其余 155 个 M4+ ASSET_ONLY 记 `BLOCKED_ENTRY_UNFROZEN`；DEFERRED/MODULE_EVIDENCE 均不得算 PASS。
- DEVICE_GATE 是 T/O/W 纯实机门槛，保留未验收；F 通过不能替代。

## 四、入口与验证命令

在项目根执行：

```powershell
python -m unittest discover -s tests -p test_offline_scenarios.py
python -m tests.scenarios.framework.runner --mode assets
python -m tests.scenarios.framework.runner --mode acceptance --target .
python -m tests.scenarios.framework.runner --mode acceptance --only SC-C01 --target .
```

- unittest：资产完整性、严格比较、时钟/重放、路径安全、隔离 tripwire、缺 app 拒绝、M3 探针/真实阶梯 mutant 等 75 项检查；只证明判据能抓错误。
- `--mode assets`：只读清单/预期/夹具并校验；退出 0 仅表示资产自洽，不表示产品通过。
- `--mode acceptance`：先校验资产，失败即拒绝执行；只为已实现里程碑的 READY F 变体创建隔离根，运行真实 `python -m app`、M0 协议边界替身、M1 探针/受控两进程、M2 受限探针或 M3 探针。全量当前 136 PASS、0 FAIL，但 M4+/ASSET_ONLY/DEFERRED/MODULE/设备门槛使总体 BLOCKED、退出 3；M0 子集退出 0，M1 子集退出 3，M2 子集退出 0，M3 子集退出 3，均不声明产品 PASS。
- 报告 JSON 写入被忽略的 `data/evals/scenario-runs/`；不提交生成报告，不修改 `.gitignore`。

## 五、维护要求

1. 修改场景、变体或预期时，先改 `tools/build_assets.py` 中的人工判据，再运行生成脚本；不要手工编辑生成 JSON 后与作者脚本脱节。
2. 新增 READY 变体必须有冻结入口步骤、夹具和可执行 acceptance；只写文字 assertions 的变体只能是 ASSET_ONLY，不得标 READY。
3. 每个变体必须有 `expected_ref` 且独立预期只存在于 `expected/oracles.json`；夹具与 manifest 不得引用 expected。
4. 不读取 `config.json`、环境凭据、真实 HR 内容、历史业务库或截图；夹具只用“合成联系人甲/合成机构甲/合成岗位甲”等合成值。
5. 不复制模块单元测试；这里只做跨模块场景与横向不变量。发现生产缺陷先向协调端报告，由业务方修复。
6. 新增、删除、改名或移动本目录文件/子目录时，同步更新本文件和根 `tests/AGENTS.md`（由业务方串行维护，本任务不直接修改）。
7. 三层验收分开报告：资产自检、已交付里程碑的真实 CLI 场景、未实现/纯实机门槛；缺 app 或未执行不得写成 PASS。
