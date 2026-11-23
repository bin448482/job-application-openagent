# hr-act HR 动作原始操作

> 依据只读参考仓库 `D:\0-development\works\android-job-application` 的静态阅读整理。行号以本次阅读时的文件为准；未运行任何 Python 代码、未连接设备、未读取参考仓库的 `config.json`、`data/`、`screenshots/`、数据库、日志与凭据。

## 0 概览

### 0.1 入口命令

| 命令 | 入口 | 说明 |
| --- | --- | --- |
| `hr apply` | `scripts/main.py:926-983`（`run_hr`）、`scripts/hr/apply.py:907-943`（`run_apply`） | 执行单条 Phase 2 决策，按意图调用动作执行器 |
| `hr review list` | `scripts/main.py:1209-1211`、`scripts/hr/review.py:56-71` | 只读列出待核对发现、动作与未关闭消息 |
| `hr review close` / `resolve-message` / `reopen` | `scripts/main.py:1235-1243` | 人工关闭、解除发现阻塞、重开发现 |
| `hr review resolve-not-sent` / `resolve-sent` | `scripts/main.py:1244-1249`、`scripts/hr/review.py:611-632` | 解除动作待核对状态 |
| `hr review record-resume` | `scripts/main.py:1177-1183`、`scripts/hr/review.py:635-640` | 仅写本地简历证据，不操作手机 |
| `hr review dismiss-non-text` | `scripts/main.py:1214-1217`、`scripts/hr/review.py:643-653` | 排除非文本入站 |
| `hr review update-draft` | `scripts/main.py:1229-1231`、`scripts/hr/review.py:318-337` | 替换待发草稿正文 |
| `hr review send-draft` | `scripts/main.py:1218-1220`、`scripts/hr/review.py:286-315`、`435-538` | 人工确认后发送待发草稿 |
| `hr review send-text` | `scripts/main.py:1221-1223`、`scripts/hr/review.py:148-283` | 人工确认后主动追加一条文本 |
| `hr review continue` | `scripts/main.py:1224-1228`、`scripts/hr/review.py:340-402` | 人工确认后恢复 blocked 消息 |
| `hr cleanup list` / `ledger` / `run` | `scripts/main.py:1279-1310`、`scripts/hr/cleanup.py:219-316`、`319-331`、`486-801` | 清理候选扫描、台账、删除 |
| `heartbeat start` / `status` / `round-done` / `stop` / `wait` | `scripts/main.py:1120-1172`、`scripts/heartbeat/heartbeat.py:79-172`、`180-203`、`206-294`、`398-411`、`414-477` | 心跳调度 |

`hr apply`、`review send-text`/`send-draft`、`cleanup run`、`heartbeat start` 分属不同设备占用与授权门禁，见 `scripts/main.py:1609-1633`（`_dispatch`）与 `scripts/main.py:828-834`（`_inline_device`）。

### 0.2 涉及文件

- `scripts/hr/apply.py`：决策执行主流程、动作编排、发送后尾部核验、拒绝清理。
- `scripts/hr/actions.py`：设备侧动作原语（联系方式、简历、文本输入发送、等待模板）。
- `scripts/hr/review.py`：待核对清单、人工处置、草稿发送、主动追加文本、人工恢复。
- `scripts/hr/cleanup.py`：清理候选判定与手机端删除。
- `scripts/hr/store.py`：动作事件、草稿、消息/会话状态、清理台账、待核对门禁（只取与状态流转相关部分）。
- `scripts/hr/contract.py`：Phase 1 契约组装，提供 `message_key`、`contact`、`latest_inbound_text` 等输入。
- `scripts/heartbeat/heartbeat.py`：心跳起停、等待、轮次结束、审计门控。
- `scripts/heartbeat/store.py`：心跳调度表状态迁移。
- `scripts/main.py`：`hr apply/review/cleanup` 与 `heartbeat` 命令组、参数校验、设备围栏。
- 回看确认的 UI 原语：`scripts/core/adb.py`、`scripts/core/uitree.py`、`scripts/core/message_list.py`、`scripts/core/workspace.py`、`scripts/hr/chat.py`。

### 0.3 涉及页面

页面分类常量见 `scripts/core/workspace.py:15-21`。

- 消息列表 `chat_list`：清理扫描、草稿发送前定位会话行。
- 聊天详情 `chat_detail`：文本输入、发送、联系方式交换、简历发送、草稿发送。
- 弹层 `popup`：交换确认框、附件简历确认框、长按菜单、删除确认框。
- 登录页 `login` / 首页 `main_page` / 职位详情 `job_detail`：仅作为“非目标页面”门禁，不在本文件展开。

### 0.4 前置状态

- 设备已由 `adb.select_device()` 唯一选中（`scripts/core/adb.py:110-148`），且未锁屏（`scripts/core/adb.py:180-185`、`scripts/heartbeat/heartbeat.py:41-43`）。
- `hr apply` 需要 Phase 1 契约 JSONL 与 decision JSON，且 `message_key` 在契约中唯一（`scripts/main.py:940-955`、`scripts/hr/apply.py:49-59`）。
- 执行动作前全局不得存在未核实动作（`scripts/hr/apply.py:477-478`、`scripts/hr/store.py:2028-2033`）。
- 清理候选要求列表年龄满 7 天或已是拒绝终态，并受草稿/人工待办保护（`scripts/hr/cleanup.py:150-216`）。
- 心跳要求无进行中的对账问题（`scripts/heartbeat/heartbeat.py:87-89`、`230-232`）。

### 0.5 授权前提

- `hr apply` 带 Dispatch 时按 `authorization.send_reply / request_resume / request_wechat / request_phone / delete_rejected` 裁剪动作（`scripts/hr/apply.py:500-516`、`624-641`）。
- 出现拒绝且不保留联系方式时必须有 `--serial`，否则在写意图前暂停（`scripts/hr/apply.py:518-523`）。
- `send-text`、`send-draft`、`continue`、`cleanup run`、`heartbeat stop` 均要求显式 `--confirmed`（`scripts/main.py:1184-1206`、`1281-1285`、`scripts/hr/review.py:350-355`、`scripts/heartbeat/heartbeat.py:401-402`）。
- 固定等待模板还要求 Dispatch 显式授权且模板已确认（`scripts/hr/apply.py:288-302`、`scripts/hr/scan.py:364-370`）。

## 1 状态判定原语

- **J01 发送成功（我方回显新增）**
  - 代码位置：`scripts/hr/actions.py:723-726`、`818-825`（`send_resource_message`）
  - 规则：点击发送前统计 `resource-id` 以 `tv_content_text` 结尾且 `text` 等于正文的节点数 `existing_count`；点击后同一条件下节点数 `> existing_count` 才算 `sent`。发送前后都重新核验聊天标题与联系人（`scripts/hr/actions.py:722`、`817`）。

- **J02 发送结果未知**
  - 代码位置：`scripts/hr/actions.py:826-836`、`132-134`、`843-845`
  - 规则：已注入点击（`tapped=True`）后，若输入框已清空/变为占位符但未出现回显新增，或 3 秒内既无回显又未清空，返回 `unknown`；读屏失败补读一次仍失败也返回 `unknown`（`post_click_read_failed`）。点击之后任何异常都不得降级为“未发送”。

- **J03 发送前确定未发送**
  - 代码位置：`scripts/hr/actions.py:80-89`（`_pre_click_failure`）、`714-720`、`731-753`、`776-780`、`837-850`
  - 规则：尚未点击发送控件前，身份不符、输入框不唯一、正文两次未落入输入框、发送控件定位失败、输入法恢复前异常等，统一返回 `not_sent` 并带细分码（`read_failed`/`wrong_screen`/`target_missing`/`needs_draft_clear` 等）。

- **J04 输入框唯一且为空**
  - 代码位置：`scripts/hr/actions.py:727-730`、`741-753`（`send_resource_message`）
  - 规则：`resource-id` 以 `editText_with_scrollbar` 结尾的节点必须恰好 1 个；清空后其 `text` 必须属于 `{"", "回复消息", "介绍自己回复率更高哦~", "和Ta聊一聊~"}`。不满足时最多再清空一次，仍不满足返回 `needs_draft_clear`。

- **J05 输入正文一致**
  - 代码位置：`scripts/hr/actions.py:755-775`
  - 规则：`ADB_INPUT_B64` 广播后重新读屏，输入框 `text` 必须与正文完全相等；最多两轮，两轮都不等返回 `not_sent`。

- **J06 待核对门禁（全局未核实动作）**
  - 代码位置：`scripts/hr/store.py:2028-2033`（`has_unverified_actions`）、`2021-2025`（`pending_actions`）
  - 规则：按“每会话每动作最新一条事件”判断，状态属于 `pending/unknown/failed/needs_probe` 即视为未核实，阻塞新的 `apply`、`send-text`、固定等待与清理义务判定（`scripts/hr/apply.py:477-478`、`scripts/hr/review.py:179-180`、`203-204`、`scripts/hr/cleanup.py:622-626`）。

- **J07 待核对状态产生**
  - 代码位置：`scripts/hr/apply.py:718-736`、`739-756`、`762`
  - 规则：动作执行后按证据收敛为 `not_sent`/`unknown`/`needs_probe`/`needs_attachment`/`failed` 等；`unknown`、`needs_probe`、`needs_attachment`、`failed` 留待人工核对。点击前可证明未发送时写 `not_sent`，不进入待核对。

