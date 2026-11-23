# jobs 职位浏览与沟通原始操作

## 0 概览

### 0.1 入口命令

全部入口集中在 `scripts/main.py` 的参数字典与三个分派函数。

| 命令组 | 子命令 | 分派位置 | 是否操作设备 |
| --- | --- | --- | --- |
| `jobs` | `init` / `add` / `list` / `discoveries` / `check-continue` / `mark-continued` / `mark-applied` / `blacklist` | `scripts/main.py:170-196`（定义）、`scripts/main.py:551-643`（`run_jobs`） | 否，纯本地库 |
| `jobs` | `recommend capture` / `recommend apply` | `scripts/main.py:198-210`、`scripts/main.py:553-581` | 是（聊天页推荐弹层） |
| `adb` | `inspect` / `open-item` / `scroll` / `capture` / `confirm-identity` / `continue` | `scripts/main.py:220-241`、`scripts/main.py:646-674`（`run_adb`） | 是（职位列表、详情页） |
| `batch` | `start` / `status` / `close` / `active` / `probe` / `resolve` / `resolve-share` / `run` / `cooldown` | `scripts/main.py:242-273`、`scripts/main.py:677-811`（`run_batch`） | `probe`、`run` 是；其余为本地库或仅门禁 |

### 0.2 涉及文件

- `scripts/jobs/store.py`：职位表、发现事件、列表别名、黑名单、沟通/投递状态与沟通前置门禁（纯本地）。
- `scripts/jobs/resources.py`：从 `docs/求职展示资源.md` 读取批次正文并校验 URL（纯本地，`resources.py:11-21`）。
- `scripts/jobs/enroll.py`：列表卡片读取、详情页读取、滚动、打开卡片、入库、身份核对、继续沟通。
- `scripts/jobs/recommend.py`：聊天页推荐卡识别、弹层打开、候选解析、判定校验、推荐职位沟通。
- `scripts/jobs/batch.py`：批次主循环、新职位通知入口、列表恢复、底部 tab 导航、批次检查点。
- `scripts/jobs/batch_store.py`：批次、批次职位、资源消息发送状态、每日配额与冷却（纯本地）。
- 复用到的核心层：`scripts/core/adb.py`、`scripts/core/uitree.py`、`scripts/core/workspace.py`、`scripts/core/message_list.py`、`scripts/hr/actions.py`、`scripts/hr/chat.py`。

### 0.3 涉及页面

- 消息列表（`chat_list`，含「发布了新职位」通知行）。
- 综合推荐页（`recommend_feed`，含「新职位」标签）。
- 职位列表（可滚动容器 `*:id/recycler_view` 或 `*:id/rv_list`）。
- 职位详情页（含 `tv_description` 与 `btn_chat` / `btn_continue_chat`）。
- 聊天详情页（含 `mRecycleView` 或 `et_msg`；推荐卡与推荐弹层也在此页之上）。
- 推荐弹层（「活跃职位列表」标题 + `rv_recommend_job_list`）。
- 二次确认弹窗（「温馨提示」+「是否就新职位…」+「沟通新职位」）。

### 0.4 前置状态

1. 唯一一台已授权设备；多设备必须显式 `--serial`（`core/adb.py:110-147`）。
2. 前台为 BOSS 直聘（包名 `com.hpbr.bosszhipin`，`core/adb.py:19`）；`adb inspect` 显式校验 `current_activity` 以包名开头（`enroll.py:370-372`）。
3. 页面停在其中之一：消息列表、职位列表、职位详情页、待处理聊天页（`batch.py:508-512` 的报错文案给出了允许集合）。
4. 批次类操作要求存在 `status = 'active'` 的批次且存在可预留名额（`enroll.py:645-648`、`batch_store.py:577-583`）。
5. 继续沟通要求职位已入库、身份已核对、未沟通、未投递（`store.py:411-423`）。
6. 资源消息正文必须含至少一个 URL 且不重复（`resources.py:18-20`）。

---

## 1 状态判定原语

每条都是只读判定，不含点击；`## 2` 的动作以这些 ID 作为前置条件或成功判据。

