"""声明 M4a 策略包文件工作流、恢复活动状态检查与固定错误的最小端口。"""

from __future__ import annotations

from typing import Protocol

from app.domain.policy import PolicyIssue

POLICY_REASON_CODES = frozenset(
    {
        "invalid_input",
        "invalid_policy",
        "policy_exists",
        "policy_io_error",
        "policy_changed",
        "unknown_case",
        "invalid_provider",
        "eval_failed",
        "authorization_required",
    }
)


class PolicyError(Exception):
    """携带固定策略原因码与可定位诊断；不携带原值、路径或底层异常文本。"""

    def __init__(
        self,
        reason_code: str,
        issues: list[PolicyIssue] | None = None,
        *,
        activity_state: str | None = None,
    ) -> None:
        """保存白名单原因码、诊断列表与可选活动状态，异常消息只使用原因码。"""
        if reason_code not in POLICY_REASON_CODES:
            raise ValueError("policy reason is invalid")
        if activity_state not in (None, "active", "unknown"):
            raise ValueError("policy activity state is invalid")
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.issues = list(issues or [])
        self.activity_state = activity_state


class PolicyRepositoryPort(Protocol):
    """抽象项目内策略文件读取、暂存、原子发布与受控清理。"""

    def read_base(self) -> dict[str, str]:
        """读取两份公开 base 原文，按文件名返回。"""
        ...

    def read_example_pack(self) -> dict[str, str]:
        """读取公开示例四载荷文件，不读取本机活动包。"""
        ...

    def read_pack(self, pack_rel: str) -> dict[str, str]:
        """读取一个活动包的四载荷原文，并校验目录归属与普通文件。"""
        ...

    def read_snapshot_pack(self, snapshot_rel: str) -> dict[str, str]:
        """读取 history 直接快照的四载荷原文，用于恢复前复核。"""
        ...

    def read_snapshot_guides(self, snapshot_rel: str) -> dict[str, str]:
        """读取快照内可选双指南；都没有时返回空对象。"""
        ...

    def read_guides(self, pack_rel: str) -> dict[str, str]:
        """读取包内可选双指南；都没有时返回空对象。"""
        ...

    def read_preview_case(self, case_id: str) -> dict[str, object]:
        """读取固定目录中的独立合成 preview 案例，不读取答案。"""
        ...

    def list_pack_dirs(self) -> list[str]:
        """列出 local 直接活动包目录的相对路径。"""
        ...

    def list_history_dirs(self) -> list[str]:
        """列出 history 直接快照目录的相对路径。"""
        ...

    def list_files(self, rel_path: str) -> list[str]:
        """列出目录的直接文件/子目录名，用于暂存与快照完整性检查。"""
        ...

    def read_manifest(self, manifest_rel: str) -> dict[str, object]:
        """读取并解析相邻快照清单，重复 JSON 键拒绝。"""
        ...

    def path_exists(self, rel_path: str) -> bool:
        """判断项目内相对路径是否存在。"""
        ...

    def make_staging_dir(self, parent_rel: str) -> str:
        """在 local 或 history 下创建不被活动包发现器接纳的暂存目录。"""
        ...

    def write_text_exact(self, rel_path: str, text: str) -> None:
        """向暂存路径按 UTF-8 原字节写入且不覆盖已有文件。"""
        ...

    def read_text_exact(self, rel_path: str) -> str:
        """按 UTF-8 原字节读取文件，不做换行转换。"""
        ...

    def promote(self, staging_rel: str, dest_rel: str) -> None:
        """同卷改名发布；目标已存在时拒绝，不覆盖。"""
        ...

    def remove_owned(self, rel_path: str) -> None:
        """只清理本次操作创建的暂存或目标路径。"""
        ...


class PolicyActivityPort(Protocol):
    """声明 restore 前对活动运行、停止与未决租约的最小检查能力。"""

    def assess_restore(self, *, profile_id: str, dest_pack: str) -> str:
        """返回 stopped/active/unknown；只有 stopped 才允许恢复。"""
        ...
