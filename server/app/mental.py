"""心智纯函数库（v0.3 · 对齐第二轮讨论裁决）：事件进、状态出，全部可单测、可回放。

v0.2 → v0.3 变更：
- kernel.value_priority（扁平列表）→ **value_tree 价值树**：终极价值/工具价值分层
  （Rokeach；means-end chain）+ 情景激活线索（Verplanken & Holland 2002：价值被
  情景线索激活才指导行为）。appraise 与 resolve_plan 的价值核对都改为沿树匹配。
- kernel.motive_profile **删除**（与价值树命中功能重复，McClelland 三重需要降级为
  创作工具，不进公式）。
- core_beliefs 的 is_core（二元）→ **resistance 连续抵抗力**（0~1）：核心信念只是
  抵抗力高的来源之一；更新公式 Δ ÷ (1+resistance)。
- 信念更新接入**关系来源系数 f(信任, 好感)**（relationships 表，对特定说话者此刻的
  态度；French & Raven：信任=信内容/好感=愿配合；恐惧不进信念，走服从通道）。
- hard_limits 从字符串列表 → **{rule, breaking[], threshold} 可破底线**：LLM 窄判定
  条件是否出现（是/否+依据），code 累积压力，过阈值才降级为"高代价选项"
  （Tetlock 神圣价值/禁忌交易）。
- perception 拆两通道：**environment**（对物：attentiveness→detect_change）与
  **testimony**（对话语：suspicion/min_acceptance→update_belief）。
- emotion 收编 self_control（抑制控制，Gross 情绪调节侧）；planning 删 planning_depth、
  expression 删 sociability（与思维链链长/外向性重复）——深度 = thinking_chain 环节数。
- 铁律不变：纯函数、无 IO；一切概率抽样经 seeded_rng 可回放；数值只进 code，
  LLM 只见词映射函数产出的状态词。
"""
import math
import random

# ---------------------------------------------------------------------------
# 常量：权重与查表写死在代码，不做参数（参数降维铁律——调权重 = 改代码 + 补单测）
# ---------------------------------------------------------------------------

_SIGMOID_K = 2.0          # detect_change 增益：salience=1、attentiveness=100 → P≈0.73
_DETECT_BIAS = 0.5        # 察觉偏置：salience×attentiveness 恰到 0.5 时 P=0.5
_GOAL_WEIGHT = 1.5        # 变化察觉：差异与当前目标相关 → 显著性加权（Folk 1992）
_NOVEL_WEIGHT = 1.2       # 变化察觉：从无到有的新出现 → 新异刺激加权
_BLEND_NEW = 0.6          # 情绪混合：新事件主导 0.6，旧情绪按衰减留存 0.4
_NEGATIVE_BIAS = 0.25     # 负性偏置：负面事件强度 ×(1+0.25×neuroticism/100)（Baumeister 2001）
_ANGRY_ACCEPT = 1.2       # 愤怒主导时采信增幅（怒增确信感，Lerner & Keltner 2000）
_FEAR_ACCEPT = 0.8        # 恐惧主导时采信降幅（惧增不确定感）
_AROUSAL_RATIONAL_LOSS = 0.3  # 高唤醒削弱理性：eff_rational=rational_bias×(1−0.3×arousal)（Arnsten 2009）
_PREEMPT_K = 3.0          # 抢占公式增益（校准例见单测）
_PREEMPT_BIAS = 0.35      # 抢占偏置：中庸参数默认走 System 2
_OPPORTUNITY_BASE = 0.6   # 机会触发阈值基线：salience ≥ 0.6×(1.2−flexibility/500)
_PRESSURE_PER_HIT = 35    # 底线压力：每次窄判定命中一条突破条件 → 压力 +35（0~threshold 封顶）
_SRC_BASE = 0.6           # 来源系数基线：信任/好感皆未知时的默认采信倾向
_SRC_TRUST = 0.4          # 信任每 100 点带来的采信增量（信内容）
_SRC_AFFECTION = 0.2      # 好感每 100 点带来的采信增量（愿往好处想）
_SRC_CAP = (0.5, 1.3)     # 来源系数上下限（再信任也不超过 1.3，再厌恶也至少 0.5）
_NEUTRAL_SOURCE = 0.8     # 无关系记录（陌生人）时的默认来源系数

# 行动打分权重（utility AI；motive 项已删，权重并入 goal/value）
_W_GOAL, _W_VALUE, _W_RISK = 0.45, 0.30, 0.25

# OCC 简化情绪族 → PAD 基向量 + 状态词（强→中→微，Russell 1980 象限落位）
FAMILIES = {
    "anger":   {"pad": (-0.7, 0.8, 0.6), "words": ("盛怒", "恼火", "有些烦躁")},
    "fear":    {"pad": (-0.6, 0.7, 0.25), "words": ("惊恐", "紧张不安", "隐隐发慌")},
    "sadness": {"pad": (-0.7, 0.2, 0.2), "words": ("悲痛欲绝", "闷闷不乐", "有些低落")},
    "disgust": {"pad": (-0.6, 0.4, 0.7), "words": ("深恶痛绝", "心存鄙夷", "略感反感")},
    "joy":     {"pad": (0.8, 0.6, 0.7), "words": ("狂喜", "劲头十足", "颇感振奋")},
    "calm":    {"pad": (0.5, 0.2, 0.6), "words": ("如释重负", "平静", "略感轻松")},
    "liking":  {"pad": (0.7, 0.4, 0.5), "words": ("满心欢喜", "颇有好感", "略生好感")},
}

# 行动情绪相容度：候选行动的 emotion_tags × 当前主导情绪族（缺省 0.3 中性）
ACTION_EMOTION_COMPAT = {
    "anger": {"attack": 0.9, "confront": 0.9, "shout": 0.85, "comfort": 0.1, "talk": 0.2, "cooperate": 0.1},
    "fear": {"flee": 0.9, "hide": 0.85, "observe": 0.6, "attack": 0.3, "talk": 0.4, "cooperate": 0.5},
    "sadness": {"withdraw": 0.8, "wait": 0.7, "attack": 0.2, "cooperate": 0.4, "talk": 0.5},
    "disgust": {"avoid": 0.85, "confront": 0.6, "cooperate": 0.15, "talk": 0.3},
    "joy": {"talk": 0.8, "cooperate": 0.8, "give_item": 0.7, "attack": 0.2},
    "calm": {"observe": 0.7, "wait": 0.6, "talk": 0.6, "cooperate": 0.6},
    "liking": {"talk": 0.85, "cooperate": 0.85, "give_item": 0.8, "comfort": 0.8},
}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, x))))


