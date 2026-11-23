# BOSS直聘手机客户端原始操作原子化分析

> 文件用途：把旧项目 `scripts/` 的四份静态阅读章节（设备与页面、职位、HR 读取、HR 动作）合并为一份按原子 ID 组织的操作手册。
> 本稿只做编辑、去重、编号与交叉引用整理，不新增章节以外的技术断言；所有技术结论均保留原章节的 `file:line` 证据。

## 0 阅读说明与范围

### 0.1 来源与未做的事

- 本文全部结论来自对旧项目 `scripts/` 的**静态阅读**：`scripts/main.py`、`scripts/core/`、`scripts/jobs/`、`scripts/hr/`、`scripts/heartbeat/`。
- 本次合并**未运行任何 Python 代码、未执行任何 ADB 命令、未连接手机**，也未读取旧项目的 `config.json`、`data/`、`screenshots/`、数据库、日志或任何凭据。
- 行号以本次阅读时的文件副本为准；旧项目后续变更后需重新定位函数名再核对行号。
- 本文是**操作语义与判据的整理**，不构成对真实手机的执行授权。当前仓库没有业务 CLI、SQLite 运行时记忆或设备执行器；任何真实发送、删除、清理、心跳都必须另行取得授权并按本机规则确认。

### 0.2 “原子操作”定义

- **一个 ID = 一次可观察动作，或一次判定。**
  - *判定*：只读层级/像素/本地库，不产生手机副作用；失败时给出原因码而不是猜。
  - *动作*：产生手机或本机副作用（点击、滑动、按键、截图、写库、写日志），必须写清是否可幂等。
- **复合步骤只允许用 ID 串联表达**，不得为“一整套流程”新造一个原子 ID。
- 为避免每次重复长链，本文定义两个**固定序列**（成员 ID 固定，不构成新原子）：
  - **读屏序列** = `CORE-A10` → `CORE-A11` → `CORE-A12` → `CORE-A13` → `CORE-A14`。正文中“读屏”一律指该五步串联。
  - **返回列表序列** = 判定 `CORE-M01`；失败则 `CORE-A18` + 等待 0.6 秒 + 读屏序列 + `CORE-M01`，最多 3 轮。正文中“返回列表序列”一律指该链。
- 其余组合（批次循环、推荐流程、清理流程、心跳轮次）在**第 6 节**按用户可见任务给出完整 ID 链。

### 0.3 ID 域与唯一权威

| 前缀 | 域 | 所在小节 | 内容 |
| --- | --- | --- | --- |
| `CORE-A` | 设备与 ADB | 1.1–1.4 | 设备选择、启动、读屏、点击、滑动、按键、截图、传输围栏 |
| `CORE-U` | UI 树解析 | 1.2 | bounds、遍历、签名、文本定位、资源 ID、证据截断 |
| `CORE-M` | 消息列表 | 1.5 | 行解析、未读证据、列表滑动、归端、筛选、返回 |
| `CORE-W` | 页面识别与导航 | 2 | 页面信号、优先级归类、底部导航、弹窗与登录 |
| `CORE-I` | 文本输入 | 1.6 | 输入法切换、清空、广播写入、发送控件定位 |
| `CORE-E`/`O`/`L`/`P` | 事务、观测、日志、占用 | 1.4、1.7、1.8 | 指标、证据截图、脱敏归档、JSONL 日志、设备占用闸门 |
| `JOBS-P`/`JOBS-A` | 职位浏览与沟通 | 3 | 卡片/详情/批次判定与职位域点击 |
| `HRR-R` | HR 消息读取 | 4 | 未读/已读读取、会话身份、聊天解析、契约 |
| `HRA-P`/`HRA-A` | HR 动作、清理、心跳 | 5 | 发送/交换/简历/删除/草稿/心跳状态机 |

- 编号统一为两位数字，例如 `CORE-A23`、`JOBS-P18`、`HRR-R37`、`HRA-A41`；域内连续编号，新增 ID 追加在末尾，不复用已删除编号。
- **读屏、点击、滑动、返回键、截图只由 `CORE-` 定义**，是唯一权威。其它章节删除重复定义，改写为“调用 `CORE-Axx`”，并保留该业务的定位规则、业务判据与失败码。
- 正文引用格式为反引号包裹的完整 ID（如 `CORE-W14`）；第 9 节给出源码文件到 ID 的对照。

### 0.4 脱敏与占位符约定

- 所有路径一律相对仓库根（如 `scripts/core/adb.py`、`data/jobs.sqlite3`、`screenshots/<本地日期>/…`）；不写本机绝对路径。
- 设备标识一律写作 `<serial>`；包名 `com.hpbr.bosszhipin` 与输入法 `com.android.adbkeyboard/.AdbIME` 是公开常量，不是凭据。
- 四份草稿合并前已逐份扫描本机绝对路径、用户名、设备序列号、真实姓名/联系方式、公司-聊天关联、密钥与令牌：**均未发现**，故无需替换原文；本次仍统一了占位符写法（见 0.4 前两条）。
- 第 7 节的数据库/日志/截图只描述结构与本机相对位置，不复述任何业务原文。

### 0.5 阅读顺序

0 说明 → 1 设备原语 → 2 页面识别 → 3/4/5 三个业务域 → 6 组合流程 → 7 副作用与不可逆动作 → 8 不确定项 → 9 索引。

## 1 设备与环境原语（CORE）

### 1.1 环境前提、连接与启动

- `adb` 在 PATH；`adb devices` 至少一台状态为 `device` 的已授权设备；多台须显式 `--serial`（`scripts/core/adb.py:110-147`）。
- 手机已开启 USB 调试并由使用者在手机上授权本机；`unauthorized`/`offline` 分别映射 `device_unauthorized`/`device_disconnected`（`scripts/core/adb.py:127-141`）。
- 解锁只认 Android 窗口服务明确报告 `isKeyguardShowing=false`；未出现该字段也按未解锁处理（`scripts/core/adb.py:162-164, 180-184`）。
- 普通 ADB 命令超时 15 秒（`scripts/core/adb.py:91`），设备清单 5 秒（`:114`），截图类 3 秒（`:281, :302, :363`）。
- 层级 dump 依赖 `uiautomator dump` 与 `/sdcard/job_tracker_ui.xml` 可写（`scripts/core/adb.py:212-216`）。
- 包名硬编码 `com.hpbr.bosszhipin`（`scripts/core/adb.py:19`；`scripts/core/obs.py:14` 另有同名常量）；启动组件每次解析，不写死（`scripts/core/adb.py:187-202`）。
- `adb` 与 `workspace` 命令执行前都经过设备占用闸门：`scripts/main.py:1609-1633`（`_dispatch`）→ `scripts/hr/scan.py:750-801`（`device_command`），占用冲突以退出码 2 拒绝。

#### CORE-A01 运行单条 ADB 命令
- 代码：`scripts/core/adb.py:81-107`（`run_adb`）
- 动作：`adb [-s <serial>] <args...>`，一次子进程，`check=True`、`capture_output=True`，15 秒超时，UTF-8。
- 判据/失败：退出码 0；记录 `adb_failed`/`adb_failures`，命中 `CORE-A04` 即抛 `DeviceDisconnected`；已有序列号且远端命令非零退出时只调用 `CORE-A03` 探测一次连接，**绝不重放原命令**。
- 副作用：写操作日志与事务指标；手机侧是否变化取决于具体子命令，`CORE-A01` 本身不可假定幂等。

#### CORE-A02 读取设备清单
- 代码：`scripts/core/adb.py:110-120`（`select_device` 内调用）
- 动作：`adb devices`，超时 5 秒。
- 判据/失败：退出码 0；断连按 `CORE-A04` 抛出，其他子进程错误原样上抛。
- 副作用：只读，幂等。

#### CORE-A03 判定单一已授权设备
- 代码：`scripts/core/adb.py:121-147`（`select_device`）
- 判定：跳过首行，按空白切分取状态为 `device` 的序列号；显式串号必须为 `device`，未指定时必须恰好 1 台（一次判定）。
- 失败：`unauthorized`/`offline` 给专属原因码；0 台或多台抛错；不重试。
- 副作用：纯判定，幂等。

#### CORE-A04 判定 ADB 错误是否断连
- 代码：`scripts/core/adb.py:39-52`（`raise_if_disconnected`）
- 判定：`TimeoutExpired`、stderr 命中 `no devices/emulators found|device offline|unauthorized|not found|error: closed|protocol fault|transport error` 等视为断连；普通远端命令非零退出**不**视为断连。
- 输出：不抛即“非断连”，抛 `DeviceDisconnected(reason)` 即“断连”；自身不重试。
- 副作用：纯判定，幂等。

#### CORE-A05 读取窗口状态
- 代码：`scripts/core/adb.py:150-165`（`window_state`）
- 动作：`adb -s <serial> shell dumpsys window`，正则解析 `mCurrentFocus` 与全部 `isKeyguardShowing`。
- 输出/判据：`(component|None, unlocked: bool)`；退出码 0；解析失败时 component 为 None、unlocked 为 False，不抛解析异常。
- 副作用：只读，幂等。

#### CORE-A06 判定前台组件
- 代码：`scripts/core/adb.py:167-172`（`current_component`）、`:175-177`（`current_activity`）
- 判定：读取 `CORE-A05` 的 component；必须匹配 `包名/Activity`（一次判定）。
- 失败：None 时抛 `RuntimeError("无法确认手机当前前台页面")`；不重试。
- 副作用：纯判定，幂等。

#### CORE-A07 判定已解锁
- 代码：`scripts/core/adb.py:180-184`（`verify_unlocked`）
- 判定：`dumpsys window` 中所有 `isKeyguardShowing` 取值集合必须恰为 `{"false"}`；零命中、含 `true`、无法解析均判未解锁（一次判定）。
- 失败：抛错；不重试、不自动解锁。
- 副作用：纯判定，幂等。

#### CORE-A08 解析启动入口
- 代码：`scripts/core/adb.py:187-195`（`resolve_launcher_component`）
- 动作：`adb -s <serial> shell cmd package resolve-activity --brief com.hpbr.bosszhipin`。
- 判据：取最后一个非空行，整体匹配 `com.hpbr.bosszhipin/<[\w.$]+>`；不匹配即抛错，不猜 Activity。
- 副作用：只读，幂等。

#### CORE-A09 启动或切回 App
- 代码：`scripts/core/adb.py:198-202`（`launch_app`）
- 动作：`adb -s <serial> shell am start -n <component>`。
- 判据/失败：退出码 0；页面是否就绪由 `CORE-W14` 判定，不由此步保证；启动失败原样抛出，调用方转 `launcher_unresolved`，不重试。
- 副作用：改变前台任务栈；重复执行通常回到同一入口，但不保证保留原页面位置。

#### CORE-A23 在占用围栏内启动 ADB 子进程
- 代码：`scripts/core/adb.py:55-78`（`_run_transport`）；围栏变量 `scripts/core/adb.py:21`
- 动作：在 `ContextVar` 围栏内 `Popen` 一次，再在锁外 `communicate(timeout)`；围栏未设置时退化为普通 `subprocess.run`。
- 判据/失败：进程结束且 `check` 通过；任何异常都 kill + wait 子进程后原样抛出。
- 副作用：一次进程启动；不可重入由调用方保证。

### 1.2 层级读取与 UI 树解析

#### CORE-A10 删除旧层级 dump 文件
- 代码：`scripts/core/adb.py:212-213`（`read_hierarchy`）
- 动作：`adb -s <serial> shell rm -f /sdcard/job_tracker_ui.xml`。
- 判据/失败：退出码 0（文件不存在时 `rm -f` 仍为 0）；错误上抛。
- 副作用：删除手机侧固定路径临时文件；幂等。

#### CORE-A11 生成 UI 层级 dump
- 代码：`scripts/core/adb.py:214-222`（`read_hierarchy`）
- 动作：`adb -s <serial> shell uiautomator dump /sdcard/job_tracker_ui.xml`。
- 判据/失败：退出码 0；仅退出码 137 时最多重试一次（总 2 次），第二次仍 137 抛 `RuntimeError`，其他退出码立即抛出。
- 副作用：覆盖手机侧固定路径文件；重试覆盖同文件，视为幂等。

#### CORE-A12 读取 dump XML 文本
- 代码：`scripts/core/adb.py:225-229`（`read_hierarchy`）
- 动作：`adb -s <serial> shell cat /sdcard/job_tracker_ui.xml`。
- 判据/失败：退出码 0；错误上抛，不重试。
- 副作用：只读，幂等。

#### CORE-A13 解析 XML 层级
- 代码：`scripts/core/adb.py:230-237`（`read_hierarchy`）
- 动作：本地 `ET.fromstring`（一次解析）。
- 判据/失败：解析成功；`ParseError` 转 `RuntimeError("Android UI 层级不是有效 XML")`，不重试。
- 副作用：纯本地，幂等。

#### CORE-A14 判定层级属于 BOSS
- 代码：`scripts/core/adb.py:238-239`（`read_hierarchy`）
- 判定：树中必须存在 `package='com.hpbr.bosszhipin'` 的节点。
- 失败：否则抛“当前界面不是 BOSS 直聘”，读屏失败指标 +1。
- 副作用：纯判定，幂等。

#### CORE-U01 解析节点 bounds
- 代码：`scripts/core/uitree.py:9-14`（`get_bounds`）
- 判定：`bounds` 必须整体匹配 `[l,t][r,b]` 且四个非负整数。
- 失败：不匹配抛 `ValueError`，调用方决定跳过或终止。
- 副作用：纯本地，幂等。

#### CORE-U02 列举节点及后代
- 代码：`scripts/core/uitree.py:17-19`（`descendants`）
- 动作：`list(node.iter("node"))`，含自身。
- 判据：始终成功；只读，幂等。

#### CORE-U03 生成屏幕签名
- 代码：`scripts/core/uitree.py:22-28`（`screen_signature`）
- 判定：遍历所有非空 `text` 节点，取 `(resource-id, text, bounds)` 有序元组；空文本节点不参与。
- 用途：证明视口是否变化（滑动、归端、返回后的比较基准）。
- 副作用：只读，幂等。

#### CORE-U04 按文本定位唯一点击节点
- 代码：`scripts/core/uitree.py:31-41`（`clickable_text`）
- 判定：找文本完全相等的节点，向上归并到最近 `clickable=true` 祖先并去重；去重后必须恰好 1 个。
- 失败：0 个或多个都返回 `None`，不点击。`未读` 另允许唯一 `:id/tv_option_name` 文本节点（`scripts/core/message_list.py:45-57`）。
- 副作用：纯判定，幂等。

#### CORE-U05 读取资源 ID 后缀文本
- 代码：`scripts/core/uitree.py:44-49`（`node_value`）；`scripts/core/message_list.py:209-216`（`_text_by_id`）为同语义列表内实现
- 判定：遍历自身及后代，匹配 `resource-id` 后缀，取第一个非空文本；缺失为空串。
- 副作用：只读，幂等。

#### CORE-U06 生成有界 UI 证据
- 代码：`scripts/core/uitree.py:52-57`（`bounded_ui_evidence`）
- 动作：序列化整棵树并按 UTF-8 字节截断（默认 8192 字节）。
- 输出：`(evidence, truncated)`；序列化异常由调用方处理。
- 副作用：纯本地，幂等。

#### CORE-U07 查找唯一文本节点
- 代码：`scripts/core/message_list.py:30-32`（`nodes_with_text`）
- 判定：遍历并比较 `text.strip()` 完全相等，返回节点列表（0/1/多由调用方判定）。
- 副作用：只读，幂等。

#### CORE-U08 查找最近可点击且启用的祖先
- 代码：`scripts/core/message_list.py:35-42`（`clickable_ancestor`）
- 判定：沿父链上溯，返回第一个 `clickable=true` 且 `enabled=true` 的节点。
- 失败：到根仍未命中返回 `None`，调用方拒绝点击。
- 副作用：纯判定，幂等。

### 1.3 坐标、手势与按键

#### CORE-A15 计算节点中心坐标
- 代码：`scripts/core/adb.py:249-255`（`tap_bounds`）
- 动作：`x=(left+right)//2`、`y=(top+bottom)//2`（一次计算）。
- 判据：坐标落在 bounds 内（整数除法天然满足，除非输入异常）；不校验 `right>left`/`top<bottom`，由 `CORE-U01` 兜底。
- 副作用：纯计算，幂等。

#### CORE-A16 点击坐标
- 代码：`scripts/core/adb.py:256-257`（`tap_bounds`）
- 动作：`adb -s <serial> shell input tap <x> <y>`，坐标为 `CORE-A15` 结果。
- 判据/失败：ADB 退出码 0；UI 是否响应不在本步保证范围；错误上抛不重试，需重试必须重新读屏并重新定位。
- 副作用：可能改变页面；不可假定幂等。

#### CORE-A17 滑动
- 代码：`scripts/core/adb.py:265-268`（`swipe`）
- 动作：`adb -s <serial> shell input swipe <x1> <y1> <x2> <y2> <duration_ms>`；默认 450 ms，长按通道 800 ms/1200 ms，同点短滑 60 ms。
- 判据/失败：ADB 退出码 0；视口是否变化由 `CORE-U03` 或截图门控比较；错误上抛不重试。
- 副作用：可能改变列表位置；不可假定幂等。同点长按是否被目标控件接受属未验证项（见 8.1）。

#### CORE-A18 发送按键
- 代码：`scripts/core/adb.py:271-273`（`keyevent`）
- 动作：`adb -s <serial> shell input keyevent <code>`；现有唯一调用为返回键 `4`。
- 判据/失败：退出码 0；错误上抛不重试；连发会产生额外层级回退，调用方必须限额。
- 副作用：可能返回页面/关闭弹层/触发“再按一次退出”；不可假定幂等。

### 1.4 截图、画面判定与证据

#### CORE-A19 读取原始 RGBA 截图
- 代码：`scripts/core/adb.py:276-294`（`read_screen_raw`）
- 动作：`adb -s <serial> exec-out screencap`，超时 3 秒。
- 判据：头 ≥16 字节、宽高非 0、像素格式为 1（RGBA）、`len(raw)-16 == width*height*4`；不合法抛 `RuntimeError("无法读取有效的原始截图")`。
- 副作用：只读，幂等；不落盘。

#### CORE-A20 计算截图 SHA-256
- 代码：`scripts/core/adb.py:297-308`（`screencap_hash`）
- 动作：`adb -s <serial> exec-out screencap -p`，超时 3 秒，对 stdout 计算 SHA-256。
- 判据/失败：退出码 0；错误上抛不重试。
- 副作用：只读；同一画面摘要稳定，但受时钟、动画、通知影响。

#### CORE-A21 等待截图稳定或变化
- 代码：`scripts/core/adb.py:311-331`（`wait_for_screen`）
- 动作：重复 `CORE-A20`；连续两次相同即 `stable`；超时返回最后一次观测。
- 输入/输出：`timeout_ms`/`interval_ms` 为 1–30000 整数（默认 3000/200）；输出 `{hash, changed, polls, elapsed_ms, stable}`，`changed` 仅在提供 `previous_hash` 时可能为 True。
- 失败：截图异常直接抛出；超时不抛错，返回 `stable=False`。
- 副作用：只读；轮询次数取决于画面与时限。

#### CORE-A22 保存 PNG 截图
- 代码：`scripts/core/adb.py:355-369`（`capture_screenshot`）
- 动作：`adb [-s serial] exec-out screencap -p`，stdout 字节写入本机 `path`。
- 判据/失败：退出码 0 且写文件成功；ADB/OS 错误上抛不重试。
- 副作用：覆盖同名文件；同画面重复写内容一致。

#### CORE-E06 截取并保存证据截图
- 代码：`scripts/core/events.py:295-310`（`_capture_evidence`、`capture_evidence`）；路径规则 `:73-99`
- 动作：启用 `_evidence_enabled` 时生成递增文件名后调用 `CORE-A22`。
- 输出/判据：`{"screenshot": path}` 或 `{"screenshot_error": 类型名}`；成功即 `CORE-A22` 成功。
- 失败：只在内部捕获 `OSError/CalledProcessError` 并返回错误字段，不阻断业务。
- 副作用：新增 PNG（`screenshots/<本地日期>/round-<8位>|transaction-<8位>/NN-<label>.png`），编号取已有最大序号 +1，不覆盖；HR 证据只在 `enable_hr_evidence` 后截取，并清理同操作 10 分钟前旧证据（`scripts/core/events.py:271-285, 306-310`）。

### 1.5 消息列表读取、滑动、归端与筛选

#### CORE-M01 解析消息行
- 代码：`scripts/core/message_list.py:239-290`（`parse_message_list`）
- 判定：定位唯一 `:id/recyclerView`，逐行读取 `tv_name/tv_position/tv_msg/tv_time_v2/badge_view` 与 `[...]` 预览标记。
- 输出：行字典列表（`name/position/message/time/unread/unread_count/kind/preview_event`）；无姓名但可点击的行输出占位记录。
- 失败：列表数量不为 1 抛 `RuntimeError`；像素尺寸不符抛 `ValueError`。纯本地，幂等。
- 未读证据：无像素时仅“本轮已证实未读筛选”或角标存在（有子节点或数字文本）判 `yes`，否则 `unknown`（`scripts/core/message_list.py:264-269`）。

#### CORE-M02 判定筛选面板可见
- 代码：`scripts/core/message_list.py:60-62`（`_filter_panel_visible`）
- 判定：两次 `CORE-U07`，“未读”和“确定”文本都至少出现一次。
- 副作用：纯判定，幂等。

#### CORE-M03 统计矩形内红色像素
- 代码：`scripts/core/message_list.py:219-236`（`count_red_pixels`）
- 判定：逐像素比较 `(255,71,83)`，每通道容差默认 30；矩形可越界，函数会裁剪。
- 输出/判据：命中像素数；`>=100`（常量 `UNREAD_RED_PIXEL_THRESHOLD`，同文件第 19 行）由 `CORE-M01` 判为未读。
- 失败：宽度 ≤0 或长度不整除 `width*4` 抛 `ValueError`。纯本地，幂等。

#### CORE-M04 核验唯一可读消息列表
- 代码：`scripts/core/message_list.py:293-306`（`_verified_message_list`）
- 判定：调用 `CORE-M01`，再调用 `CORE-W14` 并检查 kind 为 `chat_list`。
- 失败：不可识别或非 `chat_list` 抛 `RuntimeError`，**绝不当作“已到顶”**。
- 副作用：纯判定，幂等。

#### CORE-M05 判定消息列表可见
- 代码：`scripts/core/message_list.py:396-403`（`message_list_visible`）
- 判定：`CORE-M01` 后至少一行 `name` 非空。
- 失败：`CORE-M01` 抛错时经 `CORE-A04` 分类并返回 False。
- 副作用：纯判定，幂等。

