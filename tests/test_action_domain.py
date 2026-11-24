"""验证 M5 动作键、观察→三态证据、授权边界与人工卡纯领域判据。"""

from __future__ import annotations

import unittest
from dataclasses import replace

from app.domain.actions import (
    ActionAuthorization,
    ActionError,
    ActionObservation,
    ALLOWED_RESOLUTIONS,
    action_key_for,
    authorize,
    derive_action_outcome,
    human_card,
    is_action_key,
    validate_authorization,
)
from app.domain.models import canonical_hash
from tests import action_helpers as helpers

CONVERSATION = "boss:conv:" + canonical_hash(["合成联系人", "合成公司", "合成职位"])
MESSAGE = helpers.synthetic_text_key()
TASK = "boss:discovery:" + canonical_hash(["synthetic-task"])
TEXT_KEY = f"boss:{MESSAGE}:reply"


def observe(**overrides: object) -> ActionObservation:
    """构造文本发送观察，允许覆盖字段。"""
    data = helpers.observation(**overrides)
    return ActionObservation(
        action_key=TEXT_KEY,
        action_kind="send_text",
        **data,
    )


def authorization(**overrides: object) -> ActionAuthorization:
    """构造本轮仅授权文本的有效授权。"""
    digest = canonical_hash({"synthetic": "pack"})
    data: dict[str, object] = {
        "profile_id": "demo",
        "base_hash": digest,
        "pack_hash": digest,
        "acceptance_base_hash": digest,
        "acceptance_pack_hash": digest,
        "task_key": TASK,
        "action_key": TEXT_KEY,
        "authorized_kinds": ("send_text",),
        "expires_at": helpers.authorization_expiry(helpers.NOW),
        "confirmed": True,
        "nonce": helpers.BEFORE,
    }
    data.update(overrides)
    return ActionAuthorization(**data)  # type: ignore[arg-type]


class ActionKeyTests(unittest.TestCase):
    """动作键必须逐类符合 §5.5 规则。"""

    def test_five_document_keys(self) -> None:
        """文本使用 app:message_key:reply，其余使用 conversation:action。"""
        self.assertEqual(
            action_key_for("send_text", app="boss", message_key=MESSAGE),
            TEXT_KEY,
        )
        for kind, suffix in (
            ("send_resume", "resume"),
            ("change_wechat", "wechat"),
            ("change_phone", "phone"),
            ("delete_conversation", "delete"),
        ):
            key = action_key_for(kind, app="boss", conversation_key=CONVERSATION)
            self.assertEqual(key, f"{CONVERSATION}:{suffix}")
            self.assertTrue(is_action_key(kind, key))

    def test_unknown_or_cross_app_keys_are_rejected(self) -> None:
        """非摘要目标、跨应用或错误后缀都不能生成或通过校验。"""
        with self.assertRaises(ActionError):
            action_key_for("send_text", app="boss", message_key="not-a-key")
        with self.assertRaises(ActionError):
            action_key_for(
                "send_resume", app="other", conversation_key=CONVERSATION
            )
        self.assertFalse(is_action_key("send_text", CONVERSATION + ":reply"))
        self.assertFalse(is_action_key("delete_conversation", CONVERSATION + ":resume"))


