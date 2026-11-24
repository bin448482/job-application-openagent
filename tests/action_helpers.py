"""提供 M5 动作测试共用的临时项目根、合成验收凭证、seed 与动作脚本。"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

from app.adapters.files.evals import FileEvalStore
from app.domain.models import add_seconds, canonical_hash
from app.domain.policy import utc_now
from app.ports.decision import BridgeDescriptor
from app.usecases import policy_eval as eval_usecases
from tests import policy_helpers
from tests.test_policy_eval import SimulatedBridge, public_decisions

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-11-24T00:00:00.000Z"
BEFORE = "11111111-1111-4111-8111-111111111111"
AFTER = "22222222-2222-4222-8222-222222222222"
IDENTITY = ("合成联系人", "合成公司", "合成职位")


class AcceptanceBridge(SimulatedBridge):
    """把模拟 transport 报告为非 simulated，用于生成动作前当前验收。"""

    def descriptor(self) -> BridgeDescriptor:
        """返回 simulated=False 的固定摘要。"""
        return replace(super().descriptor(), simulated=False)


def temp_project():
    """创建含公开 base/示例/套件/合成动作与 sensor 夹具的临时项目根。"""
    tmp = tempfile.TemporaryDirectory(dir=ROOT, prefix=".m5-action-")
    root = Path(tmp.name)
    for rel in (
        "prompts/base",
        "prompts/examples",
        "evals/policy-suite",
        "tests/fixtures/actions",
    ):
        shutil.copytree(ROOT / rel, root / rel)
    return tmp, root


def init_pack(root: Path, rel: str = "prompts/local/demo") -> None:
    """用公开示例初始化一个本机策略包，并开放公开套件所需的两个事实键。"""
    code, payload, stderr = policy_helpers.run_cli(
        root, ["policy", "init", "--dest", rel]
    )
    if code != 0 or stderr != "":
        raise AssertionError(payload)
    pack = root / rel / "pack.toml"
    pack.write_bytes(
        pack.read_bytes()
        .decode("utf-8")
        .replace(
            "allow_keys = []",
            'allow_keys = ["work_location", "salary_expectation"]',
        )
        .encode("utf-8")
    )


def credential(root: Path, pack_rel: str = "prompts/local/demo") -> tuple[str, str]:
    """运行一次成功公开评测并返回凭证相对路径与 model_config_hash。"""
    bridge = AcceptanceBridge(public_decisions())
    result = eval_usecases.eval_pack(
        policy_helpers.repository(root),
        FileEvalStore(root),
        pack_rel,
        "public",
        bridge,
        lambda plan: True,
        now=NOW,
    )
    return str(result["acceptance_path"]), bridge.descriptor().model_config_hash


def seed_fixture(
    root: Path, *, profile_id: str = "demo", observed_at: str | None = None
) -> str:
    """在临时根写一份 profile 匹配的合成 M2 未读夹具，返回相对 manifest 路径。"""
    target = root / "tests" / "fixtures" / "actions" / "seed"
    target.mkdir(parents=True, exist_ok=True)
    profile = json.loads((ROOT / "tests/fixtures/sensor/profile.json").read_text("utf-8"))
    profile["profile_id"] = profile_id
    (target / "profile.json").write_text(
        json.dumps(profile, ensure_ascii=False), encoding="utf-8"
    )
    shutil.copyfile(
        ROOT / "tests/fixtures/sensor/positive.xml", target / "positive.xml"
    )
    manifest = {
        "profile_path": "profile.json",
        "xml_paths": ["positive.xml"],
        "app_version": profile["version"],
        "observed_at": observed_at or utc_now(),
        "baseline_rows": [],
        "coverage_proof": {
            "kind": "viewport",
            "top_proven": False,
            "bottom_proven": False,
        },
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return "tests/fixtures/actions/seed/manifest.json"


def observation(**overrides: object) -> dict[str, object]:
    """构造一个文本发送 performed 的合成观察，允许按用例覆盖字段。"""
    data: dict[str, object] = {
        "before_snapshot_id": BEFORE,
        "after_snapshot_id": AFTER,
        "observed_at": NOW,
        "clicked": True,
        "error": None,
        "scene_settled": True,
        "command_quiet": True,
        "confirmation_open": False,
        "input_box_empty": True,
        "ime_original": "synthetic-ime-default",
        "ime_current": "synthetic-ime-default",
        "body_readback_matches": True,
        "echo_count_before": 0,
        "echo_count_after": 1,
        "attachment_name_matched": False,
        "attachment_request_observed": False,
        "delivery_observed": False,
        "contact_slot_available": False,
        "contact_slot_status": None,
        "identity_unique": False,
        "target_present_before": False,
        "target_present_after": False,
        "anchor_present_after": False,
    }
    data.update(overrides)
    return data


def write_action_fixture(
    root: Path, rel: str, spec: dict[str, object]
) -> str:
    """写入一份合成动作脚本并返回项目内相对路径。"""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(spec, ensure_ascii=False), encoding="utf-8"
    )
    return rel


def model_identity() -> tuple[str, str, str]:
    """返回与 AcceptanceBridge 一致的 provider/model/model_config_hash。"""
    bridge = AcceptanceBridge(public_decisions())
    descriptor = bridge.descriptor()
    return descriptor.provider_id, descriptor.model_id, descriptor.model_config_hash


def authorization_expiry(now: str = NOW) -> str:
    """返回比 now 晚 300 秒的本轮授权截止。"""
    return add_seconds(now, 300)


def synthetic_text_key() -> str:
    """返回一个形状合法的稳定消息键，仅用于合成动作目标。"""
    return "boss:msg:" + canonical_hash({"synthetic": "message"})


def run_action_cli(
    root: Path,
    argv: list[str],
    *,
    os_root: Path,
    stop_checker: object | None = None,
):
    """在临时项目根运行 CLI，可注入测试 OS 根与合成停止判据。"""
    import io
    from contextlib import redirect_stderr, redirect_stdout
    from unittest.mock import patch

    from app.cli.main import main

    out = io.StringIO()
    err = io.StringIO()
    kwargs: dict[str, object] = {"os_root": os_root}
    if stop_checker is not None:
        kwargs["stop_checker"] = stop_checker
    with patch("app.cli.main.PROJECT_ROOT", root), redirect_stdout(out), redirect_stderr(err):
        code = main(argv, **kwargs)  # type: ignore[arg-type]
    text = out.getvalue().strip()
    payload = json.loads(text) if text else None
    return code, payload, err.getvalue()
