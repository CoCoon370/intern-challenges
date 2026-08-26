#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastapi>=0.115,<1",
#   "uvicorn>=0.30,<1",
# ]
# ///
"""
E2 评测 driver。

    uv run eval/run.py --image <img> --mode public|hidden [--scenario <path>]
                       [--mock-port 8600] [--app-port 8700] [--net-mode auto|host|bridge]

流程：起 mock（../mock/mock.py）→ docker run 候选人镜像 → 等 /healthz ≤60s → 按 scenario
发 webhook → 等到 deadline_sec → 读 /_admin/state → 逐单五项检查 → stdout 最后一行输出
结果 JSON（其它输出全部走 stderr）→ 无论成败都清理容器与 mock。

退出码：硬违规或 score < 0.5 → 1；否则 0。
（fastapi/uvicorn 只是为了让 mock 子进程复用本解释器的环境，driver 自身只用标准库。）
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
TASK_DIR = SCRIPT_DIR.parent
MOCK_PY = TASK_DIR / "mock" / "mock.py"
DEFAULT_SCENARIO = TASK_DIR / "scenarios" / "public.json"

WEBHOOK_LIMIT_SEC = 2.0  # 规范：每次 webhook 响应 ≤2s
WEBHOOK_CLIENT_TIMEOUT = 3.0  # 调用方 3s 断开
HEALTHZ_WAIT_SEC = 60.0
MOCK_WAIT_SEC = 30.0
MIN_POLL_GAP_SEC = 0.5  # 规范：同一 job 的 GET 间隔 ≥0.5s
POLL_GAP_TOLERANCE = 0.05
MAX_JOBS_PER_ORDER = 3
DUPLICATE_SEND_GAP_SEC = 0.5

SUCCEED_BEHAVIORS = {"normal", "cold_start", "transient_500", "submit_503_once"}
FAIL_BEHAVIORS = {"permanent_fail", "hang"}

# 不走任何代理：全部是 127.0.0.1
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr, flush=True)


class EvalAbort(Exception):
    """硬失败：整场评测无法进行（容器起不来、mock 起不来……）。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


# --------------------------------------------------------------------------- http
def http(method: str, url: str, body: Any = None, timeout: float = 5.0) -> tuple[int, Any]:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    if not raw:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except Exception:
        return status, raw.decode("utf-8", "replace")


def wait_http_ok(url: str, total: float, what: str, alive: Any = None) -> float:
    """轮询 url 直到 2xx；返回耗时。alive() 返回 False 时提前失败。"""
    t0 = time.monotonic()
    last_err = ""
    while time.monotonic() - t0 < total:
        if alive is not None and not alive():
            raise EvalAbort(f"{what}_died", last_err)
        try:
            status, _ = http("GET", url, timeout=2.0)
            if 200 <= status < 300:
                return time.monotonic() - t0
            last_err = f"HTTP {status}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(0.5)
    raise EvalAbort(f"{what}_not_ready", f"{url} not 2xx within {total:.0f}s ({last_err})")


