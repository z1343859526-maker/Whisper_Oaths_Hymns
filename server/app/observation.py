"""环境观察管线：把玩家的「对象级观察」落地成详细、且带本局快照记忆的描述。

为什么需要它（观察对象化的核心）：
  · 原感知快照（build_perception_snapshot）是「场景级」：只描述"这个房间有什么"的动态状态，
    不聚焦"这个物体本身什么细节"。
  · 玩家要"仔细观察这把椅子/这把刀"时，必须定位到具体对象，并给出该物自身的信息。
    这需要三件事（用户点名）：
      ① 改变观察逻辑：不限于场景——环境可及物、或自己身上的物，都可作为观察对象；
      ② 厚描述足够详细：每个物体在独立信息存储里要有极详的 detail 层，才有细节可讲；
      ③ 本局快照记忆：观察过一次后把叙述存进本局；物品没变就返回原话（前/后一致）。

铁律协同（与整套环境管线一致）：
  · 事实（厚描述 description / 深细节 detail / 当前 state / 位置）由程序从 DB 取出 ⇒ 确定性；
  · LLM 只做"把事实润色成一段观察叙述"，**不创造新细节** ⇒ 前后一致 + 不幻觉；
  · 信息厚度受玩家【观察力 perception】影响：低于门槛只看简述（detail 层被挡住），
    达到门槛才能展开深层细节 ⇒ 这正是"信息厚度受观察力影响"。

快照记忆机制（用户要求的"本局快照记录"）：
  · 首次观察 → 拼 prompt → LLM 生成叙述 → 存快照{content, fingerprint}；
  · 再次观察：若当前 state 的 fingerprint 与快照一致 ⇒ 物品没变 ⇒ 直接返回快照 content（原话）；
  · 若指纹变了（物品被 LLM 判定/程序写回改变过）⇒ 重新走分析、刷新快照。
"""
import hashlib
import json
import logging

from . import db
from . import spatial

logger = logging.getLogger(__name__)

# 模块级 LLM 单例：复用连接，避免重复建客户端（与 intent/agent 同款）。
_client = None


def _llm():
    global _client
    if _client is None:
        from .llm import DeepSeekClient
        _client = DeepSeekClient()
    return _client


# 观察叙述的 system 提示（"只润色、不增造"是防幻觉/保一致的关键）。
# 颗粒度分三档（与用户要求对齐：不是所有观察都事无巨细，要随观察力与行动重点取舍）：
#   brief  —— 扫视/只是看一眼：1~2 句概述；
#   normal —— 一般"看看"：点出最显眼、或与本次查看目的相关的那几点，其余省略；
#   detail —— 刻意仔细检查：围绕查看重点尽量详实展开（材质/做工/异常痕迹）。
_OBSERVE_SYSTEMS = {
    "brief": (
        "你是文字冒险游戏里替观察者「读景」的旁白。玩家只是扫了一眼这件物品，"
        "你要给出这一段简短的观察叙述。\n"
        "铁律：\n"
        "- 只依据下方提供的【事实】，用自然的文学口吻组织，**不虚构不存在的新细节**；\n"
        "- 只用一两句话概括它在这个距离/角度下最显眼的样貌，**不要铺陈材质、尺寸、工艺等细节**；\n"
        "- 若当前状态异常（倒着/沾血/缺部件/在谁手中），用半句点明即可；\n"
        "- 输出一小段话即可，不要列表。"
    ),
    "normal": (
        "你是文字冒险游戏里替观察者「读景」的旁白。玩家对这件物品做了普通观察，"
        "你要据此给出这一段观察叙述。\n"
        "铁律：\n"
        "- 只依据下方提供的【事实】，用自然的文学口吻组织，**不虚构不存在的新细节**；\n"
        "- 点到即止：围绕它**最显眼、或与你本次查看目的相关**的两三点即可（约2~3句），"
        "**不要事无巨细、不要逐条罗列**；\n"
        "- 没提到的方面（如材质、气味）先略过，不要凭空补；\n"
        "- 若当前状态异常（倒着/沾血/缺部件/在谁手中），一定要如实写出来；\n"
        "- 输出一段话即可，不要列表、不要第三人称评价观察者。"
    ),
    "detail": (
        "你是文字冒险游戏里替观察者「读景」的旁白。玩家刻意凑近仔细检查这件物品，"
        "你要据此给出这一段的观察叙述。\n"
        "铁律：\n"
        "- 只依据下方提供的【事实】，用自然的文学口吻组织，**不虚构不存在的新细节**；\n"
        "- 围绕你刻意检查的**重点**（如材质/做工/缝隙/异常痕迹）尽量详实展开（4~6句）；\n"
        "- 若【事实】没提到某项，就写「看不真切」，不要凭空补；\n"
        "- 若当前状态异常（倒着/沾血/缺部件/在谁手中），一定要如实写出来；\n"
        "- 输出一段话即可，不要列表、不要第三人称评价观察者。"
    ),
}

