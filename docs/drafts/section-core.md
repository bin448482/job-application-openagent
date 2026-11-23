# core 设备与页面原始操作

## 0 概览

### 0.1 入口命令

| 命令 | 解析位置 | 执行位置 | 是否直接操作手机 |
| --- | --- | --- | --- |
| `adb inspect` | `scripts/main.py:225` | `scripts/main.py:646-674` → `scripts/jobs/enroll.py:366-382` | 是（只读 + 选卡） |
| `adb open-item` | `scripts/main.py:226-227` | `scripts/main.py:656-657` → `scripts/jobs/enroll.py:385-445` | 是（点击卡片） |
| `adb scroll` | `scripts/main.py:228-229` | `scripts/main.py:658-659` → `scripts/jobs/enroll.py:448-477` | 是（滑动） |
| `adb capture` | `scripts/main.py:230-231` | `scripts/main.py:660-661` → `scripts/jobs/enroll.py:480-545` | 是（只读详情） |
| `adb confirm-identity` | `scripts/main.py:232-233` | `scripts/main.py:662-663` → `scripts/jobs/enroll.py:546-559` | 是（只读详情） |
| `adb continue` | `scripts/main.py:234-236` | `scripts/main.py:664-665` → `scripts/jobs/enroll.py:643-758` | 是（点击沟通 + 可能发资源消息） |
| `workspace ensure` | `scripts/main.py:426-431` | `scripts/main.py:1527-1530` → `scripts/core/workspace.py:447-560` | 是（启动 App、点底部标签、按返回） |
| `device recover` | `scripts/main.py:211-218` | `scripts/main.py:1636-1647` → `scripts/hr/store.py:1068-1086` | 否（纯本机数据库） |

`adb` 与 `workspace` 两组在执行前都先经过设备命令占用闸门：`scripts/main.py:1609-1633`（`_dispatch`）调用 `scripts/hr/scan.py:750-801`（`device_command`），占用冲突时以退出码 2 拒绝。`device recover` 不走该闸门，也不触达 ADB。

### 0.2 涉及文件

- `scripts/core/adb.py`：ADB 传输、设备选择、窗口状态、层级 dump/传输/解析、点击、滑动、按键、截图、哈希、稳定等待、截图落盘。
- `scripts/core/uitree.py`：bounds 解析、节点遍历、屏幕签名、可点击文本定位、资源 ID 后缀取值、失败界面证据。
- `scripts/core/message_list.py`：消息行解析、未读证据、筛选面板判定、列表核验、消息列表滑动、返回列表、列表可见性。
- `scripts/core/workspace.py`：屏幕与底部导航几何、页面信号、弹窗/登录排除、页面分类、消息工作区导航。
- `scripts/core/events.py`：事务监控、指标累加、证据截图路径与截图、监督事件发布。
- `scripts/core/obs.py`：UI 指纹、差异判定、脱敏观测归档。
- `scripts/core/operation_log.py`：跨进程加锁的 JSONL 操作日志。
- `scripts/core/keys.py`、`db.py`、`status.py`、`errors.py`、`tasks.py`：本地标识、数据库连接、词表、异常与派发/报告契约；均不直接操作手机（见第 4、5 节）。
- `scripts/main.py`：三组命令的参数解析、设备闸门与分发；`scripts/jobs/enroll.py`、`scripts/hr/actions.py`、`scripts/hr/store.py`、`scripts/hr/scan.py` 只作为命令实现与占用恢复的调用链证据。

### 0.3 涉及页面

页面常量见 `scripts/core/workspace.py:15-21`：

- `main_page`：BOSS 主界面，底部可见至少两个已知标签（`职位/消息/发现/我的`）。
- `chat_list`：消息聊天列表，唯一 `:id/recyclerView`，有具名行或可证实的空态。
- `chat_detail`：聊天详情，出现 `:id/mRecycleView` 或 `:id/et_msg`，或前台 Activity 含 `ChatRoomActivity`。
- `job_detail`：职位详情，同时出现 `tv_description` 与 `btn_chat|btn_continue_chat`，或前台 Activity 含 `BossJobPagerActivity`。
- `popup`：检测到弹窗结构或白名单按钮文案。
- `login`：检测到登录文案且不在消息正文区。
- `unknown`：其余，包含 `recommend_feed`（“综合推荐”或“收藏过我/看过我/新职位”三标签）。

### 0.4 App 包名与启动组件

- 包名硬编码为 `com.hpbr.bosszhipin`（`scripts/core/adb.py:19`；`scripts/core/obs.py:14` 另有一份同名常量）。
- 启动组件不在代码中写死，每次通过 `adb shell cmd package resolve-activity --brief com.hpbr.bosszhipin` 解析，并要求结果唯一匹配 `com.hpbr.bosszhipin/<[\w.$]+>`（`scripts/core/adb.py:187-195`），随后 `am start -n <component>`（`scripts/core/adb.py:198-202`）。

### 0.5 设备与解锁前提

- `adb` 可执行文件在 PATH；`adb devices` 至少一台状态为 `device` 的已授权设备；多台时须显式 `--serial`（`scripts/core/adb.py:110-147`）。
- 手机已开启 USB 调试并在手机上授权本机；`unauthorized`/`offline` 分别映射为 `device_unauthorized`/`device_disconnected`（`scripts/core/adb.py:127-141`）。
- 解锁判定只认 Android 窗口服务明确报告 `isKeyguardShowing=false`；未出现该字段也按未解锁处理（`scripts/core/adb.py:162-164, 180-184`）。
- 普通 ADB 命令超时 15 秒（`scripts/core/adb.py:91`），设备清单 5 秒（`:114`），截图类 3 秒（`:281, :302, :363`）。
- 屏幕层级依赖 `uiautomator dump` 与 `/sdcard/job_tracker_ui.xml` 可写（`scripts/core/adb.py:212-216`）。

## 1 状态判定原语

1. **设备清单与唯一性**：跳过 `adb devices` 首行，按空白切分，取状态为 `device` 的序列号；0 台或多台均拒绝（`scripts/core/adb.py:121-147`）。
2. **未授权/离线分类**：指定序列号存在但状态为 `unauthorized` 或 `offline` 时给出稳定原因码，否则一律 `device_disconnected`（`scripts/core/adb.py:127-141`）。
3. **ADB 断连识别**：`TimeoutExpired`、stderr 命中 `no devices/emulators found|device offline|unauthorized|not found|error: closed|protocol fault|transport error` 等视为断连；普通远端命令非零退出不视为断连（`scripts/core/adb.py:39-52`）。非零退出且已有序列号时只探测一次连接，不重放命令（`scripts/core/adb.py:99-103`）。
4. **解锁可用**：`dumpsys window` 中所有 `isKeyguardShowing` 取值集合必须恰为 `{"false"}`；零命中、含 `true`、无法解析均判未解锁（`scripts/core/adb.py:162-164, 180-184`）。
5. **前台组件可读**：`mCurrentFocus` 行必须匹配 `包名/Activity`；无匹配即“无法确认前台页面”（`scripts/core/adb.py:160-161, 167-172`）。
6. **启动入口唯一**：`resolve-activity --brief` 输出最后一行必须整体匹配 `com.hpbr.bosszhipin/<[\w.$]+>`，否则拒绝启动（`scripts/core/adb.py:191-195`）。
7. **层级属于 BOSS**：解析后的树中必须存在 `package='com.hpbr.bosszhipin'` 的节点，否则抛“当前界面不是 BOSS 直聘”（`scripts/core/adb.py:238-239`）。
8. **原始截屏有效**：至少 16 字节头；宽高非 0；像素格式必须为 1（RGBA）；`len(raw)-16 == width*height*4`（`scripts/core/adb.py:282-288`）。
9. **画面稳定**：连续两次 `screencap -p` 的 SHA-256 相同才算 stable；到期仍不同则返回 `stable=False`（`scripts/core/adb.py:322-331`）。
10. **节点可点击**：bounds 必须匹配 `[l,t][r,b]` 且四个非负整数；可点击容器必须 `clickable=true` 且 `enabled=true`（`scripts/core/uitree.py:11-14`；`scripts/core/message_list.py:35-42`）。
11. **点击目标唯一**：按文本找到的全部节点向上归并到最近可点击祖先，去重后必须恰好一个；`未读` 额外允许唯一 `:id/tv_option_name` 文本节点（`scripts/core/uitree.py:31-41`；`scripts/core/message_list.py:45-57`）。
12. **消息列表唯一**：资源 ID 以 `:id/recyclerView` 结尾的节点必须恰好一个；聊天详情另认 `:id/mRecycleView`（`scripts/core/message_list.py:246-248`；`scripts/core/workspace.py:163-167`）。
13. **未读证据**：无像素时，只有“本轮已证实未读筛选”或角标存在（有子节点或数字文本）才判 `yes`，否则 `unknown`；有像素时按 `badge_view` bounds 内接近 `(255,71,83)`、每通道容差 30 的红像素数 ≥100 判 `yes`（`scripts/core/message_list.py:19, 264-279`）。
14. **聊天列表可读**：唯一 `:id/recyclerView`、可解析出至少一个具名行，或空容器且“消息”标签 selected=true / 出现空态文案之一（`scripts/core/workspace.py:169-186`）；对外另有“任一行有联系人名”判定（`scripts/core/message_list.py:396-403`）。
15. **页面归类优先级**：弹窗 → 登录 → `ChatRoomActivity` → `BossJobPagerActivity` → 综合推荐 → 聊天详情信号 → 职位详情信号 → 聊天列表 → 底部标签 ≥2 → unknown（`scripts/core/workspace.py:276-296`）。
16. **弹窗/登录排除聊天正文**：判定前先收集消息列表与聊天详情容器内所有节点 id，弹窗/登录文案命中这些节点时不算信号（`scripts/core/workspace.py:189-210, 242-252`）。
17. **“消息”标签选中**：在目标容器自身及其后代中，任一 `selected=true` 即 True；否则任一 `false` 即 False；都没有则为 None（`scripts/core/workspace.py:150-160`）。
18. **观测指纹一致**：文本、clickable、enabled 必须完全相同，bounds 每坐标允许 ±8 像素；任一节点缺少合法 bounds 时按不同处理（`scripts/core/obs.py:38-50`）。