class EvidenceDerivationTests(unittest.TestCase):
    """performed 只能由动作特有前后差分证明。"""

    def test_text_performed_requires_distinct_snapshots_and_new_echo(self) -> None:
        """同快照或没有新增回显时不得 performed。"""
        self.assertEqual(
            derive_action_outcome(observe()).evidence.outcome, "performed"
        )
        same = observe(after_snapshot_id=helpers.BEFORE)
        self.assertEqual(derive_action_outcome(same).evidence.outcome, "unknown")
        no_change = observe(echo_count_after=0)
        self.assertEqual(derive_action_outcome(no_change).evidence.outcome, "unknown")

    def test_click_before_disconnect_is_not_performed(self) -> None:
        """未点击时断连为 not_performed，证据 effect=none。"""
        result = derive_action_outcome(
            observe(clicked=False, error="device_disconnected")
        )
        self.assertEqual(result.evidence.outcome, "not_performed")
        self.assertEqual(result.evidence.effect, "none")
        self.assertEqual(result.evidence.reason_code, "cancelled_before_action")

    def test_click_after_disconnect_is_unknown(self) -> None:
        """点击后断连保持 unknown，不降级为未执行。"""
        result = derive_action_outcome(observe(error="device_disconnected"))
        self.assertEqual(result.evidence.outcome, "unknown")
        self.assertEqual(result.evidence.reason_code, "device_disconnected")

    def test_delete_requires_target_change_and_anchor(self) -> None:
        """目标早已不存在或有变化无关都不证明删除。"""
        key = f"{CONVERSATION}:delete"
        base = dict(helpers.observation())
        base.update(
            {
                "clicked": True,
                "identity_unique": True,
                "target_present_before": True,
                "target_present_after": False,
                "anchor_present_after": True,
            }
        )
        positive = ActionObservation(
            action_key=key, action_kind="delete_conversation", **base
        )
        self.assertEqual(
            derive_action_outcome(positive).evidence.effect, "deleted"
        )
        already = ActionObservation(
            action_key=key,
            action_kind="delete_conversation",
            **{**base, "target_present_before": False},
        )
        self.assertEqual(derive_action_outcome(already).evidence.outcome, "unknown")
        no_anchor = ActionObservation(
            action_key=key,
            action_kind="delete_conversation",
            **{**base, "anchor_present_after": False},
        )
        self.assertEqual(derive_action_outcome(no_anchor).evidence.outcome, "unknown")

    def test_resume_request_is_not_delivery(self) -> None:
        """发起简历请求只产生 request_sent，交付另由 delivery 字段证明。"""
        key = f"{CONVERSATION}:resume"
        data = dict(helpers.observation())
        data.update(
            {
                "clicked": True,
                "attachment_name_matched": True,
                "attachment_request_observed": True,
                "delivery_observed": False,
            }
        )
        request = ActionObservation(
            action_key=key, action_kind="send_resume", **data
        )
        result = derive_action_outcome(request)
        self.assertEqual(result.evidence.effect, "request_sent")
        self.assertFalse(result.delivered)
        delivered = ActionObservation(
            action_key=key,
            action_kind="send_resume",
            **{**data, "delivery_observed": True},
        )
        self.assertEqual(
            derive_action_outcome(delivered).evidence.effect, "sent"
        )
        self.assertTrue(derive_action_outcome(delivered).delivered)

    def test_contact_slot_and_confirmation_pending(self) -> None:
        """联系方式只在槽位请求中时 performed；确认框未确认保持 pending 语义。"""
        key = f"{CONVERSATION}:wechat"
        data = dict(helpers.observation())
        data.update(
            {
                "clicked": True,
                "contact_slot_available": True,
                "contact_slot_status": "requested",
                "ime_original": None,
                "ime_current": None,
            }
        )
        result = derive_action_outcome(
            ActionObservation(action_key=key, action_kind="change_wechat", **data)
        )
        self.assertEqual(result.evidence.effect, "request_sent")
        pending = derive_action_outcome(
            ActionObservation(
                action_key=key,
                action_kind="change_wechat",
                **{
                    **data,
                    "clicked": False,
                    "contact_slot_status": "none",
                    "confirmation_open": True,
                },
            )
        )
        self.assertEqual(pending.evidence.outcome, "not_performed")
        self.assertTrue(pending.pause)

    def test_not_performed_with_unsettled_scene_pauses(self) -> None:
        """输入框/IME/在途命令未收敛时不能仅凭 not_performed 结束。"""
        settled = derive_action_outcome(
            observe(clicked=False, error="device_disconnected")
        )
        self.assertFalse(settled.pause)
        for overrides in (
            {"scene_settled": False},
            {"ime_current": "other-ime"},
            {"command_quiet": False},
            {"confirmation_open": True},
            {"input_box_empty": False},
        ):
            result = derive_action_outcome(
                observe(clicked=False, error="device_disconnected", **overrides)
            )
            self.assertTrue(result.pause, overrides)

    def test_strict_observation_types(self) -> None:
        """bool 不能冒充计数，坏 UUID、坏错误词表与错误动作键拒绝。"""
        with self.assertRaises(ValueError):
            derive_action_outcome(observe(echo_count_after=True))
        with self.assertRaises(ValueError):
            derive_action_outcome(observe(after_snapshot_id="not-uuid"))
        with self.assertRaises(ValueError):
            derive_action_outcome(observe(error="unknown-error"))
        with self.assertRaises(ValueError):
            derive_action_outcome(
                ActionObservation(
                    action_key="boss:conv:bad",
                    action_kind="send_text",
                    **helpers.observation(),
                )
            )


