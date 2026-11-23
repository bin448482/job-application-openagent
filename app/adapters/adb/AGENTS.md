# app/adapters/adb 目录指南

## 一、目录用途

实现 M0 显式授权的真实 ADB 只读探针，仅顺序执行 `adb devices` 与 `adb -s <serial> shell dumpsys window`。

## 二、当前文件

- `__init__.py`：声明 ADB 适配器包。
- `device.py`：`AdbDeviceProbe`、`DeviceProbeError`、设备清单解析、`isKeyguardShowing` 判定与 `mCurrentFocus` 前台判定。

## 三、命令与安全契约

- 只用 subprocess 参数数组，`shell=False`；每条命令超时 `5` 秒，不重试。
- 未安装 ADB、超时或非零退出归一为 `device_disconnected`；serial 不在清单也报断连；清单未知状态保守视为 `offline`。
- 显式 serial 只在本机内存精确匹配，输出逻辑别名固定为 `primary`，不输出真实序列号或窗口原文。
- 解锁仅在全部 `isKeyguardShowing` 为 `false` 且至少一次时为 `true`；全部 `true` 为 `false`；缺失、混合或非法值为 `null`。
- 前台仅在 `mCurrentFocus` 唯一且组件可解析时判定：包名 `com.hpbr.bosszhipin` 为 `boss`，其它包为 `other`，缺失/多值/不可解析为 `unknown`。
- 本模块不得在 fixture 模式或其他离线测试中实际执行；测试只 mock `subprocess.run`。

## 四、测试

```powershell
python -m unittest tests.test_doctor
```

覆盖成功调用顺序、`shell=False`、5 秒超时、无重试、断连/超时/非零、serial 未命中、offline/unauthorized/未知状态、锁屏与前台解析。没有真实 ADB 调用。

## 五、维护要求

1. 修改命令、解析规则或原因码时，同步更新本文件、`app/ports/AGENTS.md`、`app/usecases/AGENTS.md`、根 `AGENTS.md`、`docs/operator.md` 与测试。
2. 不扩展到 M3+ 导航、点击、截图或聊天读取；新增能力须按里程碑和确认门槛另建。
3. 每个函数/方法写 docstring，说明输入、输出和安全边界。

## 六、已知限制

- 真实 `adb devices` / `dumpsys window` 输出未在本机验证；不同 Android 版本的多显示器或字段差异可能使解锁/前台判定为未知。
- 当前只在测试中 mock subprocess，未在真机执行。