## 2 原子操作

> 约定：所有 ADB 动作均经 A01 执行；文中不再重复 15 秒超时、`adb_start/adb_finished` 日志与指标累加。所有“点击”的坐标都来自 A15。

### A01 运行单条 ADB 命令
- 目的：统一执行一条 ADB 命令并返回 stdout。
- 代码位置：`scripts/core/adb.py:81-107`（`run_adb`）
- 前置条件：`adb` 在 PATH；串号已由 A03 选定或显式给出。
- 原子动作：`adb [-s <serial>] <args...>`，一次子进程，`check=True`，`capture_output=True`，15 秒超时，UTF-8。
- 输入：`serial` 或 `None`；命令参数列表（`*args`）。
- 输出：stdout 文本。
- 成功判据：子进程退出码 0。
- 失败处理：记录 `adb_failed`、`adb_failures`；A04 命中即抛 `DeviceDisconnected`；已有序列号且为远端命令非零退出时只调用 A03 探测连接一次，绝不重放原命令。
- 副作用与幂等性：写操作日志与事务指标；手机侧是否变化取决于具体子命令，A01 本身不可假定幂等。

### A02 读取设备清单
- 目的：取得 `adb devices` 原始输出。
- 代码位置：`scripts/core/adb.py:110-120`（`select_device` 内调用）
- 前置条件：无。
- 原子动作：`adb devices`，超时 5 秒。
- 输入：无。
- 输出：文本清单。
- 成功判据：退出码 0。
- 失败处理：记录指标；断连按 A04 抛出，其他子进程错误原样上抛。
- 副作用与幂等性：只读，幂等。

### A03 判定单一已授权设备
- 目的：从清单中选出一台且仅一台 `device` 状态设备，或按 `--serial` 校验。
- 代码位置：`scripts/core/adb.py:121-147`（`select_device`）
- 前置条件：A02 已返回清单。
- 原子动作：无 ADB；解析每行 `序列号 状态` 并做唯一性判定（一次判定）。
- 输入：A02 输出；可选 `requested_serial`。
- 输出：选定序列号，或异常。
- 成功判据：显式串号状态为 `device`；或未指定时恰好 1 台。
- 失败处理：`unauthorized`/`offline` 给专属原因码；0 台或多台抛错；不重试。
- 副作用与幂等性：纯判定，幂等。

### A04 判定 ADB 错误是否断连
- 目的：把超时与特定 stderr 归类为 `DeviceDisconnected`，其余错误放行。
- 代码位置：`scripts/core/adb.py:39-52`（`raise_if_disconnected`）
- 前置条件：捕获到一个 `BaseException`。
- 原子动作：无 ADB；类型与 stderr 正则判定（一次判定）。
- 输入：异常对象。
- 输出：无返回；命中时抛出带 `reason` 的异常。
- 成功判据：不抛即“非断连”；抛 `DeviceDisconnected` 即“断连”。
- 失败处理：本身不重试。
- 副作用与幂等性：纯判定，幂等。

### A05 读取窗口状态
- 目的：一次 `dumpsys window` 同时取得前台组件与锁屏证据。
- 代码位置：`scripts/core/adb.py:150-165`（`window_state`）
- 前置条件：设备已选定（A03）。
- 原子动作：`adb -s <serial> shell dumpsys window`。
- 输入：`serial`。
- 输出：`(component|None, unlocked: bool)`。
- 成功判据：命令退出码 0；解析失败时 component 为 None、unlocked 为 False，不抛解析异常。
- 失败处理：ADB 层错误按 A01/A04；解析容错不终止。
- 副作用与幂等性：只读，幂等。

### A06 判定前台组件
- 目的：取得当前前台 `(包名, Activity)`，供页面归类与来源判定。
- 代码位置：`scripts/core/adb.py:167-172`（`current_component`）；`scripts/core/adb.py:175-177`（`current_activity`）
- 前置条件：A05 已执行。
- 原子动作：无额外 ADB；读取 A05 结果中的 component（一次判定）。
- 输入：A05 结果。
- 输出：二元组，或异常。
- 成功判据：component 非 None。
- 失败处理：None 时抛 `RuntimeError("无法确认手机当前前台页面")`；不重试。
- 副作用与幂等性：纯判定，幂等。

### A07 判定已解锁
- 目的：只有明确未锁屏时才允许后续设备动作。
- 代码位置：`scripts/core/adb.py:180-184`（`verify_unlocked`）
- 前置条件：A05 已执行。
- 原子动作：无额外 ADB；读取 unlocked 布尔（一次判定）。
- 输入：A05 结果。
- 输出：无返回。
- 成功判据：unlocked 为 True。
- 失败处理：否则抛错；不重试、不自动解锁。
- 副作用与幂等性：纯判定，幂等。

### A08 解析启动入口
- 目的：取得目标包唯一的系统启动组件。
- 代码位置：`scripts/core/adb.py:187-195`（`resolve_launcher_component`）
- 前置条件：A03 选定设备；包名 `com.hpbr.bosszhipin`。
- 原子动作：`adb -s <serial> shell cmd package resolve-activity --brief com.hpbr.bosszhipin`。
- 输入：`serial`、`package`。
- 输出：`com.hpbr.bosszhipin/...` 字符串。
- 成功判据：取输出最后一个非空行，整体匹配 `com.hpbr.bosszhipin/<[\w.$]+>`。
- 失败处理：不匹配即抛错，终止启动流程；不猜测 Activity。
- 副作用与幂等性：只读，幂等。

### A09 启动或切回 App
- 目的：把 BOSS 直聘带到前台。
- 代码位置：`scripts/core/adb.py:198-202`（`launch_app`）
- 前置条件：A08 已成功返回组件。
- 原子动作：`adb -s <serial> shell am start -n <component>`。
- 输入：`serial`、`package`。
- 输出：实际使用的组件字符串。
- 成功判据：命令退出码 0；页面是否就绪由 W14 判定，不由本步保证。
- 失败处理：启动失败原样抛出，调用方转 `launcher_unresolved`；不重试。
- 副作用与幂等性：改变前台任务栈；重复执行通常回到同一入口，但不保证保留原页面位置。

### A10 删除旧层级 dump 文件
- 目的：避免读到上一次的 XML。
- 代码位置：`scripts/core/adb.py:212-213`（`read_hierarchy`）
- 前置条件：设备已选定。
- 原子动作：`adb -s <serial> shell rm -f /sdcard/job_tracker_ui.xml`。
- 输入：`serial`。
- 输出：无。
- 成功判据：退出码 0。
- 失败处理：错误上抛（含文件不存在时 `rm -f` 仍为 0 的正常情形）。
- 副作用与幂等性：删除手机侧固定路径临时文件；幂等。

### A11 生成 UI 层级 dump
- 目的：让 uiautomator 把当前界面写成 XML。
- 代码位置：`scripts/core/adb.py:214-222`（`read_hierarchy`）
- 前置条件：A10 已执行；uiautomator 可用。
- 原子动作：`adb -s <serial> shell uiautomator dump /sdcard/job_tracker_ui.xml`。
- 输入：`serial`。
- 输出：无（文件在手机侧）。
- 成功判据：命令退出码 0。
- 失败处理：仅当退出码 137 时最多重试一次（总 2 次），第二次仍 137 抛 `RuntimeError`；其他退出码立即抛出。
- 副作用与幂等性：覆盖手机侧固定路径文件；重试会覆盖，视为幂等。

### A12 读取 dump XML 文本
- 目的：把手机侧 XML 传回本机。
- 代码位置：`scripts/core/adb.py:225-229`（`read_hierarchy`）
- 前置条件：A11 成功。
- 原子动作：`adb -s <serial> shell cat /sdcard/job_tracker_ui.xml`。
- 输入：`serial`。
- 输出：XML 字符串。
- 成功判据：退出码 0。
- 失败处理：错误上抛；不重试。
- 副作用与幂等性：只读，幂等。

