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

- 场景 73、变体 364：F 342、T 9、O 12、W 1；M4 103（66 原 + 37 子证据）。
- READY_FOR_APP 256、ASSET_ONLY 46、DEFERRED 35、MODULE_EVIDENCE 5、DEVICE_GATE 22。
- M0 `51333d8`、M1 `a600d0a`、M2 `cc103f8`、M3 `584b814`（merge `92a0b68`）、M4a/b `140c182`（merge `ec66db4`）、M4c `5cb6817`（普通 merge `248483e`）已接入。
  - M0 实际执行：C01 9 PASS + 1 T 门槛、C02 12 PASS、C03 8 PASS，共 29 PASS、0 FAIL。
  - M1 实际执行：38 PASS、0 FAIL；DEFERRED 2（abandon→M5、leased_no_start→M6）、MODULE_EVIDENCE 3（纯约束用例，不重做模块单测）。M1 子集总体 BLOCKED、退出 3，不冒充完整 M1 通过。
  - M3 实际执行：15 PASS、0 FAIL；8 DEFERRED、1 MODULE_EVIDENCE、17 设备门槛，子集退出 3。新增 10 个 M3 子变体只证明定向核对 API、读侧副作用边界、稳定身份新增键与页面/职位路径，不冒充 R03/R04/R06/R07 完整流程。
  - M4a/b/c 实际执行：77 PASS、0 FAIL；25 DEFERRED、1 MODULE_EVIDENCE，子集退出 3。D01 真实映射、D02 12+真实租约适配子证据、D03 2+真实租约桥前后子证据、D05 15（16/32/64 KiB 与编码边界、真实 junction）、D06 2（事实入口与空事实待办）、D07 3 子证据（Local/External、hr/job/anomaly）、D08 5（独立手算摘要、2048/2049、65536/65537）、D09 7（两进程恰一 primary、真实失败隔离/崩溃/TTL/PID/换 profile/字段篡改、预检先占后开）、D10 7（真实活动 stopped/active/unknown、真实 restore 三值/目标不半写、改 base/包/model 旧凭证失效）、D11 11；M4c 事实过期/冲突/白名单外/跨 profile/HR 键与真实活动/租约子证据均不冒充完整 M5 流程。
  - 全量验收：M0–M5 共 256 PASS、0 FAIL；46 个 M5+ ASSET_ONLY 入口未冻结（含 A06 六个依赖 M6）、22 个设备门槛未验收、35 DEFERRED、5 MODULE_EVIDENCE；总体 BLOCKED、退出 3。
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

- unittest：117 项（111 资产判据 + 6 个真实 facts CLI 持久案例），验证资产完整性、严格比较反例、逐元素键/类型、时钟/重放、路径安全、junction/symlink、隔离 tripwire、缺 app 拒绝、M3 探针路径、真实阶梯 mutant、真实日志变异、非首项输出、M4 提交后重试/桥工具泄露/模拟验收/常量摘要/首次失败收尾/regression 篡改 mutant，以及 M4c 探针正常执行/越界拒绝/junction 拒绝/生产字段变异被 oracle 拒绝、D06 forget 残留、并发 winner 关联、隔离 LF 规范化边界（自动与显式 LF 均保持 BOM/NUL）、单文件夹具根内绝对/上跳/Windows 语义/答案名拒绝、资产校验复用同一路径规则、合成无上传合同正例与缺合同零桥调用反例和受控异常安全 JSON。
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
3. 把交付 app 与命令/manifest 选定的合成输入复制到 `data/evals/scenario-runs/<run>/iso-.../`；`expected.json`、独立 oracle 与未引用案例不进入 SUT 输入树。D11 的公开合成无上传合同位于 `fixtures/m4/eval/attestation/holdout-attestation.synthetic.json`，经 `fixture_files` 以固定消费名映射到隔离根，不依赖 `*.local.json` 等本机忽略文件；映射原始源/目标在 resolve 前统一拒绝绝对/盘符/根/UNC/上跳与答案名，资产校验复用同一规则。自动复制与显式 `lf_inputs` 均只经唯一字节转换函数，BOM/NUL 原样、普通 CRLF 转 LF、坏 UTF-8 自动复制保留而显式 LF 拒绝。
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
- M4a/b/c 旧 24 映射、Decision 校验、提交前重试/提交后超时/2048 限额、事实/注入/档位纯契约、策略包 45 键与真实链接拒绝、init/validate/preview、摘要/渲染/篡改、显式 restore activity 子证据、公开/eval 留出首次与重复访问/整批退役/真实 regression 准入；M4c 真实事实过期/冲突/白名单/跨 profile、共享门禁两进程与失败隔离、真实租约与桥前后拒绝、活动 stopped/active/unknown、真实 restore 与旧凭证失效已接稳定接口；完整 M5 待办/授权与整轮 run 仍 DEFERRED；
- M5 已接真实 `perform_action`/`AdbActionExecutor`/`reconcile_action`/`complete_action`/领域证据校验：A01 文本 6 PASS + 2 BLOCKED、A02 8 PASS、A03 7 PASS + 1 BLOCKED、A04 7 PASS + 1 BLOCKED、A05 6 PASS、A07 3 PASS + 1 BLOCKED、A08 6 PASS；A06 六个保留 ASSET_ONLY/BLOCKED_ENTRY_UNFROZEN（依赖 M6 apply_job）。A01 输入法/发送控件候选缺 M5 控件发现；A03/A04 direct_send 被生产执行器前置拒绝（非文本需先有可逆槽位点击）；A07 reconnect 缺 M5 键屏/会话身份入口，均保持 BLOCKED，未伪造 PASS。
- M6 安全点、第 6 次抢占、预算边界、双钟/续租、三个观察钩子；
- M7 清理保护、身份核验、台账与删除后核验；
- M8 分桶/零分母、固定 C、降级回滚。

## 7. 未完成与下一步

- P2 已接入 M0/M1/M2/M3/M4a/M4b/M4c/M5 动作与事实入口；M6+ 需等业务方稳定提交后，逐一绑定调度/清理/灰度接口；不得合并未验收业务分支。
- 46 个 M5+ ASSET_ONLY 变体仍缺接口（含 A06 六个与 M6–M8）；35 个 DEFERRED 与 5 个 MODULE_EVIDENCE 保持未计 PASS。原 R03/R04/R06/R07 完整变体仍分别依赖 M6 调度/M5–M6 进入与事件时序；M4 D02/D03 完整调度重跑与超限待办、D04 ask_user/blocked 授权、D06 完整十步、D09 read_only 整轮持有、D10 额度/旧授权/在途、D11 真实 provider/私有留出仍未验收。
- 纯实机门槛、真实模型/provider、真实写入与设备证据全部未执行。
- 根 `tests/AGENTS.md`、根 `AGENTS.md` 索引与 README 由业务方串行更新；本目录只提供待同步条目。