- **J08 待核对解除**
  - 代码位置：`scripts/hr/store.py:1855-1860`（`resolve_action_event`）、`1863-1885`（`reconcile_action_event`）、`scripts/hr/review.py:611-632`
  - 规则：仅当最新事件状态属于 `ACTION_MANUAL_REVIEW_STATUSES = pending/unknown/failed/needs_probe/needs_attachment` 时可改写（`scripts/hr/store.py:23-24`、`1838-1852`）；人工确认未发送写 `not_sent`，确认已发送写目标终态；`requested → sent/delivered` 的纠正仅允许 `resume`（`scripts/hr/store.py:1874-1884`）。

- **J09 聊天尾部入站证据**
  - 代码位置：`scripts/hr/apply.py:74-93`（`_verified_latest_inbound`）、`96-122`（`_confirmed_outgoing_message`）、`scripts/hr/review.py:410-432`（`_latest_inbound_is_tail`）
  - 规则：聊天必须可解析、联系人一致、`parse_complete` 非 False；最新入站状态为 `known` 才算确认。草稿发送额外要求最新真实行就是草稿对应入站文本，且其后的行只能是 `system_tip/job_card/system_card/time` 这类透明行。

- **J10 简历交付与仅请求**
  - 代码位置：`scripts/hr/actions.py:463-470`、`541-560`、`608-637`
  - 规则：出现 `RESUME_SENT_TEXTS`（如“附件简历已发送”）或确认框消失后出现预期附件名节点/本次请求卡双按钮置灰，记 `sent`；只出现“附件简历请求已发送”、按钮文案“请求中”或“您好，可以看看我的简历”，记 `request_sent`。确认框仍打开时框内附件名与送达文案一律不算本次交付（`scripts/hr/actions.py:611-620`）。

- **J11 联系方式动作已处理/可点击**
  - 代码位置：`scripts/hr/actions.py:270-293`、`295-311`、`333-347`
  - 规则：页面出现“企业认证已失效”“暂时无法与您沟通”直接 `failed`；槽位文字已是“请求中”或“查看微信/电话”返回 `already_processed`；槽位按钮必须唯一、启用且位于对应横向区间（电话 `0-270`，微信 `270-540`），否则 `unknown`。

- **J12 附件确认框可确认**
  - 代码位置：`scripts/hr/actions.py:473-498`（`_resume_confirmation`）
  - 规则：必须出现标题“确定发送附件简历吗？”；页面中以 `.pdf/.doc/.docx` 结尾的附件名列表必须恰好等于 `[expected_attachment]`；确认/确定按钮必须唯一且启用。附件不符返回 `attachment_mismatch`，按钮不满足返回 `confirm_missing`，只有 `ready` 才允许点击。

- **J13 清理候选判定**
  - 代码位置：`scripts/hr/cleanup.py:25-26`、`150-216`（`_candidate_for_row`）、`609-633`（`cleanup_has_pending_obligation`）
  - 规则：普通 HR 会话按列表时间满 `CLEANUP_AGE_DAYS = 7` 天才候选；数据库已是 `rejected/declined` 的会话有即时清理例外；未满 7 天时未读角标、待发草稿、人工待办仍保护，满 7 天后不再保护；`force_rejected` 只放行当前拒绝目标。时间无法解析记 `time_unparsed`，身份不足不参与判定。

- **J14 删除成功**
  - 代码位置：`scripts/hr/cleanup.py:661-700`
  - 规则：删除后列表必须可唯一解析；目标会话姓名（及职位）不得再出现；若删除前记录了唯一相邻锚点行，删除后该锚点仍唯一可见才算成功；没有锚点时只接受“删除成功”提示。`target_still_present`、锚点缺失/重复、列表不可解析都记 `delete_unverified`。

- **J15 会话行唯一匹配**
  - 代码位置：`scripts/hr/cleanup.py:334-347`（`_row_nodes`）、`scripts/hr/chat.py:893-916`（`_row_container`）、`928-942`（`open_message`）
  - 规则：列表中 `recyclerView` 必须唯一；按 `tv_name` 与 `tv_position`（含公司拆分）完全匹配；同名同职位时再用 `tv_msg` 与 `tv_time_v2` 收窄。仍不唯一时拒绝点击。

- **J16 草稿陈旧判定**
  - 代码位置：`scripts/hr/review.py:483-491`、`94-101`、`438-442`
  - 规则：无待发草稿或消息已关闭直接拒绝；聊天 `parse_complete` 为 False、联系人不等、最新入站状态非 `known`、最新入站文本不等于草稿对应入站、或尾部不是该入站文本，均记 `stale_draft`/`contact_unverified`。

- **J17 心跳到期与轮次占用**
  - 代码位置：`scripts/heartbeat/heartbeat.py:445-474`、`scripts/heartbeat/store.py:117-125`
  - 规则：`next_due_at <= now` 才到期；占用使用单条 `UPDATE ... WHERE state='active' AND round_in_progress=0 AND next_due_at<=?`，`rowcount==1` 才算抢到；抢不到时若已在轮次中返回 `already_running`，否则返回停止结果。

- **J18 授权与确认门禁**
  - 代码位置：`scripts/hr/apply.py:500-516`、`624-645`、`scripts/main.py:1184-1206`、`1281-1285`
  - 规则：`send_reply/request_resume/request_wechat/request_phone/delete_rejected` 任一未授权时对应动作被跳过或整条暂停（`policy_violation`）；`send-text/send-draft/update-draft/continue/cleanup run/heartbeat stop` 无 `--confirmed` 直接拒绝。`conversation_state.reconciliation_failed` 存在时联系方式/简历动作暂停（`needs_action_review`）。

## 2 原子操作

### A01 读取当前 UI 层级
- 目的：获得当前屏幕的 Android UI XML，作为所有定位与核验的唯一事实来源。
- 代码位置：`scripts/core/adb.py:205-246`（`read_hierarchy`）
- 前置条件：设备已选中且未断连；当前前台为 BOSS 直聘。
- 原子动作：调用 core 设备原语：`adb shell rm -f /sdcard/job_tracker_ui.xml` → `uiautomator dump`（退出码 137 最多重试 1 次）→ `cat` 取回 → `ET.fromstring` 解析；XML 内必须含 `package=BOSS` 的节点。
- 输入：`serial`。
- 输出：`ET.Element` 根节点；同时记录读屏耗时与成功/失败计数。
- 成功判据：XML 可解析且存在 `package` 等于目标包的节点。
- 失败处理：解析失败转 `RuntimeError("Android UI 层级不是有效 XML")`；包不符转 `RuntimeError("当前界面不是 BOSS 直聘")`；设备断连由 `raise_if_disconnected` 识别并向上抛出。
- 副作用与幂等性：只读；会在设备 `/sdcard` 覆盖临时 XML，不产生对外消息。

### A02 点击节点中心（普通点击）
- 目的：对已唯一确定的 UI 节点执行一次 `input tap`。
- 代码位置：`scripts/core/adb.py:249-257`（`tap_bounds`，`channel="input_tap"`）
- 前置条件：调用方已通过 J04/J11/J12/J15 等判定拿到唯一节点，并用 `scripts/core/uitree.py:9-15`（`get_bounds`）取得 bounds。
- 原子动作：调用 core 设备原语：`adb shell input tap <centerX> <centerY>`；坐标为 bounds 四边取中点。
- 输入：`serial`、`bounds`。
- 输出：无返回值（命令成功即返回）。
- 成功判据：ADB 命令退出码为 0；是否生效必须由后续读屏证据判定，命令成功不等于动作生效。
- 失败处理：`CalledProcessError`/`OSError` 向上抛出；调用方按“点击前/点击后”决定 `not_sent` 或 `unknown`。
- 副作用与幂等性：非幂等；重复点击可能重复触发业务，调用方必须先落动作意图再点击。

### A03 同点按压/长按手势
- 目的：通过同坐标滑动手势实现长按（清理会话）或输入通道降级点击。
- 代码位置：`scripts/core/adb.py:249-262`（`tap_bounds` 非 tap 通道）、`scripts/hr/cleanup.py:593-597`、`604-615`
- 前置条件：目标节点 bounds 已确定；清理场景还要求按压点同时落在行与列表可视区内（`scripts/hr/cleanup.py:401-413`）。
- 原子动作：调用 core 设备原语：`adb shell input swipe x y x y <duration>`；清理长按为 1200ms，输入通道降级为 60ms/800ms。
- 输入：`serial`、同点坐标、时长。
- 输出：无返回值。
- 成功判据：后续读屏出现长按菜单（“删除消息”）或目标按钮状态变化；手势本身不构成成功证据。
- 失败处理：ADB 异常向上抛出；清理在长按后 3 次尝试仍无菜单则 `menu_unverified` 并停止（`scripts/hr/cleanup.py:604-633`）。
- 副作用与幂等性：长按本身不删除；但重复长按可能打开/关闭菜单，属于可观察副作用。

### A04 发送返回键
- 目的：从聊天页/弹层返回消息列表或关闭筛选面板。
- 代码位置：`scripts/core/adb.py:271-273`（`keyevent`）、`scripts/core/message_list.py:365-388`（`ensure_list`）、`scripts/hr/review.py:446-458`、`518-530`
- 前置条件：当前页面可能不是消息列表；调用方已判断需要返回。
- 原子动作：调用 core 设备原语：`adb shell input keyevent 4`，随后等待并重新 A01 读屏，确认页面可解析为消息列表。
- 输入：`serial`；最多 3 次。
- 输出：`True/False`（是否回到可解析列表）或调用方自定义结果。
- 成功判据：读屏后 `parse_message_list` 成功且页面为 `PAGE_CHAT_LIST`。
- 失败处理：每次失败继续下一次；3 次仍失败返回 `needs_list`/`False`。发送已确认时断连不得再发返回键（`scripts/hr/review.py:523-527`）。
- 副作用与幂等性：可能退出当前页面；重复按返回可能退到主屏，调用方有次数上限。

