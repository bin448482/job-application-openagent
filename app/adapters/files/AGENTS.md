# app/adapters/files 目录指南

## 一、目录用途

实现 M4a 策略包与 M4b 评测文件的本机文件通道：读取公开 base/示例、固定 preview 案例、活动包四载荷与可选双指南、公开/私有套件，按原始字节暂存/复核/同卷发布 history 快照，写出被忽略的 `data/evals` 报告/凭证与留出登记，受控清理本次自有路径。该适配器不连接设备、数据库、网络或模型，也不决定策略档位或授权。

## 二、当前文件

- `__init__.py`：声明文件适配器包。
- `support.py`：共用 `is_link_like`（symlink/Windows reparse/junction）、`normalize_project_relative`、`resolve_project_relative` 与 `read_bounded_bytes(max+1)`，供 policy/eval 文件适配器复用，避免第二套路径安全算法。
- `policy.py`：`FilePolicyRepository(root)`。路径只接受项目内相对 POSIX/反斜杠形式，拒绝绝对路径、盘符、`~`、`..`、链接/junction 与越界；活动包只允许 `prompts/local/<profile>` 直接子目录，快照只允许 `prompts/local/history/<snapshot-id>`，preview 只允许 `tests/fixtures/policy-preview/<case>.json`。决策边界的 `DecisionSnapshotPort` 统一使用 `app/usecases/policy_eval.py` 的 `RepositorySnapshot`；本适配器不另建快照包装。
- `evals.py`：`FileEvalStore(root)`。公开套件固定 `evals/policy-suite/public.json`，私有回归/留出固定 `evals/private/{regression,holdout}/suite.json`；报告/凭证写 `data/evals/eval-<run-id>/`，留出登记写 `data/evals/holdout-registry.local.json`，无上传合同写 `data/evals/holdout-attestation.local.json`，退役准入写 `data/evals/regression-admissions/<batch-id>/suite.json`。`read_acceptance` 只读 `data/evals/eval-<32hex>/acceptance.json` 固定形状，拒绝其它位置。只有 regression 读取/合并准入，且仅纳入 registry 已确认 retired 且 regression_ref 匹配的副本；public/holdout 不因无关坏准入失败，registry 未确认的准入不暴露。读取有界，报告/凭证同卷临时完整写入后原子链接、目标存在不覆盖，登记在独占锁内读-改-原子替换，链接/junction 拒绝；退役登记失败时保留未确认准入本机残留，由 registry 过滤使其不可运行，不删除其它 owner 文件。
- `AGENTS.md`、`CLAUDE.md`：本目录索引与入口。

## 三、行为契约

1. 所有载荷和 base 以 `read_bytes().decode("utf-8", strict)` 读取，保留 LF/CRLF 原字节，不做换行转换；拒绝 BOM、NUL、非法 UTF-8、链接与非普通文件。
2. `pack.toml` ≤16 KiB，每份 md ≤32 KiB，四载荷合计 ≤64 KiB；base 与 preview 案例有独立大小上限。多余文件、子目录、缺失载荷或只有一份指南均拒绝。
3. 暂存目录固定在 `prompts/local` 或 `prompts/local/history` 下，名称以 `.staging-` 开头，活动包发现器与 history 列表不接纳；`write_text_exact` 只写暂存路径且不覆盖。
4. `promote` 只做同卷改名，目标存在即 `policy_exists`；不提供覆盖、复制到任意目录或跨卷回退。
5. `remove_owned` 只删除本次适配器实例登记过的暂存/发布路径，防止测试或调用方误删用户数据。
6. preview 案例从项目根固定读取 `tests/fixtures/policy-preview/<case>.json`；未知或越界 ID 返回 `unknown_case`，不搜索其它目录、不读取答案或私有留出。
7. `list_pack_dirs`/`list_history_dirs` 只枚举直接项元数据；链接/junction 候选保留路径交给上层标 `valid=false`，不跟随目标、不递归。活动包/快照/指南读取共用领域路径形状与同一正文读取核心。
8. 评测适配器不把套件/私有样本正文写入报告；报告只含摘要、固定诊断与计数。`data/evals` 与 `evals/private` 均为本机/忽略位置；适配器不读取认证变量、端点、旧评测答案或真实聊天。留出登记更新必须持独占锁覆盖读-改-写，锁文件存在不表示进程存活或允许自动恢复；报告/凭证失败清理本次半产物。

## 四、测试

`tests/test_policy_store.py` 覆盖路径越界、history/暂存拒绝、额外/缺失文件、单指南、BOM/非法 UTF-8/NUL、三类大小、CRLF 原字节、链接（mock）、preview 未知 ID、list 跳过 history/dot，以及 init/snapshot/restore 的暂存清理与不覆盖；`tests/test_policy_eval.py` 覆盖有界套件读取、报告原子发布/失败清理、真实 Windows junction 拒绝且目标不变、套件/留出登记/准入与无上传合同；`tests/test_m4c_closed_loop.py` 覆盖凭证读取/摘要变化拒绝与真实文件快照重核。

## 五、维护要求

1. 修改路径边界、大小、编码或发布语义时同步本文件、`app/adapters/AGENTS.md`、根 `AGENTS.md`、`docs/operator.md` 与测试。
2. 不新增远程下载、include、环境变量插值、可执行模板或任意目录扫描。
3. 每个函数写 docstring；错误只带固定原因码/诊断码，不回显原值、绝对路径或底层异常。