- **P01 页面归类**：按弹窗 → 登录 → `ChatRoomActivity` → `BossJobPagerActivity` → 综合推荐 → 聊天详情 → 职位详情 → 消息列表 → 底部标签的顺序保守归类，识别不到就返回 `unknown`。代码：`scripts/core/workspace.py:274`（`classify_page`）。
- **P02 综合推荐页**：文本包含「综合推荐」，或同时包含「收藏过我」「看过我」「新职位」三个标签。代码：`scripts/core/workspace.py:261`（`is_recommend_feed`）。
- **P03 职位卡片提取**：遍历 `clickable = "true"` 的节点，要求 `tv_position_name`、`tv_salary_statue`、`tv_company_name` 三者皆非空；标题去掉首尾 `@ % 空格 &`；输出整卡 bounds、标题节点 bounds、公司名节点 bounds。缺任一字段的节点不算卡片。代码：`scripts/jobs/enroll.py:107-142`（`visible_job_cards`）。
- **P04 列表可视区**：唯一一个 `scrollable = "true"` 且 resource-id 以 `recycler_view` 或 `rv_list` 结尾的容器；没有则返回 `None`（`None` 时视为全部可见）。代码：`scripts/jobs/enroll.py:166-174`（`listing_viewport`）。
- **P05 卡片完整可见**：整卡四边都在可视区内，且标题下沿 `< 可视区下沿 - 24`。代码：`scripts/jobs/enroll.py:177-185`（`card_fully_visible`）。
- **P06 卡片阻断**：依次命中「标题/公司为空」「公司黑名单（可选开关）」「同标题同公司的已入库职位中已沟通或已投递」「同标题同公司命中多条」四种原因，返回第一个原因文本，否则 `None`。代码：`scripts/jobs/enroll.py:351-363`（`blocked_card_reason`）。
- **P07 详情字段与详情页判定**：必须有非空 `tv_description` 节点，且存在 `btn_chat` / `btn_continue_chat` 之一，否则 `RuntimeError("当前页面不是可识别的职位详情页")`；随后读取标题、薪资、公司、招聘者、地点、正文。代码：`scripts/jobs/enroll.py:188-263`（`detail_fields`，断言在 `:200-204`）。
- **P08 详情完整度**：正文不含「查看更多」，且不以 `…` 或 `...` 结尾即为完整。代码：`scripts/jobs/enroll.py:253-254`。
- **P09 详情身份一致**：标题规范化后相等；公司规范化后相等，或该职位已有列表别名（公司不可见时）。代码：`scripts/jobs/enroll.py:341-348`（`detail_identity_matches`）。
- **P10 沟通按钮唯一可用**：`resource-id` 以 `btn_chat` 结尾、`clickable = "true"`、文本在 `{立即沟通, 继续沟通}` 内的节点必须恰好 1 个；否则不下点击。代码：`scripts/jobs/enroll.py:694-696`、`scripts/jobs/recommend.py:364-366`。
- **P11 沟通结果三证据**：同一屏文本同时满足「含『由你发起的沟通』」+「某文本按 `title_matches_chat` 匹配职位标题」+「某文本按 `company_matches_chat` 匹配公司」，最多读 3 次、间隔 1.0 秒。代码：`scripts/jobs/enroll.py:591-620`（`_confirm_continue_page`）。
- **P12 「是否就新职位继续沟通」弹窗**：文本同时含「温馨提示」「是否就新职位…」「沟通新职位」。代码：`scripts/jobs/enroll.py:566-572`（`_confirm_continue_dialog`）。
- **P13 推荐卡存在**：`ll_recommend_job_container` 容器内存在 `tv_recommend_job_name`。代码：`scripts/jobs/recommend.py:24-27`（`detect_card`）。
- **P14 推荐弹层已打开**：存在 `tv_active_jobs_title`，且 `rv_recommend_job_list` 恰好 1 个。代码：`scripts/jobs/recommend.py:209-212`（`_sheet_open`）；`open_sheet` 内的同义判定见 `:46-54`。
- **P15 候选行解析**：`rv_recommend_job_list` 必须唯一；只取「标题与公司都非空」的直属子节点行，按出现顺序编号 1..n；标签容器内跳过 `visible-to-user="false"` 的节点。代码：`scripts/jobs/recommend.py:58-90`。
- **P16 「新职位」标签及选中态**：页面是综合推荐页，且文本等于「新职位」的节点恰好 1 个；选中态读取 `selected` / `isSelected` / `is-selected` 三种属性，全无则返回 `None`（未知）。代码：`scripts/jobs/batch.py:221-234`（`_new_job_tab`）。
- **P17 新职位通知行**：消息列表行解析结果中，行名包含「发布了新职位」的第一行即目标。代码：`scripts/jobs/batch.py:292-296`；行解析在 `scripts/core/message_list.py:239-290`（`parse_message_list`）。
- **P18 消息列表可读**：唯一 `recyclerView` 可被解析，且至少有一行带非空姓名。代码：`scripts/core/message_list.py:396-403`（`message_list_visible`）。
- **P19 批次推进门禁**：依次检查批次存在 → 已完成 → 已关闭 → 存在 `pending`/`reserved`（→`needs_reconciliation`）→ 存在未收敛资源消息（→`needs_resource_share_review`）→ 时间预算 → 每日配额。代码：`scripts/jobs/batch.py:387-403`、`scripts/jobs/batch_store.py:285-301`（`reconciliation_reason`）。
- **P20 聊天联系人一致**：聊天页 `tv_title` 文本集合必须逐字包含预期 HR 名。代码：`scripts/hr/chat.py:885-890`（`verify_contact`）。
- **P21 资源消息已存在计数**：统计 `tv_content_text` 且文本与正文完全相等的节点数，作为发送前的基线。代码：`scripts/hr/actions.py:722-726`；发送后同口径计数增加即视为已发送（`:818-825`）。

---

## 2 原子操作

### 2.0 说明

`A01`–`A05` 是命令层原语：一次读屏、一次点击、一次滑动、一次返回、一次截图，本身不承载业务目标，多个语义动作复用同一原语。`A06` 起每个 ID 绑定一个**具体目标定位规则 + 语义 + 判据**的点击，因此与 `A02` 不是重复定义；业务流程直接引用 `A06` 起的 ID，不直接引用 `A02`。

### A01 读取当前 UI 层级

- 目的：拿到当前界面的完整 Android 控件树，作为所有判定与坐标来源。
- 代码位置：`scripts/core/adb.py:205-246`（`read_hierarchy`）。
- 前置条件：设备已授权、前台为 BOSS 直聘。
- 原子动作：一次读屏序列，固定三条 ADB 往返——`adb -s <serial> shell rm -f /sdcard/job_tracker_ui.xml` → `shell uiautomator dump /sdcard/job_tracker_ui.xml` → `shell cat /sdcard/job_tracker_ui.xml`，随后本地解析 XML。
- 输入：`serial`。
- 输出：`ET.Element`（根节点）。
- 成功判据：XML 可解析，且存在 `package="com.hpbr.bosszhipin"` 的节点；否则抛错（`core/adb.py:233-239`）。
- 失败处理：`uiautomator dump` 退出码 137 时重试 1 次，再失败抛 `RuntimeError`（`:214-222`）；解析失败包成 `RuntimeError("Android UI 层级不是有效 XML")`（`:232-235`）。调用方各自决定重试次数（如 `enroll.py:412-420` 最多 5 次、间隔 0.4 秒）。
- 副作用与幂等性：设备上写入临时文件 `/sdcard/job_tracker_ui.xml`；纯读操作，可重复执行。