def seeded_rng(seed) -> random.Random:
    """从 seed 派生可回放随机源；字符串转稳定 int（跨进程同值）。
    调用方约定 seed = f"{session_id}:{npc_id}:{event_key}:{game_tick}"。"""
    if isinstance(seed, str):
        seed = int.from_bytes(seed.encode("utf-8"), "big") % (2 ** 63)
    return random.Random(seed)


def _mm_block(mm, block: str) -> dict:
    if not isinstance(mm, dict):
        return {}
    b = mm.get(block)
    return b if isinstance(b, dict) else {}


def _get(block: dict, key: str, default):
    v = block.get(key, default)
    return default if v is None else v


def _perception(mm) -> tuple:
    """取感知两通道（v0.3 嵌套结构），兼容旧扁平结构（迁移期数据）。"""
    per = _mm_block(mm, "perception")
    env = per.get("environment") if isinstance(per.get("environment"), dict) else per
    tes = per.get("testimony") if isinstance(per.get("testimony"), dict) else per
    return env, tes


def _hard_limit_rules(kernel: dict) -> list:
    """底线列表归一化：兼容旧字符串格式与 v0.3 dict 格式，统一返回 [{rule,...}]。"""
    rules = []
    for h in _get(kernel, "hard_limits", []):
        if isinstance(h, dict):
            rules.append(h)
        elif h:
            rules.append({"rule": str(h), "breaking": [], "threshold": 100})
    return rules


def _flatten_value_names(value_tree: dict) -> list:
    """价值树 → 按根先叶后的扁平名列表（行动打分的价值排名用）。"""
    names = []
    def walk(node):
        if not isinstance(node, dict) or not node.get("name"):
            return
        names.append(str(node["name"]))
        for ch in node.get("children", []) or []:
            walk(ch)
    walk(value_tree if isinstance(value_tree, dict) else {})
    return names


# ---------------------------------------------------------------------------
# 1. 价值树匹配（评估与计划重估的公共底座）
# ---------------------------------------------------------------------------

def match_value_tree(tags, value_tree, extra_cues=()) -> dict:
    """事件标签 × 价值树 → 相关性与激活路径。

    规则（§7.3-2 v0.3）：
      - 自根向叶找最深命中节点（节点名或激活线索 cues 与标签双向子串匹配）；
      - relevance = 根权重 × 路径上各子权重连乘 × 深度系数(根 0.7 / 叶 1.0)
                    × (1 + 0.1×额外激活线索命中数)，上限 1.0；
      - 无命中 → relevance=0（中性事件，情绪只衰减）。

    Returns:
        {"relevance": 0~1, "matched": [节点名...], "activated": [命中的线索...]}
    """
    tags = [str(t) for t in (tags or [])]
    extra_cues = [str(c) for c in (extra_cues or [])]
    tree = value_tree if isinstance(value_tree, dict) else {}

    def node_hit(node) -> bool:
        blob_texts = [str(node.get("name", ""))] + [str(c) for c in node.get("cues", []) or []]
        for t in tags + extra_cues:
            for b in blob_texts:
                if b and t and (b in t or t in b):
                    if b in extra_cues or t in extra_cues:
                        return True
                    # 节点名/线索与标签命中
                    if any(t in bb or bb in t for bb in blob_texts if bb):
                        return True
        return False

    best = {"relevance": 0.0, "matched": [], "activated": []}
    if not tree.get("name"):
        return best

    def walk(node, weight_product, path, depth):
        hit = node_hit(node)
        new_path = path + [str(node.get("name", ""))] if hit else path
        new_weight = weight_product * min(1.0, float(node.get("weight", 1.0)))
        if hit:
            # 越深越具体越相关：叶 1.0 > 中层 0.9 > 仅根 0.7
            depth_factor = 1.0 if not node.get("children") else (0.7 if depth == 0 else 0.9)
            rel = min(1.0, new_weight * depth_factor)
            if rel > best["relevance"]:
                cues_hit = [str(c) for c in node.get("cues", []) or []
                            if any(c in t or t in c for t in tags)]
                best.update({"relevance": rel, "matched": new_path, "activated": cues_hit})
        for ch in node.get("children", []) or []:
            walk(ch, new_weight, new_path, depth + 1)

    walk(tree, 1.0, [], 0)
    # 额外激活线索（如当前 goal 文本）只加成，不改变命中路径
    if best["matched"]:
        bonus = sum(1 for c in extra_cues
                    if any(c and (c in str(n) or str(n) in c) for n in best["matched"]))
        best["relevance"] = min(1.0, best["relevance"] * (1 + 0.1 * bonus))
    return best


# ---------------------------------------------------------------------------
# 2. detect_change —— 变化察觉（环境感知通道）
# ---------------------------------------------------------------------------

def detect_change(priors: dict, observation: dict, mm: dict,
                  goal_keywords=(), seed="detect") -> dict:
    """先验 vs 观测 → 我有没有"注意到"变化。

    公式：单条差异 base=1.0，goal 相关 ×1.5（目标导向注意），新出现 ×1.2；
          salience = max(各条)，上限 1.5；P = σ(2×(salience×attentiveness/100−0.5))。
    """
    env, _ = _perception(mm)
    attentiveness = float(_get(env, "attentiveness", 50))

    diffs = []
    for key, now in (observation or {}).items():
        was = (priors or {}).get(key)
        if was == now:
            continue
        weight = 1.0
        blob = f"{key} {now} {was}"
        if any(kw and kw in blob for kw in goal_keywords):
            weight *= _GOAL_WEIGHT
        if was is None:
            weight *= _NOVEL_WEIGHT
        diffs.append((f"{key}: {was}→{now}" if was is not None else f"{key}: {now}（新出现）", weight))
    if not diffs:
        return {"noticed": [], "salience": 0.0, "p_detect": 0.0, "detected": False}

    salience = min(1.5, max(w for _, w in diffs))  # 上限=base×目标加权，保区分度
    p_detect = sigmoid(_SIGMOID_K * (salience * attentiveness / 100.0 - _DETECT_BIAS))
    detected = seeded_rng(seed).random() < p_detect
    return {
        "noticed": [text for text, _ in diffs] if detected else [],
        "salience": salience, "p_detect": p_detect, "detected": detected,
    }


