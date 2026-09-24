"""世界时钟在线驱动（OL-2）：把 simulate 的单 tick 步抽成可复用函数，供
离线命令行模拟与在线游戏（/chat 后自动推进、/world/step 手动推进）共用。

五阶段语义（世界时序 v2，与 simulate.run_simulation 完全一致）：
  ① 计划执行（零 LLM） → ①.5 重规划/从零起（≤1 LLM/事件） → ② 并行决策
  （排班例外 + 无计划心智 NPC 兜底，上限防成本失控） → ③ fate 仲裁落库
  （机械动作确定性 + interact/trigger 的 LLM 反应仲裁） → ⑤ current_tick 推进。

在线驱动（auto_advance）：玩家每次交互（说话/行动/略过）后由 main.py 在后台
线程调用——"挂机不推进、交互才流逝"（与 Clock.gd 的设计约定一致）。会话级
互斥锁防止玩家连发两条消息时重入推两个 tick。
"""
import logging
import threading

from . import agent
from . import db
from . import fate
from . import plan_executor
from .plan_executor import _precondition_met
from . import plans
from . import recorder
from . import scheduler
from . import mind_engine
from . import replanner

logger = logging.getLogger(__name__)

# 无计划心智 NPC 的决策兜底上限（每 tick 最多几个"闲逛者"动脑——防大世界成本失控；
# 按 npc_id 字典序取，确定性）
_MAX_FREE_DECIDERS = 4

# 会话级互斥：一个会话同一时刻只推一个 tick（在线自动推进防重入）
_advance_locks: dict = {}
_locks_guard = threading.Lock()


def _session_lock(session_id: str) -> threading.Lock:
    with _locks_guard:
        if session_id not in _advance_locks:
            _advance_locks[session_id] = threading.Lock()
        return _advance_locks[session_id]


def _run_replan_phase(session_id: str, tick: int, npcs: list, world_id: str, llm_client) -> list:
    """重规划阶段（T6）：blocked → 心智策略 → LLM 生成新计划接棒；
    有 Desire 无 Intention → 从 0 起首个规划（每会话每 NPC 一次）。结果进 recorder。"""
    results = []
    gs = db.get_game_state_map(session_id)
    jobs, scratch_jobs = [], []

    for p in plans.get_blocked_plans(session_id):
        if p["npc_id"] not in npcs:
            continue
        # 死者不重规划（①.5 重规划阶段）：尸体没有"计划受阻"可言，直接跳过。
        if db.get_npc_status(session_id, p["npc_id"]).get("dead"):
            continue
        if not db.get_mental_model(p["npc_id"]):
            continue
        fails = int(gs.get(f"replan_fail:{p['id']}", 0) or 0)
        if fails >= 2:
            continue
        jobs.append((p["npc_id"], p, f"计划受阻：{str(p.get('blocked_reason', ''))[:60]}"))

    for npc_id in npcs:
        if any(j[0] == npc_id for j in jobs):
            continue
        # 死者跳过"从零起规划"：尸体没有欲望/目标可言。
        if db.get_npc_status(session_id, npc_id).get("dead"):
            continue
        if plans.get_active_plan(session_id, npc_id):
            continue
        if gs.get(f"scratch_plan:{npc_id}"):
            continue
        if not db.get_mental_model(npc_id):
            continue
        goals = db.get_goals(npc_id)
        if goals:
            scratch_jobs.append((npc_id, goals[0]))

    for npc_id, old_plan, trigger in jobs:
        ctx = mind_engine.snapshot_ctx(npc_id, session_id)
        r = replanner.replan(session_id, npc_id, old_plan, trigger, ctx, world_id,
                             llm_client=llm_client, now_tick=tick)
        results.append({"npc": npc_id, "phase": "replan", **r})
        if not r.get("ok"):
            key = f"replan_fail:{old_plan['id']}"
            db.upsert_game_state(session_id, key, int(gs.get(key, 0) or 0) + 1)
        recorder.note_replan(session_id, {"npc": npc_id, "phase": "replan", **r})

    for npc_id, goal in scratch_jobs:
        ctx = mind_engine.snapshot_ctx(npc_id, session_id)
        r = replanner.plan_from_scratch(session_id, npc_id, goal, ctx, world_id,
                                        llm_client=llm_client, now_tick=tick)
        db.upsert_game_state(session_id, f"scratch_plan:{npc_id}", 1)
        results.append({"npc": npc_id, "phase": "scratch", **r})
        recorder.note_replan(session_id, {"npc": npc_id, "phase": "scratch", **r})
    return results


