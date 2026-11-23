# app/adapters/boss 目录指南

## 一、目录用途

实现 M2 的 BOSS 合成列表读取适配器。当前只解析标准库 ElementTree 可读的层级 XML、校验 M2 最小 profile/manifest，并输出设计说明 §5.3 的 `ReadResult[UnreadSnapshot]`；不连接设备、不打开会话、不推进已读基线、不执行动作。

## 二、当前文件

- `__init__.py`：声明 BOSS 适配器包。
- `list.py`：`validate_profile`、`load_sense_fixture`、`load_and_parse_snapshot`、纯函数 `parse_snapshot` 与 `merge_snapshots`；严格 JSON/路径边界、四源判定、区域内角标、全局标签、身份去重、分页最长整行重叠、覆盖率证明、指纹与候选重算。

## 三、行为契约

1. manifest 固定 `{profile_path,xml_paths,app_version,observed_at,baseline_rows,coverage_proof}`；profile 固定 M2 最小键。所有键必填、拒绝额外键；路径相对 manifest 目录且必须位于该夹具目录内，拒绝绝对路径、`..`、符号链接/junction 与缺失文件。
2. XML 用标准库 ElementTree，只接受唯一 `hierarchy` 根，根内只允许 `node` 元素（嵌套 `hierarchy` 也拒绝）；`DOCTYPE`/`ENTITY`、损坏 XML、其它元素均为 `failed/invalid_xml`。唯一列表缺失为 `failed/wrong_page`，多列表或嵌套行为 `undetermined/incomplete_snapshot`。
3. 行字段只接受行内唯一节点的原样非空 `text`；缺字段或重复节点使对应字段未知。`identity_hash=H([姓名,公司,职位])`，只有全局唯一身份才重建 `app:conv:` 会话键；缺失或重复身份不产生候选。
4. 区域内角标只认完全落在行矩形比例区域内的 `android.widget.TextView` 唯一 ASCII 十进制整数；行字段节点不参与角标识别。null 区域/非法 bounds 为 `uncalibrated_region`，多个整数节点为 `ambiguous_signal`，无匹配节点为 `missing_signal`。
5. 新招呼与底部 tab 只按标签精确文本与直接父容器唯一计数节点解析；正值 present，`0+` 歧义，未验证语义的 0 为 unknown。`not_applicable_sources` 只由已验证版本 profile 声明，全部不适用仍为 unknown。
6. 单页 coverage 恒为 `viewport`；只有完整遍历首尾证明均真或已标定全局零信号可声明 `all`。全局零只提供 coverage，不把未完整遍历的行角标或缺基线预览改为 absent。
7. 分页只合并相邻页连续、整行字段完全一致的最长重叠段；其它重复身份保留 identity_hash 但会话键为 null。合并后按同一基线重算四源信号、指纹与候选，不按页拼接。
8. 时间距 `now` 不在 0–5 秒（含未来快照）为 `undetermined/stale_snapshot`；`calibrated=false` 或版本不符为 `undetermined/uncalibrated_profile`。所有失败/未决结果 `value=null`，不保存部分候选。

## 四、依赖与边界

- 只依赖标准库、`app/domain/models.py` 与 `app/domain/scheduler.py` 的纯聚合，不导入 CLI、SQLite、ADB 或设备动作模块。
- 不读取 `expected.json`、根 `config.json`、凭据或个人事实；合成 XML 正文只在解析期间使用，事件与日志不保存原文。
- 真实设备 profile、像素/几何判读与定向核对属于 M3，当前不实现。

## 五、测试

```powershell
python -m unittest tests.test_sensor tests.test_sensor_store
```

`tests.test_sensor` 覆盖正例独立键算法、区域边界、四源正/零/未知冲突、全局标签语义、预览基线、身份重复、分页重叠、覆盖率、过期/未来、未标定、非法 XML、manifest 越界与停止原因；`tests.test_sensor_store` 覆盖 CLI 落库、重放、终态、仓储错误与零设备/网络/进程断言。

## 六、维护要求

1. 修改 profile 字段、匹配规则、聚合判据、合并算法或原因码时，同步更新本文件、`app/domain/AGENTS.md`、`tests/fixtures/sensor/AGENTS.md`、`docs/operator.md`、根 `AGENTS.md` 与测试。
2. 新增真实版本 profile 前先取得独立标定证据；不把合成 profile 标记为已验证真实版本。
3. 每个函数/方法写 docstring；不引入第三方依赖，不预建 M3+ 页面/动作模块。

## 七、已知限制

- 只验证合成 XML 与单个最小 profile；真实 BOSS 版本的新招呼语义、无 id 角标区域与分辨率差异仍未标定。
- 当前不提供像素、视觉或人工读取阶梯；M2 的定向核对出口只报告停止原因。
- 分页夹具只覆盖合成连续页，未验证真实滚动时序、动画或设备重连。