#### CORE-M06 消息列表滑动一次
- 代码：`scripts/core/message_list.py:328-364`（`scroll_messages`）
- 前置：唯一 `:id/recyclerView`（`:336-338`）；`scrollable == "true"`（`:342-343`）；bounds 宽高为正。
- 动作：调用 `CORE-A17` 450 ms；`x` 取列表水平中点，`upper = top + 0.25×高度`，`lower = top + 0.68×高度`；`up` 为 lower→upper，`down` 反之。
- 判据：默认路径用截图门控（`scripts/core/adb.py:334-352`）；`hierarchy_only` 路径比较滑动前后 `CORE-U03`，并强制重新确认列表（`scripts/core/message_list.py:309-325`）。
- 失败：连续两次证明未变化返回 `changed=False`；列表不可识别抛 `RuntimeError`，不滑动。
- 副作用：改变列表视口，不可幂等。**合并说明**：本节取代原 core 组合流程 F06、职位草稿 A15、HR 读取草稿 R19、HR 动作草稿 A05 中的重复定义。

#### CORE-M07 证明列表已到端点
- 代码：`scripts/hr/scan.py:1294`（`_seek_list_top`），包装 `1309`（`_rewind_read_list`），上限 `1285`（`SEEK_TOP_LIMIT=30`）；清理侧 `scripts/hr/cleanup.py:256-266`、`727-736`（`SEEK_NEWEST_LIMIT=30`）
- 动作：以 `CORE-M06` 的 `hierarchy_only` 路径按方向循环滑动，每次比较是否仍变化。
- 输出/判据：`(尝试次数, root|None, proven)`；出现一次 `changed=False` 即证明；30 次仍未证明则未证明。
- 失败：未证明时 root 为 None，调用方 `paused/read_incomplete`（`scripts/hr/scan.py:2201-2202`）或 `list_read_failed`（`scripts/hr/cleanup.py:565`）。
- 副作用：把列表滚到最新端；成功后重复调用返回 `count=1`，幂等。**合并说明**：取代 HR 读取草稿 R20 与 HR 动作草稿 G01。

#### CORE-M08 设置未读筛选
- 代码：`scripts/core/message_list.py:125`（`set_unread_filter`）；打开面板 `89`（`_open_filter_panel`），点击 `65`（`_filter_action`）/`45`（`_click_unique`）
- 前置：存在“筛选”入口且当前无“取消筛选”；面板内无其他已选条件（`scripts/core/message_list.py:136-141`）。
- 动作：面板可见性用 `CORE-M02` 判定；三次独立点击（“筛选”→“未读”→“确定”），每次用 `scripts/core/adb.py:334-352` 的截图门控确认画面变化；坐标统一来自 `CORE-U01` + `CORE-A15`/`CORE-A16`。
- 判据：“取消筛选”出现、“确定”消失，且 `CORE-M01(unread_filter_verified=True)` 成功（`:173-181`）。
- 失败：`FilterEntryAbsent` 由调用方降级为底部角标兜底（`scripts/hr/scan.py:2166-2184`）；`FilterActionUnchanged` 直接 `paused/filter_unverified`（`2135-2136`）；其余 RuntimeError 最多重试 2 轮（`2097-2142`）。
- 副作用：改变列表筛选状态并重排列表；成功后入口已变，不可重复点击。**合并说明**：取代原 core F04 与 HR 读取草稿 R21。

#### CORE-M09 清除筛选
- 代码：`scripts/core/message_list.py:187`（`clear_filter_from_root`），便捷入口 `204`（`clear_unread_filter`）；HR 复核侧 `scripts/hr/review.py:460-465`
- 动作：`CORE-U04` 定位“取消筛选”，一次 `CORE-A16` 点击，再读屏复核文本消失。
- 判据/失败：复读后无“取消筛选”即成功；无入口视为已清除返回 True；点击未生效返回 False → `paused/filter_clear_unverified`。
- 副作用：改变列表筛选状态；可重入，已清除时幂等。**合并说明**：取代原 core F05、HR 读取草稿 R22、HR 动作草稿 F02。

### 1.6 文本输入（实现位于 `scripts/hr/actions.py`，编号归 CORE 域）

> 本组是设备侧文本输入的**唯一权威**定义，实现不在 `scripts/core/`，因此旧 core 章节称其为“core 之外”。职位、HR 读取、HR 动作三章不再重复定义输入法与广播步骤。

#### CORE-I01 读取当前默认输入法
- 代码：`scripts/hr/actions.py:716-718`
- 动作：`adb -s <serial> shell settings get secure default_input_method`。
- 判据/失败：非空且不为 `null`；否则抛错并停止发送。只读，幂等。

#### CORE-I02 启用并切换 ADBKeyboard
- 代码：`scripts/hr/actions.py:733-735`
- 动作：已读取 `ime list -s`；`com.android.adbkeyboard/.AdbIME` 未启用时先 `ime enable`，再 `ime set`。
- 判据/失败：两条命令退出码 0；异常经 `CORE-A04` 分类后上抛。
- 副作用：改变系统输入法；切换后输入框会失焦，必须重新点击（`CORE-I06`）。

#### CORE-I03 清空输入框
- 代码：`scripts/hr/actions.py:739-740, 746-747, 759-760`
- 动作：`adb -s <serial> shell am broadcast -a ADB_CLEAR_TEXT`，等待 0.2 秒。
- 判据：重新读屏后输入框文本属于 `""/"回复消息"/"介绍自己回复率更高哦~"/"和Ta聊一聊~"`。
- 失败：两次仍非空返回 `needs_draft_clear`，不点击发送。
- 副作用：清空输入框；重复执行结果相同。

#### CORE-I04 广播写入文本
- 代码：`scripts/hr/actions.py:755-774`（编码与广播在 755、768）
- 动作：`adb -s <serial> shell am broadcast -a ADB_INPUT_B64 --es msg <base64>`（UTF-8 正文的 base64），随后等待并回读。
- 判据：读屏后输入框 `text` 与正文完全相等。
- 失败：最多两轮，第二轮前重新点击输入框并清空；仍失败返回 `not_sent`，不点击发送。
- 副作用：覆盖输入框内容；重复写入同一正文结果相同。

#### CORE-I05 恢复原输入法
- 代码：`scripts/hr/actions.py:851-876`（`finally`）
- 动作：`ime set <original_ime>`；若 ADBKeyboard 原本未启用再 `ime disable com.android.adbkeyboard/.AdbIME`。
- 判据/失败：命令退出码 0；已点击发送时恢复失败上抛；未点击时只记 `input_method_restore_failed`，不改变“未发送”证据。
- 副作用：恢复系统设置；重复执行同值。

#### CORE-I06 点击聊天输入框
- 代码：`scripts/hr/actions.py:738`（首次）、`:766`（写入失败后补点）
- 前置：`CORE-I07` 已确认唯一输入框；**切换输入法后必须重新点击**（`:736-738`）。
- 动作：调用 `CORE-A15` + `CORE-A16`，坐标取输入框 bounds 中心。
- 判据/失败：点击本身不构成成功证据，由后续回读判定；输入框不唯一时按调用阶段返回 `needs_send_control`，不点击。
- 副作用：切换输入焦点；不产生对外消息。

#### CORE-I07 判定唯一输入框
- 代码：`scripts/hr/actions.py:727-730`、`741-753`
- 判定：`resource-id` 以 `editText_with_scrollbar` 结尾的节点必须恰好 1 个。
- 输出/失败：唯一节点；数量不为 1 时返回 `needs_send_control`/`needs_draft_clear`（按调用阶段），不继续输入。
- 副作用：只读判定，幂等。

#### CORE-I08 定位唯一发送控件
- 代码：`scripts/hr/actions.py:417-459`（`resource_send_control`）、`778-780`
- 判定：从输入框父节点逐层向上，在“位于输入框右侧且纵向重叠”的可点击节点中优先 `Button` 类，其次文本/描述含“发送”，最后 `ImageView`；同一层取最深且唯一者。
- 失败：`None` 时返回 `not_sent`，不点击。
- 副作用：只读判定，幂等。

### 1.7 事务、观测与日志

#### CORE-E01 累加 ADB 调用指标
- 代码：`scripts/core/events.py:102-105`（`note_adb_call`）
- 动作：对活动 monitor 调用 `record_adb_call`，累加次数/耗时/失败数；读屏与阶段耗时分别由 `CORE-E02`、`CORE-E03` 记录，截图失败与筛选验证由 `CORE-E04`、`CORE-E05` 置位。
- 失败：无活动事务时 no-op；同一调用不要重复上报。
- 副作用：修改内存计数。

#### CORE-E02 累加读屏指标
- 代码：`scripts/core/events.py:108-111`（`note_screen_read`）
- 动作：调用 `record_screen_read`，记录次数/耗时/失败。
- 失败：无事务时 no-op；一次读屏只报一次。副作用：内存计数。

#### CORE-E03 累加读屏阶段耗时
- 代码：`scripts/core/events.py:114-117`（`note_screen_read_breakdown`）
- 动作：把 `dump_ms/transfer_ms/parse_ms` 累加到事务。
- 失败：无事务时 no-op。副作用：内存计数。

#### CORE-E04 标记截图失败
- 代码：`scripts/core/events.py:120-123`（`note_screencap_failed`）
- 动作：置位 `_screencap_failed`，让同一事务后续证据带上 `screencap_failed`。
- 失败：no-op。副作用：布尔置位，幂等。

#### CORE-E05 标记未读筛选已验证
- 代码：`scripts/core/events.py:126-129`（`note_unread_filter_verified`）
- 动作：置位 `_unread_filter_verified`，让后续事件携带 `unread_selected/filtered_list_verified`。
- 失败：no-op。副作用：布尔置位，幂等。

#### CORE-E07 发布事务事件
- 代码：`scripts/core/events.py:343-417`（`TransactionMonitor.publish`）
- 动作：补入证据字段、校验白名单、计算索引与指标、`emit`，再经 `CORE-L02` 落日志。
- 判据/失败：状态机与字段校验通过；非法生命周期或未知字段抛 `ValueError`，证据截图失败只留错误字段。
- 副作用：写 stdout/日志并推进状态；同一事件不可重放。

#### CORE-O01 生成 UI 指纹
- 代码：`scripts/core/obs.py:21-27`（`fingerprint`）
- 判定：遍历 `package=com.hpbr.bosszhipin` 节点，取文本/clickable/enabled/bounds；根为 None 时返回空元组，非法 bounds 记 None。
- 副作用：纯本地，幂等。

#### CORE-O02 判定目标区域是否变化
- 代码：`scripts/core/obs.py:53-87`（`difference`）；比较器 `:38-50`
- 判定：先比整树，再扩展到最近卡片父节点，仅比较落在目标/卡片边界内的节点。
- 输出：`changed_at_target`、`changed_elsewhere`、`unchanged` 或 `None`。
- 失败：缺包/缺目标 bounds/空指纹返回 `None`，不猜。比较口径：文本、clickable、enabled 必须完全相同，bounds 每坐标允许 ±8 像素。
- 副作用：纯判定，幂等。

#### CORE-O03 归档脱敏观测
- 代码：`scripts/core/obs.py:119-156`（`archive`）；脱敏 `:95-116`
- 动作：差异先由 `CORE-O02` 判定，再分配递增序号，独占创建 `.jsonl`，写 before/after 脱敏 XML，再记 `observation_recorded`。
- 判据/失败：三个文件写入成功且日志记录成功；任何异常降级为 `observation_error` 日志，不覆盖业务结果。
- 副作用：新增 `screenshots/<日期>/<round|transaction-id>/NN-<action>-effects.jsonl` 与同名 `.before.xml/.after.xml`；XML 只保留 BOSS 包节点、文本做 SHA-256 摘要（白名单控制文案除外）。

#### CORE-L01 追加安全元数据日志
- 代码：`scripts/core/operation_log.py:146-153`（`log_event`）；过滤 `:20-88`
- 动作：生成 UTC 时间与 run_id，过滤字段后调用 `CORE-L03`。
- 判据/失败：事件名为安全代码；非法事件名抛 `ValueError`，IO 错误上抛。
- 副作用：追加一行，不修改旧行；同一事件重复调用会重复记录。

#### CORE-L02 追加事务监控事件
- 代码：`scripts/core/operation_log.py:156-166`（`log_monitor_event`）
- 动作：过滤后检查必需键并调用 `CORE-L03`；`record` 类型直接丢弃。
- 判据/失败：必需键齐全且时间以 `+00:00` 结尾；缺失即抛 `ValueError`，不写半条。
- 副作用：追加一行。

#### CORE-L03 加锁追加 JSONL
- 代码：`scripts/core/operation_log.py:123-143`（`_append`）；锁 `:100-121`
- 动作：打开/创建锁文件，取跨进程独占锁，`O_APPEND` 写完整一行，`finally` 解锁。
- 判据/失败：一次写完整 payload；写入异常上抛。
- 副作用：追加语义；重复调用重复追加。日志固定写仓库根 `data/operations.log`（`:17`），同目录 `.lock` 做锁；只允许白名单标量字段，`record/contract/messages/chat/resume/reply/xml/credential/password/token` 等键直接丢弃（`:20-46, :49-88`）。

### 1.8 设备命令占用（本机数据库，不触达 ADB）

#### CORE-P01 读取设备命令占用记录
- 代码：`scripts/hr/store.py:1003-1012`（`device_command`）；`:1074-1075`（恢复用带所有者字段的查询）
- 判定：一次 SQL 查询；表不存在时返回 None。
- 失败：非“表不存在”的 `OperationalError` 上抛。只读，幂等。

#### CORE-P02 登记设备命令占用
- 代码：`scripts/hr/store.py:1015-1020`（`reserve_device_command`）
- 前置：调用方已处于 `BEGIN IMMEDIATE` 事务且确认无占用（`scripts/hr/store.py:988-1000`）。
- 动作：一条 `INSERT` 写 serial/token/nonce/pid/host。
- 判据/失败：插入成功；唯一约束/数据库错误上抛，命令不执行。
- 副作用：新增占用行；token 不同则不可重复登记。

#### CORE-P03 释放设备命令占用
- 代码：`scripts/hr/store.py:1030-1037`（`release_device_command`）
- 动作：`DELETE ... WHERE serial=? AND token=?`，或把 token 置空（保留取消标记）。
- 判据/失败：执行成功（不校验影响行数）；数据库错误上抛。
- 副作用：删除本人行；重复执行无额外影响。

#### CORE-P04 判定占用进程已停止
- 代码：`scripts/hr/store.py:1040-1065`（`_process_has_stopped`）
- 判定：Windows 用 `OpenProcess(SYNCHRONIZE)` + `WaitForSingleObject(0)`；POSIX 用 `os.kill(pid, 0)`。
- 判据/失败：句柄可等待为已结束，或 `ProcessLookupError`；权限不足/未知错误一律 False（fail closed）。
- 副作用：纯判定，幂等。

#### CORE-P05 校验占用恢复前提
- 代码：`scripts/hr/store.py:1068-1086`（`recover_inline_device_command`）
- 前置：用户显式 `--confirm-stopped`。
- 判定：校验确认位、nonce/token、PID 范围（32 位正整数）、host 与本机一致、`CORE-P04` 结果。
- 输出/失败：`unchanged/blocked/released` + reason；任一不满足返回 `blocked` 且不释放；未确认直接 `ValueError`。
- 副作用：只读判定；释放动作在 `CORE-P03`。命令 `device recover` 不走设备闸门，也不触达 ADB（`scripts/main.py:1636-1647`）。

## 2 页面识别与导航（CORE-W）

### 2.1 页面定义与信号

页面常量见 `scripts/core/workspace.py:15-21`：

- `main_page`：BOSS 主界面，底部可见至少两个已知标签（`职位/消息/发现/我的`）。
- `chat_list`：消息聊天列表，唯一 `:id/recyclerView`，有具名行或可证实的空态。
- `chat_detail`：聊天详情，出现 `:id/mRecycleView` 或 `:id/et_msg`，或前台 Activity 含 `ChatRoomActivity`。
- `job_detail`：职位详情，同时出现 `tv_description` 与 `btn_chat|btn_continue_chat`，或前台 Activity 含 `BossJobPagerActivity`。
- `popup`：弹窗结构或白名单按钮文案。
- `login`：登录文案且不在消息正文区。
- `unknown`：其余，包含 `recommend_feed`（“综合推荐”或“收藏过我/看过我/新职位”三标签）。

#### CORE-W01 判定屏幕边界
- 代码：`scripts/core/workspace.py:65-78`（`_screen_bounds`）
- 判定：候选节点必须 `left==0 && top==0 && right>left && bottom>top`，去重后唯一才采用。
- 失败：0 个或多个返回 None，后续底部判定放弃。纯判定，幂等。

#### CORE-W02 计算底部导航阈值
- 代码：`scripts/core/workspace.py:81-87`（`_bottom_threshold`）
- 动作：`top + int((bottom-top)*0.7)`，即屏幕下方 30% 的上边界。
- 失败：`CORE-W01` 失败时返回 None。纯计算，幂等。

#### CORE-W03 收集底部文本节点
- 代码：`scripts/core/workspace.py:90-104`（`_bottom_nodes`）
- 判定：遍历非空文本节点，经 `CORE-U01` 解析后 `top >= threshold` 即收集；非法 bounds 跳过。
- 失败：阈值 None 时返回空列表。只读，幂等。

#### CORE-W04 收集底部标签集合
- 代码：`scripts/core/workspace.py:107-110`（`_bottom_labels`）
- 判定：取文本属于 `{职位, 消息, 发现, 我的}` 的集合。
- 用途：主界面判定要求至少 2 个已知标签。纯判定，幂等。

#### CORE-W05 定位底部标签可点击容器
- 代码：`scripts/core/workspace.py:113-142`（`locate_bottom_tab_target`）；`:145-147`（`locate_message_target`）
- 判定：以 `CORE-W02` 的阈值和 `CORE-W03` 收集的底部节点为范围，找文本相等的标签，经 `CORE-U08` 归并去重；必须恰好 1 个可点击容器。
- 失败：返回 `*_missing` 或 `*_ambiguous` 原因码，不点击。纯判定，幂等。

#### CORE-W06 读取消息标签选中态
- 代码：`scripts/core/workspace.py:150-160`（`message_tab_selected`）
- 判定：目标自身及后代中任一 `selected=true` 即 True；仅 `false` 即 False；都没有即 None。
- 失败：定位失败返回 None，不推断。纯判定，幂等。

#### CORE-W07 判定聊天列表状态
- 代码：`scripts/core/workspace.py:169-186`（`_chat_list_state`）
- 判定：要求唯一 `:id/recyclerView`；`CORE-M01` 解析；具名行→`named`；无具名行时要求空容器 + `CORE-W06`/空态文案→`empty`；否则 None。
- 失败：解析失败或多列表返回 None。纯判定，幂等。

#### CORE-W08 判定页面存在弹窗
- 代码：`scripts/core/workspace.py:195-210`（`_dialog_signal`）
- 判定：先收集消息列表与聊天详情容器内所有节点 id，排除这些正文节点后，检查 `dialog|popupwindow` 结构或白名单按钮文案（可点击或有可点击祖先）。
- 副作用：纯判定，幂等。

#### CORE-W09 匹配白名单弹窗关闭按钮
- 代码：`scripts/core/workspace.py:219-233`（`dismiss_known_dialog` 判定段）
- 前置：白名单见 `scripts/core/workspace.py:36-38`。
- 判定：标题存在、按钮文本存在、经 `CORE-U08` 归并去重后恰好 1 个可点击按钮。
- 失败：任一不满足即 None，不点击；调用方不得自行猜点。**本 ID 只覆盖判定**，点击由 `CORE-W18` 执行。

#### CORE-W10 判定登录页信号
- 代码：`scripts/core/workspace.py:242-252`（`_login_signal`）
- 判定：排除消息正文后，匹配 `{登录, 手机号登录, 验证码登录, 密码登录}` 且节点可点击或有可点击祖先。
- 副作用：纯判定，幂等。

#### CORE-W11 判定聊天详情信号
- 代码：`scripts/core/workspace.py:255-258`（`_chat_detail_signal`）
- 判定：存在 `:id/mRecycleView` 或 `:id/et_msg`。
- 副作用：纯判定，幂等。

#### CORE-W12 判定综合推荐页
- 代码：`scripts/core/workspace.py:261-264`（`is_recommend_feed`）
- 判定：文本集合含“综合推荐”，或含 `{收藏过我, 看过我, 新职位}`。
- 副作用：纯判定，幂等。

#### CORE-W13 判定职位详情信号
- 代码：`scripts/core/workspace.py:267-271`（`_job_detail_signal`）
- 判定：资源 ID 末段集合同时包含 `tv_description` 与 `btn_chat|btn_continue_chat`。
- 副作用：纯判定，幂等。

#### CORE-W14 归类当前页面
- 代码：`scripts/core/workspace.py:274-296`（`classify_page`）
- 判定优先级：弹窗 → 登录 → `ChatRoomActivity` → `BossJobPagerActivity` → 综合推荐 → 聊天详情 → 职位详情 → 聊天列表 → 底部标签 ≥2 → unknown（依次调用 `CORE-W08`、`CORE-W10`、Activity 判断、`CORE-W12`、`CORE-W11`、`CORE-W13`、`CORE-W07`、`CORE-W04`）。
- 输出/失败：`Page(kind, reason, target)`；不抛错，未知即 `unknown`；`target` 仅主界面或推荐页定位时可能非空。
- 副作用：纯判定，幂等。**这是页面分类的唯一权威定义**，其余章节只调用本 ID。

#### CORE-W15 判定来源是桌面
- 代码：`scripts/core/workspace.py:348-351`（`_is_desktop`）
- 判定：包名小写后匹配 `launcher`、`.home` 结尾或 `.home.` 中缀；仅用于报告启动来源，不参与安全门控。
- 副作用：纯判定，幂等。

### 2.2 导航动作与到达核验

#### CORE-W16 点击底部“消息”入口
- 代码：`scripts/core/workspace.py:373-415`（`_navigate_from_main`），点击 `:404-415`
- 前置：`CORE-W14` 判为主界面且 `CORE-W05` 定位成功；点击前重新读屏复核页面未变（`:379-395`）。
- 动作：`CORE-A15` + `CORE-A16`，坐标取定位容器 bounds 中心。
- 判据/失败：ADB 点击命令成功返回；读屏失败返回 `read_incomplete`，页面变化返回 `page_changed`，无 target 返回 `message_target_missing`；不重试点击。
- 副作用：一次点击，非幂等，必须随后执行 `CORE-W17`。

#### CORE-W17 核验已到达消息列表
- 代码：`scripts/core/workspace.py:418-444`（`_navigate_from_main` 轮询段）；`_POST_CLICK_WAIT_ATTEMPTS=8`（`:40`）
- 动作：循环“等待 0.5 秒 + 读屏序列 + `CORE-W14`”，最多 8 次。
- 判据：`kind == chat_list` 且 `CORE-W06` 为 True（`:431-435`）。
- 失败：选中为 False 记 `message_tab_unselected`，未知记 `message_tab_selection_missing`；进入详情/职位/弹窗/登录直接 `unrecognized`；耗尽返回 `navigation_failed`。
- 副作用：只读轮询；不重复点击。

