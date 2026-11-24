"""解析严格 CLI 参数、加载最小配置，并输出唯一的 UTF-8 JSON 结果。"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
import uuid
from pathlib import Path
from typing import NoReturn

from app.adapters.adb.device import AdbDeviceProbe, DeviceProbeError
from app.adapters.boss.list import load_and_parse_snapshot
from app.adapters.boss.read import load_and_read_fixture
from app.adapters.path_state import PATH_FILE, PATH_MISSING, probe_path
from app.adapters.files.evals import FileEvalStore
from app.adapters.files.policy import FilePolicyRepository
from app.adapters.llm.bridge_a import ExternalAgentBridge
from app.adapters.llm.bridge_b import LocalProcessBridge
from app.adapters.sqlite.activity import ProfilePolicyActivity
from app.adapters.sqlite.device_gate import SharedDeviceGate
from app.adapters.sqlite.profile_store import SqliteProfileStore, profile_runtime_path
from app.adapters.sqlite.repo import SqliteStore
from app.adapters.sqlite.schema import initialize_database
from app.domain.models import Owner, canonical_hash
from app.ports.device import ProbeSnapshot
from app.ports.device_gate import DeviceGateError
from app.ports.policy import PolicyError
from app.ports.profile import ProfileError
from app.ports.store import StoreError
from app.domain.json_input import load_json_object as _load_json_object
from app.domain.policy import FACT_KEYS, PolicyIssue, utc_now
from app.process_log import ProcessLog, elapsed_ms
from app.profile_open import open_profile_store
from app.usecases import acceptance as acceptance_usecases
from app.usecases import facts as facts_usecases
from app.usecases import policy as policy_usecases
from app.usecases import policy_eval as policy_eval_usecases
from app.usecases import run as run_usecases
from app.usecases.doctor import build_result, parse_probe_input, run_doctor
from app.usecases.read import build_read_payload
from app.usecases.sense import build_sense_payload, persist_and_aggregate

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = "data/runtime/runtime.sqlite3"
POLICY_EVAL_HELP = """
支持的桥/provider（必须显式选择，不使用默认真实模型）:
  external-agent  架构 A 外部 agent 子进程；协议 m4b-agent-v1。
                  格式：--provider external-agent --bridge-command <程序> --arg <参数>...
  local-process   架构 B 本机进程；协议 m4b-local-v1；私有留出只允许本类型。
                  格式：--provider local-process --bridge-command <程序> --arg <参数>...
认证变量/端点入口:
  本程序不读取、不转发、不记录认证变量值；桥程序如需认证，使用其自身本机环境
  或受保护凭据存储，变量名与端点参数由该桥程序文档定义。不得把凭据放进命令行。
本机要求:
  桥程序必须是本机可执行的参数数组入口，固定接收 system/developer/user 三消息、
  独立回显对象、tools=[]、history=[]；不支持三角色分区或返回角色声明不符时拒绝。
留出无上传门槛:
  --suite holdout 必须同时满足 local-process、当前 BridgeDescriptor 的 model_id/
  model_config_hash 与本机 data/evals/holdout-attestation.local.json 一致，且
  no_upload/no_cache/no_share/no_telemetry/cloud_fallback_disabled 全为 true。
  该合同是 operator/host 显式声明，不证明子进程真实无网络；通用
  --confirm-external-send 不替代留出许可，缺条件零调用。
拒绝:
  echo、参考回放（reference-provider）、smoke 冒充、未知 provider。
固定诊断:
  invalid_provider/authorization_required/invalid_decision/stale_decision/
  stale_evidence/decision_timeout/policy_changed/bridge_start_failed/
  bridge_auth/bridge_quota/bridge_disconnected/bridge_protocol/bridge_roles/eval_failed
产物:
  逐例报告与成功凭证只写本机 data/evals；失败不生成有效验收凭证。
--confirm-external-send:
  仅是使用者对本轮模型评测外发与计费的显式确认，不授予设备、事实写入或业务发送权限。
