"""上下文组装层：把"这是哪个 NPC、他是什么人设"拼进 prompt，让模型按角色说话、不 OOC。

职责：
- 输入：npc_id + 玩家这句话；
- 查角色卡（character_card）→ 组装 system 人设（含 forbidden 禁区）；
- 返回 OpenAI messages 列表，交给 llm 调用。

设计约束：本层只做"拼上下文"，不请求、不碰模型 —— 面向接口、职责单一；
llm.py 负责"把 messages 发给模型"，两者解耦，便于后续 P4-B/C/D 继续往 system 里加记忆/关系/知识。
"""
import json
import logging

from . import intent as intent_mod
from . import spatial as spatial_mod
from . import environment as env_mod
from . import mental as mental_mod
from . import world_pack
from .db import (get_character_card, get_relationship, get_knowledge_candidates,
                 get_world_name, get_player_scene, get_game_state_map)
from .memory import recall_memories
from .embedder import NGramEmbedder
from .vector_store import NumpyVectorStore

# 模块级日志器：narrate_scene 等处失败降级时用（此前缺失导致 except 内 NameError → 500）
logger = logging.getLogger(__name__)


# 向量维度 + 模块级 embedder 单例：投影矩阵固定 seed，复用避免每轮重复生成
# ⚠️ 必须与 embedder.NGramEmbedder 默认 dim、库里存量 embedding 的维度一致
#（backfill 走 vectorize_knowledge → NGramEmbedder() 默认值）——改这里必须重跑 backfill。
_EMBED_DIM = 1024
_embedder = NGramEmbedder(dim=_EMBED_DIM)


# 找不到角色卡时的兜底 system：npcs 数据缺失，或 npc_id 为空（环境直接行动 → 世界旁白）
FALLBACK_SYSTEM = (  # 文档化默认（运行时走 world_pack.storyteller_system，模组可覆盖）
    "你是一个文字冒险游戏的旁白/世界叙事者。用简体中文、第二人称客观描述玩家"
    "所处这个世界对玩家行动的反应，不要替任何角色说话，不要打破第四面墙。"
)

# 世界 → 显示名映射（不同世界不同设定）。world_id 缺失时回退成 "黄金乡"，
# 保证旧调用（未传 world_id）不因语义变化而读不到世界名。
# M1.5 泛化：世界名从 world 表读（004 迁移新增），代码不再硬编码两处 _WORLD_NAMES——
# 加世界观 = 往 world 表插一行，不改代码。这里保留一个"未知世界"兜底词，
# 仅当 DB 无该世界记录时用，保证旧调用/数据缺失不崩。
_DEFAULT_WORLD = "golden"
_DEFAULT_WORLD_NAME = "黄金乡谋杀案"


def _world_name(world_id: str) -> str:
    """把 world_id 翻译成世界显示名。

    M1.5 泛化：优先从 DB 读（world 表，像 list_worlds 一样数据驱动）；
    读不到（世界未登记 / 旧调用未传）才回退默认名。加世界观 = 插一行数据，
    不再改代码。
    """
    name = get_world_name(world_id)
    return name or _DEFAULT_WORLD_NAME


def _parse_forbidden(forbidden) -> list[str]:
    """把 forbidden 字段解析成一串"禁区"行。

    forbidden 有两种格式：
    - JSON 数组：["王族丑闻","秘道的去向"] → 直接当作禁区列表；
    - JSON 对象：{"spoiler":[...],"knowledge":[...],"behavior":[...]}
      → 按角色卡 P4 规范，分别取出各子项内容（真相 / 不该知 / 行为边界）。
    这样即使角色卡用对象格式，禁区也能正确注入，不会只剩字段名。
    """
    try:
        data = json.loads(forbidden)
    except (ValueError, TypeError):
        return [str(forbidden)] if forbidden else []

    if isinstance(data, list):
        return [str(x) for x in data]
    if isinstance(data, dict):
        lines = []
        spoiler = data.get("spoiler", [])
        if spoiler:
            lines.append("绝不能向玩家透露或承认的真相：" + "、".join(str(x) for x in spoiler))
        knowledge = data.get("knowledge", [])
        if knowledge:
            lines.append("这还轮不到你掌握/不该说破的：" + "、".join(str(x) for x in knowledge))
        behavior = data.get("behavior", [])
        if behavior:
            lines.append("行为边界（即使被激怒也绝不越线）：" + "；".join(str(x) for x in behavior))
        return lines
    return [str(data)]


def _npc_system(card, world_id: str = _DEFAULT_WORLD) -> str:
    """把角色卡字段拼成一段系统人设提示。

    card 是 get_character_card 的一行元组：
    (npc_id, name, title, personality, background, motivation, forbidden, knowledge_scope)
    world_id：世界维度——决定角色挂在哪个世界的设定下（黄金乡/测试世界），
    避免不同世界的角色被套进同一套世界观。
    """
    name, title = card[1], card[2]
    personality = card[3]
    background = card[4]
    motivation = card[5]
    forbidden = card[6]

    lines = [
        f"你是《{_world_name(world_id)}》里的 NPC：「{name}」" + (f"（{title}）" if title else "") + "。",
        "你必须始终以这个角色身份说话，用简体中文，口吻、措辞与情绪都符合此人的身份与性格，绝不跳出角色，绝不提及或承认你是 AI。",
    ]
    if personality:
        lines.append(f"性格：{personality}。")
    if background:
        lines.append(f"背景：{background}。")
    if motivation:
        lines.append(f"动机：{motivation}。")
    for f in _parse_forbidden(forbidden):
        lines.append(f)
    return "\n".join(lines)

