"""重规划器（T6，M1.6 的心智化实装）：计划受阻/无计划 → LLM 生成新 steps → 落库接棒。

定位（§1.4 架构裁决 + §5.6 计划检视）：
- 计划 = NPC 私有意图；检视（resolve_plan，纯 code）决定"要不要重估、什么策略"，
  本模块执行"换手段/换目标/从零起"的 **LLM 生成与落库**；
- LLM 预算：一次重评估 ≤1 次调用；多 NPC 同时受阻时走 chat_many 并行；
- 可回放：重评估的触发（blocked 状态）是确定性 code 判定，LLM 只产出 steps，
  产物必须过 plans.validate_plan_schema（非法绝不入库——M1.4 同一道闸）。

与环境管线的结合（本次的核心）：重规划 prompt 注入**真实空间事实**——
NPC 当前位置（npc_pos）、所在场景的实体（entities_in）、可达房间
（spatial.connected_scenes/can_reach）——LLM 在"真实可走的路、真实可拿的物"
里规划，产出的 move/use_item 步骤是环境执行器真的能执行的。
"""
import json
import logging

from . import db
from . import mental
from . import plans
from . import spatial
from . import scheduler

logger = logging.getLogger(__name__)


def _npc_env_summary(session_id: str, npc_id: str, world_id: str) -> str:
    """NPC 视角的环境事实（重规划 prompt 的"地形"段）：所在/在场物/可达房间。"""
    scene = db.get_npc_pos(session_id, npc_id) or "未知"
    lines = [f"你当前所在：{scene}"]
    try:
        ents = spatial.entities_in(scene, world_id)
        if ents:
            names = "、".join(f"{e.get('env_id')}({e.get('name')})" for e in ents[:8])
            lines.append(f"这里能碰到的东西：{names}")
    except Exception:  # noqa: BLE001  无空间骨架的世界退化为环境卡
        pass
    try:
        connected = spatial.connected_scenes(scene, world_id) if scene != "未知" else []
        if connected:
            reachable = [c for c in connected if spatial.can_reach(scene, c, world_id)]
            lines.append(f"你现在能去的房间：{'、'.join(reachable) if reachable else '（暂无，门都关着）'}")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines)


def _mental_summary(mental_ctx: dict or None) -> str:
    """心智中间态的人话摘要（情绪/目标状态），重规划要"顺着这个人"来。"""
    if not mental_ctx:
        return ""
    state = mental_ctx.get("state") or {}
    parts = []
    if state.get("emotion_word"):
        parts.append(f"你此刻{state['emotion_word']}")
    wm = [str(w) for w in (state.get("working_memory") or [])][-2:]
    if wm:
        parts.append("；".join(wm))
    return "。".join(parts)


def build_replan_messages(npc_id: str, plan: dict, trigger_desc: str,
                          mental_ctx: dict or None, world_id: str = "test") -> list:
    """构造重规划 LLM 调用：给足真实世界事实 + 计划契约，要求只输出新 steps JSON。"""
    mm = db.get_mental_model(npc_id) or {}
    kernel = mm.get("kernel", {}) if isinstance(mm.get("kernel"), dict) else {}
    goal = str(plan.get("goal", ""))
    blocked_reason = str(plan.get("blocked_reason", "") or trigger_desc)
    old_steps = plan.get("steps") or []
    old_brief = "；".join(
        f"{s.get('action_type')}->{s.get('target') or s.get('scene')}" for s in old_steps[:5])

    env_summary = ""
    if mental_ctx and mental_ctx.get("session_id"):
        env_summary = _npc_env_summary(mental_ctx["session_id"], npc_id, world_id)
    mental_summary = _mental_summary(mental_ctx)

    system = (
        "你是叙事引擎的「计划重规划器」。NPC 的计划受阻，你要为它生成一条【新的行动计划】。\n"
        "只输出一个 JSON 数组（steps），不要任何其它文字。每一步的格式：\n"
        '{"step":1,"action_type":"move|use_item|give_item|speak|observe|wait|interact|trigger_event|converse",'
        '"target":"对象id","scene":"发生地id","time_window":[起始tick,结束tick],'
        '"preconditions":[],"effects":[],"note":"这步在干什么"}\n'
        "契约要点：\n"
        "- time_window 是相对现在的 [起,止] 闭区间（每格=10分钟），必须给且递增不重叠；\n"
        "- preconditions/effects 只能用这些 check/set：env_state(键值判定/写入)、"
        "has_item(持有判定)、npc_alive、npc_status；effects 的 set 支持 env_state/npc_status；\n"
        "- target/scene 必须用下面给出的真实 id（不要发明不存在的房间/物品）；\n"
        "- 计划要绕开受阻原因，优先换手段保住目标；步数 2~5 步，别贪多。"
    )
    user_parts = [
        f"【角色】{npc_id}" + (f"（最看重：{kernel.get('value_tree', {}).get('name', '')}）"
                               if isinstance(kernel.get("value_tree"), dict) else ""),
        f"【目标（不许换）】{goal}",
        f"【受阻原计划（前几步）】{old_brief or '（无）'}",
        f"【受阻原因/触发】{blocked_reason} / {trigger_desc}",
    ]
    if mental_summary:
        user_parts.append(f"【它的状态】{mental_summary}")
    if env_summary:
        user_parts.append(f"【真实环境（只能用这些 id）】\n{env_summary}")
    user_parts.append("请输出新的 steps JSON 数组。")
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(user_parts)}]