# --------------------------------------------------------------------------- docker
def sh(cmd: list[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def wait_docker() -> None:
    """OrbStack / Docker Desktop 刚启动时 daemon 要几秒才可用——重试。"""
    last = ""
    for attempt in range(8):
        try:
            r = sh(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20)
            if r.returncode == 0:
                return
            last = (r.stderr or r.stdout).strip()
        except FileNotFoundError:
            raise EvalAbort("docker_not_found", "docker CLI not on PATH")
        except subprocess.TimeoutExpired:
            last = "docker info timed out"
        eprint(f"[eval] docker daemon not ready ({last.splitlines()[-1] if last else '?'}), retry {attempt + 1}/8")
        time.sleep(4)
    raise EvalAbort("docker_not_ready", last)


def container_running(name: str) -> bool:
    r = sh(["docker", "inspect", "-f", "{{.State.Running}}", name], timeout=20)
    return r.returncode == 0 and r.stdout.strip() == "true"


def container_logs_tail(name: str, n: int = 30) -> tuple[str, str]:
    r = sh(["docker", "logs", "--tail", str(n), name], timeout=30)
    return r.stdout, r.stderr


# --------------------------------------------------------------------------- scenario
def load_scenario(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    orders = data.get("orders")
    if not isinstance(orders, list) or not orders:
        raise SystemExit(f"scenario {path}: 'orders' must be a non-empty list")
    for o in orders:
        if not isinstance(o.get("order_id"), str):
            raise SystemExit(f"scenario {path}: bad order {o}")
        b = o.get("behavior", "normal")
        if b not in SUCCEED_BEHAVIORS | FAIL_BEHAVIORS:
            raise SystemExit(f"scenario {path}: unknown behavior {b!r}")
    data.setdefault("deadline_sec", 120)
    return data


def webhook_payload(order: dict[str, Any]) -> dict[str, Any]:
    oid = order["order_id"]
    return {
        "order_id": oid,
        "scene": order.get("scene", "living-room"),
        "image_urls": order.get("image_urls", [
            f"https://img.example.invalid/{oid}/1.jpg",
            f"https://img.example.invalid/{oid}/2.jpg",
        ]),
    }


# --------------------------------------------------------------------------- webhook sending
def send_webhooks(orders: list[dict[str, Any]], app_port: int, verbose: bool) -> list[dict[str, Any]]:
    url = f"http://127.0.0.1:{app_port}/webhook/order"
    results: list[dict[str, Any]] = []
    lock = threading.Lock()
    t_start = time.monotonic()

    def one(order: dict[str, Any], attempt: int) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "order_id": order["order_id"], "attempt": attempt,
            "t": round(time.monotonic() - t_start, 3),
            "status": None, "elapsed": None, "timed_out": False, "error": None,
        }
        t0 = time.perf_counter()
        try:
            status, _ = http("POST", url, webhook_payload(order), timeout=WEBHOOK_CLIENT_TIMEOUT)
            rec["status"] = status
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            rec["error"] = msg
            if "timed out" in msg.lower() or isinstance(e, TimeoutError):
                rec["timed_out"] = True
        rec["elapsed"] = round(time.perf_counter() - t0, 3)
        if rec["elapsed"] >= WEBHOOK_CLIENT_TIMEOUT:
            rec["timed_out"] = True
        return rec

    def worker(order: dict[str, Any]) -> None:
        offset = float(order.get("send_at_offset_sec", 0) or 0)
        remaining = (t_start + offset) - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        times = max(1, int(order.get("send_times", 1) or 1))
        for i in range(1, times + 1):
            if i > 1:
                time.sleep(DUPLICATE_SEND_GAP_SEC)
            rec = one(order, i)
            with lock:
                results.append(rec)
            if verbose:
                eprint(f"[eval] webhook {order['order_id']} #{i}: "
                       f"status={rec['status']} elapsed={rec['elapsed']}s"
                       + (f" error={rec['error']}" if rec["error"] else ""))

    threads = [threading.Thread(target=worker, args=(o,), daemon=True) for o in orders]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    results.sort(key=lambda r: (r["t"], r["order_id"], r["attempt"]))
    return results


# --------------------------------------------------------------------------- judging
def judge_order(order: dict[str, Any], webhooks: list[dict[str, Any]], state: dict[str, Any]) -> dict[str, Any]:
    oid = order["order_id"]
    behavior = order.get("behavior", "normal")
    send_times = max(1, int(order.get("send_times", 1) or 1))
    fails: list[str] = []
    hard: list[str] = []
    notes: list[str] = []

    def fail(cat: str, note: str, is_hard: bool = False) -> None:
        if cat not in fails:
            fails.append(cat)
        if is_hard and cat not in hard:
            hard.append(cat)
        notes.append(note)

    # ① 响应及时：每次 webhook ≤2s 且 2xx
    attempts = [w for w in webhooks if w["order_id"] == oid]
    if len(attempts) != send_times:
        notes.append(f"expected {send_times} webhook sends, driver recorded {len(attempts)}")
    for w in attempts:
        if w["timed_out"] or (w["elapsed"] is not None and w["elapsed"] > WEBHOOK_LIMIT_SEC):
            fail("webhook_timeout", f"webhook #{w['attempt']} took {w['elapsed']}s (limit {WEBHOOK_LIMIT_SEC}s)", True)
        elif w["status"] is None or not (200 <= w["status"] < 300):
            fail("webhook_status", f"webhook #{w['attempt']} -> {w['status'] or w['error']}")

    # ② 通知唯一
    notifs = [n for n in state["notifications"]
              if isinstance(n.get("body"), dict) and n["body"].get("order_id") == oid]
    if not notifs:
        fail("no_notification", "no notification received")
    elif len(notifs) > 1:
        fail("duplicate_notification", f"{len(notifs)} notifications", True)

    # ③ 结果正确
    if notifs:
        body = notifs[0]["body"]
        expected = "succeeded" if behavior in SUCCEED_BEHAVIORS else "failed"
        got = body.get("status")
        if got != expected:
            fail("wrong_result", f"status={got!r}, expected {expected!r}")
        elif expected == "succeeded":
            url = body.get("result_url")
            if not isinstance(url, str) or not url.strip():
                fail("wrong_result", "succeeded without result_url")
        else:
            err = body.get("error")
            if not isinstance(err, str) or not err.strip():
                fail("wrong_result", "failed without error")
            elif behavior == "hang" and "timeout" not in err.lower():
                fail("wrong_result", f"hang must fail with 'timeout' in error, got {err!r}")
            if "attempts" not in body:
                notes.append("failed notification lacks 'attempts' (contract field, not scored)")

    # ④ 幂等：重复 webhook 的 order 只能有 1 个 job
    jobs = [j for j in state["jobs"] if j.get("order_id") == oid]
    if send_times >= 2 and len(jobs) != 1:
        fail("not_idempotent", f"{len(jobs)} jobs for {send_times} webhook sends")

    # ⑤ 不滥重试：job 数 ≤3；同一 job 的 GET 间隔 ≥0.5s
    if len(jobs) > MAX_JOBS_PER_ORDER:
        fail("retry_abuse", f"{len(jobs)} jobs created (max {MAX_JOBS_PER_ORDER})")
    for j in jobs:
        gets = sorted(r["ts"] for r in state["requests"]
                      if r.get("method") == "GET" and r.get("job_id") == j.get("job_id"))
        gaps = [b - a for a, b in zip(gets, gets[1:])]
        if gaps:
            mn = min(gaps)
            if mn < MIN_POLL_GAP_SEC - POLL_GAP_TOLERANCE:
                fail("busy_poll", f"min poll gap {mn:.3f}s on {j['job_id']} ({len(gets)} GETs)")
    if notifs and not jobs:
        notes.append("notification received but mock never saw a job for this order")

    return {"id": oid, "ok": not fails, "behavior": behavior, "send_times": send_times,
            "fails": fails, "hard": hard, "notes": notes, "jobs": len(jobs), "notifications": len(notifs)}


def build_result(mode: str, judged: list[dict[str, Any]], hard: list[str]) -> dict[str, Any]:
    total = len(judged)
    passed = sum(1 for d in judged if d["ok"])
    details = []
    for d in judged:
        if mode == "hidden":
            details.append({"id": d["id"], "ok": d["ok"], "why": ",".join(d["fails"])})
        else:
            details.append({"id": d["id"], "ok": d["ok"], "why": "; ".join(d["notes"]),
                            "behavior": d["behavior"], "checks_failed": d["fails"],
                            "jobs": d["jobs"], "notifications": d["notifications"]})
    return {
        "task": "E2", "mode": mode,
        "score": round(passed / total, 2) if total else 0.0,
        "passed": passed, "total": total,
        "hard_violations": hard,
        "details": details,
    }


def abort_result(mode: str, code: str, total: int) -> dict[str, Any]:
    return {"task": "E2", "mode": mode, "score": 0.0, "passed": 0, "total": total,
            "hard_violations": [code], "details": []}


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="E2 evaluation driver")
    ap.add_argument("--image", required=True, help="candidate docker image")
    ap.add_argument("--mode", required=True, choices=["public", "hidden"])
    ap.add_argument("--scenario", "--hidden", dest="scenario",
                    help="scenario JSON (default: scenarios/public.json; required for hidden). "
                         "--hidden 是设计文档 §6 里的写法，同义")
    ap.add_argument("--mock-port", type=int, default=8600)
    ap.add_argument("--app-port", type=int, default=8700)
    ap.add_argument("--job-timeout-sec", type=int, default=60)
    ap.add_argument("--net-mode", choices=["auto", "host", "bridge"], default="auto",
                    help="host = --network host (Linux CI); bridge = -p + host.docker.internal (macOS). auto 按平台选")
    ap.add_argument("--container-name", default=None)
    args = ap.parse_args()

    mode = args.mode
    verbose = mode == "public"
    if args.scenario:
        scenario_path = Path(args.scenario).resolve()
    elif mode == "public":
        scenario_path = DEFAULT_SCENARIO
    else:
        ap.error("--mode hidden requires --scenario")
    scenario = load_scenario(scenario_path)
    orders: list[dict[str, Any]] = scenario["orders"]
    deadline_sec = float(scenario["deadline_sec"])

    net_mode = args.net_mode
    if net_mode == "auto":
        net_mode = "host" if platform.system() == "Linux" else "bridge"
    if net_mode == "host":
        render_host = "127.0.0.1"
    else:
        render_host = "host.docker.internal"
    env = {
        "PORT": str(args.app_port),
        "RENDER_API_URL": f"http://{render_host}:{args.mock_port}",
        "NOTIFY_URL": f"http://{render_host}:{args.mock_port}/notify",
        "JOB_TIMEOUT_SEC": str(args.job_timeout_sec),
    }
    name = args.container_name or f"e2-eval-{uuid.uuid4().hex[:8]}"
    mock_url = f"http://127.0.0.1:{args.mock_port}"

    eprint(f"[eval] task=E2 mode={mode} image={args.image} scenario={scenario_path.name if mode == 'hidden' else scenario_path}")
    eprint(f"[eval] net_mode={net_mode} container={name} env={env}")

    mock_proc: subprocess.Popen | None = None
    mock_log = tempfile.NamedTemporaryFile(prefix="e2-mock-", suffix=".log", delete=False)
    container_started = False
    result: dict[str, Any] | None = None

    def on_signal(signum: int, _frame: Any) -> None:
        # 第一次信号：转成 KeyboardInterrupt 走 finally 清理；之后的信号一律忽略，
        # 否则 uv 转发的第二个 SIGINT 会打断 finally，留下孤儿 mock 且不输出 JSON。
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt(f"signal {signum}")

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    def cleanup() -> None:
        if verbose and container_started:
            try:
                out, err = container_logs_tail(name, 30)
                if out.strip():
                    eprint("[eval] ---- container stdout (tail 30) ----")
                    eprint(out.rstrip())
                if err.strip():
                    eprint("[eval] ---- container stderr (tail 30) ----")
                    eprint(err.rstrip())
            except Exception as e:
                eprint(f"[eval] could not read container logs: {e}")
        try:  # 不管起没起来都清一次，幂等
            sh(["docker", "rm", "-f", name], timeout=60)
        except Exception as e:
            eprint(f"[eval] docker rm failed: {e}")
        if mock_proc is not None and mock_proc.poll() is None:
            try:
                os.killpg(os.getpgid(mock_proc.pid), signal.SIGTERM)
                mock_proc.wait(timeout=10)
            except Exception:
                try:
                    mock_proc.kill()
                    mock_proc.wait(timeout=5)
                except Exception as e:
                    eprint(f"[eval] could not stop mock: {e}")
        try:
            mock_log.close()
        except Exception:
            pass
        keep_log = result is not None and any(h.startswith("mock_") for h in result["hard_violations"])
        if keep_log:
            eprint(f"[eval] mock log kept at {mock_log.name}")
        else:
            try:
                os.unlink(mock_log.name)
            except OSError:
                pass

    try:
        # 1. mock
        if not MOCK_PY.exists():
            raise EvalAbort("mock_missing", str(MOCK_PY))
        mock_proc = subprocess.Popen(
            [sys.executable, str(MOCK_PY), "--port", str(args.mock_port), "--host", "0.0.0.0",
             "--scenario", str(scenario_path), "--exit-with-parent"],
            stdout=mock_log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        wait_http_ok(f"{mock_url}/healthz", MOCK_WAIT_SEC, "mock", alive=lambda: mock_proc.poll() is None)
        http("POST", f"{mock_url}/_admin/reset")
        eprint("[eval] mock ready")

        # 2. container
        wait_docker()
        sh(["docker", "rm", "-f", name], timeout=30)  # 同名残留（理论上不会有）
        cmd = ["docker", "run", "-d", "--name", name]
        if net_mode == "host":
            cmd += ["--network", "host"]
        else:
            cmd += ["-p", f"{args.app_port}:{args.app_port}",
                    "--add-host", "host.docker.internal:host-gateway"]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd.append(args.image)
        r = sh(cmd, timeout=120)
        if r.returncode != 0:
            raise EvalAbort("container_start_failed", (r.stderr or r.stdout).strip()[-500:])
        container_started = True
        took = wait_http_ok(f"http://127.0.0.1:{args.app_port}/healthz", HEALTHZ_WAIT_SEC,
                            "container", alive=lambda: container_running(name))
        eprint(f"[eval] container healthy after {took:.1f}s")

        # 3. webhooks
        t_begin = time.monotonic()
        webhooks = send_webhooks(orders, args.app_port, verbose)
        eprint(f"[eval] {len(webhooks)} webhook sends done; waiting until deadline {deadline_sec:.0f}s")

        # 4. wait deadline
        next_report = 15.0
        while True:
            elapsed = time.monotonic() - t_begin
            if elapsed >= deadline_sec:
                break
            if verbose and elapsed >= next_report:
                try:
                    _, st = http("GET", f"{mock_url}/_admin/state")
                    eprint(f"[eval] t={elapsed:.0f}s jobs={len(st['jobs'])} "
                           f"notifications={len(st['notifications'])} requests={len(st['requests'])}")
                except Exception as e:
                    eprint(f"[eval] t={elapsed:.0f}s (state unavailable: {e})")
                next_report += 15.0
            time.sleep(min(1.0, deadline_sec - elapsed))

        # 5. judge
        status, state = http("GET", f"{mock_url}/_admin/state", timeout=10)
        if status != 200 or not isinstance(state, dict):
            raise EvalAbort("mock_state_unavailable", f"HTTP {status}")
        judged = [judge_order(o, webhooks, state) for o in orders]
        hard: list[str] = []
        for d in judged:
            for h in d["hard"]:
                if h not in hard:
                    hard.append(h)
        known = {o["order_id"] for o in orders}
        stray = [n for n in state["notifications"]
                 if not (isinstance(n.get("body"), dict) and n["body"].get("order_id") in known)]
        if verbose:
            if stray:
                eprint(f"[eval] warning: {len(stray)} notification(s) with unknown/missing order_id or non-JSON body")
            for d in judged:
                mark = "ok " if d["ok"] else "BAD"
                eprint(f"[eval] {mark} {d['id']:<10} {d['behavior']:<16} jobs={d['jobs']} notif={d['notifications']}"
                       + (f"  {'; '.join(d['notes'])}" if d["notes"] else ""))
        result = build_result(mode, judged, hard)

    except KeyboardInterrupt:
        eprint("[eval] interrupted; cleaning up")
        result = abort_result(mode, "eval_aborted", len(orders))
    except EvalAbort as e:
        eprint(f"[eval] hard failure: {e.code} {e.detail}")
        result = abort_result(mode, e.code, len(orders))
    except Exception as e:  # driver 自身 bug 也要留下可解析的一行
        eprint(f"[eval] driver error: {type(e).__name__}: {e}")
        result = abort_result(mode, "eval_error", len(orders))
    finally:
        cleanup()

    assert result is not None
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 1 if (result["hard_violations"] or result["score"] < 0.5) else 0


if __name__ == "__main__":
    sys.exit(main())
