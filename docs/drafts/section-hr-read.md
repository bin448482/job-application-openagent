# hr-read 消息读取原始操作

## 0 概览

入口命令（`scripts/main.py` 中的 `hr` 子命令组，定义见 `scripts/main.py:274-363`）：

- `hr scan`：`run_hr` 的 scan 分支 `scripts/main.py:843-892`，最终调用 `hr_scan.run_phase1_scan`（`scripts/main.py:876`，`scripts/hr/scan.py:2811`）。读取一个未处理未读 HR 会话并输出 Phase 1 契约；不执行任何 HR 动作（`scripts/hr/scan.py:1902`）。
- `hr sync`：`run_hr` 的 sync 分支 `scripts/main.py:894-924`，最终调用 `hr_sync.run_read_sync`（`scripts/main.py:908`，`scripts/hr/sync.py:33`）。按最新优先核对今天/昨天的已读会话，只对新文本入站输出契约。
- `hr inbox`：`_run_inbox`（`scripts/main.py:1258-1276`）调用 `hr_inbox.build_inbox` / `show_message`（`scripts/hr/inbox.py:80`、`184`）。纯离线读库，不连接手机（`scripts/hr/inbox.py:1`）。

涉及文件（读取侧）：`scripts/hr/scan.py`、`scripts/hr/chat.py`、`scripts/hr/inbox.py`、`scripts/hr/sync.py`、`scripts/hr/list_evidence.py`、`scripts/hr/contract.py`、`scripts/hr/identity.py`、`scripts/hr/targeting.py`、`scripts/hr/policy.py`（仅下游消费，见 §5），以及被调用的 `scripts/core/message_list.py`、`scripts/core/workspace.py`、`scripts/core/adb.py`、`scripts/core/uitree.py`、`scripts/hr/store.py`。

涉及页面：

- BOSS 主界面（底部四标签，`scripts/core/workspace.py:28`）；
- 消息列表页（`:id/recyclerView`，筛选入口“筛选/取消筛选”）；
- 聊天详情页（`:id/mRecycleView`、`:id/et_msg`，`scripts/core/workspace.py:257`）；
- 白名单运营弹窗（“求职防骗提醒”/“知道了”，`scripts/core/workspace.py:36-38`）。

前置状态：

- 至少一台已授权设备，`adb` 可用（`scripts/core/adb.py:110-147`）。
- 手机已解锁，且 `isKeyguardShowing` 明确为 false（`scripts/core/adb.py:150-164`）。
- 页面必须已处于消息列表。`hr scan` / `hr sync` 自身**不**调用 `workspace.ensure_workspace`（全仓仅 `scripts/main.py:1529` 的 `workspace check` 命令调用）；不在列表时读取链路返回 `needs_list`（`scripts/hr/scan.py:1259`）。
- 数据库已建表（`scripts/main.py:861`、`900` 调用 `ensure_hr_schema`）。
- 全局无未核实动作：`hr_store.pending_actions` 非 0 时 scan 直接 `paused/pending_action`（`scripts/hr/scan.py:1989-1990`，`scripts/hr/sync.py:82-83`）。

设备独占：scan/sync 都在 `hr_scan.device_command` 上下文内执行（`scripts/main.py:862-864`、`901-902`；实现 `scripts/hr/scan.py:750-801`），内联入口会拒绝与活派发并行的设备占用（`scripts/hr/scan.py:720-747`）。

## 1 状态判定原语

| 原语 | 规则 | 代码位置 |
| --- | --- | --- |
| P1 页面分类 | 依次判弹窗→登录→`ChatRoomActivity` 聊天页→`BossJobPagerActivity` 职位页→推荐流→聊天控件→职位详情控件→聊天列表→主界面→unknown | `scripts/core/workspace.py:274`（`classify_page`），页面常量 `15-21` |
| P2 聊天列表可读态 | 唯一 `:id/recyclerView` 且至少一行有 `tv_name` 为 `named`；列表为空且“消息”选中或有空态文案为 `empty` | `scripts/core/workspace.py:169`（`_chat_list_state`），空态文案 `29` |
| P3 弹窗/登录信号 | 已知对话框类名/`content-desc`/按钮文案命中即视为弹窗；聊天正文节点被排除 | `scripts/core/workspace.py:195`（`_dialog_signal`）、`242`（`_login_signal`） |
| P4 “消息”tab 选中态 | 底部“消息”可点击容器或其后代 `selected=true/false`，缺失为 None | `scripts/core/workspace.py:150`（`message_tab_selected`） |
| P5 唯一消息列表 | `:id/recyclerView` 数量必须为 1，否则解析/滑动前置失败 | `scripts/core/message_list.py:246-248`、`scripts/hr/scan.py:1288`（`_has_unique_message_list`） |
| P6 未读（层级） | 行内 `:id/badge_view` 存在即 `yes`；否则 `unknown`；行有角标文字时记录 `unread_count` | `scripts/core/message_list.py:264-269` |
| P7 未读（像素） | 角标 bounds 内接近 (255,71,83)、每通道容差 30 的红像素 ≥ 100 判 `yes`，否则 `no` | `scripts/core/message_list.py:16-19`、`219`（`count_red_pixels`）、`270-279` |
| P8 底部消息数字角标 | “消息”入口子树内首个纯数字（去尾部“+”）即角标数；入口缺失为 None，无数字为 0 | `scripts/hr/scan.py:1460`（`_message_tab_badge`） |
| P9 HR 行 | 非 `NON_HR_NAMES`、非“发布了新职位”、无职位的直播行，且 `build_preview_hint.is_non_hr` 为假 | `scripts/hr/contract.py:38`（`is_hr_row`）、`15`（`build_preview_hint`）；入口包装 `scripts/hr/scan.py:1448` |
| P10 身份拆分/脱敏 | `"公司 | 职位"` 拆两段；手机号、微信号替换为 `[联系方式]` | `scripts/hr/identity.py:11`、`19`、`27` |
| P11 会话键 | 公司+职位均空走 `conversation_without_position` 独立键空间，否则 `core_keys.conversation_key` | `scripts/hr/store.py:325`（`conversation_key_for`）、`41`（`make_key`） |
| P12 聊天角色 | 聊天页 `:id/tv_sub_title` 含“招聘者/猎头/人事/recruiter/hr”判 `hr`，否则 `unknown` | `scripts/hr/chat.py:674`（`parse_chat` 内） |
| P13 消息方向 | 头像左右、我方发送状态、唯一气泡左右边界三路证据；证据冲突或缺失为 `unknown` | `scripts/hr/chat.py:114`（`_alignment_direction`）、`129`（`_avatar_direction`）、`166`（`_has_status`） |
| P14 读全 | 唯一 `:id/mRecycleView` 且 `scrollable=false`，或连续两次滑动无变化；`read_complete = read_complete and parse_complete` | `scripts/hr/chat.py:760`（`_chat_list_scrollable`）、`800-819`、`828` |
| P15 时间窗口 | `刚刚/昨天` 直接归类；其余按日期格式解析，仅今天/昨天进入同步窗口，星期标签仅两天前可信 | `scripts/hr/list_evidence.py:151`（`classify_list_time`）、`87`（`infer_weekday_date`） |
| P16 预览触发 | 去掉 `[已读]/[送达]`、折叠空白、去省略号后，正文与基线不同才返回 `preview_changed` | `scripts/hr/list_evidence.py:237`（`read_trigger`） |
| P17 阻塞 | 存在 pending/unknown/failed/needs_probe 的最新动作，或未关闭入站发现 | `scripts/hr/store.py:2019`（`pending_actions`）、`2028`（`has_unverified_actions`）、`scripts/hr/inbox.py:57` |
| P18 前台安全三态 | 未解锁或前台非 BOSS → `unsafe_focus`；BOSS 且 `ChatRoomActivity` → `safe_chat`；其余/不可解析 → `fallback` | `scripts/hr/scan.py:1322`（`_boss_chat_focus`）、`scripts/core/adb.py:150`（`window_state`） |