def _decide_targets(session_id: str, tick: int, npcs: list, mode: str = "plan") -> list:
    """决策目标。

    live（在线游戏，A5 裁决）：**全部存活 NPC 每 tick 都 LLM 决策**——计划是
    心智上下文而非执行脚本，NPC 与玩家同等自由度；无 mental_model 的角色走
    旧平铺 prompt 照样决策。
    plan（离线回放/自检）：排班例外 + 无计划心智 NPC 兜底（成本可控的确定性模式）。
    """
    if mode == "live":
        return [n for n in npcs if not db.get_npc_status(session_id, n).get("dead")]
    targets, free = [], []
    for npc_id in npcs:
        if plan_executor.has_active_plan(session_id, npc_id):
            continue
        if db.get_npc_status(session_id, npc_id).get("dead"):
            continue
        if scheduler.scheduled_actions(npc_id, tick):
            targets.append(npc_id)
        elif db.get_mental_model(npc_id):
            free.append(npc_id)
    for npc_id in sorted(free)[:_MAX_FREE_DECIDERS]:
        if npc_id not in targets:
            targets.append(npc_id)
    return targets


def _check_plan_preconditions(session_id, npcs, world_id):
    """live 模式的计划检视：检查 active 计划当前步前置（不执行效果）。
    不满足 → mark blocked——compose L5 给 NPC"计划受阻"上下文，①.5 重规划接管。"""
    for npc_id in npcs:
        plan = plans.get_active_plan(session_id, npc_id)
        if not plan or not plan.get("steps"):
            continue
        cur = int(plan.get("current_step", 1) or 1)
        step = plan["steps"][cur - 1] if 1 <= cur <= len(plan["steps"]) else None
        if not step:
            continue
        unmet = [p for p in (step.get("preconditions") or [])
                 if not _precondition_met(session_id, p, plan["npc_id"], world_id)]
        if unmet:
            reasons = "; ".join(
                f"{p.get('check')}:{p.get('target')}.{p.get('key')}!={p.get('expected')}" for p in unmet)
            plans.mark_status(session_id, plan["id"], "blocked", blocked_reason=reasons)
            recorder.note_env(session_id, npc_id,
                              {"type": "计划受阻", "target": step.get("target", ""),
                               "message": reasons[:120], "outcome": "blocked", "changed": False})


def _advance_plan_if_matched(session_id, decisions):
    """live 模式的计划推进：AI 决策与计划当前步吻合 → advance_step（意图被兑现）。
    不吻合不惩罚——NPC 有偏离计划的自由（涌现）。Returns: 推进事件列表。"""
    advanced = []
    for d in decisions or []:
        npc_id = str(d.get("agent", ""))
        plan = plans.get_active_plan(session_id, npc_id)
        if not plan or not plan.get("steps"):
            continue
        cur = int(plan.get("current_step", 1) or 1)
        step = plan["steps"][cur - 1] if 1 <= cur <= len(plan["steps"]) else None
        if not step:
            continue
        a = (d.get("action") or {})
        target_match = (str(a.get("target", "")) == str(step.get("target", ""))
                        or str(a.get("target", "")) == str(step.get("scene", "")))
        # T4 门控：只有 LLM 自报 plan_step=true 的意图才被视为"在兑现计划"，
        # 才能触发 advance_step。临时起意/观察/等待即便恰好行动吻合，也不算兑现计划步，
        # 避免"计划推进"被偶然行动误触发。
        is_plan_step = bool(d.get("plan_step", False))
        if is_plan_step and str(a.get("type", "")) == str(step.get("action_type", "")) and target_match:
            plans.advance_step(session_id, plan["id"])
            advanced.append({"npc": npc_id, "step": cur,
                             "note": f"决策兑现计划第 {cur} 步"})
            recorder.note_env(session_id, npc_id,
                              {"type": "计划推进", "target": str(step.get("target", "")),
                               "message": f"AI 决策兑现第 {cur} 步", "outcome": "ok", "changed": True})
    return advanced


