"""验证 M4a 策略包 TOML/Markdown schema、45 组合、硬下限、摘要与渲染。"""

from __future__ import annotations

import unittest

from app.domain.policy import (
    BASE_VERSION,
    POLICY_KEYS,
    SUB_INTENTS,
    effective_limits,
    parse_and_validate_pack,
    resolve_policy_tier,
    render_request,
    validate_preview_case,
)
from tests.policy_helpers import (
    NOW,
    real_base_files,
    real_example_files,
    real_example_pack,
    real_preview_case,
    valid_envelope,
)

DOC_TIERS = '''[tiers]
A = ["interested.greeting", "interested.job_question"]
B = ["interested.resume_request", "interested.interview", "interested.salary", "interested.jd_request"]
C = ["rejected.*", "declined.*", "needs_human.*", "uncertain.*", "interested.sensitive", "interested.non_text", "interested.ambiguous"]'''

DEFAULT_TIERS = '''[tiers]
A = []
B = []
C = ["interested.*", "rejected.*", "declined.*", "needs_human.*", "uncertain.*"]'''


class PolicyPackTests(unittest.TestCase):
    """覆盖 TOML 字段、词表、45 键覆盖、硬下限与 Markdown 语法。"""

    def files(self) -> dict[str, str]:
        """返回公开示例四载荷文件的新副本供变异测试使用。"""
        return real_example_files()

    def parse(self, files: dict[str, str]):
        """调用领域包校验并返回解析包与诊断列表。"""
        return parse_and_validate_pack(files)

    def assertCode(
        self,
        files: dict[str, str],
        code: str,
        *,
        file: str | None = None,
        field: str | None = None,
    ) -> None:
        """断言包校验失败且诊断中包含指定文件/字段/代码。"""
        pack, issues = self.parse(files)
        self.assertIsNone(pack)
        matches = [
            issue
            for issue in issues
            if issue.code == code
            and (file is None or issue.file == file)
            and (field is None or issue.field == field)
        ]
        self.assertTrue(
            matches,
            msg=f"missing {code} {file} {field}: {[issue.to_dict() for issue in issues]}",
        )

    def replace(self, old: str, new: str) -> dict[str, str]:
        """在示例 pack.toml 中替换一次文本并返回新文件映射。"""
        files = self.files()
        text = files["pack.toml"]
        self.assertIn(old, text)
        files["pack.toml"] = text.replace(old, new, 1)
        return files

    def test_example_pack_is_valid_and_all_c(self) -> None:
        """验证公开示例全 C、空事实、有效限制与 45 键覆盖。"""
        pack, issues = self.parse(self.files())
        self.assertEqual(issues, [])
        assert pack is not None
        self.assertEqual(pack.profile_id, "default")
        self.assertEqual(set(pack.tier_by_key), set(POLICY_KEYS))
        self.assertEqual(set(pack.tier_by_key.values()), {"C"})
        limits = effective_limits(pack)
        self.assertEqual(limits["max_chars"], 300)
        self.assertEqual(limits["reply_cooldown_seconds"], 86400)
        self.assertEqual(limits["daily_sends"], 10)
        self.assertEqual(limits["fact_keys"], [])
        self.assertEqual(limits["disclosure"], "个人开发AI助手")

    def test_missing_top_level_and_nested_keys(self) -> None:
        """验证缺失各必填 TOML 键都产生 missing 诊断。"""
        lines = (
            "schema_version = 1",
            "profile_id = \"default\"",
            "locale = \"zh-CN\"",
            "apps = [\"boss\"]",
            "text = \"个人开发AI助手\"",
            "max_chars = 300",
            "reply_cooldown_seconds = 86400",
            "daily_sends = 10",
            "extra_topics = []",
            "tone = \"tone.md\"",
            "phrase_bank = \"phrase-bank.md\"",
            "never_say = \"never-say.md\"",
            "allow_keys = []",
            "missing = \"ask_user\"",
            "conflict = \"ask_user\"",
        )
        for line in lines:
            files = self.files()
            files["pack.toml"] = files["pack.toml"].replace(line + "\n", "", 1)
            self.assertCode(files, "missing")

    def test_extra_top_level_and_nested_keys(self) -> None:
        """验证顶层与子表额外键只定位父表并拒绝。"""
        files = self.files()
        files["pack.toml"] = "extra = 1\n" + files["pack.toml"]
        self.assertCode(files, "unknown_key", file="pack.toml")
        files = self.files()
        files["pack.toml"] += "extra = 1\n"
        self.assertCode(files, "unknown_key", field="facts")

    def test_schema_profile_locale_apps(self) -> None:
        """验证版本、profile_id 词法、locale 与 apps 的类型和词表。"""
        self.assertCode(self.replace("schema_version = 1", "schema_version = true"), "type")
        self.assertCode(self.replace("schema_version = 1", "schema_version = 2"), "vocabulary")
        for value in ("Default", "1abc", "a" * 33, "a b", "a.b", ""):
            self.assertCode(self.replace('profile_id = "default"', f'profile_id = "{value}"'), "vocabulary")
        self.assertCode(self.replace('locale = "zh-CN"', 'locale = "en-US"'), "vocabulary")
        self.assertCode(self.replace('apps = ["boss"]', "apps = []"), "range")
        self.assertCode(self.replace('apps = ["boss"]', 'apps = ["boss", "boss"]'), "duplicate")
        self.assertCode(self.replace('apps = ["boss"]', 'apps = ["other"]'), "vocabulary")
        self.assertCode(self.replace('apps = ["boss"]', 'apps = ["boss", 1]'), "type")

    def test_disclosure_rules(self) -> None:
        """验证披露文本长度、标记、单行与联系方式规则。"""
        self.assertCode(self.replace('text = "个人开发AI助手"', 'text = ""'), "range")
        self.assertCode(self.replace('text = "个人开发AI助手"', 'text = "个人开发"'), "vocabulary")
        self.assertCode(
            self.replace('text = "个人开发AI助手"', 'text = "个人开发AI助手\\n第二行"'),
            "encoding",
        )
        self.assertCode(
            self.replace('text = "个人开发AI助手"', 'text = "个人开发AI助手 13800000000"'),
            "sensitive",
        )
        self.assertCode(
            self.replace('text = "个人开发AI助手"', 'text = "' + "长" * 81 + 'AI助手"'),
            "range",
        )

    def test_limits_fields_and_disclosure_floor(self) -> None:
        """验证字数/冷却/每日整数范围及不得小于披露码点数。"""
        self.assertCode(self.replace("max_chars = 300", "max_chars = true"), "type")
        self.assertCode(self.replace("max_chars = 300", "max_chars = 0"), "range")
        self.assertCode(self.replace("max_chars = 300", "max_chars = 301"), "range")
        self.assertCode(self.replace("reply_cooldown_seconds = 86400", "reply_cooldown_seconds = 86399"), "range")
        self.assertCode(self.replace("reply_cooldown_seconds = 86400", "reply_cooldown_seconds = 604801"), "range")
        self.assertCode(self.replace("reply_cooldown_seconds = 86400", "reply_cooldown_seconds = true"), "type")
        self.assertCode(self.replace("daily_sends = 10", "daily_sends = 0"), "range")
        self.assertCode(self.replace("daily_sends = 10", "daily_sends = 11"), "range")
        self.assertCode(self.replace("daily_sends = 10", "daily_sends = true"), "type")
        self.assertCode(self.replace("max_chars = 300", "max_chars = 5"), "range", field="limits.max_chars")

    def test_tiers_vocabulary_and_coverage(self) -> None:
        """验证分档类型、通配限制、重复/冲突/漏项与 A 越权。"""
        self.assertCode(self.replace(DEFAULT_TIERS, DEFAULT_TIERS.replace("A = []", "A = 1")), "type")
        self.assertCode(self.replace(DEFAULT_TIERS, DEFAULT_TIERS.replace("C = [", 'C = ["*", ')), "vocabulary")
        self.assertCode(
            self.replace(DEFAULT_TIERS, DEFAULT_TIERS.replace("C = [", 'C = ["interested.job_question.*", ')),
            "vocabulary",
        )
        self.assertCode(
            self.replace(DEFAULT_TIERS, DEFAULT_TIERS.replace('C = ["interested.*", ', 'C = ["interested.*", "interested.greeting", ')),
            "duplicate",
        )
        self.assertCode(
            self.replace(DEFAULT_TIERS, DEFAULT_TIERS.replace("B = []", 'B = ["interested.greeting"]')),
            "conflict",
        )
        missing = DEFAULT_TIERS.replace('"uncertain.*"', '"uncertain.greeting"')
        self.assertCode(self.replace(DEFAULT_TIERS, missing), "coverage")
        overreach = (
            'A = ["interested.greeting", "interested.resume_request"]\n'
            "B = []\n"
            'C = ["rejected.*", "declined.*", "needs_human.*", "uncertain.*", '
            '"interested.job_question", "interested.interview", "interested.salary", '
            '"interested.jd_request", "interested.sensitive", "interested.non_text", '
            '"interested.ambiguous"]'
        )
        self.assertCode(self.replace(DEFAULT_TIERS, "[tiers]\n" + overreach), "safety_floor", field="tiers.A[1]")
        self.assertCode(self.replace(DEFAULT_TIERS, DEFAULT_TIERS.replace("A = []", 'A = ["*"]')), "vocabulary")

    def test_documented_complete_tier_replacement_is_valid(self) -> None:
        """验证策略包说明中的完整替换分档是有效 45 键覆盖。"""
        files = self.replace(DEFAULT_TIERS, DOC_TIERS)
        pack, issues = self.parse(files)
        self.assertEqual(issues, [])
        assert pack is not None
        self.assertEqual(pack.tier_by_key["interested.greeting"], "A")
        self.assertEqual(pack.tier_by_key["interested.resume_request"], "B")
        self.assertEqual(pack.tier_by_key["rejected.greeting"], "C")

    def test_forbidden_topics_and_facts(self) -> None:
        """验证额外禁谈主题与事实白名单的类型、去重和词表。"""
        self.assertCode(self.replace("extra_topics = []", "extra_topics = 1"), "type")
        self.assertCode(self.replace("extra_topics = []", 'extra_topics = [""]'), "range")
        self.assertCode(
            self.replace(
                "extra_topics = []",
                "extra_topics = [" + ",".join(['"主题"'] * 33) + "]",
            ),
            "range",
        )
        self.assertCode(self.replace("extra_topics = []", 'extra_topics = ["a", "a"]'), "duplicate")
        self.assertCode(self.replace("extra_topics = []", 'extra_topics = ["' + "长" * 41 + '"]'), "range")
        self.assertCode(self.replace("extra_topics = []", 'extra_topics = ["13800000000"]'), "sensitive")
        self.assertCode(self.replace("allow_keys = []", 'allow_keys = ["unknown"]'), "vocabulary")
        self.assertCode(self.replace("allow_keys = []", 'allow_keys = ["work_location", "work_location"]'), "duplicate")
        self.assertCode(self.replace("allow_keys = []", "allow_keys = 1"), "type")
        self.assertCode(self.replace('missing = "ask_user"', 'missing = "guess"'), "vocabulary")
        self.assertCode(self.replace('conflict = "ask_user"', 'conflict = "guess"'), "vocabulary")

    def test_style_paths(self) -> None:
        """验证三份 md 文件名固定且拒绝其它路径或链接。"""
        for old, new in (
            ('tone = "tone.md"', 'tone = "other.md"'),
            ('tone = "tone.md"', 'tone = "../tone.md"'),
            ('tone = "tone.md"', 'tone = "/abs/tone.md"'),
            ('phrase_bank = "phrase-bank.md"', 'phrase_bank = "phrase-bank"'),
            ('never_say = "never-say.md"', 'never_say = "https://example.com"'),
        ):
            self.assertCode(self.replace(old, new), "path")

    def test_markdown_tone_rules(self) -> None:
        """验证 tone 非空普通文本且拒绝控制字符与联系方式。"""
        for text in ("", "   \n", "联系 13800000000", "a\x01b"):
            files = self.files()
            files["tone.md"] = text
            self.assertCode(files, "range" if not text.strip() else "sensitive" if "138" in text else "encoding", file="tone.md")

    def test_phrase_bank_syntax(self) -> None:
        """验证 phrase-bank 标题/列表语法、重复段与重复短句。"""
        valid = "## interested.greeting\n- 您好。\n\n## uncertain.ambiguous\n- 请人工确认。\n"
        files = self.files()
        files["phrase-bank.md"] = valid
        pack, issues = self.parse(files)
        self.assertEqual(issues, [])
        assert pack is not None
        self.assertEqual(pack.phrases["interested.greeting"], ("您好。",))
        for text in (
            "# tone\n",
            "### interested.greeting\n- 您好。\n",
            "## unknown.key\n- 您好。\n",
            "- 您好。\n",
            "## interested.greeting\n普通正文\n",
        ):
            files = self.files()
            files["phrase-bank.md"] = text
            expected = "vocabulary" if "unknown.key" in text else "format"
            self.assertCode(files, expected, file="phrase-bank.md")
        files = self.files()
        files["phrase-bank.md"] = "## interested.greeting\n- \n"
        self.assertCode(files, "format", file="phrase-bank.md")
        files = self.files()
        files["phrase-bank.md"] = (
            "## interested.greeting\n- 您好。\n## interested.greeting\n- 再您好。\n"
        )
        self.assertCode(files, "duplicate", file="phrase-bank.md")
        files = self.files()
        files["phrase-bank.md"] = "## interested.greeting\n- 您好。\n- 您好。\n"
        self.assertCode(files, "duplicate", file="phrase-bank.md")

    def test_never_say_syntax_and_disclosure_conflict(self) -> None:
        """验证 never-say 行语法、重复项与披露字面冲突。"""
        files = self.files()
        files["never-say.md"] = ""
        self.assertEqual(self.parse(files)[1], [])
        for text in ("- \n", "- 保证入职\n- 保证入职\n", "保证入职\n"):
            files = self.files()
            files["never-say.md"] = text
            expected = "duplicate" if text.count("保证入职") == 2 else "format"
            self.assertCode(files, expected, file="never-say.md")
        files = self.files()
        files["never-say.md"] = "- 个人开发AI助手\n"
        self.assertCode(files, "conflict", file="never-say.md")

    def test_contact_patterns_in_each_payload(self) -> None:
        """验证四个载荷文件的联系方式模式都产生 sensitive 诊断。"""
        for name, text in (
            ("tone.md", "联系 13800000000"),
            ("phrase-bank.md", "## interested.greeting\n- 邮箱 test@example.com\n"),
            ("never-say.md", "- 微信 wxid_test\n"),
            ("pack.toml", "# test@example.com\n" + self.files()["pack.toml"]),
        ):
            files = self.files()
            files[name] = text
            self.assertCode(files, "sensitive", file=name)

    def test_toml_duplicate_and_located_diagnostics(self) -> None:
        """验证重复 TOML 键、A 越权行号与未知键不回显可疑键名。"""
        files = self.files()
        files["pack.toml"] = files["pack.toml"].replace(
            'profile_id = "default"', 'profile_id = "default"\nprofile_id = "default"', 1
        )
        self.assertCode(files, "duplicate", file="pack.toml")
        overreach = (
            'A = ["interested.greeting", "interested.resume_request"]\n'
            "B = []\n"
            'C = ["rejected.*", "declined.*", "needs_human.*", "uncertain.*", '
            '"interested.job_question", "interested.interview", "interested.salary", '
            '"interested.jd_request", "interested.sensitive", "interested.non_text", '
            '"interested.ambiguous"]'
        )
        files = self.replace(DEFAULT_TIERS, "[tiers]\n" + overreach)
        pack, issues = self.parse(files)
        self.assertIsNone(pack)
        matches = [
            issue
            for issue in issues
            if issue.code == "safety_floor" and issue.field == "tiers.A[1]"
        ]
        self.assertTrue(matches)
        self.assertIsInstance(matches[0].line, int)
        self.assertGreater(matches[0].line, 0)
        files = self.files()
        files["pack.toml"] += "supersecret = 1\n"
        pack, issues = self.parse(files)
        self.assertIsNone(pack)
        rendered = str([issue.to_dict() for issue in issues])
        self.assertIn("facts", rendered)
        self.assertNotIn("supersecret", rendered)

    def test_tier_resolution_and_hard_floor(self) -> None:
        """验证 45 键取最严档位、空子意图、escalate 与硬下限。"""
        files = self.replace(DEFAULT_TIERS, DOC_TIERS)
        pack, issues = self.parse(files)
        self.assertEqual(issues, [])
        assert pack is not None
        self.assertEqual(resolve_policy_tier(pack, "interested", ["greeting"], None, "A"), "A")
        self.assertEqual(resolve_policy_tier(pack, "interested", ["greeting"], None, "C"), "C")
        self.assertEqual(resolve_policy_tier(pack, "interested", ["resume_request"], None, "A"), "B")
        self.assertEqual(resolve_policy_tier(pack, "rejected", ["greeting"], None, "A"), "C")
        self.assertEqual(resolve_policy_tier(pack, "interested", [], None, "A"), "C")
        self.assertEqual(
            resolve_policy_tier(pack, "interested", ["greeting", "resume_request"], None, "A"),
            "B",
        )
        self.assertEqual(
            resolve_policy_tier(pack, "interested", ["greeting"], "human_review", "A"),
            "C",
        )
        with self.assertRaises(ValueError):
            resolve_policy_tier(pack, "unknown", ["greeting"], None, "A")
        with self.assertRaises(ValueError):
            resolve_policy_tier(pack, "interested", ["unknown"], None, "A")
        with self.assertRaises(ValueError):
            resolve_policy_tier(pack, "interested", ["greeting"], None, "D")