### A02 按节点中心点击

- 目的：把任意已定位节点转成一次点击。
- 代码位置：`scripts/core/adb.py:249-262`（`tap_bounds`）。
- 前置条件：节点 `bounds` 可解析为四元组。
- 原子动作：单次 `adb -s <serial> shell input tap <x> <y>`；`x = (left + right) // 2`，`y = (top + bottom) // 2`。坐标来源：目标节点的 `bounds` 属性，不做缩放换算。
- 输入：`serial`、`bounds`、通道（默认 `input_tap`）。
- 输出：无。
- 成功判据：命令返回成功（`run_adb` 不抛错）；点击是否生效由调用方用下一次 A01 判定。
- 失败处理：ADB 非零退出或设备断开时抛异常；本层不重试、不降级到长按（`input_swipe`/`long_press` 通道仅提供能力，`:258-262`）。
- 副作用与幂等性：不可幂等；重复点击可能触发两次跳转或两次沟通。

### A03 在可滚动容器内滑动一次

- 目的：职位列表、职位详情、消息列表的翻页/回扫。
- 代码位置：`scripts/jobs/enroll.py:448-477`（`scroll_step`）；命令层 `scripts/core/adb.py:265-268`（`swipe`）。
- 前置条件：当前层级中存在 `scrollable = "true"` 且 resource-id 以 `recycler_view` 或 `rv_list` 结尾的容器，且多于 1 个时报错（`enroll.py:453-463`）。
- 原子动作：单次 `adb -s <serial> shell input swipe <x> <startY> <x> <endY> 450`；`x` 为容器水平中点，`upper = top + 0.25 × 高度`，`lower = top + 0.68 × 高度`；`direction = up` 时 `start = lower, end = upper`（手指上滑），`down` 时相反。滑动后等待 0.5 秒（`enroll.py:472`）。
- 输入：`serial`、`direction ∈ {up, down}`。
- 输出：`changed: bool` 与滑动后卡片清单（`enroll.py:476`）。
- 成功判据：滑动前后 `ui.screen_signature` 不同（`core/uitree.py:22-28`，按可见文本 + bounds 比对）。
- 失败处理：无容器抛 `RuntimeError("当前页面没有可识别的职位列表滚动容器")`，不重试；`changed = False` 由调用方转为「反向扫描」或「没有更多职位」（`batch.py:489-505`、`:450-459`）。
- 副作用与幂等性：视口位置变化，不可幂等；不改变任何业务状态。

### A04 发送返回键

- 目的：从详情页/聊天页/推荐区退回上一层。
- 代码位置：`scripts/jobs/batch.py:159`、`scripts/jobs/batch.py:172`、`scripts/jobs/recommend.py:228`；命令层 `scripts/core/adb.py:271-273`（`keyevent`）。
- 前置条件：无。
- 原子动作：单次 `adb -s <serial> shell input keyevent 4`，随后调用方等待 0.4 秒再读屏（`batch.py:160`、`:173`）。
- 输入：`serial`。
- 输出：无。
- 成功判据：本层不判定；由 A01 + P01/P03 判定是否已到达目标层。
- 失败处理：本层不重试；`return_to_list` 最多发 4 次（`batch.py:154`）。
- 副作用与幂等性：可能退出应用或触发「再按一次退出」；连发会产生额外层级回退。

### A05 截取当前屏幕

- 目的：为推荐卡、推荐弹层留下现场证据。
- 代码位置：`scripts/jobs/recommend.py:93-104`（`_snapshot`）；命令层 `scripts/core/adb.py:355-369`（`capture_screenshot`）。
- 前置条件：`monitor` 可提供轮次/事务 ID 以生成路径。
- 原子动作：单次 `adb -s <serial> exec-out screencap -p`，二进制写入 `screenshots/<日期>/[round-|transaction-]<id>/<序号>-<label>.png`（`core/events.py:73-99`）。
- 输入：`serial`、`label`、结果字典键名。
- 输出：图片路径写入结果字典；失败时写入 `screenshot_error[label] = 异常类型名`。
- 成功判据：文件写入完成。
- 失败处理：异常被捕获，只记录异常类型，不中断文本捕获流程（`recommend.py:100-104`）。
- 副作用与幂等性：每次生成新文件、不覆盖（序号自增）；截图含屏幕内容，属本机证据。

### A06 点击职位列表卡片打开详情

- 目的：从列表进入某个已选中卡片的职位详情页。
- 代码位置：`scripts/jobs/enroll.py:385-446`（`open_item_step`），点击在 `:408`。
- 前置条件：A01 已读列表；`1 ≤ index ≤ 卡片数`（`:390-392`）；P05 卡片完整可见（否则返回 `not_fully_visible`，`:395-398`）；P06 无阻断（黑名单不参与，`:400-405`）。
- 原子动作：单次点击，坐标取该卡片的 `company_bounds`（公司名节点 bounds），缺失时回退为整卡 bounds（`:136-138`）；经 A02 转换为 `input tap`。
- 输入：`serial`、`index`（1 起）。
- 输出：`True` / `{"action": "not_fully_visible", ...}` / `{"action": "open_failed", ...}` / `False`（被阻断）。
- 成功判据：点击后最多 5 次读屏、每次间隔 0.4 秒，P07 成功解析出详情字段（`:412-420`）。
- 失败处理：5 次仍不可读 → 返回 `open_failed` 并附带最多 8 KiB 的脱敏层级证据（`:416`、`:421-426`）；批次中连续 3 次打开失败即停批（`batch.py:467-478`）。
- 副作用与幂等性：页面跳转到详情页；重复点击会重复跳转，不重复写库。