def _memory_block(npc_id: str, limit: int = 8, min_importance: int = 4, session_id: str = "seed") -> str:
    """召回 NPC 的高重要度记忆，拼成一段注入 system。

    session_id：记忆隔离维度（M1.1）——召回 = 先验('seed') + 本会话两段，
    见 memory.recall_memories。缺省 'seed' 供旁白/无会话调用安全降级。

    为什么"召回 + 取 top N"而不是"全塞"：
    - 记忆越滚越多，全塞会爆 token（提示词预算），还让无关记忆干扰模型；
    - 所以按 importance 降序（recall_memories 里已 ORDER BY importance DESC），
      只取前 limit 条，这是 RAG「检索相关而非全量」思想的雏形。

    为什么 list(...)[:limit] 切片：现在记忆才几条，全量取回再截断最简单；
    等记忆多了，应把 LIMIT 下推到 SQL（给 recall_memories 加 limit 参数），
    避免全量拉取 —— 「先跑通、后优化」。
    """
    memories = list(recall_memories(npc_id, min_importance, session_id))[:limit]
    if not memories:
        return ""  # 没有符合阈值的记忆，不追加段落，保持原样

    lines = ["以下是你的记忆（你亲身经历过、必须当作事实记住的事）："]
    for m in memories:
        lines.append(f"- {m['text']}")  # recall_memories 已做 summary 优先、无摘要取原文
    return "\n".join(lines)

def _relation_block(npc_id: str, other_id: str = "player", session_id: str = "seed") -> str:
    """把 NPC 对玩家的关系（trust/affection/fear）映射成态度词，注入 system。

    session_id：关系隔离维度（M1.1）——读该会话复制出的关系行，
    本轮刷的好感只影响本轮措辞。缺省 'seed' 即初始关系（旁白/演示场景）。

    为什么映射成自然语言而不是直接给数字：
    模型看不懂"trust=5"意味着什么语气，但看得懂"你对他冷淡、提防"；
    把结构化数值翻译成态度，是让状态真正影响措辞的关键一步。
    """
    rel = get_relationship(npc_id, other_id, session_id)
    if not rel:
        return ""  # 没有关系记录，不追加段落

    trust, fear, affection, relation_type, notes = rel

    # 好感 + 信任 综合成"亲疏分"（0~200），fear 单独决定"忌惮"
    closeness = trust + affection
    if closeness >= 120:
        attitude = "你对他颇为信任、很有好感，语气亲近，愿意多说心里话"
    elif closeness >= 60:
        attitude = "你对他观感尚可，客气但有所保留"
    elif closeness >= 30:
        attitude = "你对他印象一般，礼貌而疏远"
    else:
        attitude = "你对他冷淡、心存提防，不愿多说"

    if fear >= 40:
        attitude += "；你对他有所忌惮，说话会小心翼翼"

    lines = [f"你对玩家当前的态度：{attitude}。"]
    if notes:
        lines.append(f"（你最近对他的印象：{notes}）")
    return "\n".join(lines)


def _parse_access(card) -> list[str]:
    """从角色卡 knowledge_scope 里解析 can_access 列表；缺省只给 public。"""
    try:
        scope = json.loads(card[7]) if card[7] else {}
    except (ValueError, TypeError):
        return ["public"]
    if isinstance(scope, dict):
        access = scope.get("can_access")
        if isinstance(access, list) and access:
            return [str(a) for a in access]
    return ["public"]