# 观察力门槛：低于此值的玩家看不到 detail 层（只能看简述 / 细节"看不真切"）。
_DETAIL_PERCEPTION_THRESHOLD = 50

# 观察强度信号词：决定本次观察"扫视 / 普通 / 刻意细查"。
_BRIEF_WORDS = ("扫一眼", "瞥", "瞄", "瞅一眼", "看个大概", "粗略", "快速", "匆匆",
                "环顾", "扫视", "一眼", "看看有没有什么")
_CLOSE_WORDS = ("仔细", "细看", "细查", "检查", "查看", "端详", "审视", "翻看", "翻找",
                "凑近", "贴近", "钻研", "研究", "详察", "打量", "缝隙", "检查一下")


def _observation_intensity(text: str) -> str:
    """根据玩家措辞判断本次观察的颗粒度意愿：brief / normal / detail。"""
    if not text:
        return "normal"
    for w in _CLOSE_WORDS:
        if w in text:
            return "detail"
    for w in _BRIEF_WORDS:
        if w in text:
            return "brief"
    return "normal"


def _effective_granularity(perception: float, intensity: str) -> str:
    """把「观察力」与「行动意愿」合成最终颗粒度。

    观察力是硬上限：perception < 50 时连 detail 层都看不真切，因此最多给 normal；
    无此限制时，颗粒度 = 玩家本次行动的重点（brief/normal/detail）。
    """
    if float(perception) < _DETAIL_PERCEPTION_THRESHOLD:
        return "brief" if intensity != "detail" else "normal"
    return intensity


# ---------------------------------------------------------------------------
# 指纹：判断"物品是否变化了"
# ---------------------------------------------------------------------------
def fingerprint(state) -> str:
    """对物品当前 state 取稳定指纹（用于"物品是否变化了"的判定）。

    只对 state 取指纹，不含 description/detail——观察"同一物未变"的依据是
    "它的可变状态/归属有没有变"，而非描述（描述是静态的）。
    用 md5 对"排序后的 JSON 字符串"取指纹，保证字段顺序不影响结果。
    """
    try:
        raw = json.dumps(state, ensure_ascii=False, sort_keys=True)
    except (ValueError, TypeError):
        raw = json.dumps(str(state), ensure_ascii=False)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def state_changed(snap_state, current_state) -> bool:
    """物品是否变化了：快照时的 state 指纹 vs 当前 state 指纹不一致 = 变了。"""
    return fingerprint(snap_state) != fingerprint(current_state)


# ---------------------------------------------------------------------------
# 观察对象定位：不限于场景——环境可及物 / 自己身上的物 / 场景整体
# ---------------------------------------------------------------------------
def _core_tail(name: str) -> str:
    """取名字的中心名词（去量词、去尾部），供 hint 子串匹配。例：'一把刀'->'刀'。"""
    for pre in ("一把", "一柄", "一张", "一只", "一间", "一顶"):
        if name.startswith(pre):
            name = name[len(pre):]
    return name


