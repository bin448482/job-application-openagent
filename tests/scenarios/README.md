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

- 场景 73、变体 343：F 321、T 9、O 12、W 1；M4 82（66 原 + 16 子证据）。
- READY_FOR_APP 194、ASSET_ONLY 89、DEFERRED 33、MODULE_EVIDENCE 5、DEVICE_GATE 22。
- M0 `51333d8`、M1 `a600d0a`、M2 `cc103f8`、M3 `584b814`（merge `92a0b68`）、M4a/b `140c182`（merge `ec66db4`）已普通 merge 并接入。
  - M0 实际执行：C01 9 PASS + 1 T 门槛、C02 12 PASS、C03 8 PASS，共 29 PASS、0 FAIL。
  - M1 实际执行：38 PASS、0 FAIL；DEFERRED 2（abandon→M5、leased_no_start→M6）、MODULE_EVIDENCE 3（纯约束用例，不重做模块单测）。M1 子集总体 BLOCKED、退出 3，不冒充完整 M1 通过。
  - M3 实际执行：15 PASS、0 FAIL；8 DEFERRED、1 MODULE_EVIDENCE、17 设备门槛，子集退出 3。新增 10 个 M3 子变体只证明定向核对 API、读侧副作用边界、稳定身份新增键与页面/职位路径，不冒充 R03/R04/R06/R07 完整流程。
  - M4a/b 实际执行：58 PASS、0 FAIL；23 DEFERRED、1 MODULE_EVIDENCE，子集退出 3。D01 真实映射、D02 12+子证据、D03 2+子证据、D05 14（16/32/64 KiB 与编码边界、真实 junction）、D06 1、D07 3（Local/External、hr/job/anomaly）、D08 5（独立手算摘要、2048/2049、65536/65537）、D10 4、D11 8（首次失败审计、I/O 失败、provider 固定诊断、完整合成 holdout 首次/重复/退役/真实 regression 内容核验）。
  - 全量验收：M0–M4 共 194 PASS、0 FAIL；89 个 M5+ ASSET_ONLY 入口未冻结、22 个设备门槛未验收、33 DEFERRED、5 MODULE_EVIDENCE；总体 BLOCKED、退出 3。
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
python -m tests.scenarios.framework.runner --mode acceptance --only SC-R --target .
python -m tests.scenarios.framework.runner --mode acceptance --only SC-D --target .
```

- unittest：89 项，验证资产完整性、严格比较反例、逐元素键/类型、时钟/重放、路径安全、junction/symlink、隔离 tripwire、缺 app 拒绝、M3 探针路径、真实阶梯 mutant、真实日志变异、非首项输出、M4 提交后重试/桥工具泄露/模拟验收/常量摘要/首次失败收尾/regression 篡改 mutant。
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
7. 日志断言按单次 CLI 调用快照新增文件，只读该调用的唯一 correlation，校验完整有序事件序列、`started` 成对次数、固定字段白名单/类型与敏感标记；日志目录自身/祖先链接在读取前拒绝。无像素分支出现 pixel 即失败。

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
- M3 页面/职位/未读/会话完整路径、字段阶梯/像素/上下文冲突、稳定消息身份/跨屏重叠/new_message_keys、定向核对 API 与边界、版本漂移、读侧不关闭发现；rows/cards/messages/field_evidence 的每个元素校验固定键、必要类型与非首项身份/摘要/证据；真实日志按单次调用与 correlation 校验有序事件、成对次数和敏感字段；真实进入/返回/到达时序仍依赖 M4–M6 或设备；
- M4a/b 旧 24 映射、Decision 校验、提交前重试/提交后超时/2048 限额、事实/注入/档位纯契约、策略包 45 键与真实链接拒绝、init/validate/preview、摘要/渲染/篡改、显式 restore activity 子证据、公开/eval 留出首次与重复访问/整批退役/真实 regression 准入；共享门禁与 M4c 活动完整流程仍 DEFERRED；
- M5 六个动作 × F-A 至 F-E、人工收敛、非法证据；
- M6 安全点、第 6 次抢占、预算边界、双钟/续租、三个观察钩子；
- M7 清理保护、身份核验、台账与删除后核验；
- M8 分桶/零分母、固定 C、降级回滚。

## 7. 未完成与下一步

- P2 已接入 M0/M1/M2/M3/M4a/M4b；M5+ 需等业务方稳定提交后，逐一绑定真实动作/调度/设备门禁接口；不得合并未验收业务分支。
- 89 个 M5+ ASSET_ONLY 变体仍缺接口；32 个 DEFERRED 与 5 个 MODULE_EVIDENCE 保持未计 PASS。原 R03/R04/R06/R07 完整变体仍分别依赖 M6 调度/M5–M6 进入与事件时序；M4 D09 共享门禁、D04 原事实确认/待办/授权、D10 额度/旧授权与 M4c 活动检查、D11 真实 provider/私有留出仍未验收。
- 纯实机门槛、真实模型/provider、真实写入与设备证据全部未执行。
- 根 `tests/AGENTS.md`、根 `AGENTS.md` 索引与 README 由业务方串行更新；本目录只提供待同步条目。