### A07 点击推荐卡「查看更多」打开推荐弹层

- 目的：在聊天页把推荐卡展开为「活跃职位列表」弹层。
- 代码位置：`scripts/jobs/recommend.py:30-55`（`open_sheet`），点击在 `:43`。
- 前置条件：P01 判定为 `chat_detail`（`recommend.py:129-130`）、P13 推荐卡存在（`:131-132`）；「查看更多」必须是唯一可点击节点（`uitree.clickable_text`，`:32-34`）。
- 原子动作：单次点击，坐标取「查看更多」可点击祖先节点的 bounds，且点击前校验该节点的下沿落在某张推荐卡容器内并与其水平方向重叠（`:36-42`）；经 A02 执行 `input tap`。
- 输入：`serial`、当前层级 `root`。
- 输出：`(弹层层级, "")` 或 `(None, "sheet_unverified")`。
- 成功判据：点击后最多读屏 3 次，出现 `tv_active_jobs_title` 文本「活跃职位列表」且 `rv_recommend_job_list` 存在（`:46-54`）。
- 失败处理：找不到「查看更多」、层级校验不通过、点击抛错、3 次读屏都不符 → 统一返回 `sheet_unverified`，捕获流程以 `paused` 收尾（`:34`、`:42`、`:55`）。
- 副作用与幂等性：弹层覆盖在当前聊天页上；重复点击在弹层已开时找不到「查看更多」，会直接判失败。

### A08 点击推荐弹层关闭控件

- 目的：在不选择职位（`decision = none`）或存在活动批次时关闭弹层。
- 代码位置：`scripts/jobs/recommend.py:265-298`，点击在 `:289`。
- 前置条件：A01 已读当前层级；若因批次忙而关闭，还要求 P14 成立（`:270-271`）。
- 原子动作：按固定优先级选取唯一可点击节点——`iv_close_job_panel` → `btn_confirm` → 文本为「我知道了」或「知道了」的可点击祖先（`:273-287`）；随后单次点击其 bounds（A02）。
- 输入：`serial`、当前层级。
- 输出：`closed: bool`；未收敛时结果附带 `sheet_close_unverified = True`。
- 成功判据：点击后再读一次屏，`tv_active_jobs_title` 与 `rv_recommend_job_list` 都不存在（`:290-292`）。
- 失败处理：找不到控件或读屏异常一律不重试，置 `sheet_close_unverified` 后继续返回业务原因（`:293-298`）。
- 副作用与幂等性：弹层关闭；重复执行时控件消失，只会得到未收敛标记。

### A09 点击推荐弹层内候选职位行

- 目的：进入被选中候选职位的详情页。
- 代码位置：`scripts/jobs/recommend.py:320-338`，点击在 `:331`。
- 前置条件：P14 成立；重新解析的候选顺序中 `index` 位置标题必须与契约中 `chosen.title` 逐字相等（`:323-326`）；该行 `visible-to-user != "false"` 且 `clickable == "true"`（`:329-330`）。
- 原子动作：单次点击，坐标取该候选行的 bounds；经 A02 执行 `input tap`。
- 输入：`serial`、契约、`decision.index`。
- 输出：点击后的详情层 `root`；失败返回 `job_open_unverified` 或 `stale_contract`。
- 成功判据：点击后读屏，P01 判定为 `job_detail`（`:335-336`）；最多尝试 3 次（`:320`）。
- 失败处理：任一次读屏后仍非详情页且已是第 3 次 → `job_open_unverified`；标题或可见性不符 → `stale_contract`，不点击（`:324-330`）。
- 副作用与幂等性：页面跳转；重试只在弹层仍打开时进行，不构成重复沟通。

### A10 点击详情页「继续沟通 / 立即沟通」

- 目的：对当前详情页职位发起沟通。
- 代码位置：`scripts/jobs/enroll.py:694-704`（独立命令与批次）；`scripts/jobs/recommend.py:363-368`（推荐流程同义动作）。
- 前置条件：P07、P09 通过；P10 按钮唯一可用；批次已 `reserve_job` 且已 `mark_tap_started`（`enroll.py:697-701`）；推荐流程已保存职位并预留（`recommend.py:352-360`）。
- 原子动作：单次点击，坐标取该 `btn_chat` 节点的 bounds；经 A02 执行 `input tap`。
- 输入：`serial`、`job_id`、`batch_id`、当前详情层 `root`。
- 输出：本层无返回值；结果由后续判定给出。
- 成功判据：本层不判定；`enroll` 用 P11（`enroll.py:706`），`recommend` 用「P01 = chat_detail + `tv_title` 含联系人 + 文本含『由你发起的沟通』或『期望得到您的回复』」（`recommend.py:372-376`）。
- 失败处理：按钮缺失抛 `RuntimeError("当前详情页没有可确认的…控件")`（`enroll.py:695-696`）；点击前预留失败则释放预留并抛错（`:699-701`）。
- 副作用与幂等性：不可幂等，会真实发起沟通；`mark_tap_started` 先把批次行置 `pending`，异常时阻止再次点击（`batch_store.py:601-605`）。