def parse_steps(raw: str) -> list or None:
    """解析 LLM 产物 → steps 列表；非法（结构/未过 schema 校验）→ None。"""
    import re
    text = (raw or "").strip()
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return None
    try:
        steps = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(steps, list) or not steps:
        return None
    probe = {"npc_id": "probe", "goal": "probe", "steps": steps}
    if plans.validate_plan_schema(probe):
        return None  # 校验不过 → 拒收（非法产物绝不入库）
    # 规范化：step 序号重排、time_window 平移到当前 tick 之后由调用方处理
    for i, s in enumerate(steps, 1):
        s["step"] = i
    return steps


def _shift_windows(steps: list, current_tick: int) -> list:
    """把 LLM 给的相对窗口平移到绝对 tick（LLM 常给 0 起的相对区间）。"""
    out = []
    for s in steps:
        s = dict(s)
        tw = s.get("time_window") or [0, 4]
        if isinstance(tw, list) and len(tw) == 2:
            s["time_window"] = [int(tw[0]) + current_tick, int(tw[1]) + current_tick]
        out.append(s)
    return out


def replan(session_id: str, npc_id: str, old_plan: dict, trigger_desc: str,
           mental_ctx: dict or None, world_id: str = "test",
           llm_client=None, raw_override: str = None, now_tick: int = None) -> dict:
    """一次重评估的完整执行：prompt → LLM → 校验 → replace_plan 落库。

    Args:
        old_plan: plans.get_blocked_plans 里的一条（含 id/goal/steps/blocked_reason）。
        raw_override: 测试注入（跳过真实 LLM）。
    Returns:
        {"ok": bool, "decision": 策略, "reason": str, "plan_id": 新计划id|None}
    """
    mm = db.get_mental_model(npc_id)
    kernel = mm.get("kernel", {}) if mm and isinstance(mm.get("kernel"), dict) else {}
    # 策略先过检视（tenacity/flexibility/价值粘性）——LLM 只在策略允许时被调用
    policy = mental.resolve_plan(old_plan, mm, kernel, block_reasons=[old_plan.get("blocked_reason") or trigger_desc])
    if policy["decision"] in ("wait_and_watch", "abandon", "continue"):
        if policy["decision"] == "abandon":
            plans.mark_status(session_id, old_plan["id"], "abandoned",
                              blocked_reason=str(old_plan.get("blocked_reason", ""))[:80])
        return {"ok": False, "decision": policy["decision"], "reason": policy["reason"], "plan_id": None}

    if raw_override is None and llm_client is not None:
        raw = llm_client.chat(build_replan_messages(npc_id, old_plan, trigger_desc, mental_ctx, world_id))
    else:
        raw = raw_override
    steps = parse_steps(raw)
    if not steps:
        # 产物非法：计划保持 blocked（下 tick 可重试），策略记录留档
        return {"ok": False, "decision": "replan_means", "reason": "重规划产物非法，计划保持受阻", "plan_id": None}
    # 当前 tick：优先用调用方给的（重规划发生在 tick 内、⑤ 时钟推进之前——
    # 用 game_state 会拿到上一 tick，窗口会平移错一格）
    current_tick = now_tick if now_tick is not None else         int((db.get_game_state_map(session_id) or {}).get("current_tick", 0) or 0)
    steps = _shift_windows(steps, current_tick)
    try:
        plans.replace_plan(session_id, old_plan["id"], steps,
                           blocked_reason=str(old_plan.get("blocked_reason", ""))[:80])
    except Exception as e:  # noqa: BLE001  PlanValidationError 等
        return {"ok": False, "decision": "replan_means", "reason": f"落库失败：{e}", "plan_id": None}
    logger.info("[T6] %s 重规划成功：v%d 替换 v%d（%s）",
                npc_id, int(old_plan.get("version", 1)) + 1, old_plan.get("version", 1), trigger_desc[:40])
    return {"ok": True, "decision": "replan_means", "reason": policy["reason"], "plan_id": None}