### A13 解析 XML 层级
- 目的：把 XML 字符串变成可遍历的元素树。
- 代码位置：`scripts/core/adb.py:230-237`（`read_hierarchy`）
- 前置条件：A12 返回文本。
- 原子动作：本地 `ET.fromstring`（一次解析）。
- 输入：XML 文本。
- 输出：`ElementTree.Element` 根。
- 成功判据：解析成功。
- 失败处理：`ParseError` 转 `RuntimeError("Android UI 层级不是有效 XML")`；不重试。
- 副作用与幂等性：纯本地，幂等。

### A14 判定层级属于 BOSS
- 目的：拒绝其他 App 的层级。
- 代码位置：`scripts/core/adb.py:238-239`（`read_hierarchy`）
- 前置条件：A13 已得到根节点。
- 原子动作：无 ADB；查找 `package='com.hpbr.bosszhipin'` 节点（一次判定）。
- 输入：根节点。
- 输出：无返回。
- 成功判据：至少存在一个该包节点。
- 失败处理：否则抛错，读屏失败指标 +1。
- 副作用与幂等性：纯判定，幂等。

### A15 计算节点中心坐标
- 目的：把 bounds 转成点击坐标。
- 代码位置：`scripts/core/adb.py:249-255`（`tap_bounds`）
- 前置条件：bounds 已由 U01 校验为四整数。
- 原子动作：无 ADB；`x=(left+right)//2`、`y=(top+bottom)//2`（一次计算）。
- 输入：`[left, top, right, bottom]`。
- 输出：`(x, y)`，通过后续 A16 使用。
- 成功判据：坐标落在 bounds 内（整数除法天然满足，除非 right<left 等异常输入）。
- 失败处理：不校验 right>left/top<bottom；异常输入由 U01 的格式约束兜底。
- 副作用与幂等性：纯计算，幂等。

### A16 点击坐标
- 目的：按中心点触发一次点击。
- 代码位置：`scripts/core/adb.py:256-257`（`tap_bounds`）
- 前置条件：A15 已算坐标。
- 原子动作：`adb -s <serial> shell input tap <x> <y>`。
- 输入：`serial`、`x`、`y`。
- 输出：无。
- 成功判据：ADB 退出码 0；UI 是否响应不在本步保证范围。
- 失败处理：ADB 错误上抛；不重试。调用方若需重试必须重新读屏并重新定位。
- 副作用与幂等性：可能改变页面；不可假定幂等。

### A17 滑动
- 目的：执行一次带时长的直线滑动。
- 代码位置：`scripts/core/adb.py:265-268`（`swipe`）
- 前置条件：起点/终点坐标已由调用方从容器 bounds 推导。
- 原子动作：`adb -s <serial> shell input swipe <x1> <y1> <x2> <y2> <duration_ms>`。
- 输入：起点、终点、时长（默认 450 ms；长按通道 800 ms；同点短滑 60 ms）。
- 输出：无。
- 成功判据：ADB 退出码 0；视口是否变化由 U03 比较。
- 失败处理：错误上抛；不重试。
- 副作用与幂等性：可能改变列表位置；不可假定幂等。

### A18 发送按键
- 目的：发送一个 Android keyevent。
- 代码位置：`scripts/core/adb.py:271-273`（`keyevent`）
- 前置条件：设备已选定、已解锁。
- 原子动作：`adb -s <serial> shell input keyevent <code>`。
- 输入：keyevent 码（现有唯一调用为返回键 `4`）。
- 输出：无。
- 成功判据：ADB 退出码 0。
- 失败处理：错误上抛；不重试。
- 副作用与幂等性：可能返回页面/关闭弹层；不可假定幂等。

### A19 读取原始 RGBA 截图
- 目的：取得宽高与像素缓冲，供未读角标等像素判定。
- 代码位置：`scripts/core/adb.py:276-294`（`read_screen_raw`）
- 前置条件：设备已选定。
- 原子动作：`adb -s <serial> exec-out screencap`，超时 3 秒。
- 输入：`serial`。
- 输出：`(width, height, rgba_bytes)`。
- 成功判据：头 ≥16 字节、宽高非 0、像素格式 1、像素长度精确匹配。
- 失败处理：不合法即抛 `RuntimeError("无法读取有效的原始截图")`；不重试。
- 副作用与幂等性：只读，幂等。

### A20 计算截图 SHA-256
- 目的：得到不落盘的画面摘要。
- 代码位置：`scripts/core/adb.py:297-308`（`screencap_hash`）
- 前置条件：设备已选定。
- 原子动作：`adb -s <serial> exec-out screencap -p`，超时 3 秒，对 stdout 计算 SHA-256。
- 输入：`serial`。
- 输出：64 位十六进制摘要。
- 成功判据：退出码 0。
- 失败处理：错误上抛；不重试。
- 副作用与幂等性：只读；同一画面摘要稳定，但受时钟、动画、通知影响。

### A21 等待截图稳定或变化
- 目的：以固定轮询判断画面是否已稳定、相对旧摘要是否变化。
- 代码位置：`scripts/core/adb.py:311-331`（`wait_for_screen`）
- 前置条件：`timeout_ms` 与 `interval_ms` 均为 1–30000 的整数（布尔不接受）。
- 原子动作：重复 A20；每轮比较当前摘要与上一轮；连续两次相同且未超时即 stable；超时返回最后一次观测（一次判定循环）。
- 输入：`serial`、可选 `previous_hash`、超时（默认 3000 ms）、间隔（默认 200 ms）。
- 输出：`{hash, changed, polls, elapsed_ms, stable}`；`changed` 仅在提供 `previous_hash` 时可能为 True。
- 成功判据：`stable=True` 表示连续两帧相同；`changed=True` 表示与旧摘要不同。
- 失败处理：截图异常直接抛出；超时不抛错，返回 `stable=False`。
- 副作用与幂等性：只读；轮询次数取决于画面与时限。

### A22 保存 PNG 截图
- 目的：把当前屏幕写成 PNG 证据文件。
- 代码位置：`scripts/core/adb.py:355-369`（`capture_screenshot`）
- 前置条件：目标目录已存在；`serial` 可空（空则不加 `-s`）。
- 原子动作：`adb [-s serial] exec-out screencap -p`，把 stdout 字节写入 `path`。
- 输入：`serial`、本机路径。
- 输出：无返回；磁盘出现 PNG。
- 成功判据：退出码 0 且写文件成功。
- 失败处理：ADB/OS 错误上抛；不重试。
- 副作用与幂等性：覆盖同名文件；同画面重复写内容一致。

### A23 在占用围栏内启动 ADB 子进程
- 目的：把子进程的创建与取消/占用校验串行化，进程运行期间不持锁。
- 代码位置：`scripts/core/adb.py:55-78`（`_run_transport`）；围栏变量 `scripts/core/adb.py:21`
- 前置条件：无；`transport_fence` 未设置时退化为普通 `subprocess.run`。
- 原子动作：在 `ContextVar` 围栏内 `Popen` 一次，再在锁外 `communicate(timeout)`。
- 输入：命令、超时与 `subprocess` 选项。
- 输出：`CompletedProcess`。
- 成功判据：进程结束且 `check` 通过（若启用）。
- 失败处理：任何异常都 kill + wait 子进程后原样抛出。
- 副作用与幂等性：一次进程启动；不可重入假定为由调用方保证。

### I01 读取当前默认输入法（core 之外）
- 目的：记录发送前输入法，供发送后恢复。
- 代码位置：`scripts/hr/actions.py:716-718`
- 前置条件：设备已选定；本步在输入框定位（`scripts/hr/actions.py:722-730`）之前执行，不依赖输入框。
- 原子动作：`adb -s <serial> shell settings get secure default_input_method`。
- 输入：`serial`。
- 输出：输入法组件字符串。
- 成功判据：非空且不为 `null`。
- 失败处理：否则抛错，停止发送。
- 副作用与幂等性：只读，幂等。

### I02 启用并切换 ADBKeyboard（core 之外）
- 目的：让后续广播能写入输入框。
- 代码位置：`scripts/hr/actions.py:733-735`
- 前置条件：已读取 `ime list -s`；`com.android.adbkeyboard/.AdbIME` 存在或可启用。
- 原子动作：`adb shell ime enable com.android.adbkeyboard/.AdbIME`（未启用时）后 `adb shell ime set com.android.adbkeyboard/.AdbIME`。
- 输入：`serial`。
- 输出：无。
- 成功判据：两条命令退出码 0。
- 失败处理：异常经 A04 分类后上抛。
- 副作用与幂等性：改变系统输入法；切换后输入框会失焦，必须重新点击。