def _hits_target(hint: str, name: str) -> bool:
    """hint 是否指向名叫 name 的物体：中心词命中（或单字兜底）。"""
    if not hint:
        return False
    core = _core_tail(name or "")
    if not core:
        return False
    if core in hint:
        return True
    # 单字兜底（"刀"在 hint 中）
    if len(core) >= 1 and core[-1] in hint:
        return True
    return False


def _npcs_in_scene(session_id, scene, world_id="test"):
    """当前场景「在场」的 NPC（npc_pos==scene）列表 [(npc_id, name)]。

    NPC 也是可观察对象（角色）。只有"在场"的才可被对象级观察；尸体（dead npc）同样在此
    ——npc_pos 不变、状态 dead，观察会读到"这是一具尸体"。这为"杀人→尸体可观察"打地基。
    """
    out = []
    for npc_id in db.get_all_npc_ids(world_id):
        if db.get_npc_pos(session_id, npc_id) == scene:
            card = db.get_character_card(npc_id)
            out.append((npc_id, card[1] if card else npc_id))
    return out


def resolve_observation_target(hint, scene, session_id, world_id="test"):
    """决定玩家想观察的对象。返回 {"kind":"scene|entity","env_id":"","name":""}。

    优先级（观察对象化，用户要求①）：
      ① 玩家手持物（get_held_items：mode=held 且 holder=player）——"观察手里的刀"；
      ② 当前场景实体（entities_in，环境可达物）——"观察那把椅子"；
      ③ 都没确定 → 退回"观察整个场景"（kind=scene）。
    """
    hint = hint or ""
    # ① 自己身上的物（held by player）
    for it in spatial.get_held_items(world_id, holder="player"):
        if _hits_target(hint, it["name"]):
            return {"kind": "entity", "env_id": it["env_id"], "name": it["name"]}
    # ② 环境可达物
    for e in spatial.entities_in(scene, world_id):
        if e["type"] == "room":
            continue
        if _hits_target(hint, e["name"]) or (e.get("anchor_label") and e["anchor_label"] in hint):
            return {"kind": "entity", "env_id": e["env_id"], "name": e["name"]}
    # ②.5 当前场景在场的 NPC（角色也可观察；尸体=dead npc 同样定位到这里）
    for npc_id, npc_name in _npcs_in_scene(session_id, scene, world_id):
        if _hits_target(hint, npc_name) or npc_id in hint:
            return {"kind": "npc", "env_id": npc_id, "name": npc_name}
    # ③ 场景整体
    return {"kind": "scene", "env_id": scene or "", "name": ""}


# ---------------------------------------------------------------------------
# 观察 prompt 组装：厚描述 + detail 分层（观察力门槛）+ 当前状态
# ---------------------------------------------------------------------------
def _state_summary(state, name) -> str:
    """把物品当前 state 的关键位翻译成一句状态尾巴（供观察体现"有没有变"）。"""
    if not state:
        return ""
    parts = []
    where = state.get("where") if isinstance(state.get("where"), dict) else {}
    # 兼容新 where 模型与旧 state/holder 字段：两者都代表"此刻归属"（别让观察漏了"在手中"）
    if where.get("mode") == "held" or state.get("state") == "held":
        holder = state.get("holder") or where.get("holder")
        parts.append("此刻在" + ("你" if holder == "player" else (holder or "某人")) + "手中")
    elif where.get("mode") == "placed" and where.get("position"):
        parts.append(f"此刻在坐标{where.get('position')}处")
    if where.get("orientation") == 90:
        parts.append("倒放着")
    if state.get("stained"):
        parts.append("已沾血")
    parts_dict = state.get("parts")
    if isinstance(parts_dict, dict) and isinstance(parts_dict.get("legs"), dict):
        n_removed = len(parts_dict["legs"].get("removed") or [])
        if n_removed:
            parts.append(f"缺了{n_removed}条腿")
    return "，".join(parts)


def _object_history_text(events) -> str:
    """把某物体的过程历史（world_trace 事件）译成"它经历过什么"的自然段。

    这是"物体受影响后基于变化记录的合理推理"的地基：观察"它现在怎样"时，
    把它在世界里的操作流水（谁何时搬/改/用它）翻译成一段可注入观察的事实。
    """
    if not events:
        return ""
    parts = []
    for ev in events:
        try:
            tick, actor, action_type, location, detail = ev[:5]
        except (TypeError, ValueError):
            continue
        note = detail or action_type
        parts.append(f"{actor}在{location or '某处'}{note}")
    return "；".join(parts)