## 2 原子操作

### R01 设备授权与唯一性判定
- 目的：确认后续 ADB 命令指向唯一已授权设备。
- 代码位置：`scripts/core/adb.py:110`（`select_device`）；调用点 `scripts/main.py:869`、`scripts/main.py:904`。
- 前置条件：`adb` 可执行。
- 原子动作：执行一次 `adb devices` 并按状态行归类，不写设备、不点击。
- 输入：可选 `--serial`。
- 输出：设备序列号字符串。
- 成功判据：状态为 `device`；指定序列号存在，或未指定时恰好一台。
- 失败处理：未授权抛 `DeviceDisconnected("device_unauthorized")`，offline/缺失抛 `device_disconnected`，多台抛 `RuntimeError`；不重试。
- 副作用与幂等性：只读，幂等。

### R02 前台组件与锁屏读取
- 目的：一次 `dumpsys window` 同时取前台包名/Activity 与锁屏证据。
- 代码位置：`scripts/core/adb.py:150`（`window_state`）。
- 前置条件：R01 已选中设备。
- 原子动作：执行一次 `adb shell dumpsys window`，正则解析 `mCurrentFocus` 与 `isKeyguardShowing`。
- 输入：serial。
- 输出：`((package, activity) | None, unlocked: bool)`。
- 成功判据：`unlocked` 仅当所有 `isKeyguardShowing` 值都存在且全为 `false`。
- 失败处理：解析不到焦点行返回 `component=None`，由调用方判定；ADB 失败经 `raise_if_disconnected` 决定是否终止（`scripts/core/adb.py:39`）。
- 副作用与幂等性：只读，幂等。

### R03 恢复前前台安全三态判定
- 目的：判断能否安全发送返回键/点击以回到列表。
- 代码位置：`scripts/hr/scan.py:1322`（`_boss_chat_focus`）。
- 前置条件：刚读完同一聊天（调用方传 `chat_read_complete=True`）。
- 原子动作：调用 R02 一次，按包名与 Activity 归类。
- 输入：serial。
- 输出：`safe_chat` / `unsafe_focus` / `fallback`。
- 成功判据：三态之一；设备断连原样抛出。
- 失败处理：`unsafe_focus` 时调用方不得发送任何按键或点击并返回 None（`scripts/hr/scan.py:1364-1365`）；`fallback` 走读屏分类。
- 副作用与幂等性：只读，幂等。

### R04 页面类型判定
- 目的：把当前层级归到已知页面类型，决定后续读取路径。
- 代码位置：`scripts/core/workspace.py:274`（`classify_page`）。
- 前置条件：持有当前 `read_hierarchy` 结果。
- 原子动作：纯内存判定，不发设备命令。
- 输入：`root`、可选 `activity`。
- 输出：`Page(kind, reason, target)`。
- 成功判据：`kind` 为 `PAGE_*` 之一；无法识别为 `PAGE_UNKNOWN`。
- 失败处理：不抛异常；调用方按 `PAGE_CHAT_LIST/PAGE_UNKNOWN` 放行或按 `needs_list` 暂停（`scripts/hr/scan.py:1257-1259`）。
- 副作用与幂等性：只读，幂等。

### R05 聊天列表可读态判定
- 目的：区分“有具名会话行”和“可确认空列表”，避免把空容器当列表。
- 代码位置：`scripts/core/workspace.py:169`（`_chat_list_state`）；由 R04 内部调用。
- 前置条件：R10 唯一列表成立。
- 原子动作：纯内存解析行文本，不发设备命令。
- 输入：root。
- 输出：`named` / `empty` / None。
- 成功判据：至少一行有姓名，或列表零子节点且“消息”选中/存在空态文案。
- 失败处理：返回 None，交由 R04 继续判主界面/unknown。
- 副作用与幂等性：只读，幂等。

### R06 已知白名单弹窗关闭
- 目的：关闭会遮挡列表/聊天的已知运营弹窗。
- 代码位置：`scripts/core/workspace.py:213`（`dismiss_known_dialog`）；白名单 `36-38`。
- 前置条件：R04 已发现弹窗信号（判定见 `scripts/core/workspace.py:195`）。
- 原子动作：调用 core 设备原语 `dev.tap_bounds`（`scripts/core/adb.py:249`，坐标取“知道了”可点击祖先的 `ui.get_bounds`），只点一次。
- 输入：root、serial。
- 输出：是否已点击。
- 成功判据：标题与按钮文本同时存在且唯一可点击祖先存在，点击命令成功返回。
- 失败处理：不匹配/无祖先/点击异常返回 False 且不点击；调用方最多重试 `_DISMISS_DIALOG_LIMIT=2` 次（`scripts/hr/scan.py:42`、`1249`）。
- 副作用与幂等性：一次点击；重复调用在弹窗消失后返回 False，可重入。

### R07 底部“消息”入口定位
- 目的：唯一确定底部导航“消息”标签的可点击容器与数字角标位置。
- 代码位置：`scripts/core/workspace.py:145`（`locate_message_target`）→ `113`（`locate_bottom_tab_target`）。
- 前置条件：能唯一确定屏幕 bounds（`_screen_bounds`，`scripts/core/workspace.py:65`）。
- 原子动作：纯内存定位，按屏幕高度 70% 以下筛选标签。
- 输入：root。
- 输出：`(target | None, reason)`。
- 成功判据：标签文本唯一且其可点击祖先唯一。
- 失败处理：缺失返回 `message_target_missing`，歧义返回 `message_target_ambiguous`，不猜。
- 副作用与幂等性：只读，幂等。

### R08 点击“消息”入口
- 目的：从主界面进入消息列表（仅在恢复/导航路径使用）。
- 代码位置：`scripts/core/workspace.py:404-415`（`_navigate_from_main` 内点击）；入口 `373`。
- 前置条件：R04 判为主界面且 R07 定位成功；点击前会重新读屏复核页面未变（`379-395`）。
- 原子动作：调用 core 设备原语 `dev.tap_bounds`，坐标取 R07 容器 bounds 中心。
- 输入：serial、target。
- 输出：点击是否发出。
- 成功判据：ADB 点击命令成功返回。
- 失败处理：读屏失败返回 `read_incomplete`，页面变化返回 `page_changed`，无 target 返回 `message_target_missing`；不重试点击。
- 副作用与幂等性：一次点击；非幂等，调用方需随后执行 R09。

### R09 导航结果核验
- 目的：确认已进入聊天列表且“消息”处于选中态。
- 代码位置：`scripts/core/workspace.py:418-444`（`_navigate_from_main` 轮询段）。
- 前置条件：R08 已点击。
- 原子动作：循环“等待 0.5s + 读屏 + 分类”，最多 8 次（`_POST_CLICK_WAIT_ATTEMPTS`，`40`）。
- 输入：serial。
- 输出：`reached` 或 `navigation_failed(reason)`。
- 成功判据：`classify_page` 为 `PAGE_CHAT_LIST` 且 R07/`message_tab_selected` 为 True（`431-435`）。
- 失败处理：选中为 False 记 `message_tab_unselected`，未知记 `message_tab_selection_missing`；进入详情/职位/弹窗/登录直接 `unrecognized`；耗尽返回 `navigation_failed`。
- 副作用与幂等性：只读轮询；不重复点击。

