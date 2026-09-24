"""对话仲裁层：在导演结算之前，扫描本 tick 的 speak 意图 → 生成对话邀请 → 同意/拒绝 → 递推。

对话系统 v0.4 的插入点：world._step_world_tick_inner 的 ②.5（排出玩家意图后）调用
resolve_conversations，把它返回的 final_decisions 再交给 director.resolve_scenes。

职责边界（关键——对它越界就是破坏）：
- 只裁决"某人想跟另一人说话"这件事（同意/拒绝/递推）；
- **零侵入**：没有任何对话邀请时，final_decisions 与传入 decisions 完全一致（短路返回），
  导演逻辑原样跑；
- "同意后进入多轮对话交互"（turn/end/提示词）属会话状态机（里程碑 D），本模块只负责
  "开个头"（db.start_conversation）并给 actor 打上 in_conversation 标记，供调用方在
  advance_one 层决定是否挂起 tick（里程碑 E）。
- 仲裁全程零 LLM（关系/规则判定），不增加决策成本。

对象 id 约定：玩家的固定 id 是 "player"（与 relationship 的 "player" 一致）；
NPC 之间用各自 npc_id。
"""
from . import db
from . import agent
from . import relationship

PLAYER_ID = "player"

# 被邀者"本 tick 在做实质任务"则视为忙（move/use_item/give_item/interact/trigger_event/attack）。
# wait/observe/speak 不算忙——正在发呆/观察/本来也在说话的人，可以被搭话打断。
_BUSY_TYPES = ("move", "use_item", "give_item", "interact", "trigger_event", "attack")


def _is_alive(session_id, npc_id):
    """NPC 存活判断：get_npc_status 无 dead 记录视作默认活着（与 db 约定一致）。"""
    return not bool(db.get_npc_status(session_id, npc_id).get("dead", False))


def _same_scene(a_pos, b_pos):
    """同一场景才可对话；任一位置未知视为不同场景（保守，宁可让 speak 走导演兜底）。"""
    return bool(a_pos) and bool(b_pos) and str(a_pos) == str(b_pos)


def _speak_target(decision):
    """取当前生效意图是否为 speak，是则返回 (target, speech)，否则 (None, None)。

    读的是 agent.active_action（镜像字段），保证与下游导演读到的"当前生效"一致。
    """
    action = agent.active_action(decision)
    if str(action.get("type", "")).lower() != "speak":
        return None, None
    return str(action.get("target", "") or ""), agent.active_speech(decision)


def _is_busy(decisions, npc_id):
    """被邀者本 tick 是否有实质任务在身（决定它愿不愿意被打断）。"""
    for d in decisions:
        if str(d.get("agent", "")) == str(npc_id):
            t = str((agent.active_action(d) or {}).get("type", "")).lower()
            return t in _BUSY_TYPES
    return False


def _rewrite_as_converse(final: list, target: str) -> None:
    """T2：改写被邀方（target）的决策为 converse（在第 9 类动作上）——对话成立时原地交谈。

    只改 action.type，保留 intent/detail 语义（detail 交给导演做叙事）；同步改写
    plan 当前项（_plan_index 指向的 action），保证 `active_action` 与 plan 镜像一致。
    若 target 的决策不在 final（未参与决策/异常），无副作用——被邀方原行动照旧。
    """
    target = str(target or "")
    if not target:
        return
    for d in final:
        if str(d.get("agent", "")) != target:
            continue
        act = d.get("action")
        if not isinstance(act, dict):
            act = {}
            d["action"] = act
        act["type"] = "converse"
        # 同步 plan 当前项（_parse_plan 每项独立 dict，镜像不共享引用）
        idx = int(d.get("_plan_index", 0) or 0)
        plan = d.get("plan") or []
        if 0 <= idx < len(plan) and isinstance(plan[idx], dict):
            pa = plan[idx].get("action")
            if isinstance(pa, dict):
                pa["type"] = "converse"
        return  # 只改第一个匹配（一个 NPC 一条 decision）


def _force_converse_with_player(decision: dict, player: str) -> None:
    """把某个 decision 的当前行动直接改写成【与玩家对话】(converse，target=player)。

    与 _rewrite_as_converse（NPC↔NPC，只改 type、target 保留被邀方）不同：这里是玩家主动
    发起对话、该 NPC 被强约束，target 必须指向玩家。只改 action.type/target，保留
    intent/speech/detail（交给导演做叙事）；同步改写 plan 当前项（_plan_index 指向的 action），
    保证 active_action 与 plan 镜像一致。不改 _plan_index（避免误判"已推进下一意图"）。
    """
    act = decision.get("action")
    if not isinstance(act, dict):
        act = {}
        decision["action"] = act
    act["type"] = "converse"
    act["target"] = player
    idx = int(decision.get("_plan_index", 0) or 0)
    plan = decision.get("plan") or []
    if 0 <= idx < len(plan) and isinstance(plan[idx], dict):
        pa = plan[idx].get("action")
        if isinstance(pa, dict):
            pa["type"] = "converse"
            pa["target"] = player


# T5 连续交谈机制：用会话级 KV 记录每个 NPC 最近一次参与交谈的 tick。
_DIALOGUE_WINDOW = 3  # 最近 3 tick 内被"连续叫住"过 → 计划推进时拒绝连谈


def _dialog_tick_key(npc_id: str) -> str:
    return f"last_dialog_tick:{npc_id}"