def build_observation_messages(env_id, session_id, world_id="test", observer="player",
                               perception=None, snapshot_hint=None, intensity=None):
    """构造对象级观察的 messages（system=读景规则+事实；user=观察指令）。

    颗粒度：brief / normal / detail，由「观察力 perception + 行动重点 intensity」合成——
      · 观察力 < _DETAIL_PERCEPTION_THRESHOLD：细节看不真切，最多给 normal；
      · 否则颗粒度 = 本次观察强度（扫视=概述 / 普通=点到即止 / 刻意细查=详实）。

    Args:
        perception: 玩家观察力（0~100），决定 detail 层是否可见。
        intensity:  观察强度意愿（brief/normal/detail），由调用方从玩家措辞推导传入。
        snapshot_hint: 若为快照命中的返回（content），无需调 LLM；此处不用于构造（调用方短路）。
    Returns:
        list[dict] OpenAI messages；或 None（对象存在但无卡/无描述）。
    """
    card = db.get_environment_card_meta(env_id, world_id)
    if not card:
        return None
    if perception is None:
        perception = db.get_player_attrs(session_id).get("perception", 60)
    intensity = intensity or "normal"
    gran = _effective_granularity(perception, intensity)

    lines = [f"【对象】{card['name']}"]
    # 简述（description）始终可见
    if card.get("description"):
        lines.append(f"【已知信息】{card['description']}")
    else:
        lines.append("【已知信息】（无——这是一件新生成/新出现之物）")
    # detail 层：受「观察力门槛 + 颗粒度」双重约束。
    #   扫视(brief)不看细节；普通(normal)可看 detail 但 prompt 控量；刻意细查(detail)才展开。
    detail = card.get("detail")
    if gran == "brief":
        lines.append("【观察到的细节】（只一眼扫过，无暇细看——要了解细节需凑近细看）")
    elif detail and float(perception) >= _DETAIL_PERCEPTION_THRESHOLD:
        lines.append(f"【观察到的细节】{detail}")
    elif detail:
        lines.append("【观察到的细节】有些细节看不真切——你离得不够近或注意力有限。")
    else:
        lines.append("【观察到的细节】（尚未展开——若你凑近细看，或可凭已知信息推断更多）")
    # 当前状态（动态事实）
    state = card.get("state") or {}
    state_tail = _state_summary(state, card["name"])
    if state_tail:
        lines.append(f"【当前状态】{state_tail}")
    # 过程历史（变化记录）："它怎样"要结合"它经历过什么"——问题B/C 的过程推理地基
    events = db.get_object_events(session_id, env_id, world_id) if session_id else []
    hist = _object_history_text(events)
    if hist:
        lines.append(f"【它经历过什么】{hist}")
    facts = "\n".join(lines)

    system = f"{_OBSERVE_SYSTEMS[gran]}\n\n—— 以下是观察到的【事实】 ——\n{facts}"
    if gran == "brief":
        user = (f"你只是快速地扫了一眼眼前的{card['name']}。"
                "用一两句话说说你一眼看到的它是什么样子即可，不要长篇展开。")
    elif gran == "detail":
        user = (f"你刻意凑近，仔细检查眼前的{card['name']}。"
                "围绕你此刻最关心的那几点（做工/缝隙/异常痕迹等）详实地展开；"
                "只依据上述事实，不要虚构；若它经历过变化，要体现你据此看到的【它现在怎样】。")
    else:
        user = (f"你看了看眼前的{card['name']}。"
                "简要描述它——点出最显眼、或与你这次查看目的相关的两三点即可，不要面面俱到；"
                "只依据上述事实，不要虚构；若它经历过变化，要体现你据此看到的【它现在怎样】。")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# 观察主入口：快照记忆（没变返回原话；变了重新分析）