# ---------------------------------------------------------------------------
# 3. appraise_emotion —— 评估产生情绪（价值核对→情绪生成环节）
# ---------------------------------------------------------------------------

def _pick_family(event: dict, style: str) -> str:
    """OCC 情绪族判定（目标/标准/态度三分支 + 事件语义标签）。
    congruence=0（相关但说不好好坏）由调用方按中性处理，不进这里。"""
    congruence = float(event.get("congruence", 0))
    if congruence > 0:
        if event.get("liking"):
            return "liking"
        return "joy" if style == "goal-focused" else "calm"
    if event.get("blame") and style in ("goal-focused", "person-focused"):
        return "anger"
    if event.get("loss"):
        return "sadness"
    if style == "rule-focused":
        return "disgust"   # 重规矩：违规 → 鄙夷
    if style == "person-focused":
        # 重关系：有归因对象 → 愤怒；无对象泛化威胁 → 不安
        return "anger" if event.get("blame") else "fear"
    return "fear"          # 重目标（缺省）：威胁计划 → 焦虑


def appraise_emotion(event: dict, mm: dict, cur_emotion: dict = None) -> dict:
    """事件 ×（价值树/目标）→ 情绪向量（PAD）+ 主情绪词。

    公式：relevance = max(价值树匹配, 目标文本命中 0.5 档)；
          intensity = relevance × (0.5+0.5×意外度) × (0.4+0.6×reactivity)
                      × 负性偏置(负面 ×(1+0.25×neuroticism/100))；
          PAD = 族基向量 × intensity；new = 0.6×新 + 0.4×旧×(1−decay)。
    """
    emo = _mm_block(mm, "emotion")
    kernel = _mm_block(mm, "kernel")
    reactivity = float(_get(emo, "emotional_reactivity", 0.5))
    decay = float(_get(emo, "decay_rate", 0.2))
    style = str(_get(emo, "appraisal_style", "goal-focused"))
    neuroticism = float(_get(kernel, "neuroticism", reactivity * 100.0))  # 派生反推

    tags = [str(t) for t in event.get("tags", [])]
    goal_keywords = [str(k) for k in event.get("goal_keywords", []) if k]

    # 相关性 = max(价值树最深命中, goal 文本命中 0.5 档)
    vt = match_value_tree(tags, kernel.get("value_tree"), extra_cues=goal_keywords)
    relevance = vt["relevance"]
    goal_blob = " ".join(goal_keywords)
    if goal_blob and any(t and (t in goal_blob or goal_blob in t) for t in tags):
        relevance = max(relevance, 0.5)

    congruence = float(event.get("congruence", 0))
    cur = dict(cur_emotion or {})
    if relevance <= 0.0 or congruence == 0.0:
        # 中性事件（无关，或相关但说不好好坏）：情绪只衰减，不产生新情绪
        decayed = decay_emotion(cur, decay)
        word = cur.get("word") or emotion_words(decayed)["word"]
        return {"valence": decayed.get("valence", 0.0), "arousal": decayed.get("arousal", 0.0),
                "dominance": decayed.get("dominance", 0.5), "word": word,
                "intensity": 0.0, "family": "neutral", "relevance": relevance,
                "value_path": vt["matched"], "neutral": True}

    unexpected = min(1.0, max(0.0, float(event.get("unexpectedness", 0.5))))
    intensity = relevance * (0.5 + 0.5 * unexpected) * (0.4 + 0.6 * reactivity)
    if congruence < 0:
        intensity *= 1.0 + _NEGATIVE_BIAS * neuroticism / 100.0
    intensity = min(1.0, intensity)

    family = _pick_family(event, style)
    base_v, base_a, base_d = FAMILIES[family]["pad"]
    pad_new = (base_v * intensity, base_a * intensity, base_d * intensity)

    if cur.get("valence") is not None:
        keep = _BLEND_NEW
        valence = keep * pad_new[0] + (1 - keep) * float(cur.get("valence", 0)) * (1 - decay)
        arousal = keep * pad_new[1] + (1 - keep) * float(cur.get("arousal", 0)) * (1 - decay)
        dominance = keep * pad_new[2] + (1 - keep) * float(cur.get("dominance", 0.5)) * (1 - decay)
    else:
        valence, arousal, dominance = pad_new

    words = emotion_words({"valence": valence, "arousal": arousal, "dominance": dominance,
                           "intensity": intensity}, family=family)
    return {"valence": valence, "arousal": arousal, "dominance": dominance,
            "word": words["word"], "intensity": intensity,
            "family": family, "relevance": relevance,
            "value_path": vt["matched"], "neutral": False}


def decay_emotion(emotion: dict, decay_rate: float) -> dict:
    """每 tick 情绪衰减：×(1−decay_rate)，向中性回归。"""
    if not emotion:
        return {}
    k = 1.0 - min(1.0, max(0.0, float(decay_rate)))
    return {
        "valence": float(emotion.get("valence", 0.0)) * k,
        "arousal": float(emotion.get("arousal", 0.0)) * k,
        "dominance": 0.5 + (float(emotion.get("dominance", 0.5)) - 0.5) * k,
    }


# ---------------------------------------------------------------------------
# 4. update_belief —— 信念更新（证词评估环节；v0.3：抵抗力 + 关系来源系数）
# ---------------------------------------------------------------------------

def classify_family(emotion: dict) -> str:
    """PAD → 主导情绪族（怒/惧分界在支配感：怪罪别人 vs 受制于人）。"""
    v = float(emotion.get("valence", 0.0))
    a = float(emotion.get("arousal", 0.0))
    d = float(emotion.get("dominance", 0.5))
    if v >= 0:
        return "calm" if a < 0.35 else "joy"
    if a < 0.35:
        return "sadness"
    return "anger" if d >= 0.45 else "fear"