def _knowledge_block(npc_id: str, player_text: str, card, world_id: str = _DEFAULT_WORLD, top_k: int = 3) -> str:
    """RAG：把玩家这句话向量化，从「该 NPC 有权知道」的当前世界世界观里检索最相关的注入 system。

    world_id：世界隔离维度——get_knowledge_candidates 按 world_id 过滤，
    测试角色只会检索到测试世界的知识，绝不会串到黄金乡（不同世界不同 RAG 库）。
    先按 access_level 过滤再检索：NPC 不该知道的 secret 根本不在候选里，模型没有这条知识可泄露；
    检索用余弦相似度，把玩家问题命中到的「NPC 已知世界观」挑出来，让回答有据可依。
    """
    allowed = _parse_access(card)
    candidates = get_knowledge_candidates(npc_id, allowed, world_id)
    if not candidates:
        return ""

    store = NumpyVectorStore(dim=_EMBED_DIM)
    for c in candidates:
        try:
            vec = json.loads(c[6])  # c[6] 是 embedding（JSON 字符串 -> list）
        except (ValueError, TypeError):
            continue  # 个别条目没向量化就跳过，不影响整段
        store.add(c[0], vec, {"title": c[3], "content": c[4], "access_level": c[5]})

    q = _embedder.embed(player_text)
    hits = store.search(q, top_k)
    if not hits:
        return ""

    lines = ["以下是你已知晓、与当前话题相关的世界信息（回答时可自然引用，别当成刚被告知的新情报）："]
    for h in hits:
        m = h["meta"]
        lines.append(f"- {m['title']}：{m['content']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 心智拼接（v0.3）：mental_ctx 中间态 → L1~L7 状态词块
# 铁律：本函数输出的任何文本不得含数字（mental_state 里的数值只进 code）
# ---------------------------------------------------------------------------
def _safe_card_name(card) -> tuple:
    """card 是 get_character_card 的 8 列元组；缺卡时用 (npc_id 兜底)。"""
    if card:
        return str(card[1]), str(card[2] or "")
    return "这个人", ""


def compose_mental_system(mental_ctx: dict, card, world_id: str = _DEFAULT_WORLD,
                          player_text: str = "", include_knowledge: bool = False,
                          session_id: str = "seed") -> str:
    """把 mind_engine 的中间态拼成完整 system prompt（L1~L7）。

    L1 身份锚（kernel.identity + 长期动机 + 价值树人话）
    L2 底线（规则文本，不含阈值数字）
    L3 所见与信念（noticed + 信念四态词）
    L4 情绪状态（主情绪词 + 表达抑制词——"外冷内热"的出处）
    L5 目标与计划状态（plan_status_words + 底线压力暗示）
    L6 思考习惯（思维链脚手架）+ 记忆 + 关系态度词 + RAG 知识
    L7 表达风格（verbal_style + speech_style 手册 + 示范台词 few-shot）
    """
    mm = mental_ctx["mm"]
    state = mental_ctx.get("state") or {}
    kernel = mm.get("kernel", {}) if isinstance(mm.get("kernel"), dict) else {}

    name, title = _safe_card_name(card)
    world_name = get_world_name(world_id) or "这个世界"

    # L1 身份锚
    identity = str(kernel.get("identity", "")).strip()
    lines = [f"你是《{world_name}》里的角色：「{name}」" + (f"（{title}）" if title else "") + "。"]
    if identity:
        lines.append(f"你是谁：{identity}")
    ltm = str(kernel.get("long_term_motivation", "")).strip().rstrip("。")
    if ltm:
        lines.append(f"你的长期追求：{ltm}。")
    vw = mental_mod.value_words(kernel.get("value_tree") or {})
    if vw:
        lines.append(vw + "。")
    lines.append(world_pack.prompt(world_id, "role_lock"))

    # L2 底线（规则文本；阈值/突破条件数值绝不出现）
    rules = [str(r.get("rule", "")) for r in mental_mod._hard_limit_rules(kernel) if r.get("rule")]
    if rules:
        lines.append("你的行为底线（极端处境之前绝不越过）：" + "；".join(rules) + "。")

    # L2.5 心里的秘密（v4§13 双链）：话题 + 当前口径 + 识破姿态
    # 底线封口/姿态词均为状态词；触发阈值与识破进度数字绝不出现
    for s in (mental_ctx.get("secrets_ctx") or []):
        lines.append(f"你藏着的事：{s['topic']}。你当前的口径：{s['word']}——{s['gating']}。")
        if s.get("capped"):
            lines.append("（这件事同时受你的底线约束——底线优先，口径以底线为准。）")
        if s.get("stance"):
            lines.append(f"对方似乎有所察觉。你此刻的策略：{s['stance']}。")

    # L3 所见与信念
    noticed = [str(n) for n in (state.get("noticed") or [])][-3:]
    for n in noticed:
        lines.append(f"你注意到：{n}。")
    belief_lines = []
    for b in (state.get("beliefs") or [])[:4]:
        topic = str(b.get("topic", "")).strip()
        if topic:
            belief_lines.append(f"对『{topic}』你{mental_mod.belief_words(str(b.get('state', 'doubt')))}")
    if belief_lines:
        lines.append("你的判断：" + "；".join(belief_lines) + "。")
    wm = [str(w) for w in (state.get("working_memory") or [])][-3:]
    for w in wm:
        lines.append(f"（{w}）")

    # L3.5 空间感知快照（环境管线对接）：NPC 所在场景的真实空间事实——
    # "你所在 + 这里有（含被拿走的不显示）+ 你注意到的近期痕迹"。
    # 修复环境系统文档 §8.3 已知空白"NPC 对话未注入环境感知"。
    try:
        from . import spatial as spatial_mod2
        from .db import get_npc_pos
        scene = get_npc_pos(session_id, str(card[0]) if card else "")
        if scene:
            snap = spatial_mod2.build_perception_snapshot(
                scene, session_id=session_id, world_id=world_id,
                observer=str(card[0]) if card else "player")
            snap = str(snap or "").strip()
            if snap:
                lines.append(snap)
    except Exception:  # noqa: BLE001  无空间骨架的世界（黄金乡）安静跳过
        pass

    # L3.6 随身物品（NPC 背包：环境实体 where.mode=held 且 holder=该 NPC——
    # 与玩家背包同一数据源 get_held_items，仅 holder 不同）
    try:
        from .spatial import get_held_items
        held = get_held_items(world_id, holder=str(card[0]) if card else "")
        if held:
            names = "、".join(str(h.get("name") or h.get("env_id")) for h in held[:6])
            lines.append(f"你身上带着：{names}。")
    except Exception:  # noqa: BLE001  无环境数据的世界安静跳过
        pass

    # L4 情绪状态 + 表达抑制（内外分离：感受是感受，表情是表情）
    # 平直情绪（强度≈0）不编造感受——没事就不说"你可能有点感觉"；
    # 抑制词是性格特质，与当下情绪无关，无条件输出
    emo = state.get("emotion") or {}
    intensity = state.get("emotion_intensity")
    if emo and intensity is not None and float(intensity) > 0.05:
        fam = mental_ctx.get("emotion_family") or mental_mod.classify_family(emo)
        ew = mental_mod.emotion_words(
            {"valence": emo.get("valence", 0), "arousal": emo.get("arousal", 0),
             "dominance": emo.get("dominance", 0.5), "intensity": intensity},
            family=fam,
            words=world_pack.emotion_words(world_id, fam))  # 模组词表（接入点③）
        lines.append(ew["phrase"] + "。")
    expr = mm.get("expression", {}) if isinstance(mm.get("expression"), dict) else {}
    lines.append(mental_mod.suppress_words(expr.get("composure", 50)) + "。")

    # L5 目标与计划状态 + 底线压力暗示
    plan_phrase = mental_mod.plan_status_words(mental_ctx.get("plan"))
    if plan_phrase:
        lines.append(plan_phrase + "。")
    pressure = mental_ctx.get("pressure") or {}
    for r in mental_mod._hard_limit_rules(kernel):
        pname = str(r.get("rule", ""))
        hint = mental_mod.pressure_phrase(float(pressure.get(pname, 0) or 0),
                                          float(r.get("threshold", 100) or 100))
        if hint:
            lines.append(hint + "。")
            break  # 只提示最重的一条，避免堆叠

    # L6 思考习惯（思维链脚手架：给 LLM 的推理顺序，影响其组织回答的方式）
    from . import mind_engine  # 局部导入：避免 mind_engine↔context_builder 循环依赖
    chain = mind_engine.get_chain(mm)
    if chain:
        lines.append(world_pack.prompt(world_id, "chain_scaffold").format(chain=" → ".join(chain)))

    # L6 记忆 / 关系态度词 / RAG 知识（沿用既有实现，状态词口径一致）
    mem_block = _memory_block(str(card[0]) if card else "", session_id=session_id) if card else ""
    rel = mental_ctx.get("relationship")
    if rel:
        trust, fear, affection, rel_type, notes = rel
        closeness = trust + affection
        if closeness >= 120:
            attitude = "你对他颇为信任、很有好感，语气亲近，愿意多说心里话"
        elif closeness >= 60:
            attitude = "你对他观感尚可，客气但有所保留"
        elif closeness >= 30:
            attitude = "你对他印象一般，礼貌而疏远"
        else:
            attitude = "你对他冷淡、心存提防，不愿多说"
        if fear >= 40:
            attitude += "；你对他有所忌惮，说话会小心翼翼"
        lines.append(f"你对眼前这个人的态度：{attitude}。")

    if include_knowledge and card and player_text:
        know_block = _knowledge_block(str(card[0]), player_text, card, world_id)
        if know_block:
            lines.append(know_block)

    # L7 表达风格
    expr = mm.get("expression", {}) if isinstance(mm.get("expression"), dict) else {}
    verbal_style = str(expr.get("verbal_style", "")).strip()
    if verbal_style:
        lines.append(f"你的说话方式：{verbal_style}")
    if card:
        card_speech, card_examples = _speech_assets(card)
        if card_speech:
            lines.append(f"你的说话手册：{card_speech}")
        if card_examples:
            lines.append("你的典型台词（模仿这种口吻）：")
            for ex in card_examples[:3]:
                lines.append(f"- {ex}")

    return "\n".join(lines)


def _speech_assets(card) -> tuple:
    """从角色卡读 speech_style（列8）与 example_dialogue（列9）——此前零消费的两列，
    T5 起进入 L7 块。get_character_card 只 SELECT 8 列，这里按需单查，避免扩列号耦合。"""
    from .db import execute_query
    try:
        rows = execute_query(
            "SELECT speech_style, example_dialogue FROM character_card "
            "WHERE npc_id=%s AND is_active=1", (card[0],))
    except Exception:  # noqa: BLE001
        return "", []
    if not rows:
        return "", []
    speech = rows[0][0] or ""
    raw = rows[0][1]
    examples = []
    if raw:
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(data, list):
                examples = [str(x) for x in data]
        except (ValueError, TypeError):
            examples = []
    return str(speech), examples


# 供 compose 使用的会话维度由调用方显式传入（session_id 参数），无需模块级状态。


def build_messages(npc_id: str, player_text: str, session_id: str = "seed", world_id: str = _DEFAULT_WORLD,
                   mental_ctx: dict or None = None) -> list[dict]:
    """组装 OpenAI messages：system(角色人设+禁区) + user(玩家输入)。

    session_id：会话隔离维度（M1.1）——决定召回哪段记忆、读哪份关系。
    world_id：世界隔离维度（不同世界不同 RAG 库）——决定角色挂在哪个世界设定下、
    检索哪个世界的知识。缺省 'golden' 供旁白/旧调用安全降级。
    /chat 主链路传当前会话与当前世界；缺省 'seed'/'golden' 供旁白/无会话调用安全降级。
    npc_id 为空 → 视为环境直接行动，用旁白兜底 system。
    mental_ctx：心智引擎（mind_engine）产出的中间态——非空且该角色有 mental_model
    时，走 L1~L7 心智拼接（状态词，无数值）；None → 旧四段拼接（黄金乡等未迁移
    角色的兜底路径，两条路线互不干扰）。
    """
    if not npc_id:
        # 环境/旁白路径：走环境管线（意图识别 → 执行 → 感知快照注入）
        return _environment_messages(session_id, player_text, world_id)

    card = get_character_card(npc_id)

    # 心智路径（v0.3）：L1~L7 全部由中间态状态词拼成；任何异常都降级回旧路径
    if mental_ctx is not None:
        try:
            system = compose_mental_system(mental_ctx, card, world_id,
                                           player_text=player_text, include_knowledge=True,
                                           session_id=session_id)
            return [
                {"role": "system", "content": system},
                {"role": "user", "content": player_text},
            ]
        except Exception:  # noqa: BLE001  拼接失败不能挡住对话主链路
            pass

    system = _npc_system(card, world_id) if card else world_pack.storyteller_system(world_id)

    # P4-B：在「身份（角色卡）」之后，追加「经历（记忆）」段落。
    # 有记忆才追加，没有就保持原样，不破坏 P4-A 已验收的「不 OOC」。
    mem_block = _memory_block(npc_id, session_id=session_id)
    if mem_block:
        system = system + "\n\n" + mem_block

    # P4-C：在「经历（记忆）」之后，追加「态度（关系）」段落。增量不推翻。
    rel_block = _relation_block(npc_id, session_id=session_id)
    if rel_block:
        system = system + "\n\n" + rel_block

    # P4-D：在「态度（关系）」之后，追加「知识（世界检索）」段落。
    if card:
        know_block = _knowledge_block(npc_id, player_text, card, world_id)
        if know_block:
            system = system + "\n\n" + know_block

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": player_text},
    ]