### I03 清空输入框（core 之外）
- 目的：清除占位符或旧草稿。
- 代码位置：`scripts/hr/actions.py:739-740, 746-747, 759-760`
- 前置条件：ADBKeyboard 已切换，输入框已重新点击聚焦。
- 原子动作：`adb shell am broadcast -a ADB_CLEAR_TEXT`，等待 0.2 秒。
- 输入：`serial`。
- 输出：无。
- 成功判据：重新读屏后输入框文本属于 `""/"回复消息"/"介绍自己回复率更高哦~"/"和Ta聊一聊~"`。
- 失败处理：两次仍非空返回 `needs_draft_clear`；不点击发送。
- 副作用与幂等性：清空输入框；重复执行结果相同。

### I04 广播写入文本（core 之外）
- 目的：把完整正文写入聊天输入框。
- 代码位置：`scripts/hr/actions.py:755-774`（编码与广播在 755、768）
- 前置条件：I02、I03 完成；输入框唯一且已聚焦。
- 原子动作：`adb shell am broadcast -a ADB_INPUT_B64 --es msg <base64>`。
- 输入：UTF-8 正文的 base64。
- 输出：无。
- 成功判据：读屏后输入框 `text` 与正文完全相等。
- 失败处理：最多两轮；第二轮前重新点击输入框并清空；仍失败返回 `not_sent`，不点击发送。
- 副作用与幂等性：覆盖输入框内容；重复写入同一正文结果相同，但会在第二轮前清空。

### I05 恢复原输入法（core 之外）
- 目的：发送动作结束后还原系统输入法。
- 代码位置：`scripts/hr/actions.py:859-871`
- 前置条件：发送流程进入 `finally`；未断连。
- 原子动作：`adb shell ime set <original_ime>`；若 ADBKeyboard 原本未启用再 `adb shell ime disable com.android.adbkeyboard/.AdbIME`。
- 输入：原始输入法、原始启用集合。
- 输出：无。
- 成功判据：命令退出码 0。
- 失败处理：已点击发送时恢复失败上抛；未点击时只记 `input_method_restore_failed`，不改变“未发送”证据。
- 副作用与幂等性：恢复系统设置；重复执行同值。

### U01 解析节点 bounds
- 目的：把 `[l,t][r,b]` 转成四个整数。
- 代码位置：`scripts/core/uitree.py:9-14`（`get_bounds`）
- 前置条件：节点存在 `bounds` 属性。
- 原子动作：正则整体匹配（一次判定/解析）。
- 输入：`Element`。
- 输出：`(left, top, right, bottom)`。
- 成功判据：完整匹配且全为数字。
- 失败处理：不匹配抛 `ValueError`；调用方决定跳过或终止。
- 副作用与幂等性：纯本地，幂等。

### U02 列举节点及后代
- 目的：在单个卡片/行内读取可见字段。
- 代码位置：`scripts/core/uitree.py:17-19`（`descendants`）
- 前置条件：节点已取得。
- 原子动作：`list(node.iter("node"))`。
- 输入：节点。
- 输出：节点列表（含自身）。
- 成功判据：始终成功。
- 失败处理：无。
- 副作用与幂等性：只读，幂等。

### U03 生成屏幕签名
- 目的：用可见文本与 bounds 证明视口是否变化。
- 代码位置：`scripts/core/uitree.py:22-28`（`screen_signature`）
- 前置条件：层级根已取得。
- 原子动作：遍历所有有非空 `text` 的节点，取 `(resource-id, text, bounds)` 元组（一次判定依据生成）。
- 输入：根节点。
- 输出：有序元组。
- 成功判据：始终成功；空文本节点不参与。
- 失败处理：无。
- 副作用与幂等性：只读，幂等；同一层级结果稳定。

### U04 按文本定位唯一点击节点
- 目的：点击“取消筛选”等文本时避免误点。
- 代码位置：`scripts/core/uitree.py:31-41`（`clickable_text`）
- 前置条件：根节点已取得。
- 原子动作：找文本完全相等的节点，向上找 `clickable=true` 祖先并去重（一次判定）。
- 输入：根、目标文本。
- 输出：唯一元素或 `None`。
- 成功判据：去重后恰好 1 个。
- 失败处理：0 个或多个都返回 `None`，不点击。
- 副作用与幂等性：纯判定，幂等。

### U05 读取资源 ID 后缀文本
- 目的：按 `:id/tv_name` 等后缀读取第一个非空文本。
- 代码位置：`scripts/core/uitree.py:44-49`（`node_value`）；`scripts/core/message_list.py:209-216`（`_text_by_id`）为同语义的列表内实现
- 前置条件：节点已取得。
- 原子动作：遍历自身及后代，匹配 `resource-id` 后缀（一次读取）。
- 输入：节点、后缀。
- 输出：字符串，缺失为空串。
- 成功判据：始终成功；空串表示未找到。
- 失败处理：无。
- 副作用与幂等性：只读，幂等。

### U06 生成有界 UI 证据
- 目的：失败时保留最多 8 KiB 的界面诊断文本。
- 代码位置：`scripts/core/uitree.py:52-57`（`bounded_ui_evidence`）
- 前置条件：根节点可为 None？函数签名要求 Element，实际调用均传根。
- 原子动作：序列化整棵树并按 UTF-8 字节截断（一次生成）。
- 输入：根、上限（默认 8192）。
- 输出：`(evidence, truncated)`。
- 成功判据：始终成功；`truncated` 表示是否截断。
- 失败处理：无显式捕获；序列化异常由调用方处理。
- 副作用与幂等性：纯本地，幂等。

### U07 查找唯一文本节点
- 目的：为筛选面板等按文案找节点。
- 代码位置：`scripts/core/message_list.py:30-32`（`nodes_with_text`）
- 前置条件：根节点已取得。
- 原子动作：遍历并比较 `text.strip()` 完全相等（一次读取）。
- 输入：根、文本。
- 输出：节点列表（可能 0、1、多）。
- 成功判据：不抛错；数量由调用方判定。
- 失败处理：无。
- 副作用与幂等性：只读，幂等。

### U08 查找最近可点击且启用的祖先
- 目的：把文本节点升级为可点击容器。
- 代码位置：`scripts/core/message_list.py:35-42`（`clickable_ancestor`）
- 前置条件：根与起始节点已取得。
- 原子动作：沿父链上溯，返回第一个 `clickable=true` 且 `enabled=true` 的节点（一次判定）。
- 输入：根、节点。
- 输出：元素或 `None`。
- 成功判据：命中即返回；到根仍未命中返回 None。
- 失败处理：无异常；None 由调用方拒绝点击。
- 副作用与幂等性：纯判定，幂等。

### M01 解析消息行
- 目的：读取当前消息列表可见行的结构化字段与未读证据。
- 代码位置：`scripts/core/message_list.py:239-290`（`parse_message_list`）
- 前置条件：根节点已由 A11–A14 取得；可可选传入像素缓冲。
- 原子动作：定位唯一 `:id/recyclerView`，逐行读取 `tv_name/tv_position/tv_msg/tv_time_v2/badge_view` 与 `[...]` 预览标记（一次解析）。
- 输入：根；`unread_filter_verified`；可选 `(width,height,pixels)`。
- 输出：行字典列表（`name/position/message/time/unread/unread_count/kind/preview_event`）。
- 成功判据：唯一列表存在；无名字但可点击的行输出占位记录。
- 失败处理：列表数量不为 1 抛 `RuntimeError`；像素尺寸不符抛 `ValueError`。
- 副作用与幂等性：纯本地，幂等。

### M02 判定筛选面板可见
- 目的：确认“未读”和“确定”同时出现，面板可操作。
- 代码位置：`scripts/core/message_list.py:60-62`（`_filter_panel_visible`）
- 前置条件：根节点已取得。
- 原子动作：两次 U07（一次判定）。
- 输入：根。
- 输出：布尔。
- 成功判据：两个文本都至少出现一次。
- 失败处理：无。
- 副作用与幂等性：纯判定，幂等。

### M03 统计矩形内红色像素
- 目的：以真实角标颜色判定未读，而非只看层级。
- 代码位置：`scripts/core/message_list.py:219-236`（`count_red_pixels`）
- 前置条件：RGBA 缓冲与宽度自洽；矩形可越界，函数会裁剪。
- 原子动作：逐像素比较 `(255,71,83)`，每通道容差默认 30（一次判定）。
- 输入：`pixels`、`width`、`box`、`tolerance`。
- 输出：命中像素数。
- 成功判据：正常返回整数；`>=100` 由 M01 判为未读。
- 失败处理：宽度 ≤0 或长度不整除 `width*4` 抛 `ValueError`。
- 副作用与幂等性：纯本地，幂等。

### M04 核验唯一可读消息列表
- 目的：在“归顶/滑动”后确认仍处于消息列表，而不是未知空容器。
- 代码位置：`scripts/core/message_list.py:293-306`（`_verified_message_list`）
- 前置条件：根节点已取得。
- 原子动作：调用 M01，再调用 W14 归类并检查 kind（一次判定）。
- 输入：根。
- 输出：无返回。
- 成功判据：M01 成功且 W14 返回 `chat_list`。
- 失败处理：不可识别或非 chat_list 抛 `RuntimeError`，绝不当作“已到顶”。
- 副作用与幂等性：纯判定，幂等。

