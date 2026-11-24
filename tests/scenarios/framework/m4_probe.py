"""在隔离根内调用真实 M4a/M4b 契约、文件仓库与决策用例，并输出受限摘要。

仅用于 CLI 无法直接表达的显式 now、合成包/决策变异、端口边界注入与桥载荷观察；
不复制业务判定，不读取 expected，不连接设备、模型或网络。输出只含固定状态与摘要，
不返回聊天正文、事实值、包全文或绝对路径。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

ROOT = Path.cwd().resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.files.evals import FileEvalStore
from app.adapters.files.policy import FilePolicyRepository
from app.adapters.llm import support as bridge_support
from app.adapters.llm.bridge_a import ExternalAgentBridge
from app.adapters.llm.bridge_b import LocalProcessBridge
from app.domain.models import add_seconds
from app.domain.policy import (
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
    parse_and_validate_pack,
    parse_decision,
    parse_envelope,
    render_request,
    resolve_policy_tier,
    validate_decision,
    verify_rendered_request,
)
from app.ports.decision import (
    BridgeDescriptor,
    BridgeError,
    DecisionBridgePort,
    DecisionSnapshotPort,
    DecisionLeasePort,
    DecisionClockPort,
)
from app.ports.policy import PolicyError, PolicyIssue
from app.usecases import policy as policy_usecases
from app.usecases import policy_eval as eval_usecases
from app.usecases.decision import DecisionRunError, decide


class ProbeError(RuntimeError):
    """表示 M4 探针请求或路径不满足受控协议。"""


def _within(child: Path, parent: Path) -> bool:
    """判断路径是否仍在父目录内。"""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_link_like(path: Path) -> bool:
    """识别 symlink 与 Windows junction。"""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def _reject_links_on_path(path: Path, root: Path, label: str) -> None:
    """先检查原始 leaf/祖先，再检查根祖先；不得先 resolve。"""
    current = path
    while True:
        if _is_link_like(current):
            raise ProbeError(f"{label} 路径含链接或 junction")
        if current == root or current.parent == current:
            break
        current = current.parent
    current = root
    while True:
        if _is_link_like(current):
            raise ProbeError(f"{label} 根的祖先含链接或 junction")
        if current.parent == current:
            break
        current = current.parent


def _relative(value: object, label: str, *, must_exist: bool) -> Path:
    """只接受隔离根内相对路径，先查链接再 resolve/containment。"""
    if not isinstance(value, str) or not value:
        raise ProbeError(f"{label} 必须是非空相对路径")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ProbeError(f"{label} 不得绝对或包含 ..")
    lexical = ROOT / candidate
    _reject_links_on_path(lexical, ROOT, label)
    resolved = lexical.resolve()
    if not _within(resolved, ROOT):
        raise ProbeError(f"{label} 越出隔离根")
    if must_exist and not resolved.is_file():
        raise ProbeError(f"{label} 不存在")
    return resolved


def _load_case(pack_rel: str, case_id: str) -> tuple[Any, dict[str, str], dict[str, str], Any, dict[str, Any]]:
    """经真实文件仓库加载活动包与固定 preview 案例，构建合法 envelope。"""
    repo = FilePolicyRepository(ROOT)
    _, base_files, files, _guides, pack = policy_usecases.load_active_pack(repo, pack_rel)
    case = repo.read_preview_case(case_id)
    envelope = build_envelope(
        task_id=str(case["task_id"]),
        nonce=str(case["nonce"]),
        kind=str(case["kind"]),
        subject=case["subject"],
        inbound=str(case["inbound"]),
        context=list(case["context"]),
        allowed_facts=list(case["allowed_facts"]),
        base_files=base_files,
        pack=pack,
        now=str(case["now"]),
    )
    return repo, base_files, files, pack, {"case": case, "envelope": envelope}


def _apply(value: Any, mutation: Mapping[str, Any]) -> Any:
    """按受控 delete/set/append 变异合成输入；只改测试输入，不执行产品逻辑。"""
    operation = mutation.get("op")
    path = mutation.get("path")
    if operation not in {"delete", "set", "append"} or not isinstance(path, list) or not path:
        raise ProbeError("变异形状非法")
    current = value
    for part in path[:-1]:
        if isinstance(current, list):
            if type(part) is not int or part < 0 or part >= len(current):
                raise ProbeError("变异路径越界")
            current = current[part]
        elif isinstance(current, dict):
            if part not in current:
                raise ProbeError("变异路径不存在")
            current = current[part]
        else:
            raise ProbeError("变异路径不可遍历")
    last = path[-1]
    if isinstance(current, list):
        if type(last) is not int or last < 0 or last > len(current):
            raise ProbeError("变异索引越界")
        if operation == "delete":
            if last >= len(current):
                raise ProbeError("变异索引不存在")
            del current[last]
        elif operation == "append":
            current.append(mutation.get("value"))
        elif last < len(current):
            current[last] = mutation.get("value")
        else:
            raise ProbeError("变异索引不存在")
    elif isinstance(current, dict):
        if not isinstance(last, str):
            raise ProbeError("变异键非法")
        if operation == "delete":
            current.pop(last, None)
        else:
            current[last] = mutation.get("value")
    else:
        raise ProbeError("变异目标不可写")
    return value


def _valid_decision(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """构造与 envelope 逐字回显的合成 Decision；只在测试输入侧使用。"""
    return {
        "schema_version": 1,
        "task_id": envelope["task_id"],
        "nonce": envelope["nonce"],
        "input_hash": envelope["input_hash"],
        "intent": "interested",
        "sub_intents": ["greeting"],
        "reply": "您好，感谢联系。",
        "evidence": str(envelope["inbound"])[:2],
        "escalate": None,
    }


def _serialize(value: Any, mode: object) -> str:
    """按请求生成单对象、围栏或数组文本，用于解析负例。"""
    text = json.dumps(value, ensure_ascii=False)
    if mode in (None, "object"):
        return text
    if mode == "fenced":
        return "```json\n" + text + "\n```"
    if mode == "array":
        return json.dumps([value], ensure_ascii=False)
    raise ProbeError("serialization 非法")


def _decision_operation(request: Mapping[str, Any]) -> dict[str, Any]:
    """用真实 parse/render/validate 执行合法或变异 envelope/Decision。"""
    pack_rel = str(request.get("pack"))
    case_id = str(request.get("case_id", "policy-greeting"))
    _repo, base_files, _files, pack, loaded = _load_case(pack_rel, case_id)
    case = loaded["case"]
    now = str(request.get("now") or case["now"])
    try:
        envelope = loaded["envelope"]
        inbound_length = request.get("inbound_length")
        if inbound_length is not None:
            if type(inbound_length) is not int:
                raise ProbeError("inbound_length 必须是整数")
            envelope = build_envelope(
                task_id=str(case["task_id"]),
                nonce=str(case["nonce"]),
                kind=str(case["kind"]),
                subject=case["subject"],
                inbound="x" * inbound_length,
                context=list(case["context"]),
                allowed_facts=list(case["allowed_facts"]),
                base_files=base_files,
                pack=pack,
                now=now,
            )
        envelope_mutation = request.get("envelope_mutation")
        if envelope_mutation:
            envelope = _apply(envelope, envelope_mutation)
        envelope_text = _serialize(envelope, request.get("serialization"))
        parsed_envelope = parse_envelope(envelope_text, now=now)
        rendered = render_request(base_files, pack, parsed_envelope, now=now)
        decision = _valid_decision(parsed_envelope)
        for key, value in (request.get("decision_overrides") or {}).items():
            if isinstance(key, str):
                decision[key] = value
        decision_mutation = request.get("decision_mutation")
        if decision_mutation:
            decision = _apply(decision, decision_mutation)
        lease_offset = request.get("lease_offset", 60)
        if type(lease_offset) is not int:
            raise ProbeError("lease_offset 必须是整数")
        parsed = parse_decision(_serialize(decision, request.get("decision_serialization")))
        validate_decision(
            parsed,
            parsed_envelope,
            now=now,
            lease_expires_at=add_seconds(now, lease_offset),
        )
        return {
            "status": "ok",
            "reason_code": None,
            "envelope_bytes": envelope_bytes(parsed_envelope),
            "rendered_bytes": len(rendered.user_message.encode("utf-8")),
            "prompt_hash_matches": rendered.prompt_hash
            == compute_prompt_hash(base_files, pack, parsed_envelope),
            "input_hash_matches": rendered.input_hash
            == compute_input_hash(parsed_envelope),
            "bridge_calls": 0,
        }
    except DecisionError as error:
        return {"status": "error", "reason_code": error.reason_code, "bridge_calls": 0}


def _hash_operation(request: Mapping[str, Any]) -> dict[str, Any]:
    """用真实摘要函数验证非自引用、投影排除与目录无关性。"""
    pack_rel = str(request.get("pack"))
    case_id = str(request.get("case_id", "policy-greeting"))
    _repo, base_files, files, pack, loaded = _load_case(pack_rel, case_id)
    envelope = loaded["envelope"]
    case = loaded["case"]
    base_hash = compute_base_hash(base_files)
    pack_hash = compute_pack_hash(files)
    prompt_hash = compute_prompt_hash(base_files, pack, envelope)
    input_hash = compute_input_hash(envelope)
    nonce_envelope = build_envelope(
        task_id=str(case["task_id"]),
        nonce="00000000-0000-4000-8000-000000000099",
        kind=str(case["kind"]),
        subject=case["subject"],
        inbound=str(case["inbound"]),
        context=list(case["context"]),
        allowed_facts=list(case["allowed_facts"]),
        base_files=base_files,
        pack=pack,
        now=str(case["now"]),
    )
    nonce_prompt = compute_prompt_hash(base_files, pack, nonce_envelope)
    nonce_input = compute_input_hash(nonce_envelope)
    second_pack_hash = compute_pack_hash(dict(files))
    rendered = render_request(base_files, pack, envelope, now=str(case["now"]))
    tampered = replace(rendered, data={**rendered.data, "inbound": "合成篡改"})
    tamper_reason = None
    try:
        verify_rendered_request(
            tampered, base_files, pack, envelope, now=str(case["now"])
        )
    except DecisionError as error:
        tamper_reason = error.reason_code
    return {
        "status": "ok",
        "base_hash": base_hash,
        "pack_hash": pack_hash,
        "prompt_hash": prompt_hash,
        "input_hash": input_hash,
        "base_hash_independent": base_hash == compute_base_hash(base_files),
        "pack_hash_independent": pack_hash == second_pack_hash,
        "prompt_excludes_nonce": prompt_hash == nonce_prompt,
        "input_includes_nonce": input_hash != nonce_input,
        "prompt_hash_non_self": prompt_hash not in (base_hash, pack_hash, input_hash),
        "tamper_reason": tamper_reason,
        "bridge_calls": 0,
    }


def _hash_mutations_operation(request: Mapping[str, Any]) -> dict[str, Any]:
    """用真实文件与摘要比较两个目录、注释和 base 换行的字节语义。"""
    repo = FilePolicyRepository(ROOT)
    _normalized, _base, files, _guides, pack = policy_usecases.load_active_pack(
        repo, "prompts/local/valid"
    )
    renamed = ROOT / "prompts/local/renamed"
    if renamed.exists():
        shutil.rmtree(renamed)
    shutil.copytree(ROOT / "prompts/local/valid", renamed)
    _n2, _b2, files2, _g2, pack2 = policy_usecases.load_active_pack(
        repo, "prompts/local/renamed"
    )
    same_dirs = compute_pack_hash(files) == compute_pack_hash(files2)
    (renamed / "pack.toml").write_text(
        files2["pack.toml"] + "# synthetic comment\n", encoding="utf-8"
    )
    _n3, _b3, files3, _g3, pack3 = policy_usecases.load_active_pack(
        repo, "prompts/local/renamed"
    )
    comment_changed = compute_pack_hash(files3) != compute_pack_hash(files2)
    base_path = ROOT / "prompts/base/intent-and-reply.md"
    before_base = repo.read_base()
    base_path.write_bytes(base_path.read_bytes() + b"\n")
    after_base = repo.read_base()
    base_newline_changed = compute_base_hash(before_base) != compute_base_hash(after_base)
    return {
        "status": "ok",
        "pack_hash_same_between_dirs": same_dirs,
        "pack_comment_changed": comment_changed,
        "base_newline_changed": base_newline_changed,
        "bridge_calls": 0,
    }


def _envelope_size_operation(request: Mapping[str, Any]) -> dict[str, Any]:
    """用同一公开 build_envelope 入口精确尝试 2048 和 2049 字节。"""
    pack_rel = str(request.get("pack"))
    _repo, base_files, _files, pack, loaded = _load_case(pack_rel, "policy-greeting")
    case = loaded["case"]
    ok_envelope = None
    ok_length = None
    for length in range(1, 2600):
        try:
            candidate = build_envelope(
                task_id=str(case["task_id"]),
                nonce=str(case["nonce"]),
                kind=str(case["kind"]),
                subject=case["subject"],
                inbound="x" * length,
                context=list(case["context"]),
                allowed_facts=list(case["allowed_facts"]),
                base_files=base_files,
                pack=pack,
                now=str(case["now"]),
            )
        except DecisionError as error:
            if error.reason_code == "input_too_large":
                break
            raise
        if envelope_bytes(candidate) == 2048:
            ok_envelope = candidate
            ok_length = length
            break
    if ok_envelope is None or ok_length is None:
        return {"status": "error", "reason_code": "size_not_found"}
    over_length = ok_length + 1
    try:
        over = build_envelope(
            task_id=str(case["task_id"]),
            nonce=str(case["nonce"]),
            kind=str(case["kind"]),
            subject=case["subject"],
            inbound="x" * over_length,
            context=list(case["context"]),
            allowed_facts=list(case["allowed_facts"]),
            base_files=base_files,
            pack=pack,
            now=str(case["now"]),
        )
    except DecisionError as error:
        over_accepted = False
        over_bytes = envelope_bytes(
            {**ok_envelope, "inbound": str(ok_envelope["inbound"]) + "x"}
        )
        reason = error.reason_code
    else:
        over_accepted = True
        over_bytes = envelope_bytes(over)
        reason = None
    return {
        "status": "ok",
        "envelope_2048_bytes": envelope_bytes(ok_envelope),
        "over_attempted_bytes": over_bytes,
        "over_accepted": over_accepted,
        "over_reason": reason,
        "bridge_calls": 0,
    }


def _facts_operation(request: Mapping[str, Any]) -> dict[str, Any]:
    """用真实事实交集、档位与回复复验函数验证纯契约子场景。"""
    pack_rel = str(request.get("pack"))
    case_id = str(request.get("case_id", "policy-greeting"))
    _repo, base_files, _files, pack, loaded = _load_case(pack_rel, case_id)
    case = loaded["case"]
    facts = request.get("facts", case["allowed_facts"])
    now = str(request.get("now") or case["now"])
    envelope = build_envelope(
        task_id=str(case["task_id"]),
        nonce=str(case["nonce"]),
        kind=str(case["kind"]),
        subject=case["subject"],
        inbound=str(request.get("inbound") or case["inbound"]),
        context=list(case["context"]),
        allowed_facts=list(facts),
        base_files=base_files,
        pack=pack,
        now=now,
    )
    limits = effective_limits(pack)
    tier = resolve_policy_tier(
        pack,
        str(request.get("intent", "interested")),
        list(request.get("sub_intents", ["greeting"])),
        request.get("escalate"),
        "C",
    )
    reply_reason = None
    if request.get("reply") is not None:
        try:
            append_and_validate_reply(
                str(request["reply"]),
                disclosure=str(limits["disclosure"]),
                max_chars=int(limits["max_chars"]),
                never_say=limits["never_say"],
                extra_topics=limits["extra_topics"],
                allowed_facts=envelope["allowed_facts"],
            )
        except ReplyError as error:
            reply_reason = error.code
    uncertain_reason = None
    if request.get("uncertain"):
        decision = _valid_decision(envelope)
        decision.update({"intent": "uncertain", "sub_intents": ["ambiguous"], "escalate": None})
        try:
            validate_decision(
                decision,
                envelope,
                now=now,
                lease_expires_at=add_seconds(now, 60),
            )
        except DecisionError as error:
            uncertain_reason = error.reason_code
    return {
        "status": "ok",
        "selected_fact_keys": [fact["key"] for fact in envelope["allowed_facts"]],
        "selected_fact_count": len(envelope["allowed_facts"]),
        "tier": tier,
        "missing_policy": limits.get("missing"),
        "reply_reason": reply_reason,
        "uncertain_reason": uncertain_reason,
        "bridge_calls": 0,
    }


def _render_operation(request: Mapping[str, Any]) -> dict[str, Any]:
    """渲染固定三分区并观察角色/事实/注入，不调用真实桥。"""
    pack_rel = str(request.get("pack"))
    case_id = str(request.get("case_id", "policy-greeting"))
    _repo, base_files, _files, pack, loaded = _load_case(pack_rel, case_id)
    case = loaded["case"]
    envelope = loaded["envelope"]
    if request.get("inbound") is not None:
        try:
            envelope = build_envelope(
                task_id=str(case["task_id"]),
                nonce=str(case["nonce"]),
                kind=str(case["kind"]),
                subject=case["subject"],
                inbound=str(request["inbound"]),
                context=list(case["context"]),
                allowed_facts=list(case["allowed_facts"]),
                base_files=base_files,
                pack=pack,
                now=str(case["now"]),
            )
        except DecisionError as error:
            return {"status": "error", "reason_code": error.reason_code}
    mutation = request.get("subject_mutation") or request.get("envelope_mutation")
    if mutation:
        try:
            envelope = _apply(envelope, mutation)
            parse_envelope(json.dumps(envelope, ensure_ascii=False), now=str(case["now"]))
        except DecisionError as error:
            return {"status": "error", "reason_code": error.reason_code}
    rendered = render_request(base_files, pack, envelope, now=str(case["now"]))
    user_data = json.loads(rendered.user_message)
    base_name = "job-evaluate.md" if envelope["kind"] == "job_card" else "intent-and-reply.md"
    injection = str(request.get("injection") or "")
    return {
        "status": "ok",
        "roles": 3,
        "system_is_base": rendered.system_message == base_files[base_name],
        "developer_has_disclosure": str(effective_limits(pack)["disclosure"])
        in rendered.developer_message,
        "user_is_json": isinstance(user_data, dict),
        "user_has_inbound": user_data.get("inbound") == envelope["inbound"],
        "inbound_only_user": bool(injection)
        and injection in rendered.user_message
        and injection not in rendered.system_message
        and injection not in rendered.developer_message,
        "policy_tier": user_data.get("policy_tier"),
        "fact_count": len(user_data.get("allowed_facts") or []),
        "tools": [],
        "history": [],
        "bridge_calls": 0,
    }


class _SequenceClock(DecisionClockPort):
    """按调用顺序返回预置 UTC 时间，模拟桥前/重试/桥后的可信时钟。"""

    def __init__(self, start: str) -> None:
        """保存起点并初始化调用计数。"""
        self._current = start
        self.calls = 0

    def now(self) -> str:
        """返回当前合成时间并在后续调用推进一秒。"""
        value = self._current
        self.calls += 1
        self._current = add_seconds(value, 1)
        return value


class _Snapshot(DecisionSnapshotPort):
    """返回固定 base/包原文，可注入第 N 次读取变化。"""

    def __init__(self, base_files: dict[str, str], files: dict[str, str], change_at: int | None = None) -> None:
        """保存原文与可选变化调用序号。"""
        self._base = base_files
        self._files = files
        self._change_at = change_at
        self.calls = 0

    def reload(self) -> tuple[dict[str, str], dict[str, str]]:
        """按调用次数返回原文或注入变化后的载荷。"""
        self.calls += 1
        if self._change_at is not None and self.calls >= self._change_at:
            return self._base, {**self._files, "tone.md": self._files["tone.md"] + "\n"}
        return self._base, self._files


class _Lease(DecisionLeasePort):
    """返回固定合成租约截止时间。"""

    def __init__(self, expiry: str) -> None:
        """保存截止时间。"""
        self._expiry = expiry

    def current_expiry(self) -> str:
        """返回截止时间。"""
        return self._expiry


class _FakeBridge(DecisionBridgePort):
    """记录调用并按规定序列返回合法 Decision 或传输错误。"""

    def __init__(self, envelope: Mapping[str, Any], modes: list[str]) -> None:
        """保存决策模板与逐次模式。"""
        self._envelope = envelope
        self._modes = modes
        self.calls = 0
        self.simulated = True

    def descriptor(self) -> BridgeDescriptor:
        """返回模拟桥摘要。"""
        return BridgeDescriptor(
            provider_id="local-process",
            model_id="synthetic-model",
            model_config_hash="0" * 64,
            bridge="bridge_b",
            simulated=True,
        )

    def complete(self, *, system_message: str, developer_message: str, user_message: str, echo: Mapping[str, str]) -> str:
        """按当前模式返回固定 Decision 文本或 BridgeError。"""
        del system_message, developer_message, user_message, echo
        mode = self._modes[min(self.calls, len(self._modes) - 1)]
        self.calls += 1
        if mode == "start_failed":
            raise BridgeError("bridge_start_failed", submitted=False)
        if mode == "timeout_after_submit":
            raise BridgeError("bridge_timeout", submitted=True)
        if mode == "bridge_auth":
            raise BridgeError("bridge_auth", submitted=True)
        return json.dumps(_valid_decision(self._envelope), ensure_ascii=False)


class _CapturingPopen:
    """在传输边界替代真实进程：记录请求字节并写入固定响应。"""

    last_request = b""
    protocol = "m4b-local-v1"
    response_key = "content"
    bad_roles = False

    def __init__(self, argv, stdin=None, stdout=None, stderr=None, shell=None) -> None:
        """读取 stdin、保存请求并按固定协议写入 stdout。"""
        del argv, stderr, shell
        type(self).last_request = stdin.read() if stdin is not None else b""
        response = {
            "protocol": type(self).protocol,
            "roles": ["system", "user"] if type(self).bad_roles else ["system", "developer", "user"],
        }
        response[type(self).response_key] = json.dumps(
            _valid_decision(
                {
                    "task_id": "x",
                    "nonce": "00000000-0000-4000-8000-000000000001",
                    "input_hash": "0" * 64,
                    "inbound": "合成",
                }
            ),
            ensure_ascii=False,
        )
        stdout.write(json.dumps(response, ensure_ascii=False).encode("utf-8"))
        stdout.flush()
        self.returncode = 0

    def wait(self, timeout=None):
        """立即返回成功退出码。"""
        del timeout
        return 0

    def poll(self):
        """报告进程已退出。"""
        return 0

    def kill(self) -> None:
        """无真实进程可终止。"""


def _bridge_operation(request: Mapping[str, Any]) -> dict[str, Any]:
    """经真实 LocalProcessBridge 捕获三分区载荷并运行一次 decide。"""
    pack_rel = str(request.get("pack"))
    case_id = str(request.get("case_id", "policy-greeting"))
    _repo, base_files, files, pack, loaded = _load_case(pack_rel, case_id)
    case = loaded["case"]
    envelope = loaded["envelope"]
    if request.get("inbound") is not None:
        envelope = build_envelope(
            task_id=str(case["task_id"]),
            nonce=str(case["nonce"]),
            kind=str(case["kind"]),
            subject=case["subject"],
            inbound=str(request["inbound"]),
            context=list(case["context"]),
            allowed_facts=list(case["allowed_facts"]),
            base_files=base_files,
            pack=pack,
            now=str(case["now"]),
        )
    rendered = render_request(base_files, pack, envelope, now=str(case["now"]))
    provider = str(request.get("provider", "local-process"))
    if provider == "external-agent":
        _CapturingPopen.protocol = "m4b-agent-v1"
        _CapturingPopen.response_key = "output"
        bridge_class = ExternalAgentBridge
    else:
        _CapturingPopen.protocol = "m4b-local-v1"
        _CapturingPopen.response_key = "content"
        bridge_class = LocalProcessBridge
    _CapturingPopen.bad_roles = bool(request.get("bad_roles"))
    original_popen = bridge_support.subprocess.Popen
    bridge_support.subprocess.Popen = _CapturingPopen
    try:
        bridge = bridge_class(["synthetic-bridge"], model_id="synthetic-model")
        try:
            text = bridge.complete(
                system_message=rendered.system_message,
                developer_message=rendered.developer_message,
                user_message=rendered.user_message,
                echo=rendered.protocol,
            )
        except BridgeError as error:
            return {"status": "error", "reason_code": error.reason_code}
        captured = json.loads(_CapturingPopen.last_request.decode("utf-8"))
        parsed_content = json.loads(text)
        clock = _SequenceClock(str(case["now"]))
        snapshot = _Snapshot(base_files, files)
        lease = _Lease(add_seconds(str(case["now"]), 60))
        run = decide(
            rendered=rendered,
            envelope=envelope,
            base_files=base_files,
            pack=pack,
            bridge=_FakeBridge(envelope, ["ok"]),
            snapshot=snapshot,
            lease=lease,
            clock=clock,
        )
    finally:
        bridge_support.subprocess.Popen = original_popen
    return {
        "status": "ok",
        "roles": [item.get("role") for item in captured.get("messages", [])],
        "tools": captured.get("tools"),
        "history": captured.get("history"),
        "echo_keys": sorted(captured.get("echo", {})),
        "user_is_json": isinstance(json.loads(captured["messages"][2]["content"]), dict),
        "user_equals_rendered": captured["messages"][2]["content"] == rendered.user_message,
        "system_is_base": captured["messages"][0]["content"] == rendered.system_message,
        "inbound_only_user": str(envelope["inbound"]) in captured["messages"][2]["content"]
        and str(envelope["inbound"]) not in captured["messages"][0]["content"]
        and str(envelope["inbound"]) not in captured["messages"][1]["content"],
        "response_content_object": isinstance(parsed_content, dict),
        "resolved_tier": run["resolved_tier"],
        "bridge_attempts": run["audit"]["bridge_attempts"],
        "bridge_submissions": run["audit"]["bridge_submissions"],
        "fact_count": len(envelope["allowed_facts"]),
    }


def _decision_run_operation(request: Mapping[str, Any]) -> dict[str, Any]:
    """用真实 decide 与边界端口执行正常、启动失败重试、提交后超时与包变化。"""
    pack_rel = str(request.get("pack"))
    case_id = str(request.get("case_id", "policy-greeting"))
    _repo, base_files, files, pack, loaded = _load_case(pack_rel, case_id)
    case = loaded["case"]
    envelope = loaded["envelope"]
    rendered = render_request(base_files, pack, envelope, now=str(case["now"]))
    modes = list(request.get("bridge_modes") or ["ok"])
    change_at = request.get("snapshot_change_at")
    clock = _SequenceClock(str(case["now"]))
    snapshot = _Snapshot(base_files, files, change_at if type(change_at) is int else None)
    lease = _Lease(add_seconds(str(case["now"]), int(request.get("lease_offset", 60))))
    bridge = _FakeBridge(envelope, modes)
    try:
        run = decide(
            rendered=rendered,
            envelope=envelope,
            base_files=base_files,
            pack=pack,
            bridge=bridge,
            snapshot=snapshot,
            lease=lease,
            clock=clock,
        )
    except DecisionRunError as error:
        return {
            "status": "error",
            "reason_code": error.reason_code,
            "submitted": error.submitted,
            "attempts": error.attempts,
            "submissions": error.submissions,
            "bridge_calls": bridge.calls,
            "snapshot_calls": snapshot.calls,
            "clock_calls": clock.calls,
        }
    return {
        "status": "ok",
        "reason_code": None,
        "submitted": True,
        "attempts": run["audit"]["bridge_attempts"],
        "submissions": run["audit"]["bridge_submissions"],
        "bridge_calls": bridge.calls,
        "snapshot_calls": snapshot.calls,
        "clock_calls": clock.calls,
        "resolved_tier": run["resolved_tier"],
    }


def _policy_validate_operation(request: Mapping[str, Any]) -> dict[str, Any]:
    """用真实 policy 用例读取/校验指定 local 包；链接在读取前拒绝。"""
    repo = FilePolicyRepository(ROOT)
    pack_rel = str(request.get("pack"))
    candidate = ROOT / pack_rel if isinstance(pack_rel, str) else ROOT
    link_detected = _is_link_like(candidate) or any(
        _is_link_like(parent) for parent in candidate.parents if _within(parent, ROOT)
    )
    target_reads = 0
    if request.get("watch_target"):
        target_root = (ROOT / "prompts/local/valid").resolve()
        original_open = Path.open

        def counted_open(self, *args, **kwargs):
            """在真实文件打开边界统计指向合成链接目标的读取。"""
            nonlocal target_reads
            try:
                resolved = self.resolve()
            except OSError:
                resolved = None
            if resolved is not None and _within(resolved, target_root):
                target_reads += 1
            return original_open(self, *args, **kwargs)

        Path.open = counted_open
        try:
            result = policy_usecases.validate_pack(repo, pack_rel)
        except Exception as error:  # PolicyError 是公开受控异常，不泄露原文。
            reason = getattr(error, "reason_code", None)
            issues = getattr(error, "issues", None) or []
            return {
                "status": "error",
                "reason_code": reason,
                "issue_count": len(issues),
                "issue_codes": sorted({str(getattr(item, "code", "")) for item in issues}),
                "link_detected": link_detected,
                "target_reads": target_reads,
            }
        finally:
            Path.open = original_open
        return {
            "status": "ok",
            "reason_code": None,
            "link_detected": link_detected,
            "target_reads": target_reads,
            "profile_id": result.get("profile_id"),
            "pack_hash_length": len(str(result.get("pack_hash", ""))),
            "base_hash_length": len(str(result.get("base_hash", ""))),
            "tier_keys": sum(len(value) for value in (result.get("tiers") or {}).values()),
        }
    try:
        result = policy_usecases.validate_pack(repo, pack_rel)
    except Exception as error:  # PolicyError 是公开受控异常，不泄露原文。
        reason = getattr(error, "reason_code", None)
        issues = getattr(error, "issues", None) or []
        return {
            "status": "error",
            "reason_code": reason,
            "issue_count": len(issues),
            "issue_codes": sorted({str(getattr(item, "code", "")) for item in issues}),
            "link_detected": link_detected,
        }
    return {
        "status": "ok",
        "reason_code": None,
        "link_detected": link_detected,
        "profile_id": result.get("profile_id"),
        "pack_hash_length": len(str(result.get("pack_hash", ""))),
        "base_hash_length": len(str(result.get("base_hash", ""))),
        "tier_keys": sum(len(value) for value in (result.get("tiers") or {}).values()),
    }


def _mapping_operation(request: Mapping[str, Any]) -> dict[str, Any]:
    """逐行核对旧 24 映射，并用真实渲染消费所有派生公开输入。"""
    mapping = json.loads((ROOT / "evals/policy-suite/mapping.json").read_text(encoding="utf-8"))
    suite = json.loads((ROOT / "evals/policy-suite/public.json").read_text(encoding="utf-8"))
    rows = mapping.get("rows")
    cases = suite.get("cases")
    if not isinstance(rows, list) or not isinstance(cases, list):
        raise ProbeError("映射或公开套件形状非法")
    case_by_id = {
        case["input"]["case_id"]: case
        for case in cases
        if isinstance(case, dict) and isinstance(case.get("input"), dict)
    }
    repo = FilePolicyRepository(ROOT)
    _, base_files, _files, _guides, pack = policy_usecases.load_active_pack(
        repo, str(request.get("pack"))
    )
    rendered = 0
    missing_derived: list[str] = []
    unmapped: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("original_id"), str):
            raise ProbeError("映射行形状非法")
        derived = row.get("derived_ids")
        if not isinstance(derived, list):
            raise ProbeError("映射派生列非法")
        if not derived:
            unmapped.append(row["original_id"])
            if not row.get("not_integrated_reason"):
                raise ProbeError("不能映射行缺少原因")
            continue
        for derived_id in derived:
            case = case_by_id.get(derived_id)
            if case is None:
                missing_derived.append(str(derived_id))
                continue
            case_input = case["input"]
            envelope = build_envelope(
                task_id=str(case_input["task_id"]),
                nonce=str(case_input["nonce"]),
                kind=str(case_input["kind"]),
                subject=case_input["subject"],
                inbound=str(case_input["inbound"]),
                context=list(case_input["context"]),
                allowed_facts=list(case_input["allowed_facts"]),
                base_files=base_files,
                pack=pack,
                now=str(case_input["now"]),
            )
            render_request(base_files, pack, envelope, now=str(case_input["now"]))
            rendered += 1
    return {
        "status": "ok",
        "row_count": len(rows),
        "original_ids": [row.get("original_id") for row in rows],
        "unmapped_count": len(unmapped),
        "unmapped_ids": unmapped,
        "derived_render_count": rendered,
        "missing_derived": missing_derived,
        "all_rows_owned": all(
            isinstance(row.get("owner"), str) and bool(row.get("owner")) for row in rows
        ),
        "no_expected_fields": all(
            not ({"expected", "answer", "can_send"} & set(row)) for row in rows
        ),
        "bridge_calls": 0,
    }


class _RestoreActivity:
    """按请求返回明确合成停止/活动/未知状态，不连接真实活动检查。"""

    def __init__(self, state: str) -> None:
        """保存合成状态。"""
        self._state = state

    def assess_restore(self, *, profile_id: str, dest_pack: str) -> str:
        """返回请求的合成状态，供真实 restore_pack 边界判定。"""
        del profile_id, dest_pack
        return self._state


def _restore_operation(request: Mapping[str, Any]) -> dict[str, Any]:
    """真实 snapshot/restore 事务；activity 只作为明确边界注入。"""
    repo = FilePolicyRepository(ROOT)
    now = str(request.get("now", "2026-11-24T00:00:00.000Z"))
    snapshot = policy_usecases.snapshot_pack(
        repo, str(request.get("pack", "prompts/local/valid")), now=now
    )
    dest = str(request.get("dest", "prompts/local/restored"))
    mode = str(request.get("mode", "stopped"))
    if mode == "target_exists":
        (ROOT / dest).mkdir(parents=True, exist_ok=True)
    try:
        result = policy_usecases.restore_pack(
            repo,
            str(snapshot["snapshot_path"]),
            dest,
            _RestoreActivity("stopped" if mode == "stopped" else mode),
            now=now,
        )
    except Exception as error:
        return {
            "status": "error",
            "reason_code": getattr(error, "reason_code", None),
            "activity_state": getattr(error, "activity_state", None),
            "snapshot_created": bool(snapshot.get("pack_hash")),
        }
    restored_files = repo.read_pack(dest)
    return {
        "status": "ok",
        "reason_code": None,
        "snapshot_created": True,
        "pack_hash_match": compute_pack_hash(restored_files)
        == snapshot.get("pack_hash"),
        "base_hash_match": result.get("base_version") is not None,
    }


class _FailingEvalStore(FileEvalStore):
    """只在指定真实文件写入边界注入失败，读与其他写仍委托生产适配器。"""

    def __init__(self, root: Path, boundary: str) -> None:
        """保存项目根与失败边界名。"""
        super().__init__(root)
        self._boundary = boundary

    def update_holdout_registry(self, mutate):
        """registry 边界失败时抛出固定 policy_io_error。"""
        if self._boundary == "registry":
            raise PolicyError("policy_io_error", [PolicyIssue(None, None, None, "path")])
        return super().update_holdout_registry(mutate)

    def write_regression_admission(self, batch_id, suite):
        """准入副本边界失败时抛出固定 policy_io_error。"""
        if self._boundary == "admission":
            raise PolicyError("policy_io_error", [PolicyIssue(None, None, None, "path")])
        return super().write_regression_admission(batch_id, suite)


def _registry_snapshot(store: FileEvalStore) -> str:
    """返回当前真实 registry 的规范化摘要，用于比较失败前后未变。"""
    registry = store.read_holdout_registry()
    return hashlib.sha256(
        json.dumps(registry, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _transport_marker_count(bridge: _EvalBridge, markers: list[str]) -> int:
    """检查所有实际发送角色与 echo 中是否出现合成 expected/metadata 标记。"""
    total = 0
    for payload in bridge.role_payloads:
        texts = [
            str(payload.get("system", "")),
            str(payload.get("developer", "")),
            str(payload.get("user", "")),
        ]
        echo = payload.get("echo")
        if isinstance(echo, dict):
            texts.extend(str(value) for value in echo.values())
        for text in texts:
            total += sum(text.count(marker) for marker in markers)
    return total


def _eval_operation(request: Mapping[str, Any]) -> dict[str, Any]:
    """用真实 eval_pack/留出事务与模拟桥运行合成套件，不写真实凭证。"""
    repo = FilePolicyRepository(ROOT)
    store = FileEvalStore(ROOT)
    pack_rel = str(request.get("pack"))
    suite = str(request.get("suite", "public"))
    mode = str(request.get("mode", "eval"))
    if mode == "eval":
        if request.get("edit_tone"):
            tone = ROOT / "prompts/local/eval/tone.md"
            tone.write_text(tone.read_text(encoding="utf-8") + "\n合成调参。", encoding="utf-8")
        bridge = _EvalBridge(
            str(request.get("surface", "public")), request.get("bridge_error")
        )
        confirmed = bool(request.get("authorize", True))
        markers = [str(item) for item in request.get("forbidden_markers", [])]
        observed = {"authorize_viewed": None, "authorize_rounds": None}

        def authorize(_plan: dict[str, object]) -> bool:
            """授权回调前观察本次 holdout intent 是否已落盘。"""
            if suite == "holdout":
                try:
                    registry = store.read_holdout_registry()
                    batches = registry.get("batches") or []
                    batch = next(
                        (
                            item
                            for item in batches
                            if isinstance(item, dict)
                            and item.get("batch_id") == "synthetic-batch"
                        ),
                        None,
                    )
                    if isinstance(batch, dict):
                        observed["authorize_viewed"] = bool(batch.get("viewed"))
                        observed["authorize_rounds"] = len(batch.get("access_rounds") or [])
                except Exception:
                    pass
            return confirmed

        try:
            result = eval_usecases.eval_pack(
                repo,
                store,
                pack_rel,
                suite,
                bridge,
                authorize,
                now=str(request.get("now", "2026-11-24T00:00:00.000Z")),
            )
        except Exception as error:
            reasons: list[str] = []
            reports = sorted(
                (ROOT / "data/evals").glob("eval-*/report.json"),
                key=lambda item: item.stat().st_mtime,
            )
            if reports:
                try:
                    report = json.loads(reports[-1].read_text(encoding="utf-8"))
                    reasons = sorted(
                        {
                            str(case.get("reason_code"))
                            for case in report.get("cases", [])
                            if case.get("reason_code")
                        }
                    )
                except (OSError, ValueError):
                    reasons = []
            return {
                "status": "error",
                "reason_code": getattr(error, "reason_code", None),
                "case_reasons": reasons,
                "bridge_calls": bridge.calls,
                "transport_markers": _transport_marker_count(bridge, markers),
                **observed,
            }
        holdout = None
        report_path = result.get("report_path")
        if isinstance(report_path, str):
            try:
                report = json.loads((ROOT / report_path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                report = {}
            if isinstance(report.get("holdout"), dict):
                holdout = report["holdout"]
        return {
            "status": "ok",
            "reason_code": None,
            "eval_status": result.get("status"),
            "acceptance": result.get("acceptance_path") is not None,
            "case_count": (result.get("buckets") or {}).get("total"),
            "bridge_calls": bridge.calls,
            "descriptor_simulated": bridge.descriptor().simulated,
            "transport_markers": _transport_marker_count(bridge, markers),
            "role_call_count": len(bridge.role_payloads),
            **observed,
            "holdout": holdout,
        }
    if mode == "register":
        try:
            result = eval_usecases.register_holdout(
                store,
                batch_id=str(request.get("batch_id", "synthetic-batch")),
                version="2026-11-24",
                sample_count=int(request.get("sample_count", 2)),
                annotator="synthetic-annotator",
                maintainer="synthetic-maintainer",
                frozen_at=str(request.get("frozen_at", "2026-11-24T00:00:00.000Z")),
                candidate_release="synthetic-release",
            )
        except Exception as error:
            return {"status": "error", "reason_code": getattr(error, "reason_code", None)}
        return {"status": "ok", "reason_code": None, "batch": result.get("batch", {}).get("batch_id")}
    if mode == "retire":
        try:
            result = eval_usecases.retire_holdout(
                store,
                batch_id=str(request.get("batch_id", "synthetic-batch")),
                retired_at=str(request.get("retired_at", "2026-11-25T00:00:00.000Z")),
            )
        except Exception as error:
            return {"status": "error", "reason_code": getattr(error, "reason_code", None)}
        return {
            "status": "ok",
            "reason_code": None,
            "regression_ref_present": bool(result.get("regression_ref")),
        }
    if mode == "holdout_audit":
        try:
            registry = store.read_holdout_registry()
        except Exception as error:
            return {"status": "error", "reason_code": getattr(error, "reason_code", None)}
        batches = registry.get("batches") or []
        if not isinstance(batches, list) or not batches:
            return {"status": "error", "reason_code": "eval_failed"}
        batch = next(
            (
                item
                for item in batches
                if isinstance(item, dict)
                and item.get("batch_id") == str(request.get("batch_id", "synthetic-batch"))
            ),
            None,
        )
        if batch is None:
            return {"status": "error", "reason_code": "eval_failed"}
        rounds = batch.get("access_rounds") or []
        return {
            "status": "ok",
            "reason_code": None,
            "round_count": len(rounds),
            "outcomes": [item.get("outcome") for item in rounds],
            "phases": [item.get("phase") for item in rounds],
            "repeat_access": [item.get("repeat_access") for item in rounds],
            "viewed": batch.get("viewed"),
            "batch_status": batch.get("status"),
        }
    if mode == "regression_check":
        try:
            suite = store.read_suite("regression")
            registry = store.read_holdout_registry()
        except Exception as error:
            return {"status": "error", "reason_code": getattr(error, "reason_code", None)}
        cases = suite.get("cases") if isinstance(suite, dict) else None
        batches = registry.get("batches") if isinstance(registry, dict) else None
        case_ids = [
            case.get("input", {}).get("case_id")
            for case in (cases or [])
            if isinstance(case, dict)
        ]
        case_digests = [
            hashlib.sha256(
                json.dumps(
                    case.get("input"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            for case in (cases or [])
            if isinstance(case, dict)
        ]
        statuses = sorted(
            str(batch.get("status"))
            for batch in (batches or [])
            if isinstance(batch, dict)
        )
        return {
            "status": "ok",
            "reason_code": None,
            "regression_case_ids": case_ids,
            "regression_case_digests": case_digests,
            "regression_case_count": len(case_ids or []),
            "registry_statuses": statuses,
            "frozen_absent": "frozen" not in statuses,
        }
    if mode in {"register_write_fail", "retire_admission_fail", "retire_registry_fail"}:
        import app.adapters.files.evals as evals_module

        hits = {"replace": 0, "link": 0}

        def failing_replace(*args, **kwargs):
            """在真实 registry 原子替换边界注入 OSError 并计数。"""
            del args, kwargs
            hits["replace"] += 1
            raise OSError("synthetic replace failure")

        def failing_link(*args, **kwargs):
            """在真实准入副本 link 边界注入 OSError 并计数。"""
            del args, kwargs
            hits["link"] += 1
            raise OSError("synthetic link failure")

        before = _registry_snapshot(store)
        boundary = (
            failing_link
            if mode == "retire_admission_fail"
            else failing_replace
        )
        patch_name = "link" if mode == "retire_admission_fail" else "replace"
        original_boundary = getattr(evals_module.os, patch_name)
        setattr(evals_module.os, patch_name, boundary)
        try:
            if mode == "register_write_fail":
                eval_usecases.register_holdout(
                    store,
                    batch_id=str(request.get("batch_id", "synthetic-failing")),
                    version="2026-11-24",
                    sample_count=2,
                    annotator="synthetic-annotator",
                    maintainer="synthetic-maintainer",
                    frozen_at="2026-11-24T00:00:00.000Z",
                    candidate_release="synthetic-release",
                )
            else:
                eval_usecases.retire_holdout(
                    store,
                    batch_id=str(request.get("batch_id", "synthetic-batch")),
                    retired_at="2026-11-25T00:00:00.000Z",
                )
        except Exception as error:
            after = _registry_snapshot(store)
            regression_error = None
            try:
                suite = store.read_suite("regression")
                cases = suite.get("cases") if isinstance(suite, dict) else []
            except Exception as regression_failure:
                cases = []
                regression_error = getattr(regression_failure, "reason_code", "eval_failed")
            admission_root = ROOT / "data/evals/regression-admissions"
            return {
                "status": "error",
                "reason_code": getattr(error, "reason_code", None),
                "registry_unchanged": before == after,
                "regression_case_count": len(cases or []),
                "regression_error": regression_error,
                "injection_hits": hits["replace"] + hits["link"],
                "replace_hits": hits["replace"],
                "link_hits": hits["link"],
                "admission_written": admission_root.exists()
                and any(admission_root.glob("*/suite.json")),
            }
        finally:
            setattr(evals_module.os, patch_name, original_boundary)
        return {"status": "ok", "reason_code": None, "registry_unchanged": False}
    raise ProbeError("eval mode 非法")


class _EvalBridge(DecisionBridgePort):
    """模拟 transport：按 inbound 关键词返回固定 Decision，不证明真实模型能力。"""

    def __init__(self, surface: str, error: str | None = None) -> None:
        """保存表面标签、可选固定桥错误与调用计数。"""
        self._surface = surface
        self._error = error
        self.calls = 0
        self.user_messages: list[str] = []
        self.role_payloads: list[dict[str, Any]] = []

    def descriptor(self) -> BridgeDescriptor:
        """返回 simulated 摘要，永不生成验收凭证。"""
        return BridgeDescriptor(
            provider_id="local-process",
            model_id="synthetic-model",
            model_config_hash="0" * 64,
            bridge="bridge_b",
            simulated=True,
        )

    def complete(self, *, system_message: str, developer_message: str, user_message: str, echo: Mapping[str, str]) -> str:
        """按输入关键词返回固定 Decision JSON。"""
        self.calls += 1
        self.user_messages.append(user_message)
        self.role_payloads.append(
            {
                "system": system_message,
                "developer": developer_message,
                "user": user_message,
                "echo": dict(echo),
            }
        )
        if self._error:
            raise BridgeError(self._error, submitted=True)
        data = json.loads(user_message)
        inbound = str(data.get("inbound", ""))
        facts = data.get("allowed_facts") or []
        if "忽略" in inbound or "系统提示" in inbound:
            decision = {
                "schema_version": 1,
                "task_id": echo["task_id"],
                "nonce": echo["nonce"],
                "input_hash": echo["input_hash"],
                "intent": "uncertain",
                "sub_intents": ["sensitive"],
                "reply": "此请求需要人工确认。",
                "evidence": inbound[:2],
                "escalate": "human_review",
            }
        elif "薪资" in inbound:
            value = str(facts[0]["value"]) if facts else ""
            decision = {
                "schema_version": 1,
                "task_id": echo["task_id"],
                "nonce": echo["nonce"],
                "input_hash": echo["input_hash"],
                "intent": "interested" if facts else "needs_human",
                "sub_intents": ["salary"],
                "reply": f"您好，期望薪资为{value}。" if facts else "您好，薪资需要人工确认。",
                "evidence": inbound[:2],
                "escalate": None if facts else "human_review",
            }
        elif "地点" in inbound:
            value = str(facts[0]["value"]) if facts else ""
            decision = {
                "schema_version": 1,
                "task_id": echo["task_id"],
                "nonce": echo["nonce"],
                "input_hash": echo["input_hash"],
                "intent": "interested",
                "sub_intents": ["greeting"],
                "reply": f"您好，接受{value}的工作地点。",
                "evidence": inbound[:2],
                "escalate": None,
            }
        else:
            decision = {
                "schema_version": 1,
                "task_id": echo["task_id"],
                "nonce": echo["nonce"],
                "input_hash": echo["input_hash"],
                "intent": "interested",
                "sub_intents": ["job_question"],
                "reply": "您好，岗位方向以职位说明为准。",
                "evidence": inbound[:2],
                "escalate": None,
            }
        return json.dumps(decision, ensure_ascii=False)


def _canonical_bytes(value: Any) -> int:
    """按契约 H 的规范化 JSON 计算 UTF-8 字节数，用于渲染上限观察。"""
    return len(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )


def _render_size_operation(request: Mapping[str, Any]) -> dict[str, Any]:
    """真实 pack/render 下二分定位渲染内容 65536/65537 字节边界。"""
    repo = FilePolicyRepository(ROOT)
    source = ROOT / "prompts/local/valid"
    target = ROOT / "prompts/local/size_render"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    case = repo.read_preview_case("policy-greeting")
    base_files = repo.read_base()
    original_tone = (target / "tone.md").read_text(encoding="utf-8")
    for name, desired in (("phrase-bank.md", 20000), ("never-say.md", 10000)):
        original = (target / name).read_text(encoding="utf-8").rstrip("\n")
        padding = max(0, desired - len(original.encode("utf-8")) - 1)
        (target / name).write_text(original + "\n" + " " * padding + "\n", encoding="utf-8")

    def attempt(tone_bytes: int) -> tuple[int | None, str | None]:
        """写入指定 tone 大小并真实渲染，返回内容字节或固定超限原因。"""
        text = original_tone.rstrip("\n") + "\n" + " " * max(0, tone_bytes - len(original_tone.encode("utf-8")) - 1)
        (target / "tone.md").write_text(text + "\n", encoding="utf-8")
        try:
            _normalized, loaded_base, _files, _guides, pack = policy_usecases.load_active_pack(
                repo, "prompts/local/size_render"
            )
            envelope = build_envelope(
                task_id=str(case["task_id"]),
                nonce=str(case["nonce"]),
                kind=str(case["kind"]),
                subject=case["subject"],
                inbound=str(case["inbound"]),
                context=list(case["context"]),
                allowed_facts=list(case["allowed_facts"]),
                base_files=loaded_base,
                pack=pack,
                now=str(case["now"]),
            )
            rendered = render_request(
                loaded_base, pack, envelope, now=str(case["now"])
            )
            return _canonical_bytes(rendered.content), None
        except DecisionError as error:
            return None, error.reason_code
        except PolicyError as error:
            return None, error.reason_code

    low, high = 20000, 32768
    best_size = None
    best_tone = None
    while low <= high:
        middle = (low + high) // 2
        size, reason = attempt(middle)
        if size is not None:
            best_size = size
            best_tone = middle
            low = middle + 1
        elif reason in {"input_too_large", "invalid_policy"}:
            high = middle - 1
        else:
            return {"status": "error", "reason_code": reason or "invalid_policy"}
    over_size, over_reason = attempt((best_tone or 0) + 1)
    return {
        "status": "ok",
        "content_max_bytes": best_size,
        "over_content_bytes": over_size,
        "over_reason": over_reason,
        "bridge_calls": 0,
    }


def run_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """按 operation 分派到真实公开接口，并返回固定摘要。"""
    operation = request.get("operation")
    if operation == "decision":
        return _decision_operation(request)
    if operation == "hash":
        return _hash_operation(request)
    if operation == "render_size":
        return _render_size_operation(request)
    if operation == "envelope_size":
        return _envelope_size_operation(request)
    if operation == "hash_mutations":
        return _hash_mutations_operation(request)
    if operation == "facts":
        return _facts_operation(request)
    if operation == "render":
        return _render_operation(request)
    if operation == "bridge":
        return _bridge_operation(request)
    if operation == "decision_run":
        return _decision_run_operation(request)
    if operation == "policy_validate":
        return _policy_validate_operation(request)
    if operation == "mapping":
        return _mapping_operation(request)
    if operation == "restore":
        return _restore_operation(request)
    if operation == "eval":
        return _eval_operation(request)
    raise ProbeError("未知 operation")


def main(argv: list[str] | None = None) -> int:
    """读取受控请求文件并输出单个 JSON 摘要。"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--request", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print(json.dumps({"schema_version": 1, "status": "error", "reason_code": "invalid_input"}))
        return 2
    try:
        request_path = _relative(args.request, "request", must_exist=True)
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise ProbeError("request 必须是对象")
        result = run_request(request)
    except (ProbeError, OSError, ValueError, UnicodeError) as error:
        result = {"status": "error", "reason_code": type(error).__name__}
    payload = {"schema_version": 1, "status": result.pop("status", "error"), **result}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