#### CORE-W18 关闭已知白名单弹窗
- 代码：`scripts/core/workspace.py:213-234`（`dismiss_known_dialog`）；调用点 `scripts/hr/scan.py:1224`（`_dismiss_popup_after_open`）、`1249`；重试上限 `_DISMISS_DIALOG_LIMIT=2`（`scripts/hr/scan.py:42`）
- 前置：`CORE-W08` 已发现弹窗信号。
- 动作：`CORE-W09` 匹配唯一按钮 → `CORE-A15`/`CORE-A16` 点击一次 → 重读层级。
- 判据：标题与按钮文本同时存在、唯一可点击祖先存在、点击命令成功返回。
- 失败：不匹配/无祖先/点击异常返回 False 且不点击；弹窗消失后重复调用返回 False，可重入。
- 副作用：一次点击；**不得用于白名单以外的弹窗**，未知弹窗应按 `popup` 暂停人工处理。
</content>


## 3 职位浏览与沟通（JOBS）

### 3.0 范围、入口与前置状态

入口全部集中在 `scripts/main.py` 的参数字典与三个分派函数：

| 命令组 | 子命令 | 分派位置 | 是否操作设备 |
| --- | --- | --- | --- |
| `jobs` | `init`/`add`/`list`/`discoveries`/`check-continue`/`mark-continued`/`mark-applied`/`blacklist` | `scripts/main.py:170-196`、`scripts/main.py:551-643`（`run_jobs`） | 否，纯本地库 |
| `jobs` | `recommend capture`/`recommend apply` | `scripts/main.py:198-210`、`scripts/main.py:553-581` | 是（聊天页推荐弹层） |
| `adb` | `inspect`/`open-item`/`scroll`/`capture`/`confirm-identity`/`continue` | `scripts/main.py:220-241`、`scripts/main.py:646-674`（`run_adb`） | 是（职位列表、详情页） |
| `batch` | `start`/`status`/`close`/`active`/`probe`/`resolve`/`resolve-share`/`run`/`cooldown` | `scripts/main.py:242-273`、`scripts/main.py:677-811`（`run_batch`） | `probe`、`run` 是；其余为本地库或仅门禁 |

涉及文件：

- `scripts/jobs/store.py`：职位表、发现事件、列表别名、黑名单、沟通/投递状态与沟通前置门禁（纯本地）。
- `scripts/jobs/resources.py`：从 `docs/求职展示资源.md` 读取批次正文并校验 URL（纯本地，`scripts/jobs/resources.py:11-21`）。
- `scripts/jobs/enroll.py`：列表卡片读取、详情页读取、滚动、打开卡片、入库、身份核对、继续沟通。
- `scripts/jobs/recommend.py`：聊天页推荐卡识别、弹层打开、候选解析、判定校验、推荐职位沟通。
- `scripts/jobs/batch.py`：批次主循环、新职位通知入口、列表恢复、底部 tab 导航、批次检查点。
- `scripts/jobs/batch_store.py`：批次、批次职位、资源消息发送状态、每日配额与冷却（纯本地）。
- 复用核心层：`CORE-A`/`U`/`M`/`W` 与 `CORE-I`。

涉及页面：消息列表（含“发布了新职位”通知行）、综合推荐页（含“新职位”标签）、职位列表（`*:id/recycler_view` 或 `*:id/rv_list`）、职位详情页（`tv_description` + `btn_chat`/`btn_continue_chat`）、聊天详情页（`mRecycleView`/`et_msg`）、推荐弹层（“活跃职位列表” + `rv_recommend_job_list`）、二次确认弹窗（“温馨提示” + “是否就新职位…” + “沟通新职位”）。

前置状态：

1. 唯一一台已授权设备；多设备必须显式 `--serial`（`scripts/core/adb.py:110-147`）。
2. 前台为 BOSS 直聘（`scripts/core/adb.py:19`）；`adb inspect` 显式校验 `current_activity` 以包名开头（`scripts/jobs/enroll.py:370-372`）。
3. 页面停在其中之一：消息列表、职位列表、职位详情页、待处理聊天页（`scripts/jobs/batch.py:508-512` 的报错文案给出允许集合）。
4. 批次类操作要求存在 `status='active'` 的批次且存在可预留名额（`scripts/jobs/enroll.py:645-648`、`scripts/jobs/batch_store.py:577-583`）。
5. 继续沟通要求职位已入库、身份已核对、未沟通、未投递（`scripts/jobs/store.py:411-423`）。
6. 资源消息正文必须含至少一个 URL 且不重复（`scripts/jobs/resources.py:18-20`）。
7. 本域所有判定均只读；`读屏` 指 `CORE-A10`–`CORE-A14` 序列，`点击` 指 `CORE-A15`+`CORE-A16`。

### 3.1 判定（JOBS-P）

#### JOBS-P01 职位卡片提取
- 代码：`scripts/jobs/enroll.py:107-142`（`visible_job_cards`）
- 判定：以 `CORE-U02` 遍历 `clickable="true"` 节点，用 `CORE-U05` 读取 `tv_position_name`、`tv_salary_statue`、`tv_company_name`，三者皆非空；标题去掉首尾 `@ % 空格 &`。
- 输出：整卡 bounds、标题节点 bounds、公司名节点 bounds；缺任一字段的节点不算卡片。
- 副作用：只读，幂等。

#### JOBS-P02 列表可视区
- 代码：`scripts/jobs/enroll.py:166-174`（`listing_viewport`）
- 判定：唯一一个 `scrollable="true"` 且 resource-id 以 `recycler_view` 或 `rv_list` 结尾的容器；没有则返回 None（None 视为全部可见）。
- 副作用：只读，幂等。

#### JOBS-P03 卡片完整可见
- 代码：`scripts/jobs/enroll.py:177-185`（`card_fully_visible`）
- 判定：整卡四边都在可视区内，且标题下沿 `< 可视区下沿 - 24`。
- 副作用：只读，幂等。

#### JOBS-P04 卡片阻断
- 代码：`scripts/jobs/enroll.py:351-363`（`blocked_card_reason`）
- 判定顺序：标题/公司为空 → 公司黑名单（可选开关）→ 同标题同公司的已入库职位中已沟通或已投递 → 同标题同公司命中多条；返回第一个原因文本，否则 None。
- 副作用：只读（查本地库），幂等。

#### JOBS-P05 详情字段与详情页判定
- 代码：`scripts/jobs/enroll.py:188-263`（`detail_fields`，断言在 `:200-204`）
- 判定：必须有非空 `tv_description`，且存在 `btn_chat`/`btn_continue_chat` 之一，否则抛 `RuntimeError("当前页面不是可识别的职位详情页")`；随后读取标题、薪资、公司、招聘者、地点、正文。
- 副作用：只读，幂等。**这是职位详情页判定的唯一权威定义**；`CORE-W13` 是页面归类用的同义信号。

#### JOBS-P06 详情完整度
- 代码：`scripts/jobs/enroll.py:253-254`
- 判定：正文不含“查看更多”，且不以 `…` 或 `...` 结尾即为完整。
- 副作用：只读，幂等。

#### JOBS-P07 详情身份一致
- 代码：`scripts/jobs/enroll.py:341-348`（`detail_identity_matches`）
- 判定：标题规范化后相等；公司规范化后相等，或该职位已有列表别名（公司不可见时）。
- 副作用：只读，幂等。

#### JOBS-P08 沟通按钮唯一可用
- 代码：`scripts/jobs/enroll.py:694-696`、`scripts/jobs/recommend.py:364-366`
- 判定：`resource-id` 以 `btn_chat` 结尾、`clickable="true"`、文本在 `{立即沟通, 继续沟通}` 内的节点必须恰好 1 个，否则不点击。
- 副作用：只读，幂等。

#### JOBS-P09 沟通结果三证据
- 代码：`scripts/jobs/enroll.py:591-620`（`_confirm_continue_page`）
- 判定：同一屏文本同时满足“含『由你发起的沟通』”+“某文本按 `title_matches_chat` 匹配职位标题”+“某文本按 `company_matches_chat` 匹配公司”。
- 时序：最多读 3 次、间隔 1.0 秒。
- 副作用：只读，幂等。

#### JOBS-P10 二次确认“是否就新职位继续沟通”弹窗
- 代码：`scripts/jobs/enroll.py:566-572`（`_confirm_continue_dialog`）
- 判定：文本同时含“温馨提示”“是否就新职位…”“沟通新职位”。
- 副作用：只读，幂等。

#### JOBS-P11 推荐卡存在
- 代码：`scripts/jobs/recommend.py:24-27`（`detect_card`）
- 判定：`ll_recommend_job_container` 容器内存在 `tv_recommend_job_name`。
- 副作用：只读，幂等。

#### JOBS-P12 推荐弹层已打开
- 代码：`scripts/jobs/recommend.py:209-212`（`_sheet_open`）；`open_sheet` 内同义判定 `:46-54`
- 判定：存在 `tv_active_jobs_title`，且 `rv_recommend_job_list` 恰好 1 个。
- 副作用：只读，幂等。

#### JOBS-P13 候选行解析
- 代码：`scripts/jobs/recommend.py:58-90`
- 判定：`rv_recommend_job_list` 必须唯一；只取“标题与公司都非空”的直属子节点行，按出现顺序编号 1..n；标签容器内跳过 `visible-to-user="false"` 节点。
- 副作用：只读，幂等。

#### JOBS-P14 “新职位”标签及选中态
- 代码：`scripts/jobs/batch.py:221-234`（`_new_job_tab`）
- 判定：页面是综合推荐页，且文本等于“新职位”的节点恰好 1 个；选中态读取 `selected`/`isSelected`/`is-selected` 三种属性，全无则返回 None（未知）。
- 副作用：只读，幂等。

#### JOBS-P15 新职位通知行
- 代码：`scripts/jobs/batch.py:292-296`；行解析在 `scripts/core/message_list.py:239-290`（`CORE-M01`）
- 判定：消息列表行中，行名包含“发布了新职位”的第一行即目标。
- 副作用：只读，幂等。

#### JOBS-P16 批次推进门禁
- 代码：`scripts/jobs/batch.py:387-403`、`scripts/jobs/batch_store.py:285-301`（`reconciliation_reason`）
- 判定顺序：批次存在 → 已完成 → 已关闭 → 存在 `pending`/`reserved`（→`needs_reconciliation`）→ 存在未收敛资源消息（→`needs_resource_share_review`）→ 时间预算 → 每日配额。
- 副作用：只读，幂等。

#### JOBS-P17 聊天联系人一致（标题包含匹配）
- 代码：`scripts/hr/chat.py:885-890`（`verify_contact`）
- 判定：聊天页 `tv_title` 文本集合必须逐字包含预期 HR 名。
- 调用方：仅 `scripts/hr/actions.py`、`scripts/hr/apply.py`、`scripts/hr/review.py`、`scripts/jobs/enroll.py`；读取侧 `scan.py`/`sync.py` 不用（口径差异见 8.3）。
- 副作用：只读，幂等。

#### JOBS-P18 资源消息回显计数基线
- 代码：`scripts/hr/actions.py:722-726`；发送后同口径计数 `:818-825`
- 判定：统计 `tv_content_text` 且文本与正文完全相等的节点数，作为发送前基线；发送后计数增加即视为已发送。
- 副作用：只读，幂等。

### 3.2 动作（JOBS-A）

#### JOBS-A01 职位列表滑动一次
- 代码：`scripts/jobs/enroll.py:448-477`（`scroll_step`）；底层层 `CORE-A17`
- 前置：存在 `scrollable="true"` 且 resource-id 以 `recycler_view`/`rv_list` 结尾的容器，多于 1 个时报错（`scripts/jobs/enroll.py:453-463`）。
- 动作：调用 `CORE-A17` 450 ms；`x` 为容器水平中点，`upper = top + 0.25×高度`，`lower = top + 0.68×高度`；`up` 时 start=lower、end=upper，`down` 相反；滑动后等待 0.5 秒（`:472`）。
- 判据：滑动前后 `CORE-U03` 签名不同。
- 失败：无容器抛 `RuntimeError("当前页面没有可识别的职位列表滚动容器")`，不重试；`changed=False` 由调用方转“反向扫描”或“没有更多职位”（`scripts/jobs/batch.py:489-505`、`:450-459`）。
- 副作用：视口位置变化，不可幂等；不改变业务状态。

#### JOBS-A02 点击职位列表卡片打开详情
- 代码：`scripts/jobs/enroll.py:385-446`（`open_item_step`），点击 `:408`
- 前置：读屏后 `1 ≤ index ≤ 卡片数`（`:390-392`）；`JOBS-P03` 完整可见（否则 `not_fully_visible`，`:395-398`）；`JOBS-P04` 无阻断（黑名单不参与，`:400-405`）。
- 动作：调用 `CORE-A15`+`CORE-A16`，坐标取该卡片 `company_bounds`（公司名节点 bounds），缺失时回退整卡 bounds（`:136-138`）。
- 判据：点击后最多 5 次读屏、每次间隔 0.4 秒，`JOBS-P05` 成功解析出详情字段（`:412-420`）。
- 失败：5 次仍不可读返回 `open_failed` 并附最多 8 KiB 脱敏层级证据（`:416`、`:421-426`，证据由 `CORE-U06` 生成）；批次中连续 3 次打开失败即停批（`scripts/jobs/batch.py:467-478`）。
- 副作用：页面跳转到详情页；重复点击重复跳转，不重复写库。

#### JOBS-A03 点击推荐卡“查看更多”打开推荐弹层
- 代码：`scripts/jobs/recommend.py:30-55`（`open_sheet`），点击 `:43`
- 前置：`CORE-W14` 为 `chat_detail`（`scripts/jobs/recommend.py:129-130`）；`JOBS-P11` 成立；`CORE-U04` 对“查看更多”返回唯一可点击节点（`:32-34`）。
- 动作：调用 `CORE-A16`；点击前校验该节点下沿落在某张推荐卡容器内并与其水平方向重叠（`:36-42`）。
- 判据：点击后最多读屏 3 次，出现 `tv_active_jobs_title` 文本“活跃职位列表”且 `rv_recommend_job_list` 存在（`:46-54`）。
- 失败：找不到“查看更多”、层级校验不通过、点击抛错、3 次读屏都不符 → 统一 `sheet_unverified`，捕获流程以 `paused` 收尾（`:34`、`:42`、`:55`）。
- 副作用：弹层覆盖在当前聊天页上；弹层已开时找不到“查看更多”，直接判失败。

#### JOBS-A04 点击推荐弹层关闭控件
- 代码：`scripts/jobs/recommend.py:265-298`，点击 `:289`
- 前置：读屏序列完成；若因批次忙而关闭，还要求 `JOBS-P12` 成立（`:270-271`）。
- 动作：按固定优先级选唯一可点击节点——`iv_close_job_panel` → `btn_confirm` → 文本“我知道了”/“知道了”的可点击祖先（`:273-287`），再 `CORE-A16`。
- 判据：点击后再读一次屏，`tv_active_jobs_title` 与 `rv_recommend_job_list` 都不存在（`:290-292`）。
- 失败：找不到控件或读屏异常一律不重试，置 `sheet_close_unverified` 后继续返回业务原因（`:293-298`）。
- 副作用：弹层关闭；重复执行时控件消失，只得到未收敛标记。

#### JOBS-A05 点击推荐弹层内候选职位行
- 代码：`scripts/jobs/recommend.py:320-338`，点击 `:331`
- 前置：`JOBS-P12` 成立；重新解析的候选顺序中 `index` 位置标题必须与契约 `chosen.title` 逐字相等（`:323-326`）；该行 `visible-to-user != "false"` 且 `clickable == "true"`（`:329-330`）。
- 动作：调用 `CORE-A16`，坐标取该行 bounds。
- 判据：点击后读屏，`CORE-W14` 判定为 `job_detail`（`:335-336`）；最多尝试 3 次（`:320`）。
- 失败：3 次仍非详情页 → `job_open_unverified`；标题或可见性不符 → `stale_contract`，不点击（`:324-330`）。
- 副作用：页面跳转；重试只在弹层仍打开时进行，不构成重复沟通。

#### JOBS-A06 点击详情页“继续沟通/立即沟通”
- 代码：`scripts/jobs/enroll.py:694-704`；`scripts/jobs/recommend.py:363-368`（推荐流程同义动作）
- 前置：`JOBS-P05`、`JOBS-P07` 通过；`JOBS-P08` 按钮唯一可用；批次已 `reserve_job` 且已 `mark_tap_started`（`scripts/jobs/enroll.py:697-701`）；推荐流程已保存职位并预留（`scripts/jobs/recommend.py:352-360`）。
- 动作：调用 `CORE-A16`，坐标取 `btn_chat` 节点 bounds。
- 判据：本层不判定；`enroll` 用 `JOBS-P09`（`scripts/jobs/enroll.py:706`），`recommend` 用“`CORE-W14`=`chat_detail` + `tv_title` 含联系人 + 文本含『由你发起的沟通』或『期望得到您的回复』”（`scripts/jobs/recommend.py:372-376`）。
- 失败：按钮缺失抛 `RuntimeError("当前详情页没有可确认的…控件")`（`scripts/jobs/enroll.py:695-696`）；点击前预留失败则释放预留并抛错（`:699-701`）。
- 副作用：**不可幂等，会真实发起沟通**；`mark_tap_started` 先把批次行置 `pending`，异常时阻止再次点击（`scripts/jobs/batch_store.py:601-605`）。

#### JOBS-A07 点击二次确认“沟通新职位”
- 代码：`scripts/jobs/enroll.py:575-588`（`_tap_continue_dialog`），点击 `:585`
- 前置：`JOBS-P10` 成立；文本等于“沟通新职位”且 `clickable == "true"` 的节点唯一（`:579-583`）。
- 动作：调用 `CORE-A16`。
- 判据：本层只判“点击命令已发出”；实际结果回到 `JOBS-P09` 循环继续读屏（`:608-611`）。
- 失败：弹窗判定不成立或控件缺失 → 返回 False，不重试；整个流程内最多补点 1 次（`dialog_clicked` 标志，`:599`、`:608-611`）。
- 副作用：可能触发第二次沟通；受“最多一次”标志保护。

#### JOBS-A08 点击底部“职位”标签
- 代码：`scripts/jobs/batch.py:205-218`（`_navigate_to_job_tab`），点击 `:214`
- 前置：`CORE-W05` 定位到“职位”标签；该节点必须 `clickable == "true"`，且不能是标签文本节点本身（`:209-213`）。
- 动作：调用 `CORE-A15`+`CORE-A16`。
- 判据：点击后新层级中 `JOBS-P01` 至少识别出 1 张卡片（`:215`）。
- 失败：定位失败、点击异常 → 返回 False（`:211-218`）；批次中只尝试 1 次（`job_tab_attempted`，`:425-428`）。
- 副作用：切换底部 tab；重复点击只是重复切页。

#### JOBS-A09 点击综合推荐页“新职位”标签
- 代码：`scripts/jobs/batch.py:253-256`（`_wait_for_new_job_list` 内），点击 `:254`
- 前置：`JOBS-P14` 返回 tab 非 None 且选中态不为 True；同一轮等待内尚未点过（`tab_clicked`，`:253`）。
- 动作：调用 `CORE-A16`。
- 判据：后续读屏中 `JOBS-P01` 非空（`:244-245`）。
- 失败：点击后若签名连续两次相同或出现骨架屏则立即停止等待并报 `job_list_not_loaded`（`:258-271`）。
- 副作用：切换列表 tab；单轮最多点 1 次。

#### JOBS-A10 点击“发布了新职位”消息标题
- 代码：`scripts/jobs/batch.py:274-316`（`_search_new_job_message`），点击 `:310`
- 前置：`JOBS-P15` 命中目标行；`open_notification=True`；行内存在文本逐字等于通知名的 title 节点（`:303-309`）。
- 动作：调用 `CORE-A16`，坐标取该 title 文本节点 bounds（点行内头像子节点是死区，必须点文本，`:302`）。
- 判据：点击后读屏，若 `CORE-W14` 不是 `chat_list` 则进入 `_wait_for_new_job_list`，最终 `JOBS-P01` 非空（`:311-314`、`:237-245`）。
- 失败：`CORE-U04` 不唯一 → `RuntimeError("无法唯一识别新职位消息入口")`（`:295-297`）；搜索 80 轮仍未找到 → `RuntimeError("新职位消息搜索达到上限…")`（`:328`）；失效通知点击后仍停留消息列表时，交回批次循环走 `JOBS-A08`。
- 副作用：跳转到新职位列表；通知失效时可能无跳转。

#### JOBS-A11 点击发送控件（资源消息）
- 代码：`scripts/hr/actions.py:778-835`，点击在 `:787`；控件定位 `scripts/hr/actions.py:417-459`（`CORE-I08`）
- 前置：输入框已含完整正文（`CORE-I04` 判据）；`CORE-I08` 唯一定位发送控件；发送前基线 `JOBS-P18`。
- 动作：调用 `CORE-A16`，随后 3 秒内轮询读屏。
- 判据：`JOBS-P18` 计数比基线增加，即聊天中出现与正文逐字相等的新消息节点（`:818-825`）。
- 失败：读屏失败重试 1 次后仍失败 → `unknown`（**点击已发生，禁止自动重发**）；输入框仍为空占位文案或超时 → `unknown`；定位不到控件属于点击前失败，返回 `not_sent`（`:779-780`）。
- 副作用：**不可幂等，会真实发消息**；调用方用 `claim_resource_share` 在点击前落盘 `pending`，靠本地状态阻止重发（`scripts/jobs/batch_store.py:205-232`）。

### 3.3 数据与副作用边界（职位域）

**本地数据库** `data/jobs.sqlite3`（`scripts/core/db.py:10`）：

- `jobs`：职位主记录、`dedupe_key` 唯一、`communication_status`（`not_started`/`continued`）、`application_status`（`not_applied`/`applied`/`unknown`）、`detail_complete`、`dedupe_reviewed`（`scripts/jobs/store.py:136-159`）。
- `job_discoveries`：一次扫描一次发现，`UNIQUE(job_id, run_id)`（`scripts/jobs/store.py:91-107`）。
- `job_listing_aliases`：列表标题/公司名与职位 ID 的已核对映射（`scripts/jobs/store.py:170-172`、`:457-468`）。
- `company_blacklist`：规范化公司名维度的黑名单（`scripts/jobs/store.py:173-177`）。
- `batches`/`batch_jobs`：批次目标、状态（`active`/`completed`/`closed`）、职位状态（`reserved`/`pending`/`completed`），`job_id` 全表唯一（`scripts/jobs/batch_store.py:22-47`）。
- `job_resource_shares`：每职位一行正文发送状态（`pending`/`sent`/`unknown`/`not_sent`）与 `attempts`、`resolution`（`scripts/jobs/batch_store.py:48-55`、`:205-269`）。
- `job_stage_cooldown`：单槽位冷却截止时间（`scripts/jobs/batch_store.py:56-62`、`:413-443`）。

**批次状态机**：

- `reserved`（点击前占位）→ `pending`（已点、结果未核实）→ `completed`（已核实）。只有 `pending` 能结算，`reserved` 可释放，`pending` 只能由人工核对收敛（`scripts/jobs/batch_store.py:577-621`）。
- 批次 `active` 期间禁止新建批次（`scripts/jobs/batch_store.py:541-557`）；名额 = `target - completed - reserved - pending`（`:520-538`）。
- 中断分两类：安全暂停（`time_budget_reached`、`no_more_jobs`、`daily_limit_reached`、`no_new_job_notification`）写冷却并收尾；阻断类（`needs_reconciliation`、`needs_resource_share_review`、`unconfirmed`、`needs_list`、`needs_identity_review`、`needs_detail_open_review`）不写冷却、不收尾，核对后可续跑（`scripts/jobs/batch.py:27-31`、`:92-135`）。

