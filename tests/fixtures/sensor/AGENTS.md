# tests/fixtures/sensor 目录指南

## 一、目录用途

存放 M2 合成传感器夹具：最小 profile、单页列表 XML、manifest 与独立期望结果。所有姓名、公司、职位、预览、时间和设备字段均为人工合成值，不对应真实账号、聊天或个人事实。

## 二、当前文件

- `positive.json`：验收 manifest。`xml_paths` 仅 `positive.xml`，`coverage_proof.kind=viewport`，`baseline_rows=[]`，`app_version` 与 profile 一致，`observed_at` 为固定毫秒 UTC 时间。
- `profile.json`：M2 最小合成 profile。`app=boss`、`calibrated=true`；列表/行后缀、五个行字段、行内比例角标区域、新招呼与消息 tab 标签均固定；本夹具没有 `not_applicable_sources`。
- `positive.xml`：一行合成会话，身份字段、预览、时间与区域内角标 `1` 均存在；新招呼与 tab 标签不存在，因此对应源保持 unknown。
- `expected.json`：仅测试读取的按 manifest 文件名索引的期望，`positive.json` 固定为退出 `0`、`ok/present`、`stop_reason=null`、唯一 `unread`、发现数 `1`。哈希和 UUID 不写死。

## 三、期望与边界

`python -m app sense --fixture tests/fixtures/sensor/positive.json --db data/runtime/m2-acceptance.sqlite3` 必须退出 `0`，输出唯一 `unread` 候选且 `stop_reason=null`；重放后发现仍为一行且不追加事件。夹具只触发合成解析与本机 SQLite，不连接设备、网络、模型或子进程。

`not_applicable_sources` 只有在同目录指南或独立标定记录给出已验证依据时才能加入；它只对声明的合成版本有效，不能掩盖未标定字段。当前四个源均未声明不适用。

## 四、维护要求

1. 修改 profile/manifest/XML 时同步更新 `expected.json`、`tests/test_sensor.py`、`tests/test_sensor_store.py`、`app/adapters/boss/AGENTS.md`、本文件与根 `AGENTS.md`。
2. `expected.json` 只由测试读取；解析器不得读取或引用它，也不得从被测输出反向生成期望。
3. 其它正反例由测试在临时目录生成；固定文件只保留验收正例，避免把大量错误输入公开固化。
4. 不写真实姓名、公司、职位、聊天、设备序列号、绝对路径或本机用户名。

## 五、已知限制

- profile 是合成开发契约，不代表 BOSS 14.200 或任何真实版本已验证。
- 未覆盖真实滚动分页、像素角标、输入法/版式变化与设备断连。
- 正例只证明合成解析、入库、聚合和重放判据，不证明真实设备读取或动作授权。
