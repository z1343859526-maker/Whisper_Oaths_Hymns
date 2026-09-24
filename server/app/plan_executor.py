"""M1.5 计划执行器：把 BDI 计划翻译成机器可执行的"世界状态变更"。

职责（M1.4 校验器 / M1.5 执行器 / M1.6 重规划器的中间环）：
- 主循环每 tick 调用 run_tick(session_id, tick)，遍历该会话全部 active 计划；
- 对每个计划：判断当前 tick 是否命中 current_step 的执行窗口 → 判 preconditions
  → 全满足则落 effects（写世界）+ 写 world_trace + advance_step 推进；
  → 任一 precondition 不满足则 mark_status('blocked')，交给 M1.6 重规划。

与 agent.py 的分工：
- agent.decide   = LLM 决策（计划外/无计划/排班命中的"例外唤醒"）；
- 本模块          = 计划内执行（零 LLM、确定性、可回放——这是"涌动引擎"确定性的一半）
- fate.arbitrate = LLM 决策结果的命运判定；计划执行的痕迹不走它，直接落库。

执行窗口语义：
- time_window 是 [start, end] 闭区间，表示"这一步该在哪个时间带内完成"。
- 我们约定"窗口内执行一次"：用 game_state['plan_progress:{plan_id}'] 记录
  每一步最后一次执行的 tick，只有（窗口未过期 && 该步未执行过）才执行，
  避免每 tick 重复落效果。同一步若前置一直不满足，会持续判定直到窗口结束才 block。

steps 契约（与 app/plans.py 模块注释完全一致，这里按契约实现，不另起炉灶）。
"""
import json
import logging

from . import db
from . import plans
from . import recorder

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# precondition 判定：把 plans 契约的 check 枚举翻译成"读一次世界状态"
# ---------------------------------------------------------------------------
def _npc_still_alive(session_id, npc_id) -> bool:
    """NPC 存活 = 状态 dict 里没有 dead=True（缺省即活着）。"""
    status = db.get_npc_status(session_id, npc_id)
    return not bool(status.get("dead", False))


def _precondition_met(session_id, pre, actor, world_id="golden") -> bool:
    """判一条前置条件是否满足。缺省替 actor = 计划主人（has_item 无 holder 时）。

    world_id：环境卡世界维度（004 迁移后），环境/物品状态按世界读取——
    测试世界男人的计划只读 test 的环境卡，绝不串到黄金乡。
    """
    check = pre["check"]
    if check == "env_state":
        st = json.loads(db.get_environment_state(pre["target"], world_id) or "{}")
        return st.get(pre["key"]) == pre["expected"]
    if check == "has_item":
        item_env = pre["item"]
        holder = pre.get("holder", actor)  # 缺省=计划主人
        st = json.loads(db.get_environment_state(item_env, world_id) or "{}")
        return st.get("holder") == holder
    if check == "npc_alive":
        return _npc_still_alive(session_id, pre["target"]) is pre["expected"]
    if check == "npc_status":
        status = db.get_npc_status(session_id, pre["target"])
        return status.get(pre["key"]) == pre["expected"]
    # 未知 check：计划校验器(M1.4)已拦截，这里防御性返回 False（不放过）
    logger.warning("未知前置 check=%s（M1.4 校验应已拦截）", check)
    return False


# ---------------------------------------------------------------------------
# effect 落库：把 plans 契约的 set 枚举翻译成"写一次世界状态"
# ---------------------------------------------------------------------------
def _apply_effect(session_id, tick, eff, actor, world_id="golden"):
    """落一条效果的副作用：env_state 写环境卡单 key；npc_status 写游戏状态。
    move 型动作的"移动位置"由 run_tick 额外处理（依赖 action_type，不属于 set 枚举）。
    world_id：环境卡世界维度（004 迁移后），与 _precondition_met 对齐。
    """
    setter = eff["set"]
    if setter == "env_state":
        # 物品转移（holder/state）与环境状态统一走这里；区分不了谁在拿，位置由 move 处理
        db.patch_environment_state(eff["target"], eff["key"], eff["value"], world_id)
    elif setter == "npc_status":
        # 目标是 NPC：npc_status:test_woman.dead=True 等；支持 delay_tick（到点才生效）
        db.set_npc_status(session_id, eff["target"], eff["key"], eff["value"])
    else:
        logger.warning("未知效果 set=%s（M1.4 校验应已拦截）", setter)


# ---------------------------------------------------------------------------
# 窗口命中判定：tick 是否在时间带内，且这一步还没执行过
# ---------------------------------------------------------------------------
def _window_hit(session_id, tick, plan, step) -> bool:
    """返回 (该步可否现在执行)。核心是防重复：同一步窗口内只执行一次。"""
    t0, t1 = step["time_window"]
    if not (t0 <= tick <= t1):
        return False  # 未到窗口 / 已过期
    # 该步是否已在窗口内执行过：查记录
    progress = db.get_game_state_map(session_id).get(f"plan_progress:{plan['id']}")
    progress = progress if isinstance(progress, dict) else {}
    return progress.get(str(step["step"])) is None


def _mark_step_done(session_id, plan_id, step, tick):
    """记录某步已在窗口内执行（防重）。"""
    key = f"plan_progress:{plan_id}"
    raw = db.get_game_state_map(session_id).get(key)
    progress = dict(raw) if isinstance(raw, dict) else {}
    progress[str(step["step"])] = tick
    db.upsert_game_state(session_id, key, progress)


