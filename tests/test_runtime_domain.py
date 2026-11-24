"""验证 M6 纯调度规则：安全点七条件、优先级、预算等号、游标身份恢复与抢占审计。"""

from __future__ import annotations

import unittest

from app.domain.models import canonical_hash
from app.domain.runtime import (
    DEFAULT_CANDIDATES,
    DEFAULT_DEVICE_CALLS,
    DEFAULT_PREEMPTIONS,
    DEFAULT_ROUND_SECONDS,
    BatchCursor,
    BudgetLimits,
    BudgetState,
    PreemptionObservation,
    SceneCandidates,
    Selection,
    SafePoint,
    TaskCandidate,
    build_stage_preempted,
    clock_drift_exceeded,
    detection_interval_ms,
    observe_unread,
    select_task,
)

CONV_A = "boss:conv:" + canonical_hash(["合成联系人甲", "合成公司甲", "合成职位甲"])
CONV_B = "boss:conv:" + canonical_hash(["合成联系人乙", "合成公司乙", "合成职位乙"])
JOB_A = "boss:job:" + canonical_hash(["合成职位甲"])
JOB_B = "boss:job:" + canonical_hash(["合成职位乙"])
JOB_C = "boss:job:" + canonical_hash(["合成职位丙"])
BATCH = "00000000-0000-4000-8000-000000000001"
NOW = "2026-11-25T10:00:00.000Z"


def safe_point(**overrides: object) -> SafePoint:
    """构造七项全部成立的安全点，允许逐项覆盖为假。"""
    values = {
        "list_returned": True,
        "input_empty": True,
        "confirmation_closed": True,
        "no_inflight_command": True,
        "no_pending_action": True,
        "action_state_committed": True,
        "checkpoint_committed": True,
    }
    values.update(overrides)
    return SafePoint(**values)  # type: ignore[arg-type]


def scene(
    *,
    unread: tuple[str, ...] = (),
    new_messages: tuple[str, ...] = (),
    jobs: tuple[str, ...] = (),
    cleanup: tuple[str, ...] = (),
    unread_unknown: bool = False,
    unread_signal_without_target: bool = False,
) -> SceneCandidates:
    """按身份数组构造一次候选刷新结果。"""
    return SceneCandidates(
        unread=tuple(TaskCandidate("unread", key) for key in unread),
        new_messages=tuple(TaskCandidate("new_message", key) for key in new_messages),
        jobs=tuple(TaskCandidate("job", key) for key in jobs),
        cleanup=tuple(TaskCandidate("cleanup", key) for key in cleanup),
        unread_unknown=unread_unknown,
        unread_signal_without_target=unread_signal_without_target,
    )


def choose(
    scene_value: SceneCandidates,
    *,
    switching: bool = False,
    preemptions_used: int = 0,
    limit: int = 5,
    verification_used: bool = False,
    verified_unknown: bool = False,
) -> Selection:
    """以固定默认参数调用唯一 select_task。"""
    return select_task(
        scene_value,
        switching_from_job=switching,
        preemptions_used=preemptions_used,
        preemption_limit=limit,
        unread_verification_used=verification_used,
        unread_verified_unknown=verified_unknown,
    )


class SafePointTests(unittest.TestCase):
    """安全点七条件必须逐项独立，不能合并 pending/unknown 与状态提交。"""

    def test_all_true_is_safe(self) -> None:
        """七项全部成立才允许切换任务。"""
        self.assertEqual(safe_point().failed_conditions(), ())
        self.assertTrue(safe_point().is_safe())

    def test_each_false_condition_blocks_independently(self) -> None:
        """逐个把条件置假都必须阻止安全点，且只报告该项。"""
        for name in (
            "list_returned",
            "input_empty",
            "confirmation_closed",
            "no_inflight_command",
            "no_pending_action",
            "action_state_committed",
            "checkpoint_committed",
        ):
            value = safe_point(**{name: False})
            self.assertFalse(value.is_safe(), name)
            self.assertEqual(value.failed_conditions(), (name,))

    def test_bool_is_not_integer(self) -> None:
        """安全点字段只接受真正布尔值。"""
        with self.assertRaises(ValueError):
            SafePoint(1, True, True, True, True, True, True)  # type: ignore[arg-type]


