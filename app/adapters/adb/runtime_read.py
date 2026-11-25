"""把合成 M3 manifest 接到既有 ADB 采集与真实解析，并在实际进程创建边界计数。"""

from __future__ import annotations

import uuid
from pathlib import Path

from app.adapters.boss.read import (
    load_read_fixture,
    merge_conversations,
    parse_conversation_page,
    parse_job_detail,
    parse_job_list,
    parse_page,
    parse_unread,
)
from app.domain.models import ReadResult
from app.ports.runtime import RuntimeCommandContext
from app.ports.screen import ScreenPort


class AdbRuntimeRead:
    """唯一 RuntimeReadPort 组合适配器：真实 dump/cat/screencap + 真实 M3 解析。"""

    def __init__(
        self,
        project_root: Path,
        screen: ScreenPort,
        serial: str,
    ) -> None:
        """保存项目根、真实采集端口与内存 serial；构造不启动命令。"""
        self._root = Path(project_root).resolve()
        self._screen = screen
        self._serial = serial
        self._calls = 0
        self._context: RuntimeCommandContext | None = None

    def bind_command_context(self, context: object | None) -> None:
        """绑定/解绑逐命令门禁与续租上下文。"""
        self._context = context  # type: ignore[assignment]

    def _before(self) -> None:
        """命令启动前复核；拒绝时不得进入 Popen。"""
        if self._context is not None:
            self._context.before()

    def _on_spawn(self, pid: int) -> None:
        """唯一实际 Popen 成功边界：每条创建的命令计一次。"""
        del pid
        self._calls += 1

    def _on_done(self, pid: int, bounded: bool, proven: bool) -> None:
        """命令结束后按可信时钟续租；严格模式下错误向上传播。"""
        del pid, bounded, proven
        if self._context is not None:
            self._context.after(failed=False)

    def take_device_calls(self) -> int:
        """返回并清零实际创建的命令数。"""
        calls = self._calls
        self._calls = 0
        return calls

    def _resolve(self, relative: str) -> Path:
        """只解析项目内相对 manifest 路径。"""
        if not isinstance(relative, str) or not relative:
            raise ValueError("read fixture is invalid")
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("read fixture is invalid")
        path = (self._root / candidate).resolve()
        if self._root not in path.parents and path != self._root:
            raise ValueError("read fixture is invalid")
        if not path.is_file():
            raise ValueError("read fixture is invalid")
        return path

    def _capture_xml(self) -> str:
        """执行一次真实 dump+cat。"""
        capture = self._screen.dump_hierarchy(
            self._serial,
            before_command=self._before,
            on_spawn=self._on_spawn,
            on_done=self._on_done,
            strict_callbacks=True,
        )
        return capture.xml_text

    def read(self, relative: str) -> ReadResult:
        """实际采集原始 XML/截图并调用现有 M3 解析器。"""
        path = self._resolve(relative)
        fixture = load_read_fixture(path)
        snapshot_id = str(uuid.uuid4())
        observed_at = fixture.observed_at
        if fixture.kind == "conversation":
            pages = [
                parse_conversation_page(
                    self._capture_xml(),
                    fixture.profile,
                    fixture.conversation_context,
                    observed_at,
                    snapshot_id,
                    app_version=fixture.app_version,
                )
                for _ in fixture.xml_texts
            ]
            return merge_conversations(
                pages,
                fixture.profile,
                fixture.coverage_proof,
                observed_at,
                app_version=fixture.app_version,
            )
        xml_text = self._capture_xml()
        if fixture.kind == "page":
            return parse_page(
                xml_text,
                fixture.profile,
                observed_at,
                snapshot_id,
                app_version=fixture.app_version,
            )
        if fixture.kind == "job_list":
            return parse_job_list(
                xml_text,
                fixture.profile,
                observed_at,
                snapshot_id,
                app_version=fixture.app_version,
            )
        if fixture.kind == "job_detail":
            return parse_job_detail(
                xml_text,
                fixture.profile,
                observed_at,
                snapshot_id,
                app_version=fixture.app_version,
            )
        screen = self._screen.capture_screen(
            self._serial,
            before_command=self._before,
            on_spawn=self._on_spawn,
            on_done=self._on_done,
            strict_callbacks=True,
        )
        return parse_unread(
            xml_text,
            fixture.profile,
            fixture.calibration,
            screen.raw,
            observed_at,
            snapshot_id,
            fixture.coverage_proof,
            observed_at,
            app_version=fixture.app_version,
        )