def _record_dialogue_tick(session_id, npc_id, tick):
    """记录某 NPC 本 tick 参与了一场交谈（写会话级 KV，成功/失败都不阻塞）。"""
    try:
        db.upsert_game_state(session_id, _dialog_tick_key(npc_id), int(tick))
    except Exception:  # noqa: BLE001
        pass


def _recently_dialogued(session_id, npc_id, tick, window=_DIALOGUE_WINDOW) -> bool:
    """该 NPC 最近 window tick 内是否已参与过交谈（被连续叫住）。"""
    try:
        last = db.get_game_state_map(session_id).get(_dialog_tick_key(npc_id))
        if last is None:
            return False
        return int(tick) - int(last) < window
    except (TypeError, ValueError):
        return False


def resolve_conversations(session_id, tick, decisions, player_intents=None):
    """仲裁本 tick 的所有对话邀请（zero-LLM）。

    Args:
        session_id: 会话隔离维度。
        tick: 当前 tick（用于会话 start_tick）。
        decisions: 本 tick 各 NPC 的决策（多意图，含 plan/_plan_index/action 镜像）。
        player_intents: 玩家意图池（本模块暂只读取玩家是否在忙，不影响仲裁主流程）。
    Returns:
        dict:
          - final_decisions: 递推/标记后的最终生效 decisions（交给 director.resolve_scenes）。
          - player_invites: 需要玩家裁决的邀请列表（前端弹窗"XX 想跟你对话，同意/拒绝"）。
          - started_convs: 本 tick 双方愿意的 NPC↔NPC【交谈对】（不建 active_conv；交给导演呈现 + apply_npc_dialogue 落影响）。
          - in_conversation: 已进入对话（需挂起）的 actor id 集合——仅"玩家参与"的对话会在此，
            NPC↔NPC 不再计入（不挂起世界）。
    """
    final = []
    player_invites = []
    started = []
    in_conv = set()

    for d in decisions:
        target, speech = _speak_target(d)
        if not target:
            # 非 speak 或 target 为空：原样保留（交给导演兜底，绝不吞掉 NPC 的其它行动）
            final.append(d)
            continue

        # 09-09 修复（用户洞察）：speak 的 target 常是中文显示名（如"测试男"）而非 npc_id（"test_man"）。
        # 后续 db.get_npc_pos / _same_scene / willing / director 的 outcomes.actor 都按 npc_id 建键——
        # 显示名会查空 → 被当成"不同场景"绕过 willing 判定与递推（speak 变裸痕迹、被邀方照常行动，
        # 正是"robot speak 测试男 但 test_man 却能 move"那组矛盾共同的根因）。这里先归一成 npc_id。
        try:
            from . import world_pack
            rid = world_pack.npc_name_to_id(target)
            if rid and rid != target:
                act = d.get("action") or {}
                act["target"] = rid
                # 同步 plan 当前项（_parse_plan 每项是独立 dict，镜像 action 可能不共享引用）
                idx = int(d.get("_plan_index", 0) or 0)
                plan = d.get("plan") or []
                if 0 <= idx < len(plan) and isinstance(plan[idx], dict):
                    pa = plan[idx].get("action")
                    if isinstance(pa, dict) and pa is not act:
                        pa["target"] = rid
                target = rid
        except Exception:  # noqa: BLE001  解析失败保留原 target，走导演兜底
            pass

        initiator = str(d.get("agent", "") or "")
        if not initiator:
            final.append(d)
            continue

        init_pos = db.get_npc_pos(session_id, initiator)

        # --- 对玩家说话：交给前端裁决（本 tick 该 actor 先不导演结算，等玩家应答） ---
        if target == PLAYER_ID:
            if _is_alive(session_id, initiator):
                # 09-09 用户拍板核心：玩家正与某 NPC 对话期间，世界照常推进；但【正在对话的那个
                # NPC】在下一 tick 又产生"想对玩家说话"的意图时，【不应】再次对玩家发起邀请
                # （否则每 tick 都弹"XX 想跟你对话"、反复打断对话）。判它为"已在交谈"→ 递推
                # 它的 speak 到下一个优先级意图（+ 受阻记忆"我正在跟ta聊着"），不产生 player_invite。
                pair = _talking_pair(session_id)
                if pair and str(initiator) == str(pair[0]):
                    try:
                        mind_engine.record_blocked_intent(
                            session_id, initiator, tick,
                            f"speak 玩家", "正在与玩家交谈中，无需再次邀话")
                    except Exception:  # noqa: BLE001  受阻记忆失败不阻塞递推
                        pass
                    d = agent.reject_and_advance(d)
                    final.append(d)
                    continue
                player_invites.append({
                    "initiator": initiator,
                    "target": PLAYER_ID,
                    "scene": init_pos or "",
                    "tick": tick,
                    "speech": speech,
                    "intent": str(d.get("intent", "")),
                })
                in_conv.add(initiator)
                final.append(d)  # 保留引用；advance_one 层依据 player_invites 决定是否挂起（里程碑 E）
            else:
                final.append(d)  # 已死 NPC 的 speak 无效，交给导演兜底
            continue

        # --- 自言自语 / 目标不在场 / 目标已死：无效 speak，交给导演兜底 ---
        if target == initiator:
            final.append(d)
            continue
        if not _is_alive(session_id, target):
            final.append(d)
            continue
        tgt_pos = db.get_npc_pos(session_id, target)
        if not _same_scene(init_pos, tgt_pos):
            final.append(d)  # 不同场景的 speak：导演兜底（跨场景本来就说不通）
            continue

        # --- NPC 邀请 NPC：按关系/现状判定"被邀者愿不愿意" ---
        # 09-09 改版（用户拍板）：NPC↔NPC 对话【不再建 active_conv 持久会话】。
        # 原实现 db.start_conversation 会写 active_conv:<session_id>，但既没有任何代码
        # 推进它、也没有任何代码结束它 → active_conv 永不消失 → advance_one 永远被
        # world.py:342 的 get_conversation 拦成 paused → 世界被永久冻住（"没人收场的会话"）。
        # 正确做法：NPC 之间没玩家参与、不需要逐句字幕，也就不需要挂起世界。把这次
        # "想交谈"当成导演的一次普通同场景事件 —— 保留该 speak 决策交给导演，让导演
        # narrative 写成"XX 与 YY 交谈了…"，并在导演结算后由 apply_npc_dialogue 落
        # 双方记忆/关系微调/世界痕迹。零新建会话、零挂起，世界照常推进。
        busy = _is_busy(decisions, target)
        # T4/T5：把被邀方本 tick 意图的 plan_step（是否在推进既定计划）传给 willing 判定，
        # 使"正有计划要推进"的被邀者更倾向拒绝闲聊，配合连续交谈机制。
        tgt_plan_step = bool(next((d.get("plan_step", False) for d in decisions
                                   if str(d.get("agent", "")) == str(target)), False))
        # T5：连续交谈拒绝——被邀方正推进计划（plan_step）且最近 3 tick 内已被叫住交谈过
        # → 拒绝连谈（"被反复打断"会烦）。这是独立于关系数值的节奏门控。
        if tgt_plan_step and _recently_dialogued(session_id, target, tick, window=3):
            willing = False
        else:
            willing = relationship.willing_to_dialogue(target, initiator, session_id,
                                                       busy=busy, stress=0.0,
                                                       plan_step=tgt_plan_step)
        if willing:
            started.append({
                "initiator": initiator, "target": target, "scene": init_pos or "",
                "tick": tick, "speech": speech or "",
                "intent": str(d.get("intent", "")),
            })
            _record_dialogue_tick(session_id, initiator, tick)
            _record_dialogue_tick(session_id, target, tick)
            # T2（用户拍板 09-09）：对话成立时改写【被邀方】行动——把它的原行动（如 move）
            # 改成 converse（新增第 9 类动作）。发起方保留 speak。这样被邀方"停在原地交谈"，
            # 不再执行原 move（否则会出现"没移动但位置变了/移动了但痕迹却是 converse"的矛盾）。
            _rewrite_as_converse(final, target)
            final.append(d)  # 保留该 speak、交给导演呈现"交谈"（导演在同场景聚合里描写二者）
        else:
            # 被拒 → 递推发起者的下一个优先级意图，再交给导演；全失败时 reject_and_advance 收尾 wait。
            # 09-09 用户拍板：被拒本身是 NPC 的真实认知事实，必须进记忆(否则它表现得
            # "从没想过要说话")。reject_and_advance 会原地改写 d，故先捕获原意图再记录。
            try:
                _orig = (d.get("action") or {}).get("type", "wait")
                _orig_tgt = (d.get("action") or {}).get("target", "")
                mind_engine.record_blocked_intent(
                    session_id, initiator, tick,
                    f"{_orig}{' '+str(_orig_tgt) if _orig_tgt else ''}",
                    "对方拒绝了交谈")
            except Exception:  # noqa: BLE001  受阻记忆失败不阻塞递推
                pass
            d = agent.reject_and_advance(d)
            final.append(d)

    return {"final_decisions": final, "player_invites": player_invites,
            "started_convs": started, "in_conversation": in_conv}


