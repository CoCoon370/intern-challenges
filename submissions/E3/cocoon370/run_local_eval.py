"""不使用 Docker 的本地端到端评测器。

它复用官方 mock 和 rules.py，只把“docker run”替换为本机 Python + Edge。
因此可以验证页面操作和 R1-R6；容器、Linux 网络和镜像构建仍需之后验证。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
TASK_DIR = REPO / "tasks" / "E3-wechat-autoreply"
EVAL_DIR = TASK_DIR / "eval"
sys.path.insert(0, str(EVAL_DIR))
import rules  # noqa: E402


def get_json(url: str, timeout: float = 3.0):
    """读取 mock 的公开管理日志。"""

    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def post(url: str, timeout: float = 3.0) -> None:
    """调用 mock 的重置接口。"""

    request = urllib.request.Request(url, method="POST", data=b"")
    with urllib.request.urlopen(request, timeout=timeout):
        pass


def choose_free_port() -> int:
    """为本地 Demo 选择空闲端口，避免旧窗口占用 8765 导致串剧本。"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_ready(base: str, process: subprocess.Popen, seconds: float = 30.0) -> None:
    """等待 mock 可访问，若进程提前退出则立即失败。"""

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"mock 提前退出，代码 {process.returncode}")
        try:
            get_json(f"{base}/_admin/log")
            return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.2)
    raise RuntimeError("等待 mock 启动超时")


def wait_first_connection(base: str, process: subprocess.Popen, seconds: float = 30.0) -> float:
    """等待机器人通过真实浏览器连接页面。"""

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"机器人提前退出，代码 {process.returncode}")
        events = get_json(f"{base}/_admin/log")
        if any(event.get("type") == "ws_connect" for event in events):
            return time.monotonic()
        time.sleep(0.2)
    raise RuntimeError("机器人没有在规定时间内连接页面")


def result_summary(events: list[dict], scenario: dict) -> dict:
    """把官方规则结果整理成便于阅读的摘要。"""

    result = rules.evaluate(events, scenario)
    return {
        "score": rules.score(result),
        "hard_violations": rules.hard_violations(result),
        "flags": rules.flags(result),
        "R1": result["R1"],
        "R2": result["R2"],
        "R3": result["R3"],
        "R4": result["R4"],
        "R5": result["R5"],
        "R6": result["R6"],
    }


def stop_process(process: subprocess.Popen | None) -> None:
    """尽量正常停止本地子进程，超时后再强制结束。"""

    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description="E3 无 Docker 本地评测")
    parser.add_argument("--scenario", default=str(HERE / "scenario-demo.json"))
    parser.add_argument("--port", type=int, default=0, help="本地端口；默认自动选择空闲端口")
    parser.add_argument("--headed", action="store_true", help="显示机器人控制的 Edge 窗口")
    parser.add_argument("--reply-pause", type=float, default=0.0, help="每次回复后在当前会话停留几秒，仅用于观察")
    parser.add_argument("--grace", type=float, default=5.0, help="剧本结束后的回复等待时间")
    parser.add_argument("--out", default=str(HERE / "local-out"))
    args = parser.parse_args()

    scenario_path = Path(args.scenario).resolve()
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    for filename in (
        "decisions.jsonl", "customer_notes.json", "events.json", "result.json", "mock.log", "bot.log"
    ):
        path = out_dir / filename
        if path.exists():
            path.unlink()

    port = args.port or choose_free_port()
    base = f"http://127.0.0.1:{port}"
    mock_process: subprocess.Popen | None = None
    bot_process: subprocess.Popen | None = None
    mock_log = (out_dir / "mock.log").open("w", encoding="utf-8")
    bot_log = (out_dir / "bot.log").open("w", encoding="utf-8")

    try:
        mock_process = subprocess.Popen(
            [
                sys.executable,
                str(TASK_DIR / "mock" / "mock.py"),
                "--port", str(port),
                "--host", "127.0.0.1",
                "--scenario", str(scenario_path),
            ],
            cwd=str(TASK_DIR),
            stdout=subprocess.DEVNULL,
            stderr=mock_log,
        )
        wait_ready(base, mock_process)
        post(f"{base}/_admin/reset")

        environment = os.environ.copy()
        environment.update({
            "WECHAT_URL": base,
            "OUT_DIR": str(out_dir),
            "BROWSER_CHANNEL": "msedge",
            "HEADLESS": "false" if args.headed else "true",
            "DEMO_REPLY_PAUSE_SEC": str(max(0.0, args.reply_pause)),
            "PYTHONUNBUFFERED": "1",
        })
        bot_process = subprocess.Popen(
            [sys.executable, str(HERE / "bot.py")],
            cwd=str(HERE),
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=bot_log,
        )

        connected_at = wait_first_connection(base, bot_process)
        wait_seconds = float(scenario.get("duration_sec", 0)) + args.grace
        while time.monotonic() - connected_at < wait_seconds:
            if bot_process.poll() is not None:
                raise RuntimeError(f"机器人运行中退出，代码 {bot_process.returncode}")
            time.sleep(0.25)

        events = get_json(f"{base}/_admin/log")
        summary = result_summary(events, scenario)
        (out_dir / "events.json").write_text(
            json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out_dir / "result.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1 if summary["hard_violations"] or summary["score"] < 0.5 else 0
    except Exception as error:  # noqa: BLE001 - 本地入口需要给出完整失败原因。
        print(json.dumps({"error": repr(error)}, ensure_ascii=False, indent=2))
        return 1
    finally:
        stop_process(bot_process)
        stop_process(mock_process)
        bot_log.close()
        mock_log.close()


if __name__ == "__main__":
    raise SystemExit(main())
