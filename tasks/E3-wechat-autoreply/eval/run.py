# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
E3 · 评测驱动

    uv run eval/run.py --image <img> --mode public|hidden [--scenario <path>] [--mock-port 8765] [--net-mode auto|host|bridge]

流程：起 mock → 建临时 /out → docker run 候选人镜像 → 等剧本跑完 + 35s 回复窗口
     → 读 /_admin/log → rules.py 判定 → stdout 最后一行输出统一 JSON → 清理容器与 mock。

网络（--net-mode，缺省 auto = Linux 走 host、其它走 bridge）：
    host    `--network host`，mock 绑 127.0.0.1，WECHAT_URL=http://127.0.0.1:<port>      （CI / Linux）
    bridge  不用 host 网络，mock 绑 0.0.0.0，WECHAT_URL=http://host.docker.internal:<port>（Docker Desktop for Mac/Win）

退出码：硬违规或 score < 0.5 → 1；否则 0。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK_DIR = HERE.parent
sys.path.insert(0, str(HERE))
import rules  # noqa: E402

GRACE_SEC = 35  # 最后一条消息的回复窗口（30s）+ 余量
FIRST_CONNECT_DEADLINE_SEC = 60
MOCK_READY_DEADLINE_SEC = 90
POLL_SEC = 2


def eprint(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def http_get_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post(url: str, timeout: float = 5.0) -> None:
    req = urllib.request.Request(url, method="POST", data=b"")
    with urllib.request.urlopen(req, timeout=timeout):
        pass


def wait_mock_ready(base: str, proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + MOCK_READY_DEADLINE_SEC
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"mock exited early with code {proc.returncode}")
        try:
            http_get_json(f"{base}/_admin/log", timeout=2)
            return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.5)
    raise RuntimeError("mock did not become ready in time")


def docker(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=check, capture_output=capture, text=True)