### A05 列表滑动一屏
- 目的：在消息列表内滚动一屏，用于归位、扫描与定位会话行。
- 代码位置：`scripts/core/message_list.py:309-325`、`328-362`（`scroll_messages`）
- 前置条件：`recyclerView` 唯一且 `scrollable=true`；bounds 宽高为正。
- 原子动作：调用 core 设备原语：`adb shell input swipe x <lower/upper> x <upper/lower> 450`；`hierarchy_only=True` 时只比较滑动前后层级签名，否则先截图门控再比较。
- 输入：`serial`、`root`、`direction`（`up`/`down`）、`hierarchy_only`。
- 输出：`(changed, after_root)`。
- 成功判据：滑动后层级签名与滑动前不同（或默认通道的截图门控显示变化）。
- 失败处理：`recyclerView` 不唯一直接 `RuntimeError`；两次滑动仍无变化返回 `changed=False`。
- 副作用与幂等性：改变列表视口；不产生对外消息。

### A06 等待画面稳定
- 目的：在点击/长按/删除后等待界面停止变化，避免截图或读屏撞上动画。
- 代码位置：`scripts/core/adb.py:297-331`（`screencap_hash`、`wait_for_screen`）、`355-382`（`capture_screenshot`）、`scripts/hr/cleanup.py:473-484`（`_wait_after_delete`）
- 前置条件：设备可截图。
- 原子动作：连续 `adb exec-out screencap -p` 计算 SHA-256，直到两次相同或超时；删除后固定 3000ms 上限、200ms 间隔。
- 输入：`serial`、超时与间隔。
- 输出：`{"hash","changed","polls","elapsed_ms","stable"}`；删除路径无返回值。
- 成功判据：`stable=True` 或达到超时。
- 失败处理：截图不可用回退 0.5 秒短等待（`scripts/hr/cleanup.py:479-483`）；设备断连向上抛出并停止。
- 副作用与幂等性：只读；不写入文件。

### A07 保存停止现场截图
- 目的：清理流程停止时保留现场，便于人工核对。
- 代码位置：`scripts/hr/cleanup.py:441-449`（`_capture_stop_evidence`）
- 前置条件：停止原因不是 `device_disconnected`（`scripts/hr/cleanup.py:547-548`）。
- 原子动作：`adb exec-out screencap` 写入 `screenshots/hr-cleanup-<time_ns>.png`（`scripts/core/adb.py:355-382`）。
- 输入：`serial`、结果字典。
- 输出：结果中写入 `screenshot` 或 `screenshot_error`。
- 成功判据：文件写入成功；失败只记录异常类型，不改变停止原因。
- 失败处理：捕获所有异常并记录类型，不重试。
- 副作用与幂等性：在本机磁盘产生截图文件；不产生对外消息；含聊天画面，属于敏感本机产物。

### B01 定位唯一输入框
- 目的：确认聊天页只有唯一可编辑输入框，作为后续点击与写入目标。
- 代码位置：`scripts/hr/actions.py:727-730`、`741-753`
- 前置条件：当前页为聊天详情且已通过 A01 读取。
- 原子动作：无设备动作；按 `resource-id` 后缀 `editText_with_scrollbar` 过滤节点。
- 输入：`root`。
- 输出：唯一输入框节点；或返回 `needs_send_control`/`needs_draft_clear`。
- 成功判据：节点数恰好 1，且 `text` 为空或占位符（J04）。
- 失败处理：数量不为 1 时按调用阶段返回错误码，不继续输入。
- 副作用与幂等性：只读判定。

### B02 点击输入框
- 目的：让输入框获得焦点，确保文本广播落在正确控件。
- 代码位置：`scripts/hr/actions.py:738`、`766`
- 前置条件：B01 已得到唯一输入框；切换输入法后必须重新点击（`scripts/hr/actions.py:736-738`）。
- 原子动作：调用 A02，点击输入框 bounds 中心。
- 输入：`serial`、输入框节点。
- 输出：无。
- 成功判据：后续 B03/B05 读屏显示输入框状态变化；点击本身不构成成功证据。
- 失败处理：ADB 异常向上抛出；此时尚未发送，可归 `not_sent`。
- 副作用与幂等性：非幂等但可重复点击；不产生对外消息。

### B03 清空输入框
- 目的：清除残留草稿，保证只发送本次正文。
- 代码位置：`scripts/hr/actions.py:739-740`、`746-747`、`759-760`
- 前置条件：B02 已完成或输入框已有焦点。
- 原子动作：调用 core 设备原语：`adb shell am broadcast -a ADB_CLEAR_TEXT`，等待 0.2 秒。
- 输入：`serial`。
- 输出：无。
- 成功判据：B01 复读后输入框 `text` 属于空值/占位符（J04）。
- 失败处理：最多清空两次，仍不满足返回 `needs_draft_clear`，不点击发送。
- 副作用与幂等性：丢失输入框内未发送文本；可重复执行。

### B04 输入文本正文
- 目的：把完整回复正文写入输入框。
- 代码位置：`scripts/hr/actions.py:755-769`
- 前置条件：B03 已证明输入框为空；ADBKeyboard 已启用并设为当前输入法（B06）。
- 原子动作：调用 core 设备原语：`adb shell am broadcast -a ADB_INPUT_B64 --es msg <base64>`，等待 0.3 秒；失败重试时先 B03、B02 再重发。
- 输入：`serial`、正文 `content`（UTF-8 后 base64）。
- 输出：无。
- 成功判据：B05 读屏确认输入框 `text` 与正文完全相等（J05）。
- 失败处理：两轮仍不相等返回 `not_sent`，不点击发送。
- 副作用与幂等性：可能覆盖输入框内文本；重复输入前会清空，故最终只保留一份正文。

### B05 校验输入框正文
- 目的：在点击发送前证明输入框内容就是预期正文。
- 代码位置：`scripts/hr/actions.py:770-775`、`826-829`
- 前置条件：B04 已执行。
- 原子动作：A01 读屏，按 `editText_with_scrollbar` 取唯一输入框并比较 `text`。
- 输入：`root`、期望正文。
- 输出：布尔值/节点列表。
- 成功判据：唯一输入框 `text == content`。
- 失败处理：不相等则进入下一轮输入或返回 `not_sent`。
- 副作用与幂等性：只读。

### B06 切换 ADBKeyboard 输入法
- 目的：让文本广播能被输入框接收。
- 代码位置：`scripts/hr/actions.py:705`、`716-735`
- 前置条件：原输入法可读取；设备已连接。
- 原子动作：`adb shell settings get secure default_input_method` 读取原输入法 → `adb shell ime list -s` 读取已启用列表 → 必要时 `adb shell ime enable com.android.adbkeyboard/.AdbIME` → `adb shell ime set com.android.adbkeyboard/.AdbIME`。
- 输入：`serial`。
- 输出：原输入法 ID 与启用列表（供 B07 恢复）。
- 成功判据：后续 B04/B05 成功；切换命令本身不构成发送证据。
- 失败处理：读不到原输入法直接 `RuntimeError`，在点击前中止并归 `not_sent`。
- 副作用与幂等性：改变设备全局输入法；进程异常退出可能遗留 ADBKeyboard，需 B07 恢复。

### B07 恢复原输入法
- 目的：发送结束后把设备输入法还原，避免影响用户后续使用。
- 代码位置：`scripts/hr/actions.py:851-876`（`finally`）
- 前置条件：B06 已记录原输入法与是否原本启用 ADBKeyboard。
- 原子动作：`adb shell ime set <original_ime>`；若 ADBKeyboard 原本未启用，再 `adb shell ime disable com.android.adbkeyboard/.AdbIME`。
- 输入：`serial`、原输入法、启用列表。
- 输出：无。
- 成功判据：命令返回成功。
- 失败处理：设备断连时跳过恢复；点击后恢复失败向上抛出（发送事实仍保留），点击前失败只记 `input_method_restore_failed` 日志，不改变 `not_sent` 证据（`scripts/hr/actions.py:872-876`）。
- 副作用与幂等性：改变设备全局输入法；可重复执行。

### B08 定位发送控件
- 目的：在输入框同一行找到唯一发送按钮，避免点到其他控件。
- 代码位置：`scripts/hr/actions.py:417-459`（`resource_send_control`）、`778-780`
- 前置条件：B01 已得到输入框节点。
- 原子动作：无设备动作；从输入框父节点逐层向上，在“位于输入框右侧且纵向重叠”的可点击节点中优先取 `Button` 类，其次取文本/描述含“发送”的节点，最后取 `ImageView`；同一层取最深且唯一者。
- 输入：`root`、输入框节点。
- 输出：发送控件节点或 `None`。
- 成功判据：返回唯一节点。
- 失败处理：`None` 时返回 `not_sent`，不点击。
- 副作用与幂等性：只读判定。