### A11 点击二次确认「沟通新职位」

- 目的：同公司/同 HR 二次沟通时通过 BOSS 的确认弹窗。
- 代码位置：`scripts/jobs/enroll.py:575-588`（`_tap_continue_dialog`），点击在 `:585`。
- 前置条件：P12 成立；文本等于「沟通新职位」且 `clickable == "true"` 的节点唯一（`:579-583`）。
- 原子动作：单次点击，坐标取该按钮 bounds；经 A02 执行 `input tap`。
- 输入：`serial`、当前层级。
- 输出：`True` 表示已发出点击，`False` 表示未点击。
- 成功判据：本层只判「点击命令已发出」；实际结果仍回到 P11 循环继续读屏（`enroll.py:608-611`）。
- 失败处理：弹窗判定不成立或控件缺失 → 返回 `False`，不重试；整个流程内最多补点 1 次（`dialog_clicked` 标志，`:599`、`:608-611`）。
- 副作用与幂等性：可能触发第二次沟通；受「最多一次」标志保护。

### A12 点击底部「职位」标签

- 目的：从消息列表等页面切到职位列表。
- 代码位置：`scripts/jobs/batch.py:205-218`（`_navigate_to_job_tab`），点击在 `:214`。
- 前置条件：`workspace.locate_bottom_tab_target(root, "职位", ...)` 定位到节点；该节点必须 `clickable == "true"`，且不能是标签文本节点本身（`:209-213`）。
- 原子动作：单次点击，坐标取底部标签可点击祖先的 bounds；经 A02 执行 `input tap`。
- 输入：`serial`。
- 输出：`bool`。
- 成功判据：点击后新层级中 P03 至少识别出 1 张卡片（`:215`）。
- 失败处理：定位失败、点击异常 → 返回 `False`（`:211-218`）；批次中只尝试 1 次（`job_tab_attempted`，`:425-428`）。
- 副作用与幂等性：切换底部 tab；重复点击只是重复切页。

### A13 点击综合推荐页「新职位」标签

- 目的：在综合推荐页把列表切到「新职位」。
- 代码位置：`scripts/jobs/batch.py:253-256`（`_wait_for_new_job_list` 内），点击在 `:254`。
- 前置条件：P16 返回 `tab` 非 `None` 且选中态不为 `True`；同一轮等待内尚未点过（`tab_clicked`，`:253`）。
- 原子动作：单次点击，坐标取「新职位」可点击节点 bounds；经 A02 执行 `input tap`。
- 输入：`serial`、当前层级。
- 输出：无直接返回值；重置签名计数并继续轮询（`:255-257`）。
- 成功判据：后续读屏中 P03 非空（`:244-245`）。
- 失败处理：点击后若签名连续两次相同或出现骨架屏则立即停止等待并报 `job_list_not_loaded`（`:258-271`）。
- 副作用与幂等性：切换列表 tab；单轮最多点 1 次。

### A14 点击「发布了新职位」消息标题

- 目的：从消息列表进入系统推送的新职位列表。
- 代码位置：`scripts/jobs/batch.py:274-316`（`_search_new_job_message`），点击在 `:310`。
- 前置条件：P17 命中目标行；`open_notification = True`；行内存在文本逐字等于通知名的 title 节点（`:303-309`）。
- 原子动作：单次点击，坐标取该 title 文本节点的 bounds（点行内头像子节点是死区，必须点文本，`:302`）；经 A02 执行 `input tap`。
- 输入：`serial`、`batch_id`、`on_status`。
- 输出：命中并点击返回 `True`；无通知返回 `False`。
- 成功判据：点击后读屏，若 P01 不是 `chat_list` 则进入 `_wait_for_new_job_list`，最终 P03 非空（`:311-314`、`:237-245`）。
- 失败处理：`clickable_text` 不唯一 → `RuntimeError("无法唯一识别新职位消息入口")`（`:295-297`）；搜索 80 轮仍未找到 → `RuntimeError("新职位消息搜索达到上限…")`（`:328`）；失效通知点击后仍停留消息列表时，交回批次循环走 A12。
- 副作用与幂等性：跳转到新职位列表；通知失效时可能无跳转。

### A15 消息列表滑动一次

- 目的：在消息列表中向上/向下查找「发布了新职位」通知。
- 代码位置：`scripts/core/message_list.py:328-364`（`scroll_messages`），命令在 `:317-318` 与 `:353-354`。
- 前置条件：`recyclerView` 必须唯一（`:336-338`）；容器 `scrollable == "true"`（`:342-343`）；bounds 宽高为正。
- 原子动作：单次 `adb -s <serial> shell input swipe <x> <startY> <x> <endY> 450`；`x` = 列表水平中点，`upper = top + 0.25 × 高度`，`lower = top + 0.68 × 高度`；`up` → `lower` 到 `upper`，`down` 反之。
- 输入：`serial`、`root`、`direction`、`hierarchy_only`。
- 输出：`(changed, after_root)`。
- 成功判据：默认路径用 `adb.screen_action` 的截图门控（`core/adb.py:334-352`）判定视口变化；`hierarchy_only` 路径用滑动后 `screen_signature` 比较，并强制重新确认 P18（`message_list.py:319-324`）。
- 失败处理：截图门控连续两次证明未变化 → 返回 `False`；不可识别消息列表 → `RuntimeError`，不滑动。
- 副作用与幂等性：视口变化，不可幂等。

### A16 点击聊天输入框