**文件副作用**：

- `data/pending_job.json`：单条待采集卡片 + 详情，完整入库后删除（`scripts/jobs/enroll.py:25`、`:440-441`、`:539-540`）。
- `data/job-work/recommend-<时间戳>.jsonl`：推荐机会契约，单行、拒绝覆盖（`scripts/jobs/recommend.py:13`、`:144-156`）。
- `data/operations.log` 与 `.lock`：脱敏事件日志（`CORE-L03`）。
- `screenshots/<本地日期>/…`：推荐卡与弹层截图（`CORE-E06`）。
- 设备侧临时文件 `/sdcard/job_tracker_ui.xml`（`CORE-A10`/`CORE-A11` 每次覆盖）。

**幂等性结论**：读操作（`读屏` 与 `JOBS-P`/`CORE-M` 判定）可重复；写库靠唯一键与状态条件收敛（`reserve_job` 唯一、`mark_tap_started` 只从 `reserved`、`complete_job` 只从 `pending`、`record_discovery` 唯一）；点击类操作（`JOBS-A02`–`JOBS-A11`）不可幂等，必须靠 `pending`/`sent` 状态与人工核对阻断重放。

## 4 HR 消息读取（HRR）

### 4.0 范围、入口与前置状态

入口（`scripts/main.py` 的 `hr` 子命令组，定义见 `scripts/main.py:274-363`）：

- `hr scan`：`run_hr` 的 scan 分支 `scripts/main.py:843-892`，调用 `hr_scan.run_phase1_scan`（`scripts/main.py:876`、`scripts/hr/scan.py:2811`）；读取一个未处理未读 HR 会话并输出 Phase 1 契约，**不执行任何 HR 动作**（`scripts/hr/scan.py:1902`）。
- `hr sync`：`run_hr` 的 sync 分支 `scripts/main.py:894-924`，调用 `hr_sync.run_read_sync`（`scripts/main.py:908`、`scripts/hr/sync.py:33`）；按最新优先核对今天/昨天的已读会话，只对新文本入站输出契约。
- `hr inbox`：`_run_inbox`（`scripts/main.py:1258-1276`）调用 `build_inbox`/`show_message`（`scripts/hr/inbox.py:80`、`184`）；纯离线读库，不连接手机（`scripts/hr/inbox.py:1`）。

涉及文件：`scripts/hr/scan.py`、`chat.py`、`inbox.py`、`sync.py`、`list_evidence.py`、`contract.py`、`identity.py`、`targeting.py`、`policy.py`（仅下游消费，见 8.3），以及 `scripts/core/message_list.py`、`workspace.py`、`adb.py`、`uitree.py`、`scripts/hr/store.py`。

前置状态：

- 至少一台已授权设备，`adb` 可用（`CORE-A02`/`CORE-A03`）；手机已解锁（`CORE-A07`）。
- 页面必须已处于消息列表。`hr scan`/`hr sync` 自身**不**调用工作区导航（全仓仅 `workspace check` 命令调用 `scripts/main.py:1529`）；不在列表时读取链路返回 `needs_list`（`scripts/hr/scan.py:1259`）。
- 数据库已建表（`scripts/main.py:861`、`900` 调用 `ensure_hr_schema`）。
- 全局无未核实动作：`pending_actions` 非 0 时 scan 直接 `paused/pending_action`（`scripts/hr/scan.py:1989-1990`、`scripts/hr/sync.py:82-83`），判定见 `HRA-P06`。
- 设备独占：scan/sync 都在 `scripts/hr/scan.py:750-801`（`device_command`）上下文内执行（`scripts/main.py:862-864`、`901-902`），内联入口拒绝与活派发并行占用（`scripts/hr/scan.py:720-747`），对应 `CORE-P01`–`CORE-P03`。

### 4.1 读取侧判定与去重映射

原 hr-read 章节的 18 条判定原语中，页面/列表/未读部分与 CORE 完全重复，统一收敛到 CORE；只有 HR 语义的判定保留为 HRR ID：

| 原编号 | 判定 | 权威 ID |
| --- | --- | --- |
| P1 | 页面分类（弹窗→登录→聊天页→职位页→推荐流→聊天控件→详情→列表→主界面→unknown） | `CORE-W14` |
| P2 | 聊天列表可读态（`named`/`empty`） | `CORE-W07` |
| P3 | 弹窗/登录信号（排除聊天正文） | `CORE-W08`、`CORE-W10` |
| P4 | “消息”tab 选中态 | `CORE-W06` |
| P5 | 唯一消息列表 | `CORE-M04`、`CORE-M01` |
| P6 | 未读（层级角标） | `CORE-M01` |
| P7 | 未读（红色像素阈值 ≥100，容差 30） | `CORE-M03`、`CORE-M01` |
| P8 | 底部消息数字角标 | `HRR-R03` |
| P9 | HR 行（非 `NON_HR_NAMES`、非“发布了新职位”、非无职位直播行） | `HRR-R04` |
| P10 | 身份拆分/脱敏（`"公司 \| 职位"` 拆两段；手机号、微信号替换为 `[联系方式]`） | `HRR-R08` |
| P11 | 会话键（公司+职位均空走 `conversation_without_position` 独立键空间） | `HRR-R08` |
| P12 | 聊天角色（`tv_sub_title` 含“招聘者/猎头/人事/recruiter/hr”） | `HRR-R14` |
| P13 | 消息方向（头像左右、我方发送状态、唯一气泡左右边界三路证据） | `HRR-R15` |
| P14 | 读全（唯一 `mRecycleView` 且不可滚，或连续两次滑动无变化） | `HRR-R20` |
| P15 | 时间窗口（今天/昨天；星期标签仅两天前可信） | `HRR-R05` |
| P16 | 预览触发（去 `[已读]/[送达]`、折叠空白、去省略号后与基线不同） | `HRR-R06` |
| P17 | 阻塞（pending/unknown/failed/needs_probe 最新动作或未关闭入站发现） | `HRA-P06` |
| P18 | 前台安全三态（`safe_chat`/`unsafe_focus`/`fallback`） | `HRR-R01`、`CORE-A05` |

### 4.2 原子操作（HRR-R）

#### HRR-R01 前台安全三态判定
- 代码：`scripts/hr/scan.py:1322`（`_boss_chat_focus`）
- 前置：刚读完同一聊天（调用方传 `chat_read_complete=True`）。
- 判定：调用 `CORE-A05`/`CORE-A06` 一次，按包名与 Activity 归类：未解锁或前台非 BOSS → `unsafe_focus`；BOSS 且 `ChatRoomActivity` → `safe_chat`；其余/不可解析 → `fallback`。
- 失败：`unsafe_focus` 时调用方不得发送任何按键或点击并返回 None（`scripts/hr/scan.py:1364-1365`）；`fallback` 走读屏分类；设备断连原样抛出。
- 副作用：只读，幂等。

#### HRR-R02 视口像素采集与缓存
- 代码：`scripts/core/adb.py:276`（`CORE-A19`）；缓存与回退 `scripts/hr/scan.py:1948-1962`
- 动作：当前 root 尚未采过像素时调用一次 `CORE-A19`，按 root 对象缓存。
- 判据/失败：`CORE-A19` 的成功判据；失败仅记 `progress/read_incomplete` 并回退纯层级解析（`scripts/hr/scan.py:1954-1960`），不终止命令。
- 副作用：只读；同一视口只采一次。

#### HRR-R03 底部消息数字角标读取
- 代码：`scripts/hr/scan.py:1460`（`_message_tab_badge`）；兜底判定 `scripts/hr/scan.py:1964-1979`
- 前置：存在 `unknown` 未读行（`_unread_unknown_keys`，`1472`）。
- 判定：读取 `CORE-W05` 目标子树文本，首个纯数字（去尾部“+”）即角标数；入口缺失为 None，无数字为 0。
- 失败：入口缺失 → 记 `badge_unverified` 并继续（不判完成）；角标 >0 → 返回 `paused/unread_remains`。
- 副作用：只读，幂等。

#### HRR-R04 HR 行判定
- 代码：`scripts/hr/contract.py:38`（`is_hr_row`）、`:15`（`build_preview_hint`）；调用 `scripts/hr/scan.py:2269`
- 判定：非 `NON_HR_NAMES`、非“发布了新职位”、非无职位直播行，且 `is_non_hr` 为假。
- 失败：判否时仅记 `candidate_skipped/non_hr` 并继续（`scripts/hr/scan.py:2269-2282`）。
- 副作用：只读，幂等。

#### HRR-R05 列表时间窗口分类
- 代码：`scripts/hr/list_evidence.py:151`（`classify_list_time`），解析 `105`（`parse_list_time`）
- 判定：`刚刚`/`昨天` 直接归类；其余按日期格式解析，仅今天/昨天进入同步窗口；星期标签仅两天前可信（`:87`）。
- 输出：`today`/`yesterday`/`older`/`unknown`；越界/未来/无法解析返回 `unknown`，调用方按 `out_of_window` 跳过（`scripts/hr/scan.py:2366-2367`）。
- 副作用：只读，幂等。

#### HRR-R06 列表预览变化触发判定
- 代码：`scripts/hr/list_evidence.py:237`（`read_trigger`）；调用 `scripts/hr/scan.py:1739`、`2370`
- 前置：已有非空基线预览/时间（`sweep_last_preview`/`sweep_last_list_time`）。
- 判定：去掉 `[已读]/[送达]`、折叠空白、去省略号后，正文与基线不同才返回 `preview_changed`。
- 失败：返回空串时只落未核实列表观测，不打开聊天（`scripts/hr/scan.py:2376-2394`）。
- 副作用：只读，幂等。

#### HRR-R07 发现记录重开
- 代码：`scripts/hr/store.py:1951`（`reopen_discovery`）；调用 `scripts/hr/scan.py:2612`、`1880`
- 前置：`message_key`/`conversation_key` 已由 `HRR-R08` 派生。
- 动作：新建、复用 pending 或把 resolved 改回 pending 的单事务写。
- 判据/失败：写入或状态迁移成功；返回 False → `discovery_record_failed`（`scripts/hr/scan.py:2613`）。
- 副作用：写 `hr_discoveries`；重复调用幂等。

#### HRR-R08 会话身份派生
- 代码：`scripts/hr/scan.py:1425`（`message_keys`）；会话键 `scripts/hr/store.py:325`（`conversation_key_for`），联系人拆分 `scripts/hr/identity.py:27`
- 前置：行有姓名、时间、预览（缺一即拒绝）。
- 判定：纯内存派生 `message_key` 与 `conversation_key`；公司+职位均空走独立键空间；手机号、微信号替换为 `[联系方式]`（`scripts/hr/identity.py:11`、`19`、`27`）；必要时只读查询别名表 `resolve_conversation_alias`（`scripts/hr/store.py:335`）。
- 判据/失败：两键均为合法 64 位十六进制；缺字段抛 `RuntimeError`，同名空身份会话不唯一也抛错，scan 记 `identity_unavailable` 并跳过（`scripts/hr/scan.py:2287-2296`）。
- 副作用：可能登记一次会话别名（`scripts/hr/store.py:344`）；同输入幂等。

#### HRR-R09 会话行唯一匹配
- 代码：`scripts/hr/identity.py:34`（`conversation_row_matches`）；调用 `scripts/hr/scan.py:1709`、`1720`
- 判定：纯内存按联系人+职位匹配，公司名允许 `...` 截断前缀匹配；必须恰好 1 行。
- 失败：0 行 → `conversation_not_found`；>1 行 → `conversation_ambiguous`（`scripts/hr/scan.py:1681-1684`、`1730-1733`）。
- 副作用：只读，幂等。

#### HRR-R10 消息行唯一定位
- 代码：`scripts/hr/chat.py:893`（`_row_container`），主路径 `928-942`（`open_message` 内重匹配）
- 判定：纯内存匹配；先按全字段匹配，失败再按姓名+职位（必要时加预览和时间）收窄。
- 判据/失败：匹配数与候选节点数均为 1；不唯一抛 `UiUnverified("chat_open_unverified")`，证据含期望联系人摘要与标题摘要（`:873-882`）。
- 副作用：只读，幂等。

#### HRR-R11 点击消息行进入会话
- 代码：`scripts/hr/chat.py:946`（`open_message` 内点击）；坐标来源 `943-945`（优先 `:id/tv_msg` 节点 bounds，否则容器 bounds）
- 前置：`HRR-R10` 已定位唯一容器。
- 动作：以 `scripts/core/adb.py:334-352` 的截图门控包裹一次 `CORE-A16`（门控内含最多 2 次点击）。
- 判据/失败：`changed` 不为 False（画面变化或无法取得截图）；`changed is False` 抛 `chat_open_unverified`（`:947-948`）。
- 副作用：非幂等；重复点击可能进入更深页面，调用方只在未打开时执行。

#### HRR-R12 打开后联系人核验
- 代码：`scripts/hr/chat.py:952-960`（重读后比较 `parse_chat(root)["contact"] == item["name"]`）
- 前置：`HRR-R11` 已点击。
- 动作：读屏一次 + 纯内存比较，最多 3 次（`changed is None` 时 sleep 后重读）。
- 判据/失败：`contact` 与期望姓名完全相等；3 次不符抛 `UiUnverified("contact_mismatch")`，附 `matching_rows` 与标题摘要（`:873-882`）。
- 副作用：只读核验，幂等。**口径说明**：与 `JOBS-P17`（标题列表包含匹配）不同，读取侧用首标题相等。

#### HRR-R13 打开后弹窗关闭并重读
- 代码：`scripts/hr/scan.py:1224`（`_dismiss_popup_after_open`）
- 前置：`HRR-R12` 已拿到聊天页 root。
- 动作：调用 `CORE-W18` 一次；若点击成功再读屏序列一次。
- 判据/失败：返回可解析层级；`CORE-W18` 返回 False 时原样返回；弹窗关闭或重读失败一律返回原 root，不终止（`1229-1236`），后续解析失败再判定。
- 副作用：最多一次点击；弹窗消失后调用幂等。

#### HRR-R14 聊天页身份与角色解析
- 代码：`scripts/hr/chat.py:665`（`parse_chat`），标题 `667`、角色 `668`、角色判定 `674`
- 判定：纯内存解析标题、角色标签；`:id/tv_sub_title` 含“招聘者/猎头/人事/recruiter/hr”判 `hr`，否则 `unknown`；同时计算唯一 `:id/mRecycleView` 是否成立。
- 输出/失败：`contact/role/kind/messages/latest_*/parse_complete`；`parse_complete=False` 时走 `_legacy_messages` 兜底（`672`、`427`），不抛异常。
- 副作用：只读，幂等。

#### HRR-R15 单条聊天行分类
- 代码：`scripts/hr/chat.py:260`（`_classify_group`），方向证据 `114`（`_alignment_direction`）/`129`（`_avatar_direction`），我方发送状态 `166`，整行文字摘要 `239`
- 判定：归类为文本/媒体/简历卡/卡片/系统提示/时间/未知，并给出方向；头像左右、我方发送状态、唯一气泡左右边界三路证据，冲突或缺失为 `unknown`。
- 失败：无法证明时返回 `unknown` 并带 `text_digest`；零高度占位行返回 None 被丢弃（`345-348`）。
- 副作用：只读，幂等。

#### HRR-R16 非文本与动作证据提取
- 代码：`scripts/hr/chat.py:299-348`（media/resume_card/card/system_tip 分支）、`361`（`extract_action_evidence`）、职位卡字段 `211`
- 判定：纯内存文本与资源 ID 匹配，抽取换微信、换电话、发简历的页面证据。
- 输出/失败：`action_evidence{wechat,phone,resume}`、`job_card{title,salary,...}`；未知句式不猜，整行降级 `unknown`（`340-343`）。
- 副作用：只读，幂等。

#### HRR-R17 跨屏行身份判定
- 代码：`scripts/hr/chat.py:718`（`_message_identity`）、`725`（`_repeat_safe`）
- 判定：纯内存元组比较，输出五元身份元组与是否可安全去重；有文字、时间、token、文本摘要或非空职位卡字段之一才可安全比对。
- 失败：纯媒体/空卡返回不可安全比对，拼接时保守保留。
- 副作用：只读，幂等。

#### HRR-R18 聊天向上滑动一次
- 代码：`scripts/hr/chat.py:687`（`scroll_chat`）
- 前置：`HRR-R14` 判定唯一 `:id/mRecycleView` 且 `scrollable=true`。
- 动作：调用 `CORE-A17`；x 取消息区中线，y 取 68%→25%（up），450 ms，随后 sleep 0.4 秒并读屏。
- 判据/失败：滑动前后 `_chat_view_signature`（文本+坐标）不同；方向非法抛 `ValueError`，列表不唯一抛 `RuntimeError`，读屏失败经 `CORE-A04` 决定终止或记 `read_chat` 证据（`scripts/hr/chat.py:794-799`）。
- 副作用：改变聊天视口；端点处返回未变化。

#### HRR-R19 多屏拼接去重
- 代码：`scripts/hr/chat.py:739`（`_merge_chat_screens`）
- 判定：以 `HRR-R17` 的身份元组为去重依据，纯内存拼接，按时间正序合并，只移除相邻屏幕边界上的连续重叠段；完全重复的屏幕跳过。
- 失败：无可信身份的重复行不裁剪，宁可重复不丢。
- 副作用：只读，纯函数。

#### HRR-R20 读全完成判定
- 代码：`scripts/hr/chat.py:767`（`read_full_chat`），完成条件 `800-819`，返回 `828`；可滚动判定 `760`
- 判定：`read_complete = read_complete and parse_complete`；到达不可滚动端点，或连续两次无变化，且每屏 `parse_complete` 为真。
- 输出/失败：`read_complete/scroll_count/screen_reads/budget_exceeded/parse_complete`；未完成返回 `read_complete=False`，调用方 `paused/read_incomplete`（`scripts/hr/scan.py:2635-2637`）；读屏预算触发 `on_budget_exceeded` 回调（`801-805`）。
- 副作用：只读判定；重复调用会重新滑动，非幂等。

#### HRR-R21 最新入站汇总判定
- 代码：`scripts/hr/chat.py:477`（`_latest_summary`），导出 `496`（`summarize_latest`），最近消息 `469`（`_latest_message`）
- 判定：扫描合并后的消息序列，确定最近一条可确认文本入站；最后一条入站为文本且其后无 unknown 行时状态为 `known`。
- 输出/失败：`(latest_inbound|None, known|non_text|unknown, kind)`；非文本返回 `non_text`，存在未知行返回 `unknown`，调用方据此暂停（`scripts/hr/scan.py:1847-1850`）。
- 副作用：只读，幂等。

#### HRR-R22 契约前方向门禁
- 代码：`scripts/hr/scan.py:1808-1826`（`_run_conversation_scan`），单候选路径 `2678-2707`
- 判定：最新非系统消息必须为 `incoming`，且 `latest_inbound_status == known`。
- 输出/失败：`record_emitted` 继续，或 `latest_message_not_incoming`/`latest_inbound_not_text`/`read_incomplete`；`unknown` 方向直接 `paused`，存在任意 unknown 行也 `paused/read_incomplete`（`1822-1825`）。
- 副作用：只读判定，幂等。

#### HRR-R23 非文本入站落库
- 代码：`scripts/hr/store.py:2268`（`record_non_text_pending`）；调用 `scripts/hr/scan.py:1812`、`1632`
- 前置：`HRR-R22` 判 `latest_inbound_not_text`，或列表预览为联系方式卡片。
- 动作：单条 `INSERT OR REPLACE INTO hr_non_text_pending` 并提交。
- 判据/失败：行写入成功；键非法抛 `ValueError`，异常向上由命令级处理。
- 副作用：同会话覆盖写，幂等。

#### HRR-R24 推荐卡机会判定
- 代码：`scripts/hr/scan.py:1998`（`observe_recommend_card`）
- 前置：`HRR-R19` 已合并消息，且本轮未见过该会话。
- 判定：同一行资源 ID 中同时出现 `:id/ll_recommend_job_container` 与 `:id/tv_recommend_job_name`。
- 输出/失败：返回是否已接管（True 时调用方 `paused/recommend_opportunity`）；`skip_recommend_opportunities` 时只记脱敏跳过事件并继续普通判断（`2007-2014`）。
- 副作用：写轮次状态 `recommend_seen`、撤销该会话未读计数、截一张证据图；同一会话只触发一次。

#### HRR-R25 非目标岗位判定
- 代码：`scripts/hr/targeting.py:42`（`judge_non_target`）
- 前置：`HRR-R14`/`HRR-R16` 已产出职位卡与最近入站文本。
- 判定：纯内存词表匹配，可选只读查询公司黑名单；命中任一强特征，或两类以上弱特征即非目标；技术岗优先豁免（`:56-57`）。
- 输出/失败：`{matched, reason, terms}`；证据不足返回 `matched=False`。
- 副作用：只读，幂等。

#### HRR-R26 未读阶段轮次计数落库
- 代码：`scripts/hr/scan.py:2544-2554`；状态写 `scripts/hr/store.py:1191`（`sweep_state`）
- 前置：已选出 candidate（`2544`）。
- 动作：单事务写轮次 JSON 状态（`seen/unread_pending/unread_done/unread_attempted`）。
- 判据/失败：写入成功；异常向上由命令级处理；`unread_pending` 用于下次恢复（`2040-2049`）。
- 副作用：写库；同一会话重复记录不会重复追加列表元素。

#### HRR-R27 未读失败回退计数
- 代码：`scripts/hr/scan.py:1521`（`_forget_unread_handled`）；调用 `2556-2560`、`2592`
- 前置：当前阶段为 `unread`。
- 动作：从 `unread_done` 移除并从 `unread_processed` 减一，写回轮次状态。
- 判据/失败：状态写回成功；非未读阶段或状态缺失时直接返回，不改已读阶段成果（`1525-1526`）。
- 副作用：写库；仅在计数 >0 时递减，重复调用不再变化。

#### HRR-R28 已读补库观察落库
- 代码：`scripts/hr/store.py:1254`（`save_read_sync_observation`），底层 `1262`（`_save_inbound`）；调用 `scripts/hr/scan.py:1616`
- 前置：`HRR-R20`/`HRR-R21` 通过，且最新入站与库内基线不同、无待核实外发。
- 动作：`BEGIN IMMEDIATE` 事务内写 `hr_conversations` + `hr_messages` + 清 `hr_non_text_pending`。
- 判据/失败：事务提交成功；键非法/文本非法抛 `ValueError`，异常由 sync 候选循环按 `snapshot_update_failed` 处理（`scripts/hr/sync.py:113-121`）。
- 副作用：写库；同 `message_key` 重复写为覆盖式，幂等。