### M05 判定消息列表可见
- 目的：区分消息列表与职位详情等页面。
- 代码位置：`scripts/core/message_list.py:396-403`（`message_list_visible`）
- 前置条件：根节点已取得。
- 原子动作：M01 后检查是否存在有名字的行（一次判定）。
- 输入：根。
- 输出：布尔。
- 成功判据：至少一行 `name` 非空。
- 失败处理：M01 抛错时经 A04 分类并返回 False。
- 副作用与幂等性：纯判定，幂等。

### W01 判定屏幕边界
- 目的：从层级顶层节点确定整屏 bounds，供底部导航几何计算。
- 代码位置：`scripts/core/workspace.py:65-78`（`_screen_bounds`）
- 前置条件：根节点已取得。
- 原子动作：候选节点必须 `left==0 && top==0 && right>left && bottom>top`，去重后唯一才采用（一次判定）。
- 输入：根。
- 输出：bounds 或 None。
- 成功判据：恰好一个不同候选。
- 失败处理：0 个或多个返回 None，后续底部判定放弃。
- 副作用与幂等性：纯判定，幂等。

### W02 计算底部导航阈值
- 目的：给出“屏幕下方 30%”的上边界。
- 代码位置：`scripts/core/workspace.py:81-87`（`_bottom_threshold`）
- 前置条件：W01 成功。
- 原子动作：`top + int((bottom-top)*0.7)`（一次计算）。
- 输入：屏幕 bounds。
- 输出：y 阈值或 None。
- 成功判据：W01 非 None。
- 失败处理：W01 失败时返回 None。
- 副作用与幂等性：纯计算，幂等。

### W03 收集底部文本节点
- 目的：限定“消息”等标签只在屏幕下方查找。
- 代码位置：`scripts/core/workspace.py:90-104`（`_bottom_nodes`）
- 前置条件：W02 成功。
- 原子动作：遍历所有非空文本节点，U01 解析后 `top >= threshold` 即收集（一次读取）。
- 输入：根。
- 输出：节点列表。
- 成功判据：阈值可用；非法 bounds 跳过。
- 失败处理：阈值 None 时返回空列表。
- 副作用与幂等性：只读，幂等。

### W04 收集底部标签集合
- 目的：用已知标签数量判断是否 BOSS 主界面。
- 代码位置：`scripts/core/workspace.py:107-110`（`_bottom_labels`）
- 前置条件：W03 已返回节点。
- 原子动作：取文本属于 `{职位, 消息, 发现, 我的}` 的集合（一次判定）。
- 输入：根。
- 输出：标签集合。
- 成功判据：始终成功。
- 失败处理：无。
- 副作用与幂等性：纯判定，幂等。

### W05 定位底部标签可点击容器
- 目的：唯一定位“消息”标签的可点击祖先，缺失或歧义都不猜。
- 代码位置：`scripts/core/workspace.py:113-142`（`locate_bottom_tab_target`）；`:145-147`（`locate_message_target`）
- 前置条件：W02 成功。
- 原子动作：在阈值以下找文本相等的标签，经 U08 归并去重（一次判定）。
- 输入：根、标签、缺失原因码。
- 输出：`(target|None, reason)`。
- 成功判据：恰好 1 个可点击容器。
- 失败处理：返回 `*_missing` 或 `*_ambiguous` 原因码，不点击。
- 副作用与幂等性：纯判定，幂等。

### W06 读取消息标签选中态
- 目的：验证点击“消息”后确实切换到该 tab。
- 代码位置：`scripts/core/workspace.py:150-160`（`message_tab_selected`）
- 前置条件：W05 能定位目标。
- 原子动作：检查目标自身及后代 `selected` 属性（一次判定）。
- 输入：根。
- 输出：True/False/None。
- 成功判据：出现 `true` 为 True；仅有 `false` 为 False；均无为 None。
- 失败处理：定位失败返回 None，不推断。
- 副作用与幂等性：纯判定，幂等。

### W07 判定聊天列表状态
- 目的：区分“有具名行”“可证实空列表”“不可识别”。
- 代码位置：`scripts/core/workspace.py:169-186`（`_chat_list_state`）
- 前置条件：根节点已取得。
- 原子动作：要求唯一 `:id/recyclerView`；M01 解析；无具名行时检查空容器 + W06/空态文案（一次判定）。
- 输入：根。
- 输出：`named`、`empty` 或 None。
- 成功判据：具名行存在，或空容器且有 tab 选中/空态文案。
- 失败处理：解析失败或多列表返回 None。
- 副作用与幂等性：纯判定，幂等。

### W08 判定页面存在弹窗
- 目的：识别系统/运营弹窗，避免在弹窗上继续页面判断。
- 代码位置：`scripts/core/workspace.py:195-210`（`_dialog_signal`）
- 前置条件：根节点已取得。
- 原子动作：排除消息/聊天正文节点后，检查 `dialog|popupwindow` 结构或白名单按钮文案（一次判定）。
- 输入：根。
- 输出：布尔。
- 成功判据：命中结构或可点击/有祖先的按钮文案。
- 失败处理：无。
- 副作用与幂等性：纯判定，幂等。

### W09 匹配白名单弹窗关闭按钮
- 目的：只在“标题 + 关闭按钮”同时出现且按钮唯一时给出点击目标。
- 代码位置：`scripts/core/workspace.py:219-233`（`dismiss_known_dialog` 的判定段；实际点击在 `:234`）
- 前置条件：根节点已取得；白名单见 `scripts/core/workspace.py:36-38`。
- 原子动作：无 ADB；收集文本、匹配标题、经 U08 归并去重（一次判定）。
- 输入：根。
- 输出：唯一可点击按钮或 None（本 ID 只覆盖判定）。
- 成功判据：标题存在、按钮文本存在、去重后恰好 1 个。
- 失败处理：任一不满足即 None，不点击；调用方不得自行猜点。
- 副作用与幂等性：纯判定，幂等。

### W10 判定登录页信号
- 目的：识别登录/验证码页，阻止把登录页当工作区。
- 代码位置：`scripts/core/workspace.py:242-252`（`_login_signal`）
- 前置条件：根节点已取得。
- 原子动作：排除消息正文后，匹配 `{登录, 手机号登录, 验证码登录, 密码登录}` 且节点可点击或有可点击祖先（一次判定）。
- 输入：根。
- 输出：布尔。
- 成功判据：命中即 True。
- 失败处理：无。
- 副作用与幂等性：纯判定，幂等。

### W11 判定聊天详情信号
- 目的：识别聊天详情页。
- 代码位置：`scripts/core/workspace.py:255-258`（`_chat_detail_signal`）
- 前置条件：根节点已取得。
- 原子动作：检查是否存在 `:id/mRecycleView` 或 `:id/et_msg`（一次判定）。
- 输入：根。
- 输出：布尔。
- 成功判据：命中即 True。
- 失败处理：无。
- 副作用与幂等性：纯判定，幂等。

### W12 判定综合推荐页
- 目的：识别“综合推荐”及其固定标签，供导航时按返回键退出。
- 代码位置：`scripts/core/workspace.py:261-264`（`is_recommend_feed`）
- 前置条件：根节点已取得。
- 原子动作：检查文本集合包含“综合推荐”，或包含 `{收藏过我, 看过我, 新职位}`（一次判定）。
- 输入：根。
- 输出：布尔。
- 成功判据：任一条件成立。
- 失败处理：无。
- 副作用与幂等性：纯判定，幂等。

### W13 判定职位详情信号
- 目的：识别职位详情页。
- 代码位置：`scripts/core/workspace.py:267-271`（`_job_detail_signal`）
- 前置条件：根节点已取得。
- 原子动作：资源 ID 末段集合同时包含 `tv_description` 与 `btn_chat|btn_continue_chat`（一次判定）。
- 输入：根。
- 输出：布尔。
- 成功判据：两个条件同时成立。
- 失败处理：无。
- 副作用与幂等性：纯判定，幂等。

### W14 归类当前页面
- 目的：对当前层级与可选 Activity 给出唯一页面类别。
- 代码位置：`scripts/core/workspace.py:274-296`（`classify_page`）
- 前置条件：根节点已取得；Activity 可空。
- 原子动作：按固定优先级调用 W08、W10、Activity 判断、W12、W11、W13、W07、W04（一次判定）。
- 输入：根、`activity`。
- 输出：`Page(kind, reason, target)`。
- 成功判据：返回上述七类之一；无法识别返回 `unknown`。
- 失败处理：不抛错，未知即 `unknown`；`target` 仅主界面或推荐页定位时可能非空。
- 副作用与幂等性：纯判定，幂等。

