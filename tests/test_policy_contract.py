"""验证 M4a DecisionEnvelope/Decision 契约、摘要边界、限额与回复复验。"""

from __future__ import annotations

import hashlib
import json
import unittest

from app.domain.models import add_seconds, canonical_hash
from app.domain.policy import (
    BASE_FILE_NAMES,
    BASE_RELATIVE_PATHS,
    DECISION_KEYS,
    ENVELOPE_KEYS,
    DecisionError,
    ReplyError,
    append_and_validate_reply,
    build_envelope,
    compute_base_hash,
    compute_input_hash,
    compute_pack_hash,
    compute_prompt_hash,
    effective_limits,
    envelope_bytes,
    parse_decision,
    validate_decision,
    validate_decision_envelope,
)
from tests.policy_helpers import (
    NOW,
    lease_expiry,
    real_base_files,
    real_example_files,
    real_example_pack,
    real_preview_case,
    valid_decision,
    valid_envelope,
)


def rehash(envelope: dict[str, object]) -> dict[str, object]:
    """在定制字段后按真实 base/示例包重算 prompt_hash 与 input_hash。"""
    data = dict(envelope)
    data["input_hash"] = ""
    data["prompt_hash"] = compute_prompt_hash(
        real_base_files(), real_example_pack(), data
    )
    data["input_hash"] = compute_input_hash(data)
    return data


class HashContractTests(unittest.TestCase):
    """固定 H 的键排序、UTF-8、紧凑分隔符与无规范化边界。"""

    def test_canonical_hash_matches_manual_compact_utf8(self) -> None:
        """验证 H 与手写键排序紧凑 UTF-8 字节的 SHA-256 完全一致。"""
        expected = hashlib.sha256(
            '{"a":"中","b":1}'.encode("utf-8")
        ).hexdigest()
        self.assertEqual(canonical_hash({"b": 1, "a": "中"}), expected)

    def test_canonical_hash_does_not_normalize_text(self) -> None:
        """验证 H 不对空白、换行或 Unicode 组合形式做任何规范化。"""
        self.assertNotEqual(canonical_hash("a"), canonical_hash("a "))
        self.assertNotEqual(canonical_hash("é"), canonical_hash("e\u0301"))
        self.assertNotEqual(canonical_hash("a\r\nb"), canonical_hash("a\nb"))

    def test_base_hash_uses_project_relative_posix_paths(self) -> None:
        """验证 base_hash 只使用两份 base 的项目相对 POSIX 路径到原文映射。"""
        base = real_base_files()
        expected = canonical_hash(
            {BASE_RELATIVE_PATHS[name]: base[name] for name in BASE_FILE_NAMES}
        )
        self.assertEqual(compute_base_hash(base), expected)

    def test_pack_hash_exact_inputs_and_raw_byte_sensitivity(self) -> None:
        """验证 pack_hash 的精确输入，并证明任一原文空格变化会改变摘要。"""
        files = real_example_files()
        expected = canonical_hash(
            {
                "schema_version": 1,
                "files": {name: files[name] for name in files},
            }
        )
        self.assertEqual(compute_pack_hash(files), expected)
        changed = dict(files)
        changed["tone.md"] = changed["tone.md"].replace("简洁", "简洁 ")
        self.assertNotEqual(compute_pack_hash(files), compute_pack_hash(changed))

    def test_prompt_hash_has_no_self_reference(self) -> None:
        """验证 prompt_hash 去掉协议键后不随 task_id/nonce 变化，但随 inbound 变化。"""
        base = real_base_files()
        pack = real_example_pack()
        envelope = valid_envelope()
        first = str(envelope["prompt_hash"])
        changed_task = dict(envelope)
        changed_task["task_id"] = "another-task"
        self.assertEqual(
            compute_prompt_hash(base, pack, changed_task), first
        )
        changed_nonce = dict(envelope)
        changed_nonce["nonce"] = "00000000-0000-4000-8000-000000000299"
        self.assertEqual(
            compute_prompt_hash(base, pack, changed_nonce), first
        )
        changed_inbound = dict(envelope)
        changed_inbound["inbound"] = str(envelope["inbound"]) + "!"
        self.assertNotEqual(
            compute_prompt_hash(base, pack, changed_inbound), first
        )
        self.assertNotEqual(
            compute_input_hash(changed_task), compute_input_hash(envelope)
        )
        self.assertNotEqual(
            compute_input_hash(changed_nonce), compute_input_hash(envelope)
        )

    def test_input_hash_is_last_and_includes_prompt_hash(self) -> None:
        """验证 input_hash 在 prompt_hash 之后计算且覆盖完整 envelope 去自身。"""
        envelope = valid_envelope()
        expected = canonical_hash(
            {key: envelope[key] for key in ENVELOPE_KEYS if key != "input_hash"}
        )
        self.assertEqual(expected, envelope["input_hash"])
        self.assertEqual(compute_input_hash(envelope), envelope["input_hash"])

    def test_changing_pack_comment_changes_pack_and_prompt_hash(self) -> None:
        """验证 pack.toml 注释变化会同时改变 pack_hash 与 prompt_hash。"""
        base = real_base_files()
        pack = real_example_pack()
        envelope = valid_envelope()
        pack_files = dict(pack.files)
        pack_files["pack.toml"] = pack_files["pack.toml"].replace(
            "schema_version = 1", "# comment\nschema_version = 1"
        )
        changed_hash = compute_pack_hash(pack_files)
        self.assertNotEqual(changed_hash, pack.pack_hash)
        self.assertNotEqual(
            compute_prompt_hash(base, pack, envelope),
            compute_prompt_hash(base, type(pack)(**{**pack.__dict__, "files": pack_files}), envelope),
        )


