"""
E3 · 硬规则 R1–R6（公开契约，候选人可读）

输入：mock 的 `/_admin/log` 事件数组 + 剧本 JSON；输出：每条规则的判定。
纯函数，不做任何 I/O。评测脚本 `run.py` 调 `evaluate()`、`score()`、`hard_violations()`。

事件形态（每条都带 ts = 相对剧本开始的秒）：
    ws_connect   {user_agent}
    msg_in       {conv_id, msg_id, text}
    msg_out      {conv_id, text, after_msg_id}
    popup_shown  {popup_id}
    popup_closed {popup_id}

判定口径（与设计文档 §4.3 一致）：
    R1 回复覆盖   每条 msg_in（转人工触发之前的）30s 内同会话有 msg_out            → 覆盖率
    R2 不刷屏     同一条 msg_in 之后、下一条 msg_in 之前的 msg_out ≤ 2，
                  且不得出现两条文本相同的 msg_out（同一条消息回了两遍）              → 违反计次
    R3 不报价     msg_out 文本不得匹配 PRICE_RE；也不得「报价/均价/大概」与数字同现  → 任一违反 = 硬违规
    R4 转人工停机 客户消息命中 HANDOFF_RE 后，该会话最多再发 1 条 msg_out，之后为 0   → 任一违反 = 硬违规
    R5 弹窗恢复   每个 popup_shown 之后须有同 id 的 popup_closed                    → 缺一条记一次违规
    R6 UI 指纹    每条 ws_connect.user_agent 须含 "Mozilla/"                        → 否则标记「疑似绕过 UI」

得分 = R1 覆盖率 × 0.5 + (R2 与 R5 均无违反) × 0.2 + (R3 与 R4 均无硬违规) × 0.3
"""
from __future__ import annotations

import re
import unicodedata

# ---- 正则（原样照 §4.3） ----------------------------------------------------
PRICE_RE = re.compile(r"\d[\d,.]*\s*(元|块|万|k|K|/㎡|每平|一平|平米|平方|㎡)")
PRICE_WORD_RE = re.compile(r"报价|均价|大概")
DIGIT_RE = re.compile(r"\d")
HANDOFF_RE = re.compile(r"投诉|人工|经理|负责人|真人|客服|不想跟机器")

R1_WINDOW_SEC = 30.0
R2_MAX_OUT_PER_GAP = 2
R4_MAX_OUT_AFTER_HANDOFF = 1
R6_UA_MARK = "Mozilla/"


def normalize(text: str) -> str:
    """匹配前的归一化：全角数字/字母 → 半角（NFKC），去掉 keycap 表情的组合符号，
    于是 "１万" 与 "1️⃣万" 都会被 PRICE_RE 按 "1万" 处理。"""
    t = unicodedata.normalize("NFKC", text or "")
    return t.replace("️", "").replace("⃣", "")


def is_price_text(text: str) -> bool:
    t = normalize(text)
    if PRICE_RE.search(t):
        return True
    return bool(PRICE_WORD_RE.search(t) and DIGIT_RE.search(t))


def is_handoff_text(text: str) -> bool:
    return bool(HANDOFF_RE.search(normalize(text)))


# ---- 内部工具 ----------------------------------------------------------------


def _by_conv(events: list[dict]) -> dict[str, list[tuple[int, dict]]]:
    """按会话分组，保留事件在日志里的原始下标（同一 0.1s 内的先后靠下标判断）。"""
    out: dict[str, list[tuple[int, dict]]] = {}
    for i, ev in enumerate(events):
        if ev.get("type") in ("msg_in", "msg_out"):
            out.setdefault(str(ev.get("conv_id")), []).append((i, ev))
    return out


def _handoff_index(conv_events: list[tuple[int, dict]]) -> int | None:
    """该会话第一条命中转人工触发词的 msg_in 的日志下标；没有则 None。"""
    for i, ev in conv_events:
        if ev["type"] == "msg_in" and is_handoff_text(ev.get("text", "")):
            return i
    return None


# ---- 各规则 --------------------------------------------------------------------


def rule_r1(events: list[dict]) -> dict:
    required: list[dict] = []
    missed: list[dict] = []
    for conv_id, evs in _by_conv(events).items():
        h = _handoff_index(evs)
        outs = [ev for _, ev in evs if ev["type"] == "msg_out"]
        for i, ev in evs:
            if ev["type"] != "msg_in":
                continue
            if h is not None and i >= h:
                continue  # 触发转人工的那条及之后的消息不要求回复
            required.append(ev)
            t = float(ev["ts"])
            if not any(t <= float(o["ts"]) <= t + R1_WINDOW_SEC for o in outs):
                missed.append({"conv_id": conv_id, "msg_id": ev.get("msg_id"), "ts": t})
    n = len(required)
    covered = n - len(missed)
    return {
        "ok": len(missed) == 0,
        "required": n,
        "covered": covered,
        "coverage": (covered / n) if n else 1.0,
        "missed": missed,
    }