# =============================================================================
# 邀请裁决的"收场"（09-10 用户拍板：多人同时邀请 = 多选一 + 底部婉拒对话）
# 同一 tick 里可能【不止一个】NPC 都想跟玩家说话（player_invites 本就是多条）。玩家裁决语义：
#   · 选中其中一人 → 该 NPC 进对话，其余邀请人【一并婉拒】（各自递推下一优先级意图 + 进记忆）；
#   · 点"婉拒对话" → 【全部】邀请人都不进行对话。
# 为什么必须统一收场：邀请人的 speak 决策一直留在本 tick 的 decisions 里，谁没被明确收场，
# 谁就会在导演那里继续"对玩家说话"——玩家明明只选了 A，叙述里 B 也来搭话（破窗）。
# =============================================================================
def decline_invites(session_id, tick, decisions, npc_ids, reason="对方婉拒了交谈"):
    """把若干【未被选中 / 被婉拒】的邀请人收场：写受阻记忆 + 递推 plan 的下一优先级意图。

    与 resolve_conversations 里"NPC 邀 NPC 被拒"完全同一口径（零 LLM、纯规则）：
    受阻是 NPC 的真实认知事实，必须进记忆，否则它会表现得"从没想过要说话"。

    Args:
        decisions: 本 tick 的决策列表（= 挂起态 pending_conv_offer 里存的那一份）。
        npc_ids: 需要收场的 NPC id 集合（list/set 皆可；空 → 原样返回）。
        reason: 写进 NPC 记忆的受阻原因（"对方婉拒了交谈" / "对方选择了与XX交谈"）。
    Returns:
        更新后的 decisions（新列表；不在 npc_ids 里的决策原样保留）。
    """
    want = {str(x) for x in (npc_ids or []) if str(x)}
    if not want:
        return list(decisions or [])
    out = []
    for d in decisions or []:
        ag = str(d.get("agent", "") or "")
        if ag in want:
            # 受阻记忆必须【先】写：reject_and_advance 会原地改写 d 的镜像字段，
            # 原意图（"speak player"）不提前捕获就丢了。
            try:
                _orig = (d.get("action") or {}).get("type", "wait")
                _orig_tgt = (d.get("action") or {}).get("target", "")
                mind_engine.record_blocked_intent(
                    session_id, ag, tick,
                    f"{_orig}{' ' + str(_orig_tgt) if _orig_tgt else ''}", reason)
            except Exception:  # noqa: BLE001  受阻记忆失败不阻塞递推
                pass
            d = agent.reject_and_advance(d)
        out.append(d)
    return out


