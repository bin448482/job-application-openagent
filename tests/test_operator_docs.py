"""验证操作手册存在、M6 入口可读且未把未实现能力写成可用。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.usecases.operator_docs import operator_docs_report

ROOT = Path(__file__).resolve().parents[1]


class OperatorDocsTests(unittest.TestCase):
    """文档证据只证明手册与报告控制可读，不证明新用户全流程。"""

    def test_operator_manual_exists_and_marks_m6_limits(self) -> None:
        """手册存在、列出 run prepare/execute，并保留真实设备/模型未测声明。"""
        path = ROOT / "docs" / "operator.md"
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("run prepare", text)
        self.assertIn("run execute", text)
        self.assertIn("apply_quota_contract_missing", text)
        self.assertIn("尚未实现", text)
        self.assertIn("真实设备", text)
        report = operator_docs_report(ROOT)
        self.assertEqual(report["status"], "documented")

    def test_missing_manual_report_control_is_blocked(self) -> None:
        """隔离副本缺失手册时由 app 报告控制明确 BLOCKED。"""
        with tempfile.TemporaryDirectory() as temp:
            report = operator_docs_report(Path(temp))
            self.assertEqual(report["status"], "BLOCKED")
            self.assertEqual(report["reason_code"], "manual_missing")

    def test_incomplete_manual_report_control_is_blocked(self) -> None:
        """手册缺少必需入口说明时报告控制同样 BLOCKED。"""
        with tempfile.TemporaryDirectory() as temp:
            manual = Path(temp) / "docs" / "operator.md"
            manual.parent.mkdir(parents=True)
            manual.write_text("# 操作手册\n", encoding="utf-8")
            report = operator_docs_report(Path(temp))
            self.assertEqual(report["status"], "BLOCKED")
            self.assertEqual(report["reason_code"], "manual_incomplete")


if __name__ == "__main__":
    unittest.main()