def step_world_tick(session_id: str, tick: int, world_id: str,
                    llm_client=None, npcs: list = None, mode: str = "live") -> dict:
    """推进一个 tick（五阶段全量），返回本 tick 摘要（含 recorder 全量）。

    mode（A5 裁决，2026-09-07 设计决定"所有 NPC 都是 AI 控制"）：
      live（在线游戏，默认）：计划=NPC 心智上下文——①前置检查（不执行效果，
        不满足→blocked 上下文）→ ①.5 重规划 → ② 全部存活 NPC 并行 LLM 决策
        （自由行动，计划/情绪/记忆/环境全量上下文）→ ③ 仲裁 + 决策对照推进计划
        → ⑤ 时钟。NPC 与玩家同等自由度。
      plan（离线回放/自检）：计划机械执行（零 LLM、确定性可回放）+ 排班例外
        ——simulate CLI 模式。
    """
    recorder.begin_tick(session_id, tick)
    if npcs is None:
        npcs = db.get_all_npc_ids(world_id)

    try:
        return _step_world_tick_inner(session_id, tick, world_id, llm_client, npcs, mode)
    except Exception as e:  # noqa: BLE001  tick 中途异常：错误显式记录 + 记录照常落库（历史不丢）
        recorder.note_world(session_id, "error", f"tick {tick} 异常中断：{type(e).__name__}: {e}")
        logger.warning("step_world_tick(%s, tick=%s) 异常：%s", session_id, tick, e)
        # 关键（修幽灵tick）：recorder.begin_tick 在 try 前已落库 rec:tick:N，
        # 但 current_tick 推进在 _step_world_tick_inner 末尾（异常时不会执行）——
        # 若不在此处同步推进，会长期停在 N-1（"历史到6、当前却5"）且反复重试坏 tick。
        # 世界时间必须随 recorder 记录前进（就算本 tick 有 NPC 失败，也应标记"已尝试"，
        # 否则一次坏动作让整个世界停滞）。这里用 game_state 当前值判"只前进不回退"。
        cur = int(db.get_game_state_map(session_id).get("current_tick", 0) or 0)
        if tick > cur:
            db.upsert_game_state(session_id, "current_tick", tick)
        rec = recorder.finish_tick(session_id, world_id)
        return {"tick": tick, "npcs": npcs, "replans": [], "decisions": 0,
                "traces": 0, "recorder": rec, "error": str(e)[:300]}


# 对话仲裁挂起态（game_state 键）：`_step_world_tick_inner` 扫描到"想把对话邀请抛给玩家"时，
# 暂停在"已生成决策、未导演结算"，存此待玩家答应/拒绝后再续；resume_pending_tick 读取。
PENDING_CONV_OFFER = "pending_conv_offer"

# 挂起时统一回给玩家的一句话（前端直接展示，避免"世界停着却什么都不说"）。
_PAUSED_NOTICE = "世界停在这一刻——有人在等你回应，先处理对话邀请再继续。"
# 没抢到会话锁时的一句话（上一格还在结算，本次交互没能让时间流逝）。
_SKIPPED_NOTICE = "上一刻还在结算，这一刻没有流逝——稍后再试。"


def pending_offer_fields(session_id: str) -> dict:
    """读"待玩家裁决的对话邀请"挂起态，产出【要透传给前端】的世界时序字段（纯查询）。

    修永久死锁（2026-09-10 实测"输入后卡住、前端毫无提醒"）：
      tick 因某 NPC 想邀玩家对话而挂起时（见 `_step_world_tick_inner` 的 `player_invites`
      分支），世界既不导演也不推时钟；此后每次 `advance_one` 都会在开头撞上这个挂起态、
      直接 return paused。若调用点把返回值丢掉（旧 `/chat`、`/session/move` 就是），
      前端就【完全不知道世界停了】→ 不弹邀请、玩家无从裁决 → 世界永久冻结且毫无提示。

    所以在"动手之前"先问一句"世界是不是正等人裁决"：是 → 不登记意图、不白跑一格，
    直接把挂起原因和待裁决邀请交回前端（前端据此弹"XX 想与你对话 [同意/婉拒]"）。

    Returns:
        {} = 无挂起态；否则含 world_paused / awaiting_conversation / pending_offer / notice。
    """
    offer = db.get_game_state_map(session_id).get(PENDING_CONV_OFFER)
    if not offer:
        return {}
    return {"world_paused": True, "awaiting_conversation": True,
            "pending_offer": offer.get("invites") or [], "notice": _PAUSED_NOTICE}