def source_coefficient(trust, affection) -> float:
    """对该说话者此刻的态度 → 采信来源系数。

    系数 = 0.6 + 0.4×trust/100 + 0.2×affection/100，夹在 [0.5, 1.3]；
    无关系记录（陌生人）→ 0.8。信任管"信不信内容"，好感管"愿不愿往好处想"
    （French & Raven：专家威信 / 参照性喜爱，两条独立通道）。
    """
    if trust is None and affection is None:
        return _NEUTRAL_SOURCE
    t = float(trust or 0)
    a = float(affection or 0)
    return max(_SRC_CAP[0], min(_SRC_CAP[1],
                                _SRC_BASE + _SRC_TRUST * t / 100.0 + _SRC_AFFECTION * a / 100.0))


def update_belief(conf: float, testimony_score: float, mm: dict,
                  cur_emotion: dict = None, resistance: float = 0.0,
                  supporting: bool = True, source_trust=None, source_affection=None) -> dict:
    """证词 → 置信度增量（v0.3 公式）：

      Δ = ±score × (1−suspicion/100) × 来源系数(trust,affection) × 情绪调制 × 100
          ÷ (1 + 抵抗力)
      min_acceptance：score×100 低于门槛 → 直接忽略（证据可采性）；
      四态：≥70 firm / ≥40 doubt / ≥20 shaken / <20 betrayed。
    """
    _, tes = _perception(mm)
    suspicion = float(_get(tes, "suspicion", 50))
    min_acceptance = float(_get(tes, "min_acceptance", 0))
    conf = max(0.0, min(100.0, float(conf)))

    if testimony_score * 100 < min_acceptance:
        return {"new_conf": conf, "state": belief_state(conf), "delta": 0.0, "ignored": True}

    family = classify_family(cur_emotion or {})
    mod = _ANGRY_ACCEPT if family == "anger" else (_FEAR_ACCEPT if family == "fear" else 1.0)
    src = source_coefficient(source_trust, source_affection)
    delta = (testimony_score * (1 - suspicion / 100.0) * src * mod * 100.0
             / (1.0 + max(0.0, min(1.0, float(resistance)))))
    if not supporting:
        delta = -delta
    new_conf = max(0.0, min(100.0, conf + delta))
    return {"new_conf": new_conf, "state": belief_state(new_conf),
            "delta": new_conf - conf, "ignored": False,
            "source_coeff": src, "emotion_mod": mod}


def belief_state(conf: float) -> str:
    if conf >= 70:
        return "firm"
    if conf >= 40:
        return "doubt"
    if conf >= 20:
        return "shaken"
    return "betrayed"


# ---------------------------------------------------------------------------
# 5. score_action —— 候选行动打分（行动选择环节；motive 项已删）
# ---------------------------------------------------------------------------

def _value_rank_score(tags, value_tree) -> float:
    """候选行动的价值对齐分：命中价值树节点越靠根/越深越相关（根先叶后扁平排名）。"""
    names = _flatten_value_names(value_tree)
    if not names:
        return 0.0
    best = 0.0
    for i, val in enumerate(names):
        if any(val in tag or tag in val for tag in tags):
            best = max(best, (len(names) - i) / len(names))
    return best


def _emotion_tendency(tags, cur_emotion) -> float:
    if not cur_emotion:
        return 0.3
    family = classify_family(cur_emotion)
    compat = ACTION_EMOTION_COMPAT.get(family, {})
    scores = [compat[tag] for tag in tags if tag in compat]
    return max(scores) if scores else 0.3


def score_action(candidates: list, mm: dict, cur_emotion: dict = None,
                 goal_alignment_of=None) -> dict:
    """打分：score = 0.45×goal + 0.30×value + (1−eff_rational)×emotion_tendency
              + 0.25×(1−risk)；hard_limits 命中候选标签 → 一票否决先于打分。"""
    kernel = _mm_block(mm, "kernel")
    emo = _mm_block(mm, "emotion")
    rational_bias = float(_get(emo, "rational_bias", 0.5))
    arousal = float((cur_emotion or {}).get("arousal", 0.0))
    eff_rational = rational_bias * (1 - _AROUSAL_RATIONAL_LOSS * arousal)
    value_tree = kernel.get("value_tree") if isinstance(kernel.get("value_tree"), dict) else {}
    rules = _hard_limit_rules(kernel)

    scores, excluded, best, top = {}, {}, None, None
    for cand in candidates or []:
        cid = str(cand.get("id", cand.get("label", "")))
        label_blob = f"{cid} {cand.get('label', '')} {' '.join(cand.get('emotion_tags', []))}"
        violated = next((r["rule"] for r in rules if r.get("rule") and any(
            kw in r["rule"] for kw in label_blob.split() if len(kw) >= 2)), None)
        if violated:
            excluded[cid] = violated
            continue
        goal_score = float(goal_alignment_of(cand)) if goal_alignment_of else float(cand.get("goal_alignment", 0.5))
        emotion_tags = [str(t) for t in cand.get("emotion_tags", [])]
        score = (
            _W_GOAL * max(0.0, min(1.0, goal_score))
            + _W_VALUE * _value_rank_score(
                [str(t) for t in cand.get("value_tags", [])] + emotion_tags, value_tree)
            + (1 - eff_rational) * _emotion_tendency(emotion_tags, cur_emotion)
            + _W_RISK * (1 - min(1.0, max(0.0, float(cand.get("risk", 0.5)))))
        )
        scores[cid] = score
        if top is None or score > best:
            top, best = cand, score
    return {"top_action": top, "scores": scores, "excluded": excluded}


# ---------------------------------------------------------------------------
# 6. decide_system —— System1/2 抢占（表达环节的调度规则）
# ---------------------------------------------------------------------------

def decide_system(mm: dict, cur_emotion: dict = None, seed="system") -> dict:
    """P(preempt) = σ(3×(impulsivity/100×(0.4+arousal) − self_control/200 − 0.35))
    self_control 在 v0.3 归情绪块（抑制控制）。"""
    ex = _mm_block(mm, "expression")
    emo = _mm_block(mm, "emotion")
    impulsivity = float(_get(ex, "impulsivity", 50))
    self_control = float(_get(emo, "self_control", 50))
    arousal = float((cur_emotion or {}).get("arousal", 0.2))
    p = sigmoid(_PREEMPT_K * (impulsivity / 100.0 * (0.4 + arousal)
                              - self_control / 200.0 - _PREEMPT_BIAS))
    preempt = seeded_rng(seed).random() < p
    return {"mode": "S1_preempt" if preempt else "S2", "p_preempt": p}