### R10 消息列表唯一性判定
- 目的：在解析或滑动前确认页面只有一个消息列表容器。
- 代码位置：`scripts/core/message_list.py:246-248`（`parse_message_list` 前置）、`scripts/hr/scan.py:1288`（`_has_unique_message_list`）。
- 前置条件：R04 已判为列表或 unknown。
- 原子动作：纯内存统计 `:id/recyclerView` 节点数。
- 输入：root。
- 输出：布尔值。
- 成功判据：数量恰为 1。
- 失败处理：解析抛 `RuntimeError("无法唯一识别消息列表")`，scan 映射为 `paused/needs_list`（`scripts/hr/scan.py:2218-2220`）。
- 副作用与幂等性：只读，幂等。

### R11 列表行结构化解析
- 目的：把可见会话行解析为姓名/职位/预览/时间/未读/类型的扁平结构。
- 代码位置：`scripts/core/message_list.py:239`（`parse_message_list`）；HR 侧包装 `scripts/hr/scan.py:1948`（`parse_unread_list`）。
- 前置条件：R10 成立。
- 原子动作：纯内存解析；若传入像素则由 R13 判定未读。
- 输入：root、`unread_filter_verified`、可选 `(width, height, pixels)`。
- 输出：行字典列表（`name/position/message/time/unread/unread_count/kind/preview_event`）。
- 成功判据：函数正常返回；无姓名的可点击行输出 `kind=unknown` 占位（`250-257`）。
- 失败处理：列表不唯一抛 `RuntimeError`；像素长度不符抛 `ValueError`；scan 分别映射 `needs_list` / `read_incomplete`（`scripts/hr/scan.py:2218-2223`）。
- 副作用与幂等性：只读，幂等。

### R12 未读角标层级判定
- 目的：无像素时用层级证据给出 `yes/unknown`。
- 代码位置：`scripts/core/message_list.py:264-269`。
- 前置条件：已取到行内 `:id/badge_view`。
- 原子动作：纯内存读取角标存在性与数字文字。
- 输入：行节点。
- 输出：`unread`、`unread_count`。
- 成功判据：`unread_filter_verified=true` 或角标存在时判 `yes`。
- 失败处理：无角标一律 `unknown`，**不**当作已读。
- 副作用与幂等性：只读，幂等。

### R13 未读像素判定
- 目的：用红像素阈值替代不可靠的角标层级。
- 代码位置：`scripts/core/message_list.py:270-279` 调用 `219`（`count_red_pixels`）；阈值 `19`。
- 前置条件：R14 已取得同视口 RGBA 像素，且行内角标 bounds 可解析。
- 原子动作：纯内存统计角标矩形内红色像素。
- 输入：pixels、width、badge bounds。
- 输出：`yes` / `no` / `unknown`（角标或 bounds 缺失）。
- 成功判据：红像素数 ≥ `UNREAD_RED_PIXEL_THRESHOLD=100` 判 `yes`，否则 `no`。
- 失败处理：bounds 解析失败保留 `unknown`（`275-276`）；像素长度不符抛 `ValueError`。
- 副作用与幂等性：只读，幂等。

### R14 视口原始像素采集
- 目的：为一个视口取一次原始截图，供 R13 复用。
- 代码位置：`scripts/core/adb.py:276`（`read_screen_raw`）；缓存与回退 `scripts/hr/scan.py:1948-1962`。
- 前置条件：当前 root 尚未采过像素。
- 原子动作：调用 core 设备原语 `adb exec-out screencap`，解析 16 字节头。
- 输入：serial。
- 输出：`(width, height, RGBA bytes)`。
- 成功判据：头长度 ≥16、宽高非 0、格式码 1、像素长度匹配。
- 失败处理：失败仅记 `progress/read_incomplete` 并回退纯层级解析（`scripts/hr/scan.py:1954-1960`），不终止命令。
- 副作用与幂等性：只读；按 root 对象缓存，同一视口只采一次。

### R15 底部消息数字角标读取
- 目的：行级角标无法判定时用底部入口角标兜底，防止误报“无未读”。
- 代码位置：`scripts/hr/scan.py:1460`（`_message_tab_badge`）；兜底判定 `scripts/hr/scan.py:1964-1979`。
- 前置条件：存在 `unknown` 未读行（`_unread_unknown_keys`，`1472`）。
- 原子动作：纯内存读取 R07 目标子树文本。
- 输入：root。
- 输出：角标整数 / 0 / None。
- 成功判据：入口存在则可返回数值或 0。
- 失败处理：入口缺失返回 None → 记 `badge_unverified` 并继续（不判完成）；角标 >0 → 返回 `paused/unread_remains`。
- 副作用与幂等性：只读，幂等。

### R16 HR 行判定
- 目的：把系统通知、直播行、非 HR 会话排除出候选。
- 代码位置：`scripts/hr/contract.py:38`（`is_hr_row`）；调用 `scripts/hr/scan.py:2269`。
- 前置条件：R11 已产出完整行字段。
- 原子动作：纯内存文本判定。
- 输入：行字典。
- 输出：布尔值。
- 成功判据：非 `NON_HR_NAMES`、非“发布了新职位”、非无职位直播行。
- 失败处理：判否时仅记 `candidate_skipped/non_hr` 并继续（`scripts/hr/scan.py:2269-2282`）。
- 副作用与幂等性：只读，幂等。

### R17 列表时间窗口分类
- 目的：判断该行是否落在今天/昨天的同步窗口内。
- 代码位置：`scripts/hr/list_evidence.py:151`（`classify_list_time`），解析 `105`（`parse_list_time`）。
- 前置条件：行有 `time` 文本。
- 原子动作：纯内存时间解析，`刚刚/昨天` 特判。
- 输入：时间文本、可选 `at`。
- 输出：`today` / `yesterday` / `older` / `unknown`。
- 成功判据：解析出的日期与本地自然日差 0 或 1。
- 失败处理：越界/未来/无法解析返回 `unknown`，调用方按 `out_of_window` 跳过（`scripts/hr/scan.py:2366-2367`）。
- 副作用与幂等性：只读，幂等。

### R18 列表预览变化触发判定
- 目的：用列表预览与基线的实质差异决定是否值得打开聊天。
- 代码位置：`scripts/hr/list_evidence.py:237`（`read_trigger`）；调用 `scripts/hr/scan.py:1739`、`2370`。
- 前置条件：已有非空基线预览/时间（`hr_store.sweep_last_preview` / `sweep_last_list_time`）。
- 原子动作：纯内存归一化比较。
- 输入：行字典、基线预览、基线时间、待核实外发标志。
- 输出：`preview_changed` 或空串。
- 成功判据：去标记、去省略号后的正文非空且与基线不同。
- 失败处理：返回空串时只落未核实列表观测，不打开聊天（`scripts/hr/scan.py:2376-2394`）。
- 副作用与幂等性：只读判定，幂等。