def world_timing_fields(summary) -> dict:
    """把 `advance_one` 的返回值规约成"透传给前端的世界时序字段"。

    `advance_one` 有三种结果，旧调用点全都当成功处理、把返回值丢掉，于是两类"世界没动"
    都对玩家不可见：
      · summary 是 dict 且 paused=True —— 本格刚产生"邀请玩家对话"→ 挂起（要弹邀请）；
      · summary 是 None —— 没抢到会话锁（上一 tick 还在推）/ 已达 MAX_TICK（要提示稍后再试）；
      · 正常 dict —— 结算完成，返回 {}（调用点按原逻辑处理）。
    """
    if isinstance(summary, dict) and summary.get("paused"):
        return {"world_paused": True,
                "awaiting_conversation": bool(summary.get("awaiting_conversation")),
                "pending_offer": summary.get("pending_offer") or [],
                "notice": _PAUSED_NOTICE}
    if summary is None:
        return {"world_skipped": True, "notice": _SKIPPED_NOTICE}
    return {}


def _settle_live_tick(session_id, tick, decisions, player_intents, world_id,
                      npcs, replan_results, llm_client=None, conv_infos=None) -> dict:
    """live 模式的"导演结算 + 计划推进 + 时钟推进 + 记录收尾"段。

    抽取自 _step_world_tick_inner 的 ③~⑤，供"正常无邀请 tick"与"对话邀请挂起恢复
    (resume_pending_tick)"共用，保证两条路径的结算语义一致（无邀请时行为与旧版完全等价）。
    conv_infos：可选 {player_invites, started_convs, in_conversation}，透传进摘要。
    """
    from . import director
    conv_infos = conv_infos or {"player_invites": [], "started_convs": [], "in_conversation": []}
    events = director.resolve_scenes(session_id, tick, decisions,
                                     player_intents=player_intents,
                                     world_id=world_id, llm_client=llm_client)
    # 09-09：NPC↔NPC 交谈对落影响（导演总结式）。导演为每场景生成一句 narrative，
    # 这里把它作为交谈主题传给 apply_npc_dialogue，写双方记忆/关系微调/一条世界痕迹。
    # 导演结算可能在"同一场景"把多个交谈对聚合成一段 narrative，按场景对应即可。
    scene_narratives = {e.get("scene", ""): str(e.get("narrative", "") or "")
                        for e in (events or []) if isinstance(e, dict) and e.get("scene")}
    from . import conversation as conv_mod
    from . import director as director_mod
    for pair in conv_infos.get("started_convs", []) or []:
        sc = str(pair.get("scene", "") or "")
        narrative = scene_narratives.get(sc, "")
        # T3（v0.4）：导演对话分析——对每对已成立的 NPC↔NPC 交谈，调导演 LLM 分析
        # "讨论了什么 + 对彼此认知/关系/情绪的影响"，经 apply_director_outcome 收口落库
        # （cognition→记忆表、relation→关系收口、emotion→词→PAD→热态）。
        outcome = director_mod.analyze_dialogue(pair, session_id, world_id, llm_client)
        if outcome:
            # 导演成功 → cognition/relation/emotion 已落，只需补一条"交谈"世界痕迹（客观流水），
            # 不再走 apply_npc_dialogue 的记忆部（避免 cognition 印象 + 事件记忆重复写）。
            try:
                director_mod.apply_director_outcome(pair, outcome, session_id, tick)
                conv_mod.record_dialogue_trace(session_id, tick, pair, narrative)
                continue
            except Exception:  # noqa: BLE001  收口失败降级规则版（记忆+痕迹）
                pass
        # 降级：导演分析失败/未产出 → 规则版（记忆 + 世界痕迹；关系不动，防断更）
        conv_mod.apply_npc_dialogue(session_id, tick, pair, narrative=narrative)
    # 自我记忆收口（09-09）：在导演结算/交谈落库【之后】才写"我做了什么/我说了什么"——
    # 此刻 decision 已是递推/导演裁决后的【最终生效动作】，不再出现"想说话却记成说话"的脱节。
    # 逐 agent 记录（含递推后的 wait 兜底），单条失败不阻塞世界推进。
    for d in decisions or []:
        _ag = str(d.get("agent") or "")
        if not _ag:
            continue
        try:
            mind_engine.record_self_action(session_id, _ag, tick, d, world_id)
        except Exception:  # noqa: BLE001  记忆失败不挡结算
            pass
    if decisions:
        advanced = _advance_plan_if_matched(session_id, decisions)
        if advanced:
            recorder.note_world(session_id, "plan_advanced", advanced)
    db.upsert_game_state(session_id, "current_tick", tick)
    traces = [t for t in db.get_recent_traces(session_id, tick, limit=50) if t[0] == tick]
    recorder.note_world(session_id, "traces",
                        [{"actor": t[1], "type": t[2], "target": t[3],
                          "location": t[4], "detail": t[5]} for t in traces])
    recorder.note_world(session_id, "replans", replan_results)
    rec = recorder.finish_tick(session_id, world_id)
    return {"tick": tick, "npcs": npcs, "replans": replan_results,
            "decisions": len(decisions), "traces": len(traces), "recorder": rec,
            "conversations": {"player_invites": conv_infos.get("player_invites", []),
                              "started_convs": conv_infos.get("started_convs", []),
                              "in_conversation": sorted(conv_infos.get("in_conversation", []))}}


