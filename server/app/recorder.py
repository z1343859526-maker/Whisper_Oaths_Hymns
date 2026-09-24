"""上帝视角记录器（在线世界时钟 / 用户要求的调试观测面）。

每 tick 每 NPC 记录六件事：
  ① 得知了什么（noticed/情绪词/工作记忆尾部）
  ② 给 AI 的提示词（decide 的完整 messages）
  ③ 实际做出的行动（多选标签：移动/对话/观察/改变环境/使用道具/给予/触发事件/等待）
  ④ AI 最后输出（decision dict 原文）
  ⑤ 环境影响变化（execute_npc_action 结果 + LLM 反应仲裁的 effects）
  ⑥ LLM 各段耗时（decide_ms / reaction_ms，毫秒）

存储：game_state KV `rec:tick:{n}`（值 = {npc_id: 记录} dict + world 摘要）——
零新表（MVP 不为调试面加迁移），单 key ≈ 一 tick 全量，按 tick 随取随用。

线程模型：decide_many 并发写 → 模块级 Lock；"当前 tick 上下文"按 session 键控
（一个会话同一时刻只推一个 tick，在线驱动层已有会话级互斥）。
"""
import json
import threading

from . import db

_lock = threading.RLock()  # 可重入：note_* 惰性自建上下文时会再进锁
# {session_id: {"tick": n, "npcs": {npc_id: {...}}, "replans": [...]}}
_current: dict = {}

ACTION_LABELS = {
    "move": "移动", "speak": "对话", "observe": "观察", "use_item": "使用道具",
    "give_item": "给予", "interact": "改变环境", "trigger_event": "触发事件", "wait": "等待",
}


def action_tags(decision: dict) -> list:
    """decision → 行动多选标签（人话，供面板/审计）：主类型 + 有台词加'对话'。"""
    tags = []
    atype = str(((decision or {}).get("action") or {}).get("type", "wait"))
    label = ACTION_LABELS.get(atype, atype)
    if label not in tags:
        tags.append(label)
    if decision.get("speech") and "对话" not in tags:
        tags.append("对话")
    return tags


def begin_tick(session_id: str, tick: int) -> None:
    with _lock:
        # 归档残留：上一个 tick 若未正常 finish（中途异常/直接 decide），先落库再开新夹——
        # 记录永不因覆盖而丢失（用户要求：历史永久保留）
        leftover = _current.get(session_id)
        if leftover and (leftover.get("npcs") or leftover.get("replans") or leftover.get("world")):
            db.upsert_game_state(session_id, f"rec:tick:{leftover['tick']}", leftover)
        _begin_locked(session_id, tick)


def _begin_locked(session_id: str, tick: int) -> None:
    """begin_tick 的无锁内核（调用方须已持 RLock）。"""
    _current[session_id] = {"tick": tick, "npcs": {}, "replans": [], "world": {}}


def _rec(session_id: str, npc_id: str, tick: int = None) -> dict:
    """取/建某 NPC 的记录槽；无 tick 上下文时惰性自建（以 game_state 当前 tick 为准）——
    保证任何调用顺序（如直接调 agent.decide 而未经 step_world_tick）记录都不丢。"""
    cur = _current.get(session_id)
    if cur is None:
        # 惰性创建：优先用调用方声明的 tick（decide 明确知道自己在为哪个 tick 决策），
        # 缺省回退 game_state.current_tick
        if tick is None:
            gs = db.get_game_state_map(session_id)
            tick = int(gs.get("current_tick", 0) or 0)
        _begin_locked(session_id, int(tick))
        cur = _current[session_id]
    rec = cur["npcs"].get(npc_id)
    if rec is None:
        rec = {"npc_id": npc_id}
        cur["npcs"][npc_id] = rec
    return rec


def note_learned(session_id: str, npc_id: str, learned: dict, tick: int = None) -> None:
    """① 得知了什么（agent.decide 在 process_observation 后调用）。"""
    with _lock:
        _rec(session_id, npc_id, tick).setdefault("learned", {}).update(learned or {})