### R19 列表滑动一次
- 目的：在唯一消息列表内滑动一屏。
- 代码位置：`scripts/core/message_list.py:328`（`scroll_messages`）。
- 前置条件：R10 成立；列表 `scrollable=true`（否则直接返回未变）。
- 原子动作：调用 core 设备原语 `adb shell input swipe`（`scripts/core/adb.py:265` 同语法），x 取列表中线，y 取列表高度 25%/68% 两点，时长 450ms；默认路径用截图门控（`screen_action`），`hierarchy_only` 路径用滑动前后层级签名比较。
- 输入：serial、root、`up|down`、`hierarchy_only`。
- 输出：`(changed: bool, new_root)`。
- 成功判据：滑动后层级与滑动前不同即 `changed=True`；`changed=False` 表示到达端点。
- 失败处理：`hierarchy_only` 下归顶后页面不可识别抛 `RuntimeError`；scan 侧 `_scroll_list` 把读屏异常转 `(None, None)` 并暂停 `read_incomplete`（`scripts/hr/scan.py:1273-1280`）。
- 副作用与幂等性：改变物理视口；重复滑动非幂等，端点处幂等返回未变。

### R20 列表归顶证明
- 目的：证明当前视口是最新首屏，而不是深列表中间页。
- 代码位置：`scripts/hr/scan.py:1294`（`_seek_list_top`），包装 `1309`（`_rewind_read_list`），上限 `1285`（`SEEK_TOP_LIMIT=30`）。
- 前置条件：R19 的 `hierarchy_only` 路径可用。
- 原子动作：循环 R19 `down`，每次比较是否仍变化。
- 输入：serial、root。
- 输出：`(尝试次数, root | None, proven: bool)`。
- 成功判据：出现一次 `changed=False`；否则到 30 次仍未证明。
- 失败处理：未证明时 root 返回 None，调用方 `paused/read_incomplete`（`scripts/hr/scan.py:2201-2202`）。
- 副作用与幂等性：把列表滚到最新端；成功后重复调用返回 `count=1` 且幂等。

### R21 未读筛选设置
- 目的：把列表收敛为“未读”筛选，缩小候选面。
- 代码位置：`scripts/core/message_list.py:125`（`set_unread_filter`）；打开面板 `89`（`_open_filter_panel`），点击 `65`（`_filter_action`）/`45`（`_click_unique`）。
- 前置条件：页面存在“筛选”入口且当前无“取消筛选”；面板中无其他职位/薪资条件（`136-141`）。
- 原子动作：3 次独立点击序列（“筛选”→“未读”→“确定”），每次用 `dev.screen_action` 前后截图哈希确认画面变化，共用一个坐标来源 `ui.get_bounds(可点击祖先)`。
- 输入：serial、可选 root。
- 输出：应用筛选后的新层级。
- 成功判据：“取消筛选”出现、“确定”消失，且 `parse_message_list(unread_filter_verified=True)` 成功（`173-181`）。
- 失败处理：`FilterEntryAbsent` 由调用方降级为角标兜底（`scripts/hr/scan.py:2166-2184`）；`FilterActionUnchanged` 直接 `paused/filter_unverified`（`2135-2136`）；其余 RuntimeError 最多重试 2 轮（`2097-2142`）。
- 副作用与幂等性：改变列表筛选状态并重排列表；成功后不可重复点击（入口已变为“取消筛选”）。

### R22 筛选清除
- 目的：退出未读筛选，恢复完整列表以做最终审计。
- 代码位置：`scripts/core/message_list.py:187`（`clear_filter_from_root`），便捷入口 `204`（`clear_unread_filter`）。
- 前置条件：存在“取消筛选”可点击节点。
- 原子动作：一次 `dev.screen_action` + `dev.tap_bounds` 点击“取消筛选”。
- 输入：serial、root、可选 cancel 节点。
- 输出：`(cleared: bool, new_root)`。
- 成功判据：重读后 `ui.clickable_text(root, "取消筛选") is None`。
- 失败处理：无取消入口视为已清除返回 True；点击未生效返回 False → `paused/filter_clear_unverified`（`scripts/hr/scan.py:2429-2434` 等）。
- 副作用与幂等性：可重入，已清除时幂等返回 True。

### R23 页面截图取证
- 目的：为列表页/推荐卡保留可核对的截图证据。
- 代码位置：调用点 `scripts/hr/scan.py:2211`、`2418`、`2026`（`monitor.capture_evidence`）；设备原语 `scripts/core/adb.py:355`（`capture_screenshot`）。
- 前置条件：视口页码未取证过（`sweep["captured"]`）。
- 原子动作：调用 core 设备原语 `adb exec-out screencap -p` 写入本机文件。
- 输入：标签文本（如 `list-page1`）。
- 输出：证据路径写入事件 `progress/list_screenshot`。
- 成功判据：文件写入成功且事件带 `screenshot` 字段。
- 失败处理：异常向上抛出由外层按命令级错误处理；页码不重复取证（`2085-2089`）。
- 副作用与幂等性：新增本机截图文件；按页码去重。

### R24 列表观测基线落库
- 目的：保存列表预览/时间作为下次 R18 的比较基线，与聊天核验证据分离。
- 代码位置：`scripts/hr/store.py:929`（`observe_sweep_preview`）；调用 `scripts/hr/scan.py:1162`（`_observe_verified_conversation`，`provisional` 决定写 verified 还是 UNVERIFIED 字段）。
- 前置条件：已由 R25 得到会话键。
- 原子动作：单事务 `INSERT ... ON CONFLICT DO NOTHING` + `UPDATE hr_conversations`。
- 输入：conversation_key、preview、list_time、contact、verified。
- 输出：布尔值（是否命中一行）。
- 成功判据：`rowcount == 1`。
- 失败处理：身份与会话键不一致抛 `ValueError`；失败记脱敏事件 `conversation_snapshot_failed` 并返回 False → `snapshot_update_failed`（`scripts/hr/scan.py:1167-1173`）。
- 副作用与幂等性：写库；同一输入重复写结果稳定（覆盖同类字段）。

### R25 会话身份派生
- 目的：由列表行派生稳定 `message_key` 与 `conversation_key`，供去重与落库。
- 代码位置：`scripts/hr/scan.py:1425`（`message_keys`）；会话键 `scripts/hr/store.py:325`，联系人拆分 `scripts/hr/identity.py:27`。
- 前置条件：行有姓名、时间、预览（缺一即拒绝）。
- 原子动作：纯内存派生；必要时只读查询别名表 `resolve_conversation_alias`（`scripts/hr/store.py:335`）。
- 输入：行字典、可选 connection。
- 输出：`(message_key, conversation_key)`。
- 成功判据：两键均为合法 64 位十六进制。
- 失败处理：缺字段抛 `RuntimeError`；同名空身份会话不唯一也抛错，scan 记 `identity_unavailable` 并跳过（`scripts/hr/scan.py:2287-2296`）。
- 副作用与幂等性：可能登记一次会话别名（`register_conversation_alias`，`scripts/hr/store.py:344`）；同输入幂等。

### R26 会话行唯一匹配
- 目的：把数据库会话身份映射回当前列表行，避免点错人。
- 代码位置：`scripts/hr/identity.py:34`（`conversation_row_matches`）；调用 `scripts/hr/scan.py:1709`、`1720`。
- 前置条件：已定位列表 root 且 R11 解析成功。
- 原子动作：纯内存按联系人+职位匹配，公司名允许 `...` 截断前缀匹配。
- 输入：行列表、会话字典。
- 输出：匹配行列表。
- 成功判据：恰好 1 行。
- 失败处理：0 行 → `conversation_not_found`；>1 行 → `conversation_ambiguous`（`scripts/hr/scan.py:1681-1684`、`1730-1733`）。
- 副作用与幂等性：只读，幂等。