# =============================================================================
# 玩家正在对话期间的"不打扰"判定（09-09 用户拍板：世界照常推演，但其他 NPC 不去打扰这对）
# 放在 resolve_conversations【之前】调用：对话期间若是别的 NPC 也想 speak/move/interact 玩家或
# 那株对话 NPC，会被改写为 wait（"看见他俩在聊天，就没去打扰"），而不是又一次对玩家产生邀请。
# =============================================================================
def _talking_pair(session_id):
    """当前是否有【玩家参与】的对话（active_conv），返回 (对话NPC_id, PLAYER_ID)；无则 None。

    标准：会话双方中一方固定是玩家（player）。NPC↔NPC 的交谈不建 active_conv，故不会误判。
    """
    conv = db.get_conversation(session_id)
    if not conv:
        return None
    initiator = str(conv.get("initiator", "") or "")
    target = str(conv.get("target", "") or "")
    if initiator == PLAYER_ID:
        return (target, PLAYER_ID)   # 玩家主动 / 或玩家被邀（发起者=玩家）
    if target == PLAYER_ID:
        return (initiator, PLAYER_ID)
    return None   # NPC↔NPC（理论不出现，防误判）


# 想"接触"这对的行为类型：靠近(move)/搭话(speak)/互动(interact)/递物(give_item)/攻击(attack)。
_DISTURB_TYPES = ("move", "speak", "interact", "give_item", "attack")


def marks_no_disturb(session_id, tick, decisions, world_id="test"):
    """玩家正与某 NPC 对话期间，其他 NPC 想靠近/搭话这对中任一方 → 受阻并【递推】后续意图。

    09-09 用户纠正：这不是"改成 wait"。NPC 本 tick 有多个优先级意图（plan），当最高优先级意图
    （想靠近/搭话这对中的任一方）被"受阻→没去打扰"时，应触发【既有递推链路】agent.reject_and_advance，
    自动降级到 plan 的【下一个优先级意图】（可能是 observe/move 别处等），仅当 plan 穷尽时才收尾 wait。
    ——即"受阻=该 打算 走不通 → 换下一个打算"，与该 NPC 本 tick 的最终行动是否 wait 无关。

    规则（零 LLM，社会常识"看见他们在聊天，就不去打扰"）：
      - 只处理【对话对之外】的 NPC（对话中的任一方保持行动，不干扰它们自己）；
      - 只处理动作 target 落在对话对（玩家或对话NPC）上的"接触类"意图；
      - 要求打扰者与目标同场景（不同场景谈不上"看见"）；
      - 命中 → record_blocked_intent(受阻记忆) + agent.reject_and_advance(递推到 plan 下一意图)。
    Returns: （原地修改后的）decisions，供调用方继续走 resolve_conversations。
    """
    pair = _talking_pair(session_id)
    if not pair:
        return decisions
    talk_npc, _ = pair   # 对话中的 NPC id
    for d in decisions or []:
        ag = str(d.get("agent") or "")
        if not ag or ag == talk_npc:
            continue   # 对话中的 NPC 保持自身行动，不判定它去打扰自己
        act = d.get("action") or {}
        atype = str(act.get("type", "")).lower()
        if atype not in _DISTURB_TYPES:
            continue
        tgt_raw = str(act.get("target", "") or "")
        if not tgt_raw:
            continue
        # 归一 target → npc_id（speak target 常是中文显示名，须解析回 id 再比对）
        if tgt_raw in (PLAYER_ID, "玩家"):
            rid = PLAYER_ID
        else:
            try:
                from . import world_pack
                rid = world_pack.npc_name_to_id(tgt_raw) or tgt_raw
            except Exception:  # noqa: BLE001  解析失败按原文
                rid = tgt_raw
        if rid != talk_npc and rid != PLAYER_ID:
            continue
        # 同场景才"看见"：打扰者所在 === 目标所在
        try:
            ag_scene = db.get_npc_pos(session_id, ag) or ""
            tgt_scene = (db.get_player_scene(session_id) if rid == PLAYER_ID
                         else db.get_npc_pos(session_id, rid) or "")
            if tgt_scene and ag_scene and str(ag_scene) != str(tgt_scene):
                continue
        except Exception:  # noqa: BLE001  读场景失败不判定（保守不拦）
            pass
        # 命中：受阻记忆（"看见他俩在聊天"这一认知事实）+ 触发递推链路（降到 plan 下一优先级意图）。
        # ⚠️ 递推后该 NPC 本 tick 的【最终行动】由 plan 决定（可能 observe/move 别处），不强制 wait。
        try:
            mind_engine.record_blocked_intent(
                session_id, ag, tick,
                f"{atype}{' '+tgt_raw if tgt_raw else ''}", "看见他俩在聊天，就没去打扰")
        except Exception:  # noqa: BLE001  受阻记忆失败不挡主链路
            pass
        agent.reject_and_advance(d)   # 多意图递推：本打算受阻 → 自动换下一个优先级打算
    return decisions


