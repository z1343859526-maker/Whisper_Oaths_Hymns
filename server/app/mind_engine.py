"""心智运行时引擎（T3）：把 mental_model（冷）+ npc_mental_state（热）+ 当前事件
送过该 NPC 自己的思维链，产出"中间态"（mental_ctx），再由 context_builder 拼进 prompt。

定位与分工（§7.1 / §7.4）：
- mental.py   = 纯函数（公式），不知道 DB 存在；
- 本模块      = 管线执行器：读状态 → 按触发类型走链上相关环节 → 调 mental.py →
              写回状态（npc_mental_state + game_state 压力值）→ 产出 mental_ctx；
- context_builder = 拼接层：把 mental_ctx 的中间态翻译成 L1~L7 状态词块；
- LLM 预算：/chat = 2 次（意图理解① + 回复生成②）；decide = 1 次（生成）。

思维链（环节库 + 个人序列，v0.3 裁决）：
  每个环节是思考的标准件；角色的 mental_model.thinking_chain 是他从库里选的序列。
  本模块按触发类型执行链上的相关环节：
    trigger=dialogue    → 意图理解(LLM①，调用方先做) → 证词评估 → 价值核对→情绪 → 底线压力
    trigger=observation → 环境观察(变化察觉) → 价值核对→情绪 → 计划对照(检视)
  "情绪调节"与"未来推演"环节目前内嵌在上述环节的参数里（抑制控制/风险项），
  待编排器独立成步（记录为欠账）。

有限视角：引擎只喂"该角色能感知到的"信息；没察觉到的变化不进任何环节。
"""
import json
import logging

from . import db
from . import mental
from . import plans
from . import scheduler
from . import memory as memory_mod

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 环节库：思考的标准件（executor=code 的由本模块/mental.py 执行；llm 的由调用方执行）
# ---------------------------------------------------------------------------
STEP_LIBRARY = {
    "环境观察": {"executor": "code", "desc": "变化察觉：先验 vs 观测，没察觉=丢弃"},
    "意图理解": {"executor": "llm", "desc": "他说这话想干嘛（求/骗/威胁/闲聊）"},
    "证词评估": {"executor": "code", "desc": "信念更新：多疑折扣×来源系数(信任,好感)÷抵抗力"},
    "情况分析": {"executor": "code", "desc": "价值树核对：碰没碰我看重的"},
    "情绪生成": {"executor": "code", "desc": "评估生情（OCC→PAD）——固定底座，不可省略"},
    "情绪调节": {"executor": "code", "desc": "抑制控制：镇定者压住表情，压不住的写在脸上"},
    "未来推演": {"executor": "code", "desc": "风险预估：行动打分的 risk 项来源"},
    "计划对照": {"executor": "code", "desc": "计划检视：受阻/机会 → 重评估策略"},
    "行动选择": {"executor": "code", "desc": "候选行动打分（禁区一票否决先于打分）"},
    "表达组织": {"executor": "llm", "desc": "语言生成：状态词+风格→台词"},
}

_DEFAULT_CHAIN = ["环境观察", "意图理解", "证词评估", "情况分析", "计划对照", "行动选择", "表达组织"]

# 证词分不再用固定值：v4§11 完整打分链（fact_check → score_testimony）已接入，
# 类型分×证据等级×关联度见 mental.INTENT_TYPE_WEIGHT / mental.EVIDENCE_LEVELS


def get_chain(mm: dict) -> list:
    """角色的思维链（环节序列）；未配置 → 默认全链。"""
    chain = mm.get("thinking_chain") if isinstance(mm, dict) else None
    return [str(s) for s in chain] if chain else list(_DEFAULT_CHAIN)


def has_mental_model(npc_id: str) -> bool:
    return db.get_mental_model(npc_id) is not None


def _init_state() -> dict:
    return {"emotion": {}, "emotion_word": "", "emotion_intensity": None,
            "beliefs": [], "noticed": [], "working_memory": [],
            "last_observation": None, "updated_tick": 0}


def _load_state(session_id: str, npc_id: str) -> dict:
    return db.get_mental_state(session_id, npc_id) or _init_state()


def read_emotion_snapshot(session_id: str, npc_id: str) -> dict:
    """只读情绪快照（P0-a 单一事实源入口）：从 `npc_mental_state` 热态派生取词。

    供旁白/前端/导演分析取"此刻神情"用，**不落库、不写任何状态**。永远返回合法词
    （无热态/无情绪时回退"平静"）。绝不再走 npc_status.mood 等副事实源。
    """
    try:
        state = db.get_mental_state(session_id, npc_id) or _init_state()
        return mental.emotion_snapshot(state)
    except Exception:  # noqa: BLE001  快照失败回退中性，绝不崩
        return mental._neutral_pad()


def _load_pressure(session_id: str, npc_id: str) -> dict:
    raw = db.get_game_state_map(session_id).get(f"pressure:{npc_id}")
    return dict(raw) if isinstance(raw, dict) else {}