class AuthorizationTests(unittest.TestCase):
    """分动作授权、摘要绑定、期限与本轮确认。"""

    def test_text_authorization_does_not_cover_other_actions(self) -> None:
        """文本授权不能覆盖附件、联系方式或删除。"""
        auth = authorization()
        with self.assertRaises(ActionError):
            authorize(
                auth,
                action_kind="send_resume",
                action_key=f"{CONVERSATION}:resume",
                task_key=TASK,
                profile_id="demo",
                base_hash=auth.base_hash,
                pack_hash=auth.pack_hash,
                now=helpers.NOW,
            )

    def test_authorization_expiry_equal_now_is_rejected(self) -> None:
        """授权截止等于 now 必须拒绝。"""
        auth = authorization(expires_at=helpers.NOW)
        with self.assertRaises(ActionError) as caught:
            authorize(
                auth,
                action_kind="send_text",
                action_key=TEXT_KEY,
                task_key=TASK,
                profile_id="demo",
                base_hash=auth.base_hash,
                pack_hash=auth.pack_hash,
                now=helpers.NOW,
            )
        self.assertEqual(caught.exception.reason_code, "authorization_expired")

    def test_pack_or_acceptance_drift_is_rejected(self) -> None:
        """当前包/base 或验收摘要不一致时不授权新动作。"""
        auth = authorization()
        with self.assertRaises(ActionError):
            authorize(
                auth,
                action_kind="send_text",
                action_key=TEXT_KEY,
                task_key=TASK,
                profile_id="demo",
                base_hash=canonical_hash({"other": "base"}),
                pack_hash=auth.pack_hash,
                now=helpers.NOW,
            )
        with self.assertRaises(ActionError):
            authorize(
                authorization(acceptance_pack_hash=canonical_hash({"old": "acceptance"})),
                action_kind="send_text",
                action_key=TEXT_KEY,
                task_key=TASK,
                profile_id="demo",
                base_hash=auth.base_hash,
                pack_hash=auth.pack_hash,
                now=helpers.NOW,
            )

    def test_authorization_requires_confirmation(self) -> None:
        """未本轮确认的授权不能执行动作。"""
        auth = authorization(confirmed=False)
        validate_authorization(auth)
        with self.assertRaises(ActionError):
            authorize(
                auth,
                action_kind="send_text",
                action_key=TEXT_KEY,
                task_key=TASK,
                profile_id="demo",
                base_hash=auth.base_hash,
                pack_hash=auth.pack_hash,
                now=helpers.NOW,
            )


class HumanCardTests(unittest.TestCase):
    """人工卡固定五项与去重快照。"""

    def test_card_shape_and_allowed_resolutions(self) -> None:
        """卡片只含固定键，快照去重，处置固定三项。"""
        record = {
            "task_key": TASK,
            "action_key": TEXT_KEY,
            "reason_code": "result_unverified",
            "before_snapshot_id": helpers.BEFORE,
            "after_snapshot_id": helpers.BEFORE,
        }
        card = human_card(record)
        self.assertEqual(
            set(card),
            {"task_id", "action_key", "reason_code", "snapshot_ids", "allowed_resolutions"},
        )
        self.assertEqual(card["snapshot_ids"], [helpers.BEFORE])
        self.assertEqual(tuple(card["allowed_resolutions"]), ALLOWED_RESOLUTIONS)

    def test_card_unknown_reason_falls_back_to_result_unverified(self) -> None:
        """非 unknown 词表原因不原样反射到人工卡。"""
        card = human_card(
            {
                "task_key": TASK,
                "action_key": TEXT_KEY,
                "reason_code": "something-else",
            }
        )
        self.assertEqual(card["reason_code"], "result_unverified")


if __name__ == "__main__":
    unittest.main()
