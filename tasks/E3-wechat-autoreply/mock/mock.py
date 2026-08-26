# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastapi>=0.115",
#   "uvicorn[standard]>=0.30",
#   "websockets>=12",
# ]
# ///
"""
E3 · 仿微信网页版 mock（评测用）

    uv run mock/mock.py --port 8765 --scenario scenarios/public.json

- GET  /              仿微信网页版页面（静态文件在 ./static）
- WS   /ws            页面与服务端的唯一通道（推消息 / 发消息 / 关弹窗）
- GET  /_admin/log    全部事件（评测读这个）
- POST /_admin/reset  清空事件、重置会话与计时

剧本引擎从「第一个 WS 连接建立」那一刻开始计时；所有事件的 ts 都是相对它的秒数（1 位小数）。
日志只写 stderr。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).resolve().parent / "static"


def log(*parts: object) -> None:
    print("[mock]", *parts, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- state


class State:
    def __init__(self, scenario: dict):
        self.scenario = scenario
        self.script_task: asyncio.Task | None = None
        self.clients: set[WebSocket] = set()
        self._reset_data()

    def _reset_data(self) -> None:
        self.t0: float | None = None
        self.events: list[dict] = []
        self.seq = 0
        self.convs: dict[str, dict] = {}
        for i, c in enumerate(self.scenario["conversations"]):
            self.convs[c["id"]] = {
                "id": c["id"],
                "name": c["name"],
                "order": i,
                "unread": 0,
                "last_ts": None,
                "messages": [],  # {mid, dir: in|out, text, ts}
            }
        self.open_popups: dict[str, dict] = {}

    # -- time ---------------------------------------------------------------
    def now(self) -> float:
        if self.t0 is None:
            return 0.0
        return round(time.monotonic() - self.t0, 1)

    def start_clock_if_needed(self) -> bool:
        if self.t0 is not None:
            return False
        self.t0 = time.monotonic()
        return True

    # -- events -------------------------------------------------------------
    def emit(self, etype: str, **fields: object) -> dict:
        ev = {"type": etype, "ts": self.now(), **fields}
        self.events.append(ev)
        log(json.dumps(ev, ensure_ascii=False))
        return ev

    def next_id(self, prefix: str) -> str:
        self.seq += 1
        return f"{prefix}{self.seq:03d}"

    # -- snapshot for a freshly connected page ---------------------------------
    def snapshot(self) -> dict:
        return {
            "type": "snapshot",
            "ts": self.now(),
            "me": {"name": "云栖家居·小顾"},
            "conversations": [
                {
                    "id": c["id"],
                    "name": c["name"],
                    "order": c["order"],
                    "unread": c["unread"],
                    "last_ts": c["last_ts"],
                    "messages": list(c["messages"]),
                }
                for c in self.convs.values()
            ],
            "popups": list(self.open_popups.values()),
        }

    # -- broadcast ------------------------------------------------------------
    async def broadcast(self, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        dead: list[WebSocket] = []
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    # -- domain actions ---------------------------------------------------------
    async def deliver_msg_in(self, conv_id: str, text: str) -> None:
        conv = self.convs[conv_id]
        ts = self.now()
        mid = self.next_id("m")
        conv["messages"].append({"mid": mid, "dir": "in", "text": text, "ts": ts})
        conv["unread"] += 1
        conv["last_ts"] = ts
        self.emit("msg_in", conv_id=conv_id, msg_id=mid, text=text)
        await self.broadcast(
            {"type": "msg_in", "conv_id": conv_id, "msg_id": mid, "text": text, "ts": ts, "unread": conv["unread"]}
        )

    async def send_msg_out(self, conv_id: str, text: str) -> None:
        conv = self.convs.get(conv_id)
        if conv is None:
            log(f"send: unknown conv_id {conv_id!r}, ignored")
            return
        text = text.strip()
        if not text:
            return
        ts = self.now()
        after = None
        for m in reversed(conv["messages"]):
            if m["dir"] == "in":
                after = m["mid"]
                break
        mid = self.next_id("o")
        conv["messages"].append({"mid": mid, "dir": "out", "text": text, "ts": ts})
        conv["last_ts"] = ts
        self.emit("msg_out", conv_id=conv_id, text=text, after_msg_id=after)
        await self.broadcast({"type": "msg_out", "conv_id": conv_id, "msg_id": mid, "text": text, "ts": ts})

    async def show_popup(self, popup: dict) -> None:
        pid = popup["id"]
        self.open_popups[pid] = {"id": pid, "title": popup.get("title", "系统提示"), "body": popup.get("body", "")}
        self.emit("popup_shown", popup_id=pid)
        await self.broadcast({"type": "popup", **self.open_popups[pid]})

    async def close_popup(self, pid: str) -> None:
        if pid not in self.open_popups:
            return  # 重复关闭 / 未知 id：忽略
        del self.open_popups[pid]
        self.emit("popup_closed", popup_id=pid)
        await self.broadcast({"type": "popup_closed", "id": pid})

    def mark_read(self, conv_id: str) -> None:
        conv = self.convs.get(conv_id)
        if conv is not None:
            conv["unread"] = 0

    # -- reset ------------------------------------------------------------------
    async def reset(self) -> None:
        if self.script_task is not None:
            self.script_task.cancel()
            try:
                await self.script_task
            except (asyncio.CancelledError, Exception):
                pass
            self.script_task = None
        self._reset_data()
        log("reset: log cleared, clock stopped; will restart on next ws connect")
        await self.broadcast({"type": "reset"})


# --------------------------------------------------------------------------- script engine


async def run_script(state: State) -> None:
    sc = state.scenario
    duration = float(sc.get("duration_sec", 0))
    items: list[tuple[float, int, str, str | None, dict]] = []
    for c in sc["conversations"]:
        for m in c.get("messages", []):
            items.append((float(m["at"]), 0, "msg", c["id"], m))
    for p in sc.get("popups", []):
        items.append((float(p["at"]), 1, "popup", None, p))
    items.sort(key=lambda x: (x[0], x[1]))

    log(f"script started: {len(sc['conversations'])} conversations, {len(items)} scheduled items, duration {duration:.0f}s")
    try:
        for at, _, kind, conv_id, item in items:
            if at > duration:
                log(f"skip item at={at} beyond duration_sec={duration}")
                continue
            delay = at - (time.monotonic() - state.t0)  # type: ignore[operator]
            if delay > 0:
                await asyncio.sleep(delay)
            if kind == "msg":
                await state.deliver_msg_in(conv_id, item["text"])  # type: ignore[arg-type]
            else:
                await state.show_popup(item)
        remaining = duration - (time.monotonic() - state.t0)  # type: ignore[operator]
        if remaining > 0:
            await asyncio.sleep(remaining)
        log(f"script finished at ts={state.now()}; page stays usable, no more pushes")
    except asyncio.CancelledError:
        log("script cancelled")
        raise


# --------------------------------------------------------------------------- app


def build_app(state: State) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    @app.get("/_admin/log")
    async def admin_log():
        return JSONResponse(state.events)

    @app.post("/_admin/reset")
    async def admin_reset():
        await state.reset()
        return {"ok": True}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        ua = ws.headers.get("user-agent", "")
        started = state.start_clock_if_needed()
        state.emit("ws_connect", user_agent=ua)
        if started:
            state.script_task = asyncio.create_task(run_script(state))
        state.clients.add(ws)
        try:
            await ws.send_text(json.dumps(state.snapshot(), ensure_ascii=False))
            while True:
                raw = await ws.receive_text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    log(f"ws: bad json ignored: {raw[:80]!r}")
                    continue
                mtype = data.get("type")
                if mtype == "send":
                    await state.send_msg_out(str(data.get("conv_id", "")), str(data.get("text", "")))
                elif mtype == "read":
                    state.mark_read(str(data.get("conv_id", "")))
                elif mtype == "popup_close":
                    await state.close_popup(str(data.get("popup_id", "")))
                elif mtype == "ping":
                    await ws.send_text('{"type":"pong"}')
                else:
                    log(f"ws: unknown message type {mtype!r} ignored")
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            log(f"ws: connection error: {exc!r}")
        finally:
            state.clients.discard(ws)

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="E3 仿微信网页版 mock")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--scenario", required=True, help="剧本 JSON 路径")
    args = ap.parse_args()

    scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
    state = State(scenario)
    app = build_app(state)
    log(f"listening on http://{args.host}:{args.port}  scenario={args.scenario}")
    log("clock starts at the FIRST websocket connection; open the page to start the script")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