class EnvelopeValidationTests(unittest.TestCase):
    """严格 envelope 键、类型、词表、有效期、回显摘要与字节限额。"""

    def assertInvalid(self, envelope: dict[str, object]) -> None:
        """断言给定 envelope 在显式 now 下被严格校验拒绝。"""
        with self.assertRaises(DecisionError):
            validate_decision_envelope(envelope, now=NOW)

    def test_valid_envelope_passes(self) -> None:
        """验证公开 base/示例构造的有效 envelope 通过全部契约并固定 C 档。"""
        envelope = valid_envelope()
        validate_decision_envelope(envelope, now=NOW)
        self.assertEqual(set(envelope), set(ENVELOPE_KEYS))
        self.assertEqual(envelope["policy_tier"], "C")
        self.assertEqual(envelope["constraints"]["max_bytes"], 2048)

    def test_missing_and_extra_keys_rejected(self) -> None:
        """验证 envelope 缺失任一必填键或增加额外键都被拒绝。"""
        for key in ENVELOPE_KEYS:
            envelope = dict(valid_envelope())
            del envelope[key]
            self.assertInvalid(envelope)
        envelope = dict(valid_envelope())
        envelope["extra"] = 1
        self.assertInvalid(envelope)

    def test_schema_version_rejects_bool_and_wrong_int(self) -> None:
        """验证 schema_version 只接受整数 1，bool、其它整数和字符串拒绝。"""
        for value in (True, 2, "1"):
            envelope = rehash(valid_envelope())
            envelope["schema_version"] = value
            self.assertInvalid(envelope)

    def test_text_and_identifier_fields(self) -> None:
        """验证非空文本、UUID nonce 与四类 64hex 摘要的词法和类型。"""
        for field in ("task_id", "inbound", "base_version"):
            for value in ("", 1, "\ud800"):
                envelope = rehash(valid_envelope())
                envelope[field] = value
                self.assertInvalid(envelope)
        for value in ("not-a-uuid", "00000000000040008000000000000201", 1):
            envelope = rehash(valid_envelope())
            envelope["nonce"] = value
            self.assertInvalid(envelope)
        for field in ("input_hash", "base_hash", "policy_pack_hash", "prompt_hash"):
            envelope = rehash(valid_envelope())
            envelope[field] = "A" * 64
            self.assertInvalid(envelope)

    def test_app_kind_and_tier_vocabulary(self) -> None:
        """验证 app、kind 与 policy_tier 只接受注册词表值。"""
        for field, values in (
            ("app", ("other", 1)),
            ("kind", ("chat", None)),
            ("policy_tier", ("D", "a", 1)),
        ):
            for value in values:
                envelope = rehash(valid_envelope())
                envelope[field] = value
                self.assertInvalid(envelope)

    def test_subject_minimum_requirements(self) -> None:
        """验证 HR 会话键、职位摘要的最小要求及 anomaly 可双 null。"""
        hr = rehash(valid_envelope())
        hr["subject"] = {
            "conversation_key": "boss:conv:" + "0" * 64,
            "job_brief": "一句话摘要",
        }
        hr = rehash(hr)
        validate_decision_envelope(hr, now=NOW)
        hr_empty_secondary = rehash(valid_envelope())
        hr_empty_secondary["subject"] = {
            "conversation_key": "boss:conv:" + "0" * 64,
            "job_brief": "",
        }
        hr_empty_secondary = rehash(hr_empty_secondary)
        validate_decision_envelope(hr_empty_secondary, now=NOW)
        hr_missing = rehash(valid_envelope())
        hr_missing["subject"] = {"conversation_key": None, "job_brief": None}
        hr_missing = rehash(hr_missing)
        self.assertInvalid(hr_missing)
        job = rehash(valid_envelope())
        job["kind"] = "job_card"
        job["subject"] = {"conversation_key": None, "job_brief": "后端开发"}
        job = rehash(job)
        validate_decision_envelope(job, now=NOW)
        job_empty_secondary = rehash(valid_envelope())
        job_empty_secondary["kind"] = "job_card"
        job_empty_secondary["subject"] = {
            "conversation_key": "",
            "job_brief": "后端开发",
        }
        job_empty_secondary = rehash(job_empty_secondary)
        validate_decision_envelope(job_empty_secondary, now=NOW)
        job_missing = rehash(valid_envelope())
        job_missing["kind"] = "job_card"
        job_missing["subject"] = {"conversation_key": None, "job_brief": None}
        job_missing = rehash(job_missing)
        self.assertInvalid(job_missing)
        anomaly = rehash(valid_envelope())
        anomaly["kind"] = "anomaly"
        anomaly["subject"] = {"conversation_key": None, "job_brief": None}
        anomaly = rehash(anomaly)
        validate_decision_envelope(anomaly, now=NOW)
        anomaly_empty = rehash(valid_envelope())
        anomaly_empty["kind"] = "anomaly"
        anomaly_empty["subject"] = {"conversation_key": None, "job_brief": ""}
        anomaly_empty = rehash(anomaly_empty)
        validate_decision_envelope(anomaly_empty, now=NOW)
        for subject in (
            {"conversation_key": "x"},
            {"conversation_key": None, "job_brief": None, "extra": 1},
            {"conversation_key": 1, "job_brief": None},
        ):
            envelope = rehash(valid_envelope())
            envelope["subject"] = subject
            envelope = rehash(envelope)
            self.assertInvalid(envelope)

    def test_context_is_at_most_two_strings_and_empty_allowed(self) -> None:
        """验证 context 至多两条可编码字符串，合法空字符串不被额外拒绝。"""
        for context in ([""], ["a", "b"]):
            envelope = valid_envelope(context=list(context))
            validate_decision_envelope(envelope, now=NOW)
        for context in (["a", "b", "c"], "a", [1], ["\ud800"]):
            envelope = rehash(valid_envelope())
            envelope["context"] = context
            self.assertInvalid(envelope)

    def test_allowed_facts_strict_types_expiry_and_duplicates(self) -> None:
        """验证事实四键、来源、到期时间、重复键与过期拒绝。"""
        files = real_example_files()
        files["pack.toml"] = files["pack.toml"].replace(
            "allow_keys = []",
            'allow_keys = ["work_location", "years_experience", "salary_expectation"]',
        )
        from app.domain.policy import parse_and_validate_pack

        pack, issues = parse_and_validate_pack(files)
        self.assertEqual(issues, [])
        assert pack is not None
        fact = {
            "key": "work_location",
            "value": "合成城市",
            "source": "user_confirmed",
            "expires_at": add_seconds(NOW, 60),
        }
        envelope = valid_envelope(allowed_facts=[fact], pack=pack)
        validate_decision_envelope(envelope, now=NOW)
        expired = dict(fact)
        expired["expires_at"] = NOW
        expired_selected = valid_envelope(allowed_facts=[expired], pack=pack)
        self.assertEqual(expired_selected["allowed_facts"], [])
        expired_envelope = valid_envelope(allowed_facts=[], pack=pack)
        expired_envelope["allowed_facts"] = [expired]
        expired_envelope["prompt_hash"] = compute_prompt_hash(
            real_base_files(), pack, expired_envelope
        )
        expired_envelope["input_hash"] = compute_input_hash(expired_envelope)
        with self.assertRaises(DecisionError):
            validate_decision_envelope(expired_envelope, now=NOW)
        for mutation in (
            {"key": "", "value": "x", "source": "user_confirmed", "expires_at": None},
            {"key": "unknown_fact", "value": "x", "source": "user_confirmed", "expires_at": None},
            {"key": "work_location", "value": 1, "source": "user_confirmed", "expires_at": None},
            {"key": "work_location", "value": "x", "source": "model", "expires_at": None},
            {"key": "work_location", "value": "x", "source": "user_confirmed", "expires_at": "bad"},
            {"key": "work_location", "value": "x", "source": "user_confirmed", "expires_at": None, "extra": 1},
        ):
            invalid = dict(envelope)
            invalid["allowed_facts"] = [mutation]
            self.assertInvalid(invalid)
        duplicate = dict(envelope)
        duplicate["allowed_facts"] = [fact, fact]
        self.assertInvalid(duplicate)
        empty_value = dict(fact)
        empty_value["value"] = ""
        valid = dict(envelope)
        valid["allowed_facts"] = [empty_value]
        validate_decision_envelope(rehash(valid), now=NOW)

    def test_build_envelope_filters_effective_fact_intersection(self) -> None:
        """验证构建器按 now/白名单/确认/冲突过滤事实并保留独立有效项。"""
        from app.domain.policy import parse_and_validate_pack

        files = real_example_files()
        files["pack.toml"] = files["pack.toml"].replace(
            "allow_keys = []",
            'allow_keys = ["work_location", "years_experience"]',
        )
        pack, issues = parse_and_validate_pack(files)
        self.assertEqual(issues, [])
        assert pack is not None
        valid_years = {
            "key": "years_experience",
            "value": "5",
            "source": "user_confirmed",
            "expires_at": None,
        }
        facts = [
            valid_years,
            {
                "key": "work_location",
                "value": "合成城市",
                "source": "user_confirmed",
                "expires_at": NOW,
            },
            {
                "key": "salary_expectation",
                "value": "合成薪资",
                "source": "user_confirmed",
                "expires_at": None,
            },
            {
                "key": "work_location",
                "value": "合成城市",
                "source": "assistant",
                "expires_at": None,
            },
            {
                "key": "work_location",
                "value": "合成城市甲",
                "source": "user_confirmed",
                "expires_at": add_seconds(NOW, 60),
            },
            {
                "key": "work_location",
                "value": "合成城市乙",
                "source": "user_confirmed",
                "expires_at": add_seconds(NOW, 60),
            },
        ]
        envelope = valid_envelope(allowed_facts=facts, pack=pack)
        self.assertEqual(
            [fact["key"] for fact in envelope["allowed_facts"]],
            ["years_experience"],
        )
        from app.domain.policy import select_allowed_facts

        duplicate_selected = select_allowed_facts(
            [valid_years, dict(valid_years)], pack, now=NOW
        )
        self.assertEqual(duplicate_selected, [])
        self.assertEqual(
            [fact["key"] for fact in envelope["allowed_facts"]],
            ["years_experience"],
        )

    def test_constraints_types_and_fixed_values(self) -> None:
        """验证 constraints 的整数范围、固定 true/2048 与额外键拒绝。"""
        for constraints in (
            {"max_chars": True, "no_contact": True, "max_bytes": 2048},
            {"max_chars": 0, "no_contact": True, "max_bytes": 2048},
            {"max_chars": 301, "no_contact": True, "max_bytes": 2048},
            {"max_chars": 100, "no_contact": False, "max_bytes": 2048},
            {"max_chars": 100, "no_contact": 1, "max_bytes": 2048},
            {"max_chars": 100, "no_contact": True, "max_bytes": True},
            {"max_chars": 100, "no_contact": True, "max_bytes": 2049},
            {"max_chars": 100, "no_contact": True, "max_bytes": 2048, "x": 1},
        ):
            envelope = rehash(valid_envelope())
            envelope["constraints"] = constraints
            self.assertInvalid(envelope)

    def test_input_hash_recomputed_and_tampering_rejected(self) -> None:
        """验证修改 inbound 或直接替换 input_hash 都被摘要重算拒绝。"""
        envelope = dict(valid_envelope())
        envelope["inbound"] = str(envelope["inbound"]) + "!"
        self.assertInvalid(envelope)
        envelope = dict(valid_envelope())
        envelope["input_hash"] = "0" * 64
        self.assertInvalid(envelope)

    def test_envelope_byte_limit_boundary(self) -> None:
        """验证完整 envelope 规范 UTF-8 的 2048 字节边界与超限原因。"""
        def build(length: int) -> dict[str, object]:
            """按指定 inbound 长度构造并返回能通过限额校验的 envelope。"""
            return valid_envelope(inbound="a" * length)

        low, high = 0, 4096
        while low < high:
            middle = (low + high + 1) // 2
            try:
                envelope = build(middle)
            except DecisionError as error:
                self.assertEqual(error.reason_code, "input_too_large")
                high = middle - 1
            else:
                low = middle
        accepted = build(low)
        self.assertLessEqual(envelope_bytes(accepted), 2048)
        with self.assertRaises(DecisionError) as raised:
            build(low + 1)
        self.assertEqual(raised.exception.reason_code, "input_too_large")

    def test_duplicate_json_keys_nested_rejected_by_shared_parser(self) -> None:
        """验证共享 JSON 解析器拒绝顶层与嵌套重复键并映射 invalid_decision。"""
        with self.assertRaises(DecisionError):
            parse_decision('{"a":1,"a":1}')
        with self.assertRaises(DecisionError):
            parse_decision('{"a":{"b":1,"b":2}}')

    def test_parse_envelope_uses_shared_strict_json_boundary(self) -> None:
        """验证 envelope 解析拒绝围栏、数组、重复键与尾随内容并重算摘要。"""
        from app.domain.policy import parse_envelope

        envelope = valid_envelope()
        parsed = parse_envelope(json.dumps(envelope, ensure_ascii=False), now=NOW)
        self.assertEqual(parsed["input_hash"], envelope["input_hash"])
        for text in (
            "```json\n" + json.dumps(envelope) + "\n```",
            "[]",
            json.dumps(envelope) + " trailing",
            '{"schema_version":1,"schema_version":1}',
        ):
            with self.assertRaises(DecisionError):
                parse_envelope(text, now=NOW)

    def test_render_limit_rejects_oversize_decision_content(self) -> None:
        """验证 base/策略/用户数据投影合计超过 64 KiB 时拒绝渲染。"""
        from app.domain.policy import parse_and_validate_pack, render_request

        files = real_example_files()
        files["tone.md"] = "a" * 32000
        files["phrase-bank.md"] = "## interested.greeting\n- " + "a" * 32000
        pack, issues = parse_and_validate_pack(files)
        self.assertEqual(issues, [])
        assert pack is not None
        envelope = build_envelope(
            task_id="preview-task",
            nonce="00000000-0000-4000-8000-000000000201",
            kind="hr_message",
            subject={"conversation_key": "boss:conv:" + "0" * 64, "job_brief": None},
            inbound="您好。",
            context=[],
            allowed_facts=[],
            base_files=real_base_files(),
            pack=pack,
            now=NOW,
        )
        with self.assertRaises(DecisionError) as raised:
            render_request(real_base_files(), pack, envelope, now=NOW)
        self.assertEqual(raised.exception.reason_code, "input_too_large")

    def test_render_rejects_digest_mismatch(self) -> None:
        """验证 render_request 要求 envelope 摘要与本次加载 base/包一致。"""
        from app.domain.policy import render_request

        envelope = rehash(valid_envelope())
        envelope["base_hash"] = "0" * 64
        with self.assertRaises(DecisionError):
            render_request(real_base_files(), real_example_pack(), envelope, now=NOW)

    def test_render_rejects_non_matching_policy_tier_and_limits(self) -> None:
        """验证渲染前拒绝非固定 C 档、非有效 max_chars 或错身份。"""
        from app.domain.policy import render_request

        base = real_base_files()
        pack = real_example_pack()
        for field, value in (
            ("policy_tier", "A"),
            ("policy_pack_id", "other"),
            ("base_version", "2099-01-01"),
        ):
            envelope = rehash(valid_envelope())
            envelope[field] = value
            envelope = rehash(envelope)
            with self.assertRaises(DecisionError):
                render_request(base, pack, envelope, now=NOW)
        envelope = rehash(valid_envelope())
        envelope["constraints"] = {
            "max_chars": 299,
            "no_contact": True,
            "max_bytes": 2048,
        }
        envelope = rehash(envelope)
        with self.assertRaises(DecisionError):
            render_request(base, pack, envelope, now=NOW)