# ---------------------------------------------------------------------------
# 7. resolve_plan —— 计划检视与重评估（计划对照环节）
# ---------------------------------------------------------------------------

def resolve_plan(plan: dict, mm: dict, kernel: dict = None,
                 block_reasons=(), opportunities=()) -> dict:
    """受阻/机会 → 重评估策略。价值级目标 = goal 命中价值树根名（双向子串）
    或 stickiness='high' → 粘住目标；tenacity/flexibility 定策略。"""
    pl = _mm_block(mm, "planning")
    kernel = kernel if isinstance(kernel, dict) else _mm_block(mm, "kernel")
    tenacity = float(_get(pl, "tenacity", 50))
    flexibility = float(_get(pl, "flexibility", 50))
    goal = str((plan or {}).get("goal", ""))
    sticky = str((plan or {}).get("stickiness", "normal"))
    tree = kernel.get("value_tree") if isinstance(kernel.get("value_tree"), dict) else {}
    roots = [str(tree.get("name", ""))] + [str(ch.get("name", "")) for ch in tree.get("children", []) or []]

    reasons = [str(r) for r in block_reasons if r]
    if reasons:
        value_tier = sticky == "high" or any(
            r and goal and (r in goal or goal in r) for r in roots if r)
        if value_tier:
            if tenacity >= 50:
                return {"decision": "replan_means", "use_llm": True,
                        "reason": f"价值级目标受阻但坚持度高（tenacity={tenacity:g}）→ 换手段不换目标；受阻：{reasons[0]}"}
            return {"decision": "wait_and_watch", "use_llm": False,
                    "reason": f"价值级目标受阻且坚持度低 → 暂避风头等窗口；受阻：{reasons[0]}"}
        if flexibility >= 60:
            return {"decision": "change_goal", "use_llm": True,
                    "reason": f"非价值级目标且变通度高（flexibility={flexibility:g}）→ 换目标；受阻：{reasons[0]}"}
        return {"decision": "abandon", "use_llm": False,
                "reason": f"非价值级目标且变通度低 → 放弃转入空闲；受阻：{reasons[0]}"}

    if opportunities:
        threshold = _OPPORTUNITY_BASE * (1.2 - flexibility / 500.0)
        strong = [o for o in opportunities if float(o.get("salience", 0)) >= threshold]
        if strong:
            return {"decision": "evaluate_opportunity", "use_llm": True,
                    "reason": f"显著机会（salience≥{threshold:.2f}）：{strong[0].get('desc', '')}"}

    return {"decision": "continue", "use_llm": False, "reason": "检视无异常，按既定意图继续"}


# ---------------------------------------------------------------------------
# 8. 词映射（数值 → 状态词）：prompt 桥——铁律"数字不进 prompt"的唯一通道
# ---------------------------------------------------------------------------

def emotion_words(emotion: dict, family: str = None, words: tuple = None) -> dict:
    """PAD → 主情绪词 + 强度档 + 短语；family 优先用评估结论（防低强度错档）。

    words：模组词表覆盖（强/中/微 三元组，来自 world_pack.emotion_words）——
    存储层（npc_mental_state.emotion_word）始终用引擎默认词（跨模组稳定），
    显示层（prompt 渲染）按模组文风换词。缺省 None → FAMILIES 默认。"""
    if not emotion:
        return {"word": "平静", "degree": "微", "phrase": "此刻你心情平静", "family": "calm"}
    v = float(emotion.get("valence", 0.0))
    a = float(emotion.get("arousal", 0.0))
    d = float(emotion.get("dominance", 0.5))
    intensity = float(emotion.get("intensity", min(1.0, abs(v) + a)))

    family = family or classify_family(emotion)
    word_list = tuple(words) if words and len(words) == 3 else FAMILIES[family]["words"]
    idx = 0 if intensity >= 0.66 else (1 if intensity >= 0.33 else 2)
    degree = ("强", "中", "微")[idx]
    word = word_list[idx]
    phrase = f"此刻你可能{word}"
    if d < 0.3:
        phrase += "，甚至感到受制于人"
    elif d > 0.7:
        phrase += "，且自认握有主动"
    return {"word": word, "degree": degree, "phrase": phrase, "family": family}


# ---------------------------------------------------------------------------
# 8.1 情绪单一事实源（P0-a）：只读取词快照 + 反向映射（词 → 族/PAD）
# ---------------------------------------------------------------------------

def emotion_snapshot(state: dict) -> dict:
    """从心智热态派生一个【只读】情绪快照（不落库、不写任何状态）。

    单一事实源原则（P0-a）：情绪只在 `npc_mental_state` 热态存；旁白/前端要词
    一律从这里派生，**不要再从 npc_status 读 mood/emotion_word**（那是副事实源，
    会与热态不一致、造成重复）。

    state: db.get_mental_state(session_id, npc_id) 的返回值（或 mind_engine._init_state）
    Returns: {"word","phrase","family","emotion"}；空/无情绪时回退"平静"。
    """
    state = state or {}
    emotion = state.get("emotion") or {}
    if not emotion:
        return {"word": "平静", "phrase": "此刻你心情平静", "family": "calm", "emotion": {}}
    intensity = state.get("emotion_intensity")
    if intensity is None:
        intensity = min(1.0, abs(float(emotion.get("valence", 0.0))) + float(emotion.get("arousal", 0.0)))
    try:
        ew = emotion_words({**emotion, "intensity": float(intensity)})
    except Exception:  # noqa: BLE001  反查失败回退中性，绝不崩
        ew = {"word": "平静", "degree": "微", "phrase": "此刻你心情平静", "family": "calm"}
    return {"word": ew["word"], "phrase": ew["phrase"], "family": ew["family"],
            "emotion": emotion}


def _pad_for_family(family: str, idx: int) -> dict:
    """由族名 + 强度档（0强/1中/2微）还原一个 PAD 向量 + 状态词。"""
    meta = FAMILIES.get(family)
    if not meta:
        return _neutral_pad()
    pad = meta["pad"]  # (valence, arousal, dominance) 基向量
    intensity = (0.85, 0.5, 0.2)[idx]  # 与 emotion_words 档位比例一致（强/中/微）
    words = meta["words"]
    return {
        "valence": pad[0] * intensity,
        "arousal": pad[1] * intensity,
        "dominance": pad[2],
        "intensity": intensity,
        "word": words[idx],
        "family": family,
        "degree": ("强", "中", "微")[idx],
    }