# ---------------------------------------------------------------------------
# 意图理解环节（LLM①）：窄问题的 prompt 构造与解析
# ---------------------------------------------------------------------------

def _parse_access(card) -> list:
    """从角色卡 knowledge_scope 解析 can_access（与 context_builder._parse_access 同规则，
    此处独立实现避免循环导入）。"""
    try:
        scope = json.loads(card[7]) if card and card[7] else {}
    except (ValueError, TypeError, IndexError):
        return ["public"]
    if isinstance(scope, dict):
        access = scope.get("can_access")
        if isinstance(access, list) and access:
            return [str(a) for a in access]
    return ["public"]


def _world_facts(world_id: str) -> dict:
    """全知视角的世界状态快照（只给 code 做 fact-check，绝不进 prompt）：
    {f"{env_id}.{key}": value}。"""
    facts = {}
    for env_id, _kind, _name, _desc, env_state, _per in db.get_environment_cards(world_id):
        try:
            st = json.loads(env_state) if isinstance(env_state, str) else (env_state or {})
        except ValueError:
            st = {}
        for k, v in (st or {}).items():
            facts[f"{env_id}.{k}"] = v
    return facts


def _related(rule_text: str, topic: str) -> bool:
    """底线规则与秘密话题是否相关（2-gram 包含匹配——中文无分词的轻量近似）。"""
    rule_text, topic = str(rule_text or ""), str(topic or "")
    if not rule_text or not topic:
        return False
    grams = {rule_text[i:i + 2] for i in range(len(rule_text) - 1) if len(rule_text) >= 2}
    return any(g and g in topic for g in grams if len(g) == 2)


def build_understanding_messages(npc_id: str, player_text: str, world_id: str = "test") -> list:
    """构造"意图理解"LLM 调用。三件窄事（全部是事实匹配，不是价值判断）：
    ① 这句话在说什么（意图/话题/好坏/有没有怪罪谁）；
    ② 底线突破条件是否被这句话表明（是/否+依据）；
    ③ 举证类话语翻译成可核对的世界状态键值（fact-check 输入）+
      是否表明对方知晓某秘密（被动链识破进度）。"""
    mm = db.get_mental_model(npc_id) or {}
    kernel = mm.get("kernel", {}) if isinstance(mm, dict) and isinstance(mm.get("kernel"), dict) else {}

    rules = mental._hard_limit_rules(kernel)
    cond_lines = []
    for i, r in enumerate(rules):
        for j, cond in enumerate(r.get("breaking", []) or []):
            cond_lines.append(f"条件{i}.{j}（对应底线「{r.get('rule')}」）：{cond}")
    cond_block = "\n".join(cond_lines) if cond_lines else "（该角色暂无底线突破条件）"

    # 可核对状态键清单（来自环境卡，数据驱动；换世界自动跟随）
    key_lines = []
    for env_id, _kind, _name, _desc, env_state, _per in db.get_environment_cards(world_id):
        try:
            st = json.loads(env_state) if isinstance(env_state, str) else (env_state or {})
        except ValueError:
            st = {}
        keys = "、".join(f"{env_id}.{k}" for k in (st or {}).keys())
        if keys:
            key_lines.append(keys)
    keys_block = "\n".join(key_lines) if key_lines else "（无可核对状态）"

    # 秘密清单（供"是否表明对方知晓"窄判定；话题文本只在 code 侧流转）
    secret_lines = [f"{s['secret_id']}：{s['topic']}" for s in db.get_secrets(npc_id)]
    secrets_block = "\n".join(secret_lines) if secret_lines else "（该角色暂无秘密）"

    system = (
        "你是叙事引擎的「话语理解器」。只输出一个 JSON 对象，不要任何多余文字。\n"
        '格式：{"intent":"求助|威胁|欺骗嫌疑|举证|闲聊|试探|施压|安抚|其他","topics":["话题词"],'
        '"valence":"positive|neutral|negative","blame":false,\n'
        '"breaking_hits":[{"index":"i.j","matched":false,"evidence":""}],\n'
        '"claims":[{"about":"env_id","key":"状态键","value":声称值}],\n'
        '"secret_hits":[{"secret_id":"...","matched":false,"evidence":""}],\n'
        '"env_mentions":["env_id"]}\n'
        "要求：topics 提取这句话涉及的关键名词（各一两个词）；valence 判断对该 NPC 是利好还是利空；\n"
        "blame=是否把责任/过错归到该 NPC 头上；\n"
        "breaking_hits：逐条对照突破条件，判断这句话是否**表明**条件成立——只做事实匹配；\n"
        "claims：仅当 intent=举证时，把对方声称的事实翻译成下面清单里的可核对键值"
        "（无法核对的不要编造，直接省略）；\n"
        f"secret_hits：逐条判断这句话是否**表明对方已知晓**该秘密（证据引用原句短语）。\n"
        f"【可核对状态键】\n{keys_block}\n\n【秘密清单】\n{secrets_block}"
    )
    user = f"【突破条件清单】\n{cond_block}\n\n【这句话】\n{player_text}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_understanding(raw: str) -> dict:
    """解析理解器输出；失败给中性兜底（管线不因坏输出崩溃）。"""
    import re
    text = (raw or "").strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            u = json.loads(m.group(0))
            if isinstance(u, dict):
                u.setdefault("intent", "闲聊")
                u.setdefault("topics", [])
                u.setdefault("valence", "neutral")
                u.setdefault("blame", False)
                u.setdefault("breaking_hits", [])
                u.setdefault("env_mentions", [])
                return u
        except ValueError:
            pass
    return {"intent": "闲聊", "topics": [], "valence": "neutral", "blame": False,
            "breaking_hits": [], "claims": [], "secret_hits": [], "env_mentions": []}