class DecisionValidationTests(unittest.TestCase):
    """严格 Decision 回显、词表、证据、租约有效期与 escalate 组合。"""

    def test_valid_decision_passes(self) -> None:
        """验证逐字回显、词表和证据都合法的 Decision 通过校验。"""
        envelope = valid_envelope()
        decision = valid_decision(envelope)
        validate_decision(
            decision, envelope, now=NOW, lease_expires_at=lease_expiry()
        )

    def test_missing_and_extra_keys_rejected(self) -> None:
        """验证 Decision 缺失任一必填键或增加额外键都被拒绝。"""
        envelope = valid_envelope()
        for key in DECISION_KEYS:
            decision = valid_decision(envelope)
            del decision[key]
            with self.assertRaises(DecisionError):
                validate_decision(decision, envelope, now=NOW, lease_expires_at=lease_expiry())
        decision = valid_decision(envelope)
        decision["extra"] = 1
        with self.assertRaises(DecisionError):
            validate_decision(decision, envelope, now=NOW, lease_expires_at=lease_expiry())

    def test_echo_mismatch_is_stale_decision(self) -> None:
        """验证 task_id/nonce/input_hash 不匹配统一记 stale_decision。"""
        envelope = valid_envelope()
        for field, value in (
            ("task_id", "other"),
            ("nonce", "00000000-0000-4000-8000-000000000299"),
            ("input_hash", "0" * 64),
        ):
            decision = valid_decision(envelope)
            decision[field] = value
            with self.assertRaises(DecisionError) as raised:
                validate_decision(decision, envelope, now=NOW, lease_expires_at=lease_expiry())
            self.assertEqual(raised.exception.reason_code, "stale_decision")

    def test_lease_expiry_is_stale_decision(self) -> None:
        """验证 now 达到或超过可信租约截止时间时拒绝 Decision。"""
        envelope = valid_envelope()
        decision = valid_decision(envelope)
        with self.assertRaises(DecisionError) as raised:
            validate_decision(
                decision, envelope, now=NOW, lease_expires_at=NOW
            )
        self.assertEqual(raised.exception.reason_code, "stale_decision")
        with self.assertRaises(DecisionError):
            validate_decision(decision, envelope, now=NOW, lease_expires_at="bad")

    def test_intent_and_sub_intents_strict(self) -> None:
        """验证 intent/子意图词表、去重与类型严格性。"""
        envelope = valid_envelope()
        for value in ("positive", 1, True):
            decision = valid_decision(envelope, intent=value)
            with self.assertRaises(DecisionError):
                validate_decision(decision, envelope, now=NOW, lease_expires_at=lease_expiry())
        for value in (
            ["unknown"],
            ["greeting", "greeting"],
            "greeting",
            [1],
        ):
            decision = valid_decision(envelope, sub_intents=value)
            with self.assertRaises(DecisionError):
                validate_decision(decision, envelope, now=NOW, lease_expires_at=lease_expiry())
        decision = valid_decision(envelope, sub_intents=[])
        validate_decision(decision, envelope, now=NOW, lease_expires_at=lease_expiry())

    def test_reply_length_control_and_unicode(self) -> None:
        """验证 reply 的 0–300 码点、仅 LF 与孤立代理拒绝。"""
        envelope = valid_envelope()
        validate_decision(
            valid_decision(envelope, reply=""),
            envelope,
            now=NOW,
            lease_expires_at=lease_expiry(),
        )
        validate_decision(
            valid_decision(envelope, reply="第一行\n第二行"),
            envelope,
            now=NOW,
            lease_expires_at=lease_expiry(),
        )
        for reply in ("a" * 301, "a\rb", "a\tb", "a\x01b", "\ud800"):
            with self.assertRaises(DecisionError):
                validate_decision(
                    valid_decision(envelope, reply=reply),
                    envelope,
                    now=NOW,
                    lease_expires_at=lease_expiry(),
                )

    def test_evidence_must_be_nonempty_inbound_substring(self) -> None:
        """验证 evidence 必须是非空 inbound 原样子串。"""
        envelope = valid_envelope()
        for evidence in ("", "不存在的原文", 1):
            with self.assertRaises(DecisionError) as raised:
                validate_decision(
                    valid_decision(envelope, evidence=evidence),
                    envelope,
                    now=NOW,
                    lease_expires_at=lease_expiry(),
                )
            self.assertEqual(raised.exception.reason_code, "stale_evidence")

    def test_escalate_rules(self) -> None:
        """验证 needs_human/uncertain 必须 human_review 且 escalate 词表固定。"""
        envelope = valid_envelope()
        for intent in ("needs_human", "uncertain"):
            with self.assertRaises(DecisionError):
                validate_decision(
                    valid_decision(envelope, intent=intent, escalate=None),
                    envelope,
                    now=NOW,
                    lease_expires_at=lease_expiry(),
                )
            validate_decision(
                valid_decision(envelope, intent=intent, escalate="human_review"),
                envelope,
                now=NOW,
                lease_expires_at=lease_expiry(),
            )
        with self.assertRaises(DecisionError):
            validate_decision(
                valid_decision(envelope, escalate="auto"),
                envelope,
                now=NOW,
                lease_expires_at=lease_expiry(),
            )

    def test_job_card_accepts_all_five_intents_with_same_schema(self) -> None:
        """验证职位卡复用同一 Decision schema 且五个 intent 均不缩词表。"""
        case = real_preview_case("policy-job-card")
        envelope = build_envelope(
            task_id=case["task_id"],
            nonce=case["nonce"],
            kind=case["kind"],
            subject=case["subject"],
            inbound=case["inbound"],
            context=case["context"],
            allowed_facts=case["allowed_facts"],
            base_files=real_base_files(),
            pack=real_example_pack(),
            now=case["now"],
        )
        for intent in ("rejected", "declined", "interested", "needs_human", "uncertain"):
            decision = valid_decision(
                envelope,
                intent=intent,
                escalate="human_review" if intent in {"needs_human", "uncertain"} else None,
            )
            validate_decision(
                decision, envelope, now=NOW, lease_expires_at=lease_expiry()
            )

    def test_parse_decision_rejects_fences_arrays_and_trailing(self) -> None:
        """验证 Decision 解析拒绝围栏、数组、尾随内容和重复键。"""
        for text in (
            "```json\n{}\n```",
            "[]",
            '{"a":1} trailing',
            '{"a":1}{"b":2}',
            "",
            "not json",
        ):
            with self.assertRaises(DecisionError):
                parse_decision(text)
        self.assertEqual(parse_decision('{"a":1}'), {"a": 1})


