# tests/fixtures/bridge 目录指南

## 目录用途

保存 M4b 桥测试专用的合成本地进程桩，用于验证参数数组启动、stdin/stdout 协议、固定角色分区与失败诊断；不联网、不调用真实模型、不读取个人文件或凭据。

## 当前文件

- `bridge_stub.py`：读取一条规范 JSON 请求，按模式返回 A/B 成功响应、固定 error.code、坏 JSON、重复键、角色声明不符或坏回显；可把收到的请求追加到测试临时捕获文件，供独立检查模型只收到当前案例输入。
- `AGENTS.md`：本目录职责与边界。
- `CLAUDE.md`：仅含 `@AGENTS.md`。

## 边界

- 仅由 `tests.test_bridge` 与 `tests.test_policy_eval` 以本机 Python 参数数组启动；所有输出与决策均为合成数据。
- 不得把该桩当作真实 provider 或据此生成公开验收结论；测试中真实适配器路径的评测报告强制 `simulated=true`。
- 不写仓库路径外的文件；捕获文件位于测试临时目录。

## 验证方式

```powershell
python -m unittest tests.test_bridge tests.test_policy_eval
```