def note_prompt(session_id: str, npc_id: str, messages: list, ms: float, output: dict,
                tick: int = None) -> None:
    """②④⑥ 一次 decide 调用三合一：提示词 + 耗时 + AI 输出（含行动标签③）。"""
    with _lock:
        rec = _rec(session_id, npc_id, tick)
        rec["prompt"] = [{"role": m.get("role"), "content": str(m.get("content"))}
                         for m in (messages or [])]
        rec["decide_ms"] = round(float(ms), 1)
        rec["output"] = output
        rec["action_tags"] = action_tags(output)


def note_env(session_id: str, npc_id: str, env_result: dict, reaction_ms: float = None,
             reaction_effects: list = None) -> None:
    """⑤ 环境影响（fate 落库时调用）：执行器结果 + LLM 反应仲裁效果与耗时。"""
    with _lock:
        rec = _rec(session_id, npc_id)
        rec.setdefault("env", []).append(env_result or {})
        if reaction_ms is not None:
            rec["reaction_ms"] = round(float(reaction_ms), 1)
        if reaction_effects:
            rec.setdefault("reaction_effects", []).extend(reaction_effects)


def note_error(session_id: str, npc_id: str, phase: str, error: str,
               messages: list = None, ms: float = None, tick: int = None) -> None:
    """LLM 交互失败（没收到回复）的显式记录——错误不静默（用户要求）。

    Args:
        phase: "decide"（决策调用失败）/ "reaction"（反应仲裁失败）/ "observe" 等。
        messages: 当时发给 AI 的提示词（失败也要留档——"发了什么没收到回复"）。
    """
    with _lock:
        rec = _rec(session_id, npc_id, tick)
        err = {"phase": phase, "error": str(error)[:500]}
        if messages:
            err["prompt"] = [{"role": m.get("role"), "content": str(m.get("content"))[:2000]}
                             for m in (messages or [])]
        if ms is not None:
            err["ms"] = round(float(ms), 1)
        rec.setdefault("errors", []).append(err)
        # 兜底决策也留档（wait），让面板能看到"这次它没能思考"
        rec["action_tags"] = ["等待（AI 未响应）"]


def note_replan(session_id: str, entry: dict) -> None:
    """重规划/从零起结果（world 层调用）。"""
    with _lock:
        cur = _current.get(session_id)
        if cur is not None:
            cur["replans"].append(entry)


def note_world(session_id: str, key: str, value) -> None:
    """tick 级世界摘要（traces 数等，world 层调用）。"""
    with _lock:
        cur = _current.get(session_id)
        if cur is not None:
            cur["world"][key] = value


def finish_tick(session_id: str, world_id: str) -> dict:
    """落库本 tick 全量记录（game_state KV `rec:tick:{n}`）并返回。"""
    with _lock:
        cur = _current.pop(session_id, None)
    if not cur:
        return {}
    db.upsert_game_state(session_id, f"rec:tick:{cur['tick']}", cur)
    return cur


def get_tick(session_id: str, tick: int) -> dict:
    """读某 tick 的记录（无则空 dict）。"""
    raw = db.get_game_state_map(session_id).get(f"rec:tick:{tick}")
    return dict(raw) if isinstance(raw, dict) else {}


def tick_history(session_id: str) -> list:
    """全部已记录 tick 的有序列表（历史永久累积——每 tick 落库后一直存在）。"""
    gs = db.get_game_state_map(session_id)
    out = []
    for key in gs:
        if key.startswith("rec:tick:"):
            try:
                out.append(int(key.rsplit(":", 1)[1]))
            except (ValueError, IndexError):
                continue
    return sorted(out)