# ---------------------------------------------------------------------------
# 环境管线：玩家与环境之间的交互（npc_id 为空 / 环境直接行动）
# ---------------------------------------------------------------------------
def _entity_names_list(world_id: str) -> str:
    """拼一份"本世界可引用场景/实体/角色清单"，供意图识别 LLM 填 target.id 用。

    来源：environment_card（房间+物品 id/名）+ character_card（NPC）。有空间实体
    （environment_entity）的补上锚点标签；数据来自 DB，无需硬编码，换世界通用。
    """
    from . import db as _db
    names = []
    # 环境卡（房间 + 关键物）：env_id(名称)
    for env_id, _kind, name, *_ in _db.get_environment_cards(world_id):
        names.append(f"{env_id}({name})")
    # 空间实体附加锚点（供"去床边"这类参照物）——只列锚点，避免清单过长
    try:
        for e in _db.get_environment_entities(None, world_id):
            if e[11]:  # is_anchor
                names.append(f"{e[0]}({e[2]}，锚点={e[11]})")
    except Exception:
        pass
    # NPC
    for npc in _db.get_all_npc_ids(world_id):
        names.append(npc)
    return "、".join(names) if names else "（无清单）"


def _environment_messages(session_id: str, player_text: str, world_id: str) -> list[dict]:
    """环境管线：把玩家对环境的输入（行动/查询/移动）落地，并注入感知快照给旁白 LLM。

    流程：
      ① 读玩家场景 + 当前 tick（game_state）
      ② 意图识别 intent.classify（规则优先 + LLM 兜底）
      ③ 若为环境行为（self/spatial+mutating）→ 执行器写回（复用 patch/add_world_trace）
      ④ 拼 world prompt = [世界状态感知快照] + [玩家动作结果]，注入旁白 system
    Returns:
        OpenAI messages。旁白 LLM 据"世界真实状态 + 玩家动作"生成玩家能看到的文字。
    """
    scene = get_player_scene(session_id) or ""
    gs = get_game_state_map(session_id)
    tick = gs.get("current_tick", 0)

    # ① 意图识别
    entity_names = _entity_names_list(world_id)
    intent = intent_mod.classify(player_text, scene_id=scene, world_id=world_id, entity_names=entity_names)

    # ⭐ 默认落点补全（你新增）：玩家只说"放房间1"没说放哪 → 程序筛锚点 + LLM 挑一个
    #   合理锚点填入 intent.spatial.ref_anchor，供执行器算坐标（LLM 只挑锚点、程序算坐标）。
    if intent.domain in ("self", "spatial") and intent.side_effect == "mutating":
        intent = intent_mod.fill_default_placement(intent, world_id)

    # ② 环境行为 → 执行（写世界状态）
    result = {"outcome": "ignored", "message": "", "changed": False, "scene": scene}
    if intent.domain in ("self", "spatial") and intent.side_effect == "mutating":
        result = env_mod.execute_player_action(intent, session_id, tick, world_id)

    # ③ 感知快照（执行后的世界状态）：目标1——环境实时信息必须进 LLM 请求
    after_scene = result["scene"] or scene
    snap = spatial_mod.build_perception_snapshot(after_scene, session_id=session_id, world_id=world_id)

    # ④ 拼旁白 system：世界真实状态 + 玩家动作结果
    sys_lines = [world_pack.storyteller_system(world_id)]
    if snap:
        sys_lines.append("以下是你当前所处世界的真实状态（回答玩家时以此为准，不要凭空编）：\n" + snap)
    if result.get("message"):
        sys_lines.append(f"玩家刚刚的动作结果：{result['message']}")

    # 让旁白能区分"玩家在问环境"还是"玩家在行动"——把意图也告诉它
    sys_lines.append(
        f"玩家这句的意图：{intent.domain}({'会改变世界' if intent.side_effect == 'mutating' else '只读'})，"
        f"动作：{intent.verb or '（无）'}。请据此自然叙述。"

    )
    system = "\n\n".join(sys_lines)

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": player_text},
    ]


