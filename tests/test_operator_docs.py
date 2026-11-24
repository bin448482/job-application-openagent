"""验证操作手册存在、M6 入口可读且未把未实现能力写成可用。"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class OperatorDocsTests(unittest.TestCase):
    """文档证据只证明手册与帮助可读，不证明新用户全流程。"""

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

    def test_missing_manual_in_isolated_copy_is_not_present(self) -> None:
        """隔离副本缺失手册时状态为不可用，不能把 file 存在性当作全流程验收。"""
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            self.assertFalse((Path(temp) / "docs" / "operator.md").exists())


if __name__ == "__main__":
    unittest.main()