def resume_pending_tick(session_id, world_id, llm_client=None, decisions=None) -> dict or None:
    """对话邀请裁决后恢复被挂起的 tick（玩家同意/拒绝后调用）。

    Args:
        decisions: 可选覆盖——拒绝时传"递推后"的决策；同意时传"移除进入对话方"的决策。
                   缺省则用挂起态原样存的决定。
    Returns:
        结算摘要 dict；无挂起态返回 None。
    """
    offer = db.get_game_state_map(session_id).get(PENDING_CONV_OFFER)
    if not offer:
        return None
    db.upsert_game_state(session_id, PENDING_CONV_OFFER, None)
    return _settle_live_tick(session_id, offer["tick"], decisions if decisions is not None else offer["decisions"],
                             offer.get("player_intents", []), offer["world_id"], offer["npcs"],
                             offer.get("replan_results", []), llm_client)


def _step_world_tick_inner(session_id: str, tick: int, world_id: str,
                           llm_client, npcs: list, mode: str) -> dict:
    # ②.6 对话仲裁层结果容器（live 分支覆写；plan 分支保持空——离线回放不对话仲裁）
    conv = {"player_invites": [], "started_convs": [], "in_conversation": set()}
    if mode == "live":
        # ① 计划检视（不执行——执行权在 AI）
        _check_plan_preconditions(session_id, npcs, world_id)
    else:
        # ① 计划执行（确定性，零 LLM）——离线回放模式
        plan_executor.run_tick(session_id, tick, npcs, world_id)

    # ①.5 重规划阶段
    replan_results = _run_replan_phase(session_id, tick, npcs, world_id, llm_client)

    # ② 决策阶段（并行 LLM）
    targets = _decide_targets(session_id, tick, npcs, mode=mode)
    decisions = agent.decide_many(targets, tick, session_id, world_id) if targets else []

    # ②.5 排出玩家意图池（场景导演的玩家一侧）
    player_intents = drain_player_intents(session_id)

    # ③ 结算：live → 场景聚合（多人同场景=导演 AI 集体裁决；单人=确定性执行）
    #         plan（离线回放）→ 旧 arbitrate（确定性+碰撞痕迹）
    if mode == "live":
        from . import conversation
        # 09-09 设计决定：玩家正与其 NPC 对话期间，世界照常推演，但其他 NPC 不去打扰这对。
        # 在对话仲裁【之前】先判"不打扰"：若某 NPC 的意图是靠近/搭话玩家或对话NPC → 改写为
        # wait + 受阻记忆（"看见他俩在聊天，就没去打扰"），避免对话期间又对玩家产生邀请。
        decisions = conversation.marks_no_disturb(session_id, tick, decisions, world_id)
        # 09-09（对话中NPC自身受限链）：marks_no_disturb 跳过对话者本身（ag==talk_npc continue），
        # 否则被玩家邀请对话的 NPC 本 tick 的 move/observe 等意图会照常执行（"一边对话一边走开"
        # 破窗）。这里对对话中 NPC 自身再补一道最强约束：离开/观察/接触类意图受阻并递推。
        decisions = conversation.marks_talking_npc_constrain(session_id, tick, decisions, world_id)
        # ②.6 对话仲裁层（v0.4）：扫描本 tick 的 speak → 邀请 → 同意/拒绝 → 按优先级递推。
        # 零 LLM（关系/规则判定）；无任何对话邀请时 final_decisions 与原 decisions 全等（零改动短路）。
        conv = conversation.resolve_conversations(session_id, tick, decisions,
                                                  player_intents=player_intents)
        decisions = conv["final_decisions"]

        # 有"想跟玩家说话"的邀请 → 本 tick 暂停（挂起态），邀请抛给前端等玩家答应/拒绝，
        # 不导演结算、不推时钟——否则会被导演一次性文学化呈现，失去"邀请→应答→多轮"分层。
        if conv["player_invites"]:
            db.upsert_game_state(session_id, PENDING_CONV_OFFER, {
                "tick": tick, "world_id": world_id, "npcs": npcs,
                "replan_results": replan_results, "decisions": decisions,
                "player_intents": player_intents, "invites": conv["player_invites"],
            })
            return {"paused": True, "tick": tick, "world_id": world_id,
                    "npcs": npcs, "replans": replan_results,
                    "pending_offer": conv["player_invites"]}

        return _settle_live_tick(session_id, tick, decisions, player_intents,
                                 world_id, npcs, replan_results, llm_client,
                                 conv_infos={"player_invites": conv["player_invites"],
                                             "started_convs": conv["started_convs"],
                                             "in_conversation": conv["in_conversation"]})

    # plan（离线回放）：旧《确定性 arbitrate + 碰撞痕迹》——收尾与 live 不同（无导演）
    if decisions:
        decisions.sort(key=lambda d: str(d.get("agent", "")))
        fate.arbitrate(session_id, tick, decisions, world_id=world_id)
        # 自我记忆收口（09-09）：与 live 分支同一口径，在 deterministic 落库后按
        # 【实际生效决策】记录"我做了什么"，保证离线回放与在线一致（不再记 plan[0] 意图）。
        for d in decisions:
            _ag = str(d.get("agent") or "")
            if not _ag:
                continue
            try:
                mind_engine.record_self_action(session_id, _ag, tick, d, world_id)
            except Exception:  # noqa: BLE001  记忆失败不挡结算
                pass
    db.upsert_game_state(session_id, "current_tick", tick)
    traces = [t for t in db.get_recent_traces(session_id, tick, limit=50) if t[0] == tick]
    recorder.note_world(session_id, "traces",
                        [{"actor": t[1], "type": t[2], "target": t[3],
                          "location": t[4], "detail": t[5]} for t in traces])
    recorder.note_world(session_id, "replans", replan_results)
    rec = recorder.finish_tick(session_id, world_id)
    return {"tick": tick, "npcs": npcs, "replans": replan_results,
            "decisions": len(decisions), "traces": len(traces), "recorder": rec,
            "conversations": {"player_invites": [], "started_convs": [], "in_conversation": []}}


