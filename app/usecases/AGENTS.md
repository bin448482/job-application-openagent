# app/usecases 目录指南

## 一、目录用途

组织单次业务用例的输入校验与领域判定，不直接访问属性、文件、数据库或网络。当前只有 M0 `doctor`。

## 二、当前文件

- `__init__.py`：声明用例包。
- `doctor.py`：严格校验合成探针输入，按“清单 → 选择 → 状态 → 解锁 → 前台”的顺序生成固定字段的 doctor 业务结果。

## 三、行为契约

- 输入必须是恰好包含 `devices/selected_alias/unlocked/foreground_app` 的对象；设备项恰好包含 `alias/state`。
- `alias` 唯一且非空；`state` 仅 `device/offline/unauthorized`；`selected_alias` 为已有别名或 `null`；`unlocked` 为布尔或 `null`；`foreground_app` 仅 `boss/other/unknown`。
- 零设备报 `device_disconnected`；多设备未选择报 `device_ambiguous`；单设备未选择时自动选择。
- 选择后依次判定离线/未授权、锁定或解锁未知、前台不是 BOSS 或未知。
- 失败保留已知字段，未知字段保持 `null`/`unknown`；不伪造业务成功。
- 本模块不导入具体适配器，不启动子进程。

## 四、测试

```powershell
python -m unittest tests.test_doctor
```

## 五、维护要求

1. 修改选择顺序、原因码或输出字段时，同步更新根 `AGENTS.md`、`app/cli/AGENTS.md`、`docs/operator.md` 与测试。
2. 新增用例文件时必须有对应里程碑和测试，不预建空模块。
3. 每个函数写 docstring，说明输入、输出与业务含义。

## 六、已知限制

- 当前只有 `doctor`；`sense.py`、`round.py`、`message.py`、`job.py` 等尚未创建。
- 真实设备模式只复用本用例的判定，不在用例内执行 ADB。
