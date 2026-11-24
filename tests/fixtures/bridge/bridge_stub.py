"""M4b 测试专用本地桥桩：只读写 stdin/stdout 与临时捕获文件，不联网。"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    """按固定模式返回合成 Decision 或固定桥错误，供适配器/评测测试使用。"""
    mode = argv[0] if argv else "ok"
    if mode == "fork-hold":
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(0.8)"],
            stdout=sys.stdout,
            stderr=subprocess.DEVNULL,
        )
        return 0
    if mode == "huge":
        sys.stdout.buffer.write(b"x" * (2 * 1024 * 1024))
        sys.stdout.buffer.flush()
        return 0
    if mode == "slow":
        time.sleep(5)
        return 0
    if mode == "no-read":
        time.sleep(5)
        return 0
    if mode == "stderr-huge":
        sys.stderr.write("y" * (2 * 1024 * 1024))
        sys.stderr.flush()
    request = json.loads(sys.stdin.read())
    if not isinstance(request, dict):
        return 2
    echo = request.get("echo")
    messages = request.get("messages")
    if not isinstance(echo, dict) or not isinstance(messages, list):
        return 2
    capture = Path(argv[2]) if len(argv) > 2 and argv[2] else None
    if capture is not None:
        with capture.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(request, ensure_ascii=False) + "\n")
    protocol = request.get("protocol")
    content_key = "output" if protocol == "m4b-agent-v1" else "content"
    if mode.startswith("error:"):
        code = mode.split(":", 1)[1]
        sys.stdout.write(
            json.dumps(
                {"protocol": protocol, "error": {"code": code}},
                ensure_ascii=False,
            )
            + "\n"
        )
        return 3
    if mode == "bad-json":
        sys.stdout.write("{not-json\n")
        return 0
    if mode == "roles":
        sys.stdout.write(
            json.dumps(
                {
                    "protocol": protocol,
                    "roles": ["system", "user"],
                    content_key: "{}",
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        return 0
    if mode == "duplicate":
        sys.stdout.write(
            '{"protocol":"%s","protocol":"%s","roles":["system","developer","user"],"%s":"{}"}\n'
            % (protocol, protocol, content_key)
        )
        return 0
    decisions_path = Path(argv[1])
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    spec = decisions[echo["task_id"]]
    user_data = json.loads(messages[2]["content"])
    if not isinstance(user_data, dict):
        return 2
    inbound = str(user_data.get("inbound", ""))
    nonce = echo["nonce"]
    if mode == "bad-echo":
        nonce = "00000000-0000-4000-8000-000000000000"
    decision = {
        "schema_version": 1,
        "task_id": echo["task_id"],
        "nonce": nonce,
        "input_hash": echo["input_hash"],
        "intent": spec["intent"],
        "sub_intents": spec["sub_intents"],
        "reply": spec["reply"],
        "evidence": inbound[:2],
        "escalate": spec["escalate"],
    }
    sys.stdout.write(
        json.dumps(
            {
                "protocol": protocol,
                "roles": ["system", "developer", "user"],
                content_key: json.dumps(decision, ensure_ascii=False),
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