# ---------------------------------------------------------------------------
# 触发一：dialogue —— 玩家对 NPC 说话
# ---------------------------------------------------------------------------

def process_dialogue(npc_id: str, session_id: str, world_id: str,
                     player_text: str, understanding: dict = None) -> dict or None:
    """走"证词评估 → 价值核对→情绪生成 → 底线压力"环节，写回热态，返回 mental_ctx。"""
    mm = db.get_mental_model(npc_id)
    if not mm:
        return None
    u = understanding if isinstance(understanding, dict) else parse_understanding(None)
    parse_ok = u if isinstance(u, dict) else parse_understanding(None)
    u = parse_ok
    if isinstance(u, dict):
        u.setdefault("claims", [])
        u.setdefault("secret_hits", [])
    kernel = mm.get("kernel", {}) if isinstance(mm.get("kernel"), dict) else {}
    state = _load_state(session_id, npc_id)

    # 环节：价值核对 → 情绪生成（话题 tags 驱动）
    # 计划取运行副本；会话尚未克隆计划时回读出厂意图（plan 是"他是谁"的一部分）
    plan = plans.get_active_plan(session_id, npc_id) or plans.get_active_plan("seed", npc_id)
    goal_text = str(plan.get("goal", "")) if plan else ""
    valence_map = {"positive": 0.6, "negative": -0.6}
    event = {
        "tags": [str(t) for t in u.get("topics", [])] + [str(u.get("intent", ""))],
        "congruence": valence_map.get(str(u.get("valence", "neutral")), 0.0),
        "unexpectedness": 0.6,
        "blame": bool(u.get("blame")),
        "goal_keywords": [goal_text] if goal_text else [],
    }
    new_emo = mental.appraise_emotion(event, mm, state.get("emotion") or {})
    state["emotion"] = {"valence": new_emo["valence"], "arousal": new_emo["arousal"],
                        "dominance": new_emo["dominance"]}
    state["emotion_word"] = new_emo["word"]
    state["emotion_intensity"] = round(float(new_emo["intensity"]), 3)
    state["emotion_family"] = new_emo["family"]  # 词映射用评估结论，防低强度 PAD 反推错档

    # 环节：证词评估（v4§11 完整链）——fact-check → 打分 → 信念更新
    # 来源系数 = 对玩家此刻的信任/好感；抵抗力 = 核心信念匹配
    rel = db.get_relationship(npc_id, "player", session_id)
    trust = rel[0] if rel else None
    affection = rel[2] if rel else None
    src = mental.source_coefficient(trust, affection)

    card = db.get_character_card(npc_id)
    allowed = _parse_access(card)
    known_lore = [f"{c[3]} {c[4]}" for c in
                  db.get_knowledge_candidates(npc_id, allowed, world_id)]  # 标题+正文
    npc_evidence = {
        "observed": list((state.get("last_observation") or {}).keys()),
        "known": known_lore,
        "heard": [str(w) for w in (state.get("working_memory") or [])],
    }
    fact = mental.fact_check(u.get("claims"), _world_facts(world_id), npc_evidence)

    tree = kernel.get("value_tree", {}) if isinstance(kernel.get("value_tree"), dict) else {}
    vt = mental.match_value_tree(event["tags"], tree, extra_cues=event["goal_keywords"])
    relevance = max(vt["relevance"],
                    0.5 if goal_text and any(t in goal_text for t in event["tags"]) else 0.0)
    score = mental.score_testimony(str(u.get("intent", "闲聊")), fact["level"], relevance)

    beliefs = list(state.get("beliefs") or [])
    core_beliefs = kernel.get("core_beliefs", []) or []
    for topic in [str(t) for t in u.get("topics", [])][:3]:
        if not topic:
            continue
        entry = next((b for b in beliefs
                      if topic in b.get("topic", "") or b.get("topic", "") in topic), None)
        if entry is None:
            entry = {"topic": topic, "confidence": 40.0, "state": "doubt",
                     "resistance": 0.0, "is_core": False}
            beliefs.append(entry)
        core = next((c for c in core_beliefs
                     if topic in str(c.get("topic", "")) or str(c.get("topic", "")) in topic), None)
        if core:
            entry["resistance"] = float(core.get("resistance", 0.5))
            entry["is_core"] = True
        r = mental.update_belief(entry["confidence"], score, mm,
                                 cur_emotion=state["emotion"],
                                 resistance=entry.get("resistance", 0.0),
                                 supporting=True, source_trust=trust,
                                 source_affection=affection)
        entry["confidence"] = round(float(r["new_conf"]), 1)
        entry["state"] = r["state"]
    state["beliefs"] = beliefs[:8]  # 工作集上限，防无限增长

    # 环节：底线压力（窄判定命中 → 压力累积；阈值内只是"念头"，过阈值才降级为选项）
    pressure = _load_pressure(session_id, npc_id)
    rules = mental._hard_limit_rules(kernel)
    for hit in u.get("breaking_hits", []) or []:
        if not isinstance(hit, dict) or not hit.get("matched"):
            continue
        idx = str(hit.get("index", "0.0")).split(".")
        try:
            rule = rules[int(idx[0])]
        except (IndexError, ValueError):
            continue
        name = str(rule.get("rule", ""))
        if rule.get("breaking"):
            pressure[name] = min(float(rule.get("threshold", 100)),
                                 float(pressure.get(name, 0)) + mental._PRESSURE_PER_HIT)
    if pressure:
        db.upsert_game_state(session_id, f"pressure:{npc_id}", pressure)

    # 环节：秘密双链（v4§13）——主动链（意愿→泄露口径）+ 被动链（识破进度→姿态）
    # 窄判定命中/嘴炮被抓 → detected_contradiction 累计（game_state KV，会话隔离）
    secrets_ctx = []
    detected = db.get_game_state_map(session_id).get(f"detected:{npc_id}")
    detected = dict(detected) if isinstance(detected, dict) else {}
    arousal_now = float(state["emotion"].get("arousal", 0.0))
    expr = mm.get("expression", {}) if isinstance(mm.get("expression"), dict) else {}
    for s in db.get_secrets(npc_id):
        # 底线封口：秘密与某条底线规则相关、且该底线压力未过阈值 → 锁 guard（底线优先）
        capped = False
        for r in rules:
            if _related(r.get("rule", ""), s["topic"]):
                if float(pressure.get(str(r.get("rule", "")), 0)) < float(r.get("threshold", 100)):
                    capped = True
                break
        rl = mental.reveal_level(s, trust, affection, arousal_now,
                                 float(expr.get("composure", 50)), hard_capped=capped)
        if rl["level"] != str(s.get("reveal_level", "guard")) and not capped:
            db.save_secret_reveal_level(npc_id, s["secret_id"], rl["level"])  # 回写供审计
        # 被动链计数：嘴炮被抓 / 对方表明知晓秘密
        n = int(detected.get(s["secret_id"], 0))
        if fact.get("bluff"):
            n += 1
        if any(isinstance(h, dict) and h.get("matched") and
               str(h.get("secret_id", "")) == s["secret_id"] for h in u.get("secret_hits", []) or []):
            n += 1
        detected[s["secret_id"]] = n
        stance = mental.contradiction_stance(
            n, float(expr.get("composure", 50)), s.get("detected_response"))
        secrets_ctx.append({"secret_id": s["secret_id"], "topic": s["topic"],
                            **rl, "stance": stance["word"] if stance["alert"] else "",
                            "alert": stance["alert"], "progress": n})
    if detected:
        db.upsert_game_state(session_id, f"detected:{npc_id}", detected)

    # 工作记忆：本轮玩家提到什么（滚动保留 6 条）
    wm = list(state.get("working_memory") or [])
    for t in [str(t) for t in u.get("topics", [])][:2]:
        wm.append(f"玩家提到过：{t}")
    state["working_memory"] = wm[-6:]

    tick = db.get_game_state_map(session_id).get("current_tick", 0)
    db.save_mental_state(session_id, npc_id, {
        "emotion": state["emotion"], "emotion_word": state["emotion_word"],
        "emotion_intensity": state["emotion_intensity"], "beliefs": state["beliefs"],
        "working_memory": state["working_memory"],
    }, tick=int(tick) if isinstance(tick, (int, float)) else 0)

    return {"mm": mm, "state": state, "plan": plan, "relationship": rel,
            "pressure": pressure, "understanding": u, "source_coeff": src,
            "emotion_family": new_emo["family"], "fact": fact, "secrets_ctx": secrets_ctx,
            "session_id": session_id, "world_id": world_id, "tick": tick}


