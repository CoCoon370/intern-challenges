#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastapi>=0.115,<1",
#   "uvicorn>=0.30,<1",
# ]
# ///
"""
E2 mock 渲染服务（RunPod 风格的「提交 → 轮询」任务 API + 通知接收端）。

    uv run mock/mock.py --port 8600 --scenario scenarios/public.json

端点：
    POST /jobs                {"order_id","scene","image_urls":[...]} → 202 {"job_id"}
    GET  /jobs/{job_id}       {"job_id","status","result_url"?,"error"?}
    POST /notify              记录 body → 204
    GET  /_admin/state        {"jobs":[...],"notifications":[...],"requests":[...],"meta":{...}}
    POST /_admin/reset        清空（保留 scenario）
    GET  /healthz             200

行为由 scenario 文件按 order_id 精确指定；不在 scenario 里的 order_id 按 normal 处理，
并在 state 里标 unknown_order: true。同一 order_id 重复 POST /jobs 会创建新 job——幂等是
调用方的责任。日志只写 stderr。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

log = logging.getLogger("e2-mock")

BEHAVIORS = {
    "normal",  # queued 1s → running 1s → succeeded
    "cold_start",  # queued 20s → running 3s → succeeded
    "transient_500",  # 前 N 次 GET /jobs/{id} 返回 500，之后正常（默认 N=2）
    "submit_503_once",  # 第一次 POST /jobs 返回 503，第二次起正常
    "permanent_fail",  # running 2s → failed，error: "render engine crashed"
    "hang",  # 永远 running
}


# --------------------------------------------------------------------------- state
class State:
    def __init__(self, scenario: dict[str, Any], scenario_path: str):
        self.lock = threading.Lock()
        self.scenario_path = scenario_path
        self.orders: dict[str, dict[str, Any]] = {}
        for o in scenario.get("orders", []):
            self.orders[o["order_id"]] = o
        self.jobs: list[dict[str, Any]] = []
        self.jobs_by_id: dict[str, dict[str, Any]] = {}
        self.notifications: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.submit_counts: dict[str, int] = {}
        self.t0 = time.time()
        self.m0 = time.monotonic()

    def reset(self) -> None:
        with self.lock:
            self.jobs = []
            self.jobs_by_id = {}
            self.notifications = []
            self.requests = []
            self.submit_counts = {}
            self.t0 = time.time()
            self.m0 = time.monotonic()

    def rel(self, ts: float | None = None) -> float:
        return round((ts if ts is not None else time.time()) - self.t0, 3)


STATE: State  # set in main()


def job_view(job: dict[str, Any], now_mono: float | None = None) -> dict[str, Any]:
    """根据 behavior 与 job 年龄算出当前状态（时间驱动，不需要后台线程）。"""
    age = (now_mono if now_mono is not None else time.monotonic()) - job["created_mono"]
    b = job["behavior"]
    if b in ("normal", "transient_500", "submit_503_once"):
        status = "queued" if age < 1 else "running" if age < 2 else "succeeded"
    elif b == "cold_start":
        status = "queued" if age < 20 else "running" if age < 23 else "succeeded"
    elif b == "permanent_fail":
        status = "running" if age < 2 else "failed"
    elif b == "hang":
        status = "running"
    else:  # 不会走到：scenario 加载时已校验
        status = "failed"

    view: dict[str, Any] = {"job_id": job["job_id"], "status": status}
    if status == "succeeded":
        view["result_url"] = f"https://storage.example.invalid/renders/{job['job_id']}.png"
    elif status == "failed":
        view["error"] = "render engine crashed"
    return view


# --------------------------------------------------------------------------- app
app = FastAPI(title="E2 mock render service", docs_url=None, redoc_url=None)


@app.middleware("http")
async def record_job_requests(request: Request, call_next):
    path = request.url.path
    if not (path == "/jobs" or path.startswith("/jobs/")):
        return await call_next(request)
    ts = time.time()
    response = await call_next(request)
    rec: dict[str, Any] = {
        "method": request.method,
        "path": path,
        "ts": ts,
        "t": STATE.rel(ts),
        "status_code": response.status_code,
    }
    if path.startswith("/jobs/"):
        rec["job_id"] = path[len("/jobs/"):]
    order_id = getattr(request.state, "order_id", None)
    if order_id is not None:
        rec["order_id"] = order_id
    with STATE.lock:
        STATE.requests.append(rec)
    log.info("%s %s -> %s%s", request.method, path, response.status_code,
             f" order_id={order_id}" if order_id else "")
    return response


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "service": "e2-mock"}


@app.post("/jobs")
async def create_job(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "body must be a JSON object"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"detail": "body must be a JSON object"}, status_code=400)
    order_id = body.get("order_id")
    if not isinstance(order_id, str) or not order_id.strip():
        return JSONResponse({"detail": "order_id (non-empty string) is required"}, status_code=400)
    request.state.order_id = order_id

    with STATE.lock:
        spec = STATE.orders.get(order_id)
        unknown = spec is None
        behavior = (spec or {}).get("behavior", "normal")
        n_submit = STATE.submit_counts.get(order_id, 0) + 1
        STATE.submit_counts[order_id] = n_submit

        if behavior == "submit_503_once" and n_submit == 1:
            return JSONResponse(
                {"detail": "render service temporarily unavailable, retry later"},
                status_code=503,
            )

        job_id = "job-" + uuid.uuid4().hex[:12]
        job = {
            "job_id": job_id,
            "order_id": order_id,
            "scene": body.get("scene"),
            "image_urls": body.get("image_urls"),
            "behavior": behavior,
            "n": int((spec or {}).get("n", 2)),
            "unknown_order": unknown,
            "submit_index": n_submit,
            "created_at": time.time(),
            "created_mono": time.monotonic(),
            "get_count": 0,
        }
        job["t"] = STATE.rel(job["created_at"])
        STATE.jobs.append(job)
        STATE.jobs_by_id[job_id] = job

    if unknown:
        log.warning("order_id %s not in scenario, treating as normal (unknown_order)", order_id)
    return JSONResponse({"job_id": job_id}, status_code=202)


@app.get("/jobs/{job_id}")
async def get_job(job_id: str) -> Response:
    with STATE.lock:
        job = STATE.jobs_by_id.get(job_id)
        if job is None:
            return JSONResponse({"detail": "job not found"}, status_code=404)
        job["get_count"] += 1
        if job["behavior"] == "transient_500" and job["get_count"] <= job["n"]:
            return JSONResponse({"detail": "internal render error"}, status_code=500)
        view = job_view(job)
    return JSONResponse(view)


@app.post("/notify")
async def notify(request: Request) -> Response:
    raw = await request.body()
    body: Any = None
    parse_error: str | None = None
    try:
        body = json.loads(raw.decode("utf-8")) if raw else None
    except Exception as e:  # 仍然 204（照规范），但记下来方便排查
        parse_error = f"{type(e).__name__}: {e}"
    ts = time.time()
    rec: dict[str, Any] = {
        "ts": ts,
        "t": STATE.rel(ts),
        "body": body,
        "content_type": request.headers.get("content-type"),
    }
    if parse_error:
        rec["parse_error"] = parse_error
        rec["raw"] = raw[:2000].decode("utf-8", "replace")
    with STATE.lock:
        STATE.notifications.append(rec)
    oid = body.get("order_id") if isinstance(body, dict) else None
    if parse_error:
        log.warning("POST /notify body is not JSON (%s)", parse_error)
    else:
        log.info("POST /notify order_id=%s status=%s",
                 oid, body.get("status") if isinstance(body, dict) else None)
    return Response(status_code=204)


@app.get("/_admin/state")
async def admin_state() -> dict[str, Any]:
    with STATE.lock:
        now = time.monotonic()
        jobs = []
        for j in STATE.jobs:
            v = job_view(j, now)
            jobs.append({
                **v,
                "order_id": j["order_id"],
                "behavior": j["behavior"],
                "unknown_order": j["unknown_order"],
                "submit_index": j["submit_index"],
                "get_count": j["get_count"],
                "created_at": j["created_at"],
                "t": j["t"],
                "scene": j["scene"],
                "image_urls": j["image_urls"],
            })
        return {
            "jobs": jobs,
            "notifications": list(STATE.notifications),
            "requests": list(STATE.requests),
            "meta": {
                "scenario": STATE.scenario_path,
                "orders_in_scenario": len(STATE.orders),
                "t0": STATE.t0,
                "now": time.time(),
                "submit_counts": dict(STATE.submit_counts),
            },
        }


@app.post("/_admin/reset")
async def admin_reset() -> dict[str, Any]:
    STATE.reset()
    log.info("state reset")
    return {"ok": True}


# --------------------------------------------------------------------------- main
def load_scenario(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    orders = data.get("orders")
    if not isinstance(orders, list):
        raise SystemExit(f"scenario {path}: 'orders' must be a list")
    seen: set[str] = set()
    for o in orders:
        oid = o.get("order_id")
        if not isinstance(oid, str) or not oid:
            raise SystemExit(f"scenario {path}: every order needs a string order_id: {o}")
        if oid in seen:
            raise SystemExit(f"scenario {path}: duplicate order_id {oid}")
        seen.add(oid)
        b = o.get("behavior", "normal")
        if b not in BEHAVIORS:
            raise SystemExit(f"scenario {path}: unknown behavior {b!r} for {oid} "
                             f"(allowed: {sorted(BEHAVIORS)})")
    return data


def watch_parent() -> None:
    """父进程（评测 driver）没了就退出，避免孤儿 mock 占着端口。"""
    parent = os.getppid()
    while True:
        time.sleep(1)
        if os.getppid() != parent:
            log.warning("parent %d gone, exiting", parent)
            os._exit(0)


def main() -> None:
    ap = argparse.ArgumentParser(description="E2 mock render service")
    ap.add_argument("--port", type=int, default=8600)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--scenario", required=True, help="scenario JSON (see scenarios/public.json)")
    ap.add_argument("--exit-with-parent", action="store_true",
                    help="parent process exits → mock exits (evaluation driver uses this)")
    args = ap.parse_args()

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    global STATE
    scenario = load_scenario(args.scenario)
    STATE = State(scenario, args.scenario)
    log.info("scenario %s: %d orders; listening on %s:%d",
             args.scenario, len(STATE.orders), args.host, args.port)
    if args.exit_with_parent:
        threading.Thread(target=watch_parent, daemon=True).start()
    # log_config=None：uvicorn 不再接管 logging，全部走上面的 stderr handler
    uvicorn.run(app, host=args.host, port=args.port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