#### HRR-R29 已读核对时间落库
- 代码：`scripts/hr/store.py:765`（`mark_unknown_checked`）；调用 `scripts/hr/sync.py:134`
- 前置：本轮该会话已完整读取并处理。
- 动作：单条 `UPDATE hr_conversations SET unknown_checked_at` 并提交。
- 判据/失败：`rowcount == 1`；会话不存在返回 False → `paused/snapshot_update_failed`（`scripts/hr/sync.py:134-135`）。
- 副作用：写库；重复调用只更新时间，幂等。

#### HRR-R30 契约组装
- 代码：`scripts/hr/contract.py:54`（`build_phase1_contract`）；调用 `scripts/hr/scan.py:1868`、`2787`
- 前置：`HRR-R22` 通过。
- 动作：纯内存组装，最多保留最近 8 条并补最新入站/最新外发/最近系统提示。
- 判据/失败：结构完整，`messages_truncated` 如实标记；`latest_inbound` 仅在状态 known 时填值（`:90`）。
- 副作用：纯函数，幂等。

#### HRR-R31 契约事件输出
- 代码：`scripts/hr/scan.py:1885-1891`（单会话）、`2797-2803`（未读循环）
- 前置：`HRR-R30` 已完成且 `HRR-R07` 成功。
- 动作：发布事务事件（`CORE-E07`）；`--output` 存在时向 JSONL 追加一行。
- 判据/失败：事件发布成功且写入无异常；写文件异常向上抛，此前已发布的进度事件保留。
- 副作用：追加写 JSONL，重复执行产生新行（非幂等），依赖 `HRR-R26` 去重与派发 `applied` 集合。

#### HRR-R32 恢复列表并核验筛选与选中
- 代码：`scripts/hr/scan.py:1349`（`_recover_filtered_list`）
- 前置：可选 `chat_read_complete=True`（刚读全同一聊天）。
- 动作：先用 `HRR-R01` 判前台；安全时发一次 `CORE-A18`；随后最多 3 轮“读屏序列 + `CORE-W14`”，必要时再发返回键或点击 `CORE-W05` 目标（`CORE-W16`）。
- 判据/失败：分类为 `chat_list`，且（点击过时）`CORE-W06` 为 True，且（`filter_entry_absent` 为假时）存在“取消筛选”；`unsafe_focus` 或任一核验不通过返回 None → 调用方 `abort_candidate("needs_list")`（`2608-2609`）。
- 副作用：最多 2 次返回键 + 1 次点击；重复调用在列表稳定后只读。

#### HRR-R33 页面证据截图（按页码去重）
- 代码：调用点 `scripts/hr/scan.py:2211`、`2418`、`2026`（`monitor.capture_evidence`）
- 前置：视口页码未取证过（`sweep["captured"]`）。
- 动作：调用 `CORE-E06`，标签形如 `list-page1`。
- 判据/失败：文件写入成功且事件带 `screenshot` 字段；异常向上由外层按命令级错误处理；页码不重复取证（`2085-2089`）。
- 副作用：新增本机截图文件；按页码去重。

#### HRR-R34 列表观测基线落库
- 代码：`scripts/hr/store.py:929`（`observe_sweep_preview`）；调用 `scripts/hr/scan.py:1162`（`_observe_verified_conversation`，`provisional` 决定写 verified 还是 UNVERIFIED 字段）
- 前置：已由 `HRR-R08` 得到会话键。
- 动作：单事务 `INSERT ... ON CONFLICT DO NOTHING` + `UPDATE hr_conversations`。
- 判据/失败：`rowcount == 1`；身份与会话键不一致抛 `ValueError`，失败记脱敏事件 `conversation_snapshot_failed` 并返回 False → `snapshot_update_failed`（`scripts/hr/scan.py:1167-1173`）。
- 副作用：写库；同一输入重复写结果稳定。

#### HRR-R35 阻塞项汇总
- 代码：`scripts/hr/inbox.py:57`（`_blockers`）；依赖 `blocked_action_events`、`pending_discovery_records`
- 动作：只读查询，不连接设备。
- 判据/失败：查询正常返回；异常由 `build_inbox` 包装为 `error` 事件并上抛（`scripts/hr/inbox.py:87-89`）。
- 副作用：只读，幂等。

#### HRR-R36 收件箱分组
- 代码：`scripts/hr/inbox.py:99`（`_build_inbox`），分组循环 `104-130`，原因文案 `31`（`reason_label`）
- 前置：`HRR-R35` 已得到阻断键集合。
- 判定：把未关闭消息分为阻断/必答/待确认/待判断四组，输出四组列表与 `counts/total`；`total == sum(counts)`。
- 失败：查询异常上抛；不做任何业务状态修改（`:100`）。
- 副作用：只读，幂等。

#### HRR-R37 单条消息详情读取
- 代码：`scripts/hr/inbox.py:202`（`_show_message`），包装 `184`（`show_message`）；调用 `scripts/main.py:1264`
- 前置：消息键已存在于 `hr_messages`。
- 动作：只读查询，不连接设备。
- 判据/失败：`message_detail` 命中一行；返回 None 时 `main.py` 输出 `{"error": "message_not_found"}`（`scripts/main.py:1265-1267`）。
- 副作用：只读，幂等。

### 4.3 数据与副作用边界（读取域）

- 本地数据库（全部本机 SQLite，`--db` 默认路径见 `scripts/main.py:275`）：
  - `hr_sweep_rounds`：轮次状态 JSON，写入入口 `scripts/hr/store.py:1191`；字段 `phase/page/seen/opened/unread_done/unread_attempted/unread_pending/captured/top_proven/recommend_seen`。
  - `hr_conversations`：列表基线（`scripts/hr/store.py:929`）、已读核对时间（`765`）、补库快照（`1262`）。
  - `hr_messages`：入站正文与观测时间，只在 `_save_inbound`（`1262`）与契约登记路径写入。
  - `hr_discoveries`：打开前发现记录（`1951`）与失败原因（`1920`）。
  - `hr_non_text_pending`：非文本入站待人工确认（`2268`）。
- 日志：`CORE-L01`–`CORE-L03` 只写脱敏事件，不含聊天原文。读取侧事件包括 `unread_chat_opened`（`scripts/hr/chat.py:959`）、`message_list_scrolled`（`scripts/core/message_list.py:324`、`361`）、`workspace_message_tab_tapped`（`scripts/core/workspace.py:415`）、`unread_filter_applied`（`message_list.py:183`）、`conversation_snapshot_failed`（`scripts/hr/scan.py:1170`）。
- 截图：`CORE-E06` 落本机文件，仅记录路径（`scripts/hr/scan.py:2026`、`2087`、`2211`、`2418`）；契约摘要显式排除截图字段（`scripts/hr/scan.py:461`）。
- 状态流转（读侧）：`unread` → `read`（`scripts/hr/scan.py:2445`）；`unread_pending` 在契约落库后清除（`2562-2568`）；`unread_done` 只增于未读成功并可由 `HRR-R27` 撤销。
- 只读边界：`hr scan` 不执行任何 HR 动作（`scripts/hr/scan.py:1902`）；`hr inbox` 只读本机库（`scripts/hr/inbox.py:1`、`100`）；`hr sync` 只写观察与核对时间，不写动作。
- 输出：`--output` 为 JSONL 追加（`scripts/hr/scan.py:1888-1890`）；stdout 契约/汇总 JSON（`scripts/main.py:874`、`913`）。
- 幂等性边界：列表/聊天解析、身份派生、门禁判定均纯函数幂等；滑动、点击、返回键、截图、JSONL 追加均非幂等，靠 `seen/applied/captured` 与 `top_proven` 去重。


## 5 HR 动作、清理与心跳（HRA）

### 5.0 范围、入口、授权与前置

入口命令：

| 命令 | 入口 | 说明 |
| --- | --- | --- |
| `hr apply` | `scripts/main.py:926-983`（`run_hr`）、`scripts/hr/apply.py:907-943`（`run_apply`） | 执行单条 Phase 2 决策，按意图调用动作执行器 |
| `hr review list` | `scripts/main.py:1209-1211`、`scripts/hr/review.py:56-71` | 只读列出待核对发现、动作与未关闭消息 |
| `hr review close`/`resolve-message`/`reopen` | `scripts/main.py:1235-1243` | 人工关闭、解除发现阻塞、重开发现 |
| `hr review resolve-not-sent`/`resolve-sent` | `scripts/main.py:1244-1249`、`scripts/hr/review.py:611-632` | 解除动作待核对状态 |
| `hr review record-resume` | `scripts/main.py:1177-1183`、`scripts/hr/review.py:635-640` | 仅写本地简历证据，不操作手机 |
| `hr review dismiss-non-text` | `scripts/main.py:1214-1217`、`scripts/hr/review.py:643-653` | 排除非文本入站 |
| `hr review update-draft` | `scripts/main.py:1229-1231`、`scripts/hr/review.py:318-337` | 替换待发草稿正文 |
| `hr review send-draft` | `scripts/main.py:1218-1220`、`scripts/hr/review.py:286-315`、`435-538` | 人工确认后发送待发草稿 |
| `hr review send-text` | `scripts/main.py:1221-1223`、`scripts/hr/review.py:148-283` | 人工确认后主动追加一条文本 |
| `hr review continue` | `scripts/main.py:1224-1228`、`scripts/hr/review.py:340-402` | 人工确认后恢复 blocked 消息 |
| `hr cleanup list`/`ledger`/`run` | `scripts/main.py:1279-1310`、`scripts/hr/cleanup.py:219-316`、`319-331`、`486-801` | 清理候选扫描、台账、删除 |
| `heartbeat start`/`status`/`round-done`/`stop`/`wait` | `scripts/main.py:1120-1172`、`scripts/heartbeat/heartbeat.py:79-172`、`180-203`、`206-294`、`398-411`、`414-477` | 心跳调度 |

`hr apply`、`review send-text`/`send-draft`、`cleanup run`、`heartbeat start` 分属不同设备占用与授权门禁，见 `scripts/main.py:1609-1633`（`_dispatch`）与 `scripts/main.py:828-834`（`_inline_device`）；占用登记使用 `CORE-P01`–`CORE-P03`。

涉及文件：

- `scripts/hr/apply.py`：决策执行主流程、动作编排、发送后尾部核验、拒绝清理。
- `scripts/hr/actions.py`：设备侧动作原语（联系方式、简历、文本输入发送、等待模板），其中输入法/输入框/发送控件原语已收敛为 `CORE-I`。
- `scripts/hr/review.py`：待核对清单、人工处置、草稿发送、主动追加文本、人工恢复。
- `scripts/hr/cleanup.py`：清理候选判定与手机端删除。
- `scripts/hr/store.py`：动作事件、草稿、消息/会话状态、清理台账、待核对门禁。
- `scripts/hr/contract.py`：Phase 1 契约组装，提供 `message_key`、`contact`、`latest_inbound_text` 等输入。
- `scripts/heartbeat/heartbeat.py`、`scripts/heartbeat/store.py`：心跳起停、等待、轮次结束、审计门控与调度表状态迁移。
- `scripts/main.py`：命令组、参数校验、设备围栏。

涉及页面（常量见 `scripts/core/workspace.py:15-21`）：消息列表 `chat_list`（清理扫描、草稿发送前定位会话行）、聊天详情 `chat_detail`（文本输入、发送、联系方式交换、简历发送、草稿发送）、弹层 `popup`（交换确认框、附件简历确认框、长按菜单、删除确认框）；登录页/首页/职位详情仅作为“非目标页面”门禁。

前置状态：

- 设备已由 `CORE-A02`/`CORE-A03` 唯一选中，且未锁屏（`CORE-A07`；`scripts/heartbeat/heartbeat.py:41-43`）。
- `hr apply` 需要 Phase 1 契约 JSONL 与 decision JSON，且 `message_key` 在契约中唯一（`scripts/main.py:940-955`、`scripts/hr/apply.py:49-59`）。
- 执行动作前全局不得存在未核实动作（`HRA-P06`）。
- 清理候选要求列表年龄满 7 天或已是拒绝终态，并受草稿/人工待办保护（`HRA-P13`）。
- 心跳要求无进行中的对账问题（`scripts/heartbeat/heartbeat.py:87-89`、`230-232`）。

授权前提：

- `hr apply` 带 Dispatch 时按 `authorization.send_reply`/`request_resume`/`request_wechat`/`request_phone`/`delete_rejected` 裁剪动作（`scripts/hr/apply.py:500-516`、`624-641`）。
- 出现拒绝且不保留联系方式时必须有 `--serial`，否则在写意图前暂停（`scripts/hr/apply.py:518-523`）。
- `send-text`、`send-draft`、`continue`、`cleanup run`、`heartbeat stop` 均要求显式 `--confirmed`（`scripts/main.py:1184-1206`、`1281-1285`、`scripts/hr/review.py:350-355`、`scripts/heartbeat/heartbeat.py:401-402`）。
- 固定等待模板还要求 Dispatch 显式授权且模板已确认（`scripts/hr/apply.py:288-302`、`scripts/hr/scan.py:364-370`）。
- **本文档不授予上述任何授权**；第 6 节的流程只是 ID 序列描述。

### 5.1 判定（HRA-P）

#### HRA-P01 发送成功（我方回显新增）
- 代码：`scripts/hr/actions.py:723-726`、`818-825`（`send_resource_message`）
- 规则：点击发送前统计 `resource-id` 以 `tv_content_text` 结尾且 `text` 等于正文的节点数为 `existing_count`；点击后同条件节点数 `> existing_count` 才算 `sent`。发送前后都重新核验聊天标题与联系人（`:722`、`:817`，联系人判定用 `JOBS-P17`）。
- 副作用：只读判定，幂等。

#### HRA-P02 发送结果未知
- 代码：`scripts/hr/actions.py:826-836`、`132-134`、`843-845`
- 规则：已注入点击（`tapped=True`）后，若输入框已清空/变为占位符但未出现回显新增，或 3 秒内既无回显又未清空，返回 `unknown`；读屏失败补读一次仍失败也返回 `unknown`（`post_click_read_failed`）。
- 硬约束：**点击之后任何异常都不得降级为“未发送”**。

#### HRA-P03 发送前确定未发送
- 代码：`scripts/hr/actions.py:80-89`（`_pre_click_failure`）、`714-720`、`731-753`、`776-780`、`837-850`
- 规则：尚未点击发送控件前，身份不符、输入框不唯一、正文两次未落入输入框、发送控件定位失败、输入法恢复前异常等，统一返回 `not_sent` 并带细分码（`read_failed`/`wrong_screen`/`target_missing`/`needs_draft_clear` 等）。
- 副作用：只读判定，幂等。

#### HRA-P04 输入框唯一且为空
- 规则：`CORE-I07` 必须恰好 1 个输入框；`CORE-I03` 清空后其 `text` 必须属于 `{"", "回复消息", "介绍自己回复率更高哦~", "和Ta聊一聊~"}`；不满足时最多再清空一次，仍不满足返回 `needs_draft_clear`。
- 代码：`scripts/hr/actions.py:727-730`、`741-753`。

#### HRA-P05 输入正文一致
- 规则：`CORE-I04` 广播后重新读屏，输入框 `text` 必须与正文完全相等；最多两轮，两轮都不等返回 `not_sent`。
- 代码：`scripts/hr/actions.py:755-775`；回读实现 `:770-775`、`826-829`。

#### HRA-P06 待核对门禁（全局未核实动作）
- 代码：`scripts/hr/store.py:2028-2033`（`has_unverified_actions`）、`2021-2025`（`pending_actions`）
- 规则：按“每会话每动作最新一条事件”判断，状态属于 `pending/unknown/failed/needs_probe` 即视为未核实，阻塞新的 `apply`、`send-text`、固定等待与清理义务判定（`scripts/hr/apply.py:477-478`、`scripts/hr/review.py:179-180`、`203-204`、`scripts/hr/cleanup.py:622-626`）。
- 副作用：只读，幂等。跨域引用：读取侧 `paused/pending_action` 也来自本判定。

#### HRA-P07 待核对状态产生
- 代码：`scripts/hr/apply.py:718-736`、`739-756`、`762`
- 规则：动作执行后按证据收敛为 `not_sent`/`unknown`/`needs_probe`/`needs_attachment`/`failed` 等；`unknown`、`needs_probe`、`needs_attachment`、`failed` 留待人工核对；点击前可证明未发送时写 `not_sent`，不进入待核对。
- 副作用：本地状态写入由 `HRA-A10` 执行，本判定只描述分类。

#### HRA-P08 待核对解除
- 代码：`scripts/hr/store.py:1855-1860`（`resolve_action_event`）、`1863-1885`（`reconcile_action_event`）、`scripts/hr/review.py:611-632`
- 规则：仅当最新事件状态属于 `pending/unknown/failed/needs_probe/needs_attachment` 时可改写（`scripts/hr/store.py:23-24`、`1838-1852`）；人工确认未发送写 `not_sent`，确认已发送写目标终态；`requested → sent/delivered` 的纠正仅允许 `resume`（`1874-1884`）。
- 副作用：只读判定，写入由 `HRA-A11`/`HRA-A12` 执行。

#### HRA-P09 聊天尾部入站证据
- 代码：`scripts/hr/apply.py:74-93`（`_verified_latest_inbound`）、`96-122`（`_confirmed_outgoing_message`）、`scripts/hr/review.py:410-432`（`_latest_inbound_is_tail`）
- 规则：聊天必须可解析、联系人一致、`parse_complete` 非 False；最新入站状态为 `known` 才算确认（依赖 `HRR-R14`/`HRR-R21`）。草稿发送额外要求最新真实行就是草稿对应入站文本，且其后的行只能是 `system_tip/job_card/system_card/time` 这类透明行。
- 副作用：只读判定，幂等。

#### HRA-P10 简历交付与仅请求
- 代码：`scripts/hr/actions.py:463-470`、`541-560`、`608-637`
- 规则：出现 `RESUME_SENT_TEXTS`（如“附件简历已发送”）或确认框消失后出现预期附件名节点/本次请求卡双按钮置灰，记 `sent`；只出现“附件简历请求已发送”、按钮文案“请求中”或“您好，可以看看我的简历”，记 `request_sent`。
- 硬约束：确认框仍打开时，框内附件名与送达文案一律不算本次交付（`:611-620`）。
- 副作用：只读判定，幂等。

#### HRA-P11 联系方式动作已处理/可点击
- 代码：`scripts/hr/actions.py:270-293`、`295-311`、`333-347`
- 规则：页面出现“企业认证已失效”“暂时无法与您沟通”直接 `failed`；槽位文字已是“请求中”或“查看微信/电话”返回 `already_processed`；槽位按钮必须唯一、启用且位于对应横向区间（电话 `0-270`，微信 `270-540`），否则 `unknown`。
- 副作用：只读判定，幂等。

#### HRA-P12 附件确认框可确认
- 代码：`scripts/hr/actions.py:473-498`（`_resume_confirmation`）
- 规则：必须出现标题“确定发送附件简历吗？”；页面中以 `.pdf/.doc/.docx` 结尾的附件名列表必须恰好等于 `[expected_attachment]`；确认/确定按钮必须唯一且启用。
- 输出/失败：附件不符 `attachment_mismatch`，按钮不满足 `confirm_missing`，只有 `ready` 才允许点击。
- 副作用：只读判定，幂等。

#### HRA-P13 清理候选判定
- 代码：`scripts/hr/cleanup.py:25-26`、`150-216`（`_candidate_for_row`）、`609-633`（`cleanup_has_pending_obligation`）
- 规则：普通 HR 会话按列表时间满 `CLEANUP_AGE_DAYS = 7` 天才候选；数据库已是 `rejected/declined` 的会话有即时清理例外；未满 7 天时未读角标、待发草稿、人工待办仍保护，满 7 天后不再保护；`force_rejected` 只放行当前拒绝目标。
- 失败：时间无法解析记 `time_unparsed`；身份不足不参与判定。
- 副作用：只读（可读本地库），幂等。

#### HRA-P14 删除成功
- 代码：`scripts/hr/cleanup.py:661-700`
- 规则：删除后列表必须可唯一解析；目标会话姓名（及职位）不得再出现；若删除前记录了唯一相邻锚点行，删除后该锚点仍唯一可见才算成功；没有锚点时只接受“删除成功”提示。
- 失败：`target_still_present`、锚点缺失/重复、列表不可解析都记 `delete_unverified`。
- 副作用：只读判定，幂等。

#### HRA-P15 会话行唯一匹配（清理侧）
- 代码：`scripts/hr/cleanup.py:334-347`（`_row_nodes`）、`scripts/hr/chat.py:893-916`（`_row_container`）、`928-942`（`open_message`）
- 规则：列表中 `recyclerView` 必须唯一；按 `tv_name` 与 `tv_position`（含公司拆分）完全匹配；同名同职位时再用 `tv_msg` 与 `tv_time_v2` 收窄；仍不唯一时拒绝点击。
- 副作用：只读判定，幂等。

#### HRA-P16 草稿陈旧判定
- 代码：`scripts/hr/review.py:483-491`、`94-101`、`438-442`
- 规则：无待发草稿或消息已关闭直接拒绝；聊天 `parse_complete` 为 False、联系人不等、最新入站状态非 `known`、最新入站文本不等于草稿对应入站、或尾部不是该入站文本，均记 `stale_draft`/`contact_unverified`。
- 副作用：只读判定，幂等。

#### HRA-P17 心跳到期与轮次占用
- 代码：`scripts/heartbeat/heartbeat.py:445-474`、`scripts/heartbeat/store.py:117-125`
- 规则：`next_due_at <= now` 才到期；占用使用单条 `UPDATE ... WHERE state='active' AND round_in_progress=0 AND next_due_at<=?`，`rowcount==1` 才算抢到；抢不到时若已在轮次中返回 `already_running`，否则返回停止结果。
- 副作用：判定 + 由 `HRA-A33` 执行的原子写。

#### HRA-P18 授权与确认门禁
- 代码：`scripts/hr/apply.py:500-516`、`624-645`、`scripts/main.py:1184-1206`、`1281-1285`
- 规则：`send_reply/request_resume/request_wechat/request_phone/delete_rejected` 任一未授权时对应动作被跳过或整条暂停（`policy_violation`）；`send-text/send-draft/update-draft/continue/cleanup run/heartbeat stop` 无 `--confirmed` 直接拒绝；`conversation_state.reconciliation_failed` 存在时联系方式/简历动作暂停（`needs_action_review`）。
- 副作用：只读判定，幂等。

### 5.2 动作（HRA-A）

#### HRA-A01 长按会话行（打开操作菜单）
- 代码：`scripts/hr/cleanup.py:593-597`、`604-615`
- 前置：`HRA-P15` 已唯一匹配；`HRA-A21` 已得到按压点；已等待 0.4 秒让滚动惯性停止。
- 动作：调用 `CORE-A17` 同点滑动 1200 ms；第 2/3 次尝试前再等待 0.4 秒并重复。
- 判据：`HRA-A22` 在读屏中找到“删除消息”。
- 失败：3 次仍无菜单返回 `menu_unverified` 并停止。
- 副作用：打开/关闭上下文菜单；本身不删除。