def _log_paused_tick(session_id: str, tick: int, reason: str) -> None:
    """挂起 tick 也落一条 paused 记录（保证调试面板 ticks_available 编号连续，不跳号）。

    仅当"该 tick 尚未被完整记录"时才落——用 recorder 的惰性/归档机制写出一个
    paused 标记。不推进 current_tick（世界确实没前进），只是让历史记录里这个编号
    在位、事后会被正常结算覆盖成完整记录。失败静默（记录是辅助观测，不阻塞推进）。
    """
    try:
        recorder.begin_tick(session_id, tick)
        recorder.note_world(session_id, "paused", reason)
        recorder.finish_tick(session_id, "")
    except Exception:  # noqa: BLE001  挂起记录是辅助，失败不阻塞
        pass


def advance_one(session_id: str, world_id: str, llm_client=None) -> dict or None:
    """从 game_state.current_tick 推进一个 tick（在线驱动的入口）。

    Returns:
        step_world_tick 的摘要 dict；None = 已有 tick 在推进中（会话锁未抢到）
        或已达 MAX_TICK。
    """
    lock = _session_lock(session_id)
    if not lock.acquire(blocking=False):
        logger.info("[%s] 上一 tick 仍在推进，本次交互跳过世界推进", session_id)
        return None
    try:
        gs = db.get_game_state_map(session_id)
        current = int(gs.get("current_tick", 0) or 0)
        # 09-09 设计决定：玩家与 NPC 对话【不再冻结世界】——对话期间世界照常按 tick 后台
        # 自推演（其余 NPC 照常活动、导演照常调度），只是把"玩家+正在对话的该 NPC"标为
        # 交谈中（见 _step_world_tick_inner 的 active_conv 处理），其他 NPC 不打扰这对人。
        # 因此这里不再因 get_conversation 而提前返回 paused。
        offer = gs.get(PENDING_CONV_OFFER)
        if offer:
            _log_paused_tick(session_id, current + 1, "awaiting_conversation")
            return {"paused": True, "awaiting_conversation": True, "tick": current,
                    "pending_offer": (offer.get("invites") or [])}
        if current >= scheduler.MAX_TICK:
            return None
        return step_world_tick(session_id, current + 1, world_id, llm_client=llm_client, mode='live')
    finally:
        lock.release()