### B09 点击发送控件（不可逆）
- 目的：把输入框正文作为一条聊天消息发出。
- 代码位置：`scripts/hr/actions.py:784-787`
- 前置条件：J05 已证明正文一致；B08 已定位唯一发送控件；E01 已登记 `pending` 意图（在 `apply`/`send-text`/`send-draft` 路径中）。
- 原子动作：调用 A02，点击发送控件 bounds 中心；`tapped=True` 后进入证据轮询。
- 输入：`serial`、发送控件节点。
- 输出：无。
- 成功判据：只能由 B10/J01 判定；点击命令成功不等于发送成功。
- 失败处理：点击后任何异常都保留 `unknown`（`scripts/hr/actions.py:132-134`、`843-845`）；绝不自动重点击。
- 副作用与幂等性：**不可逆对外发送**。消息一旦发出无法撤回；重复点击会产生重复消息，因此必须先落 `pending` 并在 `unknown` 后停止等人工核对。

### B10 核验发送结果
- 目的：用页面回显判断本次发送是成功、未知还是失败。
- 代码位置：`scripts/hr/actions.py:788-836`、`851-858`
- 前置条件：B09 已执行。
- 原子动作：A01 读屏循环，最多 3 秒；每轮重新核验联系人、统计回显节点、检查输入框是否清空。
- 输入：`serial`、`content`、`existing_count`、`send_before/send_target`。
- 输出：`ResumeActionResult("sent"/"unknown"/"not_sent", detail_code, after_root)`。
- 成功判据：J01 回显新增 → `sent`；J02 输入清空或超时无回显 → `unknown`。
- 失败处理：读屏失败补读一次，仍失败 `unknown("post_click_read_failed")`；不重试发送。
- 副作用与幂等性：只读；会把发送前后层级写入观测归档（`scripts/hr/actions.py:851-858`）。

### C01 请求交换联系方式（点击“换微信/换电话”）
- 目的：向 HR 发起微信或电话交换请求。
- 代码位置：`scripts/hr/actions.py:252-330`（`request_contact_exchange`）、`313-316`
- 前置条件：J11 判定槽位可点击；联系人核验通过；无限制提示；已通过 `claim_action` 认领意图（`scripts/hr/actions.py:292-293`）。
- 原子动作：先 A01 读屏并 `verify_contact`，等待 0.8 秒；随后调用 `_observed_click` → A02 点击槽位按钮；若需降级，走 A03 的 60ms 同点滑动。
- 输入：`serial`、`expected_contact`、`kind`（微信/电话）。
- 输出：`requested`/`already_processed`/`not_sent`/`unknown`/`failed`。
- 成功判据：出现“请求交换微信/电话已发送”系统提示，或槽位变为“请求中”，或出现交换确认弹层（`scripts/hr/actions.py:297-311`）。
- 失败处理：效果未知且无确认框返回 `unknown`；确认框缺失返回 `not_sent`（仅当证据证明无变化）或 `unknown`。
- 副作用与幂等性：**对外请求不可撤回**；已处理状态可防重，但仍可能向 HR 发出交换请求。

### C02 确认交换（点击“确认”）
- 目的：在“确定与对方交换微信/电话吗？”弹层中确认交换。
- 代码位置：`scripts/hr/actions.py:302-307`、`321-330`
- 前置条件：J11 已识别确认框，且“确认”按钮唯一、启用；“确定与对方交换{kind}吗？”文本存在。
- 原子动作：调用 `_observed_click` → A02 点击“确认”按钮。
- 输入：`serial`、`kind`、弹层层级。
- 输出：`requested`/`unknown`。
- 成功判据：弹层消失且出现“请求交换…已发送”或槽位“请求中”（`scripts/hr/actions.py:297-300`、`328-330`）。
- 失败处理：确认后仍无送达证据返回 `unknown`，不重试。
- 副作用与幂等性：**不可逆对外交换确认**；重复点击可能重复提交。

### D01 点击“发简历”打开确认框
- 目的：主动向 HR 发起附件简历发送请求。
- 代码位置：`scripts/hr/actions.py:501-536`（`request_resume`）、`526-527`
- 前置条件：页面无已送达/已请求证据（`scripts/hr/actions.py:511-517`）；存在唯一“发简历”可点击节点（`scripts/hr/actions.py:518-520`）；附件名非空（`scripts/hr/actions.py:504-505`）。
- 原子动作：`_observed_click` → A02 点击“发简历”；必要时 A03 降级。
- 输入：`serial`、`expected_contact`、`expected_attachment`。
- 输出：打开确认框或 `needs_resume_review`/`already_delivered`/`already_requested`。
- 成功判据：J12 判定确认框 `ready`（标题、附件名、唯一确认按钮均满足）。
- 失败处理：附件不符 `needs_resume_review("attachment_mismatch")`；无确认按钮 `confirm_missing`；点击无效果时区分 `not_sent`/`unknown`。
- 副作用与幂等性：打开确认框本身不发送；但点击“发简历”会向 HR 侧暴露动作意图，重复点击可能重复弹框。

### D02 确认发送附件简历（点击“确认/确定”）
- 目的：确认把指定附件简历发送给 HR。
- 代码位置：`scripts/hr/actions.py:539-560`
- 前置条件：J12 为 `ready`；`claim_action` 成功（`scripts/hr/actions.py:537-538`）。
- 原子动作：`_observed_click` → A02 点击唯一确认按钮。
- 输入：`serial`、确认框层级、期望附件名。
- 输出：`sent`/`request_sent`/`unknown`。
- 成功判据：J10 交付证据（送达文案/附件名节点/请求卡双按钮置灰）；仅有请求文案时 `request_sent`。
- 失败处理：无任何证据返回 `unknown`，不重试；点击后异常保留 `unknown`。
- 副作用与幂等性：**不可逆对外发送附件简历**；重复确认可能重复发送。

### D03 点击“同意”接受简历索取
- 目的：HR 已请求简历时，点击请求卡上的“同意”完成交付。
- 代码位置：`scripts/hr/actions.py:568-666`（`accept_resume_request`）、`648-666`
- 前置条件：页面存在 `我想要一份您的附件简历，您是否同意` 请求卡（`scripts/hr/actions.py:563`、`586-589`）；“同意”按钮唯一、可点击、启用（`scripts/hr/actions.py:590-597`）；`claim_action` 成功（`scripts/hr/actions.py:605-606`）。
- 原子动作：`_observed_click` → A02 点击“同意”；若随后出现附件确认框，再走 D02。
- 输入：`serial`、`expected_contact`、`expected_attachment`（可为空）。
- 输出：`request_sent`/`already_delivered`/`needs_resume_review`/`not_sent`/`unknown`。
- 成功判据：J10 的交付证据，且确认框已消失（`scripts/hr/actions.py:608-637`）。
- 失败处理：确认框仍在时交付信号无效；无交付证据且完整窗口无变化才 `not_sent`，否则 `unknown`。
- 副作用与幂等性：**不可逆对外交付简历**；同一请求卡重复点击可能重复交付。

### E01 登记动作意图 pending
- 目的：在任何设备动作前把“即将执行某动作”原子落库，防止进程中断后重放。
- 代码位置：`scripts/hr/store.py:1660-1690`（`record_action_event`）、`scripts/hr/apply.py:684`、`scripts/hr/review.py:213-214`、`500-501`
- 前置条件：`conversation_key`/`message_key` 合法；动作状态词表合法。
- 原子动作：`BEGIN IMMEDIATE`；按动作作用域（`reply` 按 message_key，其余按 conversation_key）检查是否已有 `pending/requested/request_sent/exchanged/sent/delivered/unknown` 最新事件；有则回滚返回 `None`，无则插入 `pending`。
- 输入：`conversation_key`、`message_key`、`action`、`status`、`detail_code`。
- 输出：新事件 ID 或 `None`。
- 成功判据：返回正整数事件 ID。
- 失败处理：`None` 表示已有未决/已发送动作，调用方跳过或暂停（`already_processed`/`needs_action_review`）。
- 副作用与幂等性：本地数据库写入；幂等地阻止重复动作，但不产生对外消息。

### E02 收敛动作结果
- 目的：把预留的 `pending` 事件改写为页面证据支持的终态。
- 代码位置：`scripts/hr/store.py:1821-1835`（`finish_action_event`）、`scripts/hr/apply.py:762`、`718-736`
- 前置条件：E01 已返回事件 ID。
- 原子动作：`UPDATE hr_action_events SET status=?,detail_code=?,updated_at=? WHERE id=?` 并提交。
- 输入：`event_id`、`status`、`detail_code`。
- 输出：布尔值（是否更新 1 行）。
- 成功判据：`rowcount == 1`。
- 失败处理：状态/详情码非法抛 `ValueError`；更新 0 行说明事件已被改写，调用方按未知处理。
- 副作用与幂等性：本地状态写入；不产生对外消息；重复收敛会覆盖同一条事件结果。

### E03 人工确认未发送
- 目的：人工核对应用内确实没有发出后，解除动作阻塞。
- 代码位置：`scripts/hr/store.py:1888-1893`（`resolve_action_outcome`）→ `1855-1860`；`scripts/hr/review.py:611-620`
- 前置条件：调用方带 `--confirmed`（`scripts/main.py:1233-1234`）。
- 原子动作：仅对最新一条事件、且状态属于 `pending/unknown/failed/needs_probe/needs_attachment` 时写 `not_sent`。
- 输入：`connection`、`message_key`、`action`。
- 输出：`changed` 布尔值。
- 成功判据：最新事件状态变为 `not_sent`。
- 失败处理：状态不匹配时返回 `False`，不改写历史。
- 副作用与幂等性：解除 J06 门禁；不会操作手机，也不会撤回已发出的消息。