# ---------------------------------------------------------------------------
# 触发二：observation —— 涌现线每 tick 的环境观察（agent.decide / simulate 用）
# ---------------------------------------------------------------------------

def process_observation(npc_id: str, session_id: str, world_id: str, tick: int) -> dict or None:
    """走"环境观察 → 价值核对→情绪生成 → 计划对照"环节。

    首次观察只存基线（先验尚无，无从谈"变化"）；之后每 tick 与上一帧对比。"""
    mm = db.get_mental_model(npc_id)
    if not mm:
        return None
    state = _load_state(session_id, npc_id)
    kernel = mm.get("kernel", {}) if isinstance(mm.get("kernel"), dict) else {}

    observation = {}
    for env_id, _kind, _name, _desc, env_state, _per in db.get_environment_cards(world_id):
        try:
            st = json.loads(env_state) if isinstance(env_state, str) else (env_state or {})
        except ValueError:
            st = {}
        for k, v in (st or {}).items():
            observation[f"{env_id}.{k}"] = v

    # 计划取运行副本；未克隆时回读出厂意图（与 process_dialogue 同一约定）
    plan = plans.get_active_plan(session_id, npc_id) or plans.get_active_plan("seed", npc_id)
    goal_text = str(plan.get("goal", "")) if plan else ""
    tree = kernel.get("value_tree", {}) if isinstance(kernel.get("value_tree"), dict) else {}
    goal_keywords = [goal_text] if goal_text else []
    goal_keywords += [str(c) for ch in tree.get("children", []) or []
                      for c in (ch.get("cues", []) or [])][:3]

    seed = f"{session_id}:{npc_id}:obs:{tick}"
    priors = state.get("last_observation")
    noticed, detected = [], False
    new_emo = None
    if priors:
        dr = mental.detect_change(priors, observation, mm, goal_keywords, seed=seed)
        detected = dr["detected"]
        noticed = dr["noticed"]
        if detected and noticed:
            event = {"tags": noticed[:3], "congruence": -0.3, "unexpectedness": 0.7,
                     "goal_keywords": goal_keywords}
            new_emo = mental.appraise_emotion(event, mm, state.get("emotion") or {})
            state["emotion"] = {"valence": new_emo["valence"], "arousal": new_emo["arousal"],
                                "dominance": new_emo["dominance"]}
            state["emotion_word"] = new_emo["word"]
            state["emotion_intensity"] = round(float(new_emo["intensity"]), 3)
            wm = list(state.get("working_memory") or [])
            wm.append(f"我注意到：{noticed[0]}")
            state["working_memory"] = wm[-6:]
    state["noticed"] = noticed
    state["last_observation"] = observation

    # 环节：计划对照（检视）——受阻的计划交给 resolve_plan 出策略（重规划执行待 T6）
    replan_decision = None
    if plan and str(plan.get("status", "")) == "blocked":
        replan_decision = mental.resolve_plan(plan, mm, kernel,
                                              block_reasons=[str(plan.get("blocked_reason", "受阻"))])
        state["working_memory"] = (list(state.get("working_memory") or []) +
                                   [f"计划受阻，我的判断：{replan_decision['decision']}"])[-6:]

    db.save_mental_state(session_id, npc_id, {
        "emotion": state["emotion"], "emotion_word": state.get("emotion_word", ""),
        "emotion_intensity": state.get("emotion_intensity"),
        "noticed": state["noticed"], "working_memory": state["working_memory"],
        "last_observation": observation,
    }, tick=int(tick))

    return {"mm": mm, "state": state, "plan": plan, "relationship": None,
            "pressure": _load_pressure(session_id, npc_id),
            "replan_decision": replan_decision, "detected": detected,
            "session_id": session_id, "world_id": world_id, "tick": tick}


