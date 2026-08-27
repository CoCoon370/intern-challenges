"""客户意向备注的离线测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from core import classify_intent
from customer_notes import CustomerNoteStore


class CustomerNoteTests(unittest.TestCase):
    def test_notes_accumulate_intents_for_human_follow_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CustomerNoteStore(Path(directory))
            store.update(
                "c01", "王女士", classify_intent("衣柜怎么收费"), "衣柜怎么收费"
            )
            note = store.update(
                "c01", "王女士", classify_intent("我想预约量房"), "我想预约量房"
            )

            self.assertEqual(note["intent_labels"], ["询价", "预约量房"])
            self.assertEqual(note["status"], "待确认量房安排")
            saved = json.loads((Path(directory) / "customer_notes.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["c01"]["suggested_remark"], "询价、预约量房｜情绪正常")

    def test_acknowledgement_does_not_erase_business_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CustomerNoteStore(Path(directory))
            store.update(
                "c02", "李哥", classify_intent("我想预约量房"), "我想预约量房"
            )
            note = store.update(
                "c02", "李哥", classify_intent("好的谢谢"), "好的谢谢"
            )
            self.assertEqual(note["intent_labels"], ["预约量房"])
            self.assertEqual(note["latest_topic"], "measuring")
            self.assertEqual(note["status"], "待确认量房安排")
            self.assertTrue(note["needs_human"])

    def test_handoff_note_marks_human_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CustomerNoteStore(Path(directory))
            note = store.update(
                "c05", "赵先生", classify_intent("我要投诉，找经理"), "我要投诉，找经理"
            )
            self.assertTrue(note["needs_human"])
            self.assertEqual(note["status"], "等待人工接手")

    def test_lead_information_is_saved_for_follow_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CustomerNoteStore(Path(directory))
            result = classify_intent("我想预约量房")
            test_phone = "".join(("138", "0013", "8000"))  # 仅测试格式的虚构号码。
            note = store.update(
                "c08",
                "周女士",
                result,
                f"房子在滨江那边，电话{test_phone}，周六上午方便",
            )
            self.assertEqual(note["lead_info"]["location"], "滨江")
            self.assertEqual(note["lead_info"]["contact_phone"], test_phone)
            self.assertIn("周六上午", note["lead_info"]["preferred_time_message"])


if __name__ == "__main__":
    unittest.main()
