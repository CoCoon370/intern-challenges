"""把每个会话的业务判断整理成可供人工接手的客户备注。"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import IntentResult, MOBILE_RE, extract_location, normalize


LOGGER = logging.getLogger("e3-customer-notes")

TOPIC_LABELS = {
    "price": "询价",
    "measuring": "预约量房",
    "aftersales": "售后",
    "soft_furnishing": "成品家具/软装咨询",
    "explicit_handoff": "要求人工",
    "emotion_escalation": "情绪升级",
    "store_visit": "到店咨询",
    "environment": "环保咨询",
    "warranty": "质保咨询",
    "scope": "业务范围咨询",
    "schedule": "工期咨询",
    "compliment": "案例兴趣",
    "acknowledgement": "一般跟进",
    "general": "需求待确认",
}

EMOTION_LABELS = {
    "normal": "情绪正常",
    "frustrated": "客户不满",
    "angry": "客户生气",
}


class CustomerNoteStore:
    """在输出目录维护一份客户意向汇总，不依赖题目未提供的备注接口。"""

    def __init__(self, out_dir: Path) -> None:
        self.path = out_dir / "customer_notes.json"
        self.notes: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            if self.path.exists():
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    return value
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("cannot load customer notes: %r", exc)
        return {}

    @staticmethod
    def _status(result: IntentResult) -> str:
        if result.intent == "handoff":
            return "等待人工接手"
        if result.intent == "aftersales":
            return "等待售后核实"
        if result.intent == "measuring":
            return "待确认量房安排"
        if result.intent == "price":
            return "待补充需求"
        return "继续跟进"

    @staticmethod
    def _priority(result: IntentResult) -> str:
        if result.intent == "handoff":
            return "高"
        if result.intent == "aftersales" or result.emotion == "frustrated":
            return "中"
        return "普通"

    def update(
        self,
        conv_id: str,
        customer_name: str,
        result: IntentResult,
        customer_message: str,
    ) -> dict[str, Any]:
        """保留历史标签，同时用最近一次判断更新当前状态。"""

        old = self.notes.get(conv_id, {})
        labels = list(old.get("intent_labels", []))
        current_label = TOPIC_LABELS.get(result.topic, "需求待确认")
        # “好的、谢谢、到时候联系”只是承接话，不应该覆盖前面的业务意向。
        informative = result.topic != "acknowledgement"
        if informative and current_label not in labels:
            labels.append(current_label)
        if not labels:
            labels.append(current_label)

        emotion_label = EMOTION_LABELS.get(result.emotion, "情绪待确认")
        suggested_remark = f"{'、'.join(labels)}｜{emotion_label}"
        latest_intent = result.intent if informative else old.get("latest_intent", result.intent)
        latest_topic = result.topic if informative else old.get("latest_topic", result.topic)
        status = self._status(result) if informative else old.get("status", self._status(result))
        needs_human = (
            result.intent in {"handoff", "aftersales", "measuring"}
            if informative
            else bool(old.get("needs_human", False))
        )
        lead_info = dict(old.get("lead_info", {}))
        location = extract_location((customer_message,))
        if location:
            lead_info["location"] = location
        mobile = MOBILE_RE.search(normalize(customer_message))
        if mobile:
            lead_info["contact_phone"] = mobile.group(0)
        if re.search(r"周[一二三四五六日天末]|上午|下午|晚上|几点|今天|明天|后天", normalize(customer_message)):
            lead_info["preferred_time_message"] = customer_message
        if result.intent in {"price", "measuring", "aftersales"}:
            key_messages = list(lead_info.get("key_messages", []))
            if customer_message not in key_messages:
                key_messages.append(customer_message)
            lead_info["key_messages"] = key_messages[-5:]
        record = {
            "conv_id": conv_id,
            "customer_name": customer_name,
            "suggested_remark": suggested_remark,
            "intent_labels": labels,
            "latest_intent": latest_intent,
            "latest_topic": latest_topic,
            "emotion": result.emotion,
            "status": status,
            "needs_human": needs_human,
            "priority": self._priority(result) if informative else old.get("priority", "普通"),
            "lead_info": lead_info,
            "last_customer_message": customer_message,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.notes[conv_id] = record
        self._save()
        return record

    def _save(self) -> None:
        """先写临时文件再替换，避免程序中断留下半截 JSON。"""

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(self.notes, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError as exc:
            LOGGER.warning("cannot save customer notes: %r", exc)
