# app/adapters/adb 目录指南

## 一、目录用途

实现 M0 显式授权的真实 ADB 只读探针，仅顺序执行 `adb devices` 与 `adb -s <serial> shell dumpsys window`。

## 二、当前文件

- `__init__.py`：声明 ADB 适配器包。
- `device.py`：`AdbDeviceProbe`、`DeviceProbeError`、设备清单解析、`isKeyguardShowing` 判定与 `mCurrentFocus` 前台判定。

## 三、命令与安全契约

- 只用 subprocess 参数数组，`shell=False`；每条命令超时 `5` 秒，不重试。
- 未安装 ADB、超时或非零退出归一为 `device_disconnected`；过程日志分别用固定 `adb_missing/adb_timeout/adb_oserror/adb_nonzero` 错误类别，不扩业务原因码。
- 设备清单只接受 tab 分隔的确定记录；`adb: no devices/emulators found` 等提示行、标题和守护进程行不计入设备数。serial 不在清单也报断连；清单未知状态保守视为 `offline`。
- 显式 serial 只在本机内存精确匹配，输出逻辑别名固定为 `primary`，不输出真实序列号或窗口原文。
- 解锁要求每个 `isKeyguardShowing` 都紧跟 `=` 且值 token 严格为 `true`/`false`（到空白结束）；`false123`、`false-extra`、缺值/缺等号、任意畸形或合法/畸形混杂均为 `null`，不得推定解锁。
- 前台要求 `mCurrentFocus` 唯一，且整个 focus 值（允许首尾空白）恰好是一个完整 `Window{...}` 结构，内部恰好一个 `包名/Activity` token；前后附加文本/组件、同一行或同一 Window 内多个组件、畸形/混合组件、缺失/多值均为 `unknown`。包名 `com.hpbr.bosszhipin` 为 `boss`，其它单一包为 `other`。
- 本模块不得在 fixture 模式或其他离线测试中实际执行；测试只 mock `subprocess.run`。模块导入必须零副作用。

## 四、测试

```powershell
python -m unittest tests.test_doctor
```

覆盖成功调用顺序、`shell=False`、5 秒超时、无重试、断连/超时/非零及其日志类别、提示行计数为 0、serial 未命中、offline/unauthorized/未知状态、畸形 keyguard token、单 Window 多组件与前台解析。没有真实 ADB 调用。

## 五、维护要求

1. 修改命令、解析规则或原因码时，同步更新本文件、`app/ports/AGENTS.md`、`app/usecases/AGENTS.md`、根 `AGENTS.md`、`docs/operator.md` 与测试。
2. 不扩展到 M3+ 导航、点击、截图或聊天读取；新增能力须按里程碑和确认门槛另建。
3. 每个函数/方法写 docstring，说明输入、输出和安全边界。

## 六、已知限制

- 真实 `adb devices` / `dumpsys window` 输出未在本机验证；不同 Android 版本的多显示器或字段差异可能使解锁/前台判定为未知。
- 当前只在测试中 mock subprocess，未在真机执行。