# ---------------------------------------------------------------------------
def observe(env_id, session_id, world_id="test", observer="player", perception=None, hint=""):
    """执行对象级观察，返回 {"env_id","name","content","from_snapshot","changed"}。

    - 若本局已观察过且物品 state 指纹未变 → 直接返回快照 content（原话，前后一致）；
    - 否则 → 拼 prompt → LLM 生成叙述 → 存快照 → 返回新叙述。
    """
    card = db.get_environment_card_meta(env_id, world_id)
    if not card:
        return {"env_id": env_id, "name": "", "content": "", "from_snapshot": False, "changed": False}

    # 懒生成：造物只留一句话简述；首次仔细观察时补齐 detail 层（写回持久，之后复用）——问题C(2)
    ensure_object_detail(env_id, session_id, world_id)
    card = db.get_environment_card_meta(env_id, world_id)  # 重读（detail 可能刚补上）

    if perception is None:
        perception = int(db.get_player_attrs(session_id).get("perception", 60))
    state = card.get("state") or {}
    fp = fingerprint(state)

    # 快照记忆：物品没变 → 返回原话，不调 LLM（这就是"两次观察一致"的保证）
    snap = db.get_observation_snapshot(session_id, env_id, world_id)
    if snap and snap.get("fingerprint") == fp:
        return {"env_id": env_id, "name": card["name"],
                "content": snap.get("content", ""), "from_snapshot": True, "changed": False}

    # 首次观察 / 物品已变 → 重新分析（颗粒度=观察力 + 本次观察行动重点）
    messages = build_observation_messages(env_id, session_id, world_id,
                                          observer=observer, perception=perception,
                                          intensity=_observation_intensity(hint))
    raw = ""
    if messages:
        try:
            raw = _llm().chat(messages)
        except Exception as e:  # noqa: BLE001
            logger.error("对象级观察 LLM 失败：%s", e)
            raw = ""
    content = raw.strip() or (card.get("description") or "")
    db.save_observation_snapshot(session_id, env_id, world_id, content, fp)
    return {"env_id": env_id, "name": card["name"], "content": content,
            "from_snapshot": False, "changed": True}


def ensure_object_detail(env_id, session_id, world_id="test", llm_fn=None):
    """懒生成厚描述（detail 层）：造物时只留一条简述，玩家/NPC 首次仔细观察时才补齐。

    你点名的方式（问题C-2）：不在"生成的那一刻"立即生成详细描述（避免一个 tick 里多次 API
    调用），而是造物时只记录【来源/现状】这类过程事实；等真有人要仔细观察它时才一次性生成
    detail，再持久写回 environment_card.detail（轮回内保留），之后观察直接复用
    （并配合本局快照记忆：detail 未变 → 观察叙述也复用）。

    变化过程记录（问题C-1/B 的地基）：物体每次被搬/被改/被用都进 world_trace(target=env_id)，
    生成时把这些过程记载一并注入，让"它现在怎样"有据可依、可合理推断。

    Args:
        llm_fn: 可注入的 LLM 调用（离线测试用）；缺省用模块 _llm().chat。
    Returns:
        (detail_text, generated_bool)：generated=True 表示本次新生成并写回；False=已存在/失败。
    """
    card = db.get_environment_card_meta(env_id, world_id)
    if not card:
        return "", False
    if (card.get("detail") or "").strip():
        return card["detail"], False  # 已生成过，直接复用

    if llm_fn is None:
        llm_fn = lambda msgs: _llm().chat(msgs)  # noqa: E731
    lines = [f"【事物】{card['name']}"]
    if (card.get("description") or "").strip():
        lines.append(f"【来源记录】{card['description']}")
    else:
        lines.append("【来源记录】（新生成之物，仅凭现存状态与过程记载）")
    if card.get("state"):
        lines.append(f"【现存状态】{json.dumps(card['state'], ensure_ascii=False)}")
    events = db.get_object_events(session_id, env_id, world_id) if session_id else []
    hist = _object_history_text(events)
    if hist:
        lines.append(f"【过程记载】{hist}")
    system = ("你要为文字冒险生成一件【新生成/被观察物体】的**详细观察描述**（2~3 句）。"
              "依据下方来源记录、现存状态与过程记载，描写其可触及的材质、做工、手感、气味、异常痕迹。"
              "只依据给定事实，不虚构不存在的东西；只输出描述本身，不要解释或前缀。")
    try:
        detail = llm_fn([{"role": "system", "content": system},
                         {"role": "user", "content": "\n".join(lines)}]).strip()
    except Exception as e:  # noqa: BLE001
        logger.error("对象级观察懒生成厚描述失败：%s", e)
        detail = ""
    detail = detail or (card.get("description") or "")
    if detail:
        db.update_environment_detail(env_id, detail, world_id)
        return detail, True
    return card.get("description") or "", False


