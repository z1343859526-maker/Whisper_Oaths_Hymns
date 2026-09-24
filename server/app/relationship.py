"""关系系统：把"这轮对话让 NPC 对我更亲近还是更反感"量化成 trust/affection 增减。

设计要点：
- 规则版：先用正/负面情绪词检测玩家这轮的态度，零 LLM 成本、零延迟；
- 先跑通「对话 → 加减分 → 下次措辞变化」的闭环；真正"结合整轮语义判断增减"
  要再调一次 LLM，是 P5 reflection 的升级项，本轮不引入。
"""
from . import db

# 玩家话里的正面词 → 加分；负面词 → 减分
_POSITIVE_WORDS = ("谢谢", "感谢", "感激", "佩服", "信任", "朋友", "盟友", "救", "帮")
_NEGATIVE_WORDS = ("滚", "蠢", "骗子", "撒谎", "威胁", "恨", "讨厌", "闭嘴", "背叛", "杀")

# 每个词的幅度（信任和好感同涨同跌，亲近感是整体的）
_DELTA = 5


def adjust_relationship(npc_id, player_text, session_id=""):
    """根据玩家这句话里的情绪词，调整 NPC 对玩家的关系数值（本会话的行）。

    session_id：关系隔离维度（M1.1）——UPDATE 只动该会话复制出的关系行，
    A 会话刷的好感不会串进 B 会话。不传则落空串（遗留行为，新代码禁用）。

    Returns:
        (delta, updated)：delta=本轮净增减值；updated=是否真的写回了数据库。
    """
    if not npc_id:
        return 0, False  # 环境旁白（空 npc_id）不涉及关系

    delta = 0
    for w in _POSITIVE_WORDS:
        if w in player_text:
            delta += _DELTA
    for w in _NEGATIVE_WORDS:
        if w in player_text:
            delta -= _DELTA

    if delta == 0:
        return 0, False  # 没命中任何情绪词，不动数据库

    affected = db.update_relationship(npc_id, "player", session_id,
                                      delta_trust=delta, delta_affection=delta)
    return delta, affected > 0


def willing_to_dialogue(npc_id, other_id, session_id="", busy=False, stress=0.0, plan_step=False):
    """规则判定：NPC（被邀请者）是否愿意与 other_id 对话（零 LLM，决策成本 0）。

    对话系统 v0.4：接收方（被邀请者）在仲裁阶段调用此函数决定"同不同意这场对话"。
    依据（全部来自既有关系参数 + 调用方传入的现状）：
    - trust / affection / fear（0~100）：信任与人好加分，恐惧减分；
    - relation_type 定性：亲近/盟友再加分，敌对/可疑再减分（宽泛匹配，不认识也算中性）；
    - busy：被邀请者此刻有更急的事（正移动/攻击/逃离）则更不愿被打断；
    - stress（0~1）：压力越大越戒备、越不愿闲聊；
    - plan_step（T4/T5）：被邀请者当前意图是【既定计划的推进项】→ 有更要紧的事在办，
      显著降低闲聊意愿（配合"连续交谈拒绝"），但弱于 busy 的一票否决。

    Args:
        npc_id: 被邀请者。
        other_id: 发起对话邀请的一方（对玩家可能是 "player"）。
        session_id: 关系隔离维度（M1.1），缺省落空串即 seed 基线。
        busy: 被邀请者此刻是否在忙。
        stress: 被邀请者当前压力度（0~1，调用方从心智状态换算）。
        plan_step: 被邀请者意图是否为既定计划的推进项（LLM 自报）——默认 False 兼容旧调用。
    Returns:
        bool：是否愿意对话。
    """
    rel = db.get_relationship(npc_id, other_id, session_id)
    if rel is not None:
        trust, fear, affection, rel_type, _notes = rel
    else:
        trust = fear = affection = 50  # 无记录：默认中性
        rel_type = ""

    # 正在做要紧事（移动/攻击/交互等本回合的实质任务）→ 基本不搭话。
    # 这更贴近"原本要做的事"：被拒后它会去忙原本的事，配合 agent.reject_and_advance 递推。
    if busy:
        return False

    # 以中性(三者皆 50)为基准 0：越信任/越亲近越愿意，越恐惧越戒备；
    # 阈值设 0，即"中性=边界愿意"，避免"不认识就默认拒绝"过度保守。
    will = 0.6 * (trust - 50) + 0.4 * (affection - 50) - 0.5 * (fear - 50)
    rel_type = rel_type or ""
    if rel_type in ("ally", "friend", "trusted"):
        will += 15
    elif rel_type in ("enemy", "rival", "suspicious", "hostile"):
        will -= 20
    if stress:
        will -= 25 * stress
    if plan_step:
        will -= 35  # 正打算推进计划（有更重要的事）→ 明显更不愿被闲聊打断
    return will >= 0
