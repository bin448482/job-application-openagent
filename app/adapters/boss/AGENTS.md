# app/adapters/boss 目录指南

## 一、目录用途

实现 M2 的 BOSS 合成列表读取与 M3 的页面/职位/未读/会话读取。当前解析标准库 ElementTree 可读的层级 XML、校验 M2/M3 最小 profile/manifest、执行字段阶梯/像素后备/跨屏合并，并输出设计说明 §5.3 的 `ReadResult`；不连接设备、不打开会话、不推进已读基线、不执行动作、不写库。

## 二、当前文件

- `__init__.py`：声明 BOSS 适配器包。
- `list.py`：M2 `validate_profile`、`load_sense_fixture`、`load_and_parse_snapshot`、纯函数 `parse_snapshot` 与 `merge_snapshots`；严格 JSON/路径边界、四源判定、区域内角标、全局标签、身份去重、分页最长整行重叠、覆盖率证明、指纹与候选重算。
- `read.py`：M3 `validate_read_profile`、`load_read_fixture`、`load_and_read_fixture` 与纯函数 `parse_page/parse_job_list/parse_job_detail/parse_unread/parse_conversation_page/merge_conversations`；页面分类、职位卡/详情、未读行阶梯、聊天方向/稳定身份/跨屏合并。
- `pixel.py`：`parse_screen_header`、`validate_calibration` 与纯 `parse_pixel_row_badge`；16 字节小端 RGBA 头、分辨率/格式/区域/颜色/阈值校验，像素结论只输出 `FieldReading`。
- `support.py`：M2/M3 共用的严格 JSON/XML/夹具路径、bounds、resource-id 与比例区域校验；不包含业务判定。

## 三、行为契约

1. manifest 固定 `{profile_path,xml_paths,app_version,observed_at,baseline_rows,coverage_proof}`；profile 固定 M2 最小键。所有键必填、拒绝额外键；路径相对 manifest 目录且必须位于该夹具目录内，拒绝绝对路径、`..`、符号链接/junction 与缺失文件。`badge_region` 元素先按类型与 0–1 范围检查，再对浮点判有限性；巨大整数不得在 `math.isfinite` 前溢出，非法区域一律 `invalid_fixture`。
2. XML 用标准库 ElementTree，只接受唯一 `hierarchy` 根，根内只允许 `node` 元素（嵌套 `hierarchy` 也拒绝）；`DOCTYPE`/`ENTITY`、损坏 XML、`UnicodeError`/非法编码、其它元素均为 `failed/invalid_xml`。夹具 XML 文件 UTF-8 解码失败也映射 `invalid_xml`，manifest/profile/文件 IO 错误才是 `invalid_fixture`。唯一列表缺失为 `failed/wrong_page`，多列表或嵌套行为 `undetermined/incomplete_snapshot`。
3. 行字段只接受行内唯一节点的原样非空 `text`；缺字段或重复节点使对应字段未知。`identity_hash=H([姓名,公司,职位])`，只有全局唯一身份才重建 `app:conv:` 会话键；缺失或重复身份不产生候选。
4. 区域内角标只认完全落在行矩形比例区域内的 `android.widget.TextView` 唯一 ASCII 十进制整数；行字段节点不参与角标识别。任一候选数字节点几何不可读时优先判为 `uncalibrated_region`，不能被同行的合法计数掩盖；超大行坐标的区域换算溢出也回到该未知语义。null 区域/非法 bounds 为 `uncalibrated_region`，多个合法整数节点为 `ambiguous_signal`，无匹配节点为 `missing_signal`。其它独立行的明确正信号仍可保留整体 `present`。
5. 新招呼与底部 tab 只按标签精确文本与直接父容器唯一计数节点解析；正值 present，`0+` 歧义，未验证语义的 0 为 unknown。`not_applicable_sources` 只由已验证版本 profile 声明，全部不适用仍为 unknown。
6. 单页 coverage 恒为 `viewport`；只有完整遍历首尾证明均真或已标定全局零信号可声明 `all`。全局零只提供 coverage，不把未完整遍历的行角标或缺基线预览改为 absent。
7. 分页只合并相邻页连续、整行字段完全一致的最长重叠段；其它重复身份保留 identity_hash 但会话键为 null。合并后按同一基线重算四源信号、指纹与候选，不按页拼接。合并输入的成功页必须共享同一规范 UUID `snapshot_id` 与观察时间；多个 `null` 成功页不是同次采集，返回 `failed/invalid_fixture`。M1 已失败/未决页面的 `snapshot_id=null` 契约不变，合并直接返回该失败原因。
8. 时间距 `now` 不在 0–5 秒（含未来快照）为 `undetermined/stale_snapshot`；`calibrated=false` 或版本不符为 `undetermined/uncalibrated_profile`。所有失败/未决结果 `value=null`，不保存部分候选。
9. 身份摘要复用 `app.domain.models.identity_hash_for`，会话键组装复用 `conversation_key_from_identity`；manifest/profile 严格 JSON 复用 `app.domain.json_input`，适配器不导入 CLI，各调用方保留自己的文件 IO 与 `invalid_fixture` 分类。
10. M3 `read` manifest 固定 `{profile_path,app_version,observed_at,kind,hierarchy_paths,coverage_proof,conversation_context,pixel}`；profile 固定 `{profile_id,app,version,calibrated,selectors}`。`kind` 仅 `page/job_list/job_detail/unread/conversation`，`pixel` 仅 unread，`conversation_context` 仅 conversation；全部键必填、拒绝额外键。
11. M3 页面按 `CORE-W14` 优先级；职位卡只接受 `clickable=true` 且 title/salary/company 唯一非空；职位标题只去首尾 `@%&空格`，公司/薪资只去首尾空白，`job_key="boss:job:"+H([title,company,salary])`；重复身份保留摘要但不可定位；详情截断为 `undetermined/incomplete_snapshot`，正文原文只进内存/CLI 数据区。
12. M3 未读行身份复用 `H([联系人,公司,职位])`；角标层级未知时才用显式合成像素后备，像素只证明 present/absent 不生成精确计数；分辨率/profile/version 不符为 `calibration_mismatch`，损坏/未知格式为失败，低于阈值仅在 `absent_state_verified=true` 时 absent。
13. M3 会话页要求实际标题与输入上下文逐字相等并重算会话键；上下文只是合成发现链假设，不是仓储来源认证。消息 `content_hash` 只比较内容；稳定消息身份只来自 profile 验证过的应用 `content-desc` 原值，`message_key="boss:msg:"+H([conversation_key,stable_identity_hash])`。同正文/同秒/坐标/屏幕序号/布局 resource-id 均不构成身份。
14. 跨屏只裁剪相邻页边界稳定身份与 kind/direction/content_hash/time_hint 一致的连续最长重叠；重复稳定 ID、方向冲突、截断、读未全或未知行使入站 `unknown`；单页需 `scrollable=false`，多页需 `full_traversal` 首尾证明。所有失败/未决 `value=null`。