def _narrate_environment(snap, action_msg, intent, player_text, session_id, world_id) -> str:
    """用感知快照 + 玩家动作结果拼一段「旁白叙述」给 LLM 生成（行动/场景级旁白）。

    与 _environment_messages 的区别：这里是"运行时"真正调 LLM 拿回字符串 reply，
    而 _environment_messages 只负责构造 messages（供调试打印/兼容）。两者共用 FALLBACK_SYSTEM。
    """
    from .llm import DeepSeekClient
    sys_lines = [world_pack.storyteller_system(world_id)]
    if snap:
        sys_lines.append("以下是你当前所处世界的真实状态（回答玩家时以此为准，不要凭空编）：\n" + snap)
    if action_msg:
        sys_lines.append(f"玩家刚刚的动作结果：{action_msg}")
    sys_lines.append(
        f"玩家这句的意图：{intent.domain}({'会改变世界' if intent.side_effect == 'mutating' else '只读'})，"
        f"动作：{intent.verb or '（无）'}。请据此自然叙述。"
    )
    system = "\n\n".join(sys_lines)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": player_text},
    ]
    try:
        return DeepSeekClient().chat(messages)
    except Exception as e:  # noqa: BLE001
        logger.error("环境旁白 LLM 失败：%s", e)
        # 兜底：动作结果有就用它，否则退化成感知快照原文（可观测、不静默）。
        return action_msg or snap or ""