### R27 发现记录重开
- 目的：打开会话前先持久化“本条待处理”，保证崩溃后可核对。
- 代码位置：`scripts/hr/store.py:1951`（`reopen_discovery`）；调用 `scripts/hr/scan.py:2612`、`1880`。
- 前置条件：`message_key`/`conversation_key` 已派生。
- 原子动作：新建、复用 pending 或把 resolved 改回 pending 的单事务写。
- 输入：message_key、conversation_key。
- 输出：布尔值。
- 成功判据：写入或状态迁移成功。
- 失败处理：返回 False → `discovery_record_failed`（`scripts/hr/scan.py:2613`）。
- 副作用与幂等性：写 `hr_discoveries`；重复调用幂等。

### R28 消息行唯一定位
- 目的：在屏幕上唯一确定要点击的会话行容器。
- 代码位置：`scripts/hr/chat.py:893`（`_row_container`），主路径 `928-942`（`open_message` 内重匹配）。
- 前置条件：R26 已确认行存在。
- 原子动作：纯内存匹配；先按全字段匹配，失败再按姓名+职位（必要时加预览和时间）收窄。
- 输入：root、目标 item、`match_fields`、可选 rows。
- 输出：可点击容器或 None。
- 成功判据：匹配数与候选节点数均为 1。
- 失败处理：不唯一抛 `UiUnverified("chat_open_unverified")`，证据含期望联系人摘要与标题摘要（`873-882`）。
- 副作用与幂等性：只读，幂等。

### R29 点击消息行进入会话
- 目的：打开指定会话聊天页。
- 代码位置：`scripts/hr/chat.py:946`（`open_message` 内 `dev.screen_action` + `dev.tap_bounds`）；坐标来源 `943-945`（优先 `:id/tv_msg` 节点 bounds，否则容器 bounds）。
- 前置条件：R28 已定位唯一容器。
- 原子动作：调用 core 设备原语点击一次（`scripts/core/adb.py:334` 的 `screen_action` 内含前后截图哈希门控与最多 2 次点击）。
- 输入：serial、target bounds。
- 输出：`changed: bool | None`。
- 成功判据：`changed` 不为 False（画面变化或无法取得截图）。
- 失败处理：`changed is False` 抛 `chat_open_unverified`（`947-948`）。
- 副作用与幂等性：非幂等；重复点击可能进入更深页面，故调用方只在未打开时执行。

### R30 打开后联系人核验
- 目的：确认聊天页标题与目标 HR 一致，防止对错误联系人读取/操作。
- 代码位置：`scripts/hr/chat.py:952-960`（`open_message` 重读后比较 `parse_chat(root)["contact"] == item["name"]`）。
- 前置条件：R29 已点击。
- 原子动作：读屏一次 + 纯内存比较，最多 3 次（`changed is None` 时每次 sleep 后重读）。
- 输入：serial、期望姓名。
- 输出：已核验的聊天页 root。
- 成功判据：`contact` 与期望姓名完全相等。
- 失败处理：3 次不符抛 `UiUnverified("contact_mismatch")`，附 `matching_rows` 与标题摘要（`873-882`）。
- 副作用与幂等性：只读核验；幂等。
- 备注：`verify_contact`（`scripts/hr/chat.py:885`）是同类判定，但只被 `hr/actions.py`、`hr/apply.py`、`hr/review.py`、`jobs/enroll.py` 调用，读取侧 `scan.py`/`sync.py` 不调用（grep 证据见 §5）。

### R31 打开后弹窗关闭并重读
- 目的：聊天页可能弹出白名单弹窗，关闭后重新取层级。
- 代码位置：`scripts/hr/scan.py:1224`（`_dismiss_popup_after_open`）。
- 前置条件：R30 已拿到聊天页 root。
- 原子动作：调用 R06 一次；若点击成功再 `dev.read_hierarchy` 一次。
- 输入：serial、chat_root。
- 输出：新的（或原样的）聊天页 root。
- 成功判据：返回可解析层级；R06 返回 False 时原样返回。
- 失败处理：弹窗关闭或重读失败一律返回原 root，不终止（`1229-1236`），后续 R32 解析失败再判定。
- 副作用与幂等性：最多一次点击；弹窗消失后调用幂等。

### R32 聊天页身份与角色解析
- 目的：取出聊天页标题、角色标签、是否 HR，并准备消息区解析。
- 代码位置：`scripts/hr/chat.py:665`（`parse_chat`），标题 `667`、角色 `668`、角色判定 `674`。
- 前置条件：当前 root 是聊天页。
- 原子动作：纯内存解析，同时计算唯一 `:id/mRecycleView` 是否成立。
- 输入：root。
- 输出：`contact/role/kind/messages/latest_*/parse_complete`。
- 成功判据：函数返回；`parse_complete` 表示是否唯一列表（`671`）。
- 失败处理：`parse_complete=False` 时走 `_legacy_messages` 兜底（`672`、`427`），不抛异常。
- 副作用与幂等性：只读，幂等。

### R33 单条聊天行分类
- 目的：把每个聊天行归类为文本/媒体/简历卡/卡片/系统提示/时间/未知，并给出方向。
- 代码位置：`scripts/hr/chat.py:260`（`_classify_group`），方向证据 `114`/`129`，整行文字摘要 `239`。
- 前置条件：R32 已解析出直属消息行。
- 原子动作：纯内存结构分类。
- 输入：行节点、屏幕宽度。
- 输出：`kind/text/token/direction/可选 job_card/text_digest`。
- 成功判据：能归类到已知 kind；证据冲突判 `unknown`。
- 失败处理：无法证明时返回 `unknown` 并带 `text_digest`；零高度占位行返回 None 被丢弃（`345-348`）。
- 副作用与幂等性：只读，幂等。

### R34 非文本与动作证据提取
- 目的：处理图片/地图/简历卡/职位卡/系统提示，并抽取换微信、换电话、发简历的页面证据。
- 代码位置：`scripts/hr/chat.py:299-348`（media/resume_card/card/system_tip 分支）、`361`（`extract_action_evidence`）、职位卡字段 `211`。
- 前置条件：R33 已给出 kind/direction。
- 原子动作：纯内存文本与资源 ID 匹配。
- 输入：行节点、分类结果。
- 输出：`action_evidence{wechat,phone,resume}`、`job_card{title,salary,...}`。
- 成功判据：命中已知控件文案或结构字段。
- 失败处理：未知句式不猜，整行降级 `unknown`（`340-343`）。
- 副作用与幂等性：只读，幂等。

### R35 跨屏行身份判定
- 目的：为相邻屏幕拼接提供可安全比对的重复行身份。
- 代码位置：`scripts/hr/chat.py:718`（`_message_identity`）、`725`（`_repeat_safe`）。
- 前置条件：R33 已产出方向/文本/时间/kind/content_hash。
- 原子动作：纯内存元组比较。
- 输入：消息字典。
- 输出：五元身份元组、是否可安全去重。
- 成功判据：有文字、时间、token、文本摘要或非空职位卡字段之一。
- 失败处理：纯媒体/空卡返回不可安全比对，拼接时保守保留。
- 副作用与幂等性：只读，幂等。