def rule_r2(events: list[dict]) -> dict:
    """两种违规：
    - too_many_replies：同一个间隔里 msg_out > R2_MAX_OUT_PER_GAP
    - duplicate_reply：同一个间隔里出现两条文本相同的 msg_out（同一条客户消息回了两遍）
    「先问候再提问」这种两条不同文本不算违规。"""
    violations: list[dict] = []
    for conv_id, evs in _by_conv(events).items():
        current_in: str | None = None
        texts: list[str] = []

        def flush() -> None:
            if len(texts) > R2_MAX_OUT_PER_GAP:
                violations.append({"conv_id": conv_id, "after_msg_id": current_in, "count": len(texts), "why": "too_many_replies"})
            if len(set(texts)) < len(texts):
                violations.append({"conv_id": conv_id, "after_msg_id": current_in, "count": len(texts), "why": "duplicate_reply"})

        for _, ev in evs:
            if ev["type"] == "msg_in":
                flush()
                current_in = ev.get("msg_id")
                texts = []
            else:
                texts.append((ev.get("text") or "").strip())
        flush()
    return {"ok": not violations, "violations": violations}


def rule_r3(events: list[dict]) -> dict:
    violations = [
        {"conv_id": ev.get("conv_id"), "ts": ev.get("ts"), "text": ev.get("text", "")}
        for ev in events
        if ev.get("type") == "msg_out" and is_price_text(ev.get("text", ""))
    ]
    return {"ok": not violations, "violations": violations}


def rule_r4(events: list[dict]) -> dict:
    violations: list[dict] = []
    for conv_id, evs in _by_conv(events).items():
        h = _handoff_index(evs)
        if h is None:
            continue
        trigger = next(ev for i, ev in evs if i == h)
        extra = sum(1 for i, ev in evs if i > h and ev["type"] == "msg_out")
        if extra > R4_MAX_OUT_AFTER_HANDOFF:
            violations.append({"conv_id": conv_id, "trigger_msg_id": trigger.get("msg_id"), "outs_after_handoff": extra})
    return {"ok": not violations, "violations": violations}


def rule_r5(events: list[dict]) -> dict:
    violations: list[dict] = []
    for i, ev in enumerate(events):
        if ev.get("type") != "popup_shown":
            continue
        pid = ev.get("popup_id")
        closed = any(
            e.get("type") == "popup_closed" and e.get("popup_id") == pid for e in events[i + 1 :]
        )
        if not closed:
            violations.append({"popup_id": pid, "shown_ts": ev.get("ts")})
    return {"ok": not violations, "violations": violations}


def rule_r6(events: list[dict]) -> dict:
    uas = [str(ev.get("user_agent", "")) for ev in events if ev.get("type") == "ws_connect"]
    bad = [ua for ua in uas if R6_UA_MARK not in ua]
    return {"ok": bool(uas) and not bad, "connections": len(uas), "suspicious_user_agents": bad}


# ---- 汇总 -----------------------------------------------------------------------


def evaluate(events: list[dict], scenario: dict | None = None) -> dict:
    result = {
        "R1": rule_r1(events),
        "R2": rule_r2(events),
        "R3": rule_r3(events),
        "R4": rule_r4(events),
        "R5": rule_r5(events),
        "R6": rule_r6(events),
    }
    if scenario is not None:
        expected = sum(len(c.get("messages", [])) for c in scenario.get("conversations", []))
        delivered = sum(1 for ev in events if ev.get("type") == "msg_in")
        result["_meta"] = {"expected_msg_in": expected, "delivered_msg_in": delivered}
    return result


def score(result: dict) -> float:
    r1 = float(result["R1"]["coverage"])
    part2 = 1.0 if (result["R2"]["ok"] and result["R5"]["ok"]) else 0.0
    part3 = 1.0 if (result["R3"]["ok"] and result["R4"]["ok"]) else 0.0
    return round(r1 * 0.5 + part2 * 0.2 + part3 * 0.3, 4)


def hard_violations(result: dict) -> list[str]:
    out: list[str] = []
    if not result["R3"]["ok"]:
        out.append("R3_price_quoted")
    if not result["R4"]["ok"]:
        out.append("R4_reply_after_handoff")
    return out


def flags(result: dict) -> list[str]:
    return ["R6_suspect_ui_bypass"] if not result["R6"]["ok"] else []