def narrate_scene(session_id: str, world_id: str, scene: str, view: str = "arriving",
                  partner: str = "") -> str:
    """为某场景调一次 LLM，生成一段「文学性场景叙事」（不是事实快照罗列）。

    为什么需要它：进房间只用 build_perception_snapshot（【你所在】【这里有】【你注意到】）
    是"事实查询"；但静态 desc（room json 写死的"角落有一把刀"）在物品被拿走后仍是旧的，
    会骗玩家。本函数拿「当前真实状态快照」让说书人 LLM 写一段"此刻你看到的景象"，
    状态说"无刀"，旁白就不会再说刀在。

    view（09-08 视角修正，问题1/2）：区分"刚进入"还是"停留后重述"，二者视角不同——
      · arriving：玩家刚走进来（移动进入）→ "你刚踏进"的入场视角；
      · lingering：玩家本就在此、对话后/略过片刻再重看 → "你在这儿待了一会儿"时间流逝视角；
      · conv_end（09-09 新增）：玩家刚结束一段对话、注意力回到现实 → 专属转场旁白，
        视角句带『你和{partner}结束了对话』，由对话结束回场景触发。
    此前固定"刚走进来"，导致玩家停留/略过后的场景重述视角错误。
      · opening（09-09 新增）：**只在游戏开局那一次**用。此时不调 LLM，直接返回环境卡预设
        描述（environment_card.description，即"初始文字"，改动只改数据不改代码），秒出，
        根治"首进场景被 LLM 拖 60s+ 卡住 loading"。

    入参必须是已按动态归属裁剪的感知快照（build_perception_snapshot），LLM 只做润色，
    不做事实推断（它不可靠）。失败降级：返回空串，客户端保住静态 desc，不阻塞进房。

    09-09：把原本内联的旁白约束抽到 world_pack.prompt（独立键、可被模组 prompts.json 覆盖），
    并按 arriving/lingering 两个视角拆成 scene_narrate_arriving / scene_narrate_lingering 两个独立键——
    统一在 PROMPTS 体系管理（统一）却又各视角独立、可分别覆盖（分开）。别再带 max_tokens（实测会空返回）。
    """
    # view == "opening"：开局初始场景 → 预设文字，不调 LLM（秒出，不阻塞进场景）。
    # 预设描述来自 spatial._scene_description（读 environment_card.description，即"初始文字"，改数据不改代码）。
    if view == "opening":
        return spatial_mod._scene_description(scene, world_id)

    from .llm import DeepSeekClient
    from .spatial import build_perception_snapshot
    snap = build_perception_snapshot(scene, session_id=session_id, world_id=world_id)
    if not snap:
        return ""
    # 按视角选独立键（统一在 world_pack 的 PROMPTS / 模组 prompts.json 体系里管理，模组可分别覆盖）。
    if view == "arriving":
        key = "scene_narrate_arriving"
        partner_name = ""
    elif view == "conv_end":
        key = "scene_narrate_conv_end"
        # partner 若传的是 npc_id，回查中文名给 {partner} 占位；读不到则原样用。
        partner_name = partner or ""
        try:
            from . import world_pack as wp
            partner_name = wp.npc_id_to_name(partner) or partner_name
        except Exception:  # noqa: BLE001  名字解析失败按传入原样
            partner_name = partner or ""
    else:
        key = "scene_narrate_lingering"
        partner_name = ""
    guide = world_pack.prompt(world_id, key)
    if "{partner}" in guide:
        guide = guide.replace("{partner}", partner_name or "TA")
    system = (
        world_pack.storyteller_system(world_id) + "\n\n"
        + guide + "\n当前真实状态：\n" + snap
    )
    try:
        # 09-09：实测确认 max_tokens=120 会让 DeepSeek 对"场景叙事"这类 prompt 稳定返回空串
        # （同 prompt 实测：带 120 → 空；不带 → 正常），导致环境描述旁白丢失、客户端只能落到
        # 快照"你看到：..."机械兜底。恢复不带 max_tokens（此前环境描述一直正常）。提速已由
        # opening 视角预设秒回 + 简短 prompt 承担，这里不再激进截断。
        return DeepSeekClient().chat([
            {"role": "system", "content": system},
            {"role": "user", "content": "描述这个场景。"},
        ])
    except Exception as e:  # noqa: BLE001  失败不挡进房：返回空串，客户端用静态兜底
        logger.error("场景叙事 LLM 失败：%s", e)
        return ""