def build_output(mode: str, result: dict | None, hard: list[str], flg: list[str], extra_why: str = "") -> dict:
    details: list[dict] = []
    if result is None:
        sc = 0.0
        passed, total = 0, 6
        details.append({"id": "R1", "ok": False, "why": extra_why})
    else:
        sc = rules.score(result)
        r = result
        r1 = r["R1"]
        details = [
            {"id": "R1", "ok": r1["ok"], "why": f"coverage {r1['covered']}/{r1['required']} = {r1['coverage']:.2f}"},
            {"id": "R2", "ok": r["R2"]["ok"], "why": (
                f"{len(r['R2']['violations'])} violation(s): " + ", ".join(sorted({v["why"] for v in r["R2"]["violations"]}))
                if r["R2"]["violations"] else "0 violation(s)"
            )},
            {"id": "R3", "ok": r["R3"]["ok"], "why": f"{len(r['R3']['violations'])} reply(ies) with price"},
            {"id": "R4", "ok": r["R4"]["ok"], "why": f"{len(r['R4']['violations'])} conversation(s) replied after handoff"},
            {"id": "R5", "ok": r["R5"]["ok"], "why": f"{len(r['R5']['violations'])} popup(s) never closed"},
            {"id": "R6", "ok": r["R6"]["ok"], "why": f"{r['R6']['connections']} ws connection(s), {len(r['R6']['suspicious_user_agents'])} without '{rules.R6_UA_MARK}'"},
        ]
        if mode == "public":
            # 公开模式把违规细节带上，方便候选人本地定位
            if r1["missed"]:
                details[0]["missed"] = r1["missed"]
            for idx, key in ((1, "R2"), (2, "R3"), (3, "R4"), (4, "R5")):
                if r[key]["violations"]:
                    details[idx]["violations"] = r[key]["violations"]
            if r["R6"]["suspicious_user_agents"]:
                details[5]["suspicious_user_agents"] = r["R6"]["suspicious_user_agents"]
            meta = r.get("_meta")
            if meta and meta["delivered_msg_in"] != meta["expected_msg_in"]:
                details[0]["note"] = f"scenario delivered {meta['delivered_msg_in']}/{meta['expected_msg_in']} msg_in"
        passed = sum(1 for d in details if d["ok"])
        total = len(details)
    return {
        "task": "E3",
        "mode": mode,
        "score": sc,
        "passed": passed,
        "total": total,
        "hard_violations": hard,
        "flags": flg,
        "details": details,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="E3 评测驱动")
    ap.add_argument("--image", required=True, help="候选人 docker 镜像")
    ap.add_argument("--mode", choices=["public", "hidden"], default="public")
    ap.add_argument("--scenario", default=str(TASK_DIR / "scenarios" / "public.json"))
    ap.add_argument("--mock-port", type=int, default=8765)
    ap.add_argument("--out-dir", default=None, help="保留 /out（默认用临时目录，跑完删除）")
    ap.add_argument("--net-mode", choices=["auto", "host", "bridge"], default="auto")
    args = ap.parse_args()

    scenario_path = Path(args.scenario).resolve()
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    duration = float(scenario.get("duration_sec", 0))
    base = f"http://127.0.0.1:{args.mock_port}"  # 评测脚本自己读 mock 永远走本机回环
    name = f"e3-eval-{os.getpid()}"

    net_mode = args.net_mode if args.net_mode != "auto" else ("host" if platform.system() == "Linux" else "bridge")
    if net_mode == "host":
        mock_host = "127.0.0.1"
        wechat_url = f"http://127.0.0.1:{args.mock_port}"
        net_args = ["--network", "host"]
    else:
        mock_host = "0.0.0.0"
        wechat_url = f"http://host.docker.internal:{args.mock_port}"
        net_args = ["--add-host", "host.docker.internal:host-gateway"]

    mock: subprocess.Popen | None = None
    out_dir: Path | None = None
    keep_out = args.out_dir is not None
    container_started = False
    output: dict | None = None
    exit_code = 1

    try:
        # 1. mock
        mock_log = tempfile.NamedTemporaryFile(prefix="e3-mock-", suffix=".log", delete=False)
        mock = subprocess.Popen(
            ["uv", "run", str(TASK_DIR / "mock" / "mock.py"), "--port", str(args.mock_port), "--host", mock_host, "--scenario", str(scenario_path)],
            stdout=subprocess.DEVNULL,
            stderr=mock_log,
        )
        wait_mock_ready(base, mock)
        http_post(f"{base}/_admin/reset")
        eprint(f"[eval] mock ready at {base} (stderr → {mock_log.name}); net-mode={net_mode}, container sees WECHAT_URL={wechat_url}")

        # 2. /out
        out_dir = Path(args.out_dir).resolve() if keep_out else Path(tempfile.mkdtemp(prefix="e3-out-"))
        out_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(out_dir, 0o777)

        # 3. container
        docker(
            "run", "-d", "--name", name, *net_args,
            "-e", f"WECHAT_URL={wechat_url}", "-v", f"{out_dir}:/out", args.image,
        )
        container_started = True
        eprint(f"[eval] container {name} started; waiting for first ws_connect (≤{FIRST_CONNECT_DEADLINE_SEC}s)")

        # 4. wait for first ws_connect
        t_start = time.monotonic()
        first_connect_at: float | None = None
        while time.monotonic() - t_start < FIRST_CONNECT_DEADLINE_SEC:
            events = http_get_json(f"{base}/_admin/log")
            if any(e.get("type") == "ws_connect" for e in events):
                first_connect_at = time.monotonic()
                break
            time.sleep(POLL_SEC)
        if first_connect_at is None:
            output = build_output(args.mode, None, ["no_ui_connection"], [], "no ws_connect within 60s")
            return 1

        # 5. wait for the script + grace window（不提前结束：容器早退也要等满，避免用「少收消息」抬高覆盖率）
        deadline = first_connect_at + duration + GRACE_SEC
        eprint(f"[eval] ui connected; script runs {duration:.0f}s + {GRACE_SEC}s grace")
        last_report = 0.0
        while time.monotonic() < deadline:
            time.sleep(POLL_SEC)
            elapsed = time.monotonic() - first_connect_at
            if elapsed - last_report >= 30:
                last_report = elapsed
                eprint(f"[eval] t={elapsed:.0f}s")

        # 6. judge
        events = http_get_json(f"{base}/_admin/log")
        result = rules.evaluate(events, scenario)
        hard = rules.hard_violations(result)
        flg = rules.flags(result)
        output = build_output(args.mode, result, hard, flg)
        if keep_out and args.mode == "public":
            (out_dir / "eval-events.json").write_text(json.dumps(events, ensure_ascii=False, indent=1), encoding="utf-8")
        exit_code = 1 if (hard or output["score"] < 0.5) else 0
        return exit_code
    except Exception as exc:  # noqa: BLE001
        eprint(f"[eval] error: {exc!r}")
        output = build_output(args.mode, None, ["eval_error"], [], repr(exc))
        return 1
    finally:
        if container_started:
            if args.mode == "public":
                logs = docker("logs", "--tail", "40", name, check=False)
                eprint("[eval] ---- container stderr (tail 40) ----")
                eprint(logs.stderr.rstrip() or "(empty)")
                eprint("[eval] ---- end ----")
            docker("rm", "-f", name, check=False)
        if mock is not None and mock.poll() is None:
            mock.terminate()
            try:
                mock.wait(timeout=10)
            except subprocess.TimeoutExpired:
                mock.kill()
        if out_dir is not None and not keep_out:
            shutil.rmtree(out_dir, ignore_errors=True)
        if output is not None:
            print(json.dumps(output, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    sys.exit(main())