### W15 判定来源是桌面
- 目的：仅用于报告启动来源，不参与安全门控。
- 代码位置：`scripts/core/workspace.py:348-351`（`_is_desktop`）
- 前置条件：A06 已得到包名。
- 原子动作：包名小写后匹配 `launcher`、`.home` 结尾或 `.home.` 中缀（一次判定）。
- 输入：包名。
- 输出：布尔。
- 成功判据：任一特征命中。
- 失败处理：无。
- 副作用与幂等性：纯判定，幂等。

### E01 累加 ADB 调用指标
- 目的：把一次 ADB 调用记入所有活动事务。
- 代码位置：`scripts/core/events.py:102-105`（`note_adb_call`）
- 前置条件：存在活动事务（否则静默忽略）。
- 原子动作：对活动 monitor 调用 `record_adb_call`，累加次数/耗时/失败数（一次写入）。
- 输入：耗时毫秒、是否成功。
- 输出：无。
- 成功判据：无异常。
- 失败处理：无活动事务时 no-op。
- 副作用与幂等性：修改内存计数；同一次调用不要重复上报。

### E02 累加读屏指标
- 目的：记录一次 UI 层级读取的次数/耗时/失败。
- 代码位置：`scripts/core/events.py:108-111`（`note_screen_read`）
- 前置条件：存在活动事务。
- 原子动作：调用 `record_screen_read`（一次写入）。
- 输入：耗时、是否成功。
- 输出：无。
- 成功判据：无异常。
- 失败处理：无事务时 no-op。
- 副作用与幂等性：内存计数；一次读屏只报一次。

### E03 累加读屏阶段耗时
- 目的：区分 dump、传输、解析成本。
- 代码位置：`scripts/core/events.py:114-117`（`note_screen_read_breakdown`）
- 前置条件：存在活动事务。
- 原子动作：把三段毫秒数累加到事务（一次写入）。
- 输入：`dump_ms`、`transfer_ms`、`parse_ms`。
- 输出：无。
- 成功判据：无异常。
- 失败处理：无事务时 no-op。
- 副作用与幂等性：内存计数。

### E04 标记截图失败
- 目的：让同一事务后续证据带上 `screencap_failed`。
- 代码位置：`scripts/core/events.py:120-123`（`note_screencap_failed`）
- 前置条件：存在活动事务。
- 原子动作：置位 `_screencap_failed`（一次写入）。
- 输入：无。
- 输出：无。
- 成功判据：无异常。
- 失败处理：no-op。
- 副作用与幂等性：布尔置位，幂等。

### E05 标记未读筛选已验证
- 目的：让后续事件携带 `unread_selected/filtered_list_verified`。
- 代码位置：`scripts/core/events.py:126-129`（`note_unread_filter_verified`）
- 前置条件：M01/M02 已由层级证实筛选。
- 原子动作：置位 `_unread_filter_verified`（一次写入）。
- 输入：无。
- 输出：无。
- 成功判据：无异常。
- 失败处理：no-op。
- 副作用与幂等性：布尔置位，幂等。

### E06 截取并保存证据截图
- 目的：在启用 HR 证据的事务中把当前屏幕写入归档目录。
- 代码位置：`scripts/core/events.py:295-310`（`_capture_evidence`、`capture_evidence`）；路径规则 `:73-99`
- 前置条件：`_evidence_enabled` 为 True；截图目录可创建。
- 原子动作：生成递增文件名后调用 A22。
- 输入：`serial`、可选标签。
- 输出：`{"screenshot": path}` 或 `{"screenshot_error": 类型名}`。
- 成功判据：A22 成功。
- 失败处理：只在 `_capture_evidence` 内捕获 `OSError/CalledProcessError` 并返回错误字段；不阻断业务。
- 副作用与幂等性：新增 PNG 文件，文件名递增；不覆盖。

### E07 发布事务事件
- 目的：校验生命周期与字段后写 stdout 与操作日志。
- 代码位置：`scripts/core/events.py:343-417`（`TransactionMonitor.publish`）
- 前置条件：事务状态允许该事件类型。
- 原子动作：补入证据字段、校验白名单、计算索引与指标、`emit`、`log_monitor_event`（一次发布）。
- 输入：事件类型与字段。
- 输出：完整事件字典。
- 成功判据：状态机与字段校验通过。
- 失败处理：非法生命周期或未知字段抛 `ValueError`；证据截图失败只留错误字段。
- 副作用与幂等性：写 stdout/日志并推进状态；同一事件不可重放。

### O01 生成 UI 指纹
- 目的：把 BOSS 节点压缩成可比较结构。
- 代码位置：`scripts/core/obs.py:21-27`（`fingerprint`）
- 前置条件：根可为 None。
- 原子动作：遍历 `package=com.hpbr.bosszhipin` 节点，取文本/clickable/enabled/bounds（一次生成）。
- 输入：根。
- 输出：元组；None 时为空元组。
- 成功判据：始终成功；非法 bounds 记 None。
- 失败处理：无。
- 副作用与幂等性：纯本地，幂等。

### O02 判定目标区域是否变化
- 目的：区分“目标/同卡变化”“别处变化”“无变化”。
- 代码位置：`scripts/core/obs.py:53-87`（`difference`）；比较器 `:38-50`
- 前置条件：前后层级、目标节点均有 BOSS 包与合法 bounds。
- 原子动作：先比整树，再扩展到最近卡片父节点，仅比较落在目标/卡片边界内的节点（一次判定）。
- 输入：before、after、target、`expected`。
- 输出：`changed_at_target`、`changed_elsewhere`、`unchanged` 或 None。
- 成功判据：证据完整时返回上述之一。
- 失败处理：缺包/缺目标 bounds/空指纹返回 None，不猜。
- 副作用与幂等性：纯判定，幂等。

### O03 归档脱敏观测
- 目的：保存前后层级与效果记录，不保存聊天原文。
- 代码位置：`scripts/core/obs.py:119-156`（`archive`）；脱敏 `:95-116`
- 前置条件：动作码与元数据符合安全字符集。
- 原子动作：分配递增序号，独占创建 `.jsonl`，写 before/after 脱敏 XML，再记 `observation_recorded`（一次归档）。
- 输入：前后根、目标、动作/业务动作/通道/差异/效果/等待毫秒/读取次数/截图失败标记。
- 输出：无返回。
- 成功判据：三个文件写入成功且日志记录成功。
- 失败处理：任何异常降级为 `observation_error` 日志，不覆盖业务结果。
- 副作用与幂等性：新增文件（独占创建），不覆盖已有证据；重跑生成新序号。

### L01 追加安全元数据日志
- 目的：记录事件名与白名单标量。
- 代码位置：`scripts/core/operation_log.py:146-153`（`log_event`）；过滤 `:20-88`
- 前置条件：事件名为安全代码。
- 原子动作：生成 UTC 时间与 run_id，过滤字段后调用 L03（一次写入）。
- 输入：事件名、任意字段。
- 输出：无。
- 成功判据：写入成功。
- 失败处理：非法事件名抛 `ValueError`；IO 错误上抛。
- 副作用与幂等性：追加一行，不修改旧行；同一事件重复调用会重复记录。

### L02 追加事务监控事件
- 目的：把已校验的事务事件写入日志，跳过 record 正文。
- 代码位置：`scripts/core/operation_log.py:156-166`（`log_monitor_event`）
- 前置条件：事件含事务必需元数据且时间为 UTC。
- 原子动作：过滤后检查必需键并调用 L03（一次写入）。
- 输入：事件字典。
- 输出：无。
- 成功判据：必需键齐全且时间以 `+00:00` 结尾。
- 失败处理：缺失即抛 `ValueError`，不写半条。
- 副作用与幂等性：追加一行；`record` 类型直接丢弃。

### L03 加锁追加 JSONL
- 目的：保证多进程追加不交错。
- 代码位置：`scripts/core/operation_log.py:123-143`（`_append`）；锁 `:100-121`
- 前置条件：日志父目录可创建。
- 原子动作：打开/创建锁文件，取跨进程独占锁，`O_APPEND` 写完整一行，解锁（一次写入）。
- 输入：字典。
- 输出：无。
- 成功判据：一次写完整 payload。
- 失败处理：`finally` 解锁；写入异常上抛。
- 副作用与幂等性：追加语义；重复调用重复追加。

### P01 读取设备命令占用记录
- 目的：判断设备是否被其他命令占用，或读取恢复所需的占用详情。
- 代码位置：`scripts/hr/store.py:1003-1012`（`device_command`，命令闸门用）；`:1074-1075`（恢复用带所有者字段的查询）
- 前置条件：数据库可打开；表可能不存在。
- 原子动作：一次 SQL 查询（无 ADB）。
- 输入：连接、序列号。
- 输出：行对象或 None。
- 成功判据：查询成功；表不存在时返回 None。
- 失败处理：非“表不存在”的 `OperationalError` 上抛。
- 副作用与幂等性：只读，幂等。

