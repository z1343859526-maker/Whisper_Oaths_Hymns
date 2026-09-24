"""命运分析器（全知仲裁者）：收集同 tick 所有 decision，检测碰撞，裁定结果并落库。

与 agent.py 的本质区别（§8.4）：
- agent = 有限视角，只知道自己的事；
- fate = 全知视角，知道所有 NPC 的意图/行动，裁定"多方行动撞一起后实际发生什么"。

两段式仲裁（世界时序 v2 / 用户的两调用设计）：
- **机械动作（move/use_item/give_item/开关门）**：确定性 code 裁决（execute_npc_action
  同款执行器）——可回放、零成本；
- **语义动作（interact 非门类 / trigger_event）**：LLM 反应仲裁（每 NPC 每 tick ≤1 次，
  与 decide 合计 ≤2 次的铁律）——LLM 只输出 {叙述, effects[]}，effects 过白名单校验
  （set 枚举 + target 必须真实存在）才落库；解析失败回退到"只记痕迹"（规则版行为）。

碰撞检测（规则版，保留）：同地点 ≥2 个非 wait/observe 行动 → collision 痕迹。
"""
import json
import re
import time

from . import db
from . import recorder
from . import environment as env_mod

# LLM 反应效果的合法集枚举（白名单——LLM 只能在这两类世界状态上落效果）
_REACTION_SETS = ("env_state", "npc_status")


def _lazy_llm():
    from .llm import DeepSeekClient
    return DeepSeekClient()


def _needs_reaction(decision: dict) -> bool:
    """语义动作判定：trigger_event，或 interact 但不是开门/关门（门是确定性执行器）。"""
    a = (decision or {}).get("action") or {}
    atype = str(a.get("type", ""))
    detail = str(a.get("detail", "") or "")
    if atype == "trigger_event":
        return True
    if atype == "interact":
        return not ("开" in detail and "关" not in detail) and "关" not in detail
    return False


def apply_decision(session_id, tick, decision, visible_to=None, world_id="test"):
    """把单个 NPC 的 decision 落库：写一条 world_trace，并按 action 更新环境状态。

    环境效果统一走 environment.execute_npc_action——与玩家同一套执行器/写库。
    Returns: trace 记录（dict），env=执行器结果（None=本动作不写环境）。
    """
    action = decision.get("action") or {}
    atype = action.get("type", "wait")
    target = action.get("target", "")
    location = action.get("location", "")
    detail = action.get("detail", "")
    speech = decision.get("speech")

    if atype == "speak" and speech:
        detail = f"说：「{speech}」"

    db.add_world_trace(
        session_id, tick, decision.get("agent", "?"),
        atype, target, location, detail, visible_to,
    )

    env_result = None
    if atype in ("move", "use_item", "give_item", "interact"):
        try:
            env_result = env_mod.execute_npc_action(decision, session_id, tick, world_id)
        except Exception:  # noqa: BLE001  执行失败不崩仲裁（痕迹已在，效果缺失可审计）
            env_result = None

    # recorder ⑤：环境执行器的影响
    if env_result is not None:
        recorder.note_env(session_id, str(decision.get("agent", "?")),
                          {"type": atype, "target": target, **{k: env_result.get(k) for k in
                                                              ("outcome", "message", "changed", "scene")}})
        # 09-09 用户拍板：机械动作被世界挡住(如 move 路堵/拿取不在可及范围)也是真实受阻，
        # 必须进记忆(否则 NPC 会表现得"从没想走过去")。delay import 避免循环依赖。
        if env_result.get("outcome") == "blocked":
            try:
                from . import mind_engine as _me
                _me.record_blocked_intent(
                    session_id, str(decision.get("agent", "?")), tick,
                    f"{atype}{' '+str(target) if target else ''}",
                    str(env_result.get("message", "")) or "世界阻拦")
            except Exception:  # noqa: BLE001  受阻记忆失败不阻塞落库
                pass
    return {
        "tick": tick,
        "agent": decision.get("agent"),
        "type": atype,
        "target": target,
        "location": location,
        "detail": detail,
        "speech": speech,
        "env": env_result,
    }


