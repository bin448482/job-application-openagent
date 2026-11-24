# tests/fixtures/actions 目录指南

## 一、目录用途

存放 M5 动作离线验收的纯合成脚本。脚本只描述合成 IME 与一次动作观察（或固定失败），不是真实设备记录、模型输出或 Evidence。

## 二、当前文件

- `send-text-ok.json`：文本发送 performed 的合成观察示例；正反例测试会复制并在临时项目根重写观察时间。
- `AGENTS.md`/`CLAUDE.md`：目录索引与只含 `@AGENTS.md` 的入口。

## 三、边界

- 不含真实联系人、公司、聊天正文、附件名、坐标、序列号或路径。
- `observation` 必须是固定字段集合；测试不得把 `outcome/effect` 等 Evidence 字段写入脚本。
- 该目录由 `tests/action_helpers.py` 复制到临时项目根；CLI 只经 `ScriptedActionExecutor` 读取。

## 四、维护要求

新增/改名/删除脚本时同步本文件、`tests/fixtures/AGENTS.md`、`tests/AGENTS.md` 与根 `AGENTS.md`。