### R36 聊天向上滑动一次
- 目的：在唯一聊天消息列表内向上翻页读历史。
- 代码位置：`scripts/hr/chat.py:687`（`scroll_chat`）。
- 前置条件：R32 判定唯一 `:id/mRecycleView` 且 `scrollable=true`。
- 原子动作：调用 core 设备原语 `adb shell input swipe`，x 取消息区中线，y 取 68%→25%（up），450ms，随后 sleep 0.4s 并重读层级。
- 输入：serial、root、`up|down`、可选计数器。
- 输出：`(视口是否变化, 新 root)`。
- 成功判据：滑动前后 `_chat_view_signature`（文本+坐标）不同。
- 失败处理：方向非法抛 `ValueError`；列表不唯一抛 `RuntimeError`；读屏失败经 `raise_if_disconnected` 决定终止或记 `read_chat` 证据（`scripts/hr/chat.py:794-799`）。
- 副作用与幂等性：改变聊天视口；端点处返回未变化。

### R37 多屏拼接去重
- 目的：把多次滑动读到的屏幕按时间正序拼成一条消息序列。
- 代码位置：`scripts/hr/chat.py:739`（`_merge_chat_screens`）。
- 前置条件：已采集 ≥1 屏（R32/R33）。
- 原子动作：纯内存拼接，只移除相邻屏幕边界上的连续重叠段。
- 输入：按读取顺序的屏幕列表。
- 输出：合并后的消息列表。
- 成功判据：完全重复的屏幕被跳过；重叠前缀被裁剪。
- 失败处理：无可信身份的重复行不裁剪，宁可重复不丢。
- 副作用与幂等性：只读，纯函数。

### R38 读全完成判定
- 目的：给出 `read_complete` 与 `parse_complete`，决定是否允许后续判定。
- 代码位置：`scripts/hr/chat.py:767`（`read_full_chat`），完成条件 `800-819`，返回 `828`；可滚动判定 `760`。
- 前置条件：R36/R37 已执行。
- 原子动作：纯内存判定（滑动本身由 R36 完成）。
- 输入：屏幕集合、`max_scrolls`、`max_screen_reads`。
- 输出：`read_complete/scroll_count/screen_reads/budget_exceeded/parse_complete`。
- 成功判据：到达不可滚动端点，或连续两次无变化；且每屏 `parse_complete` 为真。
- 失败处理：未完成返回 `read_complete=False`，调用方 `paused/read_incomplete`（`scripts/hr/scan.py:2635-2637`）；读屏预算触发 `on_budget_exceeded` 回调（`801-805`）。
- 副作用与幂等性：只读判定；重复调用会重新滑动，非幂等。

### R39 最新入站汇总判定
- 目的：确定最近一条可确认文本入站，并显式标记非文本/方向未知。
- 代码位置：`scripts/hr/chat.py:477`（`_latest_summary`），导出 `496`（`summarize_latest`），最近消息 `469`（`_latest_message`）。
- 前置条件：R37 已完成合并。
- 原子动作：纯内存扫描。
- 输入：消息列表。
- 输出：`(latest_inbound | None, known|non_text|unknown, kind)`。
- 成功判据：最后一条入站为文本且其后无 unknown 行时状态为 `known`。
- 失败处理：最后入站为非文本返回 `non_text`；存在未知行返回 `unknown`；调用方据此暂停（`scripts/hr/scan.py:1847-1850`）。
- 副作用与幂等性：只读，幂等。

### R40 契约前方向门禁
- 目的：在产出契约前拒绝方向不符或含未知行的聊天。
- 代码位置：`scripts/hr/scan.py:1808-1826`（`_run_conversation_scan`），单候选路径 `2678-2707`。
- 前置条件：R38 通过且 R39 已给出方向。
- 原子动作：纯内存分支判定。
- 输入：`chat`、`item`。
- 输出：`record_emitted` 继续，或 `latest_message_not_incoming` / `latest_inbound_not_text` / `read_incomplete`。
- 成功判据：最新非系统消息为 `incoming` 且 `latest_inbound_status == known`。
- 失败处理：`unknown` 方向直接 `paused`；存在任意 unknown 行也 `paused/read_incomplete`（`1822-1825`）。
- 副作用与幂等性：只读判定，幂等。

### R41 非文本入站落库
- 目的：把“读全但最新入站不是文本”的会话留给人工核对。
- 代码位置：`scripts/hr/store.py:2268`（`record_non_text_pending`）；调用 `scripts/hr/scan.py:1812`、`1632`。
- 前置条件：R40 判 `latest_inbound_not_text` 或列表预览为联系方式卡片。
- 原子动作：单条 `INSERT OR REPLACE INTO hr_non_text_pending` 并提交。
- 输入：conversation_key、列表预览、列表时间。
- 输出：无返回值。
- 成功判据：行写入成功。
- 失败处理：键非法抛 `ValueError`；异常向上由命令级处理。
- 副作用与幂等性：同会话覆盖写，幂等。

### R42 推荐卡机会判定
- 目的：识别聊天内完整“推荐职位”卡片，把会话交回机会流程。
- 代码位置：`scripts/hr/scan.py:1998`（`observe_recommend_card`）。
- 前置条件：R37 已合并消息，且本轮未见过该会话。
- 原子动作：纯内存判断 `:id/ll_recommend_job_container` 与 `:id/tv_recommend_job_name` 同时出现在同一行资源 ID 中。
- 输入：chat、conversation_key。
- 输出：是否已接管（True 时调用方 `paused/recommend_opportunity`）。
- 成功判据：两张卡资源 ID 同现且会话未在 `recommend_seen`。
- 失败处理：`skip_recommend_opportunities` 时只记脱敏跳过事件并继续普通判断（`2007-2014`）。
- 副作用与幂等性：写轮次状态 `recommend_seen`、撤销该会话未读计数、截一张证据图；同一会话只触发一次。

### R43 非目标岗位判定
- 目的：识别明显非目标（保险/纯销售等）会话，避免进入契约流程。
- 代码位置：`scripts/hr/targeting.py:42`（`judge_non_target`）。
- 前置条件：R32/R34 已产出职位卡与最近入站文本。
- 原子动作：纯内存词表匹配，可选只读查询公司黑名单。
- 输入：item、chat、可选 connection。
- 输出：`{matched, reason, terms}`。
- 成功判据：命中任一强特征，或两类以上弱特征。
- 失败处理：技术岗优先豁免（`56-57`）；证据不足返回 `matched=False`。
- 副作用与幂等性：只读，幂等。

### R44 未读阶段轮次计数落库
- 目的：在打开候选前把会话记入本轮 `seen/unread_pending/unread_done/unread_attempted`，支持崩溃恢复。
- 代码位置：`scripts/hr/scan.py:2544-2554`；状态写 `hr_store.sweep_state`（`scripts/hr/store.py:1191`）。
- 前置条件：已选出 candidate（`2544`）。
- 原子动作：单事务写轮次 JSON 状态。
- 输入：conversation_key、行未读字段。
- 输出：持久状态更新。
- 成功判据：写入成功。
- 失败处理：异常向上由命令级处理；`unread_pending` 用于下次恢复（`2040-2049`）。
- 副作用与幂等性：写库；同一会话重复记录不会重复追加列表元素。

### R45 未读失败回退计数
- 目的：候选失败时撤销本轮未读已完成计数，避免把失败当成功。
- 代码位置：`scripts/hr/scan.py:1521`（`_forget_unread_handled`）；调用 `2556-2560`（`abort_candidate`）、`2592`。
- 前置条件：当前阶段为 `unread`。
- 原子动作：从 `unread_done` 移除并从 `unread_processed` 减一，写回轮次状态。
- 输入：conversation_key、sweep、correlation_id。
- 输出：无返回值。
- 成功判据：状态写回成功。
- 失败处理：非未读阶段或状态缺失时直接返回，不改已读阶段成果（`1525-1526`）。
- 副作用与幂等性：写库；仅在计数 >0 时递减，重复调用不再变化。

