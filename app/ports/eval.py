"""声明 M4b 评测套件、本机报告、留出登记与无上传合同的文件端口。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


class EvalStorePort(Protocol):
    """抽象套件读取、本机报告/凭证、留出登记事务与无上传合同读取。"""

    def read_suite(self, suite_name: str) -> dict[str, object] | None:
        """读取指定套件；不存在返回 None，格式/路径/链接错误抛固定策略错误。"""
        ...

    def write_report(self, run_id: str, report: dict[str, object]) -> str:
        """把报告原子写到本机 data/evals 独立运行目录并返回项目相对路径。"""
        ...

    def write_acceptance(self, run_id: str, acceptance: dict[str, object]) -> str:
        """仅在全部公开契约通过时写出绑定摘要的本机验收凭证。"""
        ...

    def read_acceptance(self, rel_path: str) -> dict[str, object] | None:
        """只读本机 data/evals 运行目录内的验收凭证；不存在返回 None。"""
        ...

    def read_holdout_registry(self) -> dict[str, object]:
        """读取本机留出登记；不存在时返回空登记对象。"""
        ...

    def update_holdout_registry(
        self, mutator: Callable[[dict[str, object]], object]
    ) -> object:
        """在独占锁内读-改-原子写登记；mutator 失败不覆盖原登记。"""
        ...

    def read_holdout_attestation(self) -> dict[str, object] | None:
        """读取本机无上传合同；不存在返回 None，不读取认证变量或凭据。"""
        ...

    def write_regression_admission(
        self, batch_id: str, suite: dict[str, object]
    ) -> str:
        """把退役留出整批冻结副本写入本机 regression 准入目录，不覆盖。"""
        ...