# =============================================================================
# 对话中 NPC 自身的意图受限链（09-09 用户拍板，已按用户最新语义修正）：
# marks_no_disturb 处理"对话对外"的 NPC，但 line 316 `if ag == talk_npc: continue`
# 明确【跳过对话者本身】——导致被玩家邀请对话的那个 NPC，本 tick 的【一切】意图都照常
# 执行（"一边对话一边走开"破窗）。这里补上对 talk_npc 自身的强约束。
#
# 用户最新语义（关键修正）：
#   - 不是只拦 move/observe——【所有】行为都会受限。只要某 NPC 是"被玩家成功发起对话"的
#     对象（talk_npc），它本 tick 无论原本意图是什么（去房间二/观察/想找NPC B说话/互动…），
#     原行为都不会发生；
#   - 受限链里，若【第一个受限来源 = 玩家和其说话导致的】，就【直接把本次行为修改为"与玩家对话"】；
#   - 记忆【只记录这一次限制】。
#   - 为什么不能用 reject_and_advance 递推：递推只在 plan 的候选意图之间降级切换，而
#     "与玩家对话"（converse）【根本不在意图清单里】，递推永远产生不出它。要让"与玩家对话"
#     成为本次行动，就必须【直接把行为改写】成 converse(target=player)，而不是降级遍历。
# =============================================================================
def marks_talking_npc_constrain(session_id, tick, decisions, world_id="test"):
    """玩家正与其 NPC 对话期间，对话中 NPC 自身的【一切】意图 → 受限并改写为"与玩家对话"。

    与 marks_no_disturb 互补：marks_no_disturb 管"对话对外"的 NPC（不打扰这对），
    本函数管"对话对内"的那个 NPC 自身（不离开对话、改成正在交谈）。两者都在 resolve_conversations
    之前调用，使改写后的 converse 不被 resolve 误当 speak 再做邀请/递推（converse 非 speak，
    resolve 的 _speak_target 会判非 speak → 原样保留交给导演）。

    规则（零 LLM，社会常识"正在跟人说话，就不去做别的事"）：
      - 只处理 _talking_pair 的对话 NPC（talk_npc）自身；
      - 当前意图已是"与玩家对话"（speak/converse target=player）→ 保留，记录"正好和玩家发起了对话"；
      - 其它【所有】意图（move/observe/interact/use_item/give_item/trigger_event/attack/
        speak(别人)/wait…）→ 直接把 action 改写成 converse(target=player)，并记录一次受阻记忆
        （"本来想X但被玩家邀请了对话"）。不做 plan 穷尽遍历、不 reject_and_advance。
    Returns: 原地修改后的 decisions，供调用方继续走 resolve_conversations。
    """
    pair = _talking_pair(session_id)
    if not pair:
        return decisions
    talk_npc, player_id = pair
    for d in decisions or []:
        ag = str(d.get("agent") or "")
        if ag != talk_npc:
            continue   # 只约束对话中的 NPC 自身
        act = d.get("action") or {}
        atype = str(act.get("type", "")).lower()
        atgt = str(act.get("target", "") or "")
        # 09-10 用户洞察（现象1/2 根治）：无论当前意图是什么【统一改写为"与玩家对话"】——
        # 哪怕是"本就是 speak player"，也必须 type→converse、target→player，否则 _speak_target
        # 仍读到 type=="speak"，下一站 resolve_conversations:194-210 会因"正在交谈"把这次
        # speak 递推成 plan 下一个意图（如 observe），导致导演叙事里没有玩家（破窗）。
        # converse 非 speak → _speak_target 返回 (None,None) → resolve:160 原样保留、不递推。
        if atype in ("speak", "converse") and atgt == player_id:
            # 本来就是"与玩家对话" → 记忆记"正好和玩家发起了对话"（无"被阻止"补充）
            _desc, _reason = "与玩家对话", "正好和玩家发起了对话"
        elif atype == "wait":
            # wait 是"兜底却没被阻"的意图（用户第1点）：记忆用"原本打算wait，但玩家过来聊天"。
            _desc, _reason = f"{atype}{' '+atgt if atgt else ''}", "原本打算 wait，但玩家过来聊天"
        else:
            # 其余具体意图：记忆记"本想去完成这个打算，但被玩家邀请了对话"。
            _desc, _reason = f"{atype}{' '+atgt if atgt else ''}", "本想去完成这个打算，但被玩家邀请了对话"
        try:
            mind_engine.record_blocked_intent(session_id, ag, tick, _desc, _reason)
        except Exception:  # noqa: BLE001  受阻记忆失败不挡主链路
            pass
        _force_converse_with_player(d, player_id)   # 统一改写为 converse(player)，避免被 resolve 递推
    return decisions


# =============================================================================
# NPC↔NPC 对话的影响落库（09-09 用户拍板"导演总结式" + 09-10 关系写收口）
# =============================================================================
# 既然 NPC↔NPC 不再建 active_conv 持久会话（不挂起、无人收场），那"他们聊了什么、
# 各自受了什么影响"就必须落下来，否则交谈就凭空消失、对世界毫无影响。
#
# 多存储分工（P0-b，单一事实源/避免重复写）：
#   记忆表（memory）= 长期情节（event/impression，含 importance/related_entity）；
#   working_memory = 工作记忆（热态，mind_engine 管）；world_trace = 客观流水；
#   beliefs = 信念。**导演分析的 cognition 只写记忆表**，绝不再写 working_memory
#   或另起一套痕迹（防重复）。
#
# 关系写收口（P0-b）：此前 apply_npc_dialogue 用规则 ±2（交谈即拉近），
# 这里**删除该规则**，关系改由"导演分析产出的 relation（有符号净增）"写入——
# 经 apply_director_relation 统一收口，避免同场交谈被"规则 +2"与"导演 relation"叠记。
from . import memory as memory_mod


