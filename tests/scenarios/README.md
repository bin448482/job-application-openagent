# BOSS 离线场景资产使用说明

## 1. 范围

本目录把《BOSS直聘_求职自动化_场景测试方案_20261123》登记的 73 个场景落成可复核资产：

- `catalogue.json`：场景、变体、前置、SUT 入口、夹具与覆盖状态。
- `expected/oracles.json`：人工依据 P/A/K 契约先写的独立预期。
- `fixtures/`：纯合成 profile/XML/JSON/SQLite 前置与事件脚本。
- `framework/`：资产校验、严格比较、虚拟时钟/事件重放与隔离验收。
- `tools/build_assets.py`：作者数据与机械生成脚本。
- `reviews/`：三轮作者自审记录。

本目录不实现业务逻辑，也不把资产自检、参考回放或模块单测当作产品验收。

## 2. 当前事实

- 场景 73、变体 317：F 295、T 9、O 12、W 1。
- READY_FOR_APP 121、ASSET_ONLY 169、DEFERRED 2、MODULE_EVIDENCE 3、DEVICE_GATE 22。
- M0 `51333d8`、M1 `a600d0a`、M2 `cc103f8` 已普通 merge 并接入。
  - M0 实际执行：C01 9 PASS + 1 T 门槛、C02 12 PASS、C03 8 PASS，共 29 PASS、0 FAIL。
  - M1 实际执行：38 PASS、0 FAIL；DEFERRED 2（abandon→M5、leased_no_start→M6）、MODULE_EVIDENCE 3（纯约束用例，不重做模块单测）。M1 子集总体 BLOCKED、退出 3，不冒充完整 M1 通过。
  - 全量验收：M0–M2 共 121 PASS、0 FAIL；169 个 M3+ ASSET_ONLY 入口未冻结、22 个设备门槛未验收、2 DEFERRED、3 MODULE_EVIDENCE；总体 BLOCKED、退出 3。
  - `--only SC-C` 的 M0 子集退出 0；`--only SC-L` 的 M1 子集因 2 DEFERRED/3 MODULE_EVIDENCE 退出 3；两者 `product_acceptance` 都不是 PASS。
  - 未运行真实 ADB、设备、模型或网络。

## 3. 命令与退出码

在项目根执行：

```powershell
python -m unittest discover -s tests -p test_offline_scenarios.py
python -m tests.scenarios.framework.runner --mode assets
python -m tests.scenarios.framework.runner --mode acceptance --target .
python -m tests.scenarios.framework.runner --mode acceptance --only SC-C --target .
python -m tests.scenarios.framework.runner --mode acceptance --only SC-L --target .
```

- unittest：64 项，验证资产完整性、严格比较反例、时钟/重放、路径安全、junction/symlink、隔离 tripwire、缺 app 拒绝与 CLI 安全失败。
- assets：0 = 资产自洽；1 = 资产错误。**不执行产品，不等于业务通过。**
- acceptance：
  - 0 = 全部 F 变体被执行且一致；报告 `product_acceptance` 仍为 `NOT_ACCEPTED_DEVICE_GATES_UNVERIFIED`；
  - 1 = 至少一个真实执行差异；
  - 3 = 资产非法、缺 app、入口未冻结、里程碑未实现、DEFERRED、MODULE_EVIDENCE 或零执行；不会以零项/全 skip 报 PASS。
- 报告只写 `data/evals/scenario-runs/<run>/report.json`，不提交生成产物。

## 4. 验收隔离

acceptance 对每个可执行变体：

1. 先校验资产；失败拒绝运行任何 SUT。
2. 校验 `app/` 与整套 `fixtures/` 源树不含 symlink/junction、解析后不越界。
3. 把交付 app 与命令/manifest 选定的合成输入复制到 `data/evals/scenario-runs/<run>/iso-.../`；`expected.json`、独立 oracle 与未引用案例不进入 SUT 输入树。
4. 用最小环境白名单启动 `python -m app`，本地应用数据、TEMP、PATH 与数据库全部落在隔离根内。
5. `sitecustomize` tripwire 记录并拒绝网络、外部进程与 ADB 尝试；出现记录即 FAIL。
6. 数据库断言使用 resolve 后 `relative_to` 边界与 `mode=ro`，不读取或创建真实运行库。

这不构成完整 OS 沙箱；它只覆盖测试所需的设备/网络/配置边界，限制已在 `reviews/` 记录。

## 5. 层间口径

- **F 离线**：可由合成输入、假设备/时钟/provider 与临时库执行；不代表真实手机。
- **T 受控设备 / O 机会观察 / W 真实写入**：保留 `DEVICE_GATE`，当前全部未验收；F 通过不能替代。
- **ASSET_ONLY**：接口未冻结或依赖未交付 StorePort/共享门禁/动作执行/策略工具；只登记输入、前置和契约判据，报告记 `BLOCKED_ENTRY_UNFROZEN`。
- **E0**：现有 npm 自检；不属于本目录业务覆盖，也不混入本目录计数。

## 6. 已覆盖的离线判据类别

- M0 doctor 合成设备/配置/参数负例与真实探针协议；
- M1 初始化、租约竞争、续租/旧 nonce、恢复隔离、finish 三态与跨表回滚、仓储输入；
- M2 四源冲突、区域/bounds、9+/0+、新招呼语义、全覆盖否定、预览/时间、多页、时间窗、profile/路径、身份歧义、终态与时间倒退；
- M3 进入消耗角标、授权矩阵、版本漂移、跨级冲突、读取中断；
- M4 Decision 校验、超时、事实/注入、策略包 45 键、摘要、共享门禁、快照恢复、留出分桶；
- M5 六个动作 × F-A 至 F-E、人工收敛、非法证据；
- M6 安全点、第 6 次抢占、预算边界、双钟/续租、三个观察钩子；
- M7 清理保护、身份核验、台账与删除后核验；
- M8 分桶/零分母、固定 C、降级回滚。

## 7. 未完成与下一步

- P2 已接入 M0/M1/M2；M3+ 需等业务方稳定提交后，逐一绑定真实 CLI/StorePort/设备门禁/动作执行接口；不得合并未验收业务分支。
- 169 个 ASSET_ONLY 变体仍缺接口；2 个 DEFERRED 与 3 个 MODULE_EVIDENCE 保持未计 PASS。
- 纯实机门槛、真实模型/provider、真实写入与设备证据全部未执行。
- 根 `tests/AGENTS.md`、根 `AGENTS.md` 索引与 README 由业务方串行更新；本目录只提供待同步条目。