def auto_advance_async(session_id: str, world_id: str, llm_client=None) -> None:
    """交互后自动推进（main.py 在回复写回后调用）：后台线程跑一个 tick，不阻塞玩家。

    "挂机不推进、交互才流逝"——与客户端 Clock.gd 的设计约定一致；
    会话锁在 advance_one 内（非阻塞获取），连发消息不会叠多个 tick。

    09-10（删文学旁白）：场景叙事统一由导演 player_view 承担（经 /world/updates 增量回前端），
    本函数仅后台推一格世界，不再预生成 conv_end 旁白。
    """
    def _run():
        try:
            summary = advance_one(session_id, world_id, llm_client=llm_client)
        except Exception as e:  # noqa: BLE001  世界推进失败不影响玩家主链路
            logger.warning("auto_advance 失败：%s", e)

    threading.Thread(target=_run, daemon=True, name=f"world-tick-{session_id}").start()


# ---------------------------------------------------------------------------
# 玩家意图池（场景导演 / 设计裁决）：玩家改变环境的行动不再"说了立刻发生"，
# 而是登记进意图池，随下一 tick 与 NPC 行动按场景聚合裁决——多人同场景时
# 进"场景导演 AI"集体判定（你扑空/他溜走/两败俱伤……由导演裁定）。
# 观察类动作不受影响（立即返回）。
# ---------------------------------------------------------------------------

def register_player_intent(session_id: str, intent_dict: dict, player_text: str,
                           scene: str, world_id: str = "test") -> int:
    """登记玩家的改变环境意图（main.py /chat 环境路径调用，替代立即执行）。

    Returns: 当前池中意图数（供回复文案）。
    """
    key = f"pending_intents:{session_id}"
    pool = db.get_game_state_map(session_id).get(key)
    pool = pool if isinstance(pool, list) else []
    pool.append({"intent": intent_dict, "text": str(player_text)[:200], "scene": scene})
    db.upsert_game_state(session_id, key, pool)
    return len(pool)


def drain_player_intents(session_id: str) -> list:
    """排干意图池（tick 结算阶段调用；排出的意图随本 tick 裁决）。"""
    key = f"pending_intents:{session_id}"
    pool = db.get_game_state_map(session_id).get(key)
    pool = pool if isinstance(pool, list) else []
    if pool:
        db.upsert_game_state(session_id, key, [])
    return pool
