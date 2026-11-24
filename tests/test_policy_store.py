"""验证 M4a 策略文件边界、六命令文件事务、CLI 外层、日志与恢复受控拒绝。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.adapters.files.policy import FilePolicyRepository
from app.domain.policy import PACK_FILE_NAMES, PolicyIssue, is_snapshot_id
from app.ports.policy import PolicyError
from app.process_log import EVENT_KEYS, ProcessLog
from app.usecases import policy as policy_usecases
from tests.policy_helpers import (
    NOW,
    read_pack,
    real_example_files,
    run_cli,
    temp_project,
    write_pack,
)


class FileAdapterTests(unittest.TestCase):
    """覆盖路径、普通文件、链接、BOM、大小与原始换行读取。"""

    def setUp(self) -> None:
        """创建带公开 base/示例/preview 的临时项目根与文件适配器。"""
        self._tmp, self.root = temp_project()
        self.repo = FilePolicyRepository(self.root)

    def tearDown(self) -> None:
        """清理本测试创建的临时项目根。"""
        self._tmp.cleanup()

    def test_base_and_example_are_read_exactly(self) -> None:
        """验证 base 与示例四载荷按原字节读取。"""
        base = self.repo.read_base()
        self.assertEqual(set(base), {"intent-and-reply.md", "job-evaluate.md"})
        example = self.repo.read_example_pack()
        self.assertEqual(set(example), set(PACK_FILE_NAMES))
        self.assertEqual(example["pack.toml"], (self.root / "prompts/examples/default/pack.toml").read_bytes().decode("utf-8"))

    def test_pack_paths_are_restricted_to_local_direct(self) -> None:
        """验证活动包只接受 local 直接 profile 子目录并拒绝越界/链接式路径。"""
        valid = self.repo.read_example_pack()
        write_pack(self.root, "prompts/local/demo", valid)
        self.assertEqual(set(self.repo.read_pack("prompts/local/demo")), set(PACK_FILE_NAMES))
        for value in (
            "prompts/local/history",
            "prompts/local/.hidden",
            "prompts/local/1bad",
            "prompts/local/demo/nested",
            "prompts/local/demo/../demo",
            "C:/tmp/demo",
            "/tmp/demo",
            "prompts/local/注册",
        ):
            with self.assertRaises(PolicyError) as raised:
                self.repo.read_pack(value)
            self.assertIn(raised.exception.reason_code, {"invalid_input", "invalid_policy"})

    def test_unexpected_entries_and_missing_payload(self) -> None:
        """验证额外文件与缺失载荷都被拒绝。"""
        files = real_example_files()
        write_pack(self.root, "prompts/local/extra", files)
        (self.root / "prompts/local/extra/note.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(PolicyError) as raised:
            self.repo.read_pack("prompts/local/extra")
        self.assertEqual(raised.exception.reason_code, "invalid_policy")
        write_pack(self.root, "prompts/local/missing", files)
        (self.root / "prompts/local/missing/tone.md").unlink()
        with self.assertRaises(PolicyError) as raised:
            self.repo.read_pack("prompts/local/missing")
        self.assertEqual(raised.exception.reason_code, "invalid_policy")

    def test_single_guide_is_rejected(self) -> None:
        """验证只有一份指南时拒绝可选双指南。"""
        files = real_example_files()
        write_pack(self.root, "prompts/local/guide", files)
        (self.root / "prompts/local/guide/AGENTS.md").write_text("x", encoding="utf-8")
        with self.assertRaises(PolicyError) as raised:
            self.repo.read_guides("prompts/local/guide")
        self.assertEqual(raised.exception.reason_code, "invalid_policy")

    def test_bom_invalid_utf8_and_nul_are_rejected(self) -> None:
        """验证 BOM、非法 UTF-8 与 NUL 都被拒绝。"""
        files = real_example_files()
        write_pack(self.root, "prompts/local/bom", files)
        target = self.root / "prompts/local/bom/tone.md"
        target.write_bytes(b"\xef\xbb\xbf" + b"text")
        with self.assertRaises(PolicyError) as raised:
            self.repo.read_pack("prompts/local/bom")
        self.assertEqual(raised.exception.reason_code, "invalid_policy")
        target.write_bytes(b"\xff\xfe")
        with self.assertRaises(PolicyError):
            self.repo.read_pack("prompts/local/bom")
        target.write_bytes(b"a\x00b")
        with self.assertRaises(PolicyError) as raised:
            self.repo.read_pack("prompts/local/bom")
        self.assertEqual(raised.exception.reason_code, "invalid_policy")

    def test_size_limits(self) -> None:
        """验证 pack.toml、单份 md 与四文件总大小上限。"""
        files = real_example_files()
        write_pack(self.root, "prompts/local/big", files)
        (self.root / "prompts/local/big/pack.toml").write_text(
            "#" * (16 * 1024 + 1), encoding="utf-8"
        )
        with self.assertRaises(PolicyError) as raised:
            self.repo.read_pack("prompts/local/big")
        self.assertEqual(raised.exception.reason_code, "invalid_policy")
        write_pack(self.root, "prompts/local/big", files)
        (self.root / "prompts/local/big/tone.md").write_text(
            "a" * (32 * 1024 + 1), encoding="utf-8"
        )
        with self.assertRaises(PolicyError):
            self.repo.read_pack("prompts/local/big")
        write_pack(self.root, "prompts/local/big", files)
        for name in ("tone.md", "phrase-bank.md", "never-say.md"):
            (self.root / "prompts/local/big" / name).write_text(
                "a" * 30000, encoding="utf-8"
            )
        with self.assertRaises(PolicyError) as raised:
            self.repo.read_pack("prompts/local/big")
        self.assertEqual(raised.exception.reason_code, "invalid_policy")

    def test_crlf_is_preserved_not_normalized(self) -> None:
        """验证读取保留 CRLF 原字节而不做换行转换。"""
        files = real_example_files()
        write_pack(self.root, "prompts/local/crlf", files)
        target = self.root / "prompts/local/crlf/tone.md"
        target.write_bytes("第一行\r\n第二行\r\n".encode("utf-8"))
        read = self.repo.read_pack("prompts/local/crlf")
        self.assertEqual(read["tone.md"], "第一行\r\n第二行\r\n")

    def test_link_component_is_rejected(self) -> None:
        """验证链接/reparse 组件在路径链中被拒绝。"""
        files = real_example_files()
        write_pack(self.root, "prompts/local/link", files)
        with patch("app.adapters.files.policy._is_link_like", return_value=True):
            with self.assertRaises(PolicyError) as raised:
                self.repo.read_pack("prompts/local/link")
        self.assertIn(raised.exception.reason_code, {"invalid_input", "invalid_policy"})

    def test_preview_case_unknown_and_invalid(self) -> None:
        """验证未知 preview ID 与越界 ID 都返回 unknown_case。"""
        with self.assertRaises(PolicyError) as raised:
            self.repo.read_preview_case("does-not-exist")
        self.assertEqual(raised.exception.reason_code, "unknown_case")
        with self.assertRaises(PolicyError):
            self.repo.read_preview_case("../policy-greeting")

    def test_preview_case_duplicate_json_keys_rejected(self) -> None:
        """验证 preview 案例输入也在共享边界拒绝重复 JSON 键。"""
        target = self.root / "tests/fixtures/policy-preview/duplicate.json"
        target.write_text(
            '{"schema_version":1,"schema_version":1}', encoding="utf-8"
        )
        with self.assertRaises(PolicyError) as raised:
            self.repo.read_preview_case("duplicate")
        self.assertEqual(raised.exception.reason_code, "unknown_case")

    def test_list_keeps_link_candidates_without_following(self) -> None:
        """验证 list 保留链接候选路径并标无效，不跟随或递归读取。"""
        files = real_example_files()
        write_pack(self.root, "prompts/local/link", files)
        with patch(
            "app.adapters.files.policy._is_link_like",
            side_effect=lambda path: Path(path).name == "link",
        ):
            self.assertIn("prompts/local/link", self.repo.list_pack_dirs())
            listed = policy_usecases.list_packs(self.repo)
        self.assertEqual(listed["packs"][0]["path"], "prompts/local/link")
        self.assertFalse(listed["packs"][0]["valid"])
        self.assertIsNone(listed["packs"][0]["pack_hash"])

    def test_list_history_keeps_link_candidates_without_following(self) -> None:
        """验证 history 链接候选也保留路径并标无效，不跟随目标。"""
        history = self.root / "prompts/local/history"
        history.mkdir(parents=True)
        snapshot_id = "20261124T000000000Z-abcdef05"
        (history / snapshot_id).mkdir()
        with patch(
            "app.adapters.files.policy._is_link_like",
            side_effect=lambda path: Path(path).name == snapshot_id,
        ):
            self.assertIn(
                f"prompts/local/history/{snapshot_id}",
                self.repo.list_history_dirs(),
            )
            listed = policy_usecases.list_packs(self.repo)
        self.assertEqual(listed["history"][0]["path"], f"prompts/local/history/{snapshot_id}")
        self.assertFalse(listed["history"][0]["valid"])

    def test_list_skips_history_and_dot_dirs(self) -> None:
        """验证 list 只列 local 直接活动包并跳过 history/点目录。"""
        files = real_example_files()
        write_pack(self.root, "prompts/local/demo", files)
        (self.root / "prompts/local/.hidden").mkdir()
        (self.root / "prompts/local/history").mkdir()
        self.assertEqual(self.repo.list_pack_dirs(), ["prompts/local/demo"])
        self.assertEqual(self.repo.list_history_dirs(), [])


class WorkflowTests(unittest.TestCase):
    """覆盖 init/validate/preview/list 的实际文件流程与固定输出。"""

    def setUp(self) -> None:
        """创建临时项目根与文件适配器供文件流程测试使用。"""
        self._tmp, self.root = temp_project()
        self.repo = FilePolicyRepository(self.root)

    def tearDown(self) -> None:
        """清理本测试创建的临时项目根。"""
        self._tmp.cleanup()

    def test_init_validate_preview_path_and_guides(self) -> None:
        """验证 init 生成四载荷/双指南、profile 身份、validate 与 preview。"""
        result = policy_usecases.init_pack(self.repo, "prompts/local/demo", now=NOW)
        self.assertEqual(result["profile_id"], "demo")
        self.assertEqual(
            set(self.repo.read_pack("prompts/local/demo")), set(PACK_FILE_NAMES)
        )
        self.assertEqual(
            set(self.repo.read_guides("prompts/local/demo")), {"AGENTS.md", "CLAUDE.md"}
        )
        pack_text = (self.root / "prompts/local/demo/pack.toml").read_bytes().decode("utf-8")
        self.assertIn('profile_id = "demo"', pack_text)
        self.assertFalse(any("staging" in item.name for item in (self.root / "prompts/local").iterdir()))
        validated = policy_usecases.validate_pack(self.repo, "prompts/local/demo")
        self.assertEqual(validated["profile_id"], "demo")
        self.assertEqual(validated["base_version"], "2026-11-24")
        self.assertEqual(validated["tiers"]["C"], sorted(validated["tiers"]["C"]))
        self.assertEqual(len(validated["hard_floor"]["A_allowed"]), 2)
        self.assertEqual(len(validated["hard_floor"]["B_minimum"]), 4)
        self.assertEqual(len(validated["hard_floor"]["C_only"]), 39)
        preview = policy_usecases.preview_pack(
            self.repo, "prompts/local/demo", "policy-greeting"
        )
        self.assertTrue(preview["preview_only"])
        self.assertEqual(preview["rendered"]["protocol"]["task_id"], "preview-task-policy-greeting")
        self.assertIn("system_message", preview["rendered"])
        self.assertIn("developer_message", preview["rendered"])
        self.assertIn("user_message", preview["rendered"])

    def test_init_does_not_overwrite_or_leave_half_pack(self) -> None:
        """验证 init 不覆盖且写入失败不留可加载半包。"""
        policy_usecases.init_pack(self.repo, "prompts/local/demo", now=NOW)
        with self.assertRaises(PolicyError) as raised:
            policy_usecases.init_pack(self.repo, "prompts/local/demo", now=NOW)
        self.assertEqual(raised.exception.reason_code, "policy_exists")
        with patch.object(
            self.repo,
            "write_text_exact",
            side_effect=PolicyError("policy_io_error"),
        ):
            with self.assertRaises(PolicyError) as raised:
                policy_usecases.init_pack(self.repo, "prompts/local/failed", now=NOW)
        self.assertEqual(raised.exception.reason_code, "policy_io_error")
        self.assertFalse((self.root / "prompts/local/failed").exists())
        self.assertEqual(self.repo.list_pack_dirs(), ["prompts/local/demo"])

    def test_init_paths_and_profile_identity(self) -> None:
        """验证 init 只接受 local 直接子目录并沿用目录身份。"""
        for value in (
            "prompts/local/history",
            "prompts/local/.hidden",
            "prompts/local/1bad",
            "prompts/local/a/b",
            "/tmp/demo",
            "C:/tmp/demo",
        ):
            with self.assertRaises(PolicyError) as raised:
                policy_usecases.init_pack(self.repo, value, now=NOW)
            self.assertIn(raised.exception.reason_code, {"invalid_input", "policy_exists"})
        result = policy_usecases.init_pack(self.repo, "prompts/local/my-profile", now=NOW)
        self.assertEqual(result["profile_id"], "my-profile")

    def test_validate_and_preview_report_located_errors(self) -> None:
        """验证坏包诊断定位项目相对文件与行号。"""
        files = real_example_files()
        files["tone.md"] = ""
        write_pack(self.root, "prompts/local/bad", files)
        with self.assertRaises(PolicyError) as raised:
            policy_usecases.validate_pack(self.repo, "prompts/local/bad")
        self.assertEqual(raised.exception.reason_code, "invalid_policy")
        issues = raised.exception.issues
        self.assertTrue(
            any(
                item.file == "prompts/local/bad/tone.md" and item.line == 1
                for item in issues
            )
        )
        with self.assertRaises(PolicyError) as raised:
            policy_usecases.preview_pack(self.repo, "prompts/local/bad", "policy-greeting")
        self.assertEqual(raised.exception.reason_code, "invalid_policy")

    def test_preview_job_and_anomaly_select_contracts(self) -> None:
        """验证 preview 按 kind 选择职位或 HR base，并拒绝未知案例。"""
        policy_usecases.init_pack(self.repo, "prompts/local/demo", now=NOW)
        job = policy_usecases.preview_pack(self.repo, "prompts/local/demo", "policy-job-card")
        self.assertIn("# 职位评估契约", job["rendered"]["system_message"])
        anomaly = policy_usecases.preview_pack(self.repo, "prompts/local/demo", "policy-anomaly")
        self.assertIn("# HR 消息意图与回复契约", anomaly["rendered"]["system_message"])
        with self.assertRaises(PolicyError) as raised:
            policy_usecases.preview_pack(self.repo, "prompts/local/demo", "missing-case")
        self.assertEqual(raised.exception.reason_code, "unknown_case")

    def test_edit_tone_tiers_disclosure_without_code(self) -> None:
        """验证只改 md/TOML 即可变更语气、分档与披露并改变摘要。"""
        policy_usecases.init_pack(self.repo, "prompts/local/demo", now=NOW)
        original = policy_usecases.validate_pack(self.repo, "prompts/local/demo")
        target = self.root / "prompts/local/demo"
        (target / "tone.md").write_text("新的合成语气。\n", encoding="utf-8")
        text = (target / "pack.toml").read_text(encoding="utf-8")
        text = text.replace('text = "个人开发AI助手"', 'text = "个人开发AI助手（合成）"')
        text = text.replace(
            'C = ["interested.*", "rejected.*", "declined.*", "needs_human.*", "uncertain.*"]',
            'C = ["rejected.*", "declined.*", "needs_human.*", "uncertain.*", "interested.*"]',
        )
        (target / "pack.toml").write_text(text, encoding="utf-8")
        edited = policy_usecases.validate_pack(self.repo, "prompts/local/demo")
        self.assertNotEqual(original["pack_hash"], edited["pack_hash"])
        preview = policy_usecases.preview_pack(self.repo, "prompts/local/demo", "policy-greeting")
        self.assertIn("新的合成语气。", preview["rendered"]["developer_message"])
        self.assertIn("个人开发AI助手（合成）", preview["rendered"]["developer_message"])

    def test_init_invalid_example_returns_controlled_four_key_error(self) -> None:
        """验证坏示例下 init 不崩溃、输出四键错误且不留半包。"""
        example = self.root / "prompts/examples/default/pack.toml"
        text = example.read_bytes().decode("utf-8")
        example.write_bytes(text.replace("max_chars = 300", "max_chars = 0").encode("utf-8"))
        code, payload, stderr = run_cli(
            self.root, ["policy", "init", "--dest", "prompts/local/failed"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assertEqual(
            set(payload), {"schema_version", "status", "reason_code", "errors"}
        )
        self.assertEqual(payload["reason_code"], "invalid_policy")
        self.assertTrue(payload["errors"])
        self.assertTrue(
            all(set(item) == {"file", "field", "line", "code"} for item in payload["errors"])
        )
        self.assertFalse((self.root / "prompts/local/failed").exists())

    def test_guide_sensitive_content_is_rejected(self) -> None:
        """验证可选双指南也须通过控制字符与联系方式检查。"""
        files = real_example_files()
        write_pack(self.root, "prompts/local/guided", files)
        target = self.root / "prompts/local/guided"
        (target / "AGENTS.md").write_text("联系 13800000000\n", encoding="utf-8")
        (target / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        with self.assertRaises(PolicyError) as raised:
            policy_usecases.validate_pack(self.repo, "prompts/local/guided")
        self.assertEqual(raised.exception.reason_code, "invalid_policy")
        self.assertTrue(
            any(
                item.file == "prompts/local/guided/AGENTS.md"
                and item.code == "sensitive"
                for item in raised.exception.issues
            )
        )
        listed = policy_usecases.list_packs(self.repo)
        self.assertEqual(listed["packs"][0]["valid"], False)

    def test_list_does_not_select_latest_or_output_body(self) -> None:
        """验证 list 分离活动包/快照、不选最新且不输出正文。"""
        policy_usecases.init_pack(self.repo, "prompts/local/one", now=NOW)
        snapshot = policy_usecases.snapshot_pack(
            self.repo,
            "prompts/local/one",
            now=NOW,
            snapshot_id="20261124T000000000Z-abcdef01",
        )
        listed = policy_usecases.list_packs(self.repo)
        self.assertEqual(set(listed), {"packs", "history"})
        self.assertEqual(listed["packs"][0]["path"], "prompts/local/one")
        self.assertEqual(listed["history"][0]["path"], snapshot["snapshot_path"])
        self.assertEqual(listed["packs"][0]["kind"], "active")
        self.assertEqual(listed["history"][0]["kind"], "history")
        self.assertNotIn("latest", json.dumps(listed))
        self.assertNotIn("新的合成语气", json.dumps(listed))

    def test_list_invalid_pack_is_marked_without_body(self) -> None:
        """验证坏包只标无效且不输出正文/文件名。"""
        files = real_example_files()
        files["tone.md"] = ""
        write_pack(self.root, "prompts/local/bad", files)
        listed = policy_usecases.list_packs(self.repo)
        self.assertEqual(listed["packs"][0]["valid"], False)
        self.assertIsNone(listed["packs"][0]["pack_hash"])
        self.assertNotIn("tone.md", json.dumps(listed))


class SnapshotRestoreTests(unittest.TestCase):
    """覆盖快照清单、三值一致性、受控拒绝、失败清理与不覆盖。"""

    def setUp(self) -> None:
        """创建临时项目根、初始化活动包并生成固定合成快照。"""
        self._tmp, self.root = temp_project()
        self.repo = FilePolicyRepository(self.root)
        policy_usecases.init_pack(self.repo, "prompts/local/demo", now=NOW)
        self.snapshot = policy_usecases.snapshot_pack(
            self.repo,
            "prompts/local/demo",
            now=NOW,
            snapshot_id="20261124T000000000Z-abcdef01",
        )

    def tearDown(self) -> None:
        """清理本测试创建的临时项目根。"""
        self._tmp.cleanup()

    def test_manifest_and_copy_hashes(self) -> None:
        """验证清单固定七键、ID 词法、创建时间与副本原字节一致。"""
        manifest_path = self.root / self.snapshot["manifest_path"]
        manifest = json.loads(manifest_path.read_bytes().decode("utf-8"))
        self.assertEqual(
            set(manifest),
            {
                "schema_version",
                "profile_id",
                "source_pack",
                "base_version",
                "base_hash",
                "pack_hash",
                "created_at",
            },
        )
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["profile_id"], "demo")
        self.assertEqual(manifest["source_pack"], "prompts/local/demo")
        self.assertEqual(manifest["created_at"], NOW)
        self.assertEqual(manifest["pack_hash"], self.snapshot["pack_hash"])
        self.assertTrue(is_snapshot_id(Path(self.snapshot["snapshot_path"]).name))
        for name in PACK_FILE_NAMES:
            self.assertEqual(
                (self.root / self.snapshot["snapshot_path"] / name).read_bytes(),
                (self.root / "prompts/local/demo" / name).read_bytes(),
            )

    def test_snapshot_collision_and_invalid_source(self) -> None:
        """验证快照 ID 冲突与以 history 作为活动源都拒绝。"""
        with self.assertRaises(PolicyError) as raised:
            policy_usecases.snapshot_pack(
                self.repo,
                "prompts/local/demo",
                now=NOW,
                snapshot_id="20261124T000000000Z-abcdef01",
            )
        self.assertEqual(raised.exception.reason_code, "policy_exists")
        with self.assertRaises(PolicyError):
            policy_usecases.snapshot_pack(
                self.repo, self.snapshot["snapshot_path"], now=NOW
            )

    def test_snapshot_failure_cleans_staging_and_final(self) -> None:
        """验证发布失败只保留既有快照并清理本次暂存。"""
        with patch.object(
            self.repo, "promote", side_effect=PolicyError("policy_io_error")
        ):
            with self.assertRaises(PolicyError) as raised:
                policy_usecases.snapshot_pack(
                    self.repo,
                    "prompts/local/demo",
                    now=NOW,
                    snapshot_id="20261124T000000000Z-abcdef02",
                )
        self.assertEqual(raised.exception.reason_code, "policy_io_error")
        history = self.root / "prompts/local/history"
        self.assertEqual(
            sorted(name for name in os.listdir(history) if not name.startswith(".")),
            [
                "20261124T000000000Z-abcdef01",
                "20261124T000000000Z-abcdef01.local.json",
            ],
        )

    def test_snapshot_missing_manifest_fails_without_half_snapshot(self) -> None:
        """验证清单缺失时不发布快照目录或清单。"""
        with patch.object(self.repo, "write_manifest", return_value=None):
            with self.assertRaises(PolicyError):
                policy_usecases.snapshot_pack(
                    self.repo,
                    "prompts/local/demo",
                    now=NOW,
                    snapshot_id="20261124T000000000Z-abcdef03",
                )
        self.assertFalse(
            (self.root / "prompts/local/history/20261124T000000000Z-abcdef03").exists()
        )
        self.assertFalse(
            (self.root / "prompts/local/history/20261124T000000000Z-abcdef03.local.json").exists()
        )

    def test_snapshot_rejects_source_change_during_copy(self) -> None:
        """验证复制期间活动包被改时受控拒绝并清理本次发布/暂存。"""
        original = self.repo.write_text_exact
        changed = False

        def changing_write(rel: str, text: str) -> None:
            """首次暂存 tone 写入后模拟活动包被外部编辑。"""
            nonlocal changed
            original(rel, text)
            if not changed and rel.endswith("/tone.md") and ".staging-" in rel:
                changed = True
                active = self.root / "prompts/local/demo/tone.md"
                active.write_bytes(active.read_bytes() + "合成编辑".encode("utf-8"))

        with patch.object(self.repo, "write_text_exact", side_effect=changing_write):
            with self.assertRaises(PolicyError) as raised:
                policy_usecases.snapshot_pack(
                    self.repo,
                    "prompts/local/demo",
                    now=NOW,
                    snapshot_id="20261124T000000000Z-abcdef04",
                )
        self.assertEqual(raised.exception.reason_code, "policy_changed")
        history = self.root / "prompts/local/history"
        self.assertFalse(
            (history / "20261124T000000000Z-abcdef04").exists()
        )
        self.assertFalse(
            (history / "20261124T000000000Z-abcdef04.local.json").exists()
        )
        self.assertTrue((self.root / "prompts/local/demo/tone.md").exists())

    def test_restore_unreadable_profile_is_controlled_refusal(self) -> None:
        """验证 profile 库不可读时生产 CLI restore 以未知状态受控拒绝。"""
        profile_dir = self.root / "data/runtime/profiles/demo"
        profile_dir.mkdir(parents=True)
        (profile_dir / "runtime.sqlite3").write_bytes(b"not-a-sqlite-database")
        code, payload, stderr = run_cli(
            self.root,
            [
                "policy",
                "restore",
                "--snapshot",
                self.snapshot["snapshot_path"],
                "--dest",
                "prompts/local/rollback",
            ],
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["reason_code"], "policy_changed")
        self.assertEqual(payload["errors"][0]["code"], "conflict")
        self.assertEqual(stderr, "")
        self.assertFalse((self.root / "prompts/local/rollback").exists())

    def test_restore_with_synthetic_stopped_evidence_keeps_profile(self) -> None:
        """验证同一生产用例注入 stopped 后恢复三值一致且保留 profile。"""
        result = policy_usecases.restore_pack(
            self.repo,
            self.snapshot["snapshot_path"],
            "prompts/local/another-directory",
            _StoppedActivity(),
        )
        self.assertEqual(result["profile_id"], "demo")
        self.assertEqual(result["pack_hash"], self.snapshot["pack_hash"])
        self.assertEqual(
            result["pack_hash"],
            json.loads(
                (self.root / self.snapshot["manifest_path"]).read_bytes().decode("utf-8")
            )["pack_hash"],
        )
        restored = policy_usecases.validate_pack(
            self.repo, "prompts/local/another-directory"
        )
        self.assertEqual(restored["pack_hash"], self.snapshot["pack_hash"])
        self.assertEqual(restored["profile_id"], "demo")
        preview = policy_usecases.preview_pack(
            self.repo, "prompts/local/another-directory", "policy-greeting"
        )
        self.assertTrue(preview["preview_only"])
        with self.assertRaises(PolicyError) as raised:
            policy_usecases.restore_pack(
                self.repo,
                self.snapshot["snapshot_path"],
                "prompts/local/another-directory",
                _StoppedActivity(),
            )
        self.assertEqual(raised.exception.reason_code, "policy_exists")

    def test_restore_rejects_base_mismatch_and_tampering(self) -> None:
        """验证 base 不符与清单 pack_hash 篡改都拒绝恢复。"""
        base = self.root / "prompts/base/intent-and-reply.md"
        base.write_bytes(base.read_bytes() + b"\n")
        with self.assertRaises(PolicyError) as raised:
            policy_usecases.restore_pack(
                self.repo,
                self.snapshot["snapshot_path"],
                "prompts/local/rollback",
                _StoppedActivity(),
            )
        self.assertEqual(raised.exception.reason_code, "policy_changed")
        base.write_bytes(base.read_bytes()[:-1])
        manifest_path = self.root / self.snapshot["manifest_path"]
        manifest = json.loads(manifest_path.read_bytes().decode("utf-8"))
        manifest["pack_hash"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(PolicyError) as raised:
            policy_usecases.restore_pack(
                self.repo,
                self.snapshot["snapshot_path"],
                "prompts/local/rollback",
                _StoppedActivity(),
            )
        self.assertEqual(raised.exception.reason_code, "policy_changed")

    def test_restore_rejects_snapshot_byte_tampering(self) -> None:
        """验证快照载荷字节篡改与清单不符时拒绝。"""
        target = self.root / self.snapshot["snapshot_path"] / "tone.md"
        target.write_bytes(target.read_bytes() + b"\n")
        with self.assertRaises(PolicyError) as raised:
            policy_usecases.restore_pack(
                self.repo,
                self.snapshot["snapshot_path"],
                "prompts/local/rollback",
                _StoppedActivity(),
            )
        self.assertEqual(raised.exception.reason_code, "policy_changed")
        self.assertFalse((self.root / "prompts/local/rollback").exists())

    def test_restore_target_mismatch_removes_published_target(self) -> None:
        """验证目标发布后复算不符会删除本次目标。"""
        original = self.repo.read_pack

        def tampered(rel: str) -> dict[str, str]:
            """只为目标目录返回篡改后的包文本，模拟发布后复算不一致。"""
            files = original(rel)
            if rel == "prompts/local/rollback":
                files["tone.md"] = files["tone.md"] + "x"
            return files

        with patch.object(self.repo, "read_pack", side_effect=tampered):
            with self.assertRaises(PolicyError) as raised:
                policy_usecases.restore_pack(
                    self.repo,
                    self.snapshot["snapshot_path"],
                    "prompts/local/rollback",
                    _StoppedActivity(),
                )
        self.assertEqual(raised.exception.reason_code, "policy_changed")
        self.assertFalse((self.root / "prompts/local/rollback").exists())

    def test_restore_rejects_bad_paths_and_missing_manifest(self) -> None:
        """验证非法快照/目标路径与缺失清单的失败出口。"""
        for snapshot, dest in (
            ("prompts/local/history/not-a-snapshot", "prompts/local/rollback"),
            (self.snapshot["snapshot_path"], "prompts/local/history"),
            (self.snapshot["snapshot_path"], "/tmp/rollback"),
        ):
            with self.assertRaises(PolicyError) as raised:
                policy_usecases.restore_pack(
                    self.repo, snapshot, dest, _StoppedActivity()
                )
            self.assertEqual(raised.exception.reason_code, "invalid_input")
        (self.root / self.snapshot["manifest_path"]).unlink()
        with self.assertRaises(PolicyError) as raised:
            policy_usecases.restore_pack(
                self.repo,
                self.snapshot["snapshot_path"],
                "prompts/local/rollback",
                _StoppedActivity(),
            )
        self.assertEqual(raised.exception.reason_code, "policy_io_error")


class CliAndLogTests(unittest.TestCase):
    """覆盖 policy 四键外层、参数错误、既有命令三键与 zero-device 日志。"""

    def setUp(self) -> None:
        """创建临时项目根供 CLI 与日志测试使用。"""
        self._tmp, self.root = temp_project()

    def tearDown(self) -> None:
        """清理本测试创建的临时项目根。"""
        self._tmp.cleanup()

    def test_cli_success_and_failure_outer_shapes(self) -> None:
        """验证 policy 成功/失败四键外层与 stderr 为空。"""
        code, payload, stderr = run_cli(
            self.root, ["policy", "init", "--dest", "prompts/local/demo"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            list(payload),
            ["schema_version", "status", "reason_code", "result"],
        )
        self.assertIsNone(payload["reason_code"])
        code, payload, stderr = run_cli(
            self.root, ["policy", "validate", "--pack", "prompts/local/demo"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        code, payload, stderr = run_cli(
            self.root, ["policy", "validate", "--pack", "prompts/local/missing"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assertEqual(
            list(payload),
            ["schema_version", "status", "reason_code", "errors"],
        )
        self.assertIn("reason_code", payload)
        self.assertTrue(payload["errors"])

    def test_policy_argument_errors_use_four_keys_but_other_commands_keep_three(self) -> None:
        """验证 policy 参数错误四键而其它命令仍三键。"""
        code, payload, _ = run_cli(self.root, ["policy", "init"])
        self.assertEqual(code, 2)
        self.assertEqual(set(payload), {"schema_version", "status", "reason_code", "errors"})
        self.assertEqual(payload["errors"], [])
        code, payload, _ = run_cli(self.root, ["read"])
        self.assertEqual(code, 2)
        self.assertEqual(set(payload), {"schema_version", "status", "reason_code"})

    def test_policy_config_errors_keep_four_keys_and_other_commands_keep_three(self) -> None:
        """验证配置受控/意外错误在 policy 分支保持四键，其它命令仍三键。"""
        code, payload, _ = run_cli(
            self.root, ["--config", "missing-synthetic.json", "policy", "list"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(
            set(payload), {"schema_version", "status", "reason_code", "errors"}
        )
        self.assertEqual(payload["reason_code"], "invalid_config")
        self.assertEqual(payload["errors"], [])
        with patch("app.cli.main._validate_config", side_effect=RuntimeError("synthetic")):
            code, payload, _ = run_cli(self.root, ["--config", "any", "policy", "list"])
        self.assertEqual(code, 2)
        self.assertEqual(
            set(payload), {"schema_version", "status", "reason_code", "errors"}
        )
        self.assertEqual(payload["reason_code"], "invalid_config")
        code, payload, _ = run_cli(
            self.root,
            ["--config", "missing-synthetic.json", "doctor", "--fixture", "tests/fixtures/doctor/ready.json"],
        )
        self.assertEqual(code, 2)
        self.assertEqual(set(payload), {"schema_version", "status", "reason_code"})

    def test_config_value_containing_policy_does_not_change_command_outer(self) -> None:
        """验证只按命令位置识别 policy，配置值含 policy 不改变三键出口。"""
        code, payload, _ = run_cli(
            self.root,
            ["--config", "policy", "doctor", "--fixture", "tests/fixtures/doctor/ready.json"],
        )
        self.assertEqual(code, 2)
        self.assertEqual(set(payload), {"schema_version", "status", "reason_code"})
        self.assertEqual(payload["reason_code"], "invalid_config")

    def test_list_and_unknown_case(self) -> None:
        """验证空 list 输出与未知 preview 案例错误。"""
        code, payload, _ = run_cli(self.root, ["policy", "list"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["result"], {"packs": [], "history": []})
        run_cli(self.root, ["policy", "init", "--dest", "prompts/local/demo"])
        code, payload, _ = run_cli(
            self.root,
            ["policy", "preview", "--pack", "prompts/local/demo", "--case", "nope"],
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "unknown_case")

    def test_policy_commands_do_not_start_processes_or_network(self) -> None:
        """验证 policy 五命令零子进程与零网络调用。"""
        with patch("subprocess.run", side_effect=AssertionError("subprocess")), patch(
            "socket.create_connection", side_effect=AssertionError("network")
        ):
            for argv in (
                ["policy", "init", "--dest", "prompts/local/demo"],
                ["policy", "validate", "--pack", "prompts/local/demo"],
                ["policy", "preview", "--pack", "prompts/local/demo", "--case", "policy-greeting"],
                ["policy", "list"],
                ["policy", "snapshot", "--pack", "prompts/local/demo"],
            ):
                code, payload, _ = run_cli(self.root, argv)
                self.assertEqual(code, 0, payload)

    def test_log_records_policy_started_and_terminal_events(self) -> None:
        """验证 policy 日志固定键、started→ok 与 correlation。"""
        log = ProcessLog(self.root / "events.jsonl", correlation_id="corr")
        code, _, _ = run_cli(
            self.root,
            ["policy", "init", "--dest", "prompts/local/demo"],
            process_log=log,
        )
        self.assertEqual(code, 0)
        log_path = log.path
        events = [
            json.loads(line)
            for line in log_path.read_bytes().decode("utf-8").splitlines()
        ]
        self.assertTrue(all(list(event) == list(EVENT_KEYS) for event in events))
        policy_events = [event for event in events if event["stage"] == "policy"]
        self.assertEqual(
            [(event["operation"], event["result"]) for event in policy_events],
            [
                ("init", "started"),
                ("load", "started"),
                ("load", "ok"),
                ("validate", "started"),
                ("validate", "ok"),
                ("publish", "started"),
                ("publish", "ok"),
                ("init", "ok"),
            ],
        )
        self.assertTrue(all(event["duration_ms"] >= 0 for event in policy_events))
        self.assertEqual({event["correlation_id"] for event in events}, {"corr"})

    def test_failed_and_undetermined_policy_logs(self) -> None:
        """验证坏包日志 failed 原因/分类与 exit 收尾。"""
        files = real_example_files()
        files["tone.md"] = ""
        write_pack(self.root, "prompts/local/bad", files)
        log = ProcessLog(self.root / "failed.jsonl", correlation_id="corr")
        code, payload, _ = run_cli(
            self.root,
            ["policy", "validate", "--pack", "prompts/local/bad"],
            process_log=log,
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "invalid_policy")
        events = [
            json.loads(line)
            for line in log.path.read_bytes().decode("utf-8").splitlines()
        ]
        failed = [event for event in events if event["stage"] == "policy"]
        self.assertEqual(failed[-1]["result"], "failed")
        self.assertEqual(failed[-1]["reason_code"], "invalid_policy")
        self.assertEqual(failed[-1]["error_category"], "policy")
        self.assertEqual(events[-1]["stage"], "exit")

    def test_restore_unknown_is_logged_as_undetermined(self) -> None:
        """验证 restore 活动未知记 undetermined 与 policy_changed。"""
        run_cli(self.root, ["policy", "init", "--dest", "prompts/local/demo"])
        snapshot = policy_usecases.snapshot_pack(
            FilePolicyRepository(self.root),
            "prompts/local/demo",
            now=NOW,
            snapshot_id="20261124T000000000Z-abcdef01",
        )
        profile_dir = self.root / "data/runtime/profiles/demo"
        profile_dir.mkdir(parents=True)
        (profile_dir / "runtime.sqlite3").write_bytes(b"not-a-sqlite-database")
        log = ProcessLog(self.root / "restore.jsonl", correlation_id="corr")
        code, payload, _ = run_cli(
            self.root,
            [
                "policy",
                "restore",
                "--snapshot",
                snapshot["snapshot_path"],
                "--dest",
                "prompts/local/rollback",
            ],
            process_log=log,
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason_code"], "policy_changed")
        events = [
            json.loads(line)
            for line in log.path.read_bytes().decode("utf-8").splitlines()
        ]
        restore_events = [
            event
            for event in events
            if event["stage"] == "policy" and event["operation"] == "restore"
        ]
        self.assertEqual(restore_events[-1]["result"], "undetermined")
        self.assertEqual(restore_events[-1]["reason_code"], "policy_changed")
        validate_events = [
            event
            for event in events
            if event["stage"] == "policy" and event["operation"] == "validate"
        ]
        self.assertTrue(
            any(
                event["result"] == "undetermined"
                and event["reason_code"] == "policy_changed"
                for event in validate_events
            )
        )

    def test_preview_and_snapshot_log_real_stages(self) -> None:
        """验证 preview 记录 load/validate/render，snapshot 记录 publish。"""
        run_cli(self.root, ["policy", "init", "--dest", "prompts/local/demo"])
        log = ProcessLog(self.root / "stages.jsonl", correlation_id="corr")
        code, _, _ = run_cli(
            self.root,
            ["policy", "preview", "--pack", "prompts/local/demo", "--case", "policy-greeting"],
            process_log=log,
        )
        self.assertEqual(code, 0)
        events = [
            json.loads(line)
            for line in log.path.read_bytes().decode("utf-8").splitlines()
        ]
        operations = [
            (event["operation"], event["result"])
            for event in events
            if event["stage"] == "policy"
        ]
        self.assertIn(("load", "started"), operations)
        self.assertIn(("load", "ok"), operations)
        self.assertIn(("validate", "ok"), operations)
        self.assertIn(("render", "started"), operations)
        self.assertIn(("render", "ok"), operations)
        self.assertIn(("preview", "ok"), operations)
        log2 = ProcessLog(self.root / "snapshot-stages.jsonl", correlation_id="corr2")
        code, _, _ = run_cli(
            self.root,
            ["policy", "snapshot", "--pack", "prompts/local/demo"],
            process_log=log2,
        )
        self.assertEqual(code, 0)
        events2 = [
            json.loads(line)
            for line in log2.path.read_bytes().decode("utf-8").splitlines()
        ]
        operations2 = [
            (event["operation"], event["result"])
            for event in events2
            if event["stage"] == "policy"
        ]
        self.assertIn(("publish", "started"), operations2)
        self.assertIn(("publish", "ok"), operations2)
        self.assertIn(("snapshot", "ok"), operations2)

    def test_direct_usecase_logs_stages_with_same_correlation(self) -> None:
        """验证直接调用用例也由拥有实际操作的层记录阶段，不依赖 CLI 事后补写。"""
        repo = FilePolicyRepository(self.root)
        policy_usecases.init_pack(repo, "prompts/local/demo", now=NOW)
        log = ProcessLog(self.root / "direct.jsonl", correlation_id="direct")
        policy_usecases.validate_pack(repo, "prompts/local/demo", process_log=log)
        events = [
            json.loads(line)
            for line in log.path.read_bytes().decode("utf-8").splitlines()
        ]
        self.assertEqual(
            [(event["operation"], event["result"]) for event in events],
            [
                ("load", "started"),
                ("load", "ok"),
                ("validate", "started"),
                ("validate", "ok"),
            ],
        )
        self.assertEqual({event["correlation_id"] for event in events}, {"direct"})

    def test_log_failure_does_not_change_policy_business_result(self) -> None:
        """验证选择性/全量日志写入失败不改 policy 业务输出与退出码。"""
        log = ProcessLog(self.root / "degraded.jsonl", correlation_id="corr")

        def failing_record(**kwargs):
            """模拟所有日志写入失败并置降级标记。"""
            del kwargs
            log.degraded = True
            return False

        with patch.object(log, "record", side_effect=failing_record):
            code, payload, stderr = run_cli(
                self.root,
                ["policy", "init", "--dest", "prompts/local/demo"],
                process_log=log,
            )
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(stderr, "m0_process_log_degraded\n")
        self.assertTrue((self.root / "prompts/local/demo").exists())

    def test_full_edit_snapshot_restore_validate_preview_path(self) -> None:
        """验证编辑→validate→preview→snapshot→恢复→再 validate/preview 全路径。"""
        repo = FilePolicyRepository(self.root)
        policy_usecases.init_pack(repo, "prompts/local/demo", now=NOW)
        target = self.root / "prompts/local/demo"
        (target / "tone.md").write_text("本机合成语气。\n", encoding="utf-8")
        self.assertEqual(
            run_cli(self.root, ["policy", "validate", "--pack", "prompts/local/demo"])[0],
            0,
        )
        self.assertEqual(
            run_cli(
                self.root,
                ["policy", "preview", "--pack", "prompts/local/demo", "--case", "policy-greeting"],
            )[0],
            0,
        )
        snapshot = policy_usecases.snapshot_pack(
            repo,
            "prompts/local/demo",
            now=NOW,
            snapshot_id="20261124T000000000Z-abcdef01",
        )
        restored = policy_usecases.restore_pack(
            repo, snapshot["snapshot_path"], "prompts/local/demo-restored", _StoppedActivity()
        )
        self.assertEqual(restored["pack_hash"], snapshot["pack_hash"])
        code, payload, _ = run_cli(
            self.root, ["policy", "validate", "--pack", "prompts/local/demo-restored"]
        )
        self.assertEqual(code, 0, payload)
        code, payload, _ = run_cli(
            self.root,
            [
                "policy",
                "preview",
                "--pack",
                "prompts/local/demo-restored",
                "--case",
                "policy-greeting",
            ],
        )
        self.assertEqual(code, 0, payload)
        self.assertIn("本机合成语气。", payload["result"]["rendered"]["developer_message"])


class _StoppedActivity:
    """测试用显式合成停止证据；不进入生产 CLI 默认接线。"""

    def assess_restore(self, *, profile_id: str, dest_pack: str) -> str:
        """只对测试路径返回 stopped，证明同一生产用例的文件事务。"""
        del profile_id, dest_pack
        return "stopped"


if __name__ == "__main__":
    unittest.main()