- 目的：让 ADBKeyboard 的文本广播落到输入框。
- 代码位置：`scripts/hr/actions.py:738`（首次）与 `scripts/hr/actions.py:766`（写入失败后补点）。
- 前置条件：`editText_with_scrollbar` 节点恰好 1 个（`:727-730`、`:762-765`）。
- 原子动作：单次点击，坐标取该输入框 bounds 中心；经 A02 执行 `input tap`。
- 输入：`serial`、当前层级。
- 输出：无。
- 成功判据：本层不判定；由 A17/A18 之后的输入框文本回读判定（`:741-753`）。
- 失败处理：输入框不唯一则返回 `needs_send_control`，不点击。
- 副作用与幂等性：切换输入焦点；切换输入法后必须重新点击，否则广播落空（`:736-738`）。

### A17 广播清空输入框

- 目的：把残留草稿清空，避免与批次正文拼接。
- 代码位置：`scripts/hr/actions.py:739`、`:746`、`:759`。
- 前置条件：输入框已聚焦。
- 原子动作：单次 `adb -s <serial> shell am broadcast -a ADB_CLEAR_TEXT`，随后等待 0.2 秒。
- 输入：`serial`。
- 输出：无。
- 成功判据：回读输入框文本属于 `{"", "回复消息", "介绍自己回复率更高哦~", "和Ta聊一聊~"}` 之一（`:744-753`）。
- 失败处理：最多再清一次；仍不干净返回 `needs_draft_clear`，不发送（`:751-753`）。
- 副作用与幂等性：破坏性但可重复；仅影响本地草稿，不影响已发送消息。

### A18 广播写入批次正文

- 目的：把含 URL 的完整正文写入输入框。
- 代码位置：`scripts/hr/actions.py:755-777`，广播在 `:768`。
- 前置条件：A17 已完成且输入框干净。
- 原子动作：单次 `adb -s <serial> shell am broadcast -a ADB_INPUT_B64 --es msg <base64>`，随后等待 0.3 秒并回读；最多 2 轮（`:756`）。
- 输入：`serial`、`content`（`resources.py:11-21` 读取的正文）。
- 输出：无。
- 成功判据：回读输入框文本与正文完全相等（`:773-774`）。
- 失败处理：两轮都未写入 → 返回 `not_sent`（尚未点发送，可安全重试，`:775-777`）。
- 副作用与幂等性：覆盖输入框内容；重复执行结果一致。

### A19 点击发送控件

- 目的：把输入框里的正文发出去。
- 代码位置：`scripts/hr/actions.py:778-835`，点击在 `:787`；控件定位 `scripts/hr/actions.py:417-459`（`resource_send_control`）。
- 前置条件：输入框已含完整正文；发送控件能在输入框的祖先链中唯一定位（同一行、`clickable=true`、`enabled!=false`，优先 `Button` 类，其次含「发送」文本，其次 `ImageView`，取最深且唯一者）。
- 原子动作：单次点击，坐标取发送控件 bounds；经 A02 执行 `input tap`，随后 3 秒内轮询读屏。
- 输入：`serial`、`content`、发送前基线计数（P21）。
- 输出：`"sent"` / `"unknown"` / `"not_sent"`。
- 成功判据：P21 计数比基线增加，即聊天中出现与正文逐字相等的新消息节点（`:818-825`）。
- 失败处理：读屏失败重试 1 次后仍失败 → `unknown`（点击已发生，禁止自动重发）；输入框仍为空占位文案或超时 → `unknown`；定位不到控件属于点击前失败，返回 `not_sent`（`:779-780`）。
- 副作用与幂等性：不可幂等，会真实发消息；调用方用 `claim_resource_share` 在点击前落盘 `pending`，靠本地状态阻止重发（`batch_store.py:205-232`）。

### A20 启用 ADBKeyboard 输入法

- 目的：保证 ADBKeyboard 可用。
- 代码位置：`scripts/hr/actions.py:733-734`。
- 前置条件：已读取 `ime list -s`，且 `com.android.adbkeyboard/.AdbIME` 不在已启用列表中。
- 原子动作：单次 `adb -s <serial> shell ime enable com.android.adbkeyboard/.AdbIME`。
- 输入：`serial`。
- 输出：无。
- 成功判据：命令返回成功；后续 A21 能切换成功。
- 失败处理：异常在点击前抛出，归类为 `not_sent`（`:843-850`）。
- 副作用与幂等性：改变系统输入法启用列表；结束时若原先未启用会再禁用（`:866-870`）。

### A21 切换默认输入法到 ADBKeyboard

- 目的：让 `ADB_INPUT_B64` 广播能被接收。
- 代码位置：`scripts/hr/actions.py:735`。
- 前置条件：A20 已完成或该输入法原本已启用。
- 原子动作：单次 `adb -s <serial> shell ime set com.android.adbkeyboard/.AdbIME`。
- 输入：`serial`。
- 输出：无。
- 成功判据：命令返回成功。
- 失败处理：抛错且未点击 → `not_sent`；已点击则向上抛 `unknown`（`:843-845`）。
- 副作用与幂等性：全局输入法切换，会影响用户当前输入；结尾必须 A22 恢复。

### A22 恢复原输入法