### E04 人工确认已发送/纠正
- 目的：人工核对实际发送状态后写回终态，允许把 `resume.requested` 纠正为已交付。
- 代码位置：`scripts/hr/store.py:1896-1902`（`reconcile_action_outcome`）→ `1863-1885`；`scripts/hr/review.py:623-632`
- 前置条件：`result ∈ {requested, sent, delivered}` 且 `confirmed=True`。
- 原子动作：若最新事件是 `requested` 且动作为 `resume`，仅允许改为 `sent/delivered`；其他情况在可核对状态集合内改写。
- 输入：`message_key`、`action`、`result`。
- 输出：`changed` 布尔值。
- 成功判据：`rowcount == 1`。
- 失败处理：非 `resume` 的 `requested` 纠正抛 `ValueError`；状态不匹配返回 `False`。
- 副作用与幂等性：解除 J06 门禁；不重发消息，只修正本地事实。

### E05 保存待发草稿
- 目的：动作结果未确认而停止前，把已校验的回复正文保存为草稿，避免回复丢失。
- 代码位置：`scripts/hr/store.py:1644-1658`（`save_reply_draft`）、`scripts/hr/apply.py:263-275`、`617-618`
- 前置条件：决策 `reply.action == "send"` 且正文非空；或 `reply.action == "draft"`。
- 原子动作：写入/更新 `hr_reply_drafts` 的 `pending` 草稿。
- 输入：`conversation_key`、`message_key`、`text`。
- 输出：无（由 store 返回布尔）。
- 成功判据：草稿表存在该 `message_key` 的 `pending` 行。
- 失败处理：数据库异常向上抛出；不产生对外消息。
- 副作用与幂等性：本地写入；重复保存会覆盖同一待发草稿。

### E06 更新待发草稿
- 目的：人工替换仍开放且未发送的草稿正文。
- 代码位置：`scripts/hr/review.py:318-337`、`scripts/hr/store.py:2142-2152`
- 前置条件：`message_key` 为 64 字符；草稿仍 `pending` 且所属入站未关闭。
- 原子动作：`UPDATE hr_reply_drafts SET text=? WHERE message_key=? AND status='pending' AND EXISTS(未关闭入站)`。
- 输入：`message_key`、`text`。
- 输出：`{"updated": bool, "result": "updated"/"no_pending_open_draft"}`。
- 成功判据：`rowcount == 1`。
- 失败处理：0 行返回 `no_pending_open_draft`，不新建草稿。
- 副作用与幂等性：本地写入；不发送。

### E07 人工关闭消息
- 目的：人工确认已处理后关闭入站消息并解除同消息发现。
- 代码位置：`scripts/hr/review.py:586-608`、`scripts/hr/store.py:2107-2112`
- 前置条件：`--confirmed`；不存在未决发送动作（`scripts/hr/review.py:595-599`）。
- 原子动作：`close_message(..., "manual_close")` + `resolve_discovery` + `close_blocked_discovery`。
- 输入：`message_key`。
- 输出：`changed` 布尔值。
- 成功判据：任一关闭/解除操作返回 True。
- 失败处理：存在未决动作时抛 `RuntimeError("存在待核对发送动作，先核实动作结果")`。
- 副作用与幂等性：本地状态写入；不操作手机。

### E08 重开未产出契约的发现
- 目的：人工确认后把未生成契约的已结束发现重新纳入未读扫描。
- 代码位置：`scripts/hr/review.py:580-583`、`scripts/hr/store.py:1968-1982`
- 前置条件：`--confirmed`（`scripts/main.py:1233-1234`）。
- 原子动作：调用 `reopen_uncontracted_discovery` 改写发现状态。
- 输入：`message_key`。
- 输出：`changed` 布尔值。
- 成功判据：发现状态变为可再扫描。
- 失败处理：不满足重开条件时返回 `False`。
- 副作用与幂等性：本地状态写入；下次 scan 可能重新读取该会话。

### E09 排除非文本入站
- 目的：人工确认非文本入站无需回复，推进列表基线并清除待核对。
- 代码位置：`scripts/hr/review.py:643-653`、`scripts/hr/store.py:2278-2306`
- 前置条件：`--confirmed`；`hr_non_text_pending` 存在且有预览/时间。
- 原子动作：事务内更新会话基线、删除 `hr_non_text_pending`、追加 `hr_non_text_review_events`。
- 输入：`conversation_key`。
- 输出：`dismissed`/`no_pending_non_text`/`conversation_not_found` 等。
- 成功判据：返回 `dismissed`。
- 失败处理：事务回滚并返回原因码。
- 副作用与幂等性：本地写入；不发送回复。

### E10 人工记录简历状态
- 目的：仅记录人工目视核对的简历状态，不操作手机。
- 代码位置：`scripts/hr/review.py:635-640`、`scripts/hr/store.py:1724-1767`
- 前置条件：`--confirmed`；`status ∈ {requested, sent}`；会话存在。
- 原子动作：`BEGIN IMMEDIATE` 下读取最新 resume 事件，按“只升不降、同状态不重复”插入新事件（`detail_code='manual_evidence'`）。
- 输入：`database_path`、`conversation_key`、`status`。
- 输出：`recorded`/`unchanged`/`status_downgrade`/`needs_action_review` 等。
- 成功判据：返回 `recorded`。
- 失败处理：任一校验失败返回原因码，不写库。
- 副作用与幂等性：只写本地；可重复调用但同状态不重复。

### F01 返回消息列表
- 目的：把手机从聊天页带回消息列表，供草稿定位或清理扫描。
- 代码位置：`scripts/hr/review.py:446-458`、`scripts/core/message_list.py:365-388`
- 前置条件：当前可能仍在聊天页；设备连接。
- 原子动作：A04 最多 3 次返回键，每次等待 0.6 秒并 A01 读屏，直到可解析消息列表。
- 输入：`serial`。
- 输出：`True/False`。
- 成功判据：`parse_message_list` 成功。
- 失败处理：3 次仍失败返回 `needs_list`；断连立即停止。
- 副作用与幂等性：改变页面位置；不产生对外消息。

### F02 取消未读筛选
- 目的：清除“取消筛选”状态，避免草稿定位只在筛选后的列表里找。
- 代码位置：`scripts/hr/review.py:460-465`
- 前置条件：列表页存在“取消筛选”节点。
- 原子动作：A02 点击“取消筛选”，A01 复读确认文本消失。
- 输入：`serial`、`root`。
- 输出：更新后的层级或 `filter_clear_unverified`。
- 成功判据：复读后不再出现“取消筛选”。
- 失败处理：点击后仍存在返回 `filter_clear_unverified`，不继续。
- 副作用与幂等性：改变列表筛选状态；不产生对外消息。

### F03 点击会话行
- 目的：打开与草稿身份匹配的唯一会话。
- 代码位置：`scripts/hr/chat.py:919-961`（`open_message`）、`scripts/hr/review.py:481-482`
- 前置条件：J15 已唯一匹配会话行；目标文本节点或行容器可点击。
- 原子动作：取行内 `tv_msg`（或行容器）bounds，`dev.screen_action` 包裹 A02 点击；点击后 A01 读屏并 `parse_chat`。
- 输入：`serial`、`item`、`match_fields`。
- 输出：聊天页根节点。
- 成功判据：`parse_chat(opened)["contact"] == item["name"]`。
- 失败处理：行不唯一抛 `chat_open_unverified`；标题不符抛 `contact_mismatch`；截图判定为 False 视为未打开。
- 副作用与幂等性：改变页面位置；不产生对外消息。

### F04 核验当前聊天与草稿尾部
- 目的：发送草稿前证明当前聊天仍是草稿对应的入站，且尾部未被新消息覆盖。
- 代码位置：`scripts/hr/review.py:483-491`、`410-432`
- 前置条件：F03 已打开聊天。
- 原子动作：无设备动作；解析 `chat`，比较联系人、`parse_complete`、`latest_inbound_status`、`latest_inbound`，并检查尾部透明行。
- 输入：`chat`、`draft`。
- 输出：通过或 `stale_draft`/`contact_unverified`。
- 成功判据：J16 全部满足。
- 失败处理：不满足直接返回，不发送。
- 副作用与幂等性：只读判定。

### F05 草稿完成落库
- 目的：草稿发送成功后同步回复事件、草稿状态、会话状态并关闭消息。
- 代码位置：`scripts/hr/store.py:2183-2198`（`complete_manual_draft`）、`scripts/hr/review.py:510-514`
- 前置条件：B10 返回 `sent`/`delivered`。
- 原子动作：把最新 `pending` 回复事件改为结果；草稿改 `sent`；会话 `hr_status='replied'`；关闭消息（`replied`）。
- 输入：`message_key`、`result`。
- 输出：布尔值。
- 成功判据：事件或草稿至少 1 行被更新。
- 失败处理：数据库异常向上抛出；已发送事实不因落库失败而回滚。
- 副作用与幂等性：本地写入；重复调用不会重复发送。

### F06 发送后返回列表
- 目的：草稿发送后回到列表，恢复下一次操作起点。
- 代码位置：`scripts/hr/review.py:518-530`
- 前置条件：B10 已返回成功；设备仍连接。
- 原子动作：A04 返回键 + A01 读屏，最多 3 次。
- 输入：`serial`。
- 输出：`navigation: "ok"/"needs_list"/"device_disconnected"`。
- 成功判据：列表可解析。
- 失败处理：断连时不再执行任何 ADB，直接返回已发送结果；3 次失败标 `needs_list`。
- 副作用与幂等性：只改变页面；不改变已发送事实。