def snapshot_ctx(npc_id: str, session_id: str) -> dict or None:
    """只读版 ctx（不改状态）：给流式/兜底路径拼 prompt 用。
    秘密块只算主动链口径（不含本轮事件引起的升级），保证流式与同步口径一致。"""
    mm = db.get_mental_model(npc_id)
    if not mm:
        return None
    rel = db.get_relationship(npc_id, "player", session_id)
    state = _load_state(session_id, npc_id)
    expr = mm.get("expression", {}) if isinstance(mm.get("expression"), dict) else {}
    kernel = mm.get("kernel", {}) if isinstance(mm.get("kernel"), dict) else {}
    pressure = _load_pressure(session_id, npc_id)
    detected = db.get_game_state_map(session_id).get(f"detected:{npc_id}")
    detected = dict(detected) if isinstance(detected, dict) else {}
    secrets_ctx = []
    for s in db.get_secrets(npc_id):
        capped = any(_related(r.get("rule", ""), s["topic"]) and
                     float(pressure.get(str(r.get("rule", "")), 0)) < float(r.get("threshold", 100))
                     for r in mental._hard_limit_rules(kernel))
        rl = mental.reveal_level(s, rel[0] if rel else None, rel[2] if rel else None,
                                 float((state.get("emotion") or {}).get("arousal", 0.0)),
                                 float(expr.get("composure", 50)), hard_capped=capped)
        n = int(detected.get(s["secret_id"], 0))
        stance = mental.contradiction_stance(n, float(expr.get("composure", 50)),
                                             s.get("detected_response"))
        secrets_ctx.append({"secret_id": s["secret_id"], "topic": s["topic"],
                            **rl, "stance": stance["word"] if stance["alert"] else "",
                            "alert": stance["alert"], "progress": n})
    return {"mm": mm, "state": state,
            "plan": plans.get_active_plan(session_id, npc_id) or
            plans.get_active_plan("seed", npc_id),
            "relationship": rel, "pressure": pressure, "secrets_ctx": secrets_ctx,
            "session_id": session_id, "world_id": None}