### R46 已读补库观察落库
- 目的：把新文本入站及其列表快照写入本地库，但不产出契约。
- 代码位置：`scripts/hr/store.py:1254`（`save_read_sync_observation`），底层 `1262`（`_save_inbound`）；调用 `scripts/hr/scan.py:1616`。
- 前置条件：R38/R39 通过，且最新入站与库内基线不同、无待核实外发。
- 原子动作：`BEGIN IMMEDIATE` 事务内写 `hr_conversations` + `hr_messages` + 清 `hr_non_text_pending`。
- 输入：message_key、conversation_key、contact、latest、列表预览/时间。
- 输出：写入后的 message_key。
- 成功判据：事务提交成功。
- 失败处理：键非法/文本非法抛 `ValueError`；异常由 sync 的候选循环按 `snapshot_update_failed` 处理（`scripts/hr/sync.py:113-121`）。
- 副作用与幂等性：写库；同 message_key 重复写为覆盖式，幂等。

### R47 已读核对时间落库
- 目的：标记某会话本轮已主动核对，避免下一轮重复打开。
- 代码位置：`scripts/hr/store.py:765`（`mark_unknown_checked`）；调用 `scripts/hr/sync.py:134`。
- 前置条件：本轮该会话已完整读取并处理。
- 原子动作：单条 `UPDATE hr_conversations SET unknown_checked_at` 并提交。
- 输入：conversation_key。
- 输出：布尔值。
- 成功判据：`rowcount == 1`。
- 失败处理：会话不存在返回 False → `paused/snapshot_update_failed`（`scripts/hr/sync.py:134-135`）。
- 副作用与幂等性：写库；重复调用只更新时间，幂等。

### R48 契约组装
- 目的：把读全的聊天按 Phase 1 合同裁剪为可交接结构。
- 代码位置：`scripts/hr/contract.py:54`（`build_phase1_contract`）；调用 `scripts/hr/scan.py:1868`、`2787`。
- 前置条件：R40 通过。
- 原子动作：纯内存组装，最多保留最近 8 条并补最新入站/最新外发/最近系统提示。
- 输入：item、chat、message_key、可选 `unread_consumed_at`。
- 输出：契约字典（含 `read_complete`、`latest_inbound_text`、`messages`、`preview_hint`）。
- 成功判据：返回结构完整；`messages_truncated` 如实标记。
- 失败处理：无异常路径；`latest_inbound` 仅在状态 known 时填值（`90`）。
- 副作用与幂等性：纯函数，幂等。

### R49 契约事件输出
- 目的：发布并可选追加写入 `record` 事件，作为唯一交接产物。
- 代码位置：`scripts/hr/scan.py:1885-1891`（单会话）、`2797-2803`（未读循环）。
- 前置条件：R48 已完成且 R27 成功。
- 原子动作：发布事务事件；`--output` 存在时向 JSONL 追加一行。
- 输入：契约、output 路径。
- 输出：事件对象、文件行。
- 成功判据：事件发布成功且写入无异常。
- 失败处理：写文件异常向上抛；此前已发布的进度事件保留在日志中。
- 副作用与幂等性：追加写 JSONL，重复执行会产生新行（非幂等），故依赖 R44 去重与派发 `applied` 集合。

### R50 返回消息列表
- 目的：结束单个会话读取后回到可继续扫描的列表页。
- 代码位置：`scripts/core/message_list.py:365`（`ensure_list`）、`391`（`leave_chat`）；调用 `scripts/hr/sync.py:118`、`138`、`scripts/hr/scan.py:1695`。
- 前置条件：当前可能处于聊天页或未知页。
- 原子动作：先尝试直接读屏解析列表；失败则最多 3 次 `adb shell input keyevent 4`，每次 sleep 0.6s 后重读。
- 输入：serial、可选 snapshot 容器。
- 输出：是否已回到可解析列表。
- 成功判据：`parse_message_list` 成功（且未传 snapshot 时按未筛选校验）。
- 失败处理：3 次仍失败返回 False → `paused/needs_list`（`scripts/hr/sync.py:118-119`）。
- 副作用与幂等性：发送返回键；已在列表时不发按键，幂等。

### R51 恢复列表并核验筛选与选中
- 目的：在候选失败或读完后安全恢复列表，并确认筛选/选中状态没有漂移。
- 代码位置：`scripts/hr/scan.py:1349`（`_recover_filtered_list`）。
- 前置条件：可选 `chat_read_complete=True`（刚读全同一聊天）。
- 原子动作：先用 R03 判前台；安全时发一次返回键；随后最多 3 轮“读屏 + R04 分类”，必要时发返回键或点击 R07 目标。
- 输入：serial、`filter_entry_absent`、`opened_chat`、`chat_read_complete`。
- 输出：已核验的列表 root 或 None。
- 成功判据：分类为 `PAGE_CHAT_LIST`，且（点击过时）`message_tab_selected` 为 True，且（`filter_entry_absent` 为假时）存在“取消筛选”。
- 失败处理：`unsafe_focus` 或任一核验不通过返回 None → 调用方 `abort_candidate("needs_list")`（`2608-2609`）。
- 副作用与幂等性：最多 2 次返回键 + 1 次点击；重复调用在列表稳定后只读。

### R52 阻塞项汇总
- 目的：离线列出会阻塞扫描/发送的未核对记录。
- 代码位置：`scripts/hr/inbox.py:57`（`_blockers`）；依赖 `hr_store.blocked_action_events`、`pending_discovery_records`。
- 前置条件：数据库已建表。
- 原子动作：只读查询，不连接设备。
- 输入：connection。
- 输出：`blocked` 项列表（含原因、等待天数）。
- 成功判据：查询正常返回。
- 失败处理：异常由 `build_inbox` 包装为 `error` 事件并上抛（`scripts/hr/inbox.py:87-89`）。
- 副作用与幂等性：只读，幂等。

### R53 收件箱分组
- 目的：把未关闭消息分为阻断/必答/待确认/待判断四组。
- 代码位置：`scripts/hr/inbox.py:99`（`_build_inbox`），分组循环 `104-130`，原因文案 `31`（`reason_label`）。
- 前置条件：R52 已得到阻断键集合。
- 原子动作：只读查询 + 内存分组。
- 输入：connection、limit。
- 输出：四组列表与 `counts/total`。
- 成功判据：`total == sum(counts)`。
- 失败处理：查询异常上抛；不做任何业务状态修改（`100`）。
- 副作用与幂等性：只读，幂等。

### R54 单条消息详情读取
- 目的：按唯一消息键读取本地原文、待办与草稿。
- 代码位置：`scripts/hr/inbox.py:202`（`_show_message`），包装 `184`（`show_message`）；调用 `scripts/main.py:1264`。
- 前置条件：消息键已存在于 `hr_messages`。
- 原子动作：只读查询，不连接设备。
- 输入：connection、message_key。
- 输出：详情字典或 None。
- 成功判据：`message_detail` 命中一行。
- 失败处理：返回 None 时 `main.py` 输出 `{"error": "message_not_found"}`（`scripts/main.py:1265-1267`）。
- 副作用与幂等性：只读，幂等。

## 3 组合流程

F1 `hr scan` 未读主流程（`scripts/hr/scan.py:1895` 起）

