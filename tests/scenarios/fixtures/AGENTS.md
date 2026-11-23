# fixtures 目录指南

## 一、目录用途

保存纯合成输入，供未来真实 app 在隔离根中读取。所有姓名、公司、职位、预览、城市与时间均为合成值，不含真实 HR 内容、简历、联系方式、设备序列号或本机路径。

## 二、子目录索引

- `m0/doctor/`：M0 doctor 合成设备清单、坏 JSON、重复 alias、不存在选择、额外键与目录读取失败输入。
- `m0/config/`：M0 最小配置的省略/空对象/非法类型/额外键输入；缺文件场景由清单声明而不预建文件。
- `m2/sensor/`：M2 合成 profile、消息列表 XML、manifest 与 app 侧 expected；每个案例只覆盖一个独立判据，含正角标、区域外数字、歧义/非法 bounds、全局计数、9+/0+、新招呼语义、全覆盖否定、预览变化、多页重叠/损坏、过期/未来快照、未标定 profile、错误页面、多列表、坏 XML、路径越界、身份歧义与幂等重放。
- `m1/`：M1 纯合成探针请求：初始化/损坏注入、单会话种子、claim/start/renew/finish/recover、并发 barrier/hold_lock 与只读 inspect；不含真实数据或任意 SQL。
- `m3/`：进入会话消耗角标、授权矩阵、版本漂移与跨级冲突的声明式输入。
- `m4/`：Decision envelope 与突变、合成策略包及负例、桥注入、摘要关系、共享门禁、快照恢复输入。
- `m5/actions/`：六个不可逆动作 × F-A 至 F-E 故障窗口的声明式矩阵。
- `m6/`：S01/S03/S06/S08 的 `driver_stimuli_only` 外部刺激、安全点条件向量；产品观察与暂停结果只在 `expected/m6-observations.json`，不进入刺激。
- `m7/cleanup/`：清理候选、保护项、身份歧义与删除核验输入。
- `m8/`：分桶原始计数、零分母与降级回滚事件。
- `AGENTS.md`：本文件。
- `CLAUDE.md`：只包含 `@AGENTS.md`。

## 三、输入与答案边界

- `m2/sensor/*/expected.json` 是 M2 计划约定的 app 侧预期，供比较器使用；manifest 与 profile 不引用它，传感器不得读取。
- 独立预期另存 `../expected/oracles.json`；夹具中不得出现 `expected_ref` 或 `oracles.json` 路径。
- 路径必须在夹具目录内；`framework` 会在任何读取前拒绝 `..`、绝对路径、symlink/junction 与越界解析。
- 验收隔离只复制命令与 manifest 选定的合成输入，`expected.json`/独立 oracle 不进入 SUT 输入树。

## 四、维护要求

- 修改夹具必须同步对应 variant、独立预期和 `tools/build_assets.py`；生成脚本会重建整个 fixtures 树，避免残留旧文件。
- 新增案例先确定独立判据，再写最小合成输入；不要复制同一判据堆量，不读取旧项目原始 XML 或私有回归产物。
- 不写入真实业务数据；发现疑似敏感信息按根指南告知位置与类别，不复述原值。