#### HRA-A02 点击发送控件（文本）
- 代码：`scripts/hr/actions.py:784-787`
- 前置：`HRA-P05` 已证明正文一致；`CORE-I08` 已定位唯一发送控件；`HRA-A09` 已登记 `pending` 意图（`apply`/`send-text`/`send-draft` 路径）。
- 动作：调用 `CORE-A15`+`CORE-A16`，点击发送控件中心；`tapped=True` 后进入证据轮询。
- 判据：只能由 `HRA-A03`/`HRA-P01` 判定；点击命令成功不等于发送成功。
- 失败：点击前失败由 `HRA-P03` 判为 `not_sent`；点击后任何异常都保留 `unknown`（`:132-134`、`843-845`）；**绝不自动重点击**。
- 副作用：**不可逆对外发送**。消息一旦发出无法撤回；重复点击会产生重复消息，因此必须先落 `pending` 并在 `unknown` 后停止等人工核对。

#### HRA-A03 核验发送结果
- 代码：`scripts/hr/actions.py:788-836`、`851-858`
- 前置：`HRA-A02` 已执行。
- 动作：读屏循环最多 3 秒；每轮重新核验联系人（`JOBS-P17`）、按 `HRA-P01` 统计回显节点、按 `HRA-P04` 检查输入框是否清空。
- 输出/失败：`sent`（`HRA-P01` 命中）/`unknown`（`HRA-P02` 命中）/`not_sent`；读屏失败补读一次，仍失败 `unknown("post_click_read_failed")`；不重试发送。
- 副作用：只读；会把发送前后层级写入观测归档（`CORE-O03`，`scripts/hr/actions.py:851-858`）。

#### HRA-A04 请求交换联系方式（点击“换微信/换电话”）
- 代码：`scripts/hr/actions.py:252-330`（`request_contact_exchange`）、`313-316`
- 前置：`HRA-P11` 判定槽位可点击；联系人核验通过（`JOBS-P17`）；无限制提示；已通过 `claim_action` 认领意图（`:292-293`）。
- 动作：先读屏序列并 `verify_contact`，等待 0.8 秒；随后 `_observed_click` → `CORE-A16` 点击槽位按钮；若需降级，走 `CORE-A17` 的 60 ms 同点滑动。
- 判据：出现“请求交换微信/电话已发送”系统提示，或槽位变为“请求中”，或出现交换确认弹层（`:297-311`）。
- 失败：效果未知且无确认框返回 `unknown`；确认框缺失仅在证据证明无变化时 `not_sent`，否则 `unknown`。
- 副作用：**对外请求不可撤回**；已处理状态可防重，但仍可能向 HR 发出交换请求。

#### HRA-A05 确认交换（点击“确认”）
- 代码：`scripts/hr/actions.py:302-307`、`321-330`
- 前置：`HRA-P11` 已识别确认框；`确定与对方交换{kind}吗？` 文本存在；“确认”按钮唯一且启用。
- 动作：`_observed_click` → `CORE-A16` 点击“确认”。
- 判据：弹层消失且出现“请求交换…已发送”或槽位“请求中”（`:297-300`、`328-330`）。
- 失败：确认后仍无送达证据返回 `unknown`，不重试。
- 副作用：**不可逆对外交换确认**；重复点击可能重复提交。

#### HRA-A06 点击“发简历”打开确认框
- 代码：`scripts/hr/actions.py:501-536`（`request_resume`）、`526-527`
- 前置：页面无已送达/已请求证据（`:511-517`）；存在唯一“发简历”可点击节点（`:518-520`）；附件名非空（`:504-505`）。
- 动作：`_observed_click` → `CORE-A16`；必要时 `CORE-A17` 降级。
- 判据：`HRA-P12` 判定确认框 `ready`（标题、附件名、唯一确认按钮均满足）。
- 失败：附件不符 `needs_resume_review("attachment_mismatch")`；无确认按钮 `confirm_missing`；点击无效果时区分 `not_sent`/`unknown`。
- 副作用：打开确认框本身不发送；但点击“发简历”会向 HR 侧暴露动作意图，重复点击可能重复弹框。

#### HRA-A07 确认发送附件简历（点击“确认/确定”）
- 代码：`scripts/hr/actions.py:539-560`
- 前置：`HRA-P12` 为 `ready`；`claim_action` 成功（`:537-538`）。
- 动作：`_observed_click` → `CORE-A16` 点击唯一确认按钮。
- 判据：`HRA-P10` 的交付证据；仅有请求文案时 `request_sent`。
- 失败：无任何证据返回 `unknown`，不重试；点击后异常保留 `unknown`。
- 副作用：**不可逆对外发送附件简历**；重复确认可能重复发送。

#### HRA-A08 点击“同意”接受简历索取
- 代码：`scripts/hr/actions.py:568-666`（`accept_resume_request`）、`648-666`
- 前置：页面存在“我想要一份您的附件简历，您是否同意”请求卡（`:563`、`586-589`）；“同意”按钮唯一、可点击、启用（`:590-597`）；`claim_action` 成功（`:605-606`）。
- 动作：`_observed_click` → `CORE-A16` 点击“同意”；若随后出现附件确认框，再走 `HRA-A07`。
- 判据：`HRA-P10` 的交付证据，且确认框已消失（`:608-637`）。
- 失败：确认框仍在时交付信号无效；无交付证据且完整窗口无变化才 `not_sent`，否则 `unknown`。
- 副作用：**不可逆对外交付简历**；同一请求卡重复点击可能重复交付。

#### HRA-A09 登记动作意图 pending
- 代码：`scripts/hr/store.py:1660-1690`（`record_action_event`）、`scripts/hr/apply.py:684`、`scripts/hr/review.py:213-214`、`500-501`
- 前置：`conversation_key`/`message_key` 合法；动作状态词表合法。
- 动作：`BEGIN IMMEDIATE`；按动作作用域（`reply` 按 `message_key`，其余按 `conversation_key`）检查是否已有 `pending/requested/request_sent/exchanged/sent/delivered/unknown` 最新事件；有则回滚返回 None，无则插入 `pending`。
- 判据/失败：返回正整数事件 ID；None 表示已有未决/已发送动作，调用方跳过或暂停（`already_processed`/`needs_action_review`）。
- 副作用：本地写入；幂等地阻止重复动作，不产生对外消息。

#### HRA-A10 收敛动作结果
- 代码：`scripts/hr/store.py:1821-1835`（`finish_action_event`）、`scripts/hr/apply.py:762`、`718-736`
- 前置：`HRA-A09` 已返回事件 ID。
- 动作：`UPDATE hr_action_events SET status=?,detail_code=?,updated_at=? WHERE id=?` 并提交。
- 判据/失败：`rowcount == 1`；状态/详情码非法抛 `ValueError`；更新 0 行说明事件已被改写，调用方按未知处理。
- 副作用：本地写入；不产生对外消息；重复收敛会覆盖同一条事件结果。

#### HRA-A11 人工确认未发送
- 代码：`scripts/hr/store.py:1888-1893`（`resolve_action_outcome`）→ `1855-1860`；`scripts/hr/review.py:611-620`
- 前置：调用方带 `--confirmed`（`scripts/main.py:1233-1234`）；`HRA-P08` 允许改写。
- 动作：仅对最新一条事件写 `not_sent`。
- 判据/失败：最新事件状态变为 `not_sent`；状态不匹配返回 False，不改写历史。
- 副作用：解除 `HRA-P06` 门禁；不操作手机，也不撤回已发出的消息。

#### HRA-A12 人工确认已发送/纠正
- 代码：`scripts/hr/store.py:1896-1902`（`reconcile_action_outcome`）→ `1863-1885`；`scripts/hr/review.py:623-632`
- 前置：`result ∈ {requested, sent, delivered}` 且 `confirmed=True`。
- 动作：若最新事件是 `requested` 且动作为 `resume`，仅允许改为 `sent/delivered`；其他情况在可核对状态集合内改写。
- 判据/失败：`rowcount == 1`；非 `resume` 的 `requested` 纠正抛 `ValueError`；状态不匹配返回 False。
- 副作用：解除 `HRA-P06` 门禁；不重发消息，只修正本地事实。

#### HRA-A13 保存待发草稿
- 代码：`scripts/hr/store.py:1644-1658`（`save_reply_draft`）、`scripts/hr/apply.py:263-275`、`617-618`
- 前置：决策 `reply.action == "send"` 且正文非空；或 `reply.action == "draft"`。
- 动作：写入/更新 `hr_reply_drafts` 的 `pending` 草稿。
- 判据/失败：草稿表存在该 `message_key` 的 `pending` 行；数据库异常向上抛出。
- 副作用：本地写入；重复保存会覆盖同一待发草稿。

#### HRA-A14 更新待发草稿
- 代码：`scripts/hr/review.py:318-337`、`scripts/hr/store.py:2142-2152`
- 前置：`message_key` 为 64 字符；草稿仍 `pending` 且所属入站未关闭。
- 动作：`UPDATE hr_reply_drafts SET text=? WHERE message_key=? AND status='pending' AND EXISTS(未关闭入站)`。
- 判据/失败：`rowcount == 1`；0 行返回 `no_pending_open_draft`，不新建草稿。
- 副作用：本地写入；不发送。

#### HRA-A15 人工关闭消息
- 代码：`scripts/hr/review.py:586-608`、`scripts/hr/store.py:2107-2112`
- 前置：`--confirmed`；不存在未决发送动作（`scripts/hr/review.py:595-599`）。
- 动作：`close_message(..., "manual_close")` + `resolve_discovery` + `close_blocked_discovery`。
- 判据/失败：任一关闭/解除操作返回 True；存在未决动作时抛 `RuntimeError("存在待核对发送动作，先核实动作结果")`。
- 副作用：本地写入；不操作手机。

#### HRA-A16 重开未产出契约的发现
- 代码：`scripts/hr/review.py:580-583`、`scripts/hr/store.py:1968-1982`
- 前置：`--confirmed`（`scripts/main.py:1233-1234`）。
- 动作：调用 `reopen_uncontracted_discovery` 改写发现状态。
- 判据/失败：发现状态变为可再扫描；不满足重开条件时返回 False。
- 副作用：本地写入；下次 scan 可能重新读取该会话。

#### HRA-A17 排除非文本入站
- 代码：`scripts/hr/review.py:643-653`、`scripts/hr/store.py:2278-2306`
- 前置：`--confirmed`；`hr_non_text_pending` 存在且有预览/时间。
- 动作：事务内更新会话基线、删除 `hr_non_text_pending`、追加 `hr_non_text_review_events`。
- 判据/失败：返回 `dismissed`；事务失败回滚并返回 `no_pending_non_text`/`conversation_not_found` 等原因码。
- 副作用：本地写入；不发送回复。

#### HRA-A18 人工记录简历状态
- 代码：`scripts/hr/review.py:635-640`、`scripts/hr/store.py:1724-1767`
- 前置：`--confirmed`；`status ∈ {requested, sent}`；会话存在。
- 动作：`BEGIN IMMEDIATE` 下读取最新 resume 事件，按“只升不降、同状态不重复”插入新事件（`detail_code='manual_evidence'`）。
- 判据/失败：返回 `recorded`；任一校验失败返回 `unchanged`/`status_downgrade`/`needs_action_review` 且不写库。
- 副作用：只写本地；可重复调用但同状态不重复。

#### HRA-A19 逐屏扫描清理候选
- 代码：`scripts/hr/cleanup.py:268-301`、`740-791`
- 前置：`CORE-M07` 已归位到最新端；`max_screens >= 1`。
- 动作：读屏序列 → `CORE-M01` → 对每行调用 `HRA-P13` → `CORE-M06` 向上滑动；每屏去重。
- 输出/失败：`candidates`、`skipped`、`counts`、`scanned_rows`；页面不可识别 `needs_list`；时间无法解析延后到结束记 `time_unparsed`；断连停止。
- 副作用：只读（`list`）；`run` 在 `HRA-A27` 写台账。

#### HRA-A20 复核清理候选行
- 代码：`scripts/hr/cleanup.py:574-585`、`334-347`
- 前置：`HRA-A19` 已产出候选；`recyclerView` 唯一。
- 动作：按姓名/职位/公司精确匹配行，并记录可见身份与相邻锚点。
- 判据/失败：匹配行恰好 1 个；0 个或多个返回 `row_not_found` 并跳过。
- 副作用：只读判定。

#### HRA-A21 计算长按按压点
- 代码：`scripts/hr/cleanup.py:401-413`（`_press_point`）
- 前置：`HRA-A20` 已得到行节点，`recyclerView` 唯一。
- 动作：取列表与行 bounds 的交集中心。
- 判据/失败：交集宽高均不小于 40 像素；否则返回 None，候选记 `row_partially_visible`。
- 副作用：只读。

#### HRA-A22 定位“删除消息”菜单
- 代码：`scripts/hr/cleanup.py:604-633`、`389-394`（`_unique_action`）
- 前置：`HRA-A01` 已长按。
- 动作：读屏轮询，首次窗口 1.5 秒、后续 3.0 秒；用 `CORE-U07` + `CORE-U08` 求唯一节点。
- 判据/失败：唯一可点击的“删除消息”节点；超时无节点时记录可见菜单文本并停止。
- 副作用：只读。

#### HRA-A23 点击“删除消息”
- 代码：`scripts/hr/cleanup.py:637-646`
- 前置：`HRA-A22` 已得到唯一菜单按钮。
- 动作：`CORE-A16` 点击菜单按钮 bounds。
- 判据：读屏出现唯一“删除”按钮，或达到 2 秒超时交给 `HRA-A24` 判定。
- 失败：异常或断连停止；`confirm_mismatch` 记录。
- 副作用：仅打开确认框；尚未删除。

#### HRA-A24 判定删除确认框点名
- 代码：`scripts/hr/cleanup.py:652-654`、`416-428`（`_confirm_names_contact`）
- 前置：`HRA-A23` 已打开确认框。
- 判定：存在唯一“删除”按钮，且页面文本包含完整句子“确认删除与{姓名}的聊天吗？”或独立姓名节点；姓名归一化后完全一致。
- 失败：不满足则停止并记 `confirm_mismatch`。
- 副作用：只读。

#### HRA-A25 点击“删除”（不可逆）
- 代码：`scripts/hr/cleanup.py:658-660`
- 前置：`HRA-A24` 通过；台账已由 `HRA-A27` 归档。
- 动作：`CORE-A16` 点击唯一“删除”按钮，然后 `CORE-A21` 等待，再读屏。
- 判据：只能由 `HRA-A26`/`HRA-P14` 判定；点击成功不等于删除成功。
- 失败：点击后任何异常/断连都记 `delete_unverified` 并停止，**禁止重试**。
- 副作用：**不可逆删除手机端会话**；聊天记录在手机端消失，本地数据库不随之删除。

#### HRA-A26 核验删除结果
- 代码：`scripts/hr/cleanup.py:661-700`
- 前置：`HRA-A25` 已点击、`CORE-A21` 已等待。
- 动作：读屏序列 → `_visible_row_identities`；检查目标是否仍存在、锚点是否仍唯一、是否出现“删除成功”。
- 判据/失败：`HRA-P14`；任何不确定都停止并记台账 `unknown`（`HRA-A28`），不自动重试。
- 副作用：只读；但此时删除可能已发生，无法回滚。

#### HRA-A27 台账归档
- 代码：`scripts/hr/store.py:356-376`（`archive_cleanup_candidate`）、`scripts/hr/cleanup.py:562-571`
- 前置：候选通过 `HRA-P13`；台账无同会话同列表时间的既有行。
- 动作：`INSERT OR IGNORE ... result='archived'`。
- 判据/失败：插入成功或已存在同键行；字段非法抛 `ValueError`，已存在不覆盖。
- 副作用：本地写入；幂等。

#### HRA-A28 台账收敛（deleted/unknown）
- 代码：`scripts/hr/store.py:575-583`（`mark_cleanup_deleted`）、`586-597`（`mark_cleanup_unknown`）、`scripts/hr/cleanup.py:697-700`、`537-551`
- 前置：台账行为 `archived`。
- 动作：`UPDATE ... SET result='deleted',deleted_at=?` 或 `result='unknown',detail_code=? WHERE result='archived'`。
- 判据/失败：`rowcount == 1`；`unknown` 的 detail 码限制在 `menu_unverified/confirm_mismatch/delete_unverified/page_unexpected`；0 行说明台账已变化，流程仍停止。
- 副作用：本地写入；`unknown` 阻止下次自动重试（`scripts/hr/cleanup.py:562-569`）。

#### HRA-A29 心跳预检（设备与解锁）
- 代码：`scripts/heartbeat/heartbeat.py:119-154`、`scripts/core/adb.py:110-148`、`180-185`
- 前置：`heartbeat start` 已由用户确认。
- 动作：`CORE-A02` → `CORE-A03` 选择设备；`CORE-A05` → `CORE-A07` 判定解锁。
- 判据/失败：唯一设备且状态 `device`、`verify_unlocked` 不抛；失败按状态区分 `device_disconnected`/`device_unauthorized`/`device_ambiguous`/`device_locked`。
- 副作用：只读；断连/未授权不再执行截图，其他失败尽力截图（`scripts/heartbeat/heartbeat.py:95-117`，截图写 `screenshots/heartbeat-preflight-<timestamp>.png`，`:107-109`）。

#### HRA-A30 创建心跳并立即开始首轮
- 代码：`scripts/heartbeat/heartbeat.py:165-172`、`scripts/heartbeat/store.py:42-63`
- 前置：无活动心跳（除非 `--recover`）；`interval_seconds >= 1`；无对账阻塞。
- 动作：`BEGIN IMMEDIATE` 检查无 `active` 行 → 插入 `state='active'`、`round_in_progress=1`、`next_due_at=now`。
- 判据/失败：插入成功且读取到该 run；已有活动心跳抛 `RuntimeError`，事务回滚。
- 副作用：本地写入；唯一索引 `one_active_heartbeat` 保证最多一个活动心跳（`scripts/heartbeat/store.py:37-38`）。

#### HRA-A31 查询心跳状态
- 代码：`scripts/heartbeat/heartbeat.py:180-203`、`scripts/heartbeat/store.py:78-114`
- 动作：无设备动作；按 `run_id` 或活动行读取，计算 `round_age_seconds`、`due`、`seconds_until_due`。
- 判据/失败：读取成功；无活动返回 `not_active`；指定 ID 不存在返回 `not_found`。
- 副作用：只读，幂等。

#### HRA-A32 等待到期（轮询）
- 代码：`scripts/heartbeat/heartbeat.py:445-461`、`414-444`
- 前置：存在活动心跳且当前无进行中轮次。
- 动作：按 `poll_seconds` 睡眠；每次醒来读取心跳行；未跳过设备检查时执行 `HRA-A37`。
- 输出/失败：继续等待/`stopped`/`already_running`/`blocked`；心跳被停止返回 `_stopped_result`，设备检查失败停止心跳并返回原因。
- 副作用：睡眠与轮询无业务副作用；`main.py` 用独立设备命令包裹每次检查（`scripts/main.py:1153-1169`）。

#### HRA-A33 原子占用轮次
- 代码：`scripts/heartbeat/heartbeat.py:468-474`、`scripts/heartbeat/store.py:117-125`
- 前置：已到期、无进行中轮次、无对账阻塞（`HRA-P17`）。
- 动作：`UPDATE ... SET round_in_progress=1, round_started_at=? WHERE run_id=? AND state='active' AND round_in_progress=0 AND next_due_at<=?`。
- 判据/失败：`rowcount == 1`；抢不到时重新读取，若已在轮次中返回 `already_running`。
- 副作用：本地写入；原子，不会双开轮次。

#### HRA-A34 结束轮次并设置下次到期
- 代码：`scripts/heartbeat/heartbeat.py:233-239`、`scripts/heartbeat/store.py:128-142`
- 前置：存在进行中轮次；无对账阻塞。
- 动作：`UPDATE ... SET round_in_progress=0, last_round_started_at=round_started_at, round_started_at=NULL, last_round_finished_at=?, next_due_at=?`；`next_due_at = finished_at + interval_seconds`。
- 判据/失败：`rowcount == 1`；状态已变化抛 `RuntimeError("心跳轮次状态已变化")`。
- 副作用：本地写入；重复调用会因 `round_in_progress=1` 条件不再命中。

#### HRA-A35 审计门控回退轮次
- 代码：`scripts/heartbeat/heartbeat.py:240-267`、`scripts/heartbeat/store.py:145-160`
- 前置：`audit_mode='enforce'`；合并结论为 `fail` 或 `judge_untrusted`。
- 动作：`UPDATE ... SET round_in_progress=1, round_started_at=COALESCE(last_round_started_at, round_started_at), last_round_finished_at=NULL, next_due_at=<原值>`。
- 判据/失败：`rowcount == 1`；状态已变化抛 `RuntimeError`。
- 副作用：本地写入，返回 `paused`（`reason_code=audit_failed/judge_untrusted`）；报告/审计故障只记录错误类型，不改变裁决（`scripts/heartbeat/heartbeat.py:309-349`）。

#### HRA-A36 停止心跳
- 代码：`scripts/heartbeat/heartbeat.py:398-411`、`scripts/heartbeat/store.py:163-174`
- 前置：`--confirmed`；存在活动心跳；`reason` 为最多 64 位字母/数字/下划线。
- 动作：`UPDATE ... SET state='stopped', ended_at=?, stop_reason=?, round_in_progress=0, ...`。
- 判据/失败：`rowcount == 1`；无活动心跳抛 `RuntimeError`；原因码非法抛 `ValueError`。
- 副作用：本地写入；停止后没有自动恢复路径。

#### HRA-A37 心跳内设备/锁屏检查
- 代码：`scripts/heartbeat/heartbeat.py:55-75`
- 前置：存在活动心跳。
- 动作：调用注入的 `device_check`（默认 `CORE-A03` 链路）与 `unlock_check`（默认 `CORE-A07`）。
- 判据/失败：两项检查均不抛异常；`DispatchStateError` 取 `reason`，断连取 `error.reason`，其他 ADB/运行时错误归 `device_disconnected` 或 `device_locked`，随后调用 `HRA-A36` 的 store 停止函数（`scripts/heartbeat/store.py:163-174`）。
- 副作用：只读检查 + 可能的本地停止写入。

#### HRA-A38 显式恢复心跳
- 代码：`scripts/heartbeat/heartbeat.py:157-164`、`scripts/heartbeat/store.py:177-189`
- 前置：`--recover`；存在活动心跳。
- 动作：`UPDATE ... SET state='stopped', stop_reason = CASE WHEN round_in_progress=1 THEN 'round_interrupted' ELSE 'restarted_after_reconnect' END`，随后 `HRA-A30` 创建新 run。
- 判据/失败：`rowcount == 1` 且新 run 创建成功；状态已变化抛 `RuntimeError`。
- 副作用：本地写入；仅在显式恢复路径执行。

#### HRA-A39 草稿完成落库
- 代码：`scripts/hr/store.py:2183-2198`（`complete_manual_draft`）、`scripts/hr/review.py:510-514`
- 前置：`HRA-A03` 返回 `sent`/`delivered`。
- 动作：把最新 `pending` 回复事件改为结果；草稿改 `sent`；会话 `hr_status='replied'`；关闭消息（`replied`）。
- 判据/失败：事件或草稿至少 1 行被更新；数据库异常向上抛出，已发送事实不因落库失败而回滚。
- 副作用：本地写入；重复调用不会重复发送。

