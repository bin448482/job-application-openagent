# app/adapters/adb 目录指南

## 一、目录用途

实现 M0 显式授权的真实 ADB 只读探针与 M3 显式层级/截图采集；M0 只顺序执行 `adb devices` 与 `adb -s <serial> shell dumpsys window`，M3 只执行 `uiautomator dump`/`cat` 与 `exec-out screencap`。

## 二、当前文件

- `__init__.py`：声明 ADB 适配器包。
- `device.py`：`AdbDeviceProbe`、`DeviceProbeError`、设备清单解析、`isKeyguardShowing` 判定与 `mCurrentFocus` 前台判定。
- `screen.py`：`AdbScreenReader`；每次 dump/cat 或 screencap 只用参数数组、`shell=False`、5 秒超时、不重试；层级 XML 与原始 RGBA 保存到调用方指定的本机证据目录，文件名含独立 UUID，返回受控证据名与内存内容。

## 三、命令与安全契约

- 只用 subprocess 参数数组，`shell=False`；每条命令超时 `5` 秒，不重试。
- 未安装 ADB、超时或非零退出归一为 `device_disconnected`；过程日志分别用固定 `adb_missing/adb_timeout/adb_oserror/adb_nonzero` 错误类别，不扩业务原因码。每条命令在 `subprocess.run` 前记录同 `correlation_id` 的 `started`，完成后记录 `ok` 或 `failed`；中断/未归一异常保留 `started` 便于定位。
- 设备清单只接受 tab 分隔的确定记录；`adb: no devices/emulators found` 等提示行、标题和守护进程行不计入设备数。serial 不在清单也报断连；清单未知状态保守视为 `offline`。
- 显式 serial 只在本机内存精确匹配，输出逻辑别名固定为 `primary`，不输出真实序列号或窗口原文。
- 解锁要求每个 `isKeyguardShowing` 都紧跟 `=` 且值 token 严格为 `true`/`false`（到空白结束）；`false123`、`false-extra`、缺值/缺等号、任意畸形或合法/畸形混杂均为 `null`，不得推定解锁。
- 前台要求唯一的 `mCurrentFocus` 字段行只允许前置缩进，字段名必须是该行开头 token；拒绝 `notmCurrentFocus`/`prefixmCurrentFocus` 等粘连前缀、`prefix mCurrentFocus` 等行内前缀和 `mCurrentFocusExtra` 等后缀字段，任一 line 含该名字却非合法字段即保守 `unknown`。字段值（允许首尾空白）必须恰好是一个完整 `Window{...}` 结构，内部恰好一个 `包名/Activity` token；前后附加文本/组件、同一行或同一 Window 内多个组件、畸形/混合组件、缺失/多值均为 `unknown`。包名 `com.hpbr.bosszhipin` 为 `boss`，其它单一包为 `other`。
- 本模块不得在 fixture 模式或其他离线测试中实际执行；测试只 mock `subprocess.run`。模块导入必须零副作用。
- `AdbScreenReader` 的 dump 后 cat 固定为两条命令；`exec-out screencap` 以二进制捕获。缺失/超时/OSError/非零退出统一 `ScreenReadError('device_disconnected')`，单次 attempt 不重放；证据落盘失败为 `ScreenReadError('invalid_fixture')`，不声称已保存。
- 过程日志 `stage=adb_hierarchy/adb_screen/adb_evidence`、`command=adb_hierarchy_dump/adb_hierarchy_cat/adb_screencap`，只写固定结果、耗时和 `adb_missing/adb_timeout/adb_oserror/adb_nonzero/evidence` 类别；证据保存成功/失败记 `adb_evidence`，失败原因 `invalid_fixture`；不写 serial、设备临时路径、XML、截图或异常文本。
- 真实 `read` 在调用本适配器前必须先通过共享设备门禁；M4 门禁未交付时 usecase 直接拒绝，不以 profile 内任务锁代替。

## 四、测试

```powershell
python -m unittest tests.test_doctor tests.test_screen
```

覆盖 M0 探针成功调用顺序、`shell=False`、5 秒超时、无重试、断连/超时/非零及其日志类别；M3 覆盖 dump/cat/screencap 参数、二进制/文本捕获、证据保存与失败、单次 attempt、门禁先于采集。没有真实 ADB 调用。

## 五、维护要求

1. 修改命令、解析规则或原因码时，同步更新本文件、`app/ports/AGENTS.md`、`app/usecases/AGENTS.md`、根 `AGENTS.md`、`docs/operator.md` 与测试。
2. 不扩展到 M3+ 导航、点击、输入、滚动或聊天动作；新增能力须按里程碑和确认门槛另建。M3 只做层级/截图采集，不做真实页面解析或动作。
3. 每个函数/方法写 docstring，说明输入、输出和安全边界。

## 六、已知限制

- 真实 `adb devices` / `dumpsys window` / `uiautomator dump` / `screencap` 输出未在本机验证；不同 Android 版本的多显示器、字段差异、截图头格式可能使判定失败或未知。
- 当前只在测试中 mock subprocess，未在真机执行；M3 真实 read 在共享门禁交付前不开放。
