"""不依赖浏览器或第三方测试框架的分类与安全测试。"""

import unittest

from core import (
    SAFE_REPLIES,
    build_reply,
    classify_intent,
    detect_emotion,
    has_location,
    is_forbidden_price_reply,
    is_handoff,
    is_safe_reply,
)


class CoreTests(unittest.TestCase):
    """覆盖转人工、上下文、E1安全知识卡片和价格输出安全闸。"""

    def test_public_handoff_contract(self) -> None:
        self.assertTrue(is_handoff("我要投诉，让你们经理来跟我说"))
        self.assertTrue(is_handoff("我不想跟机器聊了，转人工"))
        self.assertFalse(is_handoff("我想约个时间量房"))

    def test_five_primary_intents(self) -> None:
        self.assertEqual(classify_intent("这个贵不贵").intent, "price")
        self.assertEqual(classify_intent("周末能安排设计师过来量尺寸吗").intent, "measuring")
        self.assertEqual(classify_intent("安装后的门板有划痕").intent, "aftersales")
        self.assertEqual(classify_intent("你们店周日营业吗").intent, "chat")
        self.assertEqual(classify_intent("我要找负责人").intent, "handoff")

    def test_context_carries_wechat_intent(self) -> None:
        measuring = classify_intent(
            "这周六上午可以不，我下午要出去",
            history=("在吗，我想约个时间量房，房子在滨江那边",),
        )
        self.assertEqual((measuring.intent, measuring.topic), ("measuring", "measuring"))

        image = classify_intent(
            "我发张照片你看下 [图片] 挺明显的",
            history=("上个月装的柜子，门板有道划痕",),
        )
        self.assertEqual((image.intent, image.topic), ("aftersales", "aftersales"))
        self.assertNotIn("提供相关照片", build_reply(image, "我发照片了 [图片]"))

    def test_frustrated_customer_gets_relevant_reply(self) -> None:
        result = classify_intent("之前的问题一直没人回，服务太差了")
        reply = build_reply(result, "之前的问题一直没人回，服务太差了")
        self.assertEqual(result.intent, "aftersales")
        self.assertEqual(result.emotion, "frustrated")
        self.assertIn("等久了", reply)
        self.assertNotIn("装修阶段", reply)
        self.assertNotIn("更换", reply)

        replacement = classify_intent("到底能不能换，等这么久了")
        replacement_reply = build_reply(replacement, "到底能不能换，等这么久了")
        self.assertIn("能否更换", replacement_reply)
        self.assertIn("抱歉", replacement_reply)

    def test_follow_up_reply_does_not_repeat_known_question(self) -> None:
        first = classify_intent("你好，想问下定制衣柜大概怎么收费的")
        first_reply = build_reply(first, "你好，想问下定制衣柜大概怎么收费的")
        price = classify_intent("一个3米衣柜，先给个大概价格让我心里有数")
        second_reply = build_reply(price, "一个3米衣柜，先给个大概价格让我心里有数")
        self.assertIn("不能直接给您报价", first_reply)
        self.assertIn("想了解什么产品或空间", first_reply)
        self.assertIn("即使有尺寸，这边也不能直接报价", second_reply)
        self.assertIn("近期哪天方便量房", second_reply)
        self.assertNotEqual(first_reply, second_reply)

        whole_home_text = "我家也是三房，全屋做下来大概要多少钱呀"
        whole_home_reply = build_reply(classify_intent(whole_home_text), whole_home_text)
        self.assertIn("不能直接给出全屋报价", whole_home_reply)
        self.assertIn("想规划哪些产品或空间", whole_home_reply)

        measuring = classify_intent("这周六上午可以不", history=("我想预约量房",))
        measuring_reply = build_reply(measuring, "这周六上午可以不")
        self.assertIn("时间我先记下", measuring_reply)
        self.assertNotIn("合适的时间吗", measuring_reply)

        confirmation_history = ("在吗，我想约个时间量房，房子在滨江那边", "这周六上午可以不")
        confirmation = classify_intent("行 那到时候联系[抱拳]", history=confirmation_history)
        confirmation_reply = build_reply(
            confirmation, "行 那到时候联系[抱拳]", history=confirmation_history
        )
        self.assertEqual(confirmation.topic, "measuring")
        self.assertEqual(confirmation_reply, "好的，到时联系您。")

    def test_measuring_does_not_ask_for_known_location_again(self) -> None:
        first_text = "在吗，我想约个时间量房，房子在滨江那边"
        self.assertTrue(has_location((first_text,)))
        first = classify_intent(first_text)
        first_reply = build_reply(first, first_text)
        self.assertIn("哪天、哪个时间段", first_reply)
        self.assertNotIn("记录", first_reply)

        second_text = "这周六上午可以不，我下午要出去"
        second = classify_intent(second_text, history=(first_text,))
        second_reply = build_reply(second, second_text, history=(first_text,))
        self.assertIn("方便的时间我先记下", second_reply)
        self.assertNotIn("区域", second_reply)
        self.assertNotRegex(second_reply, r"提供.*城区|提供.*小区")

    def test_price_details_never_turn_into_an_offline_quote(self) -> None:
        first_text = "定制衣柜怎么收费"
        history = (first_text,)
        detail_text = "房子在滨江，想做衣柜和餐边柜"
        detail = classify_intent(detail_text, history=history)
        detail_reply = build_reply(detail, detail_text, history=history)
        self.assertEqual(detail.intent, "price")
        self.assertIn("下一步需要安排量房", detail_reply)
        self.assertIn("联系电话", detail_reply)
        self.assertFalse(is_forbidden_price_reply(detail_reply))

        test_phone = "".join(("138", "0013", "8000"))  # 仅测试格式的虚构号码，不是客户数据。
        phone_text = f"我电话是{test_phone}，周日下午方便"
        phone = classify_intent(phone_text, history=(*history, detail_text))
        phone_reply = build_reply(phone, phone_text, history=(*history, detail_text))
        self.assertNotIn(test_phone, phone_reply)
        self.assertIn("联系方式和方便的时间都收到了", phone_reply)

    def test_store_visit_follow_up_continues_to_discover_need(self) -> None:
        history = ("哈哈哈你们店周末开门不",)
        result = classify_intent("行吧，我路过在来看看😄", history=history)
        reply = build_reply(result, "行吧，我路过在来看看😄", history=history)
        self.assertEqual(result.topic, "store_visit")
        self.assertIn("主要想了解哪类产品或空间", reply)

    def test_soft_furnishing_inquiry_is_not_treated_as_cabinet_quote(self) -> None:
        for text in ("你们沙发多少钱", "想问一下餐桌怎么卖", "可以配窗帘和软装家具吗"):
            result = classify_intent(text)
            reply = build_reply(result, text)
            self.assertEqual(result.topic, "soft_furnishing")
            self.assertIn("不能直接报价", reply)
            self.assertIn("确认门店是否能够提供", reply)
            self.assertNotIn("安排量房", reply)

    def test_tone_is_consistent_and_apology_is_contextual(self) -> None:
        # 所有固定话术都使用同一套专业、温和表达，不混入口头语气词。
        inconsistent_phrases = ("～", "哎", "好嘞", "收到啦")
        for name, reply in SAFE_REPLIES.items():
            for phrase in inconsistent_phrases:
                self.assertNotIn(phrase, reply, f"{name}: {reply}")

        # 服务问题和客户不满需要道歉；普通询价不应无缘无故道歉。
        aftersales = build_reply(classify_intent("柜门有划痕"), "柜门有划痕")
        complaint = build_reply(classify_intent("我要投诉，找你们经理"), "我要投诉，找你们经理")
        price = build_reply(classify_intent("衣柜怎么收费"), "衣柜怎么收费")
        self.assertRegex(aftersales, r"不好意思|抱歉")
        self.assertIn("抱歉", complaint)
        self.assertNotRegex(price, r"不好意思|抱歉")

    def test_safe_e1_topics(self) -> None:
        self.assertEqual(classify_intent("你们板材环保吗，有检测报告吗").topic, "environment")
        self.assertEqual(classify_intent("硬装和餐边柜都能做吗").topic, "scope")
        self.assertEqual(classify_intent("质保期怎么确认").topic, "warranty")

    def test_emotion_requires_strong_signal(self) -> None:
        self.assertEqual(detect_emotion("在吗？？？")[0], "normal")
        self.assertEqual(detect_emotion("到底有没有人处理？？？")[0], "angry")
        self.assertEqual(detect_emotion("你们就是骗子黑店")[0], "angry")
        self.assertEqual(classify_intent("我要曝光你们").intent, "handoff")

    def test_every_template_passes_safety_guard(self) -> None:
        for name, reply in SAFE_REPLIES.items():
            self.assertTrue(is_safe_reply(reply), f"{name}: {reply}")
            self.assertFalse(is_forbidden_price_reply(reply), reply)

        samples = (
            "衣柜怎么收费", "能来量房吗", "安装后坏了", "门店在哪里", "你们案例很好看",
            "甲醛和板材怎么样", "你们能做餐边柜吗", "随便问问", "我要找人工",
        )
        for text in samples:
            result = classify_intent(text)
            self.assertTrue(is_safe_reply(build_reply(result, text)))

    def test_price_guard_matches_fullwidth_and_keycap_digits(self) -> None:
        self.assertTrue(is_forbidden_price_reply("大概１万元"))
        self.assertTrue(is_forbidden_price_reply("大概1️⃣万"))
        self.assertTrue(is_forbidden_price_reply("报价需要2天确认"))

    def test_internal_information_guard(self) -> None:
        self.assertFalse(is_safe_reply("飞书告警群的webhook配置在这里"))

    def test_customer_reply_does_not_disclose_automation(self) -> None:
        self.assertFalse(is_safe_reply("我是AI机器人，正在自动回复"))
        handoff = build_reply(classify_intent("我要找人工"), "我要找人工")
        self.assertIn("已经完整记录", handoff)
        self.assertNotIn("标记", handoff)
        self.assertNotIn("同事接手", handoff)
        self.assertNotIn("自动回复", handoff)
        self.assertNotIn("机器人", handoff)


if __name__ == "__main__":
    unittest.main()