#### HRA-A40 发送后返回列表
- 代码：`scripts/hr/review.py:518-530`
- 前置：`HRA-A03` 已返回成功；设备仍连接。
- 动作：执行**返回列表序列**（最多 3 轮）。
- 判据/失败：列表可解析；断连时不再执行任何 ADB，直接返回已发送结果；3 次失败标 `needs_list`。
- 副作用：只改变页面；不改变已发送事实。

#### HRA-A41 保存停止现场截图
- 代码：`scripts/hr/cleanup.py:441-449`（`_capture_stop_evidence`）
- 前置：停止原因不是 `device_disconnected`（`scripts/hr/cleanup.py:547-548`）。
- 动作：调用 `CORE-A22` 写入 `screenshots/hr-cleanup-<time_ns>.png`。
- 判据/失败：文件写入成功；失败只记录 `screenshot_error` 异常类型，不改变停止原因。
- 副作用：在本机磁盘产生截图；含聊天画面，属敏感本机产物；不产生对外消息。

### 5.3 数据与副作用边界（动作域）

- **本地数据库**：`hr_action_events`（意图/结果/待核对）、`hr_reply_drafts`、`hr_messages`、`hr_conversations`、`hr_cleanup_ledger`、`hr_non_text_pending`、`hr_non_text_review_events`、`heartbeat_runs`。写入集中在 `scripts/hr/store.py` 与 `scripts/heartbeat/store.py`；进程中断后以数据库事实为准，不靠内存恢复。
- **操作日志**：`CORE-L01`–`CORE-L03` 记录命令拒绝、人工处置、输入法恢复失败、发送后尾部未知等；日志只含码值与摘要键，不含正文（`scripts/hr/actions.py:849`、`870`、`scripts/hr/apply.py:870`）。
- **截图**：清理停止现场写入 `screenshots/hr-cleanup-<time_ns>.png`（`scripts/hr/cleanup.py:443`）；心跳预检写入 `screenshots/heartbeat-preflight-<timestamp>.png`（`scripts/heartbeat/heartbeat.py:107-109`）。截图含聊天画面，属敏感本机文件。
- **对外发送**：`HRA-A02`（文本）、`HRA-A04`/`HRA-A05`（联系方式请求与确认）、`HRA-A07`/`HRA-A08`（附件简历）会立即对 HR 可见且不可撤回；`unknown` 必须人工核对，不得自动重试。
- **对外删除**：`HRA-A25` 删除手机端会话不可逆；本地数据库不会同步删除，后续同名 HR 再发消息可能以新会话出现（`scripts/hr/store.py:710-733` 保留上下文）。
- **输入法副作用**：`CORE-I02`/`CORE-I05` 改变设备全局输入法；异常退出可能遗留 ADBKeyboard，需人工确认。
- **心跳调度**：`HRA-A30`/`HRA-A33`/`HRA-A34`/`HRA-A35`/`HRA-A36`/`HRA-A38` 都是本地状态迁移；心跳本身不直接发消息，业务动作仍走 `hr apply`/`hr review`。


## 6 跨域组合流程

> 本节只给 ID 序列与分支意图，不重复动作细节。`读屏` = `CORE-A10`–`CORE-A14`，`返回列表序列` 见 0.2。

### 6.1 打开应用并到达可操作页面

**公共前置（所有 `adb`/`workspace` 命令）**

1. `CORE-P01` 在设备闸门事务内确认无占用（`scripts/main.py:1623-1628`）。
2. `CORE-P02` 登记本次命令占用（`scripts/hr/scan.py:761-773`）。
3. `CORE-A02` → `CORE-A03` 读取设备清单并选定唯一已授权设备。
4. 执行命令主体。
5. `CORE-P03` 按 token 释放占用（`scripts/hr/scan.py:797-801`）。
6. 占用冲突时以退出码 2 拒绝，不执行任何设备动作（`scripts/main.py:1609-1633`）。

**workspace ensure**

1. 公共前置的 `CORE-P01`/`CORE-P02`/`CORE-A02`/`CORE-A03`，但设备错误只映射为 `device_error` 结果，不抛给 CLI（`scripts/core/workspace.py:455-461`）。
2. `CORE-A05` 一次读窗口 → `CORE-A07` 判解锁 → `CORE-A06` 取前台组件（`:464-473`）。
3. 前台非 BOSS：`CORE-W15` 判来源 → `CORE-A08` 解析入口 → `CORE-A09` 启动 → 以 0.5 秒间隔循环 20 次（读屏 + `CORE-W14`）等待 BOSS 页面（`:475-490`）。
4. 前台是 BOSS：直接读屏 + `CORE-W14`（`:491-495`）。
5. 弹窗：`CORE-W08` → `CORE-W18`（内含 `CORE-W09` 判定、一次点击、重读），至多 2 次（`:354-370`、`:497`）。
6. 若 `CORE-W14` 为 `recommend_feed`：先 `CORE-W05` 找底部“消息”（`:499-503`）；找不到则 `CORE-A18`(4) + 读屏 + `CORE-W14`，至多 2 次（`:504-533`）。
7. 若为聊天详情/职位详情：`CORE-A18`(4) + 读屏 + `CORE-W14`，再回到第 6 或第 8 步（`:535-552`）。
8. 主界面路径：读屏复核 → `CORE-W08`/`CORE-W18` 清弹窗 → `CORE-W05` → `CORE-W16` → `CORE-W17`（8 次、0.5 秒，要求 `chat_list` 且选中态为 True，`:373-444`）。
9. 已在 `chat_list` 时直接返回 reached（`:554-557`）。
10. 任何断连经 `CORE-A04` 收敛为 `device_error`（`:447-452`）。

### 6.2 浏览职位

**inspect（读当前屏卡片）**

公共前置 → `CORE-A05`/`CORE-A06` → 读屏 → `JOBS-P01` → 对每张卡 `JOBS-P04` → 本地记录发现 → 输出 JSON。意图：只读盘点当前可见职位，标注可沟通/跳过原因，不点击。

**open-item（打开指定卡片）**

公共前置 → 读屏 → `JOBS-P01` → 序号越界检查 → `JOBS-P02` + `JOBS-P03`（不完整即 `not_fully_visible`）→ `JOBS-P04`（黑名单关闭）→ `JOBS-A02` → 最多 5 ×（等待 0.4 秒 + 读屏 + `JOBS-P05`）→ 写 `data/pending_job.json`。

**capture（详情入库）**

读屏 → `JOBS-P05` → `JOBS-P06` 判完整度 → 与 `pending_job.json` 做标题/公司一致性校验 → 缺字段报错 → 本地查重（`JOBS-P04` 口径 + 等价职位）→ `save_job` → 列表别名写入 → 完整时删除待采集文件。无设备写入动作。

**confirm-identity（命令行身份核对）**

读屏 → `JOBS-P05` → `JOBS-P07` → 本地 `confirm_identity`。

**scroll（列表翻页）**

公共前置 → 读屏 → `JOBS-P02` → `JOBS-A01` → 读屏 → `CORE-U03` 比较视口。

**batch probe（只读通知探测）**

读屏 → `CORE-M05` → `JOBS-P15` → 命中即返回；未命中 `CORE-M06`(up) → 到底后 `CORE-M06`(down) → 仍无则结束。全程不点击。

**batch run（批次主循环）**

`JOBS-P16` 门禁 → 读屏 → [卡片非空：逐卡 `JOBS-P04` → 选中候选] → 若卡片不完整：`JOBS-A01`(up) 最多 2 次重试 → `JOBS-A02` → 读屏 + `JOBS-P05` → 本地 `capture_step` → 最多 20 次（读屏 + `JOBS-P07`，不匹配则 `JOBS-A01`(down)）→ 6.3 的沟通段 → `返回列表序列` → 回到循环。列表为空时按“`CORE-M05` → 6.2 的新职位通知流程 → `JOBS-A08` → 报 `job_list_not_loaded`”顺序恢复。

**新职位通知流程**

读屏 → `CORE-M05` → `JOBS-P15` → `JOBS-A10` → 读屏 →（仍为消息列表则交回 batch run）→ `JOBS-P14` 未选中则 `JOBS-A09` → 最多 5 屏 / 12 秒轮询读屏，连续两次同签名或骨架屏即停。

**recommend capture（推荐机会捕获）**

读屏 → `CORE-W14`(`chat_detail`) → `JOBS-P11` → `CORE-E06`(卡片) → `JOBS-A03` → 3 ×（读屏 + `JOBS-P12`）→ `CORE-E06`(弹层) → `JOBS-P13` → 写单行契约 JSONL（拒绝覆盖）。任一判定失败按固定 `reason_code` 收尾，不进入点击序列。

### 6.3 发起沟通

**单条 continue（独立命令 / 批次复用）**

本地门禁（`JOBS-P16` 子集、黑名单、`JOBS-P04` 口径）→ 读屏 → `JOBS-P05` → `JOBS-P07` → `JOBS-P08` → 本地 `reserve_job` + `mark_tap_started` → `JOBS-A06` → 等 1.0 秒 → 3 ×（读屏 + `JOBS-P09`；命中 `JOBS-P10` 时插入 `JOBS-A07`，且只补点一次）→ 资源消息段 → 本地 `complete_job`。

**资源消息段（职位域）**

`CORE-I01` → `CORE-I07` → `CORE-I02` → `CORE-I06` → `CORE-I03` → `CORE-I04` → `CORE-I08` → `JOBS-A11` → `JOBS-P18`；结束时必然经过 `CORE-I05` 恢复输入法（`finally`）。任一前置不满足即 `not_sent`，不进入点击。

**recommend apply（推荐职位沟通）**

契约/判定结构校验 → 读屏 + `JOBS-P12` → `JOBS-A05` → 读屏 → `JOBS-P05` → `JOBS-P07` → 本地 `save_job` + `confirm_identity` + `create_opportunistic_batch` + `reserve_job` + `mark_tap_started` → `JOBS-A06` → 3 ×（读屏 + 推荐版三证据；仅当仍是同一详情页时才重试）→ 资源消息段 → 本地 `complete_job` → `返回列表序列`。`decision = none` 或存在活动批次时只走 `JOBS-A04` 关闭弹层。

### 6.4 读取未读消息（hr scan）

**主流程（`scripts/hr/scan.py:1895` 起）**

`CORE-A02`/`CORE-A03` → `CORE-W14`/`CORE-W07`（`_read_list_root`，`:2057`）→ `CORE-W18` → `CORE-M09`（若存在筛选且不复用）→ `CORE-M08` → `CORE-M04` → `CORE-M07`（证明首屏）→ `HRR-R33`（首屏取证）→ `HRR-R02` → `CORE-M01`（含 `CORE-M03` 像素判定）→ `HRR-R03`（角标兜底）→ `HRR-R04` → `HRR-R08` → `HRR-R05`/`HRR-R06`（已读阶段）→ `HRR-R26` → `HRR-R07` → `HRR-R10` → `HRR-R11` → `HRR-R12` → `HRR-R13` → `HRR-R14` → `HRR-R15`/`HRR-R16` → `HRR-R18` → `HRR-R19` → `HRR-R20` → `HRR-R21` → `HRR-R22` → `HRR-R25` → `HRR-R24` → `HRR-R34` → `HRR-R23` → `HRR-R30` → `HRR-R31` → `返回列表序列` → `HRR-R32`。

意图：先收敛到可读的未读列表，再逐个候选“定位→打开→核验→读全→解析→门禁→落库→回列表”。

**定向重开（`--conversation`，`scripts/hr/scan.py:1991-1993` 转 `1648`）**

`CORE-A02`/`CORE-A03` → `CORE-W18` → `CORE-M09` → `CORE-M04` → `HRR-R09` → `HRR-R10` → `HRR-R11` → `HRR-R12` → `HRR-R13` → `HRR-R14` → `HRR-R15`/`HRR-R16` → `HRR-R18` → `HRR-R19` → `HRR-R20` → `HRR-R21` → `HRR-R22` → `HRR-R25` → `HRR-R30` → `HRR-R31` → `返回列表序列`。跳过列表候选筛选，直接按身份重开已知会话；未在首屏时按 3 下 + 9 上滚动重找（`:1714-1726`）。

### 6.5 同步已读消息（hr sync）

`CORE-A02`/`CORE-A03` → `CORE-W14` → `CORE-W18` → `CORE-M09` → `CORE-M04` → `CORE-M01` → `HRR-R05` → `HRR-R06`（列表级筛选）→ `HRR-R08` → `HRR-R26`（轮次去重）→ `HRR-R07` → `HRR-R10` → `HRR-R11` → `HRR-R12` → `HRR-R13` → `HRR-R14` → `HRR-R18` → `HRR-R19` → `HRR-R20` → `HRR-R21` → `HRR-R28` → `HRR-R29` → `返回列表序列`。

失败候选按 `read_incomplete`/`chat_open_unverified`/`contact_mismatch` 跳过（`scripts/hr/sync.py:18-21`、`114-120`）。

### 6.6 回复消息

**hr apply：interested（回复 + 简历/联系方式）**

1. 读契约与决策，取出唯一 `message_key`（`scripts/main.py:940-955`）→ `HRA-P18` 授权检查。
2. `HRA-A09` 登记 `resume`/`wechat`/`phone`/`reply` 的 `pending`（`scripts/hr/apply.py:684`）。
3. `resume`：`HRA-A06` → `HRA-P12` → `HRA-A07` → `HRA-P10`；或 HR 已索取时 `HRA-A08` → `HRA-A07`。
4. `wechat`/`phone`：`HRA-A04` → `HRA-P11` → `HRA-A05`。
5. `reply`：`CORE-I01` → `CORE-I07` → `CORE-I02` → `CORE-I06` → `CORE-I03` → `CORE-I04` → `CORE-I08` → `HRA-A02` → `HRA-A03`。
6. `HRA-A10` 收敛每个动作；`HRA-P02` 命中或 `needs_probe`/`needs_attachment` 立即暂停（`scripts/hr/apply.py:782-790`）。
7. 发送成功后重新读聊天：`HRA-P09` 确认尾部；必要时 `HRA-A13` 保存草稿（`scripts/hr/apply.py:819-887`）。
8. 结束发现、`返回列表序列`、刷新基线（`scripts/hr/apply.py:420-450`）；`CORE-I05` 在发送段结束时恢复输入法。

**hr apply：declined / needs_human / uncertain**

- `declined` + `reply.action=send`：`HRA-A09`(reply) → 回复段 → `HRA-A10` → 关闭 `declined_sent`（`scripts/hr/apply.py:591-597`、`889-893`）。
- `declined` + `draft`：`HRA-A13`，关闭 `declined`（`:593-595`）。
- `needs_human`/`uncertain`：`HRA-A13`（若 draft）→ 固定等待的 `HRA-A10` 前置门禁 → 发送 `HOLDING_REPLY_TEXT`（回复段，正文固定）→ `HRA-A10`；`unknown` 暂停，`not_sent` 记升级（`:572-589`、`:278-367`）。

**hr review send-text（主动追加文本）**

1. 校验 64 位会话键、正文长度 1-300、无手机号/微信号、24 小时去重与每日 3 条上限（`scripts/hr/review.py:104-145`、`173-191`）。
2. 读屏核验联系人（`JOBS-P17`，`:195-201`）→ 再次检查 `HRA-P06` 与身份。
3. `HRA-A09` 登记 `reply/pending/manual_followup`（`:213-214`）。
4. 回复段（正文为人工文本）。
5. 成功：`HRA-P09` 尾部核验 → 记录最后我方消息 → `HRA-A10`(`sent`)（`:233-260`）。
6. 未知/失败：`HRA-A10`(`unknown/needs_probe/failed/not_sent`)，不重试（`:262-278`）。

**hr review send-draft**

1. `HRA-P16` 预检（`:94-101`）→ `CORE-A02`/`CORE-A03`（`:302-308`）。
2. `返回列表序列` → `CORE-M09` → `HRA-A19` 定位会话行（`:446-479`）→ `HRR-R11` → `HRA-P09`/`HRA-P16`。
3. `HRA-A09` 登记 `reply/pending/draft_send`（`:500-501`）。
4. 回复段（正文为草稿文本）。
5. 成功：`HRA-A39` → `HRA-A40`（`:510-530`）；失败：`HRA-A10` 收敛，明确未发送可重试。

**hr review continue（blocked 恢复）**

1. 校验 `--confirmed`、`message_key`、`--serial`（`:350-355`）。
2. 从持久 blocked 轮次读取契约/决策路径并核对摘要（`:356-378`）。
3. 在设备命令围栏内重跑 `apply`，只允许 `interested` 且只执行已核对 `needs_probe`（`:384-395`、`scripts/hr/apply.py:454-496`）。
4. 复用 interested 的动作链；恢复前重读聊天尾部（`HRA-P09`）。

**hr review 待核对处置（离线）**

`HRR-R35`/`HRR-R36` 或 `HRA-P06`/`HRA-P07` 清单 → 人工在应用内目视核对 → 未发送 `HRA-A11`；已发送 `HRA-A12`；消息整体处理完 `HRA-A15`；非文本 `HRA-A17`；仅简历状态 `HRA-A18`；草稿正文需改 `HRA-A14`；需重扫 `HRA-A16`。每步都经 `HRA-P18` 的 `--confirmed` 门禁。

### 6.7 清理会话

**hr apply：rejected（删除会话）**

1. 先确认 `--serial` 与授权 `delete_rejected`（`scripts/hr/apply.py:512-523`，`HRA-P18`）。
2. 若 `keeps_contact`：只 `HRA-A04`/`HRA-A05` 交换电话，不删除（`:603-604`）。
3. 否则 `返回列表序列` → `CORE-M07` → `HRA-A19`（`force_rejected=True`、`limit=1`、`min_age_days=0`）→ `HRA-A20` → `HRA-A21` → `HRA-A01` → `HRA-A22` → `HRA-A23` → `HRA-A24` → `HRA-A27` → `HRA-A25` → `CORE-A21` → `HRA-A26` → `HRA-A28`（`:558-568`）。
4. 只有 `deleted_count == 1` 才写 `rejected` 并关闭消息；否则暂停 `rejected_cleanup_unverified`。

**hr cleanup list（只读候选）**

`HRA-A29` 设备预检 + `HRA-P13` 限制校验（`scripts/hr/cleanup.py:228-232`）→ 读屏 + `CORE-W14` 页面判定（`:246-251`）→ `CORE-M07` → `HRA-A19` → 输出 `candidates/skipped/counts`。不写台账、不改业务表（`:219-221`）。

**hr cleanup run（删除）**

1. `HRA-P18` 的 `--confirmed` 与限制校验（`:500-509`）。
2. 若存在“取消筛选”：`CORE-M09`（`:714-726`）。
3. `CORE-M07` → `HRA-A19` → `HRA-A20` → `HRA-A27` → `HRA-A21` → `HRA-A01` → `HRA-A22` → `HRA-A23` → `HRA-A24` → `HRA-A25` → `CORE-A21` → `HRA-A26` → `HRA-A28`。
4. 任一不确定立即停止并由 `HRA-A28` 记 `unknown`，不重试（`:537-551`）；停止现场截图 `HRA-A41`。

### 6.8 心跳

1. `heartbeat start`：`HRA-A29` → `HRA-A30`（首轮立即进行中）；只读查看进度用 `HRA-A31`。
2. `heartbeat wait`：`HRA-A32` 轮询 → `HRA-A37` → `HRA-A33` 占用。
3. 业务侧执行读取或动作（`hr scan`/`hr apply`，见 6.4–6.7）。
4. `heartbeat round-done`：`HRA-A34` → 报告 → 审计/B 档判定（`scripts/heartbeat/heartbeat.py:309-349`）→ 若 `enforce` 且不通过，`HRA-A35` 回退；否则 `waiting`。
5. `heartbeat stop`：`HRA-A36`；重连后 `--recover`：`HRA-A38` → `HRA-A30`。

### 6.9 失败恢复与设备占用恢复

**读取侧失败恢复**

`HRR-R32` → `HRR-R01`（安全三态）→ `CORE-W18` → `CORE-W16` → `CORE-W17` → 回到 6.4 的解析步骤。只在能证明前台安全时发送返回键，绝不盲退。

**device recover（离线，不触达 ADB）**

`CORE-P05` 校验 `--confirm-stopped`、nonce/token、PID 范围与 host → `CORE-P01` 读取占用行（无行返回 `unchanged/no_reservation`）→ `CORE-P04` 判定 PID 已停止（未证明停止返回 `blocked`）→ `CORE-P03` 释放并返回 `released`（`scripts/hr/store.py:1073-1086`）。

## 7 数据与副作用、幂等性与不可逆动作

### 7.1 本机与设备侧产物

| 位置（相对仓库根或设备） | 内容 | 写入方 |
| --- | --- | --- |
| `data/jobs.sqlite3` | 职位、发现、别名、黑名单、批次、资源消息状态、冷却、HR 会话/消息/动作事件/草稿/清理台账/非文本待办、轮次状态、心跳调度 | `JOBS-*`、`HRR-R*`、`HRA-*`、`CORE-P*` 的本地写入项 |
| `data/operations.log` + `.lock` | 脱敏 JSONL 事件；白名单标量；不含正文 | `CORE-L01`–`CORE-L03` |
| `data/pending_job.json` | 单条待采集卡片 + 详情，完整入库后删除 | `JOBS-A02` 链 |
| `data/job-work/recommend-<时间戳>.jsonl` | 推荐机会契约，单行、拒绝覆盖 | 6.2 recommend capture |
| `screenshots/<本地日期>/round-<id>/NN-<label>.png` | 事务证据截图 | `CORE-E06` |
| `screenshots/<本地日期>/<round\|transaction-id>/*-effects.jsonl/.before.xml/.after.xml` | 脱敏观测归档 | `CORE-O03` |
| `screenshots/hr-cleanup-<time_ns>.png` | 清理停止现场 | `HRA-A41` |
| `screenshots/heartbeat-preflight-<timestamp>.png` | 心跳预检失败现场 | `HRA-A29` |
| `/sdcard/job_tracker_ui.xml`（设备） | 每轮先删后写的层级 dump | `CORE-A10`/`CORE-A11` |

数据库连接：`scripts/core/db.py:13-37` 只读打开（`mode=ro`，可加 `immutable=1`），不建库、不迁移；业务库固定 `data/jobs.sqlite3`（`scripts/core/db.py:10`）。设备占用存于 `hr_device_commands`，含 serial/token/dispatch_nonce/owner_pid/owner_host/cancelled（`scripts/hr/store.py:1003-1037`）。

### 7.2 幂等性分级

