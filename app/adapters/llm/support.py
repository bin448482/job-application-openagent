"""实现 M4b A/B 子进程桥共用的参数数组、有界超时与严格协议解析。"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from typing import Mapping

from app.domain.json_input import load_json_object
from app.domain.models import canonical_hash, is_nonempty_text
from app.ports.decision import (
    BRIDGE_MAX_OUTPUT_BYTES,
    BRIDGE_PROTOCOLS,
    BRIDGE_ROLES,
    BRIDGE_TIMEOUT_SECONDS,
    BridgeDescriptor,
    BridgeError,
)

ERROR_CODE_MAP = {
    "auth": "bridge_auth",
    "quota": "bridge_quota",
    "disconnected": "bridge_disconnected",
    "unavailable": "bridge_unavailable",
    "timeout": "bridge_timeout",
}
ECHO_KEYS = ("task_id", "nonce", "input_hash")


class SubprocessBridge:
    """以固定 argv 数组、`shell=False` 和 20 秒超时调用一个角色分区子进程。"""

    provider_id = ""
    bridge_name = ""
    content_key = ""

    def __init__(self, argv: list[str], *, model_id: str) -> None:
        """校验显式参数数组与非空模型标签，并计算不含原值的配置摘要。"""
        if (
            not isinstance(argv, list)
            or not argv
            or any(not is_nonempty_text(item) or "\x00" in item for item in argv)
        ):
            raise ValueError("bridge argv is invalid")
        if not is_nonempty_text(model_id):
            raise ValueError("bridge model id is invalid")
        protocol = BRIDGE_PROTOCOLS.get(self.provider_id)
        if protocol is None or not self.bridge_name or not self.content_key:
            raise ValueError("bridge provider is invalid")
        self._argv = tuple(argv)
        self._model_id = model_id
        self._protocol = protocol
        self._timeout = float(BRIDGE_TIMEOUT_SECONDS)
        self._config_hash = canonical_hash(
            {
                "provider_id": self.provider_id,
                "model_id": model_id,
                "bridge": self.bridge_name,
                "protocol": protocol,
                "argv": list(self._argv),
            }
        )

    def _set_timeout_for_test(self, seconds: float) -> None:
        """仅供离线测试缩短总预算；生产默认固定 20 秒，CLI 不暴露该入口。"""
        if not isinstance(seconds, (int, float)) or not 0 < float(seconds) <= BRIDGE_TIMEOUT_SECONDS:
            raise ValueError("timeout is invalid")
        self._timeout = float(seconds)

    def descriptor(self) -> BridgeDescriptor:
        """返回报告使用的固定摘要；不暴露 argv、端点或凭据。"""
        return BridgeDescriptor(
            provider_id=self.provider_id,
            model_id=self._model_id,
            model_config_hash=self._config_hash,
            bridge=self.bridge_name,
            simulated=False,
        )

    def _request(
        self,
        system_message: str,
        developer_message: str,
        user_message: str,
        echo: Mapping[str, str],
    ) -> bytes:
        """构造固定三消息、独立回显、无工具无历史的规范请求字节。"""
        if not all(
            isinstance(item, str)
            for item in (system_message, developer_message, user_message)
        ):
            raise BridgeError("bridge_protocol", submitted=False)
        if not isinstance(echo, Mapping) or set(echo) != set(ECHO_KEYS):
            raise BridgeError("bridge_protocol", submitted=False)
        if any(not is_nonempty_text(echo[key]) for key in ECHO_KEYS):
            raise BridgeError("bridge_protocol", submitted=False)
        request = {
            "protocol": self._protocol,
            "messages": [
                {"role": "system", "content": system_message},
                {"role": "developer", "content": developer_message},
                {"role": "user", "content": user_message},
            ],
            "echo": {key: echo[key] for key in ECHO_KEYS},
            "tools": [],
            "history": [],
        }
        text = json.dumps(
            request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return (text + "\n").encode("utf-8")

    def _decode_stdout(self, stdout: bytes) -> str:
        """严格解码有界 stdout；坏编码或超限映射固定协议失败。"""
        if not stdout or len(stdout) > BRIDGE_MAX_OUTPUT_BYTES:
            raise BridgeError("bridge_protocol", submitted=True)
        try:
            return stdout.decode("utf-8")
        except UnicodeDecodeError:
            raise BridgeError("bridge_protocol", submitted=True) from None

    def _error_from_stdout(self, text: str) -> BridgeError:
        """只接受固定 error.code；进程已启动后的失败一律视为可能已提交。"""
        try:
            parsed = load_json_object(text.strip())
        except (ValueError, UnicodeError):
            raise BridgeError("bridge_protocol", submitted=True) from None
        if (
            not isinstance(parsed, dict)
            or set(parsed) != {"protocol", "error"}
            or parsed.get("protocol") != self._protocol
            or not isinstance(parsed.get("error"), dict)
            or set(parsed["error"]) != {"code"}
            or not isinstance(parsed["error"]["code"], str)
        ):
            raise BridgeError("bridge_protocol", submitted=True)
        reason = ERROR_CODE_MAP.get(parsed["error"]["code"])
        if reason is None:
            raise BridgeError("bridge_protocol", submitted=True)
        raise BridgeError(reason, submitted=True)

    def _content_from_stdout(self, text: str) -> str:
        """严格解析成功响应，检查协议、显式角色声明与内容字段。"""
        try:
            parsed = load_json_object(text.strip())
        except (ValueError, UnicodeError):
            raise BridgeError("bridge_protocol", submitted=True) from None
        expected = {"protocol", "roles", self.content_key}
        if not isinstance(parsed, dict) or set(parsed) != expected:
            raise BridgeError("bridge_protocol", submitted=True)
        if parsed["protocol"] != self._protocol:
            raise BridgeError("bridge_protocol", submitted=True)
        roles = parsed["roles"]
        if not isinstance(roles, list) or roles != list(BRIDGE_ROLES):
            raise BridgeError("bridge_roles", submitted=True)
        content = parsed[self.content_key]
        if not isinstance(content, str) or not content:
            raise BridgeError("bridge_protocol", submitted=True)
        return content

    def complete(
        self,
        *,
        system_message: str,
        developer_message: str,
        user_message: str,
        echo: Mapping[str, str],
    ) -> str:
        """在总预算内用本机临时文件承载 stdin/stdout；超时/超限/IO 失败受控回收。"""
        request = self._request(system_message, developer_message, user_message, echo)
        deadline = time.monotonic() + self._timeout
        request_file = None
        output = None
        process = None
        try:
            try:
                request_file = tempfile.TemporaryFile()
                output = tempfile.TemporaryFile()
            except OSError:
                raise BridgeError("bridge_protocol", submitted=False) from None
            if time.monotonic() >= deadline:
                raise BridgeError("bridge_timeout", submitted=False)
            try:
                request_file.write(request)
                request_file.seek(0)
            except OSError:
                raise BridgeError("bridge_start_failed", submitted=False) from None
            if time.monotonic() >= deadline:
                raise BridgeError("bridge_timeout", submitted=False)
            try:
                process = subprocess.Popen(
                    list(self._argv),
                    stdin=request_file,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                )
            except (FileNotFoundError, PermissionError):
                raise BridgeError("bridge_start_failed", submitted=False) from None
            except OSError:
                raise BridgeError("bridge_unavailable", submitted=True) from None
            except (ValueError, TypeError):
                raise BridgeError("bridge_start_failed", submitted=False) from None

            timed_out = False
            overflow = False
            io_error = False
            while True:
                try:
                    output.seek(0, os.SEEK_END)
                    if output.tell() > BRIDGE_MAX_OUTPUT_BYTES:
                        overflow = True
                        self._safe_kill(process)
                        break
                except (OSError, ValueError):
                    io_error = True
                    self._safe_kill(process)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    self._safe_kill(process)
                    break
                try:
                    process.wait(timeout=min(0.02, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
                except OSError:
                    io_error = True
                    self._safe_kill(process)
                    break
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                self._safe_kill(process)
                try:
                    process.wait(timeout=0.2)
                except (OSError, subprocess.TimeoutExpired):
                    io_error = True
            except OSError:
                io_error = True
                self._safe_kill(process)
            try:
                output.seek(0, os.SEEK_END)
                if output.tell() > BRIDGE_MAX_OUTPUT_BYTES:
                    overflow = True
            except (OSError, ValueError):
                io_error = True
            if timed_out:
                raise BridgeError("bridge_timeout", submitted=True)
            if io_error or overflow:
                raise BridgeError("bridge_protocol", submitted=True)
            try:
                output.seek(0)
                raw = output.read(BRIDGE_MAX_OUTPUT_BYTES + 1)
            except (OSError, ValueError):
                raise BridgeError("bridge_protocol", submitted=True) from None
            if len(raw) > BRIDGE_MAX_OUTPUT_BYTES:
                raise BridgeError("bridge_protocol", submitted=True)
            text = self._decode_stdout(raw)
            if process.returncode != 0:
                raise self._error_from_stdout(text)
            return self._content_from_stdout(text)
        finally:
            if process is not None:
                try:
                    poll = getattr(process, "poll", None)
                    if poll is None or poll() is None:
                        self._safe_kill(process)
                except (OSError, AttributeError):
                    pass
            for stream in (request_file, output):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass

    @staticmethod
    def _safe_kill(process: subprocess.Popen) -> None:
        """尽力终止直接子进程并忽略已退出/无权限等清理噪声。"""
        try:
            process.kill()
        except OSError:
            pass