class BudgetTests(unittest.TestCase):
    """默认预算与等号停止语义必须精确。"""

    def test_defaults_match_developer_baseline(self) -> None:
        """固定开发基线为 900 秒/200 设备调用/20 候选/30 秒职位/5 次抢占。"""
        limits = BudgetLimits()
        self.assertEqual(
            (
                limits.round_seconds,
                limits.device_calls,
                limits.candidates,
                limits.job_seconds,
                limits.preemptions,
            ),
            (
                DEFAULT_ROUND_SECONDS,
                DEFAULT_DEVICE_CALLS,
                DEFAULT_CANDIDATES,
                30,
                DEFAULT_PREEMPTIONS,
            ),
        )

    def test_equality_stops_new_work_and_overrun_is_reported(self) -> None:
        """任一项等于零即停止新工作；在途核实可留下负数并单列超支。"""
        state = BudgetState.initial(BudgetLimits())
        self.assertTrue(state.can_start_work())
        state = state.spend_device_calls(DEFAULT_DEVICE_CALLS)
        self.assertFalse(state.can_start_work())
        self.assertEqual(state.exhausted_reason(), "device_calls")
        overrun = state.spend_device_calls(2)
        self.assertEqual(overrun.overrun(BudgetLimits()), ("device_calls",))

    def test_cursor_round_trip_preserves_remaining_budget(self) -> None:
        """游标 JSON 严格往返，不因重启回补预算。"""
        cursor = BatchCursor.initial(BATCH, BudgetLimits()).visit_job(JOB_A)
        restored = BatchCursor.from_json(cursor.to_json())
        self.assertEqual(restored, cursor)
        self.assertEqual(restored.next_job((JOB_A, JOB_B)), JOB_B)

    def test_cursor_rejects_negative_budget_and_extra_keys(self) -> None:
        """坏游标与缺失/额外键不被默认采纳。"""
        payload = BatchCursor.initial(BATCH, BudgetLimits()).to_json()
        payload["remaining_budget"]["seconds"] = -1  # type: ignore[index]
        with self.assertRaises(ValueError):
            BatchCursor.from_json(payload)
        payload = BatchCursor.initial(BATCH, BudgetLimits()).to_json()
        payload["index"] = 3
        with self.assertRaises(ValueError):
            BatchCursor.from_json(payload)


class CursorIdentityTests(unittest.TestCase):
    """游标按 dedupe_key 恢复；重排、插入与消失都不得重复点击。"""

    def test_reorder_does_not_repeat_visited(self) -> None:
        """已访问职位在重排后仍被跳过。"""
        cursor = BatchCursor.initial(BATCH, BudgetLimits()).visit_job(JOB_A)
        self.assertEqual(cursor.next_job((JOB_B, JOB_C, JOB_A)), JOB_B)

    def test_disappearance_skips_key(self) -> None:
        """最后职位消失时从当前顺序找下一条未访问职位。"""
        cursor = BatchCursor.initial(BATCH, BudgetLimits()).visit_job(JOB_A)
        self.assertEqual(cursor.next_job((JOB_C, JOB_A)), JOB_C)

    def test_insertion_before_last_is_not_lost(self) -> None:
        """重新排序后插入到已处理职位之前的候选仍会在后续被选中。"""
        cursor = BatchCursor.initial(BATCH, BudgetLimits()).visit_job(JOB_B)
        self.assertEqual(cursor.next_job((JOB_A, JOB_B)), JOB_A)

    def test_all_visited_returns_none(self) -> None:
        """全部职位访问后没有可重复任务。"""
        cursor = (
            BatchCursor.initial(BATCH, BudgetLimits())
            .visit_job(JOB_A)
            .visit_job(JOB_B)
        )
        self.assertIsNone(cursor.next_job((JOB_A, JOB_B)))

    def test_visit_twice_does_not_duplicate_or_spend_twice(self) -> None:
        """重复登记同一身份不重复占候选预算。"""
        cursor = BatchCursor.initial(BATCH, BudgetLimits()).visit_job(JOB_A)
        again = cursor.visit_job(JOB_A)
        self.assertEqual(again.visited_keys, (JOB_A,))
        self.assertEqual(
            again.budget.remaining_candidates, DEFAULT_CANDIDATES - 1
        )