| 级别 | 含义 | 代表 ID |
| --- | --- | --- |
| 纯判定/纯函数 | 重复执行结果相同，无副作用 | `CORE-U*`、`CORE-W*`、`CORE-M01`–`CORE-M05`、`JOBS-P*`、`HRR-R05`–`HRR-R25`、`HRA-P*` |
| 只读设备 | 可重复读，但结果随时钟/动画/通知变化 | `CORE-A05`、`CORE-A19`–`CORE-A21`、`HRR-R02`、`HRR-R03` |
| 有条件的本地写 | 靠唯一键/状态条件收敛 | `CORE-P02`/`CORE-P03`、`HRR-R07`、`HRR-R26`–`HRR-R29`、`HRR-R34`、`HRA-A09`/`HRA-A10`、`HRA-A13`–`HRA-A18`、`HRA-A27`/`HRA-A28`、`HRA-A30`–`HRA-A38` |
| 追加写 | 每次调用新增一行/一文件 | `CORE-E07`、`CORE-L01`–`CORE-L03`、`CORE-O03`、`HRR-R31`、`CORE-E06` |
| 页面动作 | 改变前台/列表/焦点，可能不可逆 | `CORE-A09`、`CORE-A16`–`CORE-A18`、`CORE-M06`–`CORE-M09`、`CORE-W16`/`CORE-W18`、`CORE-I02`–`CORE-I06`、`JOBS-A01`–`JOBS-A10` |
| 不可逆对外动作 | 发出即对 HR 可见或从手机删除，无法撤回 | `HRA-A02`、`HRA-A04`、`HRA-A05`、`HRA-A07`、`HRA-A08`、`HRA-A25`、`JOBS-A06`、`JOBS-A07`、`JOBS-A11` |

### 7.3 不可逆动作汇总

| ID | 不可逆内容 | 防重手段 | 结果未知时 |
| --- | --- | --- | --- |
| `JOBS-A06` | 向 HR 发起沟通 | 批次 `reserve_job` → `mark_tap_started` 置 `pending` | 停止，人工核对批次行 |
| `JOBS-A07` | 二次确认后再次沟通 | 流程内 `dialog_clicked` 最多一次 | 停止，人工核对 |
| `JOBS-A11` | 发送资源消息正文 | `claim_resource_share` 先落 `pending` | `unknown`，禁止重发（`HRA-P02` 同口径） |
| `HRA-A02` | 发送文本消息 | `HRA-A09` 先落 `pending` | `HRA-P02`：`unknown`，人工核对 |
| `HRA-A04`/`HRA-A05` | 交换微信/电话请求与确认 | 槽位“请求中/查看”判已处理 | `unknown`，不重试 |
| `HRA-A07`/`HRA-A08` | 交付附件简历 | `claim_action` + `HRA-P10` 交付证据 | `unknown`，不重试 |
| `HRA-A25` | 删除手机端会话 | `HRA-A27` 台账先归档；`HRA-A24` 点名核对 | `delete_unverified`，`HRA-A28` 记 `unknown`，不重试 |

### 7.4 门禁与阻断状态

- **全局未核实门禁**：`HRA-P06`。最新动作状态为 `pending/unknown/failed/needs_probe` 时阻塞新的 `apply`、`send-text`、固定等待与清理义务判定。
- **批次阻断类**：`needs_reconciliation`、`needs_resource_share_review`、`unconfirmed`、`needs_list`、`needs_identity_review`、`needs_detail_open_review`：不写冷却、不收尾，核对后可续跑（`scripts/jobs/batch.py:27-31`、`:92-135`）。
- **读取侧阻断**：`needs_list`（`CORE-M04`/`HRR-R32` 失败）、`paused/pending_action`（`HRA-P06`）、`paused/filter_unverified`（`CORE-M08`）、`paused/unread_remains`（`HRR-R03`）、`paused/read_incomplete`（`HRR-R20`）、`paused/recommend_opportunity`（`HRR-R24`）。
- **授权门禁**：`HRA-P18` 的授权字段与 `--confirmed`；本稿不授予任何一项。
- **设备闸门**：`CORE-P01`–`CORE-P03`；占用冲突退出码 2；`device recover` 只能恢复同主机、无派发 nonce、PID 已停止的内联占用。

## 8 未覆盖与不确定项

> 四份草稿共 51 条原始条目；其中 5 条属于重复表述，已合并为 8.0 的 2 条，剩余 46 条按域列出，合计 48 条。

### 8.0 共通（合并重复项）

1. **全部结论来自静态阅读**：未运行 Python、未执行 ADB、未连接设备，未读取 `config.json`/`data/`/`screenshots/`/数据库/日志；行号以阅读时文件为准，旧项目变更后会漂移（原 core 第 18 条、jobs 第 12 条、hr-read 第 9 条、hr-act 第 10 条合并）。
2. **旧项目目录指南仅作背景**：旧项目各目录 `AGENTS.md` 未逐条与代码比对，如冲突以代码为准（原 core 第 18 条、hr-act 第 2 条合并）。

### 8.1 设备与页面

1. **`--any-device` 实际未生效**：解析器定义了该开关（`scripts/main.py:217`），但 `_recover_device` 只把 `args.serial or ""` 传给恢复函数（`scripts/main.py:1640`）；用空串查询只会命中 `serial=''` 的占用。
2. **恢复只支持同主机内联占用**：host 必须等于本机（`scripts/hr/store.py:1081`），带 `dispatch_nonce` 的占用直接 `blocked/dispatch_reservation`（`:1079-1080`），跨机器或派发占用无法通过该命令恢复。
3. **`window_state` 解锁语义偏保守**：只要 `dumpsys window` 没有输出 `isKeyguardShowing`，集合为空就不等于 `{"false"}`，会被判未解锁（`scripts/core/adb.py:162-164`）；不同 ROM/Android 版本是否稳定输出该字段未验证。
4. **`screen_action` 把“变化但未稳定”当成功**：`if not observed["stable"] or observed["changed"]: return True`（`scripts/core/adb.py:350-351`），画面仍在动画时也可能返回 True；重试边界只有 2 次。
5. **`wait_for_screen` 的 `changed` 依赖旧摘要**：未传 `previous_hash` 时永远为 False（`scripts/core/adb.py:328`）；超时返回 `stable=False` 与 `changed` 的组合语义需调用方自行解释。
6. **长按与同点短滑通道未验证真实响应**：`tap_bounds` 的 `long_press` 用同点 `swipe` 800 ms、`input_swipe` 用同点 60 ms（`scripts/core/adb.py:258-262`），代码注释明确“不保证 BOSS 控件响应”；清理另用 1200 ms。实际 UI 行为未知（原 core 第 6 条与 hr-act 第 3 条合并）。
7. **层级 dump 使用固定路径**：`/sdcard/job_tracker_ui.xml` 是全局固定文件（`scripts/core/adb.py:212`），绕过 CLI 直接调用 core 的两个进程会互相覆盖；`uiautomator dump` 退出码 137 只重试一次（`:214-222`）。
8. **文本输入实现不在 core 包**：core 没有 `input text`/粘贴函数，文本输入完全位于 `scripts/hr/actions.py` 并依赖外装 ADBKeyboard 广播（`scripts/hr/actions.py:705`、`733-735`、`755-768`）。本文把 `CORE-I01`–`CORE-I08` 归入 CORE 域只是编号归属，不改变实现位置；重新设计时需决定输入层归属与失败恢复策略。
9. **未读判定存在两种证据源**：层级角标存在即 `yes`（`scripts/core/message_list.py:269`），与像素阈值 `>=100`（`:278-279`）可能不一致；`unread_filter_verified=True` 时所有行都被标 `yes`（`:269`），无法区分个别已读行。
10. **无名字但可点击的消息行**：会被写成空字段占位记录并标 `kind=unknown`（`scripts/core/message_list.py:252-257`），其真实含义（分隔符/系统卡/加载占位）未在代码中说明。
11. **页面分类可能误判**：`_dialog_signal` 把“同意/允许/确定”等通用按钮文案当作弹窗信号（`scripts/core/workspace.py:31-34`、`207-209`），虽有“排除消息正文”保护（`:189-210`），职位详情等页面出现同名按钮时仍可能归类为 `popup`。
12. **`workspace` 在 chat_list 分支上再次导航**：从聊天详情返回后若已是 `chat_list`，代码仍定位“消息”目标并调用 `_navigate_from_main`（`scripts/core/workspace.py:549-552`），即在列表页再点一次“消息”tab；真实 UI 效果与必要性无法从代码确认。
13. **`_navigate_from_main` 的页面变更比较较宽**：`confirmed.kind != page.kind` 在 `recommend_feed` 归为 `unknown` 时比较的是 `unknown` 与后续 `chat_list`，推荐页路径依赖额外分支（`scripts/core/workspace.py:393-399`）；实际机型表现未验证。
14. **屏幕几何依赖顶层节点 `top==0`**：`_screen_bounds` 要求候选 bounds 左上为 (0,0) 且唯一（`scripts/core/workspace.py:65-78`）；带刘海/状态栏偏移或根节点非全屏的层级会直接放弃底部导航定位。
15. **`obs.difference` 的“同卡”扩展规则**：只有父节点满足 `card` 命名或同时含“同意/拒绝”、且非 recycler 时才扩展到父级边界（`scripts/core/obs.py:62-79`）；不满足时只按目标 bounds 比较，可能把同卡其他字段变化判为 `changed_elsewhere`。
16. **`tasks`/`status`/`errors` 与设备层无直接调用关系**：`report_status` 只是把停止原因映射为四态（`scripts/core/tasks.py:176-186`），`status.py` 提供词表（如 `NON_HR_NAMES` 被 `message_list.py:281` 使用），`errors.py` 的 `UiUnverified` 在 core/命令路径中未被引用（`scripts/core/errors.py:16-22`）；它们对设备动作的约束只体现在上层流程。
17. **`adb continue` 缺少独立确认开关**：该命令只要求 `--batch-id` 与 `--db`（`scripts/main.py:234-236`），点击“继续沟通”是直接设备动作；是否有上层批次/派发门控不在本节范围内，重新设计时需明确确认边界。

### 8.2 职位

1. **列表上的“已沟通/已投递”徽标未被读取**：跳过判定只来自本地库（`scripts/jobs/enroll.py:351-363`），没有从卡片 UI 读取徽标的代码；页面上是否存在对应 resource-id 未知。
2. **卡片点击点选的是公司名区域**：`scripts/jobs/enroll.py:408` 用 `company_bounds`，代码未说明为何不点整卡；公司名节点缺失时回退整卡 bounds（`:136-138`），实际点击热区是否等价未验证。
3. **详情页滚动依赖容器 id 猜测**：批次身份滚动复用 `scroll_step`，要求容器 resource-id 以 `recycler_view`/`rv_list` 结尾（`scripts/jobs/enroll.py:453-461`）；详情页真实容器 id 未在代码中出现，也未在文档中枚举。
4. **“新职位”tab 选中态属性不固定**：代码同时尝试 `selected`、`isSelected`、`is-selected`（`scripts/jobs/batch.py:230-233`），三者都缺失时返回 None，此时会直接点击一次 tab，真机行为未验证。
5. **推荐弹层关闭控件顺序是经验性兜底**：`iv_close_job_panel` → `btn_confirm` → 文本“我知道了”/“知道了”（`scripts/jobs/recommend.py:273-287`），注释提到真机底部为“我知道了”，但未给出各机型差异证据。
6. **发送依赖外部输入法 ADBKeyboard**（`com.android.adbkeyboard/.AdbIME`）：仓库内没有该 APK 或安装步骤；设备未安装时 `CORE-I02` 会失败并归为 `not_sent`。
7. **资源消息是否发送的最终证据只有“正文节点计数增加”**（`scripts/hr/actions.py:818-825`）：若聊天页做了节点复用或正文被折叠，计数可能不增加而消息已发出，此时按 `unknown` 处理，需人工核对。
8. **批次主循环的时限/次数是硬编码**：外层 80 轮（`scripts/jobs/batch.py:386`）、通知搜索 80 轮（`:290`）、等待列表 5 屏/12 秒（`:239-263`）、身份滚动 20 次（`:527`）、打开重试 5 次（`scripts/jobs/enroll.py:412`）、沟通确认 3 次/1 秒（`scripts/jobs/enroll.py:562-563`）；真机延迟下的合理值未在本仓库论证。
9. **`store.set_hr_status` 引用了 `jobs.hr_status` 列**（`scripts/jobs/store.py:502-505`），但 `jobs` 建表与迁移都没有该列（`:136-169`）。按当前 schema 调用会报 `no such column`；是否由 HR 域另行加列未在只读范围内确认，属疑似不一致。
10. **推荐候选的 `index` 语义依赖列表顺序稳定**（`scripts/jobs/recommend.py:319-330`）：代码只校验同位置标题相等，不校验公司；弹层若插入新行导致整体位移，可能命中同标题不同公司的职位。
11. **`jobs recommend apply` 的 DB 连接只在“已确认 + 契约/判定合法 + `decision=pick`”时创建**（`scripts/main.py:560-564`），其余分支传 None；`apply` 内部对 None 连接只在 `none` 或批次忙分支才会安全返回，其他分支的容错边界未显式断言。
12. **坐标与配额未验证**：所有坐标均为 `bounds` 相对值，未验证分辨率适配；每日配额、冷却时长（`JOB_STAGE_COOLDOWN_SECONDS = 3600`，`scripts/jobs/batch_store.py:12`）的实际取值也未确认。
13. **`inspect` 与 `open-item` 只处理当前可见卡片**（`scripts/jobs/enroll.py:373-374`、`:388-392`），跨屏去重由调用方（人或批次）负责；`visited_cards` 只在批次内存在（`scripts/jobs/batch.py:356`、`:437-440`）。

### 8.3 HR 消息读取

1. **`hr policy.py` 不在读取链路**：`scan.py` 的导入清单（`scripts/hr/scan.py:28-33`）与 `sync.py` 的导入清单（`scripts/hr/sync.py:10-13`）都没有 `policy`；`classify_reply`（`scripts/hr/policy.py:60`）与 `validate_decision`（`:77`）只服务 Phase 2 决策校验，读取侧只产出 `latest_inbound_text` 等字段供其消费。
2. **`verify_contact` 未被 scan/sync 调用**：grep 命中仅 `scripts/hr/actions.py:76`、`265`、`817`、`scripts/hr/apply.py:177`、`219`、`scripts/hr/review.py:195`、`scripts/jobs/enroll.py:671`；读取侧的联系人核验实为 `open_message` 内的 `parse_chat(root)["contact"] == item["name"]`（`scripts/hr/chat.py:958`），两者判定口径不同（前者是标题列表包含匹配，后者是首标题相等），未在代码中统一。
3. **`hr scan`/`hr sync` 不主动导航到消息工作区**：`workspace.ensure_workspace` 仅由 `workspace check` 命令调用（`scripts/main.py:1529`、`scripts/core/workspace.py:447`）；“前置状态”依赖调用方先到位，代码本身不保证。
4. **未读像素阈值来自真机标定注释**（`scripts/core/message_list.py:16-19`），不同屏幕密度/主题下是否仍成立未知；仓库未提供可复现夹具。
5. **星期时间标签只承认“两天前”一种映射**（`scripts/hr/list_evidence.py:87-102` 注释明示只有一次真机证据），3–6 天与跨周样本缺失，`classify_list_time` 对其余情况返回 `unknown`。
6. **可自动关闭弹窗白名单只有一条**“求职防骗提醒”→“知道了”（`scripts/core/workspace.py:36-38`），其他弹窗会被判 `popup` 并让读取暂停，未覆盖其关闭路径。
7. **`read_latest_chat`（`scripts/hr/chat.py:837`）只被 `scripts/hr/apply.py:183` 使用**，读取侧未使用；本文未把它列为原子操作，但人工恢复路径会用到相反方向的滚动。
8. **`_unread_badge_count`（`scripts/hr/scan.py:1453`）未见调用点**，可能是遗留统计函数；未确认其归属，故不作为原子操作。
9. **`hr sync` 的 `mark_unknown_checked` 只在 `completed` 且原因不在 `not_located/unread_handled/list_unchanged` 时执行**（`scripts/hr/sync.py:128-137`），但“哪些原因算已核对”的业务含义只体现在这一处分支，缺少独立文档说明，属于口径不确定项。

### 8.4 HR 动作、清理与心跳

1. **`scripts/core/status.py:17-24` 的 `ACTION_STATUSES` 全集未逐项展开**：本文只确认 `pending/not_sent/request_sent` 是补充状态，完整词表需在实现时以该文件为准。
2. **清理长按 1200 ms 无实测依据**（`scripts/hr/cleanup.py:596-597`）：代码只注释“等画面稳定再长按”，未给出该值的实测来源；可能随机型变化。
3. **拒绝清理的参数组合语义需产品确认**：`hr apply` 的拒绝清理调用 `run_candidates(min_age_days=0, limit=1, force_rejected=True)`（`scripts/hr/apply.py:558-560`），与 CLI 默认 `--min-age-days 0`、常量 `CLEANUP_AGE_DAYS=7` 的组合语义未说明；代码上 `force_rejected` 仍受未读角标/草稿/待办保护，最终边界建议实现前复核。
4. **`heartbeat wait` 的设备占用语义有两条路径**：`main.py` 用独立 `hr_scan.device_command` 包裹设备选择与解锁检查（`scripts/main.py:1153-1169`），而 `heartbeat.py` 默认直接调用 `adb.select_device`；两条路径是否等价，旧项目文档未说明。
5. **`hr apply` 未提供 `--serial` 时的暂停码需按运行时授权确认**：`DeviceApplyExecutor(None)` 会让动作返回 `needs_device`/`needs_attachment`（`scripts/hr/actions.py:357-414`），但拒绝分支已在更早位置要求 `--serial`（`scripts/hr/apply.py:518-523`）。
6. **心跳审计的 A 档/B 档实现未展开**：由 `main.py` 注入（`scripts/main.py:1105-1117`、`1132-1142`），`scripts/audit/*` 内部实现超出本文范围。
7. **聊天读取的只读子流程未在本域展开**：`read_full_chat`、`scroll_chat` 等属读取章节（`HRR-R18`–`HRR-R20`），本域只引用其结论，未重复展开。

## 9 索引：源码文件到操作 ID

### 9.1 逐文件对照

| 源码文件 | 主要 ID |
| --- | --- |
| `scripts/main.py` | 命令解析、`CORE-P01`–`CORE-P03` 闸门调用、`HRA-P18` 参数门禁；无独立原子 ID |
| `scripts/core/adb.py` | `CORE-A01`–`CORE-A23`、`HRR-R02`（`CORE-A19` 调用点） |
| `scripts/core/uitree.py` | `CORE-U01`–`CORE-U06`；`CORE-U03` 被 `JOBS-A01`、`HRR-R18` 复用 |
| `scripts/core/message_list.py` | `CORE-U07`、`CORE-U08`、`CORE-M01`–`CORE-M09` |
| `scripts/core/workspace.py` | `CORE-W01`–`CORE-W18` |
| `scripts/core/events.py` | `CORE-E01`–`CORE-E07` |
| `scripts/core/obs.py` | `CORE-O01`–`CORE-O03` |
| `scripts/core/operation_log.py` | `CORE-L01`–`CORE-L03` |
| `scripts/core/keys.py`、`db.py`、`status.py`、`errors.py`、`tasks.py` | 无原子 ID（纯本地标识/连接/词表/异常/契约，见 8.1-16） |
| `scripts/hr/store.py` | `CORE-P01`–`CORE-P05`、`HRR-R07`/`HRR-R08`/`HRR-R23`/`HRR-R28`/`HRR-R29`/`HRR-R34`、`HRA-A09`–`HRA-A18`、`HRA-A27`/`HRA-A28`、`HRA-A39` |
| `scripts/hr/scan.py` | `HRR-R01`、`HRR-R03`、`HRR-R07`、`HRR-R08`、`HRR-R13`、`HRR-R22`、`HRR-R26`、`HRR-R27`、`HRR-R32`、`HRR-R33`、`HRR-R34` |
| `scripts/hr/list_evidence.py` | `HRR-R05`、`HRR-R06` |
| `scripts/hr/identity.py` | `HRR-R08`、`HRR-R09` |
| `scripts/hr/chat.py` | `JOBS-P17`、`HRR-R10`–`HRR-R21` |
| `scripts/hr/contract.py` | `HRR-R04`、`HRR-R30` |
| `scripts/hr/targeting.py` | `HRR-R25` |
| `scripts/hr/inbox.py` | `HRR-R35`–`HRR-R37` |
| `scripts/hr/sync.py` | `HRR-R28`、`HRR-R29`（调用点） |
| `scripts/hr/actions.py` | `CORE-I01`–`CORE-I08`、`JOBS-P18`、`JOBS-A11`、`HRA-P01`–`HRA-P05`、`HRA-P10`–`HRA-P12`、`HRA-A02`–`HRA-A08` |
| `scripts/hr/apply.py` | `HRA-P06`（调用点）、`HRA-P07`、`HRA-P09`、`HRA-A09`/`HRA-A10`/`HRA-A13` |
| `scripts/hr/review.py` | `HRA-P08`、`HRA-P16`、`HRA-A11`/`HRA-A12`/`HRA-A14`–`HRA-A18`、`HRA-A39`、`HRA-A40`；调用 `CORE-M09` |
| `scripts/hr/cleanup.py` | `HRA-P13`–`HRA-P15`、`HRA-A01`、`HRA-A19`–`HRA-A28`、`HRA-A41`；调用 `CORE-M07` |
| `scripts/hr/policy.py` | 无原子 ID（Phase 2 决策校验，见 8.3-1） |
| `scripts/jobs/enroll.py` | `JOBS-P01`–`JOBS-P10`、`JOBS-A01`、`JOBS-A02`、`JOBS-A06`、`JOBS-A07` |
| `scripts/jobs/recommend.py` | `JOBS-P11`–`JOBS-P13`、`JOBS-A03`–`JOBS-A06` |
| `scripts/jobs/batch.py` | `JOBS-P14`–`JOBS-P16`、`JOBS-A08`–`JOBS-A10` |
| `scripts/jobs/store.py` | `JOBS-P04` 的本地查询、职位/别名/黑名单表 |
| `scripts/jobs/batch_store.py` | `JOBS-P16`、`JOBS-A06`/`JOBS-A11` 的状态门禁、批次状态机 |
| `scripts/jobs/resources.py` | 无原子 ID（批次正文读取与 URL 校验） |
| `scripts/heartbeat/heartbeat.py` | `HRA-A29`–`HRA-A38` |
| `scripts/heartbeat/store.py` | `HRA-A30`–`HRA-A38`（状态写） |

### 9.2 按域统计

| 域 | ID 段 | 数量 |
| --- | --- | --- |
| CORE-A | `CORE-A01`–`CORE-A23` | 23 |
| CORE-U | `CORE-U01`–`CORE-U08` | 8 |
| CORE-M | `CORE-M01`–`CORE-M09` | 9 |
| CORE-W | `CORE-W01`–`CORE-W18` | 18 |
| CORE-I | `CORE-I01`–`CORE-I08` | 8 |
| CORE-E/O/L/P | `CORE-E01`–`CORE-E07`、`CORE-O01`–`CORE-O03`、`CORE-L01`–`CORE-L03`、`CORE-P01`–`CORE-P05` | 18 |
| JOBS | `JOBS-P01`–`JOBS-P18`、`JOBS-A01`–`JOBS-A11` | 29 |
| HRR | `HRR-R01`–`HRR-R37` | 37 |
| HRA | `HRA-P01`–`HRA-P18`、`HRA-A01`–`HRA-A41` | 59 |
| **合计** |  | **209** |