### G01 归位到最新端
- 目的：清理扫描前把列表滚到最新端，保证从顶部开始而不是从上次停留处开始。
- 代码位置：`scripts/hr/cleanup.py:256-266`、`727-736`、`scripts/core/message_list.py:328-362`
- 前置条件：当前为消息列表且 `recyclerView` 唯一可滚动。
- 原子动作：A05 向 `down` 方向滑动，`hierarchy_only=True`，最多 `SEEK_NEWEST_LIMIT = 30` 次（`scripts/hr/cleanup.py:29`）。
- 输入：`serial`、`root`。
- 输出：最新端层级或 `list_read_failed`。
- 成功判据：某次滑动 `changed=False`。
- 失败处理：达到 30 次仍在变化返回 `list_read_failed` 并停止。
- 副作用与幂等性：改变列表视口；不产生对外消息。

### G02 逐屏扫描与候选判定
- 目的：在有限屏数内枚举列表行并产出清理候选。
- 代码位置：`scripts/hr/cleanup.py:268-301`、`740-791`
- 前置条件：G01 完成；`max_screens >= 1`。
- 原子动作：A01 读屏 → `parse_message_list` → 对每行调用 J13 → A05 向上滑动；每屏去重。
- 输入：`serial`、`connection`、`min_age_days`、`max_screens`、`limit`。
- 输出：`candidates`、`skipped`、`counts`、`scanned_rows`。
- 成功判据：扫描完成或`max_screens`/`limit` 用尽。
- 失败处理：页面不可识别 `needs_list`；时间无法解析延后到结束记 `time_unparsed`；断连停止。
- 副作用与幂等性：只读（`list`）；`run` 会在 G12 写台账。

### G03 复核候选行
- 目的：删除前在最新层级中重新唯一定位候选行，避免用旧屏坐标误删。
- 代码位置：`scripts/hr/cleanup.py:574-585`、`334-347`
- 前置条件：G02 已产出候选；`recyclerView` 唯一。
- 原子动作：无设备动作；按姓名/职位/公司精确匹配行，并记录可见身份与相邻锚点。
- 输入：`root`、`candidate`。
- 输出：唯一行节点与锚点身份。
- 成功判据：匹配行恰好 1 个。
- 失败处理：0 个或多个返回 `row_not_found` 并跳过。
- 副作用与幂等性：只读判定。

### G04 计算按压点
- 目的：得到同时位于行与列表可视区内的长按坐标。
- 代码位置：`scripts/hr/cleanup.py:401-413`（`_press_point`）
- 前置条件：G03 已得到行节点，`recyclerView` 唯一。
- 原子动作：无设备动作；取列表与行 bounds 的交集中心。
- 输入：`root`、行节点。
- 输出：`(x, y)` 或 `None`。
- 成功判据：交集宽高均不小于 40 像素。
- 失败处理：交集过小返回 `None`，候选记 `row_partially_visible`。
- 副作用与幂等性：只读。

### G05 长按会话行
- 目的：打开该会话行的操作菜单。
- 代码位置：`scripts/hr/cleanup.py:593-597`、`604-615`
- 前置条件：G04 已得到按压点；已等待 0.4 秒让滚动惯性停止。
- 原子动作：A03 同点滑动 1200ms；第 2/3 次尝试前再等待 0.4 秒并重复。
- 输入：`serial`、`x`、`y`。
- 输出：无。
- 成功判据：G06 在读屏中找到“删除消息”。
- 失败处理：3 次仍无菜单返回 `menu_unverified` 并停止。
- 副作用与幂等性：打开/关闭上下文菜单；不删除。

### G06 定位“删除消息”菜单
- 目的：确认长按菜单已出现且“删除消息”唯一可点击。
- 代码位置：`scripts/hr/cleanup.py:604-633`、`389-394`（`_unique_action`）
- 前置条件：G05 已长按。
- 原子动作：A01 读屏轮询，首次窗口 1.5 秒、后续 3.0 秒；用 `nodes_with_text` + `clickable_ancestor` 求唯一节点。
- 输入：`serial`。
- 输出：菜单按钮节点或 `menu_unverified`。
- 成功判据：唯一可点击的“删除消息”节点。
- 失败处理：超时无节点时记录可见菜单文本并停止。
- 副作用与幂等性：只读。

### G07 点击“删除消息”
- 目的：从操作菜单进入删除确认框。
- 代码位置：`scripts/hr/cleanup.py:637-646`
- 前置条件：G06 已得到唯一菜单按钮。
- 原子动作：A02 点击菜单按钮 bounds。
- 输入：`serial`、菜单按钮。
- 输出：删除确认框层级。
- 成功判据：读屏出现唯一“删除”按钮或达到 2 秒超时交给 G08 判定。
- 失败处理：异常或断连停止；`confirm_mismatch` 记录。
- 副作用与幂等性：仅打开确认框；尚未删除。

### G08 判定删除确认框点名
- 目的：确认弹层确实在询问删除当前联系人，避免误删相邻会话。
- 代码位置：`scripts/hr/cleanup.py:652-654`、`416-428`（`_confirm_names_contact`）
- 前置条件：G07 已打开确认框。
- 原子动作：无设备动作；检查存在唯一“删除”按钮，且页面文本包含完整句子“确认删除与{姓名}的聊天吗？”或独立姓名节点。
- 输入：`dialog`、`contact_name`。
- 输出：通过或 `confirm_mismatch`。
- 成功判据：按钮唯一且姓名归一化后完全一致。
- 失败处理：不满足则停止并记 `confirm_mismatch`。
- 副作用与幂等性：只读。

### G09 点击“删除”（不可逆）
- 目的：在确认框中执行手机端会话删除。
- 代码位置：`scripts/hr/cleanup.py:658-660`
- 前置条件：G08 通过；台账已归档（G12）。
- 原子动作：A02 点击唯一“删除”按钮，然后 G10 等待，再 A01 读屏。
- 输入：`serial`、删除按钮。
- 输出：删除后的列表层级。
- 成功判据：只能由 G11/J14 判定；点击成功不等于删除成功。
- 失败处理：点击后任何异常/断连都记 `delete_unverified` 并停止，禁止重试。
- 副作用与幂等性：**不可逆删除手机端会话**；聊天记录在手机端消失，本地数据库不随之删除。

### G10 等待删除动画
- 目的：删除后等待列表稳定，避免在动画中途读屏导致误判。
- 代码位置：`scripts/hr/cleanup.py:473-484`
- 前置条件：G09 已点击删除。
- 原子动作：A06 截图稳定等待（3000ms 上限、200ms 间隔）。
- 输入：`serial`。
- 输出：无。
- 成功判据：画面稳定或达到超时。
- 失败处理：截图不可用回退 0.5 秒；断连向上抛出并停止。
- 副作用与幂等性：只读。

### G11 核验删除结果
- 目的：用列表证据证明目标会话已被删除。
- 代码位置：`scripts/hr/cleanup.py:661-700`
- 前置条件：G09 已点击、G10 已等待。
- 原子动作：A01 读屏 → `_visible_row_identities`；检查目标是否仍存在、锚点是否仍唯一、是否出现“删除成功”。
- 输入：`after`、`target_identity`、`anchor_identities`。
- 输出：成功或 `delete_unverified`（含 `target_still_present`/`anchor_missing`/`delete_list_unparsed` 等）。
- 成功判据：J14。
- 失败处理：任何不确定都停止并把台账记 `unknown`（G13），不自动重试。
- 副作用与幂等性：只读；但此时删除可能已发生，无法回滚。

### G12 台账归档
- 目的：删除前登记候选，保证“已尝试”可审计，避免不确定动作被重复执行。
- 代码位置：`scripts/hr/store.py:356-376`（`archive_cleanup_candidate`）、`scripts/hr/cleanup.py:562-571`
- 前置条件：候选通过 J13；台账无同会话同列表时间的既有行。
- 原子动作：`INSERT OR IGNORE ... result='archived'`。
- 输入：`conversation_key`、身份字段、`category`、`evidence`、`list_preview`、`list_time`。
- 输出：是否插入新行。
- 成功判据：插入成功或已存在同键行。
- 失败处理：字段非法抛 `ValueError`；已存在不覆盖。
- 副作用与幂等性：本地写入；幂等。

### G13 台账收敛（deleted/unknown）
- 目的：删除核验后把台账从 `archived` 改为 `deleted` 或 `unknown`。
- 代码位置：`scripts/hr/store.py:575-583`（`mark_cleanup_deleted`）、`586-597`（`mark_cleanup_unknown`）、`scripts/hr/cleanup.py:697-700`、`537-551`
- 前置条件：台账行为 `archived`。
- 原子动作：`UPDATE ... SET result='deleted',deleted_at=?` 或 `result='unknown',detail_code=? WHERE result='archived'`。
- 输入：`conversation_key`、`list_time`、`detail_code`。
- 输出：布尔值。
- 成功判据：`rowcount == 1`。
- 失败处理：`unknown` 的 detail 码限制在 `menu_unverified/confirm_mismatch/delete_unverified/page_unexpected`；0 行说明台账已变化，流程仍停止。
- 副作用与幂等性：本地写入；`unknown` 会阻止下次自动重试（`scripts/hr/cleanup.py:562-569`）。