def _dialogue_topic(pair: dict, narrative: str) -> str:
    """交谈主题：优先导演 narrative，其次首句，最后意图词兜底。"""
    topic = (narrative or "").strip()
    if not topic:
        topic = str(pair.get("speech", "") or "").strip()[:30]
    if not topic:
        topic = str(pair.get("intent", "") or "").strip() or "交谈"
    return topic


def apply_npc_dialogue(session_id, tick, pair, narrative="") -> None:
    """落一段 NPC↔NPC 交谈对的【记忆 + 世界痕迹】（关系由导演 analysis 单独收口）。

    注意（P0-b 关系写收口）：本函数**不再**写 update_relationship 规则 ±2；
    关系影响由 T3 导演分析产出 relation 后，经 apply_director_relation 写入。
    此处只保证"谈过这件事"进记忆表 + 一条客观痕迹（双方召回时进认知上下文）。

    Args:
        session_id: 会话隔离维度（记忆/世界痕迹都要它）。
        tick: 当前 tick。
        pair: resolve_conversations 收集的交谈对 {initiator,target,scene,tick,speech,intent}。
        narrative: 导演为本场景生成的一句话叙述（可选，用作痕迹正文/记忆补充）。
    """
    initiator = str(pair.get("initiator", "") or "")
    target = str(pair.get("target", "") or "")
    if not initiator or not target or initiator == target:
        return
    scene = str(pair.get("scene", "") or "")
    topic = _dialogue_topic(pair, narrative)

    # ① 双方各写一条"经历"记忆（相关实体=对方；召回时进上下文 → 影响认知）
    try:
        for who, other in ((initiator, target), (target, initiator)):
            memory_mod.write_memory(
                who, "event",
                f"我和 {other} 交谈：{topic}",
                importance=5, summary=topic[:30],
                related_entity=other, session_id=session_id,
            )
    except Exception:  # noqa: BLE001  记忆失败不阻塞世界推进（降级）
        pass

    # ② 【已收口】关系不再用规则 ±2 —— 交给导演 analysis 的 relation（P0-b）。

    # ③ 一条全可见世界痕迹（前端/导演叙述可见，符合"世界在运转"）
    record_dialogue_trace(session_id, tick, pair, narrative)


def record_dialogue_trace(session_id, tick, pair, narrative="") -> None:
    """只写一条"交谈"的世界痕迹（客观流水）。供导演分析成功路径补痕迹用，
    避免与 apply_npc_dialogue 的记忆部重复写（P0-b：认知进记忆表，痕迹进 world_trace）。"""
    initiator = str(pair.get("initiator", "") or "")
    target = str(pair.get("target", "") or "")
    if not initiator or not target or initiator == target:
        return
    topic = _dialogue_topic(pair, narrative)
    scene = str(pair.get("scene", "") or "")
    try:
        detail = f"{initiator} 与 {target} 交谈了：{topic}"
        db.add_world_trace(session_id, tick, initiator, "conversation",
                           target=target, location=scene, detail=detail)
    except Exception:  # noqa: BLE001
        pass


def apply_director_relation(session_id, initiator, target, relation=None) -> None:
    """关系写收口（P0-b）：把导演分析产出的 relation（有符号净增）落到双方关系行。

    relation 格式（与 director 分析产物一致）：
        {"initiator": +delta, "target": +delta}  —— 有符号净增；0 / 缺省 = 不变。
    仅当存在非零增量时才写（避免 0 增量循环写库）。这与被删掉的应用
    "规则 ±2"（apply_npc_dialogue 旧版）互斥——同一场交谈只会走这一条关系路径。
    """
    relation = relation or {}
    try:
        d_i = float(relation.get(initiator, 0) or 0)
        d_t = float(relation.get(target, 0) or 0)
    except (TypeError, ValueError):
        return
    if d_i == 0 and d_t == 0:
        return
    try:
        # 有符号净增：delta>0 提升关系，delta<0 拉远（信任/好感同向）
        if d_i != 0:
            db.update_relationship(initiator, target, session_id,
                                   delta_trust=d_i, delta_affection=d_i)
        if d_t != 0 and str(target) != str(initiator):
            db.update_relationship(target, initiator, session_id,
                                   delta_trust=d_t, delta_affection=d_t)
    except Exception:  # noqa: BLE001  关系失败不阻塞
        pass


# =============================================================================
# 会话状态机 + 对话专属提示词（里程碑 D）
# =============================================================================
# 与 agent 的"行为决策提示词"（_snapshot_to_prompt）明确分离：说话时用不上
# 8 类动作规则 / move/observe 引导，只需聚焦"对话情境 + 关系 + 历史 + 轮次 + 目的"。
# 复用 mind_engine.process_dialogue（LLM①，理解玩家那句并更新内心情态）。
from . import llm as llm_mod
from . import scheduler
from . import mind_engine

_conversation_client = llm_mod.DeepSeekClient()  # 模块级单例，与 agent 同口径


def _display_name(npc_id):
    if str(npc_id) == PLAYER_ID:
        return "玩家"
    card = db.get_character_card(npc_id)
    return card[1] if card else str(npc_id)