def _scene_has_co_actor(session_id, scene, world_id) -> bool:
    """当前场景除玩家外是否还有【在场且存活】的 NPC。

    导演只在"同场景 ≥2 个行动者"时才接管（director.resolve_scenes 的 conflicted 判定）。
    玩家独自一人时不可能构成冲突，其改变环境的行动应当场立即执行并返回真实结果，
    而不是走登记制只给"命运的齿轮"固定台词（否则玩家不知道做没做成）。
    True = 场景还有其他存活行动者（可能冲突）→ 应 defer；False = 独处 → 立即执行。
    """
    try:
        from . import db as db_mod
        from . import observation as obs_mod
        for npc_id, _n in obs_mod._npcs_in_scene(session_id, scene, world_id):
            if npc_id == "player":
                continue
            if not db_mod.get_npc_status(session_id, npc_id).get("dead"):
                return True
    except Exception:  # noqa: BLE001  读不到则保守 defer（避免漏掉可能的冲突）
        return True
    return False


def run_environment(session_id, player_text, world_id, scene_override: str = "",
                    defer_mutating: bool = False) -> dict:
    """环境管线【运行时唯一入口】：main.py 在 npc_id=''（玩家对环境直接行动）时调用。

    它承接并扩展 _environment_messages 的能力，返回前端真正可用的 reply（字符串）。
    关键新增：**对象级观察（短路径）**——玩家要"仔细观察某个物体"（环境可及物或自己
    身上的物）时，走 observation.observe：
      · 本局快照命中（物品没变）→ 直接返回原话，不调 LLM（"两次观察一致"的保证）；
      · 首次/物品已变 → 拼对象厚描述+状态 → LLM 生成叙述 → 存快照。

    返回：{"reply": str, "changed": bool, "intent": dict}
    """
    from . import observation as obs_mod

    # 场景：优先用请求显式带的客户端当前房间（消除 sync_location 时序/会话不一致导致的
    # scene 为空），否则回退读会话存值。此前客户端刚移动就观察时，后端 scene 尚未同步，
    # 观察读空场景→"找不到椅子"。
    scene = scene_override.strip() or get_player_scene(session_id) or ""
    gs = get_game_state_map(session_id)
    tick = gs.get("current_tick", 0)
    entity_names = _entity_names_list(world_id)

    # ① 意图解析（多意图，09-10 设计决定）：一句话可能含多步动作
    #    on_notice 让"再解析一次"【立刻】可见——不必等 /chat 这个同步请求返回，
    #    前端在忙等期间轮询 /session/notices 就能看到"事情比想象中复杂……"。
    res = intent_mod.parse_player_input(
        player_text, scene_id=scene, world_id=world_id, entity_names=entity_names,
        on_notice=lambda msg: _push_parse_notice(session_id, msg))
    intents = res.intents
    mutating = [i for i in intents
                if i.domain in ("self", "spatial") and i.side_effect == "mutating"]

    # ---- ① 行动（mutating）：场景导演登记制（defer）或按序当场执行 ----
    if mutating:
        # 09-08 修复：只有当本场景"可能还有另一位存活行动者"（同场景 ≥2 人才触发导演
        # 冲突判定）时才登记进池；玩家独自一人时不可能冲突 → 走下方"立即执行"，当场返回
        # 真实结果（如"你拿起了刀。"），避免只有"命运齿轮"固定台词、玩家不知道做没做成。
        if defer_mutating and _scene_has_co_actor(session_id, scene, world_id):
            # 场景导演（设计裁决）：改变环境的意图登记进池，随 tick 与 NPC 行动
            # 按场景聚合裁决——本函数只返回"确认"，结果经 /world/updates 回来。
            # 多意图：整句的【全部】意图作为一条登记项进池（打成一个玩家参与者，
            # 而不是拆成多条——否则导演名单里会出现好几个"玩家"）。
            from .db import append_player_intent
            append_player_intent(session_id, {
                "intents": [i.to_dict() for i in intents],
                "intent": intents[0].to_dict(),      # 兼容只读首条的旧消费方
                "text": player_text, "scene": scene, "source": res.source,
            })
            return {"reply": "", "changed": False, "intent": intents[0].to_dict(),
                    "intents": [i.to_dict() for i in intents], "deferred": True,
                    "parse": res.to_dict()}
        return _run_mutating_now(session_id, player_text, world_id, scene, tick, res)

    # ---- ② 观察（只读）：单条对象级观察直接返回结果 ----
    # 注意：这里【故意】不额外调一次旁白 LLM——observation.observe 有自己的快照缓存，
    # 再包一层叙述会破坏"同一物品两次观察结果一致"的保证。
    if len(intents) == 1 and intents[0].domain == "spatial" \
            and (intents[0].spatial or {}).get("op") == "look":
        it = intents[0]
        target = obs_mod.resolve_observation_target(it.target.get("hint", ""), scene,
                                                    session_id, world_id)
        if target["kind"] == "entity":
            o = obs_mod.observe(target["env_id"], session_id, world_id,
                                hint=it.target.get("hint", ""))
            return _env_result(o["content"], o["changed"], it, res)
        if target["kind"] == "npc":
            o = obs_mod.observe_npc(target["env_id"], session_id, world_id)
            return _env_result(o["content"], o["changed"], it, res)

    # ---- ③ 其它（场景级观察 / 对话 / 其它只读）：场景快照旁白兜底 ----
    snap = spatial_mod.build_perception_snapshot(scene, session_id=session_id, world_id=world_id)
    reply = _narrate_environment(snap, "", intents[0], player_text, session_id, world_id)
    return _env_result(reply, False, intents[0], res)