### H01 预检设备选择
- 目的：心跳启动前确认唯一可用设备。
- 代码位置：`scripts/heartbeat/heartbeat.py:119-148`、`scripts/core/adb.py:110-148`
- 前置条件：`heartbeat start` 已由用户确认。
- 原子动作：调用 core 设备原语：`adb devices` 选择设备；失败时按状态区分 `device_disconnected`/`device_unauthorized`/`device_ambiguous`。
- 输入：`requested_serial`。
- 输出：`serial` 或 `preflight_failed` 结果。
- 成功判据：唯一设备且状态为 `device`。
- 失败处理：断连/未授权不再执行截图；其他失败尽力截图（`scripts/heartbeat/heartbeat.py:95-117`）。
- 副作用与幂等性：只读；可能写预检截图。

### H02 预检解锁
- 目的：确认设备未锁屏，心跳才能操作应用。
- 代码位置：`scripts/heartbeat/heartbeat.py:149-154`、`41-43`、`scripts/core/adb.py:180-185`
- 前置条件：H01 已选中设备。
- 原子动作：调用 core 设备原语：读取 Keyguard 状态，仅明确未显示时放行。
- 输入：`serial`。
- 输出：无或 `preflight_failed(device_locked)`。
- 成功判据：`verify_unlocked` 不抛异常。
- 失败处理：`RuntimeError` 映射 `device_locked`；ADB 错误映射 `device_disconnected`。
- 副作用与幂等性：只读。

### H03 创建心跳并立即开始首轮
- 目的：落库唯一活动心跳，并把首轮标记为进行中。
- 代码位置：`scripts/heartbeat/heartbeat.py:165-172`、`scripts/heartbeat/store.py:42-63`
- 前置条件：无活动心跳（除非 `--recover`）；`interval_seconds >= 1`；无对账阻塞。
- 原子动作：`BEGIN IMMEDIATE` 检查无 `active` 行 → 插入 `state='active'`、`round_in_progress=1`、`next_due_at=now`。
- 输入：`serial`、`interval_seconds`、`started_at`、`next_due_at`。
- 输出：`run_id` 与 `status=round_started`。
- 成功判据：插入成功且读取到该 run。
- 失败处理：已有活动心跳抛 `RuntimeError`；事务回滚。
- 副作用与幂等性：本地写入；唯一索引 `one_active_heartbeat` 保证最多一个活动心跳（`scripts/heartbeat/store.py:37-38`）。

### H04 查询状态
- 目的：只读报告活动/最近心跳与到期情况。
- 代码位置：`scripts/heartbeat/heartbeat.py:180-203`、`scripts/heartbeat/store.py:78-114`
- 前置条件：无（不连接设备）。
- 原子动作：无设备动作；按 `run_id` 或活动行读取，计算 `round_age_seconds`、`due`、`seconds_until_due`。
- 输入：`run_id`（可选）。
- 输出：状态字典。
- 成功判据：读取成功。
- 失败处理：无活动返回 `not_active`；指定 ID 不存在返回 `not_found`。
- 副作用与幂等性：只读。

### H05 等待到期（轮询）
- 目的：在后台等待 `next_due_at`，期间持续检查停止、断连与锁屏。
- 代码位置：`scripts/heartbeat/heartbeat.py:445-461`、`414-444`
- 前置条件：存在活动心跳且当前无进行中轮次。
- 原子动作：按 `poll_seconds` 睡眠；每次醒来读取心跳行；未跳过设备检查时执行 H10。
- 输入：`poll_seconds`、`skip_device_check`、`device_check`、`unlock_check`。
- 输出：继续等待 / `stopped` / `already_running` / `blocked`。
- 成功判据：到达 `due_at` 后进入 H06。
- 失败处理：心跳被停止返回 `_stopped_result`；设备检查失败停止心跳并返回原因。
- 副作用与幂等性：睡眠与轮询本身无业务副作用；`main.py` 用独立设备命令包裹每次检查（`scripts/main.py:1153-1169`）。

### H06 原子占用轮次
- 目的：到期后只让一个等待进程开启下一轮。
- 代码位置：`scripts/heartbeat/heartbeat.py:468-474`、`scripts/heartbeat/store.py:117-125`
- 前置条件：已到期、无进行中轮次、无对账阻塞。
- 原子动作：`UPDATE ... SET round_in_progress=1, round_started_at=? WHERE run_id=? AND state='active' AND round_in_progress=0 AND next_due_at<=?`。
- 输入：`run_id`、`now_iso`。
- 输出：`due` 或 `already_running`/停止结果。
- 成功判据：`rowcount == 1`。
- 失败处理：抢不到时重新读取；若已在轮次中返回 `already_running`。
- 副作用与幂等性：本地写入；原子，不会双开轮次。

### H07 结束轮次并设置下次到期
- 目的：本轮业务完成后记录结束时间并推进 `next_due_at`。
- 代码位置：`scripts/heartbeat/heartbeat.py:233-239`、`scripts/heartbeat/store.py:128-142`
- 前置条件：存在进行中轮次；无对账阻塞。
- 原子动作：`UPDATE ... SET round_in_progress=0, last_round_started_at=round_started_at, round_started_at=NULL, last_round_finished_at=?, next_due_at=?`。
- 输入：`run_id`、`finished_at`、`next_due_at = finished_at + interval_seconds`。
- 输出：布尔值；后续写报告/审计。
- 成功判据：`rowcount == 1`。
- 失败处理：状态已变化抛 `RuntimeError("心跳轮次状态已变化")`。
- 副作用与幂等性：本地写入；重复调用会因 `round_in_progress=1` 条件不再命中。

### H08 审计门控回退轮次
- 目的：`enforce` 模式下审计 `fail` 或 B 档判定不可信时，不推进下一轮并恢复本轮。
- 代码位置：`scripts/heartbeat/heartbeat.py:240-267`、`scripts/heartbeat/store.py:145-160`
- 前置条件：`audit_mode='enforce'`；合并结论为 `fail` 或 `judge_untrusted`。
- 原子动作：`UPDATE ... SET round_in_progress=1, round_started_at=COALESCE(last_round_started_at, round_started_at), last_round_finished_at=NULL, next_due_at=<原值>`。
- 输入：`run_id`、`previous_next_due`。
- 输出：`paused` 结果，含 `reason_code=audit_failed/judge_untrusted`。
- 成功判据：`rowcount == 1`。
- 失败处理：状态已变化抛 `RuntimeError`。
- 副作用与幂等性：本地写入；报告/审计故障只记录错误类型，不改变裁决（`scripts/heartbeat/heartbeat.py:309-349`）。

### H09 停止心跳
- 目的：用户确认后停止活动心跳，不提供自动恢复。
- 代码位置：`scripts/heartbeat/heartbeat.py:398-411`、`scripts/heartbeat/store.py:163-174`
- 前置条件：`--confirmed`；存在活动心跳；`reason` 为最多 64 位字母/数字/下划线。
- 原子动作：`UPDATE ... SET state='stopped', ended_at=?, stop_reason=?, round_in_progress=0, ...`。
- 输入：`reason`、`stopped_at`。
- 输出：`status=stopped` 与心跳行。
- 成功判据：`rowcount == 1`。
- 失败处理：无活动心跳抛 `RuntimeError`；原因码非法抛 `ValueError`。
- 副作用与幂等性：本地写入；停止后没有自动恢复路径。

### H10 心跳内设备/锁屏检查
- 目的：等待与到期时确认设备仍连接且已解锁，异常则立即停止心跳。
- 代码位置：`scripts/heartbeat/heartbeat.py:55-75`
- 前置条件：存在活动心跳。
- 原子动作：调用注入的 `device_check`（默认 `adb.select_device`）与 `unlock_check`（默认 `_verify_unlocked`）。
- 输入：`run`、`moment`、检查函数。
- 输出：`None`（通过）或停止原因码。
- 成功判据：两项检查均不抛异常。
- 失败处理：`DispatchStateError` 取 `reason`；断连取 `error.reason`；其他 ADB/运行时错误归 `device_disconnected` 或 `device_locked`；随后调用 H09 的 store 停止函数（`scripts/heartbeat/store.py:163-174`）。
- 副作用与幂等性：只读检查 + 可能的本地停止写入。

### H11 显式恢复心跳
- 目的：用户确认手机重连后收敛上次残留轮次并立即开始新心跳。
- 代码位置：`scripts/heartbeat/heartbeat.py:157-164`、`scripts/heartbeat/store.py:177-189`
- 前置条件：`--recover`；存在活动心跳。
- 原子动作：`UPDATE ... SET state='stopped', stop_reason = CASE WHEN round_in_progress=1 THEN 'round_interrupted' ELSE 'restarted_after_reconnect' END`，随后 H03 创建新 run。
- 输入：`stopped_at`。
- 输出：`recovered.previous_run_id`/`previous_stop_reason`。
- 成功判据：`rowcount == 1` 且新 run 创建成功。
- 失败处理：状态已变化抛 `RuntimeError`。
- 副作用与幂等性：本地写入；仅在显式恢复路径执行。

## 3 组合流程

### C-1 `hr apply`：interested（发送回复 + 简历/联系方式）
1. 读契约与决策，取出唯一 `message_key`（`main.py:940-955`）→ J18 授权检查。
2. E01 登记 `resume`/`wechat`/`phone`/`reply` 的 `pending`（`apply.py:684`）。
3. 对 `resume`：D01 → J12 → D02 → J10；或 D03（HR 已索取）→ D02。
4. 对 `wechat`/`phone`：C01 → J11/C02 → C02。
5. 对 `reply`：B01 → B02 → B03 → B05 → B04 → B05 → B08 → B09 → B10。
6. E02 收敛每个动作；`unknown`/`needs_probe`/`needs_attachment` 立即暂停（`apply.py:782-790`）。
7. 发送成功后重新读聊天：J09 确认尾部；必要时 E05 保存草稿（`apply.py:819-887`）。
8. 结束发现、返回列表、刷新基线（`apply.py:420-450`）。

