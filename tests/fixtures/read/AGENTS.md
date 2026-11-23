# tests/fixtures/read 目录指南

## 一、目录用途

存放 M3 `read` 的纯合成输入：页面/职位/未读/会话 XML、M3 profile、manifest 与仅供测试读取的期望结果。所有联系人、公司、职位、正文、时间、角标与证据名均为人工构造，不使用真实设备、账号、聊天或个人事实。

## 二、当前文件

- `profile.json`：M3 合成读取 profile，固定 `profile_id/app/version/calibrated/selectors`；只代表当前离线版式，不是真实 BOSS 版本标定。`conversation_stable_id_verified=true` 仅用于合成稳定消息标识语义验收。
- `page.json`/`page.xml`：聊天列表页面读取正例。
- `job_list.json`/`job_list.xml`：两张完整职位卡读取正例。
- `job_detail.json`/`job_detail.xml`：职位详情正文、摘要、长度与完整性正例。
- `unread.json`/`unread.xml`：唯一行区域角标为 1 的未读读取正例；不覆盖像素后备，像素正反例由测试在临时目录生成。
- `conversation.json`/`conversation-0.xml`/`conversation-1.xml`：两页会话、稳定消息标识、方向证据、连续重叠与最新入站正例。
- `expected.json`：仅供测试读取的独立期望，生产解析器不得引用；哈希/UUID 不写死，由测试重算。
- `AGENTS.md`、`CLAUDE.md`：本目录索引与入口。

## 三、约束

- manifest 引用只能指向本目录内既有文件，不接受绝对路径、`..`、链接/junction 或越界；生产解析器只读 manifest/profile/XML/可选像素，不读 `expected.json`。
- `read` 夹具零设备、零网络、零模型、零业务库、零子进程；只写本次独立过程日志。
- 合成 profile `calibrated=true` 只表示 fixture 可解析；真实分辨率、像素阈值、控件结构、稳定消息标识与版本迁移均未标定。
- 不写入真实姓名、公司、职位、正文、联系方式、设备序列号、绝对路径或截图。像素测试若生成原始 RGBA，只在临时目录使用，不提交真实画面。

## 四、维护要求

1. 新增/改名/删除夹具时同步更新本文件、`tests/fixtures/AGENTS.md`、`tests/AGENTS.md` 与根 `AGENTS.md`。
2. 变更 manifest/profile/XML 字段时同步 M3 契约文档、`docs/operator.md` 与 `tests/test_read.py`；不得为通过实现而放宽独立期望。
3. 不把合成稳定标识、`actionable=true` 或 `inbound_state=known` 写成真实设备已验证或发送授权。