## 四、依赖与边界

- 只依赖标准库、`app/domain/models.py`、`app/domain/reading.py`、`app/domain/ladder.py` 与 `app/domain/scheduler.py` 的纯判定，不导入 CLI、SQLite、ADB 或设备动作模块。
- 不读取 `expected.json`、根 `config.json`、凭据或个人事实；合成 XML/正文/稳定 ID 只在解析期间使用，事件与日志不保存原文。
- M3 真实设备 profile、真实像素阈值、真实消息稳定标识与导航属于后续受控步骤；当前只实现合成契约与模拟证据。

## 五、测试

```powershell
python -m unittest tests.test_sensor tests.test_sensor_store tests.test_read
```

`tests.test_sensor` 覆盖 M2 正例独立键算法、区域边界与巨大整数、四源正/零/未知冲突、全局标签语义、预览基线、身份重复、分页重叠、覆盖率、过期/未来、未标定、非法 XML/Unicode/嵌套 hierarchy、成功页 null snapshot、manifest 越界与停止原因；`tests.test_sensor_store` 覆盖 CLI 落库、重放、终态、仓储错误、三项输入边界零落库与零设备/网络/进程断言。`tests.test_read` 覆盖 M3 页面/职位/未读/会话、稳定消息键、字段阶梯、像素阈值/分辨率/损坏、跨屏冲突/新增消息、门禁与 CLI。

## 六、维护要求

1. 修改 profile 字段、匹配规则、聚合判据、合并算法、稳定身份或原因码时，同步更新本文件、`app/domain/AGENTS.md`、`tests/fixtures/sensor/AGENTS.md`、`tests/fixtures/read/AGENTS.md`、`docs/operator.md`、M3 契约文档、根 `AGENTS.md` 与测试。
2. 新增真实版本 profile 前先取得独立标定证据；不把合成 profile 标记为已验证真实版本。
3. 每个函数/方法写 docstring；不引入第三方依赖，不预建 M4+ 页面/动作模块。

## 七、已知限制

- 只验证合成 XML、单个 M2 profile、单个 M3 合成 profile 与合成 RGBA；真实 BOSS 版本的新招呼语义、无 id 角标区域、稳定消息标识、像素阈值与分辨率差异仍未标定。
- M3 只提供读取/门禁与跨屏身份，不提供真实导航、会话进入、滚动、视觉模型或正文持久化。
- 分页夹具只覆盖合成连续页，未验证真实滚动时序、动画或设备重连；真实 ADB 未执行。