def _build_reaction_messages(decision: dict, session_id: str, tick: int, world_id: str) -> list:
    """构造 LLM 反应仲裁 prompt：给真实世界事实，只准输出白名单效果的 JSON。"""
    a = decision.get("action") or {}
    target = str(a.get("target", ""))
    facts = []
    if target:
        st = db.get_environment_state(target, world_id)
        if st:
            facts.append(f"对象 {target} 当前状态：{st}")
    actor = str(decision.get("agent", "?"))
    pos = db.get_npc_pos(session_id, actor)
    if pos:
        facts.append(f"行动者 {actor} 位于 {pos}")
    system = (
        "你是叙事引擎的「世界反应仲裁器」。一个 NPC 对世界采取了语义行动（撬锁/藏物/下毒/触发事件等），"
        "请裁定这个行动的实际结果。只输出一个 JSON 对象：\n"
        '{"narrative":"一句话描述实际发生了什么","effects":[{"set":"env_state 或 npc_status",'
        '"target":"对象id","key":"状态键","value":"新值"}]}\n'
        "铁律：effects 只允许 set=env_state（改环境卡状态键）或 npc_status（改 NPC 状态键）；"
        "target 必须用下面给出的真实 id；没有状态变化就给空数组；不要发明不存在的对象。"
    )
    user = (f"【行动者】{actor}\n【行动】{a.get('type')} -> {target}：{a.get('detail', '')}\n"
            f"【真实世界事实】\n" + ("\n".join(facts) if facts else "（无可补充事实）"))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_reaction(raw: str) -> dict or None:
    """解析反应产物；非法（无 JSON/超白名单）→ None（回退规则版：只记痕迹）。"""
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
        if not isinstance(eff, dict):
            continue
        s, t, k = eff.get("set"), str(eff.get("target", "")), str(eff.get("key", ""))
        if s in _REACTION_SETS and t and k:
            effects.append({"set": s, "target": t, "key": k, "value": eff.get("value")})
    return {"narrative": str(data.get("narrative", "")), "effects": effects}


def _apply_reaction_effects(session_id, tick, actor, reaction: dict, world_id: str) -> list:
    """落 LLM 反应效果（白名单已过）+ 追加叙述痕迹。返回实际落库的 effects。"""
    applied = []
    for eff in reaction.get("effects", []):
        try:
            if eff["set"] == "env_state":
                db.patch_environment_state(eff["target"], eff["key"], eff["value"], world_id)
            else:
                db.set_npc_status(session_id, eff["target"], eff["key"], eff["value"])
            applied.append(eff)
        except Exception:  # noqa: BLE001  单条失败跳过（其余照落）
            continue
    narrative = str(reaction.get("narrative", "")).strip()
    if narrative or applied:
        desc = narrative or "世界对此作出了回应"
        db.add_world_trace(session_id, tick, "fate", "reaction", "", "",
                           f"{actor} 的行动结果：{desc}")
    return applied


def arbitrate(session_id, tick, decisions, visible_to=None, llm_client=None, world_id="test"):
    """全知仲裁：机械动作确定性落库 + 语义动作 LLM 反应仲裁 + 碰撞检测。

    Args:
        decisions: list[decision dict]（调用方已按 agent id 排序）。
        llm_client: LLM 客户端（None=懒加载单例）；多条语义动作走 chat_many 并行。
        world_id: 环境效果落库的世界维度。
    Returns:
        list[trace dict]，本 tick 实际发生的痕迹。
    """
    traces = []

    # ① 机械落库（确定性顺序）
    for d in decisions:
        traces.append(apply_decision(session_id, tick, d, visible_to=visible_to, world_id=world_id))

    # ② 语义动作的 LLM 反应仲裁（并行，≤1 次/NPC/tick）
    ambiguous = [d for d in decisions if _needs_reaction(d)]
    if ambiguous:
        client = llm_client or _lazy_llm()
        msgs = [_build_reaction_messages(d, session_id, tick, world_id) for d in ambiguous]
        t0 = time.perf_counter()
        if len(msgs) == 1:
            try:
                results = [(True, client.chat(msgs[0]))]
            except Exception:  # noqa: BLE001
                results = [(False, "")]
        else:
            results = client.chat_many(msgs)
        batch_ms = (time.perf_counter() - t0) * 1000.0
        for d, (ok, raw) in zip(ambiguous, results):
            actor = str(d.get("agent", "?"))
            reaction = _parse_reaction(raw) if ok else None
            applied = []
            if reaction:
                applied = _apply_reaction_effects(session_id, tick, actor, reaction, world_id)
            # recorder ⑤⑥：反应效果与耗时（批次均摊到各 NPC 记录上）
            recorder.note_env(session_id, actor,
                              {"type": "reaction", "target": str((d.get("action") or {}).get("target", "")),
                               "outcome": "ok" if reaction else "fallback",
                               "narrative": (reaction or {}).get("narrative", "")},
                              reaction_ms=batch_ms / max(1, len(ambiguous)),
                              reaction_effects=applied)

    # ③ 碰撞检测（规则版）：同一地点有 >=2 个 NPC 采取非 wait/observe 的行动
    by_location = {}
    for d in decisions:
        a = d.get("action") or {}
        if a.get("type") in ("wait", "observe"):
            continue
        by_location.setdefault(a.get("location", "") or "?", []).append(d)
    for loc, ds in by_location.items():
        if len(ds) >= 2:
            agents = "、".join(d.get("agent", "?") for d in ds)
            detail = f"{agents} 在 {loc} 撞个正着，命运在此交织"
            db.add_world_trace(session_id, tick, "fate", "collision", "", loc, detail, visible_to)
            traces.append({
                "tick": tick, "agent": "fate", "type": "collision",
                "target": "", "location": loc, "detail": detail, "speech": None,
            })
    return traces