class ReplyValidationTests(unittest.TestCase):
    """程序追加披露并复验完整正文的码点、控制字符、禁止项与事实来源。"""

    def limits(self) -> dict[str, object]:
        """返回公开示例包的有效限制供回复复验使用。"""
        return effective_limits(real_example_pack())

    def complete(self, reply: str, **overrides: object) -> str:
        """按指定覆盖参数追加披露并返回完整回复，失败抛 ReplyError。"""
        limits = self.limits()
        params: dict[str, object] = {
            "disclosure": limits["disclosure"],
            "max_chars": limits["max_chars"],
            "never_say": limits["never_say"],
            "extra_topics": limits["extra_topics"],
            "allowed_facts": [],
        }
        params.update(overrides)
        return append_and_validate_reply(reply, **params)  # type: ignore[arg-type]

    def test_empty_reply_does_not_get_disclosure(self) -> None:
        """验证空候选正文不追加披露。"""
        self.assertEqual(self.complete(""), "")

    def test_nonempty_reply_appends_disclosure_once(self) -> None:
        """验证非空正文追加一次披露且已含完整披露时不重复。"""
        disclosure = str(self.limits()["disclosure"])
        result = self.complete("您好。")
        self.assertEqual(result, f"您好。\n{disclosure}")
        self.assertEqual(self.complete(result), result)

    def test_complete_length_is_enforced_after_disclosure(self) -> None:
        """验证披露后的完整文本仍受有效码点上限约束。"""
        disclosure = str(self.limits()["disclosure"])
        reply = "a" * (300 - len(disclosure) - 1)
        self.assertEqual(len(self.complete(reply)), 300)
        with self.assertRaises(ReplyError) as raised:
            self.complete(reply + "a")
        self.assertEqual(raised.exception.code, "range")

    def test_contact_patterns_are_blocked(self) -> None:
        """验证手机号、邮箱与微信类联系方式模式被阻断。"""
        for reply in (
            "联系 13800000000",
            "邮箱 test@example.com",
            "微信 wxid_test",
        ):
            with self.assertRaises(ReplyError) as raised:
                self.complete(reply)
            self.assertEqual(raised.exception.code, "sensitive")

    def test_never_say_and_extra_topics_are_literal_and_substring(self) -> None:
        """验证 never-say 与额外禁谈主题按字面/子串命中阻断。"""
        with self.assertRaises(ReplyError):
            self.complete("可以保证入职")
        with self.assertRaises(ReplyError):
            self.complete("聊聊股票", extra_topics=["股票"])
        self.complete("可以聊聊岗位")

    def test_fact_source_heuristics_require_allowed_fact(self) -> None:
        """验证经验/地点/薪资信号缺少已确认事实时转冲突。"""
        for reply in ("我有5年经验", "我目前在北京", "期望薪资 20000"):
            with self.assertRaises(ReplyError) as raised:
                self.complete(reply)
            self.assertEqual(raised.exception.code, "conflict")
        self.complete(
            "我有5年经验",
            allowed_facts=[
                {
                    "key": "years_experience",
                    "value": "5",
                    "source": "user_confirmed",
                    "expires_at": None,
                }
            ],
        )

    def test_recognized_fact_values_must_match_confirmed_values(self) -> None:
        """验证已识别事实形式与已确认值严格一致，保留小数/范围/单位/地点全文。"""
        years = [
            {
                "key": "years_experience",
                "value": "3",
                "source": "user_confirmed",
                "expires_at": None,
            }
        ]
        self.complete("我有3年经验", allowed_facts=years)
        with self.assertRaises(ReplyError) as raised:
            self.complete("我有99年经验", allowed_facts=years)
        self.assertEqual(raised.exception.code, "conflict")
        decimal_years = [
            {
                "key": "years_experience",
                "value": "3.5",
                "source": "user_confirmed",
                "expires_at": None,
            }
        ]
        self.complete("我有3.5年经验", allowed_facts=decimal_years)
        with self.assertRaises(ReplyError):
            self.complete("我有35年经验", allowed_facts=decimal_years)
        range_years = [
            {
                "key": "years_experience",
                "value": "3到5年",
                "source": "user_confirmed",
                "expires_at": None,
            }
        ]
        self.complete("我有3到5年经验", allowed_facts=range_years)
        with self.assertRaises(ReplyError):
            self.complete("我有35年经验", allowed_facts=range_years)
        location = [
            {
                "key": "work_location",
                "value": "北京",
                "source": "user_confirmed",
                "expires_at": None,
            }
        ]
        self.complete("我目前在北京", allowed_facts=location)
        with self.assertRaises(ReplyError):
            self.complete("我目前在上海", allowed_facts=location)
        with self.assertRaises(ReplyError):
            self.complete("我在北京以外", allowed_facts=location)
        salary = [
            {
                "key": "salary_expectation",
                "value": "30k",
                "source": "user_confirmed",
                "expires_at": None,
            }
        ]
        self.complete("期望月薪30k", allowed_facts=salary)
        with self.assertRaises(ReplyError):
            self.complete("期望月薪30万元", allowed_facts=salary)
        salary_yuan = [
            {
                "key": "salary_expectation",
                "value": "20000",
                "source": "user_confirmed",
                "expires_at": None,
            }
        ]
        self.complete("期望薪资 20000", allowed_facts=salary_yuan)
        with self.assertRaises(ReplyError):
            self.complete("期望薪资 30000", allowed_facts=salary_yuan)
        self.complete("我有3个问题想了解", allowed_facts=[])
        with self.assertRaises(ReplyError):
            self.complete("我有3亿年经验", allowed_facts=years)
        salary_unitless = [
            {
                "key": "salary_expectation",
                "value": "30",
                "source": "user_confirmed",
                "expires_at": None,
            }
        ]
        with self.assertRaises(ReplyError):
            self.complete("期望月薪30亿美元", allowed_facts=salary_unitless)
        with self.assertRaises(ReplyError):
            self.complete("我有3年半经验", allowed_facts=years)

    def test_control_and_lone_surrogate_rejected(self) -> None:
        """验证 CR、TAB、其它控制字符与孤立代理被拒绝。"""
        for reply in ("a\rb", "a\tb", "a\x01b", "\ud800"):
            with self.assertRaises(ReplyError):
                self.complete(reply)

    def test_code_points_not_utf16_units(self) -> None:
        """验证回复长度按 Unicode 码点而非 UTF-16 单元计算。"""
        disclosure = str(self.limits()["disclosure"])
        reply = "😀" * (300 - len(disclosure) - 1)
        result = self.complete(reply)
        self.assertEqual(len(result), 300)
        with self.assertRaises(ReplyError):
            self.complete(reply + "😀")


if __name__ == "__main__":
    unittest.main()