- 目的：把系统输入法还原为操作前的默认值。
- 代码位置：`scripts/hr/actions.py:859-876`，`ime set` 在 `:862`，`ime disable` 在 `:868`。
- 前置条件：操作前已成功读取 `settings get secure default_input_method`（`:716-718`）；设备未断开。
- 原子动作：单次 `adb -s <serial> shell ime set <original_ime>`；若 ADBKeyboard 原先未启用，再执行一次 `shell ime disable com.android.adbkeyboard/.AdbIME`（`:866-870`）。
- 输入：`serial`、`original_ime`、`enabled_imes`。
- 输出：无。
- 成功判据：两条命令均返回成功。
- 失败处理：恢复失败时，若已点击发送则向上抛错（因为可能已发出），否则只记录 `input_method_restore_failed` 日志、不改变「未发送」结论（`:872-876`）。
- 副作用与幂等性：可重复执行；必须放在 `finally` 中，任何提前返回都要经过它。

---

## 3 组合流程

只列 ID 序列与步骤意图，不重复动作细节。

**C1 inspect（读当前屏卡片）**
A01 → P03 → P06（逐卡）→ 本地 `record_listing_discoveries` → 输出 JSON。意图：只读盘点当前可见职位，标注可沟通/跳过原因，不点击。

**C2 open-item（打开指定卡片）**
A01 → P03 → 序号越界检查 → P04 + P05（不完整即返回 `not_fully_visible`）→ P06（黑名单关闭）→ A06 → 最多 5 ×（等待 0.4 秒 + A01 + P07）→ 写 `pending_job.json`。意图：把「打开」和「读取」拆成一次点击 + 一组只读重试。

**C3 capture（详情入库）**
A01 → P07 → 与 `pending_job.json` 做标题/公司一致性校验 → 缺字段报错 → 本地查重（P06 口径 + 等价职位）→ `save_job` → 列表别名写入 → 完整时删除待采集文件。无设备写入动作。

**C4 confirm-identity（命令行身份核对）**
A01 → P07 → P09 → 本地 `confirm_identity`。意图：只读页面 + 本地确认位。

**C5 continue（独立命令 / 批次复用）**
本地门禁（P19 子集、黑名单、P06 口径）→ A01 → P07 → P09 → P10 → 本地 `reserve_job` + `mark_tap_started` → A10 → 等 1.0 秒 → 3 ×（A01 → P11；命中 P12 时插 A11 且只补点一次）→ 资源消息段（A16 → A17 → A20/A21 → A16 → A18 → A19 → P21）→ 本地 `complete_job`。

**C6 batch run（批次主循环）**
门禁 P19 → A01 → [卡片非空：逐卡 P06 → 选中候选] → 若卡片不完整：A03(up) 最多 2 次重试 → A06 → A01 + P07 → 本地 `capture_step` → 最多 20 次（A01 + P09，不匹配则 A03(down)）→ C5 的点击段 → A04 循环（C10）→ 回到循环。列表为空时按「P18 → C11 → A12 → 报 `job_list_not_loaded`」顺序恢复。

**C7 batch probe（只读通知探测）**
A01 → P18 → P17 → 命中即返回；未命中 A15(up) → 到底后 A15(down) → 仍无则结束。全程不点击。

**C8 recommend capture（推荐机会捕获）**
A01 → P01(=chat_detail) → P13 → A05(卡片) → A07 → 3 ×（A01 → P14）→ A05(弹层) → P15 → 写单行契约 JSONL（`open("x")`，拒绝覆盖）。任意判定失败按固定 `reason_code` 收尾，不进入点击序列。

**C9 recommend apply（推荐职位沟通）**
契约/判定结构校验 → A01 + P14 → A09 → A01 → P07 → P09 → 本地 `save_job` + `confirm_identity` + `create_opportunistic_batch` + `reserve_job` + `mark_tap_started` → A10 → 3 ×（A01 + 推荐版三证据；仅当仍是同一详情页时才重试）→ 资源消息段（A16–A22）→ 本地 `complete_job` → C10。`decision = none` 或存在活动批次时只走 A08 关闭弹层。

**C10 返回职位列表**
最多 4 轮：A01 → P03 非空即成功；P02 综合推荐页 → A04；否则读 Activity，仍属聊天/详情页 → A04；不属可回退页面 → 失败。意图：只按层级证据判定，不盲目连按返回。

**C11 打开新职位通知并等待列表**
A01 → P18 → P17 → A14 → A01 →（仍为消息列表则交回 C6）→ P16 未选中则 A13 → 最多 5 屏 / 12 秒轮询 A01，连续两次同签名或骨架屏即停。

---

## 4 数据与副作用边界

**本地数据库** `data/jobs.sqlite3`（`core/db.py:10`）：
- `jobs`：职位主记录、`dedupe_key` 唯一、`communication_status`（`not_started`/`continued`）、`application_status`（`not_applied`/`applied`/`unknown`）、`detail_complete`、`dedupe_reviewed`（`store.py:136-159`）。
- `job_discoveries`：一次扫描一次发现，`UNIQUE(job_id, run_id)`（`store.py:91-107`）。
- `job_listing_aliases`：列表标题/公司名与职位 ID 的已核对映射（`store.py:170-172`、`:457-468`）。
- `company_blacklist`：规范化公司名维度的黑名单（`store.py:173-177`）。
- `batches` / `batch_jobs`：批次目标、状态（`active`/`completed`/`closed`）、职位状态（`reserved`/`pending`/`completed`），`job_id` 全表唯一（`batch_store.py:22-47`）。
- `job_resource_shares`：每职位一行正文发送状态（`pending`/`sent`/`unknown`/`not_sent`）与 `attempts`、`resolution`（`batch_store.py:48-55`、`:205-269`）。
- `job_stage_cooldown`：单槽位冷却截止时间（`batch_store.py:56-62`、`:413-443`）。