def _neutral_pad() -> dict:
    """未识别/空词 → 中性回退（calm 中性），保证永远有一个合法 PAD 输出。"""
    return {"valence": 0.0, "arousal": 0.0, "dominance": 0.5,
            "intensity": 0.0, "word": "平静", "family": "calm", "degree": "微"}


def word_to_pad(word: str, family_words: dict = None) -> dict:
    """反向映射：中文情绪词 → {valence,arousal,dominance,intensity,word,family,degree}。

    词↔PAD 容错（P0-a）：导演/前端给中文词时，把词反查回 PAD 向量供热态落库，
    绕过"词≠PAD"的硬映射。复用 FAMILIES（一文一档）。

    family_words：可选，模组词表覆盖（family → (强,中,微) 三元组，来自
    world_pack.emotion_words 返回的元组）。缺省用 FAMILIES 默认词。
    未命中 / 空词 → 回退中性（不抛异常），保证永远可落库。
    """
    if not word:
        return _neutral_pad()
    word = str(word).strip()
    pools = family_words or {k: v["words"] for k, v in FAMILIES.items()}
    # ① 精确匹配"一文一档"（优先，结果最准）
    for family, words in pools.items():
        words = words or ()
        if not isinstance(words, (tuple, list)) or len(words) != 3:
            continue
        for idx, w in enumerate(words):
            if str(w) == word:
                return _pad_for_family(family, idx)
    # ② 容忍"词被 emotion_words 加过短语缀"（如"此刻你可能恼火"）——剥前后再试
    for family, words in pools.items():
        words = words or ()
        if not isinstance(words, (tuple, list)) or len(words) != 3:
            continue
        for idx, w in enumerate(words):
            if str(w) and (str(w) in word or word in str(w)):
                return _pad_for_family(family, idx)
    # ③ 未命中 → 中性回退
    return _neutral_pad()


def belief_words(state: str) -> str:
    return {"firm": "深信不疑", "doubt": "将信将疑",
            "shaken": "大为动摇", "betrayed": "彻底破灭"}.get(state, "将信将疑")


def suppress_words(composure: float) -> str:
    """镇定/掩饰（表达抑制，Gross 1998）：遮表情不改内心感受。"""
    composure = float(composure)
    if composure >= 70:
        return "你习惯不动声色，喜怒不形于色"
    if composure >= 40:
        return "你会斟酌措辞，不轻易流露心声"
    return "你的情绪很容易写在脸上"


def pressure_phrase(pressure: float, threshold: float) -> str:
    """底线压力 → 状态词：接近阈值时给 LLM 一句"念头压不住"的暗示。"""
    if threshold <= 0 or pressure <= 0:
        return ""
    ratio = min(1.0, pressure / float(threshold))
    if ratio >= 0.8:
        return "有个念头你越来越压不住了——你清楚那意味着什么"
    if ratio >= 0.5:
        return "某个念头偶尔冒出来，你把它按了回去"
    return ""


def value_words(value_tree: dict) -> str:
    """价值树 → 一句话人设（L1 块）：根 + 其余根级分支，无数字。"""
    if not isinstance(value_tree, dict) or not value_tree.get("name"):
        return ""
    root = str(value_tree["name"])
    others = [str(ch.get("name")) for ch in value_tree.get("children", []) or []
              if isinstance(ch, dict) and ch.get("name")]
    # 根级分支按权重排序展示前两个（权重本身不出现）
    ranked = sorted(value_tree.get("children", []) or [],
                    key=lambda ch: -float(ch.get("weight", 0)) if isinstance(ch, dict) else 0)
    top_others = [str(ch.get("name")) for ch in ranked[:2]
                  if isinstance(ch, dict) and ch.get("name")]
    if top_others:
        return f"你最看重{root}，其后是{'与'.join(top_others)}"
    return f"你最看重{root}"


def plan_status_words(plan: dict) -> str:
    """计划行 → 状态词（L5 块）：进度/受阻用人话，不出现步数数字。"""
    if not plan:
        return ""
    status = str(plan.get("status", "active"))
    goal = str(plan.get("goal", ""))
    steps = plan.get("steps") or []
    cur = int(plan.get("current_step", 1) or 1)
    total = max(1, len(steps))
    ratio = min(1.0, (cur - 1) / total)
    stage = "刚开始推进" if ratio < 0.34 else ("正在推进" if ratio < 0.7 else "接近终点")
    if status == "blocked":
        reason = str(plan.get("blocked_reason", "") or "遇到阻碍")
        return f"你正在推进『{goal}』，但计划受阻（{reason[:40]}），你心里正在盘算别的路子"
    return f"你正在推进『{goal}』，目前{stage}"


# ---------------------------------------------------------------------------
# 9. derive_from_personality —— 旧参数（OCEAN/cognitive）→ v0.3 迁移派生
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 10. 证词打分完整链 + 秘密双链（R3 深化：v4 §11/§13 落地，全部纯函数）
# ---------------------------------------------------------------------------

# 证据等级（fact-check，v4§11"玩家此刻是否真持有该证据"→ 未持有降级 hearsay 防嘴炮）
EVIDENCE_LEVELS = {
    "physical": 1.0,      # 物证在手：玩家持有相关物品，或声称与世界状态一致且身在现场
    "witnessed": 0.85,    # NPC 自己察觉过（上一帧观测/察觉记录支持该说法）
    "known_lore": 0.7,    # NPC 可访问的世界知识里有该内容（第二手）
    "heard_before": 0.5,  # 此前听人说过（工作记忆残留）
    "hearsay": 0.35,      # 空口/传闻封顶——什么背书都没有
    "bluff": 0.1,         # 声称被世界状态证伪（反被抓到撒谎 → 被动链识破进度）
}

# 意图 → 证词类型分（v4§11："若在举证才进打分"，其他话语对信念的影响大打折扣）
INTENT_TYPE_WEIGHT = {
    "举证": 1.0, "施压": 0.7, "威胁": 0.6, "求助": 0.5, "试探": 0.4,
    "询问": 0.4, "安抚": 0.3, "其他": 0.3, "闲聊": 0.25,
}

