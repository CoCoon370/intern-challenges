"""E3 的意图判断、情绪升级、安全知识与回复模板。

这一层不依赖浏览器，方便单独测试。公开硬规则使用确定性正则；普通意图
使用高精度短语、最近上下文和字符 n-gram 相似度。评测环境无外网、无
LLM key 时也能完整运行。
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Sequence


# 与公开评测规则保持一致。命中后必须最多回复一次并停止该会话。
HANDOFF_RE = re.compile(r"投诉|人工|经理|负责人|真人|客服|不想跟机器")

# 高精度情绪升级：只覆盖明显辱骂、外部维权或强烈失控表达，避免普通问号误判。
ABUSE_RE = re.compile(
    r"垃圾|骗子|黑店|坑人|废物|傻逼|脑残|有病|妈的|他妈|操你|艹|滚蛋|狗屁"
)
ESCALATION_RE = re.compile(r"曝光|报警|消协|12315|起诉|法院|媒体|差评")
NEGATIVE_RE = re.compile(
    r"没人回|没回复|不处理|不解决|服务太差|太差了|等这么久|拖了|又坏|到底|怎么回事|什么意思"
)
PUNCTUATION_BURST_RE = re.compile(r"[?？!！]{3,}")

# 只判断客户是否已经提供房屋位置，不尝试维护全国行政区词典。
# 覆盖“房子在某城区那边、位于某区域、小区在某片区”等常见微信说法。
LOCATION_RE = re.compile(
    r"(?:房子|新房|小区|项目)?(?:在|位于)([一-鿿]{2,12}?)(?:那边|这边|附近|区域|区|小区|，|。|,|$)"
)
MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

# 与评测器相同的价格输出禁令；任何候选回复发出前都要经过它。
PRICE_RE = re.compile(r"\d[\d,.]*\s*(元|块|万|k|K|/㎡|每平|一平|平米|平方|㎡)")
PRICE_WORD_RE = re.compile(r"报价|均价|大概")
DIGIT_RE = re.compile(r"\d")

# 不允许知识库或模板把内部系统、配置和凭证带给客户。
INTERNAL_RE = re.compile(r"webhook|token|secret|飞书告警群|crm字段|补偿权限|运营负责人", re.I)
AUTOMATION_DISCLOSURE_RE = re.compile(r"\bai\b|人工智能|机器人|自动回复|大模型", re.I)


@dataclass(frozen=True)
class IntentResult:
    """保存主意图、细分主题、情绪与可解释原因。"""

    intent: str
    topic: str
    confidence: float
    emotion: str
    reason: str


def normalize(text: str) -> str:
    """统一全角字符、空白和大小写，避免同一句话出现多种写法。"""

    value = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"\s+", "", value)


def is_handoff(text: str) -> bool:
    """判断客户是否触发公开契约中的转人工规则。"""

    return bool(HANDOFF_RE.search(normalize(text)))


def extract_location(texts: Sequence[str]) -> str | None:
    """从最近的消息中提取客户主动提供的房屋区域。"""

    for text in reversed(texts):
        match = LOCATION_RE.search(normalize(text))
        if match:
            return match.group(1)
    return None


def has_location(texts: Sequence[str]) -> bool:
    """判断当前消息或历史消息里是否已经提供房屋所在区域。"""

    return extract_location(texts) is not None


def detect_emotion(text: str, history: Sequence[str] = ()) -> tuple[str, str]:
    """识别明确愤怒和普通不满；普通连续问号不会单独触发转人工。"""

    cleaned = normalize(text)
    if ABUSE_RE.search(cleaned):
        return "angry", "出现明显辱骂"
    if ESCALATION_RE.search(cleaned):
        return "angry", "出现外部维权或曝光表达"

    negative_now = bool(NEGATIVE_RE.search(cleaned))
    recent_negative = any(NEGATIVE_RE.search(normalize(item)) for item in history[-3:])
    if PUNCTUATION_BURST_RE.search(cleaned) and (negative_now or recent_negative):
        return "angry", "连续强烈标点且当前或近期存在不满"
    if negative_now:
        return "frustrated", "出现不满或催促表达"
    return "normal", "未发现明显情绪风险"


def is_forbidden_price_reply(text: str) -> bool:
    """发送前检查回复是否含评测禁止的价格表达。"""

    value = normalize(text).replace("️", "").replace("⃣", "")
    return bool(PRICE_RE.search(value) or (PRICE_WORD_RE.search(value) and DIGIT_RE.search(value)))


def is_safe_reply(text: str) -> bool:
    """统一的最后安全闸门：不报价，也不泄露内部配置。"""

    return bool(
        text.strip()
        and not is_forbidden_price_reply(text)
        and not INTERNAL_RE.search(text)
        and not AUTOMATION_DISCLOSURE_RE.search(text)
    )


# 细分主题只用于决定回复内容；最终 intent 始终落在题目要求的五类。
TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "aftersales": (
        "售后", "划痕", "损坏", "维修", "换掉", "能不能换", "更换", "返修", "开裂",
        "鼓包", "脱胶", "装坏", "安装后", "装完", "门板", "五金坏", "异味",
        "问题一直没人回", "一直没人回", "服务太差", "没人处理",
    ),
    "price": (
        "多少钱", "收费", "价格", "价位", "预算", "贵不贵", "费用", "做下来", "怎么计价",
        "单价", "优惠", "活动价", "先有个数",
    ),
    "measuring": (
        "量房", "量尺寸", "上门测量", "预约", "设计师过来", "交房", "交付", "复尺",
    ),
    "store_visit": (
        "门店", "地址", "在哪里", "在哪儿", "开门", "营业", "到店", "过去看看", "店里",
    ),
    "environment": (
        "甲醛", "环保", "enf", "e0", "检测报告", "板材等级", "颗粒板", "多层板", "欧松板",
    ),
    "warranty": ("质保", "保修", "保几年", "质保期"),
    "scope": (
        "做什么", "能做", "做不做", "全屋定制包括", "衣柜", "橱柜", "餐边柜", "榻榻米",
        "鞋柜", "电视柜", "阳台柜", "实木家具", "硬装", "软装",
    ),
    "schedule": ("工期", "多久装好", "什么时候装", "生产多久", "交付周期", "加急"),
    "compliment": ("好看", "喜欢", "不错", "漂亮", "案例", "刷到", "奶油风"),
    "acknowledgement": ("好的", "好吧", "行", "可以", "知道了", "谢谢", "ok", "收到", "抱拳"),
}

# 同时命中时，风险更高、动作更明确的主题优先。
TOPIC_PRIORITY = (
    "aftersales", "price", "measuring", "store_visit", "environment", "warranty",
    "schedule", "scope", "compliment", "acknowledgement",
)

TOPIC_TO_INTENT = {
    "price": "price",
    "measuring": "measuring",
    "aftersales": "aftersales",
    "store_visit": "chat",
    "environment": "chat",
    "warranty": "chat",
    "scope": "chat",
    "schedule": "chat",
    "compliment": "chat",
    "acknowledgement": "chat",
    "general": "chat",
}

# 没有关键词时，用这些真实微信式说法做离线相似度兜底。
PROTOTYPES: dict[str, tuple[str, ...]] = {
    "price": (
        "全屋做下来需要准备多少预算", "我想先了解一下价位", "普通家庭做这个贵不贵",
    ),
    "measuring": (
        "周末能安排人来看看尺寸吗", "这周六上午可以不", "房子快交付想先做设计",
    ),
    "aftersales": (
        "柜子安装以后出了问题", "照片发你了挺明显的", "一直没有人处理能换吗",
    ),
    "store_visit": (
        "我想先去你们店里看看", "周末你们那边有人吗", "怎么去你们展厅",
    ),
    "environment": (
        "你们用的板子环保标准怎么样", "装好以后会不会有味道", "可以看材料检测文件吗",
    ),
    "scope": (
        "哪些柜子可以一起定制", "你们除了衣柜还可以做什么", "硬装也包含在里面吗",
    ),
    "compliment": (
        "刷到你们家的装修案例了", "这种风格挺喜欢的", "设计效果看起来不错",
    ),
}


def _char_vector(text: str) -> Counter[str]:
    """把中文短句变成二元、三元字符向量，离线比较相似表达。"""

    cleaned = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", normalize(text))
    vector: Counter[str] = Counter()
    for size in (2, 3):
        for index in range(max(0, len(cleaned) - size + 1)):
            vector[cleaned[index : index + size]] += 1
    return vector


def _cosine(left: Counter[str], right: Counter[str]) -> float:
    """计算两个稀疏字符向量的余弦相似度。"""

    if not left or not right:
        return 0.0
    common = left.keys() & right.keys()
    dot = sum(left[key] * right[key] for key in common)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


PROTOTYPE_VECTORS = {
    topic: tuple(_char_vector(example) for example in examples)
    for topic, examples in PROTOTYPES.items()
}


def _keyword_topic(text: str) -> tuple[str | None, list[str]]:
    """返回第一组高优先级主题及其命中词。"""

    cleaned = normalize(text)
    for topic in TOPIC_PRIORITY:
        matches = [word for word in TOPIC_KEYWORDS[topic] if normalize(word) in cleaned]
        if matches:
            return topic, matches
    return None, []


def _context_topic(history: Sequence[str]) -> str | None:
    """从最近客户消息中寻找可延续的业务主题。"""

    for item in reversed(history[-4:]):
        topic, _ = _keyword_topic(item)
        if topic and topic not in {"acknowledgement", "compliment"}:
            return topic
    return None


def classify_intent(text: str, history: Sequence[str] = ()) -> IntentResult:
    """结合当前消息和最近微信上下文，输出题目要求的五类主意图。"""

    cleaned = normalize(text)
    if is_handoff(cleaned):
        return IntentResult("handoff", "explicit_handoff", 1.0, "angry", "命中公开转人工硬规则")

    emotion, emotion_reason = detect_emotion(cleaned, history)
    if emotion == "angry":
        return IntentResult("handoff", "emotion_escalation", 0.96, emotion, emotion_reason)

    topic, matches = _keyword_topic(cleaned)

    # “[图片]”“周六上午可以吗”这类短句依赖上一轮业务上下文。“可以”这类
    # 礼貌词不能盖过上一轮明确的量房或售后主题。
    previous_topic = _context_topic(history)
    if MOBILE_RE.search(cleaned) and previous_topic:
        return IntentResult(
            TOPIC_TO_INTENT[previous_topic], previous_topic, 0.98, emotion,
            f"收到联系方式并承接最近主题: {previous_topic}; {emotion_reason}",
        )

    # 询价后客户补充区域或柜体范围，视为销售线索补充，继续推进量房，
    # 而不是把它误判为一个全新的泛咨询。
    if previous_topic == "price" and (has_location((text,)) or topic == "scope"):
        return IntentResult(
            "price", "price", 0.90, emotion,
            f"客户补充询价所需的区域或定制范围; {emotion_reason}",
        )
    follow_up = bool(
        re.search(
            r"图片|照片|周[一二三四五六日天末]|上午|下午|晚上|可以不|行不行|那就|这个呢|路过|过来|看看",
            cleaned,
        )
    )
    acknowledgement_continuation = bool(
        topic == "acknowledgement" and re.search(r"到时候|联系|回头|再约|抱拳", cleaned)
    )
    if previous_topic and (follow_up or acknowledgement_continuation) and topic in {None, "acknowledgement"}:
        return IntentResult(
            TOPIC_TO_INTENT[previous_topic], previous_topic, 0.82, emotion,
            f"短句承接最近主题: {previous_topic}; {emotion_reason}",
        )

    if topic:
        intent = TOPIC_TO_INTENT[topic]
        confidence = min(0.99, 0.86 + 0.03 * (len(matches) - 1))
        return IntentResult(intent, topic, confidence, emotion, f"命中短语: {', '.join(matches)}; {emotion_reason}")

    query_vector = _char_vector(cleaned)
    scores = {
        topic_name: max((_cosine(query_vector, vector) for vector in vectors), default=0.0)
        for topic_name, vectors in PROTOTYPE_VECTORS.items()
    }
    best_topic, best_score = max(scores.items(), key=lambda item: item[1])
    second_score = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0.0

    # 分数低或前两类非常接近时不强行理解，改用安全澄清。
    if best_score < 0.20 or best_score - second_score < 0.035:
        return IntentResult("chat", "general", best_score, emotion, f"分类把握不足; {emotion_reason}")
    return IntentResult(
        TOPIC_TO_INTENT[best_topic], best_topic, best_score, emotion,
        f"离线文本相似度最接近: {best_topic}; {emotion_reason}",
    )


# 从 E1 对客文件提炼的安全知识卡片。没有复制价格、内部系统、实时数据和补偿权限。
SAFE_REPLIES = {
    "handoff": "好的，您的情况我已经完整记录，麻烦您稍等一下。",
    "handoff_apology": "很抱歉给您带来不好的体验。您的情况我已经完整记录，麻烦您稍等一下。",
    "price": "这边不能直接给您报价，需要量房并确认方案后才能核算。您可以先告诉我房子所在区域和准备做哪些柜子；如果暂时不方便确定量房时间，也可以留一个联系电话，方便后续沟通。",
    "price_followup": "谢谢您补充尺寸。即使有尺寸，这边也不能直接报价，还需要量房并确认方案。您房子在哪个区域，近期哪天方便量房？也可以留一个联系电话，方便确认安排。",
    "price_whole_home": "这边不能直接给出全屋报价，需要量房并确认具体方案。您可以先告诉我房子所在区域、准备做哪些柜子和目前的装修阶段；也可以留下联系电话和方便量房的时间。",
    "price_details_received": "了解，您的区域和定制需求我先登记。下一步需要安排量房，您哪天、哪个时间段方便？也可以留一个联系电话，便于确认上门时间。",
    "contact_received": "好的，联系方式收到了。您哪天、哪个时间段方便量房？",
    "contact_and_time_received": "好的，联系方式和方便的时间都收到了，后续会再确认具体上门安排。",
    "measuring": "可以预约。您把房子所在区域和方便的日期、时间段发给我，我帮您登记量房需求。",
    "measuring_location_known": "可以预约。您哪天、哪个时间段比较方便？",
    "measuring_time": "可以，时间我先记下。房子在哪个城区或小区？",
    "measuring_ready": "可以，您方便的时间我先记下，后续会再确认具体上门安排。",
    "measuring_confirmation": "好的，到时联系您。",
    "aftersales": "不好意思，给您添麻烦了。您把具体问题和相关照片发给我，我先完整登记，这种情况需要售后同事进一步核实。",
    "aftersales_image": "照片收到了，辛苦您补充。我会和前面描述的问题一起记录；只凭图片暂时不能确定处理方案，需要售后同事核实后再给您明确答复。",
    "aftersales_frustrated": "确实让您等久了，很抱歉。前面一直没人回复的情况和您这次催促，我会一起完整登记，这种情况需要售后同事进一步核实。",
    "aftersales_replacement": "确实让您等久了，很抱歉。能否更换需要售后核实，我现在不能随意承诺；您的换板诉求和催促我会一起完整登记。",
    "store_visit": "可以的。不同区域对应的门店不一样，您告诉我所在的城市或城区，我帮您确认地址和营业时间。",
    "store_followup": "欢迎您过来。您这次主要想看衣柜、全屋方案，还是先了解板材和工艺？您告诉我大概需求，我也好提前帮您准备。",
    "environment": "可以提供检测报告。柜体使用ENF级板材，不过任何人造板都不能承诺零甲醛；门店可以提供对应批次的材料检测报告。您如果有特别关注的空间或板材，也可以一起告诉我。",
    "warranty": "明白您关心质保。具体范围需要结合产品类型、签约时间和合同确认，我不想在没有核实的情况下答错。您把订单情况发给我，我先登记，再请售后同事确认。",
    "scope": "衣柜、橱柜、玄关柜、餐边柜、书柜和阳台柜等柜类产品都可以一起规划。水电、瓷砖和吊顶属于硬装，不在全屋定制的范围内。您可以说一下准备做哪些空间，我帮您进一步梳理。",
    "schedule": "明白您想提前安排时间。交付周期会受空间数量、方案确认和生产排期影响，我先不随意承诺；完成量房和设计后会更容易确认。您可以先告诉我目前的装修进度。",
    "compliment": "谢谢您的认可。方便告诉我户型、所在区域和目前的装修进度吗？我可以根据您喜欢的案例继续帮您梳理需求。",
    "acknowledgement": "好的，后续有需要您随时联系我。",
    "general": "您好，我在的。您主要想了解哪一方面？可以告诉我户型、所在区域或目前的装修进度，我帮您一起梳理。",
}


def build_reply(result: IntentResult, text: str, history: Sequence[str] = ()) -> str:
    """根据主意图与细分主题生成一条自然、安全的门店回复。"""

    cleaned = normalize(text)
    previous_topic = _context_topic(history)
    current_topic, _ = _keyword_topic(text)
    location_in_current = has_location((text,))
    location_known = has_location((*history, text))
    time_in_current = bool(re.search(r"周[一二三四五六日天末]|上午|下午|晚上|几点|今天|明天|后天", cleaned))
    if result.intent == "handoff":
        complaint_context = bool(
            "投诉" in cleaned
            or ABUSE_RE.search(cleaned)
            or ESCALATION_RE.search(cleaned)
            or NEGATIVE_RE.search(cleaned)
        )
        reply = SAFE_REPLIES["handoff_apology" if complaint_context else "handoff"]
    elif MOBILE_RE.search(cleaned) and time_in_current:
        reply = SAFE_REPLIES["contact_and_time_received"]
    elif MOBILE_RE.search(cleaned):
        reply = SAFE_REPLIES["contact_received"]
    elif result.intent == "price" and previous_topic == "price" and (
        location_in_current or current_topic == "scope"
    ):
        reply = SAFE_REPLIES["price_details_received"]
    elif result.intent == "price" and re.search(r"全屋|三房|四房|多个空间|几个房间", cleaned):
        reply = SAFE_REPLIES["price_whole_home"]
    elif result.intent == "price" and re.search(r"先有个数|心里有数|[0-9一二三四五六七八九十]+米", cleaned):
        reply = SAFE_REPLIES["price_followup"]
    elif result.intent == "measuring" and re.search(r"到时候|联系|抱拳", cleaned):
        reply = SAFE_REPLIES["measuring_confirmation"]
    elif result.intent == "measuring" and time_in_current and location_known:
        reply = SAFE_REPLIES["measuring_ready"]
    elif result.intent == "measuring" and time_in_current:
        reply = SAFE_REPLIES["measuring_time"]
    elif result.intent == "measuring" and location_in_current:
        reply = SAFE_REPLIES["measuring_location_known"]
    elif result.intent == "aftersales" and result.emotion == "frustrated" and re.search(r"换|更换", cleaned):
        reply = SAFE_REPLIES["aftersales_replacement"]
    elif result.intent == "aftersales" and result.emotion == "frustrated":
        reply = SAFE_REPLIES["aftersales_frustrated"]
    elif result.intent == "aftersales" and re.search(r"图片|照片", cleaned):
        reply = SAFE_REPLIES["aftersales_image"]
    elif result.topic == "store_visit" and previous_topic == "store_visit":
        reply = SAFE_REPLIES["store_followup"]
    elif result.intent in {"price", "measuring", "aftersales"}:
        reply = SAFE_REPLIES[result.intent]
    else:
        reply = SAFE_REPLIES.get(result.topic, SAFE_REPLIES["general"])

    # 模板未来即使被修改，也不能越过最后一道安全闸门。
    if not is_safe_reply(reply):
        return SAFE_REPLIES["general"]
    return reply
