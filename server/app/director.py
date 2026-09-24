"""场景导演（Scene Director）：多人同场景同 tick → 一个全知 AI 集体裁决。

用户裁决（2026-09-07）：当同一场景里多个行动者（NPC 决策 + 玩家登记意图）打算
在同一 tick 行动时，不再逐个独立判定，而是把所有人的意图集合起来，交给一个
有全知视角、专属机制的导演 AI，裁定"这一刻这里实际发生了什么"。

为什么可行且必要：
- 此前 NPC 各自对着 tick 起点快照决策、玩家行动立即执行——两套时钟互不知情，
  出现"玩家扑空打空气"这类平行宇宙式结果；
- 导演是 closed-world 选择题 + 白名单效果：全知但不越权（效果只允许
  env_state/npc_status 两类，target 必须真实存在），非法产物回退逐人执行。

成本：仅多人同场景时 1 次调用/场景；单人场景维持确定性执行器（零成本）。
记录：导演的 prompt/输出/耗时进 recorder（npc_id="导演@场景"），逐人结果进各自 env。
"""
import json
import logging
import re
import time

from . import db
from . import recorder

logger = logging.getLogger(__name__)


def _lazy_llm():
    from .llm import DeepSeekClient
    return DeepSeekClient()


def build_director_messages(participants: list, world_id: str, scene: str,
                            session_id: str = "", arrival_hint: bool = False) -> list:
    """场景导演 prompt：所有参与者的意图（各带有限视角）+ 全知世界事实。

    arrival_hint（09-10 用户定版）：True = "玩家本 tick 正走进本场景"——在本条提示词末尾
    追加一个【到达视角】的额外输出要求（arrival_view）。由【目的地房间自己】的这次调用产出，
    只喂它自己的场景事实，不跨房间喂信息（尊重"每房 LLM 只管自己"的隔离）。

    用户设计（09-08）：让导演【按真实 id 对准每个人】，不像旧版只给参与者原文意图
    （玩家说"那个男的"，导演得自己猜是谁）。这里为每个参与者补上「ID/名字/性别」
    映射——导演的 outcomes.actor 必须用这些真实 id，effects.target 也必须用这些 id。
    这样玩家口语指代"那个男的" → 导演对照名单后填 test_man，游戏按 id 落后果。

    09-10（用户拍板）：所有存活 NPC 每 tick 都产出一个 decision（world._decide_targets
    live 模式返回【全部非死亡 NPC】），因此同场景"在场但无动机"的旁观者【实际不存在】——
    每个在场者都是参与者。故不再引入 bystanders（在场且未行动）概念；处于对话中的玩家也
    作为在场参与者补入（见 resolve_scenes），导演全知视野即=全部参与者。
    """
    from . import world_pack
    facts = []
    for env_id, _kind, name, _desc, state_raw, _p in db.get_environment_cards(world_id):
        if env_id != scene:
            continue
        facts.append(f"场景 {scene}（{name}）：{state_raw}")
    # 09-10（用户二次拍板·口径统一）：移动统一按【移动前】的场景参与推演与场景描述——
    #    不再把"玩家目的地的感知快照"塞进导演 facts（此前为写"你向房间二走去，那里…"）。
    #    玩家到达新场景的到达画面由【目标场景自己的第二人称描述】（"你看到…"）承接，
    #    与本格"谁在场、各自如何行动"的推演互不冲突。
    poses = []
    for p in participants:
        if p.get("pos"):
            poses.append(f"{p['actor']} 在 {p['pos']}")

    # 角色名单（ID=名字，带性别线索）：让导演对照"玩家说的那个人"定位到真实 id。
    # 参与者都列进名单，各标身份。test_man/test_woman 等从 world_pack.npc_id_to_name 拿显示名。
    roster = []
    npc_cache = {}
    for p in participants:
        pid = str(p.get("actor", ""))
        if pid == "player":
            roster.append("player（玩家）")
            continue
        nm = npc_cache.get(pid)
        if nm is None:
            nm = world_pack.npc_id_to_name(pid, world_id) or pid
            npc_cache[pid] = nm
        roster.append(f"{pid}（{nm}）")
    roster_str = "、".join(roster) if roster else "（无）"

    # 09-10 双轨 narrative：导演始终【全知】输入与推理。
    # 注意：narrative 是【全知真相】（进世界真相/调试面板，完整无保留）；
    #       player_view 是把全知结论【包装成玩家有限视角】给玩家看的叙述（第二人称，只讲玩家
    #       能亲眼看到/亲身经历的事，玩家不可感知则为空串）——两者不可混淆。给玩家的是 player_view，
    #       绝不把全知 narrative 直接播给玩家（会泄露玩家看不见的事实、破坏感知边界）。
    system = (
        "你是叙事引擎的「场景导演」。多个行动者在同一场景、同一时刻各自打算行动——"
        "你【全知视角】裁定这一刻【实际发生什么】：谁先谁后、谁成谁败、会不会相遇冲突。\n"
        f"本场景在场者（请用这些【真实 id】对准人，别自己编）：{roster_str}。\n"
        "只输出一个 JSON 对象：\n"
        '{"narrative":"这一刻的【全知】场景真相叙述（两三句，供世界真相落库与调试面板对照，无需替玩家隐藏信息）",'
        '"player_view":"把上面的全知结论【包装成玩家有限视角】给玩家看的叙述：用第二人称「你」，'
        '只讲玩家此刻能亲眼看到、亲身经历的事；玩家不在场/看不见/未察觉的一律不写；'
        '若无玩家可感知之事则为空串；若玩家正在对话/交谈，用「你回过神来，注意到…」交代'
        '对话结束后玩家发现或回味的东西。",'
        '"outcomes":[{"actor":"行动者id","result":"他的行动实际结果（一句话）"}],'
        '"effects":[{"set":"env_state 或 npc_status","target":"对象id","key":"状态键","value":"新值"}]}\n'
        "铁律：① effects 只允许 set=env_state/npc_status，target 必须是上面在场者或场景的真实 id；"
        "没有状态变化就给空数组。"
        "② outcomes 必须覆盖每一个参与者 actor。"
        "③ 玩家用口语指代某人（如'那个男的'）时，请对照名单确定真实 id 填进 actor/target。"
        "④ 若某人被'杀/刺中要害/击中致命'，务必在 effects 里补一条 "
        '{"set":"npc_status","target":"该真实id","key":"dead","value":"true"}——'
        "这样他才会变成尸体、可被搜尸，否则世界状态与叙述不符。"
        "⑤ narrative 与 player_view 是两回事：narrative 全知无保留（供世界真相/调试）；"
        "player_view 只讲玩家视野内的事（给玩家看，不得透露玩家看不见的——如玩家不在场房间发生的事、"
        "被拿走的东西、别人藏在暗处的动作）。玩家不在场/无可见之事时 player_view 输出空串。"
    )
    # 09-10 arrival_view（用户定版）：玩家本 tick 正【走进本场景】。到达画面由【目的地房间自己的】
    # 这次调用产出（只喂它自己的快照，不跨房间）。这里按需追加一个独立输出字段要求；非到达场景
    # 不追加，保证常规调用的提示词长度不变（提示词预算敏感）。
    if arrival_hint:
        system += (
            "\n【到达视角·额外要求】玩家本 tick 正从别处走向本场景、即将抵达。"
            '请在 JSON 中【额外】输出字段 "arrival_view"：以第二人称「你」描摹玩家踏进本场景那一刻'
            "看到的画面（只依据【真实事实】与在场者，不写玩家看不见的东西），一到两句；"
            "若本场景此刻确无可看之处则给空串。arrival_view 不参与本格谁先谁后的裁定，"
            "只是玩家进门时看到的画面；本场景若无任何行动者，请让 outcomes/effects 保持空数组。"
        )
    # 参与者神情（P0-a，观察他人情绪）：导演全知视角也补每人"此刻神情"，
    # 从心智热态派生（read_emotion_snapshot），供 narrative 体现神情互动；平静不注入。
    def _participant_mood(p_actor, p_kind, session_id, world_id):
        try:
            if p_kind == "player":
                return ""  # 玩家情绪未建模，不注入
            from . import mind_engine
            w = mind_engine.read_emotion_snapshot(session_id, p_actor).get("word", "")
            return f"；神情={w}" if w and w != "平静" else ""
        except Exception:  # noqa: BLE001
            return ""

    plist = "\n".join(
        f"- {p['actor']}（{'玩家' if p['kind'] == 'player' else 'NPC'}）："
        f"意图={p.get('intent', '')}；行动={p.get('desc', '')}"
        f"{_participant_mood(p.get('actor'), p.get('kind'), session_id, world_id)}"
        for p in participants)
    # 09-10 arrival_view：目的地【空房】被按需纳入导演时参与者为空——明确告知"无行动者"，
    # 避免"参与者"段留白让 LLM 自由发挥（空房应只产出 arrival_view，不该编造行动）。
    user = (f"【场景】{scene}\n【真实事实】\n" + ("\n".join(facts + poses) or "（无）")
            + "\n\n【参与者与意图】\n" + (plist or "（无——本场景此刻没有行动者）"))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_director(raw: str) -> dict or None:
    """解析导演产物；非法（无 JSON、且 outcomes 与 arrival_view 均为空）→ None（回退逐人执行）。"""
    m = re.search(r"\{.*\}", (raw or "").strip(), re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    effects = []
    for eff in data.get("effects") or []:
        if isinstance(eff, dict) and eff.get("set") in ("env_state", "npc_status") and eff.get("target"):
            effects.append({"set": eff["set"], "target": str(eff["target"]),
                            "key": str(eff.get("key", "")), "value": eff.get("value")})
    outcomes = [o for o in (data.get("outcomes") or []) if isinstance(o, dict) and o.get("actor")]
    arrival_view = str(data.get("arrival_view", "")).strip()
    # 09-10 arrival_view：目的地【空房】被按需纳入导演时本格没有行动者 → outcomes 天然为空，
    # 但 arrival_view 有效。故放宽判据：outcomes 或 arrival_view 任一非空即视为有效产物。
    if not outcomes and not arrival_view:
        return None
    # 09-10 双轨：player_view = 玩家视角包装叙述（给玩家看，可能为空串=玩家不可感知）；
    # narrative = 全知真相（世界/调试）；arrival_view = 玩家踏进本场景的到达画面（独立落点）。
    return {"narrative": str(data.get("narrative", "")),
            "player_view": str(data.get("player_view", "")).strip(),
            "arrival_view": arrival_view,
            "outcomes": outcomes, "effects": effects}


def _known_target(target: str, scene: str, world_id: str, present_npcs: list) -> bool:
    """白名单：导演 effects 的 target 必须是真实可寻址对象（NPC/场景实体/当前场景），
    虚构 id（LLM 幻觉）一律拒绝落库。"""
    from . import world_pack
    t = str(target or "")
    if not t:
        return False
    if t == scene:                       # 当前场景本身（如 room_2 的 blood_stain）
        return True
    if t in present_npcs:                # 参与者的真实 npc_id
        return True
    if world_pack.npc_id_to_name(t, world_id):   # 任一已知 NPC（含不在场的，如尸体）
        return True
    # 场景实体（环境卡里的 env_id：房间+物品）
    try:
        for env_id, *_ in db.get_environment_cards(world_id):
            if env_id == t:
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def apply_director(session_id: str, tick: int, scene: str, director: dict, world_id: str,
                   present_npcs: list = None) -> list:
    """落导演效果（白名单已过）+ 叙述痕迹。返回实际落库的 effects。

    present_npcs：本场景参与者（含玩家），用于把 target 限制在真实角色/场景/实体上，
    拦截导演虚构的 id（防 LLM 幻觉乱写状态）。缺省时用该场景在场 NPC 兜底。
    """
    from . import world_pack
    if present_npcs is None:
        present_npcs = []
        for n in db.get_all_npc_ids(world_id):
            if (db.get_npc_pos(session_id, n) or "") == scene:
                present_npcs.append(n)
    applied = []
    for eff in director.get("effects", []):
        try:
            if not _known_target(eff["target"], scene, world_id, present_npcs):
                logger.warning("导演 target 非真实对象，跳过: %r", eff.get("target"))
                continue
            if eff["set"] == "env_state":
                db.patch_environment_state(eff["target"], eff["key"], eff["value"], world_id)
            else:
                # 布尔标准化：LLM 常把 dead 输出成字符串 "true"/"false"（JSON 值），
                # 与 environment.execute_player_action 落库的 Python True/False 不一致。
                # 统一转成 bool，保证 get_npc_status(...).get("dead") 恒为真布尔——
                # 避免各处 bool()/is True 判断出现 "true" != True 的隐性错位。
                val = eff["value"]
                if eff["key"] == "dead":
                    val = str(val).strip().lower() in ("true", "1", "yes", "是", "死亡")
                db.set_npc_status(session_id, eff["target"], eff["key"], val)
            applied.append(eff)
        except Exception:  # noqa: BLE001  单条失败跳过
            continue
    # 09-10 双轨：给玩家 trace 的是【玩家视角包装】player_view，而非全知 narrative——
    # 玩家 trace 直接落到右框当旁白（去破墙、不加"导演裁定"前缀），因此绝不能用全知 narrative
    # （会泄露玩家看不见的事实）。player_view 为空（玩家不可感知/无可见之事）则【不落这条 trace】，
    # 玩家自然收不到通知。全知 narrative 仍供世界真相/调试面板（recorder.note_prompt 记录）。
    player_view = str(director.get("player_view", "")).strip()
    if player_view:
        # player_view 落在【聚合场景】（=该角色参与推演的那一格）。09-10 用户拍板：移动统一
        # 按【移动前】的场景聚合，故移动者（含玩家）的视角叙事也落在出发地；玩家到达新场景
        # 的画面由目标场景自己的第二人称描述（"你看到…"）承接。
        db.add_world_trace(session_id, tick, "fate", "director", "", scene, player_view)
    return applied


def _aggregate_scene(action: dict, cur_pos: str) -> str:
    """该决策进入导演时的聚合场景——即"它参与推演的那一格"。

    09-10 用户拍板（口径统一）：**移动统一按【移动前】的场景（=cur_pos，出发地）聚合**。
    原因：move 的目的地存在 action.target；action.location 是"发生地"，可能被 LLM 填成
    目的地，不能当聚合键。非移动动作仍优先用 action.location（发生地），空则回退当前位置。
    → NPC 与玩家（玩家按 db.get_player_scene()=当前位置聚合）口径一致：谁都按"移动前"的
    格子参与推演，不预知自己或别人下一步要去哪一格。
    """
    a = action or {}
    cur = str(cur_pos or "")
    if str(a.get("type", "") or "") == "move":
        return cur or "?"
    return str(a.get("location", "") or cur or "?")


def _execute_player_intent_steps(session_id, tick, player_intent, world_id):
    """把一条【玩家意图登记项】按说话先后顺序逐步落地（机械动作走确定性执行器）。

    多意图顺序语义与 `context_builder._run_mutating_now` 完全一致：位置先落地，
    后续动作在【新位置】的感知下执行（"去房间三打他"= 先到 room_3 再动手）。

    ⚠️ 09-10 修"玩家的行动只被叙述、没被落地"（用户现场实证）：
      · 导演分支此前只补执行 NPC 的机械动作（原 `if p["kind"] != "npc": continue` 把玩家整条跳过），
        玩家的 move/pick/… 在导演分支里【没有任何人执行】；
      · 而下方"非冲突分支"因为 `conflicted` 恒等于 `by_scene`（见 :354 推导式只保留非空场景）
        永远进不去 → 玩家执行器成了死代码。
      两者叠加的结果：只要玩家身边还有别的 NPC（常态），玩家输入的"去房间二"就只被导演
      写成文学叙述（"你踏进房间二…"），`player_scene` 从没变过 —— 叙述与世界状态不一致。
      实证：会话 sess_20260910_151303_4ebb8a 的 tick4 有 room_3 出发 + room_2 到达的导演痕迹，
      但 player_scene 仍是 room_3、且没有 player/move 痕迹。

    Args:
        player_intent: 意图池的一条登记项 {"intents":[intent...], "intent":.., "text", "scene"}。
    Returns:
        每步的执行结果列表（最后一项 = 最终落点；旧消费方读 result 即取它）。
    """
    from . import environment as env_mod
    from . import intent as intent_mod
    pi = player_intent or {}
    steps = pi.get("intents")
    if not isinstance(steps, list) or not steps:
        steps = [pi.get("intent") or {}]
    results = []
    for idict in steps:
        idict = idict or {}
        it = intent_mod.Intent(domain=str(idict.get("domain", "spatial")),
                               side_effect=str(idict.get("side_effect", "mutating")),
                               target=idict.get("target") or {},
                               verb=str(idict.get("verb", "")),
                               spatial=idict.get("spatial") or {})
        try:
            results.append(env_mod.execute_player_action(
                it, session_id, tick, world_id, player="player"))
        except Exception:  # noqa: BLE001  单步失败不阻断整条链（与 _run_mutating_now 同口径）
            logger.warning("玩家意图步落地失败：%s", idict, exc_info=True)
    return results


def resolve_scenes(session_id: str, tick: int, decisions: list, player_intents: list = None,
                   world_id: str = "test", llm_client=None, visible_to=None) -> list:
    """live 模式统一结算：按场景聚合 → 多人同场景进导演 / 单人确定性执行。

    Args:
        decisions: NPC decision dict 列表（并行决策产物）。
        player_intents: 意图池排出的玩家意图 [{"intent":dict,"text","scene"}]。
        llm_client: LLM 客户端（冲突场景才调用；多冲突场景 chat_many 并行）。
    Returns:
        events：[{scene, participants, type, ...}]（含导演叙述与逐人结果，供 recorder/前端）。
    """
    # env_mod / intent_mod 的用法已收口到 _execute_player_intent_steps（玩家机械动作落地）
    from . import fate as fate_mod   # 复用 NPC 决策落库（apply_decision 含反应仲裁）

    events = []

    def _participant(actor, kind, decision=None, player_intent=None):
        if decision is not None:
            a = decision.get("action") or {}
            scene = _aggregate_scene(a, db.get_npc_pos(session_id, actor))
            return {"actor": actor, "kind": kind, "decision": decision, "scene": scene,
                    "pos": scene,
                    "desc": f"{a.get('type', '')} → {a.get('target', '')}（{a.get('detail', '')}）",
                    "intent": str(decision.get("intent", ""))}
        d = player_intent or {}
        # 多意图（09-10 用户拍板）：一条登记项里可能有多步动作，逐步列出并标出先后，
        # 让导演知道"玩家这一步先拿了椅子、再去房间三、最后动了手"，而不是只看到最后一步。
        steps = d.get("intents") if isinstance(d.get("intents"), list) else None
        if not steps:
            steps = [d.get("intent") or {}]
        descs = []
        for _s in steps:
            _s = _s or {}
            # 玩家行动若是针对某人，把已消解的 target.id 也带给导演（若 id 空则给 hint 原文），
            # 便于导演"对照名单对准人"——玩家口语指代'那个男的'时，若规则层已解成 test_man 直接带上。
            _tg = _s.get("target") or {}
            _tinfo = _tg.get("id") or _tg.get("hint") or ""
            descs.append(f"{_s.get('verb', '')} → {_tinfo}")
        pscene = db.get_player_scene(session_id) or "?"
        return {"actor": "player", "kind": "player", "player_intent": d, "scene": pscene,
                "pos": pscene,
                "desc": "，然后".join(descs) if len(descs) > 1 else (descs[0] if descs else ""),
                "intent": str(d.get("text", ""))}

    parts = []
    for d in decisions or []:
        parts.append(_participant(str(d.get("agent", "?")), "npc", decision=d))
    for pi in player_intents or []:
        parts.append(_participant("player", "player", player_intent=pi))

    # 09-10 核心断层根治(v2)：玩家作为【在场者】始终注入其所在场景——否则导演把玩家当透明。
    # 覆盖两种导演"看不见玩家"的情况：
    #   a) 玩家【主动发起对话】(_talking_pair 命中)：desc="正在与XX交谈"；
    #   b) 玩家仅【在现场】(略过/移动后站在某处、无 player_intent、无对话)：test_robot 等找玩家
    #      搭话时，导演会把"玩家"当成不存在 → 裁定"玩家不在场，转向别人"(上图)。
    # 注入后导演 roster/poses 有"player(玩家)"/"玩家在房间一"，别人 speak 玩家能对准。
    # 玩家无 player_intent 且不在对话 → 记为"在场、暂无主动行动"(导演知道他在即可，不勉强编排)。
    # 玩家已有 player_intent 动作(intent 非空)时不覆盖。玩家场景为空(开局未知)则不注入。
    try:
        pscene = db.get_player_scene(session_id)
        if pscene:
            existing = next((p for p in parts if str(p.get("actor", "")) == "player"), None)
            from . import conversation as conv_mod
            pair = conv_mod._talking_pair(session_id)  # (talk_npc, player) or None
            if pair:
                talk_npc, _player = pair
                desc, intent = f"正在与{talk_npc}交谈", "对话中"
            else:
                # 09-10：玩家略过时间/未动/无意图 → 等价于"wait"（原地等待、时间流逝），
                # 与 NPC 的 wait 意图对等，导演才会把玩家当作"正在守候/等待"而非游离的"观察者"。
                desc, intent = "在原地等待,时间缓缓流逝", "wait"
            if existing is not None:
                existing["scene"] = existing.get("scene") or pscene
                existing["pos"] = existing.get("pos") or pscene
                if not existing.get("intent"):  # 无明确动作才补"在场"描述(有动作则不覆盖)
                    existing["desc"] = desc
                    existing["intent"] = intent
            else:
                parts.append({"actor": "player", "kind": "player", "scene": pscene,
                              "pos": pscene, "player_intent": None,
                              "desc": desc, "intent": intent})
    except Exception:  # noqa: BLE001  玩家在场者补全失败不阻断导演
        pass

    # 按行动发生地聚合
    by_scene = {}
    for p in parts:
        by_scene.setdefault(p["scene"], []).append(p)
    # 09-10 全场景导演（用户拍板：每个 tick 所有场景都走导演，不只是玩家所在/冲突场景）——
    # 游戏由 AI 驱动 NPC 涌现式叙事，因此每个 tick 里所有有参与者的场景（任何 NPC 意图或玩家
    # 意图）都进导演 AI：每个都有本轮意图清单，导演逐个场景汇总分析"这一格实际发生什么"。
    # 玩家的【所在场景】输出 player_view（玩家视角，经 /world/updates 给前端）；其余场景是纯
    # 背景模拟——narrative 进世界真相/调试（左框），player_view 强制为空、不落玩家 trace。
    # ⚠️ 空房间（无任何行动者）没有意图清单，不导演（省 LLM）；纯 NPC 单人场景不再走确定性执行器。
    conflicted = {sc: ps for sc, ps in by_scene.items() if ps}

    # 09-10 arrival_view（用户定版）：本 tick 玩家若发起「移动」，把其【目的地场景】按需纳入导演
    # 集合——哪怕它是空房（空房无行动者、本不导演）。理由：到达画面必须由【目的地房间自己的】LLM
    # 用它自己的快照产出，不跨房间喂信息（尊重"每房 LLM 只管自己"的隔离）。目的地有 NPC 时本就在
    # conflicted 里，这里只是多给它一个「玩家即将抵达」标记；空房则净增这一次调用（仅移动这一刻）。
    arrival_scene = ""
    for _pi in (player_intents or []):
        # 多意图：移动可能只是整句里的一步（"拿上椅子去房间三打他"），所以要把每一步都扫一遍，
        # 不能只看第一条——否则多意图玩家的到达画面会静默丢失。
        _steps = _pi.get("intents")
        if not isinstance(_steps, list) or not _steps:
            _steps = [_pi.get("intent") or {}]
        for _idict in _steps:
            _idict = _idict or {}
            _sp = _idict.get("spatial") or {}
            # 两种移动写法都认：地图点击写 verb="move"+target.id；自由输入写 op="move_self"。
            _is_move = (str(_idict.get("verb", "") or "") == "move"
                        or str(_sp.get("op", "") or "") == "move_self")
            if not _is_move:
                continue
            _dest = str(((_idict.get("target") or {}).get("id")) or "").strip() \
                or str(_sp.get("dest_scene") or "").strip()
            if _dest:
                arrival_scene = _dest
                break
        if arrival_scene:
            break
    scene_parts = dict(conflicted)
    if arrival_scene and arrival_scene not in scene_parts:
        scene_parts[arrival_scene] = []   # 空房按需纳入：无参与者，只产出 arrival_view

    # 冲突场景：导演 AI（多场景并行 chat_many）
    if scene_parts:
        # 第二齿轮（09-08 用户拍板）：只有"多人相互影响判定"（同场景 ≥2 人、各自的行动都已从
        # AI 返回）才写这条提示——语义是"所有角色都做好行动，开始相互影响判定"。玩家独处
        # （单人场景）不算，不写（它是玩家一个人的回合，无"相互影响"可言）。
        # 前端据此渲染第二齿轮。⚠️ 这是固定的【二级等待词】，不是文学旁白：只作为等待状态
        # 提示给玩家，绝不进入任何 AI 提示词（narrate_scene 的 snapshot 已过滤 actor==fate，
        # 故不会把它文学化）。走 world trace，结算完成后经 /world/updates 增量回前端。
        # 词固定为"命运的齿轮再次开始转动"（用户拍板），勿文学化。
        for sc in sorted(conflicted):
            if len(conflicted[sc]) >= 2:
                db.add_world_trace(session_id, tick, "fate", "phase", "", sc,
                                   "命运的齿轮再次开始转动")
        client = llm_client or _lazy_llm()
        msgs, scene_keys = [], []
        for sc, ps in sorted(scene_parts.items()):
            for p in ps:
                p["pos"] = p.get("pos") or db.get_npc_pos(session_id, p["actor"]) or \
                    (db.get_player_scene(session_id) if p["actor"] == "player" else "")
            msgs.append(build_director_messages(
                ps, world_id, sc, session_id=session_id, arrival_hint=(sc == arrival_scene)))
            scene_keys.append(sc)
        t0 = time.perf_counter()
        if len(msgs) == 1:
            try:
                results = [(True, client.chat(msgs[0]))]
            except Exception:  # noqa: BLE001
                results = [(False, "")]
        else:
            results = client.chat_many(msgs)
        batch_ms = (time.perf_counter() - t0) * 1000.0
        for sc, msg, (ok, raw) in zip(scene_keys, msgs, results):
            ps = scene_parts.get(sc, [])
            director = parse_director(raw) if ok else None
            if not director:
                # 空房（按需纳入的到达场景）本就没有可回退的行动者，记 warning 无意义。
                if ps:
                    logger.warning("场景 %s 导演产物非法/失败，回退逐人执行", sc)
                    recorder.note_error(session_id, f"导演@{sc}", "director",
                                        "调用失败或产物非法", messages=msg)
                    for p in ps:
                        if p["kind"] == "npc" and p.get("decision"):
                            events.append({"scene": sc, "traces": fate_mod.arbitrate(
                                session_id, tick, [p["decision"]], visible_to=visible_to,
                                llm_client=llm_client, world_id=world_id)})
                        elif p["kind"] == "player" and p.get("player_intent"):
                            # 09-10 与 NPC 同口径：导演【产物失败/AI 超时】时玩家那一步骤也必须落地。
                            # 否则世界状态会取决于"这次 LLM 是否成功"（不可接受的失败耦合）——
                            # 导演失败时 NPC 照常执行，玩家却不执行，是最隐蔽的一类不一致。
                            _execute_player_intent_steps(session_id, tick,
                                                         p["player_intent"], world_id)
                continue
            # 玩家不在场的场景 = 纯背景模拟：导演的全知 narrative 进世界真相/调试（左框），
            # 但 player_view 强制置空——玩家看不到，绝不落玩家 trace（否则跨房间泄露背景真相）。
            if not any(p.get("kind") == "player" for p in ps):
                director["player_view"] = ""
            applied = apply_director(session_id, tick, sc, director, world_id,
                                     present_npcs=[p["actor"] for p in ps])
            # 09-10 arrival_view：移动到达画面【独立字段 + 独立落点】——落 location=sc（=玩家将抵达
            # 的那个场景）。与 player_view 解耦：player_view 有"本格参与者含 player 才输出、否则强制
            # 置空"的逻辑（上一段），而目的地那格按推演口径不含 player，故到达画面必须走独立通道。
            arrival_view = str(director.get("arrival_view", "")).strip()
            if arrival_view and sc == arrival_scene:
                db.add_world_trace(session_id, tick, "fate", "director", "", sc, arrival_view)
            # 09-09 用户拍板（问题1：位置没变）：同场景冲突时 NPC 的【机械动作】
            # （move/use_item/give_item/interact）此前只写成导演 narrative 的"结果"（一句
            # 文学描述），从不真正落库 → 位置不变、痕迹也不进该 NPC 的 own_traces。
            # 这里在导演裁决后，让这些机械动作仍走确定性执行器（fate.arbitrate →
            # apply_decision → execute_npc_action），真实落位置+痕迹+受阻记忆。
            # 与导演 narrative 不冲突：导演描述"谁先谁后、成或败"，执行器保证世界状态
            # 与之一致（move 成功则位置变，move 被堵则记"路不通"）。仅机械动作如此，
            # 纯语义动作（speak/interact 叙事）仍以导演 narrative 为准。
            try:
                for p in ps:
                    # ⚠️ 09-10 补玩家：玩家在这一支的机械动作此前被整条跳过（原 `!= "npc": continue`），
                    # 导致"输入去房间二"只被导演叙述、player_scene 从不落地（详见 helper 注释）。
                    # 与 NPC 同口径：导演写"谁先谁后、成或败"，执行器随后保证世界状态与之一致。
                    if p["kind"] == "player":
                        if p.get("player_intent"):
                            _execute_player_intent_steps(session_id, tick,
                                                         p["player_intent"], world_id)
                        continue
                    if p["kind"] != "npc":
                        continue
                    _act = (p.get("decision") or {}).get("action") or {}
                    if str(_act.get("type", "")) not in ("move", "use_item", "give_item", "interact"):
                        continue
                    fate_mod.arbitrate(session_id, tick, [p["decision"]],
                                       visible_to=visible_to, llm_client=llm_client,
                                       world_id=world_id)
            except Exception:  # noqa: BLE001  导演下机械执行失败不阻断（narrative 兜底）
                logger.warning("导演@%s 机械动作落库失败", sc, exc_info=True)
            recorder.note_prompt(session_id, f"导演@{sc}", msg,
                                 batch_ms / max(1, len(scene_keys)),
                                 {"narrative": director.get("narrative", ""),
                                  "outcomes": director.get("outcomes", []),
                                  "effects": applied})
            for p in ps:
                res = next((o.get("result") for o in director.get("outcomes", [])
                            if str(o.get("actor", "")) == p["actor"]), "")
                recorder.note_env(session_id, p["actor"],
                                  {"type": "导演裁定", "target": sc,
                                   "message": res or director.get("narrative", ""),
                                   "outcome": "ok", "changed": bool(applied)})
            events.append({"scene": sc, "type": "director",
                           "narrative": director.get("narrative", ""),
                           "participants": [p["actor"] for p in ps]})

    # 非冲突场景：确定性执行（NPC 决策 / 玩家意图走各自执行器）
    # ⚠️ 09-10 说明：`by_scene` 的每个 key 都至少挂着一个参与者（`by_scene.setdefault(sc, []).append(p)`），
    # 而 `conflicted` 就是"只保留非空场景"的同一份字典 → 两者恒等，本循环实际【走不到】。
    # 玩家/单人场景的机械执行因此全部落在上面的导演分支（已补玩家，见那里的注释）。
    # 这里保留为语义兜底：若将来把 `conflicted` 收窄成"真冲突（≥2 人）"，本支会自动接管单人场景。
    for sc, ps in by_scene.items():
        if sc in conflicted:
            continue
        for p in ps:
            if p["kind"] == "npc" and p.get("decision"):
                events.append({"scene": sc, "traces": fate_mod.arbitrate(
                    session_id, tick, [p["decision"]], visible_to=visible_to,
                    llm_client=llm_client, world_id=world_id)})
            elif p["kind"] == "player" and p.get("player_intent"):
                # 多意图（09-10）：按玩家说话的先后顺序逐步执行（复用同一 helper，零新逻辑）。
                results = _execute_player_intent_steps(session_id, tick,
                                                       p["player_intent"], world_id)
                # result 保留=最后一步（旧消费方读它），results 给出全过程（新的多意图视角）
                events.append({"scene": sc, "type": "player_solo",
                               "result": results[-1] if results else {},
                               "results": results})
    return events


# =============================================================================
# T3 导演对话分析（v0.4）：NPC↔NPC 交谈成立后，导演全知分析"讨论内容 + 对彼此影响"，
# 产出 discussion/cognition/relation/emotion，经 apply_director_outcome 收口落库。
# =============================================================================
def analyze_dialogue(pair: dict, session_id: str, world_id: str = "test", llm_client=None) -> dict or None:
    """对一对已成立的交谈做导演分析（每对交谈 1 次 LLM）。

    Args:
        pair: resolve_conversations 的 started_convs 项
              {initiator,target,scene,tick,speech,intent}。
        llm_client: LLM 客户端（缺省懒加载）。
    Returns:
        dict or None：parse 后的 {discussion, cognition, relation, emotion}；
        非法产物返回 None（调用方降级为 apply_npc_dialogue 规则版记忆）。
    """
    from . import world_pack
    initiator = str(pair.get("initiator", "") or "")
    target = str(pair.get("target", "") or "")
    if not initiator or not target or initiator == target:
        return None
    scene = str(pair.get("scene", "") or "")
    topic = str(pair.get("speech", "") or "").strip() or str(pair.get("intent", "") or "") or "交谈"
    i_name = world_pack.npc_id_to_name(initiator, world_id) or initiator
    t_name = world_pack.npc_id_to_name(target, world_id) or target

    system = world_pack.prompt(world_id, "director_dialogue_analysis") or _DEFAULT_DIALOGUE_ANALYSIS_SYSTEM
    user = (
        f"【场景】{scene}\n"
        f"【交谈双方】{initiator}（{i_name}）与 {target}（{t_name}）\n"
        f"【发起方】{initiator}（{i_name}）说了：{topic}\n"
        "请输出分析 JSON。"
    )
    client = llm_client or _lazy_llm()
    try:
        raw = client.chat([{"role": "system", "content": system},
                           {"role": "user", "content": user}])
    except Exception:  # noqa: BLE001
        logger.warning("导演对话分析失败：%s↔%s", initiator, target)
        return None
    return _parse_dialogue_analysis(raw)


_DEFAULT_DIALOGUE_ANALYSIS_SYSTEM = (
    "你是叙事引擎的「对话导演」。两名 NPC 刚刚在交谈，请以全知视角分析这段对话产生了什么影响。"
    '只输出一个 JSON 对象，不要其它文字：{"discussion":"两人讨论了什么","cognition":{"<actor_id>":'
    '"认知变化句"},"relation":{"<actor_id>":0},"emotion":{"<actor_id>":"中文情绪词"}}。'
    "actor_id 必须用真实 id；relation 是不变给 0；emotion 给中文词即可。"
)


def _parse_dialogue_analysis(raw: str) -> dict or None:
    """解析导演对话分析产物；非法（无 JSON）→ None。"""
    m = re.search(r"\{.*\}", (raw or "").strip(), re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "discussion": str(data.get("discussion", "")),
        "cognition": data.get("cognition") if isinstance(data.get("cognition"), dict) else {},
        "relation": data.get("relation") if isinstance(data.get("relation"), dict) else {},
        "emotion": data.get("emotion") if isinstance(data.get("emotion"), dict) else {},
    }


def apply_director_outcome(pair: dict, outcome: dict, session_id: str, tick: int) -> dict:
    """收口函数（P0-a/P0-b）：把导演分析结果按单一事实源落库。

    - cognition → 记忆表 write_memory(event/impression + importance + related_entity=对方)，
      绝不写 working_memory 或另起痕迹（P0-b 认知只进记忆表）；
    - relation → conversation.apply_director_relation（有符号净增，0 跳过）；
    - emotion → 中文词经 mental.word_to_pad 反查成 PAD → save_mental_state(热态)；
      仅当词非"平静/空"才有意义，未命中回退中性。
    Returns: {written: {...}} 便于调试/assert。
    """
    from . import mental as mental_mod
    from . import memory as memory_mod
    from . import conversation as conv_mod
    initiator = str(pair.get("initiator", "") or "")
    target = str(pair.get("target", "") or "")
    outcome = outcome or {}
    written = {"cognition": 0, "relation": 0, "emotion": 0}

    # ① cognition → 记忆表（双方各一条：对对方的认知变化）
    cognition = outcome.get("cognition") or {}
    for who, other in ((initiator, target), (target, initiator)):
        note = str(cognition.get(who, "") or "").strip()
        if not note:
            continue
        try:
            memory_mod.write_memory(
                who, "impression", f"这次交谈后我对 {other} 的印象：{note}",
                importance=6, summary=note[:30], related_entity=other, session_id=session_id)
            written["cognition"] += 1
        except Exception:  # noqa: BLE001
            pass

    # ② relation → 收口（有符号净增，0 自动跳过）
    relation = outcome.get("relation") or {}
    try:
        conv_mod.apply_director_relation(session_id, initiator, target, relation)
        written["relation"] = 1 if any(float(v) for v in relation.values()) else 0
    except Exception:  # noqa: BLE001
        pass

    # ③ emotion → 中文词反查 PAD → 热态（单一事实源）
    emotion = outcome.get("emotion") or {}
    for who in (initiator, target):
        word = str(emotion.get(who, "") or "").strip()
        if not word or word == "平静":
            continue
        try:
            pad = mental_mod.word_to_pad(word)
            # 与 mind_engine 一致：热态存 PAD，emotion_word/强度同步刷新（单一事实源）
            db.save_mental_state(session_id, who, {
                "emotion": {"valence": pad["valence"], "arousal": pad["arousal"],
                            "dominance": pad["dominance"]},
                "emotion_word": pad["word"],
                "emotion_intensity": pad["intensity"],
            }, tick=tick)
            written["emotion"] += 1
        except Exception:  # noqa: BLE001
            pass
    return written