SECRET_LEVELS = ("guard", "hint", "half", "reveal")
# 口径状态词（prompt 用）：等级 → (态度词, 表达指令)
SECRET_LEVEL_WORDS = {
    "guard": ("守口如瓶", "无论对方怎么套话都只字不提，被逼问就含糊带过"),
    "hint": ("可以暗示", "可以给模糊的暗示或试探性反问，但绝不点破"),
    "half": ("可以说一半", "可以承认其中一部分，但关键细节含糊其辞"),
    "reveal": ("可以坦言", "如果对方直接问起，可以坦然承认"),
}


def normalize_threshold(t) -> float:
    """阈值归一：旧 isabella seed 用 0~1 尺度，现行 0~100（≤1 视为旧尺度 ×100）。"""
    v = float(t)
    return v * 100.0 if v <= 1.0 else v


def fact_check(claims: list, world_facts: dict, npc_evidence: dict) -> dict:
    """证据核实（v4§11）：玩家的声称有没有"真凭实据"支撑。

    Args:
        claims: 理解器的可核对声称 [{about: env_id, key: 状态键, value: 声称值}]；
                空列表 = 无可核对声称，按话题背书降级。
        world_facts: {f"{env_id}.{key}": 当前真值}（code 从环境卡/物品栏取，全知但只给 code）。
        npc_evidence: {"observed": [该 NPC 上一帧观测的键...],
                       "known": [可访问世界知识的标题/正文...],
                       "heard": [工作记忆条目...]}
    Returns:
        {"level": EVIDENCE_LEVELS 键, "bluff": bool, "checked": 核对的声称数}
    规则：声称与世界状态相反 → bluff（嘴炮被抓现行，进被动链）；
          一致且玩家持有该物 → physical；NPC 自己观测过 → witnessed；
          话题在世界知识/工作记忆里 → known_lore / heard_before；否则 hearsay。
    """
    observed = [str(k) for k in (npc_evidence or {}).get("observed", [])]
    known = [str(k) for k in (npc_evidence or {}).get("known", [])]
    heard = [str(k) for k in (npc_evidence or {}).get("heard", [])]

    checked = 0
    worst_is_bluff = False
    best = "hearsay"
    for c in claims or []:
        if not isinstance(c, dict):
            continue
        about, key = str(c.get("about", "")), str(c.get("key", ""))
        if not about or not key:
            continue
        checked += 1
        fk = f"{about}.{key}"
        if fk in (world_facts or {}):
            actual, claimed = world_facts[fk], c.get("value")
            same = str(actual).lower() == str(claimed).lower()
            if not same:
                worst_is_bluff = True  # 被世界状态证伪
                continue
            best = "physical"
            continue
        # 该键世界状态里没有（无法核对）：按 NPC 自身背书降级
        if any(o.startswith(f"{about}.") for o in observed):
            best = "witnessed" if best in ("hearsay", "heard_before") else best
        elif any(about in k or k in about for k in known):
            best = "known_lore" if best in ("hearsay", "heard_before") else best

    if checked == 0:
        # 无可核对声称：纯话题背书
        if known:
            best = "known_lore"
        elif heard:
            best = "heard_before"
    elif worst_is_bluff:
        best = "bluff"
    return {"level": best, "bluff": worst_is_bluff, "checked": checked}


def score_testimony(intent: str, evidence_level: str, relevance: float) -> float:
    """证词打分（v4§11）：score = 类型分 × 证据等级 × 关联度，输出 0~1。

    来源可信度不在此处相乘——update_belief 内部已按"对该说话者此刻的信任/好感"
    施加来源系数，避免双重计入（拆分见函数注释）。
    """
    type_w = INTENT_TYPE_WEIGHT.get(str(intent), 0.3)
    ev = EVIDENCE_LEVELS.get(str(evidence_level), EVIDENCE_LEVELS["hearsay"])
    return max(0.0, min(1.0, type_w * ev * max(0.0, min(1.0, float(relevance)))))


def reveal_level(secret: dict, trust, affection, arousal: float,
                 composure: float, hard_capped: bool = False) -> dict:
    """主动链（v4§13-A）：意愿驱动的泄露等级状态机 guard→hint→half→reveal。

    依据：社会渗透理论（关系递进→表露加深，Altman & Taylor）、情绪唤起说漏嘴
    （峰值超阈值临时降门槛）、表达抑制（Gross：composure 压住口误）、
    回报-成本（成本侧由底线封口承担——hard_capped 时锁 guard，底线优先）。

    Args:
        secret: {reveal_level: 当前档, active_triggers: [{type,threshold}]}
    Returns:
        {"level": 档, "word"/"gating": 口径状态词, "capped": bool}
    """
    cur = SECRET_LEVELS.index(str(secret.get("reveal_level", "guard"))) if \
        str(secret.get("reveal_level", "guard")) in SECRET_LEVELS else 0

    if hard_capped:
        lv = 0
    else:
        lv = cur
        for trig in secret.get("active_triggers", []) or []:
            if not isinstance(trig, dict):
                continue
            ttype, th = str(trig.get("type", "")), normalize_threshold(trig.get("threshold", 100))
            if ttype == "trust" and float(trust or 0) >= th:
                lv = max(lv, 1)
            elif ttype == "affection" and float(affection or 0) >= th:
                lv = max(lv, 1)
            elif ttype == "emotion" and float(arousal or 0) * 100.0 >= th:
                lv = max(lv, min(3, lv + 1))  # 情绪峰值说漏嘴：临时 +1，不超过可坦言
        if float(affection or 0) >= 70:
            lv = max(lv, min(3, lv + 1))          # 深亲密度：社会渗透再剥一层
        if float(composure or 0) >= 70 and lv > cur:
            lv = max(cur, lv - 1)                  # 高镇定者压住口误倾向
        lv = max(cur, min(3, lv))                  # 等级只升不降（单次对话内）
        if hard_capped:
            lv = 0

    level = SECRET_LEVELS[lv]
    word, gating = SECRET_LEVEL_WORDS[level]
    return {"level": level, "word": word, "gating": gating, "capped": hard_capped}


