"""指称消解器（Referent Grounding）：自然语言指称 → 世界实体 id。

问题定性（架构教训，2026-09-07）：这是"召回 → 打分 → 兜底"的排序问题，
**不是词表枚举问题**。此前的实现用词典（量词表"一把/一柄…"、部件表"腿/扶手…"、
场景映射"房间三→room_3"）解决它——词典永远不完备（汉语量词几十个、部件由内容
创作决定、换世界全崩），且词典层"碰巧命中"会抢跑：本该交给 LLM 精配的难例被
错误地本地消化（confidently wrong，比没命中更危险）。

核心洞察：量词/指示词/部件词**不需要枚举**——
- 字符 bigram 双向 F1 对"一把椅子 vs 我拆下椅子腿"天然高分（"椅子"两字都命中），
  而"一把"这个量词根本不在 hint 里、不参与比对——量词问题被正确的度量**消解**；
- 部件是**数据**（environment_card.state.parts 由拆解系统维护），不是词典；
- 场景对齐用**数字对齐**（"房间三"↔room_3 名内数字相等），不需要映射表。

四层级联（closed-world：候选只来自世界状态，LLM 兜底只做选择题、不可能幻觉）：
  L0 精确 id 命中（1.0）
  L1 候选召回：场景实体 + 持有物 + 全世界非房间兜底（召回在本模块外做，候选可注入）
  L2 n-gram 打分：bigram F1 + 单字覆盖 + 数字对齐 + 先验（在场加成/全球兜底衰减）
  L3 兜底：need_llm=True，附排序后的候选清单（上层 LLM 做选择题）

铁律：本模块是**纯函数**（候选注入、无 IO）——确定性可回放、可对任意未知实体压测。
"""
import re

# 打分权重（写死常量——参数降维铁律：调权重=改代码+补单测）
_W_BIGRAM = 0.6        # bigram F1：主相似度（抗量词/指示词/语序）
_W_UNI = 0.4           # 名字单字覆盖率：兜住单字/短名实体（"刀"）
_DIGIT_BONUS = 0.25    # 数字对齐加成（房间三↔room_3）
_DIGIT_MISMATCH = 0.4  # 数字错位惩罚系数（房间一 vs room_3 → 分数×0.6）
_GLOBAL_DAMP = 0.85    # 全世界兜底候选的衰减（优先在场物）
_ACCEPT = 0.34         # 接受阈值：≥此分且对次名有明显优势 → 本地消解
_MARGIN = 0.06         # 次名优势阈值：分差不足 → 交 LLM 选择题
_TOPK_FOR_LLM = 5      # 交给 LLM 的候选数

_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "0": 0, "1": 1,
              "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9}


def _bigrams(text: str) -> set:
    t = re.sub(r"\s+", "", str(text or ""))
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _skipgrams(text: str) -> set:
    """跳跃 1 字的 2-gram（i, i+2）：捕捉"干柴/捆柴"这类插入一字后的形态变异。"""
    t = re.sub(r"\s+", "", str(text or ""))
    if len(t) < 3:
        return set()
    return {t[i] + t[i + 2] for i in range(len(t) - 2)}


def _windows(name: str) -> set:
    """实体的"核心词窗口"集合 = 连续 bigram ∪ 跳跃 bigram。
    任一窗口作为子串出现在 hint 中 = 强证据（用户指的就是这个核心名词）。"""
    return _bigrams(name) | _skipgrams(name)


def _unigrams(text: str) -> set:
    return {ch for ch in str(text or "") if ch.strip()}


def _digits(text: str) -> set:
    """文本里的数字集合（中文数字/阿拉伯数字统一）：'房间三'→{3}，'room_3'→{3}。"""
    return {_CN_DIGITS[ch] for ch in str(text or "") if ch in _CN_DIGITS}


def bigram_f1(a: str, b: str) -> float:
    """字符 bigram 双向 F1（0~1）：抗量词/指示词/语序的主相似度。"""
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    inter = len(ba & bb)
    return 2.0 * inter / (len(ba) + len(bb))


def _name_coverage(name: str, hint: str) -> float:
    """名字单字被 hint 覆盖的比例（方向性：实体名的字出现在指称里）。"""
    uni = _unigrams(name)
    if not uni:
        return 0.0
    h = _unigrams(hint)
    return len(uni & h) / len(uni)


def _digit_relation(a: str, b: str) -> float:
    """数字对齐关系：1.0 对齐 / 0.0 无数字 / -1 错位。"""
    da, db = _digits(a), _digits(b)
    if not da or not db:
        return 0.0
    return 1.0 if (da & db) else -1.0