### C-2 `hr apply`：rejected（删除会话）
1. E01 前先确认 `--serial` 存在与授权 `delete_rejected`（`apply.py:512-523`）。
2. 若 `keeps_contact`：只 C01/C02 交换电话，不删除（`apply.py:603-604`）。
3. 否则先 F01 返回列表，再 G01 → G02（`force_rejected=True`、`limit=1`、`min_age_days=0`）→ G03 → G04 → G05 → G06 → G07 → G08 → G09 → G10 → G11 → G13（`apply.py:558-568`）。
4. 只有 `deleted_count == 1` 才写 `rejected` 并关闭消息；否则暂停 `rejected_cleanup_unverified`。

### C-3 `hr apply`：declined / needs_human / uncertain
1. `declined` + `reply.action=send`：E01(reply) → B01…B10 → E02 → 关闭 `declined_sent`（`apply.py:591-597`、`889-893`）。
2. `declined` + `draft`：E05，关闭 `declined`（`apply.py:593-595`）。
3. `needs_human`/`uncertain`：E05（若 draft）→ 固定等待 E02 前置门禁 → 发送 `HOLDING_REPLY_TEXT`（B 序列，正文固定）→ E02；`unknown` 暂停，`not_sent` 记升级（`apply.py:572-589`、`278-367`）。

### C-4 `hr review send-text`（主动追加文本）
1. 校验 64 位会话键、正文长度 1-300、无手机号/微信号、24 小时去重与每日 3 条上限（`review.py:104-145`、`173-191`）。
2. 读屏核验联系人（`review.py:195-201`）→ 再次检查 J06 与身份。
3. E01 登记 `reply/pending/manual_followup`（`review.py:213-214`）。
4. B01…B10（正文为人工文本）。
5. 成功：J09 尾部核验 → 记录最后我方消息 → E02(`sent`)（`review.py:233-260`）。
6. 未知/失败：E02(`unknown/needs_probe/failed/not_sent`)，不重试（`review.py:262-278`）。

### C-5 `hr review send-draft`
1. J16 预检（`review.py:94-101`）→ 设备选择（`review.py:302-308`）。
2. F01 → F02 → G02 定位会话行（`review.py:446-479`）→ F03 → F04。
3. E01 登记 `reply/pending/draft_send`（`review.py:500-501`）。
4. B01…B10（正文为草稿文本）。
5. 成功：F05 → F06（`review.py:510-530`）；失败：E02 收敛，明确未发送可重试。

### C-6 `hr review` 待核对解除
1. `list`：只读 J06/J07 清单（`review.py:56-91`）。
2. 人工在应用内目视核对后：
   - 未发送 → E03；
   - 已发送 → E04；
   - 消息整体处理完 → E07；
   - 非文本 → E09；
   - 仅简历状态 → E10；
   - 草稿正文需改 → E06；
   - 需重扫 → E08。
3. 每步都经 J18 的 `--confirmed` 门禁。

### C-7 `hr review continue`（blocked 恢复）
1. 校验 `--confirmed`、`message_key`、`--serial`（`review.py:350-355`）。
2. 从持久 blocked 轮次读取契约/决策路径并核对摘要（`review.py:356-378`）。
3. 在设备命令围栏内重跑 `apply`，只允许 `interested` 且只执行已核对 `needs_probe`（`review.py:384-395`、`apply.py:454-496`）。
4. 复用 C-1 的动作链；恢复前重读聊天尾部（J09）。

### C-8 `hr cleanup list`（只读候选）
1. H01 设备选择 + J13 限制校验（`cleanup.py:228-232`）。
2. A01 读屏 → J13 页面判定（`cleanup.py:246-251`）。
3. G01 → G02 → 输出 `candidates/skipped/counts`。
4. 不写台账、不改业务表（`cleanup.py:219-221`）。

### C-9 `hr cleanup run`（删除）
1. J18 `--confirmed` 与限制校验（`cleanup.py:500-509`）。
2. 若存在“取消筛选”：A02 点击并 A01 复读（`cleanup.py:714-726`）。
3. G01 → G02 → G03 → G12 → G04 → G05 → G06 → G07 → G08 → G09 → G10 → G11 → G13。
4. 任一不确定立即停止并 G13 记 `unknown`，不重试（`cleanup.py:537-551`）。

### C-10 心跳一轮
1. `heartbeat start`：H01 → H02 → H03（首轮立即进行中）。
2. `heartbeat wait`：H05 轮询 → H10 → H06 占用。
3. 业务侧执行 HR 扫描/动作（本文件不展开 scan 只读细节）。
4. `heartbeat round-done`：H07 → 报告 → 审计/B 档判定（`heartbeat.py:309-349`）→ 若 `enforce` 且不通过，H08 回退；否则 `waiting`。
5. `heartbeat stop`：H09；重连后 `--recover`：H11 → H03。

## 4 数据与副作用边界

- **本地数据库**：`hr_action_events`（意图/结果/待核对）、`hr_reply_drafts`、`hr_messages`、`hr_conversations`、`hr_cleanup_ledger`、`hr_non_text_pending`、`hr_non_text_review_events`、`heartbeat_runs`。写入集中在 `scripts/hr/store.py` 与 `scripts/heartbeat/store.py`；进程中断后以数据库事实为准，不靠内存恢复。
- **操作日志**：`scripts/core/operation_log.py` 记录命令拒绝、人工处置、输入法恢复失败、发送后尾部未知等；日志只含码值与摘要键，不含正文（`scripts/hr/actions.py:849`、`870`、`scripts/hr/apply.py:870`）。
- **截图**：清理停止现场写入 `screenshots/hr-cleanup-<time_ns>.png`（`scripts/hr/cleanup.py:443`）；心跳预检写入 `screenshots/heartbeat-preflight-<timestamp>.png`（`scripts/heartbeat/heartbeat.py:107-109`）。截图含聊天画面，属于敏感本机文件。
- **对外发送**：B09（文本）、C01/C02（联系方式请求与确认）、D02/D03（附件简历）会立即对 HR 可见且不可撤回；`unknown` 必须人工核对，不得自动重试。
- **对外删除**：G09 删除手机端会话不可逆；本地数据库不会同步删除，后续同名 HR 再发消息可能以新会话出现（`scripts/hr/store.py:710-733` 保留上下文）。
- **输入法副作用**：B06/B07 改变设备全局输入法；异常退出可能遗留 ADBKeyboard，需人工确认。
- **心跳调度**：H03/H06/H07/H08/H09/H11 都是本地状态迁移；心跳本身不直接发消息，业务动作仍走 `hr apply/review`。

## 5 未覆盖与不确定项

1. `scripts/core/status.py:17-24` 的 `ACTION_STATUSES` 全集未逐项展开；本文件只确认 `pending/not_sent/request_sent` 是补充状态，完整词表需在实现时以该文件为准。
2. 各目录 `AGENTS.md` 仅作背景，未逐条与代码比对；如存在冲突，本文件按任务要求以代码为准。
3. `_observed_click` 的 `input_swipe` 降级通道（60ms/800ms）是否被 BOSS 控件真实接受，代码注释明确“不保证响应”（`scripts/core/adb.py:259-262`），无法静态确认。
4. 清理长按使用 1200ms（`scripts/hr/cleanup.py:596-597`），代码只注释“等画面稳定再长按”，未给出 1200ms 的实测依据；该值可能随机型变化。
5. `hr apply` 的拒绝清理调用 `run_candidates(min_age_days=0, limit=1, force_rejected=True)`（`scripts/hr/apply.py:558-560`），与 CLI 默认 `--min-age-days 0`、常量 `CLEANUP_AGE_DAYS=7` 的组合语义需要产品确认：代码上 `force_rejected` 仍受未读角标/草稿/待办保护，但“即时清理例外”的最终边界建议在实现前复核。
6. `heartbeat wait` 在 `main.py` 用独立 `hr_scan.device_command` 包裹设备选择与解锁检查（`scripts/main.py:1153-1169`），而 `heartbeat.py` 默认直接调用 `adb.select_device`；两种路径的设备占用语义是否等价，参考仓库文档未说明。
7. `hr apply` 未提供 `--serial` 时 `DeviceApplyExecutor(None)` 会让动作返回 `needs_device`/`needs_attachment`（`scripts/hr/actions.py:357-414`），但拒绝分支已在更早位置要求 `--serial`（`scripts/hr/apply.py:518-523`）；其他意图的实际暂停码需按运行时 dispatch 授权确认。
8. 心跳的 A 档审计、B 档判定文件校验由 `main.py` 注入（`scripts/main.py:1105-1117`、`1132-1142`），`scripts/audit/*` 内部实现超出本文件范围，未展开。
9. 本文件覆盖 `hr/chat.py` 的 `verify_contact` 与 `open_message`，未展开 `read_full_chat`、`scroll_chat` 等扫描侧只读逻辑；这些属于 scan 章节。
10. 全部结论来自静态阅读，未运行 Python、未执行 ADB、未读取参考仓库运行数据；行号在参考仓库后续变更后可能漂移，落地实现时需重新定位函数名。