**批次状态机与中断点**（本任务唯一需要关注的「非 UI 副作用」）：
- `reserved`（点击前占位）→ `pending`（已点、结果未核实）→ `completed`（已核实）。只有 `pending` 能结算，`reserved` 可释放，`pending` 只能由人工核对收敛（`batch_store.py:577-621`）。
- 批次 `active` 期间禁止新建批次（`batch_store.py:541-557`）；名额算 `target - completed - reserved - pending`（`:520-538`）。
- 中断分两类：安全暂停（`time_budget_reached`、`no_more_jobs`、`daily_limit_reached`、`no_new_job_notification`）会写冷却并收尾；阻断类（`needs_reconciliation`、`needs_resource_share_review`、`unconfirmed`、`needs_list`、`needs_identity_review`、`needs_detail_open_review`）不写冷却、不收尾，核对后可续跑（`batch.py:27-31`、`:92-135`）。

**文件副作用**：
- `data/pending_job.json`：单条待采集卡片 + 详情，完整入库后删除（`enroll.py:25`、`:440-441`、`:539-540`）。
- `data/job-work/recommend-<时间戳>.jsonl`：推荐机会契约，单行、拒绝覆盖（`recommend.py:13`、`:144-156`）。
- `data/operations.log` 与 `.lock`：脱敏事件日志（`core/operation_log.py:17`、`:100-156`）。
- `screenshots/<本地日期>/...`：推荐卡与弹层截图（`core/events.py:73-99`）。
- 设备侧临时文件 `/sdcard/job_tracker_ui.xml`（A01 每次覆盖）。
- SQLite 设备占用：`batch run`/`batch probe` 通过 `_inline_device` 先占设备再执行（`main.py:828-833`、`:712`、`:770`）。

**幂等性结论**：读操作（A01、P 系列）可重复；写库操作靠唯一键与状态条件收敛（`reserve_job` 唯一、`mark_tap_started` 只从 `reserved`、`complete_job` 只从 `pending`、`record_discovery` 唯一）；点击类操作（A06–A19）不可幂等，必须靠 `pending`/`sent` 状态与人工核对阻断重放。

---

## 5 未覆盖与不确定项

1. **列表上的「已沟通/已投递」徽标未被读取**。跳过判定只来自本地库（`enroll.py:351-363`），没有任何从卡片 UI 读取徽标的代码；页面上是否存在对应 resource-id 未知。
2. **卡片点击点选的是公司名区域**（`enroll.py:408` 用 `company_bounds`），代码未说明为何不点整卡；若公司名节点缺失会回退整卡 bounds（`:136-138`），实际点击热区是否等价未验证。
3. **详情页滚动依赖容器 id 猜测**。批次身份滚动复用 `scroll_step`，要求容器 resource-id 以 `recycler_view`/`rv_list` 结尾（`enroll.py:453-461`），详情页真实容器 id 未在代码中出现，也未在文档中枚举。
4. **「新职位」tab 选中态属性不固定**。代码同时尝试 `selected`、`isSelected`、`is-selected`（`batch.py:230-233`），三者都缺失时返回 `None`，此时会直接点击一次 tab，真机行为未验证。
5. **推荐弹层关闭控件顺序是经验性兜底**。`iv_close_job_panel` → `btn_confirm` → 文本「我知道了」/「知道了」（`recommend.py:273-287`），注释提到真机底部为「我知道了」，但未给出各机型差异证据。
6. **A19 依赖外部输入法 ADBKeyboard**（`com.android.adbkeyboard/.AdbIME`）。仓库内没有该 APK 或安装步骤；设备未安装时 A20/A21 会失败并归为 `not_sent`。
7. **资源消息是否发送的最终证据只有「正文节点计数增加」**（`actions.py:818-825`）。若聊天页做了节点复用或正文被折叠，计数可能不增加而消息已发出，此时按 `unknown` 处理，需人工核对。
8. **批次主循环的时限/次数是硬编码**：外层 80 轮（`batch.py:386`）、通知搜索 80 轮（`:290`）、等待列表 5 屏/12 秒（`:239-263`）、身份滚动 20 次（`:527`）、打开重试 5 次（`enroll.py:412`）、沟通确认 3 次/1 秒（`enroll.py:562-563`）。真机延迟下的合理值未在本仓库论证。
9. **`store.set_hr_status` 引用了 `jobs.hr_status` 列**（`store.py:502-505`），但 `jobs` 建表与迁移都没有该列（`store.py:136-169`）。按当前 schema 调用会报 `no such column`；是否由 HR 域另行加列未在本次只读范围内确认，属疑似不一致。
10. **推荐候选的 `index` 语义依赖列表顺序稳定**（`recommend.py:319-330`）。代码只校验同位置标题相等，不校验公司；弹层若插入新行导致整体位移，可能命中同标题不同公司的职位。
11. **`jobs recommend apply` 的 DB 连接只在「已确认 + 契约/判定合法 + `decision=pick`」时创建**（`main.py:560-564`），其余分支传 `None`；`apply` 内部对 `None` 连接只在 `none` 或批次忙分支才会安全返回，其他分支的容错边界未在代码中显式断言。
12. **本次未做任何运行验证**：没有连接设备、没有执行 Python、没有读取 `config.json`。因此所有坐标均为 `bounds` 相对值，未验证分辨率适配；每日配额、冷却时长（`JOB_STAGE_COOLDOWN_SECONDS = 3600`，`batch_store.py:12`）的实际取值也未确认。
13. **`inspect` 与 `open-item` 的分页语义**：两张命令都只处理「当前可见」卡片（`enroll.py:373-374`、`:388-392`），跨屏去重由调用方（人或批次）负责；`visited_cards` 只在批次内存在（`batch.py:356`、`:437-440`）。