def _relationship_line(conv, speaker, session_id):
    """以 speaker 视角构造"你对对话对象的关系"一句；无记录则不写。"""
    other = conv["target"] if speaker == conv["initiator"] else conv["initiator"]
    rel = db.get_relationship(speaker, other, session_id)
    if not rel:
        return ""
    trust, fear, affection, rel_type, _notes = rel
    seg = f"你对对方的关系：trust={trust}, affection={affection}, fear={fear}"
    if rel_type:
        seg += f"（{rel_type}）"
    return seg


def _transcript(conv):
    """把对话历史渲染成可读文本（最近若干条，带说话人名字）。"""
    rows = (conv.get("history") or [])[-12:]
    if not rows:
        return "（尚未开始）"
    return "\n".join(f"{_display_name(h['speaker'])}：{h['text']}" for h in rows)


def build_conversation_messages(conv, speaker, session_id, world_id="test") -> list:
    """构造【对话专属提示词】（不含行为引导）。

    与 agent._snapshot_to_prompt（行为决策，含 8 类动作规则/move/observe）明确分离：
    说话时那些引导用不上，只需聚焦对话情境、关系、历史、轮次、目的。

    Args:
        conv: 会话 dict（db.get_conversation）。
        speaker: 本句该说话的人（NPC id 或 PLAYER_ID）。
        session_id: 会话隔离维度（召回各自关系/角色卡）。
        world_id: 世界隔离维度（角色人设按世界取）。
    Returns:
        (system, user) 组成的 messages。
    """
    role = "玩家" if speaker == PLAYER_ID else _display_name(speaker)
    other = conv["target"] if speaker == conv["initiator"] else conv["initiator"]
    other_name = "玩家" if other == PLAYER_ID else _display_name(other)
    clock = scheduler.tick_to_clock(int(conv.get("start_tick", 0) or 0))

    sys_lines = [
        f"你（{role}）正在和 {other_name} 对话。这是一场有限的多轮谈话，最多约 {conv.get('max_rounds', 5)} 句。",
    ]
    if clock:
        sys_lines.append(f"【时间】{clock}")
    if conv.get("scene"):
        sys_lines.append(f"【地点】{conv['scene']}")
    if speaker != PLAYER_ID:
        card = db.get_character_card(speaker)
        if card:
            name, title = card[1], card[2]
            sys_lines.append(f"【你的身份】{name}" + (f"（{title}）" if title else ""))
            if card[3]:
                sys_lines.append(f"性格：{card[3]}")
            if card[5]:
                sys_lines.append(f"动机：{card[5]}")
        rel_line = _relationship_line(conv, speaker, session_id)
        if rel_line:
            sys_lines.append(f"【关系】{rel_line}")
    if conv.get("topic"):
        sys_lines.append(f"【你这次想聊的】{conv['topic']}")
    sys_lines.append("【目前的谈话】")
    sys_lines.append(_transcript(conv))
    sys_lines.append(
        f"【进度】这是第 {int(conv.get('round', 0)) + 1} 句 / 共约 {conv.get('max_rounds', 5)} 句。"
        "若已接近尾声或你想结束这场谈话，就自然地说一句收尾的话；否则可以继续话题、"
        "或按你心里的目的旁敲侧击。"
    )
    sys_lines.append(f"现在轮到你（{role}）说话。只输出这一句话本身，不要旁白、不要加引号、不要扮演对方。")
    return [
        {"role": "system", "content": "\n".join(sys_lines)},
        {"role": "user", "content": "（轮到你说话）"},
    ]


def push_turn(conv, speaker, text):
    """把一句写进会话（含说话人），并递增轮次。返回更新的 conv。
    结束判定交给 is_conversation_over（round>=max_rounds 或 status 非 active）。"""
    conv.setdefault("history", []).append({"speaker": speaker, "text": text})
    conv["round"] = int(conv.get("round", 0)) + 1
    return conv


def is_conversation_over(conv):
    """对话是否结束：主动结束（status!=active）或已达句数上限。"""
    return conv.get("status") != "active" or conv.get("round", 0) >= conv.get("max_rounds", 5)


def npc_next_speaker(conv, last_speaker=None):
    """下一个该说话的人：发起者第一个说，之后双方交替（含玩家参与）。
    返回"上一个说话者之外的另一方"；无上一个则返回发起者。"""
    a, b = conv["initiator"], conv["target"]
    if not last_speaker:
        return a
    return b if str(last_speaker) == str(a) else a


def other_party(conv, who):
    """返回对话中除 who 之外的另一方。"""
    return conv["target"] if str(who) == str(conv["initiator"]) else conv["initiator"]


def npc_generate(conv, speaker, session_id, world_id="test", llm_client=None):
    """让 NPC 在本轮说一句，返回该句文本（失败返回空字符串，由端点兜底）。
    只生成、不写回 history（写回由调用方 push_turn，便于流式与失败处理）。"""
    client = llm_client or _conversation_client
    try:
        return client.chat(build_conversation_messages(conv, speaker, session_id, world_id))
    except Exception as e:  # noqa: BLE001  单句失败：返回空，调用方决定结束或重试
        return ""


def npc_generate_stream(conv, speaker, session_id, world_id="test", llm_client=None):
    """流式生成 NPC 本句（chat_stream），生成结束后把该句写回 history（finally 保证落账）。

    Returns: 逐段产出回复文本的生成器；写回由生成器自身在结束/异常时执行。
    会话状态一致性：即使流被中断，已生成部分也已入库（下次 turn 能接上）。
    """
    client = llm_client or _conversation_client

    def _gen():
        full = []
        try:
            for token in client.chat_stream(build_conversation_messages(conv, speaker, session_id, world_id)):
                full.append(token)
                yield token
        except Exception:  # noqa: BLE001
            pass
        finally:
            if full:
                push_turn(conv, speaker, "".join(full))
    return _gen()