def _env_result(reply: str, changed: bool, intent, res=None) -> dict:
    """环境管线的统一返回体（含多意图/解析来源，供调试与上层观测）。"""
    out = {"reply": reply, "changed": bool(changed), "intent": intent.to_dict()}
    if res is not None:
        out["intents"] = [i.to_dict() for i in res.intents]
        out["parse"] = {"source": res.source, "repaired": res.repaired,
                        "unresolved": res.unresolved}
    return out


def _push_parse_notice(session_id: str, text: str) -> None:
    """把解析层的"正在重试"提示推进提示池（前端忙等期间轮询取走显示）。

    为什么走"池 + 轮询"而不是直接返回：解析发生在 /chat 这个【同步阻塞】请求内部，
    请求不返回前端就看不到中途状态；而这一格的世界结算还要等几十秒。提示通道是
    让"机器在忙什么"对玩家可见的唯一侧信道。失败绝不抛（不能拖累解析/执行）。
    """
    try:
        from .db import push_notice
        push_notice(session_id, text)
    except Exception:  # noqa: BLE001  提示只影响体验，不能影响主流程
        logger.warning("即时提示入池失败（不影响解析）", exc_info=True)
        return
    try:
        from . import debug_trace
        debug_trace.record("player_notice", session_id=session_id, raw=text)
    except Exception:  # noqa: BLE001  观测失败同样不能影响主流程
        pass


def _run_mutating_now(session_id, player_text, world_id, scene, tick, res) -> dict:
    """当场按【说话先后顺序】执行 mutating 多意图，再统一出一段旁白。

    顺序语义（关键）：位置先落地，后续动作在【新位置】上生效——
    所以"去房间三攻击他"里，攻击是在 room_3 的感知下执行的；每步的 cur_scene 接力传递。
    单意图时行为与旧实现完全一致（action_msg = 那一条结果消息、旁白用那个 intent），
    多意图时把动词串起来（"拿、去、攻击"）喂旁白，让叙述体现"这几步都发生了"。
    """
    from . import observation as obs_mod
    cur_scene, changed, messages, executed = scene, False, [], []
    for it in res.intents:
        sp = it.spatial or {}
        op = sp.get("op")
        if it.domain in ("self", "spatial") and it.side_effect == "mutating":
            it = intent_mod.fill_default_placement(it, world_id)
            r = env_mod.execute_player_action(it, session_id, tick, world_id)
            changed = bool(r.get("changed", False)) or changed
            cur_scene = r.get("scene") or cur_scene
            if r.get("message"):
                messages.append(str(r["message"]))
            executed.append(it)
        elif it.domain == "spatial" and op == "look":
            # 顺序里的"顺手看一眼"：在上一步之后的场景里解析（可能在别的房间）
            tgt = obs_mod.resolve_observation_target(it.target.get("hint", ""), cur_scene,
                                                     session_id, world_id)
            if tgt["kind"] == "entity":
                o = obs_mod.observe(tgt["env_id"], session_id, world_id,
                                    hint=it.target.get("hint", ""))
            elif tgt["kind"] == "npc":
                o = obs_mod.observe_npc(tgt["env_id"], session_id, world_id)
            else:
                continue        # 场景级观察不出文字，交给结尾统一旁白
            if o.get("content"):
                messages.append(str(o["content"]))
            changed = bool(o.get("changed")) or changed

    snap = spatial_mod.build_perception_snapshot(cur_scene, session_id=session_id,
                                                 world_id=world_id)
    narr_intent = executed[-1] if executed else res.intents[-1]
    if len(executed) > 1:
        verbs = "、".join(i.verb for i in executed if i.verb)
        if verbs:
            narr_intent = intent_mod.Intent(narr_intent.domain, narr_intent.side_effect,
                                            narr_intent.target, verbs, narr_intent.spatial)
    reply = _narrate_environment(snap, "；".join(messages), narr_intent, player_text,
                                 session_id, world_id)
    return _env_result(reply, changed, executed[0] if executed else res.intents[0], res)