"""



class _CliArgumentError(Exception):
    """表示参数形状错误，统一映射为 invalid_input。"""


class _ConfigError(Exception):
    """表示配置读取或内容错误，统一映射为 invalid_config。"""


class _FixtureError(Exception):
    """表示夹具读取或形状错误，统一映射为 invalid_fixture。"""


class _StrictArgumentParser(argparse.ArgumentParser):
    """禁用自动缩写，并把参数错误转成可安全输出的异常。"""

    def error(self, message: str) -> NoReturn:
        """阻止 argparse 向 stderr 打印用法或原始输入。"""
        raise _CliArgumentError(message) from None


class _UniqueStore(argparse.Action):
    """拒绝同一选项重复出现，避免后值静默覆盖前值。"""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        """在目标属性已赋值时抛出参数错误。"""
        if getattr(namespace, self.dest, None) is not None:
            raise _CliArgumentError("duplicate option") from None
        setattr(namespace, self.dest, values)


class _UniqueStoreTrue(argparse.Action):
    """拒绝重复的布尔开关，保持参数校验严格。"""

    def __init__(
        self,
        option_strings: list[str],
        dest: str,
        default: object = False,
        help: str | None = None,
    ) -> None:
        """按零参数开关初始化动作。"""
        super().__init__(
            option_strings=option_strings,
            dest=dest,
            nargs=0,
            default=default,
            help=help,
        )

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        """在开关已打开时抛出参数错误。"""
        if getattr(namespace, self.dest, False):
            raise _CliArgumentError("duplicate option") from None
        setattr(namespace, self.dest, True)


def _build_parser() -> argparse.ArgumentParser:
    """构造全局 --config 与 doctor/store/sense/read/policy 子命令的严格解析器。"""
    parser = _StrictArgumentParser(
        prog="python -m app",
        description=(
            "M0 只读 doctor、M1 store init、M2 sense、M3 read、M4a policy、M4b policy eval、"
            "M4c facts/run check 与共享设备门禁；除显式 policy eval 本机桥或已确认的真实探针外，"
            "默认不连接设备、不调用网络或模型。"
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "--config",
        action=_UniqueStore,
        metavar="<相对文件>",
        help="可选的最小只读配置；省略时使用内置默认值。",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_StrictArgumentParser,
    )
    doctor = subparsers.add_parser(
        "doctor",
        allow_abbrev=False,
        help="运行一次只读设备探针。",
        description="在合成 fixture 或显式真实设备模式中选择其一。",
    )
    modes = doctor.add_mutually_exclusive_group()
    modes.add_argument(
        "--fixture",
        action=_UniqueStore,
        metavar="<相对文件>",
        help="读取合成探针输入，不启动子进程。",
    )
    modes.add_argument(
        "--device",
        action=_UniqueStoreTrue,
        default=False,
        help="显式启用一次真实 ADB 探针，必须同时给出 --serial。",
    )
    doctor.add_argument(
        "--serial",
        action=_UniqueStore,
        metavar="<serial>",
        help="仅与 --device 同用；只在内存匹配，不写入输出。",
    )
    doctor.add_argument(
        "--confirm-device",
        action=_UniqueStoreTrue,
        default=False,
        help="确认本轮独占一次真实 ADB 探针；未确认时任何设备子进程都不启动。",
    )
    store = subparsers.add_parser(
        "store",
        allow_abbrev=False,
        help="维护 M1 本机状态库。",
        description="只支持 store init；不导入旧库、不覆盖已有业务行。",
    )
    store_commands = store.add_subparsers(
        dest="store_command",
        required=True,
        parser_class=_StrictArgumentParser,
    )
    store_init = store_commands.add_parser(
        "init",
        allow_abbrev=False,
        help="幂等创建或核验 v1 空库。",
        description="首次创建五表、约束与索引；已存在 v1 库只核验不改行。",
    )
    store_init.add_argument(
        "--db",
        action=_UniqueStore,
        metavar="<项目内相对文件>",
        help=f"本机 SQLite 路径；省略为 {DEFAULT_DB_PATH}。",
    )
    sense = subparsers.add_parser(
        "sense",
        allow_abbrev=False,
        help="解析 M2 合成未读快照并按需落库。",
        description="必须提供合成 fixture；不连接设备、不调用模型或网络。",
    )
    sense.add_argument(
        "--fixture",
        action=_UniqueStore,
        required=True,
        metavar="<相对文件>",
        help="读取 sensor manifest 及其同目录 profile/XML。",
    )
    sense.add_argument(
        "--db",
        action=_UniqueStore,
        metavar="<项目内相对文件>",
        help=f"本机 SQLite 路径；省略为 {DEFAULT_DB_PATH}。",
    )
    read = subparsers.add_parser(
        "read",
        allow_abbrev=False,
        help="读取 M3 合成页面、职位、未读或会话。",
        description="必须提供合成 read fixture；不连接设备、库、模型或网络。",
    )
    read.add_argument(
        "--fixture",
        action=_UniqueStore,
        required=True,
        metavar="<相对文件>",
        help="读取 read manifest 及其同目录 profile/XML/像素文件。",
    )
    facts = subparsers.add_parser(
        "facts",
        allow_abbrev=False,
        help="按 profile 确认、查看、撤销或遗忘长期事实。",
        description=(
            "值只写本机 profile 独立库；提案需再次 --confirm 才落库；"
            "不读取 HR 文本、策略包事实或任何外部来源。"
        ),
    )
    facts_commands = facts.add_subparsers(
        dest="facts_command",
        required=True,
        parser_class=_StrictArgumentParser,
    )
    facts_set = facts_commands.add_parser(
        "set",
        allow_abbrev=False,
        help="提案或确认写入一条长期事实。",
        description="未带 --confirm 时只显示提案；写入后再次显示库中确认结果。",
    )
    facts_set.add_argument(
        "--profile", action=_UniqueStore, required=True, metavar="<profile_id>"
    )
    facts_set.add_argument("--key", action=_UniqueStore, required=True, metavar="<事实键>")
    facts_set.add_argument("--value", action=_UniqueStore, required=True, metavar="<本机值>")
    facts_set.add_argument(
        "--expires-at", action=_UniqueStore, metavar="<UTC时间>", default=None
    )
    facts_set.add_argument(
        "--replace",
        action=_UniqueStoreTrue,
        default=False,
        help="明确作废该键现有 active 来源；不静默按较新时间择一。",
    )
    facts_set.add_argument(
        "--confirm",
        action=_UniqueStoreTrue,
        default=False,
        help="确认本轮写入；未确认不落库。",
    )
    facts_set.add_argument(
        "--pack", action=_UniqueStore, metavar="<prompts/local/名称>", default=None
    )
    facts_show = facts_commands.add_parser(
        "show",
        allow_abbrev=False,
        help="显示本机事实、来源、有效期与按包白名单计算的待办。",
        description="只读；不存在 profile 库时返回空结果，不创建库。",
    )
    facts_show.add_argument(
        "--profile", action=_UniqueStore, required=True, metavar="<profile_id>"
    )
    facts_show.add_argument(
        "--pack", action=_UniqueStore, metavar="<prompts/local/名称>", default=None
    )
    for name, help_text in (
        ("revoke", "提案或确认撤销该键当前 active 事实。"),
        ("forget", "提案或确认删除该键当前事实并保留无值来源审计。"),
    ):
        command = facts_commands.add_parser(
            name,
            allow_abbrev=False,
            help=help_text,
            description="未带 --confirm 时只显示提案；确认后写入本机 profile 库。",
        )
        command.add_argument(
            "--profile", action=_UniqueStore, required=True, metavar="<profile_id>"
        )
        command.add_argument("--key", action=_UniqueStore, required=True, metavar="<事实键>")
        command.add_argument(
            "--confirm",
            action=_UniqueStoreTrue,
            default=False,
            help="确认本轮撤销/遗忘；未确认不落库。",
        )
        command.add_argument(
            "--pack", action=_UniqueStore, metavar="<prompts/local/名称>", default=None
        )
    run = subparsers.add_parser(
        "run",
        allow_abbrev=False,
        help="运行前 mode/确认/验收/共享占用预检，不执行动作。",
        description=(
            "只做预检：不探测设备、不调用模型、不执行 M5 动作；"
            "预检成功不等于自动发送授权。"
        ),
    )
    run_commands = run.add_subparsers(
        dest="run_command",
        required=True,
        parser_class=_StrictArgumentParser,
    )
    run_check = run_commands.add_parser(
        "check",
        allow_abbrev=False,
        help="检查指定 mode 的前置护栏。",
        description="read_only 只要求本轮设备确认；draft/A 另按当前验收与固定 C 判定。",
    )
    run_check.add_argument(
        "--pack", action=_UniqueStore, required=True, metavar="<prompts/local/名称>"
    )
    run_check.add_argument(
        "--mode",
        action=_UniqueStore,
        required=True,
        choices=("read_only", "draft_only", "auto_send_tier_a"),
        metavar="<mode>",
    )
    run_check.add_argument(
        "--confirm-device",
        action=_UniqueStoreTrue,
        default=False,
        help="确认本轮设备独占；未确认时预检失败且不取得共享占用。",
    )
    run_check.add_argument(
        "--credential",
        action=_UniqueStore,
        metavar="<data/evals/eval-<id>/acceptance.json>",
        default=None,
    )
    run_check.add_argument(
        "--provider", action=_UniqueStore, metavar="<provider_id>", default=None
    )
    run_check.add_argument(
        "--model-id", action=_UniqueStore, metavar="<model_id>", default=None
    )
    run_check.add_argument(
        "--model-config-hash", action=_UniqueStore, metavar="<摘要>", default=None
    )
    policy = subparsers.add_parser(
        "policy",
        allow_abbrev=False,
        help="离线编辑/校验/预览/回滚策略包，并运行显式本机桥评测。",
        description=(
            "init/validate/preview/list/snapshot/restore/holdout/acceptance 只读本机策略文件与凭证；"
            "restore 另查真实 profile/共享占用；eval 只在显式 provider 与本轮确认下启动本机桥程序。"
        ),
    )
    policy_commands = policy.add_subparsers(
        dest="policy_command",
        required=True,
        parser_class=_StrictArgumentParser,
    )
    policy_init = policy_commands.add_parser(
        "init",
        allow_abbrev=False,
        help="从公开示例创建 local 直接子目录策略包。",
        description="目标存在不覆盖；失败不留可加载半包。",
    )
    policy_init.add_argument(
        "--dest",
        action=_UniqueStore,
        required=True,
        metavar="<prompts/local/名称>",
        help="新的 local 直接 profile 子目录。",
    )
    policy_validate = policy_commands.add_parser(
        "validate",
        allow_abbrev=False,
        help="校验活动包与 base 并输出有效限制和摘要。",
        description="不调用设备、网络或模型，不修改包。",
    )
    policy_validate.add_argument(
        "--pack",
        action=_UniqueStore,
        required=True,
        metavar="<prompts/local/名称>",
        help="local 直接活动包目录。",
    )
    policy_preview = policy_commands.add_parser(
        "preview",
        allow_abbrev=False,
        help="用独立合成输入纯渲染决策投影。",
        description="不调用桥、设备、网络或模型，不读取黄金答案。",
    )
    policy_preview.add_argument(
        "--pack",
        action=_UniqueStore,
        required=True,
        metavar="<prompts/local/名称>",
        help="local 直接活动包目录。",
    )
    policy_preview.add_argument(
        "--case",
        action=_UniqueStore,
        required=True,
        metavar="<案例ID>",
        help="固定 tests/fixtures/policy-preview 中的合成案例 ID。",
    )
    policy_commands.add_parser(
        "list",
        allow_abbrev=False,
        help="列出活动包与 history 快照，不自动选最新。",
        description="只输出相对路径、身份、摘要与有效性，不输出正文。",
    )
    policy_snapshot = policy_commands.add_parser(
        "snapshot",
        allow_abbrev=False,
        help="把已校验活动包复制为 history 快照。",
        description="复制前后摘要一致且清单完成后才发布成功。",
    )
    policy_snapshot.add_argument(
        "--pack",
        action=_UniqueStore,
        required=True,
        metavar="<prompts/local/名称>",
        help="local 直接活动包目录。",
    )
    policy_restore = policy_commands.add_parser(
        "restore",
        allow_abbrev=False,
        help="校验清单/快照/目标三值后恢复到新目录。",
        description="M4a 生产缺可信停止证据时受控拒绝；不覆盖旧包。",
    )
    policy_restore.add_argument(
        "--snapshot",
        action=_UniqueStore,
        required=True,
        metavar="<prompts/local/history/快照ID>",
        help="history 直接快照目录。",
    )
    policy_restore.add_argument(
        "--dest",
        action=_UniqueStore,
        required=True,
        metavar="<prompts/local/名称>",
        help="新的 local 直接目标目录。",
    )
    policy_eval = policy_commands.add_parser(
        "eval",
        allow_abbrev=False,
        help="用当前包经真实桥路径运行本机公开/分层评测。",
        description=(
            "实际加载当前包并经同一 DecisionBridgePort 运行；必须显式选择本地桥程序，"
            "不接受 echo/参考回放。"
        ),
        epilog=POLICY_EVAL_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    policy_eval.add_argument(
        "--pack",
        action=_UniqueStore,
        required=True,
        metavar="<prompts/local/名称>",
        help="local 直接活动包目录。",
    )
    policy_eval.add_argument(
        "--suite",
        action=_UniqueStore,
        required=True,
        choices=("public", "regression", "holdout"),
        metavar="<public|regression|holdout>",
        help="公开契约、私有回归或冻结留出套件。",
    )
    policy_eval.add_argument(
        "--provider",
        action=_UniqueStore,
        required=True,
        metavar="<external-agent|local-process>",
        help="显式桥/provider 类型；不支持的名字报 invalid_provider。",
    )
    policy_eval.add_argument(
        "--bridge-command",
        action=_UniqueStore,
        required=True,
        metavar="<本机程序>",
        help="桥程序路径或命令名；作为 argv[0]，不经过 shell。",
    )
    policy_eval.add_argument(
        "--arg",
        action="append",
        default=[],
        metavar="<参数>",
        help="追加到参数数组的单个参数，可重复；不放凭据。",
    )
    policy_eval.add_argument(
        "--model-id",
        action=_UniqueStore,
        required=True,
        metavar="<标签>",
        help="报告使用的模型标签；非空、不含凭据。",
    )
    policy_eval.add_argument(
        "--confirm-external-send",
        action=_UniqueStoreTrue,
        default=False,
        help="确认本轮模型评测外发与计费；不授予设备/事实/业务发送权限。",
    )
    policy_holdout = policy_commands.add_parser(
        "holdout",
        allow_abbrev=False,
        help="登记或退役本机私有留出冻结批次。",
        description=(
            "只读私有留出套件并维护本机登记；不读取真实私有样本内容到报告，"
            "不调用模型、设备或网络。"
        ),
    )
    holdout_commands = policy_holdout.add_subparsers(
        dest="holdout_command",
        required=True,
        parser_class=_StrictArgumentParser,
    )
    holdout_register = holdout_commands.add_parser(
        "register",
        allow_abbrev=False,
        help="登记新的冻结留出批次。",
        description="要求维护者、独立标注者、版本、数量、时间与候选发布齐全。",
    )
    holdout_register.add_argument(
        "--batch-id", action=_UniqueStore, required=True, metavar="<批次ID>"
    )
    holdout_register.add_argument(
        "--version", action=_UniqueStore, required=True, metavar="<版本>"
    )
    holdout_register.add_argument(
        "--sample-count", action=_UniqueStore, required=True, type=int, metavar="<数量>"
    )
    holdout_register.add_argument(
        "--annotator", action=_UniqueStore, required=True, metavar="<独立标注者>"
    )
    holdout_register.add_argument(
        "--maintainer", action=_UniqueStore, required=True, metavar="<维护者>"
    )
    holdout_register.add_argument(
        "--candidate-release", action=_UniqueStore, required=True, metavar="<候选发布>"
    )
    holdout_register.add_argument(
        "--frozen-at", action=_UniqueStore, metavar="<UTC时间>", default=None
    )
    holdout_retire = holdout_commands.add_parser(
        "retire",
        allow_abbrev=False,
        help="整批退役留出批次。",
        description="退役后必须为新候选发布重新登记冻结批次。",
    )
    holdout_retire.add_argument(
        "--batch-id", action=_UniqueStore, required=True, metavar="<批次ID>"
    )
    holdout_retire.add_argument(
        "--retired-at", action=_UniqueStore, metavar="<UTC时间>", default=None
    )
    policy_acceptance = policy_commands.add_parser(
        "acceptance",
        allow_abbrev=False,
        help="只读复核 M4b 验收凭证是否绑定当前摘要。",
        description="不调用模型、不授予设备或发送权限；摘要变化拒绝旧凭证。",
    )
    acceptance_commands = policy_acceptance.add_subparsers(
        dest="acceptance_command",
        required=True,
        parser_class=_StrictArgumentParser,
    )
    acceptance_check = acceptance_commands.add_parser(
        "check",
        allow_abbrev=False,
        help="比较当前 base/pack 与凭证绑定摘要。",
        description="可附加 provider/model 摘要比较；任何不一致退出 2。",
    )
    acceptance_check.add_argument(
        "--pack",
        action=_UniqueStore,
        required=True,
        metavar="<prompts/local/名称>",
        help="local 直接活动包目录。",
    )
    acceptance_check.add_argument(
        "--credential",
        action=_UniqueStore,
        required=True,
        metavar="<data/evals/eval-<id>/acceptance.json>",
        help="本机验收凭证相对路径。",
    )
    acceptance_check.add_argument(
        "--provider", action=_UniqueStore, metavar="<provider_id>", default=None
    )
    acceptance_check.add_argument(
        "--model-id", action=_UniqueStore, metavar="<model_id>", default=None
    )
    acceptance_check.add_argument(
        "--model-config-hash",
        action=_UniqueStore,
        metavar="<摘要>",
        default=None,
    )
    return parser


def _resolve_project_path(value: str) -> Path:
    """把相对输入解析到项目根内，拒绝绝对路径、盘符和越界链接。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("path is empty")
    candidate = Path(value)
    if (
        candidate.is_absolute()
        or candidate.drive
        or candidate.anchor
        or value.startswith("~")
    ):
        raise ValueError("path is not project-relative")
    resolved = (PROJECT_ROOT / candidate).resolve()
    if not resolved.is_relative_to(PROJECT_ROOT):
        raise ValueError("path escapes project root")
    return resolved