def player_respond(conv, player_text, session_id, world_id="test"):
    """玩家回一句：先走心智理解（process_dialogue，LLM①），再把这句记进会话。
    玩家在对话里回应的对象是"非玩家的另一方"（发起或被邀的 NPC）。
    Returns: 更新后的 conv。"""
    npc_partner = other_party(conv, PLAYER_ID)
    try:
        mind_engine.process_dialogue(npc_partner, session_id, world_id, player_text)
    except Exception:  # noqa: BLE001  理解失败不挡对话主链路（降级）
        pass
    return push_turn(conv, PLAYER_ID, player_text)


def npc_reply_and_turn(conv, speaker, session_id, world_id="test", llm_client=None):
    """NPC 说一句并写回（npc_generate + push_turn 的便捷组合）。返回 (conv, reply)。"""
    reply = npc_generate(conv, speaker, session_id, world_id, llm_client)
    if reply:
        push_turn(conv, speaker, reply)
    return conv, reply


def mark_conversation_ended(conv):
    """标记对话结束（主动/超限后置 status=ended，供 advance_one 恢复推进）。"""
    conv["status"] = "ended"
    return conv


# =============================================================================
# 动态 NPC 介绍（第2点）：进入对话前，先给玩家一段"此刻眼中 TA"的介绍
# =============================================================================
def _static_intro(card, npc_id):
    """回退：角色卡静态字段拼接（不出错、不空手）。"""
    if card:
        name, title, personality, _bg, motivation = card[1], card[2], card[3], card[4], card[5]
        parts = [name + (f"（{title}）" if title else "")]
        if personality:
            parts.append(personality)
        if motivation:
            parts.append(motivation)
        return "；".join(parts)
    return str(npc_id)


def intro_dynamic_npc(npc_id, session_id, world_id="test", llm_client=None):
    """生成【此刻玩家眼中的 TA】动态介绍（第2点）。

    与静态角色卡不同：随该 NPC 的当前状态 / 近期行动 / 与玩家的关系 / 所在场景变化而变化——
    玩家第一次照面读到的，是这个 NPC「现在」的样子，而不是出厂设定。每次进入对话前生成一次
    （1 次 LLM，符合 ≤2 次/交互预算）；失败回退静态卡，绝不阻塞对话。
    """
    card = db.get_character_card(npc_id)
    name = card[1] if card else npc_id
    pos = db.get_npc_pos(session_id, npc_id) or ""
    try:
        st = db.get_npc_status(session_id, npc_id)
        status = st if isinstance(st, dict) else {}
    except Exception:  # noqa: BLE001
        status = {}
    try:
        rel = list(db.get_relationship(npc_id, "player", session_id) or []) + [None] * 5
    except Exception:  # noqa: BLE001
        rel = [None] * 5
    try:
        traces = [t for t in (db.get_recent_traces(session_id, 999999, limit=30) or [])
                  if str(t[1]) == str(npc_id) and str(t[2]) not in ("wait",)]
    except Exception:  # noqa: BLE001
        traces = []

    sys = (
        f"你是《{db.get_world_name(world_id) or '这个世界'}》的说书人。用一两段流畅的中文，向刚走到"
        f"这个场景的玩家介绍【此刻】正站在他面前的这个角色。要写的是「现在这一刻」：身份、神情、"
        "举手投足透出的状态、最近的异样或举动、与玩家此刻的关系距离。像场景旁白一样自然，"
        "不要出现「NPC」「角色」「测试」这类词，不要用列点式，直接写成一段叙述。"
    )
    user = f"【角色】{name}\n"
    if pos:
        user += f"【此刻所在】{pos}\n"
    if status:
        dead = status.get("dead")
        if dead:
            user += "【状态】ta 已经不在人世了。\n"
        else:
            # 单一事实源（P0-a）：神情一律从心智热态派生（mental.emotion_snapshot 只读快照），
            # 绝不从 npc_status 读 mood/emotion_word（那是副事实源，会与热态不一致/重复）。
            try:
                from . import mental
                mood = mental.emotion_snapshot(db.get_mental_state(session_id, npc_id)).get("word", "")
            except Exception:  # noqa: BLE001  快照失败回退空，不阻塞
                mood = ""
            if mood:
                user += f"【神情】{mood}\n"
    if rel and rel[3]:
        trust, fear, affection, rtype, notes = rel[0], rel[1], rel[2], rel[3], rel[4]
        user += (f"【与你的关系】{rtype}：信任{trust} 好感{affection} 畏惧{fear}"
                 + (f"（{notes}）" if notes else "") + "\n")
    if traces:
        user += "【ta 最近做过/说过的】\n" + "\n".join(f"- {t[5]}" for t in traces[:5]) + "\n"
    try:
        from .spatial import build_perception_snapshot
        snap = build_perception_snapshot(pos, session_id=session_id, world_id=world_id, observer="player")
        if snap:
            user += snap + "\n"
    except Exception:  # noqa: BLE001
        pass

    client = llm_client or _conversation_client
    try:
        out = client.chat([{"role": "system", "content": sys},
                           {"role": "user", "content": user + "\n请写这段此刻的介绍。"}])
        return str(out).strip()
    except Exception:  # noqa: BLE001
        return _static_intro(card, npc_id)