def overview(session_id: str, world_id: str, tick: int = None) -> dict:
    """聚合视图（GET /debug/overview 的数据源）：指定 tick（缺省=当前）的 recorder
    + 世界即时状态（NPC 位置/存活 + 环境卡状态 + 本 tick 痕迹）。"""
    gs = db.get_game_state_map(session_id)
    cur_tick = int(gs.get("current_tick", 0) or 0)
    t = cur_tick if tick is None else int(tick)
    rec = get_tick(session_id, t)
    npcs = {}
    from . import mind_engine
    from .spatial import get_held_items
    for npc_id in db.get_all_npc_ids(world_id):
        _st = db.get_npc_status(session_id, npc_id)
        entry = {
            "pos": db.get_npc_pos(session_id, npc_id) or "",
            "status": _st if isinstance(_st, dict) else {},
        }
        # 全量心智状态（用户要求：调试面板返回 NPC 的所有信息——上帝视角，
        # 数值直接展示，这不是给 LLM 的 prompt，不受"数字不进 prompt"铁律约束）
        try:
            ctx = mind_engine.snapshot_ctx(npc_id, session_id)
            if ctx:
                st = ctx.get("state") or {}
                # P0-a 单一事实源：情绪快照统一走 mental.emotion_snapshot（空热态回退"平静"），
                # 与旁白/神情路径同口径。不再直接用裸热态 emo——那是空 dict 时前端显示为"-"的根源。
                try:
                    from . import mental
                    _snap = mental.emotion_snapshot(st)
                except Exception:  # noqa: BLE001  快照失败回退空短语，不阻塞调试面板
                    _snap = {"word": "", "emotion": {}}
                _emo = _snap.get("emotion") or {}
                entry["full_state"] = {
                    "emotion": {"word": _snap.get("word", ""),
                                "valence": _emo.get("valence"), "arousal": _emo.get("arousal"),
                                "dominance": _emo.get("dominance"),
                                "intensity": st.get("emotion_intensity"),
                                "updated_tick": st.get("updated_tick")},
                    "beliefs": st.get("beliefs") or [],
                    "working_memory": st.get("working_memory") or [],
                    "noticed": st.get("noticed") or [],
                    "plan": _plan_brief(ctx.get("plan")),
                    "secrets": [{"topic": s.get("topic"), "口径": s.get("word"),
                                 "识破进度": s.get("progress")}
                                for s in (ctx.get("secrets_ctx") or [])],
                    "relationship_to_player": _rel_brief(ctx.get("relationship")),
                    "pressure": ctx.get("pressure") or {},
                    "held_items": [str(h.get("name") or h.get("env_id"))
                                   for h in get_held_items(world_id, holder=npc_id)],
                }
        except Exception:  # noqa: BLE001  全量状态拼装失败不影响基础字段
            pass
        npcs[npc_id] = entry
    envs = []
    location_ids = []
    for env_id, kind, name, _desc, state_raw, _p in db.get_environment_cards(world_id):
        try:
            st = json.loads(state_raw) if isinstance(state_raw, str) else (state_raw or {})
        except ValueError:
            st = {}
        envs.append({"env_id": env_id, "name": name, "state": st})
        if kind == "location":
            location_ids.append(env_id)
    # 09-09 用户拍板（问题4：历史痕迹只显示本轮）：拆成两份。
    #  ① traces_now = 仅 tick==t（本 tick 痕迹）——供 _space_events 做"本 tick 按场景聚合"，
    #     保持"即时的上帝视角"语义（empty/solo/multi + 导演结果），不因累计而互相串台。
    #  ② traces_out = 到 tick<=t 为止的【累计】痕迹（正序，带 tick 字段）——供前端
    #     "世界历史痕迹"折叠块展示"到这一 tick 为止的所有轮次痕迹"，方便用户分析记录。
    # get_recent_traces 返回倒序（最新在前），累计展示需正序（最老在前，时间轴感）再 reverse。
    traces_now = [t2 for t2 in db.get_recent_traces(session_id, t, limit=100) if t2[0] == t]
    traces_upto = [t2 for t2 in db.get_recent_traces(session_id, t, limit=1000)][::-1]
    traces_out = [{"actor": x[1], "type": x[2], "target": x[3], "location": x[4],
                   "detail": x[5], "tick": x[0]} for x in traces_upto]
    # 按场景聚合（第5点）：每个空间发生了什么（empty/solo/multi + 导演结果）
    events = _space_events(location_ids, rec, npcs,
                           [{"actor": x[1], "type": x[2], "target": x[3],
                             "location": x[4], "detail": x[5]} for x in traces_now])
    return {"session_id": session_id, "world_id": world_id, "tick": t, "current_tick": cur_tick,
            "ticks_available": tick_history(session_id),
            "recorder": rec, "npcs": npcs, "environments": envs,
            "events": events, "traces": traces_out}