def contradiction_stance(count: int, composure: float,
                         detected_response: list = None) -> dict:
    """被动链（v4§13-B）：识破进度 → NPC 察觉"被看穿"后的姿态。

    count（detected_contradiction 累计）≥3 → 进入新博弈（选策略）；
    1~2 → 警觉但未摊牌。策略按 composure 调制（构造指南 I：镇定者抵赖，慌乱者坦白）。
    """
    responses = [str(r) for r in (detected_response or ["deny", "blame_shift", "confess"])]
    if count >= 3:
        if "confess" in responses and float(composure or 0) < 40:
            stance = "confess"
        elif float(composure or 0) >= 70 and "deny" in responses:
            stance = "deny"
        elif "blame_shift" in responses:
            stance = "blame_shift"
        else:
            stance = responses[0]
        words = {"deny": "抵赖到底，把话题岔开，绝不让步",
                 "blame_shift": "反咬一口，把怀疑引向别处",
                 "confess": "瞒不住了，承认吧"}
        return {"alert": True, "stance": stance, "word": words.get(stance, responses[0])}
    if count >= 1:
        return {"alert": True, "stance": None,
                "word": "对方的话让你心里一紧——他好像知道些什么"}
    return {"alert": False, "stance": None, "word": ""}


# ---------------------------------------------------------------------------
# 11. 秘密演化（世界时序 v2）：说漏嘴检测——代码侧确定性启发式，不靠 LLM 自报
# ---------------------------------------------------------------------------

def secret_slip(topic: str, text: str, level: str) -> bool:
    """文本是否"说漏"了秘密话题（2-gram 命中）。

    规则：口径已到 half/reveal（本来就可以说）不算漏；guard/hint 下文本触碰
    秘密话题的任意 2-gram 即算漏。中文无分词，2-gram 是轻量近似——
    宁可误报（多记一次"我说漏嘴"记忆）不可漏报（秘密无声流失）。
    """
    if str(level) in ("half", "reveal"):
        return False
    topic, text = str(topic or ""), str(text or "")
    if len(topic) < 2 or not text:
        return False
    grams = {topic[i:i + 2] for i in range(len(topic) - 1)}
    return any(len(g) == 2 and g in text for g in grams)


def slip_level(current: str) -> str:
    """说漏嘴 → 口径升一档，封顶 half（说漏不等于全盘托出）；
    已到 reveal 的不再变动（说漏不可能让口径倒退）。"""
    current = str(current)
    if current not in SECRET_LEVELS or current == "reveal":
        return "reveal" if current == "reveal" else "guard"
    idx = SECRET_LEVELS.index(current)
    return SECRET_LEVELS[min(2, idx + 1)] if idx < 3 else current


def derive_from_personality(ocean: dict, cognitive: dict) -> dict:
    """按派生公式生成 mental_model 局部（v0.3 嵌套结构）。手工创作参数不派生。"""
    ocean = ocean if isinstance(ocean, dict) else {}
    cognitive = cognitive if isinstance(cognitive, dict) else {}

    def _num(d, key, default=0.0):
        try:
            return float(d.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "emotion": {
            "emotional_reactivity": round(_num(ocean, "neuroticism", 50) / 100.0, 3),
            "rational_bias": round(_num(cognitive, "rational_ratio", 50) / 100.0, 3),
            "self_control": 50,
        },
        "planning": {
            "tenacity": _num(ocean, "conscientiousness", 50),
            "flexibility": _num(ocean, "openness", 50),
        },
        "expression": {
            "composure": _num(cognitive, "composure", 50),
        },
        "perception": {
            "environment": {"attentiveness": _num(cognitive, "perception", 50)},
            "testimony": {"suspicion": _num(cognitive, "suspicion", 50)},
        },
    }


# ---------------------------------------------------------------------------
# 10. 轨迹收敛（P0-c，方案C：高信息痕迹 + 同类去重，控制 token 且保关键叙事）
# ---------------------------------------------------------------------------

# 动作信息重要度：值越高越值得保留（按"对世界/认知有信息量"排序）。
# 参照方案 C 定档：trigger_event/interact/speak 高 > use_item/give_item > move > observe > wait。
ACTION_INFO_WEIGHT = {
    "trigger_event": 5, "speak": 5, "interact": 4, "attack": 4,
    "give_item": 3, "use_item": 3,
    "move": 2, "observe": 1,
    "wait": 0,  # 纯等待无信息量，剔除
}


def converge_own_timeline(own_traces: list, max_lines: int = 8) -> list:
    """把「自己作为 actor 的痕迹」收敛成高信息、去重的时间线文本行（P0-c 方案C）。

    输入 world_trace 元组（顺序同 db.get_recent_traces SELECT）：
        [t0]=tick, [t1]=actor, [t2]=action_type, [t3]=target,
        [t4]=location, [t5]=detail, [t6]=visible_to
    规则：
        · 剔除无信息量动作（wait，权重 0）；
        · 同类动作去重：同一 actor 同一 action_type 只保留【最近一次】（避免"缓步走进，
          又走进，再走进"刷屏）；
        · 按信息权重降序 @ 时间倒序排，截取前 max_lines 条；
    返回收敛后的人类可读文本行列表（带"tick 前"感，不含裸数字 token——detail 已人话化）。
    """
    if not own_traces:
        return []
    # ① 剥掉无信息量动作 + 按 (权重, tick) 排序，同类只留最新
    scored = []
    for t in own_traces:
        if not isinstance(t, (tuple, list)) or len(t) < 6:
            continue
        atype = str(t[2] or "").lower()
        weight = ACTION_INFO_WEIGHT.get(atype, 1)
        if weight <= 0:
            continue
        tick = t[0]
        scored.append((weight, tick if isinstance(tick, (int, float)) else 0, atype, t))
    # 按权重降序（同级按 tick 新在前），保证"同类只留最新"用 first-wins 实现
    scored.sort(key=lambda s: (-s[0], -s[1]))
    seen_types = set()
    out = []
    for weight, _tick, atype, t in scored:
        if atype in seen_types:
            continue  # 同类已保留更早（更高权/更新）的一条
        seen_types.add(atype)
        detail = str(t[5] or t[2] or "").strip()
        if detail:
            out.append(detail)
        if len(out) >= max_lines:
            break
    return out