def _make_detail(step, actor) -> str:
    """把一步翻译成人话旁白（落 world_trace.detail + 打印）。"""
    note = step.get("note", "")
    scene = step.get("scene", "")
    action = step["action_type"]
    if action == "move":
        target = step.get("target", "")
        base = f"{actor} → {target}" if target else f"{actor} 移动"
    elif action == "use_item":
        base = f"{actor} 使用 {step.get('target', '')}"
    elif action == "interact":
        base = f"{actor} 与 {step.get('target', '')} 互动"
    elif action == "trigger_event":
        base = f"{actor} 触发事件于 {step.get('target', '')}"
    elif action == "converse":
        base = f"{actor} 与 {step.get('target', '')} 交谈"
    else:
        base = f"{actor} {action}@{scene}"
    return f"{base}；{note}" if note else base


def _run_plan(session_id, tick, plan, world_id="golden"):
    """执行一个计划在当前 tick 的一步。

    Returns:
        str | None：'executed' 产生实际动作、'blocked' 前置不满足被冻结、
        None 未到窗口（本 tick 该 NPC 不动）。供 run_tick 汇总统计。
    world_id：计划所属世界——环境/物品状态读写按世界隔离（004 迁移后）。
    """
    step = plan["steps"][plan["current_step"] - 1]
    actor = plan["npc_id"]

    if not _window_hit(session_id, tick, plan, step):
        return None  # 未到窗口，本 tick 该 NPC 不动

    # 判前置：全部满足才执行
    unmet = [pre for pre in step.get("preconditions", [])
             if not _precondition_met(session_id, pre, actor, world_id)]
    if unmet:
        # 前置不满足 → block（M1.6 重规划入口）。但只在窗口内的最后判定才报，
        # 避免每次都刷 blocked 日志；这里直接标记，窗口内多次未满足重复标记无害。
        reasons = []
        for pre in unmet:
            why = f"前置不满足: {pre.get('check')}:{pre.get('target')}.{pre.get('key')}!=<expected>"
            reasons.append(why + " " + json.dumps(pre, ensure_ascii=False))
        plans.mark_status(session_id, plan["id"], "blocked",
                          blocked_reason="; ".join(reasons))
        logger.info("[%s] 计划 %s/%s blocked：%s", tick, plan["npc_id"],
                    plan["goal"], "; ".join(reasons))
        recorder.note_env(session_id, actor,
                          {"type": "计划受阻", "target": step.get("target", ""),
                           "message": "; ".join(reasons)[:120],
                           "outcome": "blocked", "changed": False})
        return "blocked"

    # 前置满足 → 落效果
    for eff in step.get("effects", []):
        _apply_effect(session_id, tick, eff, actor, world_id)

    # move 型：额外维护 NPC 当前位置（M1.7 感知/可达性的数据基础）
    if step["action_type"] == "move":
        db.set_npc_pos(session_id, actor, step.get("target", ""))

    # 写世界痕迹（计划执行的确定性日志，可回放/审计）
    db.add_world_trace(session_id, tick, actor, step["action_type"],
                       step.get("target", ""), step.get("scene", ""),
                       _make_detail(step, actor))

    # 标记该步已执行 + 推进计划
    _mark_step_done(session_id, plan["id"], step, tick)
    new_plan = plans.advance_step(session_id, plan["id"])
    # recorder：计划执行也进上帝视角（此前只有 LLM 决策有记录，面板看不见计划 NPC）
    recorder.note_env(session_id, actor,
                      {"type": "计划执行", "target": step.get("target", ""),
                       "message": _make_detail(step, actor),
                       "outcome": "ok", "changed": True})
    # 任何按计划执行的 NPC 都打印（M1.5 泛化：不再只打测试男，正式版也要可见）
    logger.info("[%s] %s 执行第%s步 %s → 当前推进到 step %s",
                tick, actor, step["step"], step["action_type"],
                new_plan["current_step"])
    return "executed"


# ---------------------------------------------------------------------------
# 对外入口：主循环每 tick 调用
# ---------------------------------------------------------------------------
def run_tick(session_id, tick, npc_ids, world_id="golden") -> dict:
    """本 tick 驱动所有 active 计划执行；返回 {npc_id: 是否产生动作}。

    npc_ids：本世界参与模拟的角色池（caller 已按 world_id 过滤）——
    只对"池内且有 active 计划"的 NPC 执行，其余交给 agent 排班/例外唤醒。
    world_id：计划所属世界——环境/物品状态读写按世界隔离（004 迁移后）。
    Returns:
        执行统计 dict：{"executed": n, "blocked": m} 供 simulate 打印。
    """
    executed = 0
    blocked = 0
    for plan in plans.get_active_plans(session_id):
        if plan["npc_id"] not in npc_ids:
            continue  # 非本世界角色（防御：世界池已由调用方过滤到这里再兜一层）
        outcome = _run_plan(session_id, tick, plan, world_id)
        if outcome == "executed":
            executed += 1
        elif outcome == "blocked":
            blocked += 1
    return {"executed": executed, "blocked": blocked}


# 供 simulate 判断"某 NPC 是否有 active 计划"（决定是否跳过 agent 决策）
def has_active_plan(session_id, npc_id) -> bool:
    return plans.get_active_plan(session_id, npc_id) is not None
