"""生成操作手册隔离报告控制：手册缺失时明确 BLOCKED，不把文件存在当全流程验收。"""

from __future__ import annotations

from pathlib import Path

REPORT_SCHEMA_VERSION = 1


def operator_docs_report(root: Path) -> dict[str, object]:
    """只读检查操作手册并返回稳定报告；缺失即 BLOCKED，存在仅表示文档证据。"""
    manual = Path(root) / "docs" / "operator.md"
    if not manual.is_file():
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "status": "BLOCKED",
            "manual": "docs/operator.md",
            "reason_code": "manual_missing",
        }
    text = manual.read_text(encoding="utf-8")
    required = ("run prepare", "run execute", "apply_quota_contract_missing")
    missing = [token for token in required if token not in text]
    if missing:
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "status": "BLOCKED",
            "manual": "docs/operator.md",
            "reason_code": "manual_incomplete",
        }
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "documented",
        "manual": "docs/operator.md",
        "reason_code": None,
    }