### P02 登记设备命令占用
- 目的：在执行 adb/workspace 命令期间声明本机占用。
- 代码位置：`scripts/hr/store.py:1015-1020`（`reserve_device_command`）
- 前置条件：调用方已处于 `BEGIN IMMEDIATE` 事务且确认无占用（`scripts/hr/store.py:988-1000`）。
- 原子动作：一条 `INSERT` 写 serial/token/nonce/pid/host（一次写入）。
- 输入：连接、序列号、token、nonce。
- 输出：无。
- 成功判据：插入成功。
- 失败处理：唯一约束/数据库错误上抛，命令不执行。
- 副作用与幂等性：新增占用行；token 不同则不可重复登记。

### P03 释放设备命令占用
- 目的：命令结束后删除本人 token 的占用。
- 代码位置：`scripts/hr/store.py:1030-1037`（`release_device_command`）
- 前置条件：持有 token；可选保留取消标记。
- 原子动作：`DELETE ... WHERE serial=? AND token=?`，或把 token 置空（一次写入）。
- 输入：连接、序列号、token、`retain_cancellation`。
- 输出：无。
- 成功判据：执行成功（不校验影响行数）。
- 失败处理：数据库错误上抛。
- 副作用与幂等性：删除本人行；重复执行无额外影响。

### P04 判定占用进程已停止
- 目的：恢复前证明本机 PID 已退出，避免误释放活进程占用。
- 代码位置：`scripts/hr/store.py:1040-1065`（`_process_has_stopped`）
- 前置条件：PID 为 32 位正整数且 host 与本机一致（由 P05 判定）。
- 原子动作：Windows 用 `OpenProcess(SYNCHRONIZE)` + `WaitForSingleObject(0)`；POSIX 用 `os.kill(pid, 0)`（一次判定）。
- 输入：PID。
- 输出：布尔。
- 成功判据：进程句柄可等待为已结束，或 `ProcessLookupError`。
- 失败处理：权限不足/未知错误一律 False（fail closed）。
- 副作用与幂等性：纯判定，幂等。

### P05 校验占用恢复前提
- 目的：只允许恢复“同主机、无派发 nonce、有 token、PID 合法且已停止”的内联占用。
- 代码位置：`scripts/hr/store.py:1068-1086`（`recover_inline_device_command`）
- 前置条件：用户显式 `--confirm-stopped`。
- 原子动作：无 ADB；校验确认位、nonce/token、PID 范围、host、P04 结果（一次判定）。
- 输入：连接、序列号、`confirmed`。
- 输出：恢复结果字典（`unchanged/blocked/released` + reason）。
- 成功判据：全部校验通过则进入 P03 释放并返回 `released`。
- 失败处理：任一不满足返回 `blocked` 且不释放；未确认直接 `ValueError`。
- 副作用与幂等性：只读判定；释放动作在 P03。

### 补充：不产生手机动作的本地原语

以下模块被 core 与命令实现引用，但没有直接 ADB 或 UI 动作，故不分配原子 ID：

- `scripts/core/keys.py:10-71`：空白/大小写归一、标题占位符清理、公司后缀清理、SHA-256 稳定键、64 位十六进制校验；全部纯本地。
- `scripts/core/db.py:13-37`：只读 SQLite 连接、UTC 秒/毫秒时间戳、扫描 ID 生成；不建表、不写入。
- `scripts/core/status.py:5-45`：意图、动作状态、HR 状态、批次状态与 `NON_HR_NAMES` 词表；`NON_HR_NAMES` 在 `scripts/core/message_list.py:281` 参与消息行 `kind` 判定。
- `scripts/core/errors.py:6-49`：`ContractError`/`UiUnverified`/`StateConflict`/`PolicyViolation`/`DispatchStateError` 原因码异常；本节涉及的 core 路径未直接抛出 `UiUnverified`。
- `scripts/core/tasks.py:176-186`：`report_status` 把停止原因映射为 `completed/paused/blocked/error`；`:311-399` 校验派发与报告封闭字段、1KB 上限；只对上层交接生效。
- `scripts/core/events.py:148-232`：摘要模式开关、结果记忆与单行终态输出；属于输出格式，不触达设备。

## 3 组合流程

### 3.1 CLI 命令到原子 ID 的映射

| 命令 | 原子序列（意图） |
| --- | --- |
| `adb inspect` | F01 公共前置 → A05/A06 读前台 → A11/A12/A13/A14 读层级 → U05/U01 解析卡片字段 |
| `adb open-item` | F01 → A11–A14 → U01/A15 → A16 点卡片 → 循环 A11–A14 至多 5 次读详情 |
| `adb scroll` | F01 → A11–A14 → U01 取容器 → A15 → A17 → A11–A14 → U03 比较视口 |
| `adb capture` | F01 → A11–A14 → U05/U01 读详情字段 |
| `adb confirm-identity` | F01 → A11–A14 → U05 读详情并与本地记录比对 |
| `adb continue` | F01 → A11–A14 → U05/W14 核详情 → A15/A16 点沟通 → A11–A14/W14 循环核验（必要时 W09/A16 处理二次确认） |
| `workspace ensure` | F02 |
| `device recover` | F03 |

### 3.2 F01 adb 命令公共前置
1. P01：在设备闸门事务内确认无占用（`scripts/main.py:1623-1628`）。
2. P02：登记本次命令占用（`scripts/hr/scan.py:761-773`）。
3. A02 → A03：读取设备清单并选定唯一已授权设备。
4. 命令主体（见上表）。
5. P03：命令退出时按 token 释放占用（`scripts/hr/scan.py:797-801`）。

### 3.3 F02 workspace ensure
1. F01 的 P01/P02/A02/A03，但设备错误只映射为 `device_error` 结果，不抛给 CLI（`scripts/core/workspace.py:455-461`）。
2. A05 一次读窗口；A07 判解锁；A06 取前台组件（`scripts/core/workspace.py:464-473`）。
3. 若前台包不是 BOSS：W15 判来源 → A08 解析入口 → A09 启动 → 以 0.5 秒间隔循环 A11–A14 + W14 至多 20 次等待 BOSS 页面（`scripts/core/workspace.py:475-490`）。
4. 若前台是 BOSS：直接 A11–A14 + W14（`scripts/core/workspace.py:491-495`）。
5. 弹窗处理：W08 判弹窗 → W09 匹配按钮 → A15/A16 点击 → A11–A14 重读 → 至多 2 次（`scripts/core/workspace.py:354-370, 497`）。
6. 若 W14 判为 `recommend_feed`：先 W05 找底部“消息”（`scripts/core/workspace.py:499-503`）；找不到则 A18(4) 返回 + A11–A14 + W14，至多 2 次（`scripts/core/workspace.py:504-533`）。
7. 若 W14 判为聊天详情/职位详情：A18(4) 返回 + A11–A14 + W14，再重新走第 6 步或第 8 步（`scripts/core/workspace.py:535-552`）。
8. 主界面路径：A11–A14 复核 → W08/W09 清弹窗 → W05 取“消息”目标 → A15/A16 点击 → 以 0.5 秒间隔循环 A11–A14 + W14 + W06 至多 8 次，要求 `chat_list` 且 selected=true（`scripts/core/workspace.py:373-444`）。
9. 已在 `chat_list` 时直接返回 reached（`scripts/core/workspace.py:554-557`）。
10. 任何断连经 A04 收敛为 `device_error`（`scripts/core/workspace.py:447-452`）。

### 3.4 F03 device recover（离线）
1. P05 校验 `--confirm-stopped`、nonce/token、PID 范围与 host。
2. P01 读取占用行；无行返回 `unchanged/no_reservation`。
3. P04 判定 PID 已停止；未证明停止返回 `blocked`。
4. P03 释放并返回 `released`（`scripts/hr/store.py:1073-1086`）。

### 3.5 F04 设置未读筛选
1. A11–A14 → M02；面板不可见时先用 U07/U08 定位“筛选”并用 A15/A16 点击（`scripts/core/message_list.py:125-135, 89-122`）。
2. A20 取点击前摘要 → A16 点“筛选” → A21 判变化（`scripts/core/message_list.py:65-86`）。
3. 变化后 A11–A14 → M02 确认面板；未稳定则 0.5 秒间隔重读，至多 4 次（`scripts/core/message_list.py:102-122`）。
4. 面板内若有其他已选筛选项则终止（`scripts/core/message_list.py:136-141`）。
5. U07 定位“未读” → 若未选中则 A15/A16 点击 → A11–A14 复核 selected（`scripts/core/message_list.py:142-161`）。
6. A15/A16 点“确定” → A11–A14 复核面板关闭且列表可解析 → E05（`scripts/core/message_list.py:162-184`）。

### 3.6 F05 清除筛选
1. A11–A14 → U04 找“取消筛选”；不存在即视为无筛选（`scripts/core/message_list.py:187-201`）。
2. A20 取摘要 → A15/A16 点击 → A21 判变化（截图失败则等待 0.4 秒）。
3. A11–A14 → U04 再次确认“取消筛选”已消失。