# ---------------------------------------------------------------------------
# 自我记忆与秘密演化（世界时序 v2 / W3+W4）：
# NPC 要记得自己做了什么、说了什么——包括说过谎。谎言在心里是"我知道的真相 +
# 我对外的版本"两条记录；**关键谎话**才注册成可被戳穿的新秘密（被动链适用），
# 客套敷衍谎只进记忆不注册（09-09 用户：避免"这里很安静"刷进秘密区）。
# 说漏嘴用代码侧确定性检测（mental.secret_slip），不信任 LLM 自报。
# ---------------------------------------------------------------------------

# 关键谎判定里要剔除的场景虚词：这些字词在秘密 topic 和普通客套话里都会出现，
# 若保留会导致"这里很安静"这类敷衍谎被误判成"在隐瞒核心秘密"而注册成秘密。
# 只把**实质词**（刀/杀/女人/计划/猎杀/房间三等）当作"触及核心秘密"的信号。
_SCENE_STOPWORDS = frozenset(
    "这里 那里 这个 那个 一个 什么 怎么 就是 为了 然后 因为 所以 我们 你们 他们 "
    "刚才 现在 已经 没有 不是 还是 不过 只是 可能 应该 可以 知道 觉得 确实 真的 "
    "安静 刚刚 刚刚好 挺好 不错 请 谢谢 没关系 不必 别 看看".split())


def _key_grams(topic: str) -> set:
    """从秘密 topic 提取"实质 2-gram"：全量 2-gram 剔除以场景虚词开头的。
    这样"我来这里…"里的"这里"被剔除，只留下"杀掉/房间/女人"等实义词对。"""
    topic = str(topic or "")
    grams = set()
    for i in range(len(topic) - 1):
        g = topic[i:i + 2]
        if g[0] in _SCENE_STOPWORDS or g in _SCENE_STOPWORDS:
            continue
        if any(w in g for w in _SCENE_STOPWORDS):
            continue
        grams.add(g)
    return grams


def _is_key_lie(npc_id: str, speech: str) -> bool:
    """这句谎算不算"关键谎话"（值得注册为可被戳穿的秘密）？

    判定：谎话触及本 NPC 的**关键词语料**（秘密 topic + goals.plan/note + 角色卡
    forbidden 的实质 2-gram，剔除"这里/那个"等场景虚词）——说明这套谎是在隐瞒
    自己的核心秘密，才值得被"戳穿"。与秘密无关的客套/敷衍谎（如"这里很安静"）
    不进秘密区，仅留在记忆里（09-09 用户：秘密只聚焦"男杀女"主剧情）。

    为什么聚合多词源而非仅 topic：秘密 topic 只是"秘密的一句话"，而 NPC 的核心
    剧情用词（如"猎杀计划""取刀"）可能只在 goals/forbidden 里出现；只用 topic
    会漏检。聚合语料让判定覆盖整个角色的核心主题词。
    """
    try:
        text = str(speech or "")
        if len(text) < 2:
            return False
        corpus = []
        for s in db.get_secrets(npc_id):
            corpus.append(str(s.get("topic", "")))
        for g in db.get_goals(npc_id):
            corpus.append(str(g.get("plan", "")))
            corpus.append(str(g.get("note", "")))
        card = db.get_character_card(npc_id)
        if card and len(card) > 6:
            corpus.append(str(card[6] or ""))  # forbidden
        grams = set()
        for piece in corpus:
            grams |= _key_grams(piece)
        return any(len(g) == 2 and g in text for g in grams)
    except Exception:  # noqa: BLE001  判定失败保守注册，不吞谎
        return True


