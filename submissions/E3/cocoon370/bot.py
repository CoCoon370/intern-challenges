"""通过真实浏览器 UI 驱动仿微信页面并自动回复。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, Page, Playwright, async_playwright

from core import build_reply, classify_intent, is_handoff, is_safe_reply
from customer_notes import CustomerNoteStore


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
LOGGER = logging.getLogger("e3-bot")


class WeChatBot:
    """负责页面扫描、幂等处理、弹窗恢复和安全发送。"""

    def __init__(self, page: Page, out_dir: Path) -> None:
        self.page = page
        self.out_dir = out_dir
        self.processed_ids: set[str] = set()
        self.handed_off: set[str] = set()
        self.bootstrapped: set[str] = set()
        self.customer_notes = CustomerNoteStore(out_dir)
        self.poll_seconds = float(os.getenv("POLL_INTERVAL_SEC", "0.35"))
        # 只供本地可视化验收使用；正式容器不设置，默认完全不等待。
        self.demo_reply_pause_seconds = float(os.getenv("DEMO_REPLY_PAUSE_SEC", "0"))

    async def close_popups(self) -> int:
        """从最上层开始关闭所有弹窗；元素变化后每次重新定位。"""

        closed = 0
        for _ in range(20):
            buttons = self.page.locator(".modal-mask .modal-close")
            count = await buttons.count()
            if count == 0:
                break
            try:
                await buttons.last.click(timeout=1200)
                closed += 1
                await self.page.wait_for_timeout(30)
            except Exception as exc:  # 页面可能正好重绘，下一轮重新定位即可。
                LOGGER.debug("close popup retry: %r", exc)
                await self.page.wait_for_timeout(50)
        if closed:
            LOGGER.info("closed %s popup(s)", closed)
        return closed

    async def conversation_ids(self) -> list[str]:
        """每轮从当前 DOM 重新读取会话编号，以适应新消息导致的重排。"""

        return await self.page.locator(".chat-list .chat-item").evaluate_all(
            "els => els.map(el => el.getAttribute('data-conv')).filter(Boolean)"
        )

    @staticmethod
    def _css_attr(value: str) -> str:
        """转义属性选择器中的反斜杠和双引号。"""

        return value.replace("\\", "\\\\").replace('"', '\\"')

    async def open_conversation(self, conv_id: str) -> bool:
        """点击会话后确认右侧面板确实属于目标会话。"""

        selector = f'.chat-item[data-conv="{self._css_attr(conv_id)}"]'
        try:
            await self.close_popups()
            item = self.page.locator(selector)
            if await item.count() == 0:
                return False
            await item.click(timeout=1500)
            panel = self.page.locator(f'.chat-panel[data-conv="{self._css_attr(conv_id)}"]')
            await panel.wait_for(state="attached", timeout=1500)
            return True
        except Exception as exc:
            LOGGER.debug("open conversation %s failed: %r", conv_id, exc)
            return False

    async def conversation_name(self, conv_id: str) -> str:
        """读取会话列表中的客户名称，供人工备注表使用。"""

        selector = f'.chat-item[data-conv="{self._css_attr(conv_id)}"] .name'
        try:
            name = (await self.page.locator(selector).text_content(timeout=1000) or "").strip()
            return name or conv_id
        except Exception:
            return conv_id

    async def read_rows(self, conv_id: str) -> list[dict[str, str]]:
        """只读取当前会话面板中稳定契约允许的消息元素。"""

        panel = self.page.locator(f'.chat-panel[data-conv="{self._css_attr(conv_id)}"]')
        if await panel.count() == 0:
            return []
        return await panel.locator(".msg-row").evaluate_all(
            """els => els.map(el => ({
                id: el.getAttribute('data-mid') || '',
                direction: el.classList.contains('msg-in') ? 'in' : 'out',
                text: (el.querySelector('.bubble')?.textContent || '').trim()
            }))"""
        )

    async def _reply_appeared(self, conv_id: str, old_out_ids: set[str], reply: str) -> bool:
        """发送后通过页面确认出现了新的我方消息，避免盲目重复点击。"""

        for _ in range(20):
            if not await self.open_conversation(conv_id):
                await self.page.wait_for_timeout(80)
                continue
            rows = await self.read_rows(conv_id)
            if any(
                row["direction"] == "out"
                and row["id"] not in old_out_ids
                and row["text"] == reply
                for row in rows
            ):
                return True
            await self.page.wait_for_timeout(80)
        return False

    async def send_reply(self, conv_id: str, reply: str) -> bool:
        """关闭竞态弹窗、重新确认会话并发送；失败时先查 DOM 再决定是否重试。"""

        if not is_safe_reply(reply):
            LOGGER.error("unsafe reply blocked conv=%s", conv_id)
            return False

        # 基线只在第一次发送前采集。即使第一次点击后页面确认暂时失败，
        # 后续重试也仍能识别那条新消息，避免把它加入基线后再次发送。
        initial_out_ids: set[str] | None = None
        send_attempted = False
        for attempt in range(3):
            try:
                if not await self.open_conversation(conv_id):
                    continue
                rows = await self.read_rows(conv_id)
                if initial_out_ids is None:
                    initial_out_ids = {
                        row["id"] for row in rows if row["direction"] == "out"
                    }
                elif send_attempted and any(
                    row["direction"] == "out"
                    and row["id"] not in initial_out_ids
                    and row["text"] == reply
                    for row in rows
                ):
                    return True

                await self.close_popups()
                if not await self.open_conversation(conv_id):
                    continue
                editor = self.page.locator("textarea.editor")
                await editor.fill(reply, timeout=1500)

                # 弹窗可能在填入和点击之间出现，因此发送前再次清理并确认会话。
                await self.close_popups()
                active = await self.page.locator(".chat-panel").get_attribute("data-conv")
                if active != conv_id:
                    continue
                await self.page.locator("button.send-btn").click(timeout=1500)
                send_attempted = True
                if await self._reply_appeared(conv_id, initial_out_ids, reply):
                    return True
            except Exception as exc:
                LOGGER.warning("send retry conv=%s attempt=%s error=%r", conv_id, attempt + 1, exc)

            # 可能已经发送成功但确认阶段遇到重绘；重试前必须再检查一次。
            if send_attempted and initial_out_ids is not None and await self._reply_appeared(
                conv_id, initial_out_ids, reply
            ):
                return True
        return False

    def write_decision(self, record: dict[str, Any]) -> None:
        """把决策追加到挂载目录，供人工评审理解每一次选择。"""

        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            path = self.out_dir / "decisions.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            LOGGER.warning("cannot write decision log: %r", exc)

    async def process_conversation(self, conv_id: str) -> None:
        """合并同一轮看到的新消息，只针对最新上下文发送一条回复。"""

        if not await self.open_conversation(conv_id):
            return
        rows = await self.read_rows(conv_id)

        # 浏览器崩溃重连后，从页面已有的我方回复恢复处理进度，防止历史消息
        # 被再次回复。最后一条我方消息之前的客户消息都视为已经处理。
        if conv_id not in self.bootstrapped:
            last_out_index = max(
                (index for index, row in enumerate(rows) if row["direction"] == "out"),
                default=-1,
            )
            self.processed_ids.update(
                row["id"] for index, row in enumerate(rows)
                if index < last_out_index and row["direction"] == "in" and row["id"]
            )
            recovered_history: list[str] = []
            for row in rows[: last_out_index + 1]:
                if row["direction"] != "in":
                    continue
                if classify_intent(row["text"], recovered_history).intent == "handoff":
                    self.handed_off.add(conv_id)
                recovered_history.append(row["text"])
            self.bootstrapped.add(conv_id)

        pending = [
            row for row in rows
            if row["direction"] == "in" and row["id"] and row["id"] not in self.processed_ids
        ]
        if not pending:
            return

        # 一旦该会话已经转人工，后续消息只记账，不再自动发送。
        if conv_id in self.handed_off:
            self.processed_ids.update(row["id"] for row in pending)
            for row in pending:
                self.write_decision({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "conv_id": conv_id,
                    "msg_id": row["id"],
                    "intent": "handoff",
                    "action": "suppressed_after_handoff",
                    "reply": "",
                })
            return

        # 批次里只要出现公开转人工词，就按最高风险处理；否则判断最新消息，
        # 并把同一微信会话中的历史客户消息作为上下文。
        handoff_rows = [row for row in pending if is_handoff(row["text"])]
        target = handoff_rows[0] if handoff_rows else pending[-1]
        target_index = next(
            index for index, row in enumerate(rows)
            if row["id"] == target["id"] and row["direction"] == "in"
        )
        history = [
            row["text"] for row in rows[:target_index]
            if row["direction"] == "in"
        ]
        result = classify_intent(target["text"], history=history)
        reply = build_reply(result, target["text"], history=history)
        sent = await self.send_reply(conv_id, reply)
        if not sent:
            LOGGER.error("reply not confirmed conv=%s msg=%s", conv_id, target["id"])
            return

        self.processed_ids.update(row["id"] for row in pending)
        if result.intent == "handoff":
            self.handed_off.add(conv_id)

        customer_note = self.customer_notes.update(
            conv_id=conv_id,
            customer_name=await self.conversation_name(conv_id),
            result=result,
            customer_message=target["text"],
        )

        LOGGER.info(
            "replied conv=%s msg=%s intent=%s confidence=%.2f",
            conv_id,
            target["id"],
            result.intent,
            result.confidence,
        )
        self.write_decision({
            "ts": datetime.now(timezone.utc).isoformat(),
            "conv_id": conv_id,
            "msg_id": target["id"],
            "intent": result.intent,
            "topic": result.topic,
            "emotion": result.emotion,
            "confidence": round(result.confidence, 4),
            "reason": result.reason,
            "action": "handoff" if result.intent == "handoff" else "reply",
            "reply": reply,
            "suggested_remark": customer_note["suggested_remark"],
            "customer_status": customer_note["status"],
            "batched_msg_ids": [row["id"] for row in pending],
        })
        if self.demo_reply_pause_seconds > 0:
            await self.page.wait_for_timeout(self.demo_reply_pause_seconds * 1000)

    async def run(self) -> None:
        """持续扫描所有会话；单个会话失败不影响整个进程。"""

        while True:
            try:
                await self.close_popups()
                for conv_id in await self.conversation_ids():
                    await self.process_conversation(str(conv_id))
                await self.close_popups()
            except Exception as exc:
                LOGGER.exception("scan loop recovered from error: %r", exc)
            await self.page.wait_for_timeout(self.poll_seconds * 1000)


async def launch_browser(playwright: Playwright) -> Browser:
    """容器使用自带 Chromium；本地Demo可通过环境变量调用 Edge。"""

    headless = os.getenv("HEADLESS", "true").strip().lower() not in {"0", "false", "no"}
    channel = os.getenv("BROWSER_CHANNEL", "").strip() or None
    launch_options: dict[str, Any] = {
        "headless": headless,
        "args": ["--no-sandbox", "--disable-dev-shm-usage"],
    }
    if channel:
        launch_options["channel"] = channel
    return await playwright.chromium.launch(**launch_options)


async def main() -> None:
    """连接目标页面；浏览器异常退出后自动重建。"""

    url = os.getenv("WECHAT_URL")
    if not url:
        raise RuntimeError("WECHAT_URL is required")
    out_dir = Path(os.getenv("OUT_DIR", "/out"))

    async with async_playwright() as playwright:
        while True:
            browser: Browser | None = None
            try:
                browser = await launch_browser(playwright)
                context = await browser.new_context(viewport={"width": 1280, "height": 800})
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                await page.locator(".chat-list").wait_for(state="attached", timeout=15_000)
                LOGGER.info("connected to %s", url)
                await WeChatBot(page, out_dir).run()
            except Exception as exc:
                LOGGER.exception("browser session ended, retrying: %r", exc)
                await asyncio.sleep(1)
            finally:
                if browser is not None:
                    await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