### 3.7 F06 消息列表滑动
1. A11–A14 → M01/M04 确认唯一可读列表。
2. U01 取列表 bounds；`scrollable` 不为 true 则结束（`scripts/core/message_list.py:336-343`）。
3. 取 `x=(l+r)//2`、上下界为容器高度的 25%/68%，按方向决定起点/终点（`scripts/core/message_list.py:344-349`）。
4. A17 滑动 450 ms → A11–A14 → M04 → U03 比较前后签名（`scripts/core/message_list.py:350-362`）。
5. 层级证明模式可省截图门控，但仍每次重验列表（`scripts/core/message_list.py:309-325`）。

### 3.8 F07 返回消息列表
1. A11–A14 → M01；成功即结束（`scripts/core/message_list.py:365-372`）。
2. 失败时循环至多 3 次：A18(4) → 等待 0.6 秒 → A11–A14 → M01（`scripts/core/message_list.py:376-388`）。
3. 3 次仍失败返回 False，不无限按返回。

## 4 数据与副作用边界

- **操作日志**：固定写入仓库根 `data/operations.log`（`scripts/core/operation_log.py:17`），同目录 `.lock` 做跨进程锁（`:100-111`）；每行一个 JSON，只允许白名单标量字段，`record/contract/messages/chat/resume/reply/xml/credential/password/token` 等键直接丢弃（`:20-46, :49-88`）；事务 `record` 事件不落日志（`:156-159`）。`run_id` 每进程一个（`:18`）。
- **截图**：根目录 `screenshots/<本地日期>/round-<8位>|transaction-<8位>/NN-<label>.png`，编号取已有最大序号 +1，不覆盖（`scripts/core/events.py:87-99`）。HR 证据只在 `enable_hr_evidence` 后截取，并清理同操作 10 分钟前的旧证据（`scripts/core/events.py:271-285, 306-310`）。
- **观测归档**：`screenshots/<日期>/<round|transaction-id>/NN-<action>-effects.jsonl` 与同名 `.before.xml/.after.xml`，以独占创建避免并发覆盖；XML 只保留 BOSS 包节点、文本做 SHA-256 摘要（白名单控制文案除外）（`scripts/core/obs.py:95-116, 119-148`）。
- **监督事件**：stdout 一行一个 JSON（摘要模式下只保留终态一行），同时经 L02 落日志；事件携带 ADB 次数/耗时/失败、读屏次数/耗时/失败与三段耗时（`scripts/core/events.py:343-417`）。
- **数据库**：业务库固定 `data/jobs.sqlite3`（`scripts/core/db.py:10`）；`connect_read_only` 以 `mode=ro`（可加 `immutable=1`）打开，不建库、不迁移（`:13-22`）。设备命令占用存于 `hr_device_commands`，含 serial/token/dispatch_nonce/owner_pid/owner_host/cancelled（`scripts/hr/store.py:1003-1037`）。
- **手机侧写入**：唯一固定路径是 `/sdcard/job_tracker_ui.xml`，每轮先删后写（`scripts/core/adb.py:212-216`）；截图与点击不落手机文件。文本输入会临时切换输入法并在结束后恢复（`scripts/hr/actions.py:733-735, 859-871`）。
- **待采集临时文件**：`adb open-item` 会写 `PENDING_RECORD_PATH`（`scripts/jobs/enroll.py:440-441`），属 jobs 层，不在 core。
- **契约与密钥**：core 不读取 `config.json`、不读取设备凭据；`keys.make_key` 只做 SHA-256 摘要并要求非空输入（`scripts/core/keys.py:61-65`）；`tasks` 报告上限 1KB、字段封闭（`scripts/core/tasks.py:36, 591-596`）。

## 5 未覆盖与不确定项

1. **`--any-device` 实际未生效**：解析器定义了 `--any-device`（`scripts/main.py:217`），但 `_recover_device` 只把 `args.serial or ""` 传给恢复函数（`scripts/main.py:1640`），该开关没有进入任何判定；用空串查询只会命中 `serial=''` 的占用。
2. **恢复只支持同主机内联占用**：host 必须等于本机（`scripts/hr/store.py:1081`），带 `dispatch_nonce` 的占用直接 `blocked/dispatch_reservation`（`:1079-1080`），跨机器或派发占用无法通过该命令恢复。
3. **`window_state` 的解锁语义偏保守**：只要 `dumpsys window` 没有输出 `isKeyguardShowing`，集合为空就不等于 `{"false"}`，会被判为未解锁（`scripts/core/adb.py:162-164`）。不同 ROM/Android 版本是否稳定输出该字段未验证。
4. **`screen_action` 把“变化但未稳定”当成功**：`if not observed["stable"] or observed["changed"]: return True`（`scripts/core/adb.py:350-351`），因此画面仍在动画时也可能返回 True；重试边界只有 2 次。
5. **`wait_for_screen` 的 `changed` 依赖旧摘要**：未传 `previous_hash` 时永远是 False（`scripts/core/adb.py:328`）；超时返回的 `stable=False` 与 `changed` 组合语义需要调用方自行解释。
6. **长按通道未验证真实响应**：`tap_bounds` 的 `long_press` 用同点 `swipe` 800 ms、`input_swipe` 用同点 60 ms（`scripts/core/adb.py:258-262`），代码注释明确“不保证 BOSS 控件响应”，实际 UI 行为未知。
7. **层级 dump 使用固定路径**：`/sdcard/job_tracker_ui.xml` 是全局固定文件（`scripts/core/adb.py:212`），两个进程即使有数据库占用闸门，若绕过 CLI 直接调用 core 仍会互相覆盖；`uiautomator dump` 退出码 137 只重试一次（`:214-222`）。
8. **无文本输入原子操作在 core**：core 没有 `input text`/粘贴函数；现有文本输入完全位于 `hr/actions.py`，依赖外装 ADBKeyboard 广播（`scripts/hr/actions.py:705, 733-735, 755-768`）。重新设计时需决定输入层归属与失败恢复策略。
9. **未读判定存在两种证据源**：层级角标存在即 `yes`（`scripts/core/message_list.py:269`），与像素阈值 `>=100`（`:278-279`）可能不一致；`unread_filter_verified=True` 时所有行都被标 `yes`（`:269`），无法区分个别已读行。
10. **无名字但可点击的消息行**：会被写成空字段占位记录并标 `kind=unknown`（`scripts/core/message_list.py:252-257`），其真实含义（分隔符/系统卡/加载占位）未在代码中说明。
11. **页面分类可能误判**：`_dialog_signal` 把 `同意/允许/确定` 等通用按钮文案当作弹窗信号（`scripts/core/workspace.py:31-34, 207-209`），虽有“排除消息正文”保护（`:189-210`），但职位详情等页面出现同名按钮时仍可能归类为 `popup`。
12. **`workspace` 在 chat_list 分支上再次导航**：从聊天详情返回后若已是 `chat_list`，代码仍定位“消息”目标并调用 `_navigate_from_main`（`scripts/core/workspace.py:549-552`），即在列表页再点一次“消息”tab；该路径的真实 UI 效果与必要性无法从代码确认。
13. **`_navigate_from_main` 的页面变更比较较宽**：`confirmed.kind != page.kind` 在 `recommend_feed` 归为 `unknown` 时比较的是 `unknown` 与后续 `chat_list`，`page_changed` 之外的推荐页路径依赖额外分支（`scripts/core/workspace.py:393-399`）；实际机型表现未验证。
14. **屏幕几何依赖顶层节点 `top==0`**：`_screen_bounds` 要求候选 bounds 左上为 (0,0) 且唯一（`scripts/core/workspace.py:65-78`）；带刘海/状态栏偏移或根节点非全屏的层级会直接放弃底部导航定位。
15. **`obs.difference` 的“同卡”扩展规则**：只有父节点满足 `card` 命名或同时含“同意/拒绝”、且非 recycler 时才扩展到父级边界（`scripts/core/obs.py:62-79`）；不满足时只按目标 bounds 比较，可能把同卡其他字段变化判为 `changed_elsewhere`。
16. **`tasks`/`status`/`errors` 与设备层无直接调用关系**：`report_status` 只是把停止原因映射为四态（`scripts/core/tasks.py:176-186`），`status.py` 提供词表（如 `NON_HR_NAMES` 被 `message_list.py:281` 使用），`errors.py` 的 `UiUnverified` 在本节涉及的 core/命令路径中未被引用（`scripts/core/errors.py:16-22`）；它们对设备动作的约束只体现在上层流程。
17. **`adb continue` 缺少独立确认开关**：该命令只要求 `--batch-id` 与 `--db`（`scripts/main.py:234-236`），点击“继续沟通”是直接设备动作；是否有上层批次/派发门控不在本节范围内，重新设计时需明确确认边界。
18. **参考资料冲突**：`scripts/core/AGENTS.md` 与 `scripts/AGENTS.md` 仅作背景，本节所有结论以代码为准；本文未运行任何 Python、未连接设备、未读取 `config.json`/`data/`/`screenshots/`/数据库/日志，因此上述真机行为均属静态推断。