def record_self_action(session_id: str, npc_id: str, tick: int,
                       decision: dict, world_id: str = "test") -> dict or None:
    """NPC 行动后的自我记忆（agent.decide / simulate 主循环调用）。

    记三层：
    ① 我做了什么（event，importance 6）——含心里真实意图；
    ② 我对外说了什么——若自报撒谎（honest=false），额外记一条
      "我说了谎：对外版本 vs 心里真相"（importance 8）；关键谎话才注册为新秘密，
      客套谎只进记忆（_is_key_lie 判定，09-09 防"这里很安静"刷成秘密）；
    ③ 说漏嘴检测：台词触碰守口如瓶秘密的话题 → 口径升一档 + 记忆留痕。
    """
    try:
        if not decision or not db.get_mental_model(npc_id):
            return None
        action = decision.get("action") or {}
        atype = str(action.get("type", "wait"))
        intent = str(decision.get("intent", "")).strip()
        speech = decision.get("speech")
        location = str(action.get("location", "") or "原地")
        clock = scheduler.tick_to_clock(tick)
        out = {"memory_written": False, "lied": False, "slips": []}

        # ① 行动记忆：做什么 + 心里真实的意图（记忆里永远存真相）
        detail = str(action.get("detail", "")).strip()
        act_desc = {"move": f"移动到{action.get('target', '')}",
                    "use_item": f"使用了{action.get('target', '')}",
                    "give_item": f"把{action.get('target', '')}给了别人",
                    "speak": f"和{action.get('target', '')}说话",
                    "observe": f"观察{action.get('target', '')}",
                    "interact": detail or f"与{action.get('target', '')}互动",
                    "trigger_event": f"触发了{action.get('target', '')}",
                    "wait": "原地等待",
                    "converse": f"与{action.get('target', '')}交谈"}.get(atype, f"{atype}({detail or action.get('target', '')})")
        memory_mod.write_memory(
            npc_id, "event",
            f"{clock} 我在{location}{act_desc}。我心里想的是：{intent or '没有特别的念头'}",
            importance=6, summary=f"我{act_desc}", related_entity=action.get("target", ""),
            session_id=session_id)
        out["memory_written"] = True

        # ② 言语记忆 + 谎言追踪
        if speech:
            lied = decision.get("honest") is False
            out["lied"] = lied
            if lied:
                memory_mod.write_memory(
                    npc_id, "event",
                    f"我说了谎：对外说「{speech}」，其实我心里想的是：{intent or '另有打算'}",
                    importance=8, summary="我说了谎", session_id=session_id)
                # 说谎者记忆：这句谎话是"我对外说的版本"，记进记忆（真相另记一条）。
                memory_mod.write_memory(
                    npc_id, "event", f"我对外说了谎：「{str(speech)[:40]}」",
                    importance=8, summary="我撒了谎", session_id=session_id)
                # 只有"关键谎话"才注册为可被戳穿的秘密（09-09 用户：客套敷衍谎不该
                # 刷进秘密区、与主剧情无关）。关键判定=谎话触及本 NPC 现有秘密的
                # 关键词（secret_slip 同源 2-gram），说明是在隐瞒自己的核心秘密。
                # 其余客套谎话只进记忆，不注册秘密——避免"这里很安静"刷成守口如瓶。
                if _is_key_lie(npc_id, str(speech)):
                    lie_secret_id = f"sec_lie_{abs(hash(f'{npc_id}:{speech}')) % 100000:05d}"
                    db.add_secret(npc_id, lie_secret_id,
                                  f"我谎称过：「{str(speech)[:40]}」", reveal_level="guard",
                                  detected_response=["deny", "confess"])
                    out["lie_secret_id"] = lie_secret_id
            else:
                memory_mod.write_memory(
                    npc_id, "event", f"{clock} 我对外说：「{speech}」",
                    importance=5, summary=f"我说：「{str(speech)[:30]}」",
                    session_id=session_id)
            # ③ 说漏嘴检测（对当前所有守口如瓶/可暗示的秘密）
            for s in db.get_secrets(npc_id):
                if mental.secret_slip(s["topic"], str(speech), s["reveal_level"]):
                    new_lv = mental.slip_level(s["reveal_level"])
                    db.save_secret_reveal_level(npc_id, s["secret_id"], new_lv)
                    memory_mod.write_memory(
                        npc_id, "event",
                        f"{clock} 我说漏了嘴——关于「{s['topic']}」，我不该提的",
                        importance=8, summary="我说漏了嘴", session_id=session_id)
                    out["slips"].append({"secret_id": s["secret_id"], "level": new_lv})
        return out
    except Exception as e:  # noqa: BLE001  记忆失败不挡决策主链路
        logger.warning("record_self_action 失败：%s", e)
        return None


def record_blocked_intent(session_id: str, npc_id: str, tick: int,
                          intent_desc: str, reason: str) -> bool:
    """记录一次「意图受阻/被拒」的自我认知事实(09-09 用户拍板)。

    与 record_self_action(记录"最终实际做什么")互补：这里记录"我想做某事，但没做成
    的原因"。speak 被对方拒绝、move 遇到路堵/不可达等，都是 NPC 真实经历，必须进记忆，
    否则它会表现得"从没想过要做这件事"，造成行为与自我认知脱节。

    ⚠️ 一次 tick 内若意图被拒后多次递推(如 speak 被拒→move 又路堵)，每次受阻都应调用
    一次本函数——用户明确要求"同轮里多次行为改变都记录下来"，因此**绝不覆盖、逐条累加**。

    Args:
        intent_desc: 原意图人话描述(如"和测试男说话"/"移动到房间一")。
        reason: 受阻/被拒原因(如"对方拒绝了交谈"/"道路被堵")。
    Returns:
        写入成功 True；失败 False(不阻塞主链路)。
    """
    try:
        clock = scheduler.tick_to_clock(tick)
        text = (intent_desc or "").strip() or "做点什么"
        memory_mod.write_memory(
            npc_id, "event",
            f"{clock} 我想{text}，但{reason or '没有做成'}。",
            importance=5, summary=f"受阻：{reason or text}",
            session_id=session_id)
        return True
    except Exception as e:  # noqa: BLE001  记忆失败不挡调用方
        logger.warning("record_blocked_intent 失败：%s", e)
        return False