def observe_npc(npc_id, session_id, world_id="test", perception=None, llm_fn=None):
    """观察一个 NPC（角色）：基于角色卡（外貌/性格/来历）+ 当前状态（生死/位置）生成叙述。

    与 observe(物品) 的区别：fact 源是 character_card + npc_status，而非 environment_card。
    同样套本局快照记忆：NPC 状态指纹未变 → 返回上次叙述（前后一致）。尸体=dead npc 也走这里。

    Returns: {"env_id","name","content","from_snapshot","changed","dead"}。
    """
    if perception is None:
        perception = int(db.get_player_attrs(session_id).get("perception", 60))
    card = db.get_npc_observe_card(npc_id)
    if card:
        name, title, appearance, personality, background = card
    else:
        name, title, appearance, personality, background = npc_id, "", "", "", ""
    status = db.get_npc_status(session_id, npc_id) or {}
    pos = db.get_npc_pos(session_id, npc_id)
    fp = fingerprint(status)

    # 快照记忆：NPC 状态没变 → 返回上次原话
    snap = db.get_observation_snapshot(session_id, f"npc:{npc_id}", world_id)
    if snap and snap.get("fingerprint") == fp:
        return {"env_id": npc_id, "name": name, "content": snap.get("content", ""),
                "from_snapshot": True, "changed": False, "dead": bool(status.get("dead"))}

    lines = [f"【TA】{name}" + (f"（{title}）" if title else "")]
    if appearance:
        try:
            a = json.loads(appearance) if isinstance(appearance, str) else appearance
        except (ValueError, TypeError):
            a = {}
        if not isinstance(a, dict):
            a = {}
        for k in ("age", "build", "face", "eyes", "aura", "voice", "habit"):
            if a.get(k):
                lines.append(f"【外貌·{k}】{a[k]}")
    if personality:
        lines.append(f"【性格】{personality}")
    if background:
        lines.append(f"【来历】{background[:120]}")
    if status.get("dead"):
        lines.append("【状态】TA已经没有气息，是一具尸体。")
    else:
        lines.append(f"【状态】此刻在{pos or '某处'}。")
    facts = "\n".join(lines)

    system = ("你是文字冒险游戏里替观察者「读人」的旁白。玩家在仔细观察一个人。\n"
              "铁律：\n- 只依据下方【事实】描述TA的外貌、气质与当下状态，用文学口吻组织，"
              "**不虚构不存在的新细节**；\n- 事实没提到的（如TA在想什么）不要猜；\n"
              "- 若TA已死，如实写出这是一具尸体；\n- 输出一段话，不要列表。")
    messages = [{"role": "system", "content": f"{system}\n\n—— 观察到的事实 ——\n{facts}"},
                {"role": "user", "content": f"请描述你看到的{name}。"}]
    raw = ""
    try:
        fn = llm_fn or (lambda msgs: _llm().chat(msgs))
        raw = fn(messages)
    except Exception as e:  # noqa: BLE001
        logger.error("观察NPC LLM失败：%s", e)
    content = (raw or "").strip() or (personality or background or name)
    db.save_observation_snapshot(session_id, f"npc:{npc_id}", world_id, content, fp)
    return {"env_id": npc_id, "name": name, "content": content,
            "from_snapshot": False, "changed": True, "dead": bool(status.get("dead"))}