class RenderTests(unittest.TestCase):
    """验证固定 system/developer/user 分区、来源选择与无自引用摘要。"""

    def render(self, case_id: str = "policy-greeting"):
        """按指定 preview 案例构造 envelope 并返回固定三分区渲染结果。"""
        case = real_preview_case(case_id)
        base = real_base_files()
        pack = real_example_pack()
        envelope = valid_envelope(
            task_id=case["task_id"],
            nonce=case["nonce"],
            kind=case["kind"],
            subject=case["subject"],
            inbound=case["inbound"],
            context=case["context"],
            allowed_facts=case["allowed_facts"],
            base_files=base,
            pack=pack,
            now=case["now"],
        )
        return envelope, render_request(base, pack, envelope, now=str(case["now"]))

    def test_system_selects_one_base_contract_per_kind(self) -> None:
        """验证 HR/anomaly 与职位分别选择一份 base，两份都进摘要映射。"""
        base = real_base_files()
        for case_id, name in (
            ("policy-greeting", "intent-and-reply.md"),
            ("policy-anomaly", "intent-and-reply.md"),
            ("policy-job-card", "job-evaluate.md"),
        ):
            envelope, rendered = self.render(case_id)
            self.assertEqual(rendered.system_message, base[name])
            self.assertEqual(rendered.prompt_hash, envelope["prompt_hash"])
            self.assertEqual(set(rendered.base_files), {
                "prompts/base/intent-and-reply.md",
                "prompts/base/job-evaluate.md",
            })
            self.assertEqual(set(rendered.pack_files), set(real_example_files()))

    def test_developer_contains_policy_values_and_not_inbound(self) -> None:
        """验证 developer 包含策略措辞且不包含不可信 inbound。"""
        envelope, rendered = self.render()
        self.assertIn("个人开发AI助手", rendered.developer_message)
        self.assertIn("简洁、礼貌", rendered.developer_message)
        self.assertIn("保证入职", rendered.developer_message)
        self.assertIn("interested.greeting", rendered.developer_message)
        self.assertNotIn(str(envelope["inbound"]), rendered.developer_message)
        self.assertNotIn(str(envelope["inbound"]), rendered.system_message)

    def test_user_message_is_data_projection_json_only(self) -> None:
        """验证不可信原文只进入 user JSON 投影，不进入可信分区。"""
        envelope, rendered = self.render("policy-injection")
        self.assertIn("<system>", rendered.user_message)
        self.assertNotIn("<system>", rendered.developer_message)
        self.assertNotIn("<system>", rendered.system_message)
        from app.domain.policy import envelope_projection

        self.assertEqual(rendered.data, envelope_projection(envelope))
        self.assertNotIn("task_id", rendered.user_message)

    def test_rendered_request_hash_matches_prompt_hash(self) -> None:
        """验证手算 RenderedRequest 摘要等于 envelope.prompt_hash。"""
        envelope, rendered = self.render()
        manual = {
            "base": {
                "version": BASE_VERSION,
                "hash": rendered.base_hash,
                "files": rendered.base_files,
            },
            "pack": {
                "hash": rendered.pack_hash,
                "config": rendered.pack_config,
                "files": rendered.pack_files,
            },
            "data": rendered.data,
        }
        from app.domain.models import canonical_hash

        self.assertEqual(canonical_hash(manual), envelope["prompt_hash"])
        self.assertEqual(rendered.protocol["task_id"], envelope["task_id"])
        self.assertEqual(rendered.protocol["nonce"], envelope["nonce"])

    def test_preview_case_schema(self) -> None:
        """验证四个 preview 案例正例与键/类型负例。"""
        for case_id in (
            "policy-greeting",
            "policy-job-card",
            "policy-anomaly",
            "policy-injection",
        ):
            validate_preview_case(real_preview_case(case_id))
        case = dict(real_preview_case("policy-greeting"))
        case["extra"] = 1
        with self.assertRaises(ValueError):
            validate_preview_case(case)
        for mutation in (
            {"case_id": "Bad ID"},
            {"now": "bad"},
            {"nonce": "bad"},
            {"kind": "other"},
            {"context": ["a", "b", "c"]},
            {"subject": {"conversation_key": None, "job_brief": None}},
        ):
            case = dict(real_preview_case("policy-greeting"))
            case.update(mutation)
            with self.assertRaises(ValueError):
                validate_preview_case(case)


if __name__ == "__main__":
    unittest.main()