R01 → R04/R05（`_read_list_root`，`2057`）→ R06 → R22（若存在筛选且不复用）→ R21 → R10 → R20（证明首屏）→ R23（首屏取证）→ R14/R11/R12/R13（解析视口）→ R15（角标兜底）→ R16 → R25 → R17/R18（已读阶段）→ R44 → R27 → R28 → R29 → R30 → R31 → R32 → R33/R34 → R36 → R37 → R38 → R39 → R40 → R43 → R42 → R24 → R41 → R48 → R49 → R50 → R51。步骤意图：先收敛到可读的未读列表，再逐个候选“定位→打开→核验→读全→解析→门禁→落库→回列表”。

F2 `hr scan --conversation` 定向重开（`scripts/hr/scan.py:1991-1993` 转 `1648`）

R01 → R06 → R22 → R10 → R26 → R28 → R29 → R30 → R31 → R32 → R33/R34 → R36 → R37 → R38 → R39 → R40 → R43 → R48 → R49 → R50。步骤意图：跳过列表候选筛选，直接按身份重开已知会话；未在首屏时按 3 下 + 9 上滚动重找（`1714-1726`）。

F3 `hr sync` 已读候选流程（`scripts/hr/sync.py:100-140`）

R01 → R04 → R06 → R22 → R10 → R11 → R17 → R18（列表级筛选）→ R25 → R44 类似轮次去重 → R27 → R28 → R29 → R30 → R31 → R32 → R36 → R37 → R38 → R39 → R46 → R47 → R50，失败候选按 `read_incomplete / chat_open_unverified / contact_mismatch` 跳过（`scripts/hr/sync.py:18-21`、`114-120`）。

F4 失败恢复与继续

R51 → R03（安全三态）→ R06 → R08 → R09 → 回到 F1 的解析步骤。步骤意图：只在能证明前台安全时发送返回键，绝不盲退。

F5 `hr inbox`

R52 → R53（列表）或 R54（`show`）。步骤意图：完全离线，输出前不触达设备。

## 4 数据与副作用边界

- 本地数据库（全部本机 SQLite，`--db` 默认路径见 `scripts/main.py:275`）：
  - `hr_sweep_rounds`：轮次状态 JSON，写入入口 `hr_store.sweep_state`（`scripts/hr/store.py:1191`），读侧字段 `phase/page/seen/opened/unread_done/unread_attempted/unread_pending/captured/top_proven/recommend_seen`。
  - `hr_conversations`：列表基线（`observe_sweep_preview`，`scripts/hr/store.py:929`）、已读核对时间（`mark_unknown_checked`，`765`）、补库快照（`_save_inbound`，`1262`）。
  - `hr_messages`：入站正文与观测时间，只在 `_save_inbound`（`1262`）与契约登记路径写入。
  - `hr_discoveries`：打开前发现记录（`reopen_discovery`，`1951`）与失败原因（`block_discovery`，`1920`）。
  - `hr_non_text_pending`：非文本入站待人工确认（`record_non_text_pending`，`2268`）。
- 日志：`core.operation_log` 只写脱敏事件，不含聊天原文。读取侧事件包括 `unread_chat_opened`（`scripts/hr/chat.py:959`）、`message_list_scrolled`（`scripts/core/message_list.py:324`、`361`）、`workspace_message_tab_tapped`（`scripts/core/workspace.py:415`）、`unread_filter_applied`（`message_list.py:183`）、`conversation_snapshot_failed`（`scripts/hr/scan.py:1170`）。
- 截图：`monitor.capture_evidence` 落本机文件，仅记录路径（`scripts/hr/scan.py:2026`、`2087`、`2211`、`2418`）；契约摘要显式排除截图字段（`scripts/hr/scan.py:461`）。
- 状态流转（读侧）：`unread` → `read`（`scripts/hr/scan.py:2445`）；`unread_pending` 在契约落库后清除（`2562-2568`）；`unread_done` 只增于未读成功并可由 R45 撤销。
- 只读边界：`hr scan` 明确不执行任何 HR 动作（`scripts/hr/scan.py:1902`）；`hr inbox` 明确只读本机库（`scripts/hr/inbox.py:1`、`100`）；`hr sync` 只写观察与核对时间，不写动作。
- 输出：`--output` 为 JSONL 追加（`scripts/hr/scan.py:1888-1890`）；stdout 契约/汇总 JSON（`scripts/main.py:874`、`913`）。
- 幂等性边界：列表/聊天解析、身份派生、门禁判定均纯函数幂等；滑动、点击、返回键、截图、JSONL 追加均非幂等，靠 `seen/applied/captured` 与 `top_proven` 去重。

## 5 未覆盖与不确定项

1. `hr policy.py` 不在读取链路：`scan.py` 的导入清单（`scripts/hr/scan.py:28-33`）与 `sync.py` 的导入清单（`scripts/hr/sync.py:10-13`）都没有 `policy`；`classify_reply`（`scripts/hr/policy.py:60`）与 `validate_decision`（`77`）只服务 Phase 2 决策校验。读取侧只负责产出 `latest_inbound_text` 等字段供其消费。
2. `verify_contact`（`scripts/hr/chat.py:885`）未被 `scan.py`/`sync.py` 调用，grep 命中仅 `hr/actions.py:76`、`265`、`817`、`hr/apply.py:177`、`219`、`hr/review.py:195`、`jobs/enroll.py:671`。读取侧的联系人核验实为 `open_message` 内的 `parse_chat(root)["contact"] == item["name"]`（`scripts/hr/chat.py:958`），两者判定口径不同（前者是标题列表包含匹配，后者是首标题相等），未在代码中统一。
3. `hr scan` / `hr sync` 不主动导航到消息工作区：`workspace.ensure_workspace` 仅由 `workspace check` 命令调用（`scripts/main.py:1529`、`scripts/core/workspace.py:447`）。因此“前置状态”依赖调用方先到位，代码本身不保证。
4. 未读像素阈值来自真机标定注释（`scripts/core/message_list.py:16-19`），不同屏幕密度/主题下是否仍成立未知；仓库未提供可复现夹具。
5. 星期时间标签只承认“两天前”一种映射（`scripts/hr/list_evidence.py:87-102` 注释明示只有一次真机证据），3–6 天与跨周样本缺失，`classify_list_time` 对其余情况返回 `unknown`。
6. 可自动关闭弹窗白名单只有一条“求职防骗提醒”→“知道了”（`scripts/core/workspace.py:36-38`），其他弹窗会被判 `PAGE_POPUP` 并让读取暂停，未覆盖其关闭路径。
7. `read_latest_chat`（`scripts/hr/chat.py:837`）只被 `hr/apply.py:183` 使用，读取侧未使用；本文件未把它列为原子操作，但人工恢复路径会用到相反方向的滚动。
8. `_unread_badge_count`（`scripts/hr/scan.py:1453`）在本次阅读范围内未见调用点，可能是遗留统计函数；未确认其归属，故不作为原子操作。
9. 本次分析为纯只读代码阅读，未运行任何 Python 或 ADB 命令，未连接手机；所有行号基于当前工作副本，若参考仓库后续变更需重新核对。
10. `hr sync` 的 `mark_unknown_checked` 只在 `completed` 且原因不在 `not_located/unread_handled/list_unchanged` 时执行（`scripts/hr/sync.py:128-137`），但“哪些原因算已核对”的业务含义只体现在这一处分支，缺少独立文档说明，属于口径不确定项。