def build_scratch_messages(npc_id: str, goal: dict, mental_ctx: dict or None,
                           world_id: str = "test") -> list:
    """"从 0 起"规划（有 Desire 无 Intention，如 test_woman）：目标+处境 → 首个计划。"""
    mm = db.get_mental_model(npc_id) or {}
    kernel = mm.get("kernel", {}) if isinstance(mm.get("kernel"), dict) else {}
    env_summary = ""
    if mental_ctx and mental_ctx.get("session_id"):
        env_summary = _npc_env_summary(mental_ctx["session_id"], npc_id, world_id)
    mental_summary = _mental_summary(mental_ctx)
    system = (
        "你是叙事引擎的「初始规划器」。这个 NPC 有目标但还没有任何行动计划——"
        "请为它生成第一条从当前处境出发的行动计划。\n"
        "只输出一个 JSON 数组（steps），格式与要点同重规划器："
        '每步 {"step","action_type","target","scene","time_window":[起,止],'
        '"preconditions","effects","note"}；action_type 只能是 '
        '"move|use_item|give_item|speak|observe|wait|interact|trigger_event|converse"；'
        "time_window 相对现在的 [起,止]；"
        "target/scene 只用给出的真实 id；步数 2~4 步。"
    )
    user_parts = [
        f"【角色】{npc_id}" + (f"（最看重：{kernel.get('value_tree', {}).get('name', '')}）"
                               if isinstance(kernel.get("value_tree"), dict) else ""),
        f"【它的目标】{goal.get('goal_id')}：{'；'.join(goal.get('plan') or []) or goal.get('note', '')}",
    ]
    if mental_summary:
        user_parts.append(f"【它的状态】{mental_summary}")
    if env_summary:
        user_parts.append(f"【真实环境（只能用这些 id）】\n{env_summary}")
    user_parts.append("请输出 steps JSON 数组。")
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(user_parts)}]


def plan_from_scratch(session_id: str, npc_id: str, goal: dict, mental_ctx: dict or None,
                      world_id: str = "test", llm_client=None, raw_override: str = None,
                      now_tick: int = None) -> dict:
    """有 Desire 无 Intention → 生成首个计划并落库（plans.create_plan，v1, source=llm）。"""
    if raw_override is None and llm_client is not None:
        raw = llm_client.chat(build_scratch_messages(npc_id, goal, mental_ctx, world_id))
    else:
        raw = raw_override
    steps = parse_steps(raw)
    if not steps:
        return {"ok": False, "reason": "初始规划产物非法"}
    current_tick = now_tick if now_tick is not None else         int((db.get_game_state_map(session_id) or {}).get("current_tick", 0) or 0)
    steps = _shift_windows(steps, current_tick)
    try:
        goal_text = "；".join(goal.get("plan") or []) or str(goal.get("note", "")) or goal.get("goal_id", "目标")
        new_id = plans.create_plan(session_id, npc_id, goal.get("goal_id", "goal"),
                                   goal_text, "normal", steps, source="llm")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"落库失败：{e}"}
    # 出生地兜底：无计划 NPC 没有 npc_pos（环境系统文档 §8.7）——按新计划第一步 scene 定位
    first_scene = (steps[0] or {}).get("scene", "")
    if first_scene and not db.get_npc_pos(session_id, npc_id):
        db.set_npc_pos(session_id, npc_id, first_scene)
    logger.info("[T6] %s 从零起规划成功：goal=%s，v1（%d 步）", npc_id, goal.get("goal_id"), len(steps))
    return {"ok": True, "plan_id": new_id, "steps": steps}
