"""声明 M4b 评测套件、本机报告与留出登记的最小文件端口。"""

from __future__ import annotations

from typing import Protocol


class EvalStorePort(Protocol):
    """抽象公开/回归/留出套件读取、本机报告写入与留出登记读写。"""

    def read_suite(self, suite_name: str) -> dict[str, object] | None:
        """读取指定套件；不存在返回 None，格式/路径/链接错误抛固定策略错误。"""
        ...

    def write_report(self, run_id: str, report: dict[str, object]) -> str:
        """把报告写到本机 data/evals 独立运行目录并返回项目相对路径。"""
        ...

    def write_acceptance(self, run_id: str, acceptance: dict[str, object]) -> str:
        """仅在全部公开契约通过时写出绑定摘要的本机验收凭证。"""
        ...

    def read_holdout_registry(self) -> dict[str, object]:
        """读取本机留出登记；不存在时返回空登记对象。"""
        ...

    def write_holdout_registry(self, registry: dict[str, object]) -> str:
        """原子写入本机留出登记并返回项目相对路径。"""
        ...