def _plan_brief(plan) -> dict:
    """计划行 → 人话简报（goal/状态/进度/当前步）。"""
    if not plan:
        return {"note": "（无计划）"}
    steps = plan.get("steps") or []
    cur = int(plan.get("current_step", 1) or 1)
    step = steps[cur - 1] if 1 <= cur <= len(steps) else {}
    return {"goal": plan.get("goal", ""), "status": plan.get("status", ""),
            "progress": f"{cur - 1}/{len(steps)}",
            "next_step": f"{step.get('action_type', '')} -> {step.get('target', '') or step.get('scene', '')}"
                         + (f"（{step.get('note', '')}）" if step.get("note") else "")}


def _rel_brief(rel) -> dict:
    """关系行 → 简报。"""
    if not rel:
        return {}
    return {"trust": rel[0], "fear": rel[1], "affection": rel[2],
            "type": rel[3], "notes": rel[4]}


def _effects_of(env_res):
    """从某 NPC 的 env 记录里提取影响概述（对自身 / 对环境两条线）。

    - 对自身：命运/导演裁定、反应仲裁（它自己受到什么对待）；
    - 对环境：执行器落库的环境物品变化（拿/放/开关/损坏等，玩家可识别）。
    """
    self_eff, env_eff = [], []
    for e in env_res or []:
        e_type = str(e.get("type", ""))
        msg = e.get("message") or e.get("narrative") or ""
        outcome = e.get("outcome", "")
        line = f"{e_type}" + (f"：{msg}" if msg else "") + (f"（{outcome}）" if outcome else "")
        if e_type in ("导演裁定", "reaction"):
            self_eff.append(line)
        else:
            env_eff.append(line)
    return {"self": self_eff, "env": env_eff}


def _space_events(location_ids, rec, npcs, traces):
    """按场景聚合「每个空间发生了什么」（第5点）。

    规则：
    - empty：该场景本 tick 无人无事发生；
    - solo  ：一人 → 其最后行动 + 对自身/环境的双影响；
    - multi ：多人（或走了导演）→ 每条参与者行动+双影响 + 导演综合结果(narrative/outcomes/effects/reasoning)+对环境影响。
    """
    events = {}
    rec_npcs = rec.get("npcs") or {}
    player_traces = [x for x in traces if str(x.get("actor", "")) == "player"]
    for sc in (list(location_ids) or []):
        people = []
        director = None
        for npc_id, r in rec_npcs.items():
            key = str(npc_id)
            if key.startswith("导演@"):
                # director 记录以 "导演@场景" 为键（recorder.note_prompt）
                if key.split("@", 1)[1] == sc:
                    director = dict(r.get("output") or {})
                    director["_has_prompt"] = bool(r.get("prompt"))
                continue
            out = r.get("output") or {}
            act = out.get("action") or {}
            a_scene = act.get("location") or (npcs.get(key, {}).get("pos") or "") or ""
            if a_scene != sc:
                continue
            people.append({
                "id": key, "kind": "npc",
                "action": act, "speech": out.get("speech"),
                "reasoning": out.get("reasoning", ""), "plan": out.get("plan") or [],
                "env_effects": _effects_of(r.get("env") or []),
            })
        for pt in player_traces:
            if str(pt.get("location", "")) == sc:
                people.append({"id": "player", "kind": "player",
                               "action": {"type": pt.get("type"), "target": pt.get("target"),
                                          "detail": pt.get("detail")},
                               "speech": None, "reasoning": "", "plan": [],
                               "env_effects": []})
        if people:
            events[sc] = {"scene": sc,
                          "kind": "multi" if (len(people) > 1 or director) else "solo",
                          "people": people,
                          # director 恒为 dict：空场景/无导演用 {}，避免 JSON 序列化成 null
                          "director": dict(director) if director else {}}
        else:
            events[sc] = {"scene": sc, "kind": "empty", "people": [], "director": {}}
    return events