def _lcs_len(a: str, b: str) -> int:
    """最长公共连续子串长度（"一把椅子腿" vs "把一根椅子腿放到房间三" → 3："椅子腿"）。"""
    ba, bb = str(a or ""), str(b or "")
    if not ba or not bb:
        return 0
    prev = [0] * (len(bb) + 1)
    best = 0
    for i in range(1, len(ba) + 1):
        cur = [0] * (len(bb) + 1)
        for j in range(1, len(bb) + 1):
            if ba[i - 1] == bb[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def score_candidate(hint: str, name: str, env_id: str = "",
                    parts_keys: list = None, in_scene: bool = True,
                    is_global_fallback: bool = False) -> float:
    """单个候选的消解得分（0~1，纯函数）。

    Args:
        hint: 自然语言指称（"那把刀"/"我拆下椅子腿"/"窗边的东西"）。
        name: 候选实体显示名（"一把椅子"——量词无需处理，见模块注释）。
        parts_keys: state.parts 的键（数据化部件，如 ["legs"] 或中文键）。
        in_scene: 候选是否在当前场景（L1 在场召回）。
        is_global_fallback: 是否来自全世界兜底召回（衰减，优先在场物）。
    """
    hint = str(hint or "")
    if not hint:
        return 0.0
    # L0 精确 id 命中
    if env_id and env_id in hint:
        return 1.0

    base = _W_BIGRAM * bigram_f1(name, hint) + _W_UNI * _name_coverage(name, hint)
    # 核心词窗口：名字的（连续/跳跃）2-gram 窗口作为子串出现在 hint 中 = 强证据——
    # 实体名常为"修饰语+核心名词"（青铜+烛台/生锈的+铁门/一捆+干柴），整名 F1 会被
    # 修饰语稀释；窗口命中等价于"用户指的就是这个核心名词"，且对量词/换缀免疫。
    if any(w and w in hint for w in _windows(name)):
        base = max(base, 0.55)
    # 特指加成：与 hint 的最长公共连续子串 ≥3 字 → 用户在特指该物/其部件
    # （"一根椅子腿"与"一把椅子腿"公共子串"椅子腿"3 字 > 整椅的"椅子"2 字——
    #   部件比宿主更特指，纯 bigram 区分不出这个差异）
    if _lcs_len(name, hint) >= 3:
        base = max(base, 0.6)
    # 部件数据：parts 键本身出现在 hint 里 = 用户在指这个物件的部件 → 强证据指向宿主
    for pk in parts_keys or []:
        if pk and pk in hint:
            base = max(base, 0.5)
            break
        if pk and bigram_f1(pk, hint) >= 0.5:
            s_local = min(1.0, base + 0.2)
            base = max(base, s_local)
            break
    # ⚠️ 此处禁用数字对齐（教训）：实体名里的数字多为量词（"一**把**椅子"的"一"），
    # 与 hint 里的数字（"两**条**腿"）错位会造成伪惩罚——0.55 分直接被打成 0.22。
    # 数字对齐只用于场景消解（resolve_scene：房间三↔room_3，数字是名字本体）。
    if not in_scene or is_global_fallback:
        base *= _GLOBAL_DAMP
    return round(max(0.0, min(1.0, base)), 4)


def rank_candidates(hint: str, candidates: list) -> list:
    """对候选列表打分排序（零分候选**保留**——LLM 兜底需要完整选择清单）。"""
    scored = []
    for c in candidates or []:
        name = str(c.get("name_for_match") or c.get("name") or "")
        s = score_candidate(hint, name, str(c.get("env_id", "")),
                            c.get("parts_keys"), c.get("in_scene", True),
                            c.get("global_fallback", False))
        scored.append((c, s))
    scored.sort(key=lambda x: -x[1])
    return scored


def resolve(hint: str, candidates: list, prefer_accessible: bool = False) -> dict:
    """级联判定（L2+L3）：打分排序 → 阈值+优势判定 → 本地消解或交 LLM 选择题。

    Args:
        prefer_accessible: 并列消歧的可及性二段过滤（操作类动作传 True）——
            "把**另一根**椅子腿放到房间一"里两根椅子腿打分并列，但操作对象必在
            操作者可及范围（in_scene），过滤后唯一即消解。这是通用世界规则
            （你只能操作你够得着的），不是为某个用例打的补丁。
    Returns:
        {"resolved": bool, "env_id": str, "name": str, "score": float,
         "need_llm": bool, "candidates": [(候选, 分数)]（排序后截断）}
    """
    ranked = rank_candidates(hint, candidates)
    topk = ranked[:_TOPK_FOR_LLM]
    if not ranked:
        return {"resolved": False, "env_id": "", "name": hint, "score": 0.0,
                "need_llm": True, "candidates": []}
    top_c, top_s = ranked[0]
    second_s = ranked[1][1] if len(ranked) > 1 else 0.0
    if top_s >= _ACCEPT and (top_s - second_s) >= _MARGIN:
        return {"resolved": True, "env_id": str(top_c.get("env_id", "")),
                "name": str(top_c.get("name", "")), "score": top_s,
                "need_llm": False, "candidates": topk}
    # 并列消歧（可及性二段）：
    # ① 特指优先：LCS 最长者胜出——"一根椅子腿"特指部件（LCS=3）而非整椅（LCS=2），
    #    这个差异纯 bigram 打分体现不出来，必须在消歧阶段用特指度裁决；
    # ② 特指集合内同名等价（"任意一根"）→ 取首；不同名仍并列 → 交 LLM。
    if prefer_accessible:
        accessible = [(c, s) for c, s in topk if c.get("in_scene") and s > 0]
        if accessible:
            best_lcs = max(_lcs_len(str(c.get("name", "")), hint) for c, _ in accessible)
            pool = [(c, s) for c, s in accessible
                    if _lcs_len(str(c.get("name", "")), hint) == best_lcs] if best_lcs >= 3 else accessible
            names = {str(c.get("name", "")) for c, _ in pool}
            if len(pool) == 1 or len(names) == 1:
                c, s = pool[0]
                return {"resolved": True, "env_id": str(c.get("env_id", "")),
                        "name": str(c.get("name", "")), "score": s,
                        "need_llm": False, "candidates": topk}
    return {"resolved": False, "env_id": "", "name": hint, "score": top_s,
            "need_llm": True, "candidates": topk}


def resolve_scene(hint: str, scene_candidates: list) -> str:
    """目的地场景消解（替代"房间三→room_3"映射表）：候选来自世界数据（type=room 实体）。

    Args:
        hint: 目的地指称（"房间三"/"room_2"/"三"，或整句"把X搬到room_3"）。
        scene_candidates: [{"env_id","name"}]——世界全部 room 实体（数据驱动）。
    Returns:
        scene id；消解不出返回 ""（调用方按"路不通/没有可辨认目的地"处理）。
    """
    hint = str(hint or "").strip()
    if not hint or not scene_candidates:
        return hint if hint in {c.get("env_id") for c in scene_candidates} else ""
    # 精确 id / 句中显式写出目的地 id（"把chair_leg_1搬到room_3"里的 room_3）
    for c in scene_candidates:
        if hint == c.get("env_id"):
            return hint
        if c.get("env_id") and str(c["env_id"]) in hint:
            return str(c["env_id"])
    # 短数字/中文数字指称（"三"）：直接按数字对齐（小候选集内唯一即可）
    hd = _digits(hint)
    if hd and len(hint) <= 2:
        by_digit = [c for c in scene_candidates if _digits(str(c.get("name", ""))) or
                    _digits(str(c.get("env_id", "")))]
        matches = [c for c in by_digit if _digits(str(c.get("name", ""))) == hd
                   or _digits(str(c.get("env_id", ""))) == hd]
        if len(matches) == 1:
            return str(matches[0]["env_id"])
    best_id, best_s = "", 0.0
    for c in scene_candidates:
        cid, cname = str(c.get("env_id", "")), str(c.get("name", ""))
        s = 0.7 * bigram_f1(cname, hint) + 0.3 * _name_coverage(cname, hint)
        if hint in cname:
            s = max(s, 0.75)
        # 名字核心词窗口（"房间"）出现在整句 hint 里 → 相关，但需数字/覆盖确认
        if any(w and w in hint for w in _windows(cname)):
            s = max(s, 0.5)
            # 上下文数字对齐：取 hint 中【窗口之后】的数字（"放到房间三"的三），
            # 排除窗口之前正文里的量词数字（"一**根**椅子腿"的一）——否则
            # "一根…房间三"的数字集合 {1,3} 与 room_1/room_3 同时对齐（并列毒化）。
            tail_digits = set()
            for w in _windows(cname):
                if w and w in hint:
                    tail_digits = _digits(hint[hint.find(w) + len(w): hint.find(w) + len(w) + 3])
                    if tail_digits:
                        break
            if tail_digits:
                nd = _digits(cname) | _digits(cid)
                if nd & tail_digits:
                    s = min(1.0, s + 0.3)
                else:
                    s *= 0.5
        if s > best_s:
            best_id, best_s = cid, s
    return best_id if best_s >= 0.5 else ""
