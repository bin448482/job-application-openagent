"""验证默认与显式过程日志路径都由每次运行隔离，两个独立进程并发不丢行或互相覆盖。"""

from __future__ import annotations

import json
import multiprocessing
import tempfile
import unittest
import uuid
from pathlib import Path

from app.process_log import ProcessLog

ROOT = Path(__file__).resolve().parents[1]
COUNT = 40


def _write_concurrent_events(
    base_path: str | None,
    barrier: object,
    queue: object,
    count: int,
) -> None:
    """在独立进程中构造自己的日志器，等待同步后写入固定事件序列。"""
    log = ProcessLog(path=Path(base_path)) if base_path is not None else ProcessLog()
    barrier.wait(timeout=30)
    recorded = True
    for index in range(count):
        recorded = (
            log.record(
                stage="store",
                result="started" if index % 2 == 0 else "ok",
                duration_ms=index,
            )
            and recorded
        )
    queue.put(
        {
            "correlation_id": log.correlation_id,
            "path": str(log.path),
            "recorded": recorded,
        }
    )


def _read_events(path: Path) -> list[dict[str, object]]:
    """逐行解析一个运行日志，返回事件对象。"""
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _remove_owned_logs(paths: list[Path]) -> None:
    """只删除子进程明确返回的本次日志路径，不按目录差集推断文件所有权。"""
    for path in paths:
        path.unlink(missing_ok=True)


class ProcessLogConcurrencyTests(unittest.TestCase):
    """覆盖两个独立进程写默认/显式路径时的隔离与完整关联。"""

    def _run_pair(self, bases: tuple[str | None, str | None]) -> list[dict[str, object]]:
        """启动两个 spawn 进程并在 Barrier 后同时写日志，返回各自结果。"""
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        queue = context.Queue()
        processes = [
            context.Process(
                target=_write_concurrent_events,
                args=(base, barrier, queue, COUNT),
            )
            for base in bases
        ]
        for process in processes:
            process.start()
        results: list[dict[str, object]] = []
        try:
            for _ in processes:
                results.append(queue.get(timeout=30))
        finally:
            for process in processes:
                process.join(timeout=30)
            alive = [process for process in processes if process.is_alive()]
            for process in alive:
                process.terminate()
                process.join(timeout=5)
        self.assertFalse(alive, "concurrent log worker did not finish")
        self.assertEqual([process.exitcode for process in processes], [0, 0])
        return results

    def _assert_isolated(
        self,
        results: list[dict[str, object]],
        expected_parent: Path,
        expected_prefix: str,
    ) -> None:
        """断言两个运行各有完整文件、完整事件序列且没有跨运行混行。"""
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["recorded"] is True for result in results))
        paths = [Path(str(result["path"])) for result in results]
        self.assertEqual(len(set(paths)), 2)
        correlations = [str(result["correlation_id"]) for result in results]
        self.assertEqual(len(set(correlations)), 2)
        expected_sequence = [
            ("store", "started" if index % 2 == 0 else "ok", index)
            for index in range(COUNT)
        ]
        for result, path, correlation in zip(results, paths, correlations):
            with self.subTest(correlation=correlation):
                self.assertEqual(path.parent, expected_parent)
                self.assertTrue(path.name.startswith(expected_prefix))
                self.assertTrue(path.name.endswith(".jsonl"))
                events = _read_events(path)
                self.assertEqual(len(events), COUNT)
                self.assertTrue(
                    all(event["correlation_id"] == correlation for event in events)
                )
                self.assertTrue(all(event["schema_version"] == 1 for event in events))
                self.assertEqual(
                    [
                        (event["stage"], event["result"], event["duration_ms"])
                        for event in events
                    ],
                    expected_sequence,
                )
        observed = [
            event["correlation_id"]
            for path in paths
            for event in _read_events(path)
        ]
        self.assertEqual(len(observed), COUNT * 2)
        self.assertEqual(sorted(observed), sorted(correlations * COUNT))
        self.assertFalse(any(path.name == "m0-process.jsonl" for path in paths))

    def test_default_path_runs_are_isolated(self) -> None:
        """默认生产目录两并发运行各写独立 UUID 文件，清理只删明确返回路径。"""
        logs_dir = ROOT / "data" / "logs"
        results = self._run_pair((None, None))
        self._assert_isolated(results, logs_dir, "m0-process-")
        paths = [Path(str(result["path"])) for result in results]
        sentinel = logs_dir / f"m0-process-{uuid.uuid4()}.jsonl"
        sentinel.write_text("foreign-sentinel\n", encoding="utf-8")
        self.addCleanup(sentinel.unlink, missing_ok=True)
        _remove_owned_logs(paths)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "foreign-sentinel\n")

    def test_explicit_path_runs_are_isolated(self) -> None:
        """显式基础路径也只作为命名基础，两个并发运行仍写独立文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            base = directory / "m0-process.jsonl"
            results = self._run_pair((str(base), str(base)))
            self._assert_isolated(results, directory, "m0-process-")
            self.assertFalse(base.exists())