class PriorityTests(unittest.TestCase):
    """每次取任务重新计算固定优先级。"""

    def test_unread_beats_new_message_job_and_cleanup(self) -> None:
        """可定位未读始终优先。"""
        result = choose(
            scene(
                unread=(CONV_A,),
                new_messages=(CONV_B,),
                jobs=(JOB_A,),
                cleanup=("synthetic-cleanup",),
            )
        )
        self.assertEqual(result.task, TaskCandidate("unread", CONV_A))

    def test_new_message_beats_job(self) -> None:
        """无未读时可读取的新消息优先于职位。"""
        result = choose(scene(new_messages=(CONV_A,), jobs=(JOB_A,)))
        self.assertEqual(result.task, TaskCandidate("new_message", CONV_A))

    def test_job_beats_cleanup(self) -> None:
        """职位优先于清理；清理必须有显式候选。"""
        result = choose(scene(jobs=(JOB_A,), cleanup=("synthetic-cleanup",)))
        self.assertEqual(result.task, TaskCandidate("job", JOB_A))

    def test_unknown_unread_requests_one_verification(self) -> None:
        """未知或计数无定位先允许一次定向核对。"""
        for values in (
            scene(unread_unknown=True),
            scene(unread_signal_without_target=True),
        ):
            result = choose(values)
            self.assertTrue(result.verify_unread)
            self.assertIsNone(result.pause_reason)

    def test_verification_still_unknown_pauses(self) -> None:
        """一次定向核对后仍未知/无定位则暂停，不把缺失当零。"""
        for scene_value in (
            scene(unread_unknown=True),
            scene(unread_signal_without_target=True),
        ):
            result = choose(scene_value, verification_used=True)
            self.assertEqual(result.pause_reason, "unread_unverified")
        result = choose(scene(unread_unknown=True), verified_unknown=True)
        self.assertEqual(result.pause_reason, "unread_unverified")

    def test_known_absent_with_signal_without_target_still_pauses(self) -> None:
        """有正信号但无定位目标不能进入职位。"""
        result = choose(
            scene(unread_signal_without_target=True, jobs=(JOB_A,))
        )
        self.assertTrue(result.verify_unread)


class PreemptionTests(unittest.TestCase):
    """抢占只在安全点后实际切换并开始处理时记录。"""

    def test_sixth_request_pauses(self) -> None:
        """达到上限（第 6 次请求）暂停，不回退职位/清理。"""
        result = choose(
            scene(
                unread=(CONV_A,),
                jobs=(JOB_A,),
                cleanup=("synthetic-cleanup",),
            ),
            switching=True,
            preemptions_used=5,
            limit=5,
        )
        self.assertEqual(result.pause_reason, "preemption_limit")
        self.assertIsNone(result.task)

    def test_fifth_request_is_allowed_equality(self) -> None:
        """已用 4 次时第 5 次仍允许，第六次才暂停。"""
        result = choose(
            scene(unread=(CONV_A,)), switching=True, preemptions_used=4, limit=5
        )
        self.assertEqual(result.task, TaskCandidate("unread", CONV_A))

    def test_first_observation_is_retained_and_wait_is_monotonic(self) -> None:
        """刷新保留首次观察，waited_ms 只来自单调时钟。"""
        first = observe_unread(None, wall_now=NOW, monotonic_ms=1000)
        refreshed = observe_unread(
            first,
            wall_now="2026-11-25T10:05:00.000Z",
            monotonic_ms=9000,
        )
        self.assertEqual(refreshed, first)
        event = build_stage_preempted(
            job_key=JOB_A,
            trigger="unread_count>0",
            first=refreshed,
            handling_started_at="2026-11-25T10:05:01.000Z",
            handling_started_monotonic_ms=10000,
        )
        self.assertEqual(event["event"], "stage_preempted")
        self.assertEqual(event["waited_ms"], 9000)
        self.assertEqual(event["first_observed_at"], NOW)

    def test_build_rejects_negative_wait(self) -> None:
        """单调时钟倒退不生成抢占事件。"""
        first = PreemptionObservation(NOW, 2000)
        with self.assertRaises(ValueError):
            build_stage_preempted(
                job_key=JOB_A,
                trigger="unread_count>0",
                first=first,
                handling_started_at=NOW,
                handling_started_monotonic_ms=1000,
            )

    def test_detection_interval_is_separate(self) -> None:
        """检测间隔独立计算，不与调度等待合并。"""
        self.assertEqual(detection_interval_ms(1000, 1250), 250)


class ClockTests(unittest.TestCase):
    """墙钟与单调时钟漂移超过 1 秒必须停止新动作。"""

    def test_within_tolerance_is_ok(self) -> None:
        """1 秒整不构成漂移。"""
        self.assertFalse(
            clock_drift_exceeded(
                wall_start_ms=0,
                wall_now_ms=1000,
                monotonic_start_ms=0,
                monotonic_now_ms=1000,
            )
        )

    def test_over_tolerance_and_backwards_are_drift(self) -> None:
        """超过 1 秒或时钟倒退都判漂移。"""
        self.assertTrue(
            clock_drift_exceeded(
                wall_start_ms=0,
                wall_now_ms=1002,
                monotonic_start_ms=0,
                monotonic_now_ms=2003,
            )
        )
        self.assertTrue(
            clock_drift_exceeded(
                wall_start_ms=1000,
                wall_now_ms=0,
                monotonic_start_ms=0,
                monotonic_now_ms=10,
            )
        )


if __name__ == "__main__":
    unittest.main()