def _validate_config(value: str | None) -> None:
    """只校验最小只读配置；省略或空对象保持固定 read_only 默认契约。"""
    if value is None:
        return
    try:
        path = _resolve_project_path(value)
        data = _load_json_object(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise _ConfigError from None

    if not isinstance(data, dict):
        raise _ConfigError
    if not set(data) <= {"schema_version", "mode"}:
        raise _ConfigError
    if "schema_version" in data:
        version = data["schema_version"]
        if type(version) is not int or version != 1:
            raise _ConfigError
    if "mode" in data:
        mode = data["mode"]
        if not isinstance(mode, str) or mode != "read_only":
            raise _ConfigError


def _load_fixture(value: str) -> ProbeSnapshot:
    """读取并严格校验合成探针输入，不启动子进程也不写业务库。"""
    try:
        path = _resolve_project_path(value)
        data = _load_json_object(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise _FixtureError from None
    try:
        return parse_probe_input(data)
    except ValueError:
        raise _FixtureError from None


def _mode_is_valid(args: argparse.Namespace) -> bool:
    """检查 fixture/device 互斥、serial 归属以及缺参数情形。"""
    if args.command != "doctor":
        return False
    if args.fixture is not None and not args.fixture:
        return False
    if args.device:
        return bool(args.serial and args.serial.strip() and args.confirm_device)
    return (
        args.fixture is not None
        and args.serial is None
        and not args.confirm_device
    )


def _requested_mode(args: argparse.Namespace) -> str | None:
    """返回参数请求的 fixture/device 模式；冲突或缺失时为 null。"""
    if args.fixture is not None and not args.device:
        return "fixture"
    if args.device and args.fixture is None:
        return "device"
    return None


def _finish(
    log: ProcessLog,
    started: float,
    exit_code: int,
    reason_code: str | None,
    error_category: str | None,
) -> int:
    """追加 exit 事件并返回退出码；日志降级时用固定 stderr 提示不完整。"""
    log.record(
        stage="exit",
        result="ok" if exit_code == 0 else "failed",
        reason_code=reason_code,
        error_category=error_category,
        duration_ms=elapsed_ms(started),
        exit_code=exit_code,
    )
    log.emit_degraded_warning()
    return exit_code


def _emit(payload: dict[str, object]) -> bool:
    """把白名单 JSON 对象以单行 UTF-8 写入 stdout；写出失败返回 False。"""
    try:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is not None:
            buffer.write(text.encode("utf-8"))
            buffer.flush()
        else:
            sys.stdout.write(text)
            sys.stdout.flush()
    except Exception:
        return False
    return True


def _error_payload(reason_code: str) -> dict[str, object]:
    """构造参数、配置或夹具错误的安全固定 JSON 对象。"""
    return {
        "schema_version": 1,
        "status": "failed",
        "reason_code": reason_code,
    }


def _store_success_payload() -> dict[str, object]:
    """返回 store init 成功时的唯一固定 JSON 对象。"""
    return {"schema_version": 1, "status": "ok", "reason_code": None}


def _run_store_init(log: ProcessLog, started: float, args: argparse.Namespace) -> int:
    """解析项目内 --db 并幂等初始化或核验，最多输出一个安全 JSON。"""
    db_value = args.db if args.db is not None else DEFAULT_DB_PATH
    stage_started = time.perf_counter()
    log.record(stage="store", operation="init", result="started")
    try:
        path = _resolve_project_path(db_value)
    except ValueError:
        log.record(
            stage="store",
            operation="init",
            result="failed",
            reason_code="invalid_input",
            error_category="store",
            duration_ms=elapsed_ms(stage_started),
        )
        return _finalize_output(
            log, started, _error_payload("invalid_input"), 2, "invalid_input", "store"
        )
    try:
        initialize_database(path)
    except StoreError as error:
        log.record(
            stage="store",
            operation="init",
            result="failed",
            reason_code=error.reason_code,
            error_category="store",
            duration_ms=elapsed_ms(stage_started),
        )
        return _finalize_output(
            log,
            started,
            _error_payload(error.reason_code),
            2,
            error.reason_code,
            "store",
        )
    except Exception:
        log.record(
            stage="store",
            operation="init",
            result="failed",
            error_category="internal",
            duration_ms=elapsed_ms(stage_started),
        )
        return _finalize_output(
            log, started, _error_payload("store_io_error"), 2, "store_io_error", "store"
        )
    log.record(
        stage="store",
        operation="init",
        result="ok",
        duration_ms=elapsed_ms(stage_started),
    )
    return _finalize_output(log, started, _store_success_payload(), 0, None, None)


def _run_sense(log: ProcessLog, started: float, args: argparse.Namespace) -> int:
    """解析 M2 合成夹具，只有全部有效且合并 ok 后才初始化/打开本机库。"""
    try:
        fixture_path = _resolve_project_path(args.fixture)
    except ValueError:
        log.record(
            stage="sense",
            operation="parse",
            result="failed",
            reason_code="invalid_input",
            error_category="argument",
        )
        return _finalize_output(
            log, started, _error_payload("invalid_input"), 2, "invalid_input", "argument"
        )

    parse_started = time.perf_counter()
    log.record(stage="sense", operation="parse", result="started")
    snapshot_id = str(uuid.uuid4())
    try:
        result = load_and_parse_snapshot(fixture_path, snapshot_id)
    except (OSError, UnicodeError, ValueError):
        log.record(
            stage="sense",
            operation="parse",
            result="failed",
            reason_code="invalid_fixture",
            error_category="fixture",
            duration_ms=elapsed_ms(parse_started),
        )
        return _finalize_output(
            log,
            started,
            _error_payload("invalid_fixture"),
            2,
            "invalid_fixture",
            "fixture",
        )
    except Exception:
        log.record(
            stage="sense",
            operation="parse",
            result="failed",
            error_category="internal",
            duration_ms=elapsed_ms(parse_started),
        )
        return _finalize_output(
            log,
            started,
            _error_payload("invalid_fixture"),
            2,
            "invalid_fixture",
            "internal",
        )
    log.record(
        stage="sense",
        operation="parse",
        result="failed" if result.status == "failed" else "ok",
        reason_code=result.reason_code,
        error_category="sense" if result.status == "failed" else None,
        duration_ms=elapsed_ms(parse_started),
    )

    if result.status != "ok":
        reason_code = result.reason_code
        log.record(
            stage="decision",
            result="failed",
            reason_code=reason_code,
            error_category="sense",
        )
        return _finalize_output(
            log,
            started,
            build_sense_payload(result, reason_code),
            2,
            reason_code,
            "sense",
        )

    db_value = args.db if args.db is not None else DEFAULT_DB_PATH
    try:
        db_path = _resolve_project_path(db_value)
    except ValueError:
        log.record(
            stage="store",
            operation="init",
            result="failed",
            reason_code="invalid_input",
            error_category="store",
        )
        return _finalize_output(
            log, started, _error_payload("invalid_input"), 2, "invalid_input", "store"
        )

    init_started = time.perf_counter()
    log.record(stage="store", operation="init", result="started")
    try:
        initialize_database(db_path)
    except StoreError as error:
        log.record(
            stage="store",
            operation="init",
            result="failed",
            reason_code=error.reason_code,
            error_category="store",
            duration_ms=elapsed_ms(init_started),
        )
        return _finalize_output(
            log,
            started,
            _error_payload(error.reason_code),
            2,
            error.reason_code,
            "store",
        )
    except Exception:
        log.record(
            stage="store",
            operation="init",
            result="failed",
            error_category="internal",
            duration_ms=elapsed_ms(init_started),
        )
        return _finalize_output(
            log, started, _error_payload("store_io_error"), 2, "store_io_error", "store"
        )
    log.record(
        stage="store",
        operation="init",
        result="ok",
        duration_ms=elapsed_ms(init_started),
    )

    try:
        store = SqliteStore(db_path, process_log=log)
    except StoreError as error:
        return _finalize_output(
            log,
            started,
            _error_payload(error.reason_code),
            2,
            error.reason_code,
            "store",
        )
    except Exception:
        return _finalize_output(
            log, started, _error_payload("store_io_error"), 2, "store_io_error", "store"
        )

    try:
        try:
            aggregated, stop_reason = persist_and_aggregate(store, result, log)
        except StoreError as error:
            return _finalize_output(
                log,
                started,
                _error_payload(error.reason_code),
                2,
                error.reason_code,
                "store",
            )
        except Exception:
            return _finalize_output(
                log,
                started,
                _error_payload("store_io_error"),
                2,
                "store_io_error",
                "internal",
            )
    finally:
        try:
            store.close()
        except Exception:
            pass

    exit_code = 0 if stop_reason is None else 2
    error_category = None if stop_reason is None else "business"
    log.record(
        stage="decision",
        result="ok",
        reason_code=stop_reason,
        error_category=error_category,
    )
    return _finalize_output(
        log,
        started,
        build_sense_payload(aggregated, stop_reason),
        exit_code,
        stop_reason,
        error_category,
    )



def _run_read(log: ProcessLog, started: float, args: argparse.Namespace) -> int:
    """加载 M3 合成读取夹具，输出唯一 JSON 并按决策门禁返回退出码。"""
    try:
        fixture_path = _resolve_project_path(args.fixture)
    except ValueError:
        log.record(
            stage="read",
            operation="load",
            result="failed",
            reason_code="invalid_input",
            error_category="argument",
        )
        return _finalize_output(
            log, started, _error_payload("invalid_input"), 2, "invalid_input", "argument"
        )
    load_started = time.perf_counter()
    log.record(stage="read", operation="load", result="started")
    try:
        result = load_and_read_fixture(
            fixture_path, str(uuid.uuid4()), process_log=log
        )
    except (OSError, UnicodeError, ValueError):
        log.record(
            stage="read",
            operation="load",
            result="failed",
            reason_code="invalid_fixture",
            error_category="fixture",
            duration_ms=elapsed_ms(load_started),
        )
        return _finalize_output(
            log, started, _error_payload("invalid_fixture"), 2, "invalid_fixture", "fixture"
        )
    except Exception:
        log.record(
            stage="read",
            operation="load",
            result="failed",
            reason_code=None,
            error_category="internal",
            duration_ms=elapsed_ms(load_started),
        )
        return _finalize_output(
            log, started, _error_payload("invalid_fixture"), 2, "invalid_fixture", "internal"
        )
    log.record(
        stage="read",
        operation="load",
        result=result.status,
        reason_code=result.reason_code,
        error_category="read" if result.status == "failed" else None,
        duration_ms=elapsed_ms(load_started),
    )
    gate_started = time.perf_counter()
    log.record(stage="read", operation="gate", result="started")
    try:
        payload = build_read_payload(result)
    except Exception:
        log.record(
            stage="read",
            operation="gate",
            result="failed",
            reason_code=None,
            error_category="internal",
            duration_ms=elapsed_ms(gate_started),
        )
        return _finalize_output(
            log, started, _error_payload("invalid_fixture"), 2, "invalid_fixture", "internal"
        )
    actionable = bool(payload["actionable"])
    gate_reason = payload["gate_reason"]
    exit_code = 0 if actionable else 2
    error_category = None if actionable else "read"
    log.record(
        stage="read",
        operation="gate",
        result="ok" if actionable else "failed",
        reason_code=gate_reason,
        error_category=error_category,
        duration_ms=elapsed_ms(gate_started),
    )
    log.record(
        stage="decision",
        result="ok" if actionable else "failed",
        reason_code=result.reason_code if result.reason_code is not None else gate_reason,
        error_category=error_category,
    )
    return _finalize_output(
        log,
        started,
        payload,
        exit_code,
        result.reason_code if result.reason_code is not None else gate_reason,
        error_category,
    )


def _gate_owner() -> Owner:
    """构造本次进程的共享门禁持有者身份；host 只在本机库内使用。"""
    return Owner(
        id=str(uuid.uuid4()),
        pid=os.getpid(),
        host=canonical_hash(platform.node()),
    )


def _facts_pack(
    repo: FilePolicyRepository,
    pack_rel: str | None,
    log: ProcessLog,
):
    """按可选 --pack 加载并校验活动包，供事实待办按白名单筛选。"""
    if pack_rel is None:
        return None
    _, _base, _files, _guides, pack = policy_usecases.load_active_pack(
        repo, pack_rel, process_log=log
    )
    return pack


def _run_facts(
    log: ProcessLog, started: float, args: argparse.Namespace
) -> int:
    """执行 facts set/show/revoke/forget；值只进本机库与交互 stdout。"""
    operation = args.facts_command
    now = utc_now()
    stage_started = time.perf_counter()
    log.record(stage="facts", operation=operation, result="started")
    store: SqliteProfileStore | None = None
    try:
        repo = FilePolicyRepository(PROJECT_ROOT)
        pack = _facts_pack(repo, getattr(args, "pack", None), log)
        if pack is not None and pack.profile_id != args.profile:
            raise ProfileError("profile_mismatch")
        path = profile_runtime_path(PROJECT_ROOT, args.profile)
        path_state = probe_path(path)
        if operation == "show" and path_state == PATH_MISSING:
            result: dict[str, object] = {
                "profile_id": args.profile,
                "facts": [],
                "active_keys": [],
                "registered_keys": sorted(FACT_KEYS),
                "pending": [],
                "allowed": [],
                "scoped_by_pack": pack is not None,
            }
        else:
            if operation != "set" and path_state == PATH_MISSING:
                raise ProfileError("fact_not_found")
            if path_state not in (PATH_MISSING, PATH_FILE):
                raise ProfileError("profile_io_error")
            store = SqliteProfileStore(
                PROJECT_ROOT,
                args.profile,
                create=operation == "set",
                now=now,
            )
            open_profile_store(store, log)
            if operation == "set":
                if args.confirm:
                    result = facts_usecases.apply_set(
                        store,
                        profile_id=args.profile,
                        key=args.key,
                        value=args.value,
                        expires_at=args.expires_at,
                        replace=args.replace,
                        now=now,
                        pack=pack,
                    )
                else:
                    result = facts_usecases.propose_set(
                        store,
                        profile_id=args.profile,
                        key=args.key,
                        value=args.value,
                        expires_at=args.expires_at,
                        replace=args.replace,
                        now=now,
                    )
            elif operation == "show":
                result = facts_usecases.show_facts(
                    store, profile_id=args.profile, pack=pack, now=now
                )
            elif operation == "revoke":
                if args.confirm:
                    result = facts_usecases.apply_revoke(
                        store,
                        profile_id=args.profile,
                        key=args.key,
                        now=now,
                        pack=pack,
                    )
                else:
                    result = facts_usecases.propose_revoke(
                        store, profile_id=args.profile, key=args.key
                    )
            elif operation == "forget":
                if args.confirm:
                    result = facts_usecases.apply_forget(
                        store,
                        profile_id=args.profile,
                        key=args.key,
                        now=now,
                        pack=pack,
                    )
                else:
                    result = facts_usecases.propose_forget(
                        store, profile_id=args.profile, key=args.key
                    )
            else:
                raise ProfileError("invalid_fact")
    except (ProfileError, PolicyError) as error:
        log.record(
            stage="facts",
            operation=operation,
            result="failed",
            reason_code=error.reason_code,
            error_category="facts",
            duration_ms=elapsed_ms(stage_started),
        )
        return _finalize_output(
            log,
            started,
            _error_payload(error.reason_code),
            2,
            error.reason_code,
            "facts",
        )
    except Exception:
        log.record(
            stage="facts",
            operation=operation,
            result="failed",
            error_category="internal",
            duration_ms=elapsed_ms(stage_started),
        )
        return _finalize_output(
            log,
            started,
            _error_payload("profile_io_error"),
            2,
            "profile_io_error",
            "internal",
        )
    finally:
        if store is not None:
            store.close()
    log.record(
        stage="facts",
        operation=operation,
        result="ok",
        duration_ms=elapsed_ms(stage_started),
    )
    status = (
        "confirmation_required"
        if result.get("confirmed") is False
        else "ok"
    )
    return _finalize_output(
        log,
        started,
        {
            "schema_version": 1,
            "status": status,
            "reason_code": None,
            "result": result,
        },
        0,
        None,
        None,
    )


def _run_run(
    log: ProcessLog,
    started: float,
    args: argparse.Namespace,
    os_root: Path | None,
) -> int:
    """执行 run check 预检；不探测设备、不调用模型、不执行动作。"""
    stage_started = time.perf_counter()
    log.record(stage="run", operation="check", result="started")
    now = utc_now()
    try:
        if args.run_command != "check":
            raise PolicyError("invalid_input")
        repo = FilePolicyRepository(PROJECT_ROOT)
        _pack_rel, base_files, _files, _guides, pack = (
            policy_usecases.load_active_pack(repo, args.pack, process_log=log)
        )
        profile_path = profile_runtime_path(PROJECT_ROOT, pack.profile_id)
        gate = SharedDeviceGate(
            profile_id=pack.profile_id,
            profile_db_path=profile_path,
            os_root=os_root,
            process_log=log,
        )
        profile_store = SqliteProfileStore(
            PROJECT_ROOT, pack.profile_id, create=True, now=now
        )
        result = run_usecases.check_preflight(
            base_files=base_files,
            pack=pack,
            mode=args.mode,
            confirm_device=args.confirm_device,
            gate=gate,
            profile_store=profile_store,
            owner=_gate_owner(),
            profile_db_path=str(profile_path),
            now=now,
            eval_store=FileEvalStore(PROJECT_ROOT),
            credential_rel=args.credential,
            provider_id=args.provider,
            model_id=args.model_id,
            model_config_hash=args.model_config_hash,
            process_log=log,
        )
    except (PolicyError, DeviceGateError, ProfileError) as error:
        log.record(
            stage="run",
            operation="check",
            result="failed",
            reason_code=error.reason_code,
            error_category="run",
            duration_ms=elapsed_ms(stage_started),
        )
        return _finalize_output(
            log,
            started,
            _error_payload(error.reason_code),
            2,
            error.reason_code,
            "run",
        )
    except Exception:
        log.record(
            stage="run",
            operation="check",
            result="failed",
            error_category="internal",
            duration_ms=elapsed_ms(stage_started),
        )
        return _finalize_output(
            log,
            started,
            _error_payload("run_io_error"),
            2,
            "run_io_error",
            "internal",
        )
    log.record(
        stage="run",
        operation="check",
        result="ok",
        duration_ms=elapsed_ms(stage_started),
    )
    return _finalize_output(
        log,
        started,
        {
            "schema_version": 1,
            "status": "ok",
            "reason_code": None,
            "result": result,
        },
        0,
        None,
        None,
    )


def _policy_error_payload(
    reason_code: str, issues: list[PolicyIssue] | None = None
) -> dict[str, object]:
    """构造策略四键失败对象，errors 项固定 file/field/line/code。"""
    return {
        "schema_version": 1,
        "status": "failed",
        "reason_code": reason_code,
        "errors": [issue.to_dict() for issue in issues or []],
    }


def _policy_command_token(argv: list[str]) -> str | None:
    """只按第一个位置参数识别命令位置，跳过全局 --config 及其值。"""
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--config":
            index += 2
            continue
        if token.startswith("--config="):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return None


def _run_policy(
    log: ProcessLog,
    started: float,
    args: argparse.Namespace,
    os_root: Path | None,
) -> int:
    """执行 policy 子命令，复用唯一日志器、correlation 与输出收尾。"""
    operation = args.policy_command
    stage_started = time.perf_counter()
    log.record(stage="policy", operation=operation, result="started")
    try:
        repository = FilePolicyRepository(PROJECT_ROOT)
        now = utc_now()
        if operation == "init":
            result = policy_usecases.init_pack(
                repository, args.dest, now=now, process_log=log
            )
        elif operation == "validate":
            result = policy_usecases.validate_pack(
                repository, args.pack, now=now, process_log=log
            )
        elif operation == "preview":
            result = policy_usecases.preview_pack(
                repository, args.pack, args.case, now=now, process_log=log
            )
        elif operation == "list":
            result = policy_usecases.list_packs(repository, now=now, process_log=log)
        elif operation == "snapshot":
            result = policy_usecases.snapshot_pack(
                repository, args.pack, now=now, process_log=log
            )
        elif operation == "restore":
            result = policy_usecases.restore_pack(
                repository,
                args.snapshot,
                args.dest,
                ProfilePolicyActivity(
                    PROJECT_ROOT, os_root=os_root, process_log=log
                ),
                now=now,
                process_log=log,
            )
        elif operation == "acceptance":
            if args.acceptance_command != "check":
                raise PolicyError("invalid_input")
            _pack_rel, base_files, _files, _guides, pack = (
                policy_usecases.load_active_pack(repository, args.pack, process_log=log)
            )
            result = acceptance_usecases.check_acceptance(
                FileEvalStore(PROJECT_ROOT),
                args.credential,
                base_files=base_files,
                pack=pack,
                provider_id=args.provider,
                model_id=args.model_id,
                model_config_hash=args.model_config_hash,
            )
        elif operation == "eval":
            bridge_types = {
                "external-agent": ExternalAgentBridge,
                "local-process": LocalProcessBridge,
            }
            bridge_type = bridge_types.get(args.provider)
            argv = [args.bridge_command, *args.arg]
            if bridge_type is None or any(
                "reference-provider" in item.lower() for item in argv
            ):
                raise PolicyError("invalid_provider")
            try:
                bridge = bridge_type(argv, model_id=args.model_id)
            except ValueError:
                raise PolicyError("invalid_provider") from None

            def authorize(plan: dict[str, object]) -> bool:
                """只有本轮显式确认才允许启动桥；模拟传输也走同一检查。"""
                del plan
                return bool(args.confirm_external_send)

            result = policy_eval_usecases.eval_pack(
                repository,
                FileEvalStore(PROJECT_ROOT),
                args.pack,
                args.suite,
                bridge,
                authorize,
                now=now,
                process_log=log,
            )
        elif operation == "holdout":
            eval_store = FileEvalStore(PROJECT_ROOT)
            if args.holdout_command == "register":
                result = policy_eval_usecases.register_holdout(
                    eval_store,
                    batch_id=args.batch_id,
                    version=args.version,
                    sample_count=args.sample_count,
                    annotator=args.annotator,
                    maintainer=args.maintainer,
                    frozen_at=args.frozen_at or now,
                    candidate_release=args.candidate_release,
                    process_log=log,
                )
            elif args.holdout_command == "retire":
                result = policy_eval_usecases.retire_holdout(
                    eval_store,
                    batch_id=args.batch_id,
                    retired_at=args.retired_at or now,
                    process_log=log,
                )
            else:
                raise PolicyError("invalid_input")
        else:
            raise PolicyError("invalid_input")
    except PolicyError as error:
        result_status = (
            "undetermined" if error.activity_state == "unknown" else "failed"
        )
        log.record(
            stage="policy",
            operation=operation,
            result=result_status,
            reason_code=error.reason_code,
            error_category="policy",
            duration_ms=elapsed_ms(stage_started),
        )
        return _finalize_output(
            log,
            started,
            _policy_error_payload(error.reason_code, error.issues),
            2,
            error.reason_code,
            "policy",
        )
    except Exception:
        log.record(
            stage="policy",
            operation=operation,
            result="failed",
            error_category="internal",
            duration_ms=elapsed_ms(stage_started),
        )
        return _finalize_output(
            log,
            started,
            _policy_error_payload("policy_io_error"),
            2,
            "policy_io_error",
            "internal",
        )
    log.record(
        stage="policy",
        operation=operation,
        result="ok",
        duration_ms=elapsed_ms(stage_started),
    )
    payload = {
        "schema_version": 1,
        "status": "ok",
        "reason_code": None,
        "result": result,
    }
    return _finalize_output(log, started, payload, 0, None, None)


def _release_gate(
    gate: SharedDeviceGate, log: ProcessLog, *, converged: bool
) -> DeviceGateError | None:
    """按收敛状态释放或隔离共享占用；日志由门禁适配器唯一记录。"""
    del log
    try:
        gate.release(now=utc_now(), converged=converged)
    except DeviceGateError as error:
        return error
    return None


def _device_snapshot(
    log: ProcessLog,
    started: float,
    args: argparse.Namespace,
    os_root: Path | None,
    *,
    mode: str | None,
) -> tuple[ProbeSnapshot | None, int | None]:
    """在共享占用下执行一次真实探针；未取得占用时不启动任何子进程。"""
    del mode
    profile_id = "m0-doctor"
    profile_path = profile_runtime_path(PROJECT_ROOT, profile_id)
    try:
        gate = SharedDeviceGate(
            profile_id=profile_id,
            profile_db_path=profile_path,
            os_root=os_root,
            process_log=log,
        )
        gate.acquire(
            profile_id=profile_id,
            profile_db_path=str(profile_path),
            owner=_gate_owner(),
            now=utc_now(),
        )
    except DeviceGateError as error:
        return None, _finalize_output(
            log,
            started,
            _error_payload(error.reason_code),
            2,
            error.reason_code,
            "gate",
        )

    stage_started = time.perf_counter()
    log.record(stage="probe", result="started")
    try:
        snapshot = AdbDeviceProbe(process_log=log, gate=gate).probe(args.serial)
    except DeviceProbeError as error:
        device_count = max(0, error.device_count)
        log.record(
            stage="probe",
            result="failed",
            reason_code=error.reason_code,
            error_category="probe",
            duration_ms=elapsed_ms(stage_started),
            device_count=device_count,
        )
        _release_gate(gate, log, converged=True)
        log.record(
            stage="decision",
            result="failed",
            reason_code=error.reason_code,
            error_category="probe",
            device_count=device_count,
        )
        return None, _finalize_output(
            log,
            started,
            build_result(
                status="failed",
                reason_code=error.reason_code,
                device_count=device_count,
                selected_alias=error.selected_alias,
                unlocked=None,
                foreground_app="unknown",
            ),
            2,
            error.reason_code,
            "probe",
        )
    except DeviceGateError as error:
        log.record(
            stage="probe",
            result="failed",
            reason_code=error.reason_code,
            error_category="gate",
            duration_ms=elapsed_ms(stage_started),
        )
        _release_gate(gate, log, converged=False)
        return None, _finalize_output(
            log,
            started,
            _error_payload(error.reason_code),
            2,
            error.reason_code,
            "gate",
        )
    except Exception:
        log.record(
            stage="probe",
            result="failed",
            error_category="internal",
            duration_ms=elapsed_ms(stage_started),
        )
        _release_gate(gate, log, converged=False)
        return None, _finalize_output(
            log,
            started,
            _error_payload("invalid_input"),
            2,
            "invalid_input",
            "internal",
        )
    log.record(
        stage="probe",
        result="ok",
        duration_ms=elapsed_ms(stage_started),
        device_count=len(snapshot.devices),
    )
    release_error = _release_gate(gate, log, converged=True)
    if release_error is not None:
        return None, _finalize_output(
            log,
            started,
            _error_payload(release_error.reason_code),
            2,
            release_error.reason_code,
            "gate",
        )
    return snapshot, None


def _finish_output_failure(log: ProcessLog, started: float) -> int:
    """输出通道不可用时记录 output=failed，并安全完成 exit。"""
    log.record(
        stage="output",
        result="failed",
        error_category="internal",
        exit_code=2,
    )
    return _finish(log, started, 2, None, "internal")


def _finalize_output(
    log: ProcessLog,
    started: float,
    payload: dict[str, object],
    exit_code: int,
    reason_code: str | None,
    error_category: str | None,
) -> int:
    """唯一 JSON 输出收尾：安全写出、记录 output 事件并完成 exit。"""
    output_started = time.perf_counter()
    if _emit(payload):
        log.record(
            stage="output",
            result="ok",
            duration_ms=elapsed_ms(output_started),
            exit_code=exit_code,
        )
        return _finish(log, started, exit_code, reason_code, error_category)
    return _finish_output_failure(log, started)


def main(
    argv: list[str] | None = None,
    *,
    process_log: ProcessLog | None = None,
    os_root: Path | None = None,
) -> int:
    """执行一次 CLI 命令并返回 0 或 2，不向 stderr 回显输入或异常。

    `os_root` 只供测试注入临时应用数据根；生产 CLI 不提供该参数，固定使用
    LOCALAPPDATA 下的共享设备门禁位置。
    """
    log = process_log if process_log is not None else ProcessLog()
    started = time.perf_counter()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    policy_invocation = _policy_command_token(raw_argv) == "policy"
    log.record(stage="start", result="started")

    try:
        args = _build_parser().parse_args(argv)
    except _CliArgumentError:
        log.record(
            stage="arguments",
            result="failed",
            reason_code="invalid_input",
            error_category="argument",
        )
        payload = (
            _policy_error_payload("invalid_input")
            if policy_invocation
            else _error_payload("invalid_input")
        )
        return _finalize_output(
            log,
            started,
            payload,
            2,
            "invalid_input",
            "argument",
        )
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else 2
        reason_code = None if code == 0 else "invalid_input"
        error_category = None if code == 0 else "argument"
        log.record(
            stage="arguments",
            result="ok" if code == 0 else "failed",
            reason_code=reason_code,
            error_category=error_category,
            exit_code=code,
        )
        # argparse 已直接写出帮助/用法；这里只记录其输出路径，不伪造 doctor JSON。
        log.record(
            stage="output",
            result="ok" if code == 0 else "failed",
            reason_code=None,
            error_category=None if code == 0 else "internal",
            exit_code=code,
        )
        _finish(log, started, code, reason_code, error_category)
        raise

    log.record(stage="arguments", result="ok")
    config_source = "file" if args.config is not None else "default"
    try:
        _validate_config(args.config)
    except _ConfigError:
        log.record(
            stage="config",
            result="failed",
            reason_code="invalid_config",
            error_category="config",
            config_source=config_source,
        )
        payload = (
            _policy_error_payload("invalid_config")
            if policy_invocation
            else _error_payload("invalid_config")
        )
        return _finalize_output(
            log,
            started,
            payload,
            2,
            "invalid_config",
            "config",
        )
    except Exception:
        log.record(
            stage="config",
            result="failed",
            reason_code=None,
            error_category="internal",
            config_source=config_source,
        )
        payload = (
            _policy_error_payload("invalid_config")
            if policy_invocation
            else _error_payload("invalid_config")
        )
        return _finalize_output(
            log,
            started,
            payload,
            2,
            "invalid_config",
            "internal",
        )
    log.record(stage="config", result="ok", config_source=config_source)

    if args.command == "store":
        return _run_store_init(log, started, args)
    if args.command == "sense":
        return _run_sense(log, started, args)
    if args.command == "read":
        return _run_read(log, started, args)
    if args.command == "facts":
        return _run_facts(log, started, args)
    if args.command == "run":
        return _run_run(log, started, args, os_root)
    if args.command == "policy":
        return _run_policy(log, started, args, os_root)

    mode = _requested_mode(args)
    if not _mode_is_valid(args):
        log.record(
            stage="mode",
            result="failed",
            reason_code="invalid_input",
            error_category="argument",
            mode=mode,
            config_source=config_source,
        )
        return _finalize_output(
            log,
            started,
            _error_payload("invalid_input"),
            2,
            "invalid_input",
            "argument",
        )
    log.record(stage="mode", result="ok", mode=mode, config_source=config_source)

    snapshot: ProbeSnapshot
    if args.fixture is not None:
        stage_started = time.perf_counter()
        log.record(
            stage="fixture",
            result="started",
            mode=mode,
            config_source=config_source,
        )
        try:
            snapshot = _load_fixture(args.fixture)
        except _FixtureError:
            log.record(
                stage="fixture",
                result="failed",
                reason_code="invalid_fixture",
                error_category="fixture",
                duration_ms=elapsed_ms(stage_started),
                mode=mode,
                config_source=config_source,
            )
            return _finalize_output(
                log,
                started,
                _error_payload("invalid_fixture"),
                2,
                "invalid_fixture",
                "fixture",
            )
        except Exception:
            log.record(
                stage="fixture",
                result="failed",
                reason_code=None,
                error_category="internal",
                duration_ms=elapsed_ms(stage_started),
                mode=mode,
                config_source=config_source,
            )
            return _finalize_output(
                log,
                started,
                _error_payload("invalid_input"),
                2,
                "invalid_input",
                "internal",
            )
        log.record(
            stage="fixture",
            result="ok",
            duration_ms=elapsed_ms(stage_started),
            mode=mode,
            config_source=config_source,
        )
    else:
        snapshot, early_exit = _device_snapshot(
            log, started, args, os_root, mode=mode
        )
        if early_exit is not None:
            return early_exit
        assert snapshot is not None

    try:
        result = run_doctor(snapshot)
    except Exception:
        log.record(
            stage="decision",
            result="failed",
            reason_code=None,
            error_category="internal",
            mode=mode,
            config_source=config_source,
        )
        return _finalize_output(
            log,
            started,
            _error_payload("invalid_input"),
            2,
            "invalid_input",
            "internal",
        )

    status = result["status"]
    exit_code = 0 if status == "ok" else 2
    reason_code = result["reason_code"]
    error_category = None if status == "ok" else "business"
    log.record(
        stage="decision",
        result=status,
        reason_code=reason_code,
        error_category=error_category,
        mode=mode,
        config_source=config_source,
        device_count=result["device_count"],
    )
    return _finalize_output(
        log,
        started,
        result,
        exit_code,
        reason_code,
        error_category,
    )