def on_reply(npc_id: str, session_id: str, reply: str) -> list:
    """对话回复后的秘密演化钩子（/chat 调用，配合 memory.remember_conversation）。

    只做说漏嘴检测：台词触碰 guard/hint 级秘密的话题 → 口径升一档 + 记忆留痕。
    谎言追踪在 /chat 侧暂缺（回复无 intent 对照），列为欠账。
    Returns: 说漏嘴事件列表（供调用方日志/测试断言）。
    """
    slips = []
    try:
        if not reply or not db.get_mental_model(npc_id):
            return slips
        for s in db.get_secrets(npc_id):
            if mental.secret_slip(s["topic"], str(reply), s["reveal_level"]):
                new_lv = mental.slip_level(s["reveal_level"])
                db.save_secret_reveal_level(npc_id, s["secret_id"], new_lv)
                memory_mod.write_memory(
                    npc_id, "event",
                    f"我说漏了嘴——关于「{s['topic']}」，我不该提的",
                    importance=8, summary="我说漏了嘴", session_id=session_id)
                slips.append({"secret_id": s["secret_id"], "topic": s["topic"], "level": new_lv})
    except Exception as e:  # noqa: BLE001
        logger.warning("on_reply 失败：%s", e)
    return slips


# ---------------------------------------------------------------------------
# NPC 的有限视角环境事实（决策 prompt 用）+ 足迹系统（已知房间=去过的房间）
#
# 设计（用户裁决）：NPC 开局对世界一无所知——它"知道的房间"只来自【去过】；
# 详细环境来自【它观察时收到的原文】（存进记忆），而不是厚描述原卡。
# 与空间快照（叙事模板）的关系：同一事实层，不同渲染——决策要结构化事实。
# ---------------------------------------------------------------------------

def get_visited(session_id: str, npc_id: str) -> list:
    """该 NPC 去过的房间（足迹，game_state KV `visited:{npc}`）。"""
    v = db.get_game_state_map(session_id).get(f"visited:{npc_id}")
    return [str(x) for x in v] if isinstance(v, list) else []


def note_visited(session_id: str, npc_id: str, scene: str, world_id: str = "test") -> bool:
    """记录足迹；首次到访 → 写一条"初到印象"记忆（此地的客观事实快照，非厚描述）。

    Returns: 是否首次到访。
    """
    visited = get_visited(session_id, npc_id)
    if scene in visited or not scene:
        return False
    visited.append(scene)
    db.upsert_game_state(session_id, f"visited:{npc_id}", visited)
    try:
        from . import memory as memory_mod
        fact = fact_snapshot(scene, session_id, world_id, observer=npc_id)
        if fact:
            memory_mod.write_memory(
                npc_id, "event", f"我来到了{scene}。{fact}",
                importance=6, summary=f"初到{scene}", session_id=session_id)
    except Exception:  # noqa: BLE001  记忆失败不影响足迹记录
        pass
    return True


def fact_snapshot(scene: str, session_id: str, world_id: str, observer: str) -> str:
    """决策用事实模板（有限视角）：所在房间 + 在场物/人 + 可去房间。零坐标、零他室信息。"""
    lines = [f"【你所在】{scene}"]
    cards = db.get_environment_cards(world_id)
    here, rooms = [], []
    for env_id, kind, name, _desc, state_raw, _p in cards:
        try:
            st = json.loads(state_raw) if isinstance(state_raw, str) else (state_raw or {})
        except ValueError:
            st = {}
        w = st.get("where") or {}
        if kind == "location":
            rooms.append({"env_id": env_id, "name": name})
            continue
        if w.get("mode") == "held":
            continue   # 被人拿在手里：不在任何房间
        eff_scene = w.get("scene") or st.get("current_place")
        if eff_scene == scene:
            desc_bits = [f"{name}（{env_id}）"]
            for k in ("state", "stained", "locked"):
                if k in st:
                    desc_bits.append(f"{k}={st[k]}")
            here.append("；".join(desc_bits))
    # 在场的其他 NPC（位置来自 npc_pos——有限视角：只看得到同房间的）
    for other in db.get_all_npc_ids(world_id):
        if other == observer:
            continue
        if db.get_npc_pos(session_id, other) == scene:
            alive = not db.get_npc_status(session_id, other).get("dead")
            here.append(f"{other}（{'在场' if alive else '倒在地上'}）")
    lines.append("【这个房间里】" + ("；".join(here) if here else "似乎没有什么显眼的东西"))
    # 可达房间（连通+门开）：spatial 有骨架用骨架，没有则退化为足迹
    try:
        from . import spatial
        reachable = [c for c in spatial.connected_scenes(scene, world_id)
                     if spatial.can_reach(scene, c, world_id)]
        if reachable:
            lines.append("【现在能去的房间】" + "、".join(reachable))
    except Exception:  # noqa: BLE001  无空间骨架的世界：足迹即认知
        visited = get_visited(session_id, observer)
        if visited:
            lines.append("【你去过的房间】" + "、".join(visited))
    return chr(10).join(lines)
