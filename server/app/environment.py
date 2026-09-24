"""环境执行器：把玩家的"环境型意图"落地成"世界状态变更"（目标2：环境被玩家修改）。

与 plan_executor / fate 的分工：
  plan_executor  = NPC 按其计划改世界（零 LLM、确定性）；
  fate           = NPC 拟行动的命运判定与落库；
  environment    = 玩家对环境的直接行为（拿/放/开门/移动），复用同一套写库函数。

关键设计（"复用"而非"另造"）：
  - 写回一律走 db.patch_environment_state / db.add_world_trace / db.upsert_game_state
    —— 与 NPC 改世界（fate._apply_use_item、plan_executor._apply_effect）同源，
    玩家和 NPC 改世界的底层代码是同一套，只是入口不同。这满足"可合并"。

分派逻辑（回应意图识别层的 domain/side_effect）：
  self     + mutating  → 移动：校验可达(can_reach) → 更新玩家 scene + 痕迹
  spatial  + mutating  → 写世界：目标解析 → patch_environment_state + 痕迹
  spatial  + read_only → 只读感知：不写回（由 build_perception_snapshot 出快照）
  dialogue + read_only → 对话：不写回（走对话管线）

返回：执行结果 dict，含 outcome(ok/blocked/ignored) + 执行描述 + 是否产生世界变更，
供上层拼旁白 / 决定是否续调 LLM。
"""
import json
import re
import logging
from types import SimpleNamespace

logger = logging.getLogger(__name__)

from . import db
from . import grounding
from . import spatial
from .intent import Intent  # T6：NPC 决策适配器需要构造 Intent（intent.py 不反向依赖本模块，无循环）


# ---------------------------------------------------------------------------
# 目标解析：把意图里的 target.hint 对齐到具体实体 id（语义精配的程序部分）
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 目标解析：把意图里的 target.hint 对齐到具体实体 id（指称消解 / Referent Grounding）
#
# 架构教训（2026-09-07 重构）：此前这里用量词表（"一把/一柄…"）、部件表
# （"腿/扶手…"）、场景映射表（"房间三→room_3"）做枚举匹配——词典永远不完备
# （量词几十个、部件由内容创作决定、换世界全崩），且词典层"碰巧命中"会抢跑，
# 让本该交给 LLM 精配的难例被错误地本地消化（confidently wrong）。
# 现统一走 app/grounding.py 的级联消解：召回(数据驱动) → bigram 打分 → 阈值
# 判定 → 交 LLM 选择题（closed-world，候选只来自世界状态）。词表已全部删除。
# ---------------------------------------------------------------------------
def resolve_target(intent, scene, world_id="golden"):
    """把意图对象对齐到具体实体：优先用户给的 target.id，否则 hint 走 grounding 级联。

    Returns:
        dict：{"kind":"entity|scene|npc|none", "env_id":"", "name":"",
               "candidates":[(候选,分数)]}  # none 时附排序候选，供上层 LLM 选择题
    """
    t = intent.target
    if t.get("type") == "npc":
        return {"kind": "npc", "env_id": t.get("id", ""), "name": t.get("id", "")}

    # L0：用户已给明确 id（LLM 从清单里选了）
    if t.get("id") and t["id"] not in ("", "null"):
        return {"kind": "entity", "env_id": t["id"], "name": t["id"]}

    hint = t.get("hint", "")

    # 方位锚点命中（数据驱动：anchor_label 来自 DB，非词表）
    res = spatial.resolve(hint, scene, world_id)
    if res.get("anchor"):
        # name 用 region（锚点标签或实体名）——旁白不裸吐英文 id
        return {"kind": "entity", "env_id": res["anchor"], "name": res.get("region") or res["anchor"]}

    # grounding 级联：召回（在场 + 全世界兜底）→ bigram 打分 → 阈值/优势判定
    # prefer_accessible=True：mutating 操作的对象必在操作者可及范围——
    # "把另一根椅子腿放到房间一"两根并列时，可及性过滤后唯一即消解（通用世界规则）
    r = grounding.resolve(hint, _recall_candidates(scene, world_id), prefer_accessible=True)
    if r["resolved"]:
        return {"kind": "entity", "env_id": r["env_id"], "name": r["name"]}

    # 未达阈值：交上层 LLM 判定（三层判定之②软推理），附排序候选清单（closed-world 选择题）
    return {"kind": "none", "env_id": "", "name": hint,
            "candidates": [(c.get("env_id"), c.get("name"), round(s, 3)) for c, s in r["candidates"]]}


def _recall_candidates(scene, world_id):
    """L1 候选召回（数据驱动，零词典）：在场实体 + 全世界非房间实体兜底。

    - 在场（entities_in）：按动态 where 归属——被搬到别的房间的物不在其中；
    - 全世界兜底：被拿走（held）/远处的物品仍是"存在物"，可被指称
      （"那把刀"即使被别人拿着也要能命中，才能提示"已经在别人那里"），
      grounding 对兜底候选做衰减打分（优先在场物）。
    parts 键来自环境卡 state.parts（拆解系统维护的数据）——部件匹配靠数据不靠词典。
    """
    cands, seen = [], set()
    for e in spatial.entities_in(scene, world_id):
        if e.get("type") == "room":
            continue
        cands.append({"env_id": e["env_id"], "name": e.get("name") or e["env_id"],
                      "in_scene": True, "global_fallback": False,
                      "parts_keys": _parts_keys(e)})
        seen.add(e["env_id"])
    for row in db.get_environment_entities(None, world_id):
        if row[3] == "room" or row[0] in seen:
            continue
        st = _item_state_dict(row[0], world_id)
        cands.append({"env_id": row[0], "name": row[2] or row[0],
                      "in_scene": False, "global_fallback": True,
                      "parts_keys": list((st.get("parts") or {}).keys()) if isinstance(st.get("parts"), dict) else []})
    return cands


def _parts_keys(entity: dict) -> list:
    """实体 state.parts 的键（数据化部件清单；无 parts 返回空）。"""
    st = entity.get("state") or {}
    parts = st.get("parts") if isinstance(st, dict) else None
    return list(parts.keys()) if isinstance(parts, dict) else []


def _extract_dest_scene(dest, world_id):
    """目的地场景消解（数据驱动）：候选=本世界全部 room 实体，grounding 打分
    （名字 bigram + 数字对齐"房间三"↔room_3）。替代旧"房间三→room_3"映射表
    （旧表含"一"→room_1 的活 bug：说"拿一杯水"会命中 room_1）。"""
    rooms = [{"env_id": r[0], "name": r[2] or r[0]}
             for r in db.get_environment_entities(None, world_id) if r[3] == "room"]
    return grounding.resolve_scene(dest, rooms)


def _take_item(session_id, tick, item_id, holder, world_id, scene):
    """拿取数据：物品被某人拿走 → state=held, holder=xxx。复用 plan_executor 的效果。"""
    db.patch_environment_state(item_id, "state", "held", world_id)
    db.patch_environment_state(item_id, "holder", holder, world_id)


def _open_door(session_id, tick, room, world_id):
    """开门：房间环境卡 state['door_open']=true。"""
    db.patch_environment_state(room, "door_open", True, world_id)


# ---------------------------------------------------------------------------
# 玩家环境行为主执行
# ---------------------------------------------------------------------------
def execute_player_action(intent, session_id, tick, world_id="golden", player="player"):
    """执行玩家的环境型意图，返回执行结果 dict。

    Args:
        intent: intent.Intent 对象。
        session_id: 会话/轮回标识。
        tick: 当前时间步（0=18:00）。
        world_id: 世界维度。
        player: 行动者标识（默认 "player"）。
    Returns:
        dict：{
          "outcome": "ok" | "blocked" | "ignored",
          "message": str           # 执行描述（旁白用）
          "changed": bool          # 是否发生了世界变更（写回）
          "scene": str             # 玩家执行后的场景（self 移动会更新）
        }
    """
    scene = db.get_player_scene(session_id)
    intent_domain = intent.domain
    side_effect = intent.side_effect

    # ---- ① self + mutating：移动 ----
    if intent_domain == "self":
        return _exec_move(intent, session_id, tick, world_id, scene, player)

    # ---- ② spatial + mutating：写世界 ----
    if intent_domain == "spatial" and side_effect == "mutating":
        return _exec_mutate(intent, session_id, tick, world_id, scene, player)

    # ---- ③ spatial + read_only：只读感知，不写回 ----
    if intent_domain == "spatial":
        return {"outcome": "ignored", "message": "", "changed": False, "scene": scene}

    # ---- ④ dialogue：对话，不写回 ----
    return {"outcome": "ignored", "message": "", "changed": False, "scene": scene}


def _exec_move(intent, session_id, tick, world_id, scene, player):
    """移动执行：校验可达 → 更新玩家场景 + 痕迹。"""
    t = intent.target
    dest = t.get("id", "") or t.get("hint", "")
    # 从 hint 里提取目的地场景 id（grounding.resolve_scene：room 实体候选 + 数字对齐，零映射表）
    dest = _extract_dest_scene(dest, world_id)

    if not dest:
        return {"outcome": "blocked", "message": "你要去哪？", "changed": False, "scene": scene}

    if scene == dest:
        return {"outcome": "ok", "message": "你已经在" + dest, "changed": False, "scene": scene}

    if not spatial.can_reach(scene, dest, world_id):
        # 记一条"尝试不可达"的痕迹（目标3：空间逻辑可被交互感知确认）
        db.add_world_trace(session_id, tick, player, "move", dest, scene,
                           f"试图从 {scene} 走向 {dest}，但路不通（门关着或不相连）")
        return {"outcome": "blocked", "message": "你过不去——这条路不通。", "changed": False, "scene": scene}

    # 更新玩家位置 + 痕迹
    db.upsert_game_state(session_id, "scene", dest)
    db.add_world_trace(session_id, tick, player, "move", dest, scene,
                       f"从 {scene} 移动到 {dest}")
    return {"outcome": "ok", "message": f"你来到了{dest}。", "changed": True, "scene": dest}


def move_player_scene(session_id, world_id, dest, player="player"):
    """明确目标的玩家移动（点地图移动，方向1）。

    与 /chat 里玩家输入"走到房间X"的区别：目标由客户端地图直接给定（dest 已是确切场景 id），
    不再走 LLM 意图识别。复用 _exec_move 的执行语义（校验可达 → 写 scene + 痕迹），
    保证两条路径对世界状态的落库完全一致。

    Args:
        dest: 目标场景 id（与客户端 location_id 对齐，如 room_2）。
    Returns:
        与 _exec_move 同构的 dict：{outcome, message, changed, scene}。
    """
    gs = db.get_game_state_map(session_id)
    tick = gs.get("current_tick", 0)
    scene = db.get_player_scene(session_id) or ""
    intent = SimpleNamespace(target={"id": dest, "hint": dest})
    return _exec_move(intent, session_id, tick, world_id, scene, player)


def _npc_target_id(intent, session_id, scene, world_id, prefer_dead: bool = False) -> str:
    """把意图所指的 NPC 定位成 npc_id（攻击/搜尸共用）。

    与意图识别侧的 _npc_target 配套：此处拿到 intent.target（type=npc, id 可能为空即代词），
    ① 若 id 非空直接用；② id 空（代词"他/她"或泛指"尸体"）→ 用 hint 里 NPC 名对齐；
    ③ 仍空且所在场景在场"该状态"的 NPC 唯一 → 用它：
       - 攻击（prefer_dead=False）：在场唯一的**幸存**NPC（不能攻击尸体）；
       - 搜尸（prefer_dead=True）：在场唯一的**死者尸体**（"搜尸体"即指那具）。
    Returns: npc_id；定位不到返回 ""。
    """
    from . import db as _db
    target = intent.target or {}
    npc_id = str(target.get("id", "") or "")
    if npc_id:
        return npc_id
    hint = target.get("hint", "") or ""
    from . import world_pack as _wp
    npc_id = _wp.npc_name_to_id(hint, world_id)
    if npc_id:
        return npc_id
    # 代词/"尸体" + 在场唯一该状态 NPC 兜底
    if any(p in hint for p in ("他", "她", "那人", "男人", "女人", "尸体", "遗体")):
        present = [n for n in _db.get_all_npc_ids(world_id)
                   if (_db.get_npc_pos(session_id, n) or "") == scene
                   and bool(_db.get_npc_status(session_id, n).get("dead")) is prefer_dead]
        if len(present) == 1:
            return present[0]
    return ""


def _npc_display_name(npc_id, world_id) -> str:
    """npc_id → 中文显示名（攻击/搜尸旁白用，避免裸吐 test_man）；读不到回退 id。"""
    from . import world_pack as _wp
    return _wp.npc_id_to_name(npc_id, world_id) or npc_id


def _exec_mutate(intent, session_id, tick, world_id, scene, player):
    """写世界执行：按 intent.spatial.op 分派具体写回（pick/place/move_body/tip_over/open/close/other）。

    ⚠️ 重大升级（对应目标①②）：写回从"单 key patch"升级为"覆盖式写 where"（set_entity_where）——
       · 目标②「物体改变记录改变后的状态、覆盖原有」：一次写回完整的 where（场景+坐标+姿态），
         不再像 patch 那样只改单一状态位、留下中间态；
       · 目标①「语义识别调动对应效果 + 物品空间移动」：op=pick/place/move_body/tip_over 各对应一类
         空间状态迁移（拿取/放置/搬到别处/放倒）。
    """
    op = (intent.spatial or {}).get("op", "pick")

    # 造物（AI自由度）是"从无到有"，不需要先解析一个既有目标实体（造的是新物）。
    # 必须放在 resolve_target 之前，否则 resolve 返回 none 会提前 return、绕过 _exec_create。
    if op == "create":
        return _exec_create(intent, session_id, tick, world_id, scene, player)

    resolved = resolve_target(intent, scene, world_id)

    # 未定位到实体：开放世界兜底，交给上层 LLM 语义精配（不误写）。
    if resolved["kind"] == "none":
        return {"outcome": "ignored", "message": "", "changed": False, "scene": scene}

    env_id, name = resolved["env_id"], resolved["name"]

    # ------ 攻击/伤害（作用于 NPC）---- 修"偷袭后NPC不死、复活"——
    # 玩家对目标的伤害意图落 npc_status.dead=True + 痕迹，让 NPC 真正进入死亡态，
    # 之后 _decide_targets 会把它从决策目标剔除（dead 过滤），不再"复活"。
    if op == "attack":
        if resolved["kind"] != "npc":
            return {"outcome": "ignored", "message": "", "changed": False, "scene": scene}
        # 目标定位（含代词+在场唯一兜底）复用 _npc_target_id，攻击/搜尸同源消重。
        target = _npc_target_id(intent, session_id, scene, world_id)
        if not target:
            return {"outcome": "ignored", "message": "", "changed": False, "scene": scene}
        disp = _npc_display_name(target, world_id)
        db.set_npc_status(session_id, target, "dead", True)
        db.add_world_trace(session_id, tick, player, "attack", target, scene,
                           f"{intent.verb or '攻击'}了{disp}，命中要害")
        return {"outcome": "ok",
                "message": f"你{intent.verb or '攻击'}了{disp}，对方倒下不再动弹。",
                "changed": True, "scene": scene}

    # ------ 搜尸/摸尸（作用于"已死"NPC）---- 杀人→尸体→可搜身的核心交互：
    # 玩家搜刮死者随身物品：把 get_held_items(holder=npc_id) 的物品转移给玩家（搜走）。
    # 只对 dead NPC 生效；活人是"搜身"而是被拒绝（不同语义，避免误开箱）。无物品则提示空。
    if op == "search":
        # NPC 目标定位（搜尸：偏好在场唯一"死者尸体"兜底，传递 prefer_dead=True）
        target = _npc_target_id(intent, session_id, scene, world_id, prefer_dead=True)
        if not target:
            return {"outcome": "ignored", "message": "", "changed": False, "scene": scene}
        st = db.get_npc_status(session_id, target)
        if not st.get("dead"):
            disp = _npc_display_name(target, world_id)
            return {"outcome": "blocked",
                    "message": f"{disp}还活着——这不算搜身，直接问ta吧。",
                    "changed": False, "scene": scene}
        disp = _npc_display_name(target, world_id)
        held = spatial.get_held_items(world_id, holder=target)
        if not held:
            db.add_world_trace(session_id, tick, player, "search", target, scene,
                               f"搜了{disp}的身，一无所获")
            return {"outcome": "ok",
                    "message": f"你搜了搜{disp}的尸体，身上什么也没有。",
                    "changed": False, "scene": scene}
        # 搜身转移：死者的物品 → 都是 player 的（mode=held, holder=player, scene 就地）
        got = []
        for h in held:
            db.set_entity_where(h["env_id"],
                                {"mode": "held", "holder": player, "scene": scene,
                                 "position": None, "orientation": 0, "anchored_to": None},
                                world_id)
            got.append(h["name"] or h["env_id"])
        db.add_world_trace(session_id, tick, player, "search", target, scene,
                           f"搜了{disp}的身，拿走了：{'、'.join(got)}")
        return {"outcome": "ok",
                "message": f"你搜了搜{disp}的尸体，拿走了{('、'.join(got))}。",
                "changed": True, "scene": scene}

    # ------ 开关门（作用于房间实体）------
    if op == "open":
        _open_door(session_id, tick, env_id, world_id)
        return {"outcome": "ok", "message": f"你打开了{name}的门。", "changed": True, "scene": scene}
    if op == "close":
        db.patch_environment_state(env_id, "door_open", False, world_id)
        return {"outcome": "ok", "message": f"你关上了{name}的门。", "changed": True, "scene": scene}

    # ------ 物品写入（pick/place/move_body/tip_over）------
    if op == "pick":
        return _exec_pick(env_id, name, session_id, tick, world_id, scene, player)
    if op == "move_body":
        return _exec_move_body(intent, env_id, name, session_id, tick, world_id, scene, player)
    if op == "place":
        return _exec_place(intent, env_id, name, session_id, tick, world_id, scene, player)
    if op == "tip_over":
        return _exec_tip_over(intent, env_id, name, session_id, tick, world_id, scene, player)
    if op == "disassemble":
        return _exec_disassemble(intent, env_id, name, session_id, tick, world_id, scene, player)
    if op == "create":
        return _exec_create(intent, session_id, tick, world_id, scene, player)

    # ------ 其它写入（凿/刻/挖/藏...）：记录痕迹，具体语义由上层 LLM 续写叙述 ------
    db.add_world_trace(session_id, tick, player, "interact", env_id, scene,
                       f"{intent.verb or op}{name}")
    return {"outcome": "ok", "message": f"你{intent.verb or '做了'}。", "changed": True, "scene": scene}


def _item_state_dict(env_id, world_id):
    """读某物品的环境卡 state（dict）。"""
    st = db.get_environment_state(env_id, world_id)
    try:
        return json.loads(st) if st else {}
    except (ValueError, TypeError):
        return {}


def _resolve_position(intent, dest_scene, world_id):
    """把意图的"落点线索"翻译成目标坐标（程序算坐标，铁律：LLM 不摸坐标）。

    三级推理（你新增的"没说具体位置也给适当落点"）：
      ① 意图已挑好 ref_anchor（默认落点补全器填的）→ spatial.near_position(锚点旁)；
      ② hint 含方位/锚点名 → spatial.resolve 现有相对方位→坐标；
      ③ 都没命中 → 场景中央兜底（往哪放都合理的默认）。
    三级都算不出返回 None（交由上层 LLM 叙述兜底，不算错）。
    """
    ref = (intent.spatial or {}).get("ref_anchor")
    if ref:
        pos = spatial.near_position(ref, world_id)
        if pos:
            return pos
    hint_text = intent.target.get("hint", "")
    res = spatial.resolve(hint_text, dest_scene, world_id)
    if res.get("position"):
        return res["position"]
    return spatial.scene_center(dest_scene, world_id)


def _exec_pick(env_id, name, session_id, tick, world_id, scene, player):
    """拿起：物品 → mode=held, holder=player（覆盖式写 where）。"""
    st = _item_state_dict(env_id, world_id)
    w = st.get("where") if isinstance(st.get("where"), dict) else {}
    holder = w.get("holder") or st.get("holder")
    if holder:
        return {"outcome": "ok", "message": f"{name}已经在别人手里——现在在【{holder}】身上。",
                "changed": False, "scene": scene, "holder": holder}
    db.set_entity_where(env_id, {"mode": "held", "holder": player, "scene": scene,
                                 "position": None, "orientation": 0, "anchored_to": None}, world_id)
    db.add_world_trace(session_id, tick, player, "pick", env_id, scene, f"拿起了{name}")
    return {"outcome": "ok", "message": f"你拿起了{name}。", "changed": True, "scene": scene}


def _exec_move_body(intent, env_id, name, session_id, tick, world_id, scene, player):
    """搬到某场景：物品 → mode=placed, scene=dest（物品空间移动，目标①）。"""
    dest = (intent.spatial or {}).get("dest_scene")
    if not dest:
        return {"outcome": "blocked", "message": f"要把{name}搬到哪个房间？", "changed": False, "scene": scene}
    pos = _resolve_position(intent, dest, world_id)
    orient = (intent.spatial or {}).get("orientation", 0)
    db.set_entity_where(env_id, {"mode": "placed", "holder": None, "scene": dest,
                                 "position": pos, "orientation": orient, "anchored_to": None}, world_id)
    db.add_world_trace(session_id, tick, player, "move_item", env_id, dest,
                       f"把{name}搬到{dest}" + (f"（坐标{pos}）" if pos else ""))
    return {"outcome": "ok", "message": f"你把{name}搬到了{dest}。", "changed": True, "scene": scene}


def _exec_place(intent, env_id, name, session_id, tick, world_id, scene, player):
    """放置到某处：物品 → mode=placed, scene=dest, position=坐标, orientation=姿态。

    这是"放倒放到墙角"的落点：dest 来自意图、position 由 spatial.resolve 算、
    orientation=90 表示放倒。一次覆盖式写 where，完整记录摆放后的状态。
    """
    dest = (intent.spatial or {}).get("dest_scene") or scene
    pos = (intent.spatial or {}).get("position")
    if pos is None:
        pos = _resolve_position(intent, dest, world_id)
    orient = (intent.spatial or {}).get("orientation", 0)
    db.set_entity_where(env_id, {"mode": "placed", "holder": None, "scene": dest,
                                 "position": pos, "orientation": orient, "anchored_to": None}, world_id)
    db.add_world_trace(session_id, tick, player, "place", env_id, dest,
                       f"把{name}放到{dest}" + (f"（坐标{pos}）" if pos else "") +
                       ("并放倒" if orient == 90 else ""))
    tip = "，并放倒了" if orient == 90 else ""
    return {"outcome": "ok", "message": f"你把{name}放到了{dest}{tip}。", "changed": True, "scene": scene}


def _exec_tip_over(intent, env_id, name, session_id, tick, world_id, scene, player):
    """放倒（不换场景）：保持当前场景/位置，只覆盖 orientation=90（目标②：覆盖式记录改变后状态）。"""
    st = _item_state_dict(env_id, world_id)
    w = st.get("where") if isinstance(st.get("where"), dict) else {}
    cur_scene = w.get("scene") or scene
    cur_pos = w.get("position", _resolve_position(intent, cur_scene, world_id))
    db.set_entity_where(env_id, {"mode": "placed", "holder": None, "scene": cur_scene,
                                 "position": cur_pos, "orientation": 90,
                                 "anchored_to": w.get("anchored_to")}, world_id)
    db.add_world_trace(session_id, tick, player, "tip_over", env_id, cur_scene, f"把{name}放倒")
    return {"outcome": "ok", "message": f"你把{name}放倒了。", "changed": True, "scene": scene}


_PART_SUFFIX = {"腿": "leg", "脚": "leg", "扶手": "arm", "靠背": "back", "背板": "back"}


def _exec_disassemble(intent, env_id, name, session_id, tick, world_id, scene, player):
    """拆下部件：把一个物体拆出 N 个独立部件实体（路径A 造物）。

    你问的"把椅子腿拆下来两条变成独立物体"就在这里落地：
      ① 读物体环境卡 state.parts（声明可拆部件；无声明/拆完 → blocked）；
      ② 为每个部件生成独立 env_id + 实体行 + 环境卡行（source=runtime，轮回时清掉）；
      ③ 覆盖式更新原物体 state.parts（remaining 减、removed 追加）——记录"被改造"；
      ④ 记录痕迹。
    """
    st = _item_state_dict(env_id, world_id)
    parts = st.get("parts")
    if not (isinstance(parts, dict) and isinstance(parts.get("legs"), dict)):
        return {"outcome": "blocked", "message": f"{name}上没什么能拆下来的部件。",
                "changed": False, "scene": scene}
    legs = parts["legs"]
    remaining = int(legs.get("remaining", 0))
    count = min(2, remaining)  # 默认一次拆 2 条（用户场景）；不能超过现有
    if count <= 0:
        return {"outcome": "blocked", "message": f"{name}的部件已经拆完了。",
                "changed": False, "scene": scene}

    part_name = (intent.spatial or {}).get("part") or "腿"
    sfx = _PART_SUFFIX.get(part_name, "part")
    removed = list(legs.get("removed", []))
    base = len(removed)  # 已拆编号基数；loop 内不再用 len(removed)（会被 append 污染导致跳号）
    # 原物体当前位置：拆下的部件落它附近（在手/原位都以其 where 为准）
    w = st.get("where") if isinstance(st.get("where"), dict) else {}
    base_scene = w.get("scene") or scene
    base_pos = w.get("position") or spatial.scene_center(base_scene, world_id)
    # 普遍化：若意图指定了"拆到某房间"（如"把椅子腿拆下来扔到房间三"），
    # 部件直接落目标场景中心；否则落原物体附近。dest_scene 由 _infer_write_op 从
    # _extract_dest 提取（"放到/扔到/搬到"+房间），目标场景已消解成 room_xx。
    # 这样"拆+跨场景放置"一次完成，杜绝"拆完却原地没动"的怪异表现。
    dest_scene = (intent.spatial or {}).get("dest_scene") or ""
    part_scene = dest_scene if dest_scene and dest_scene != base_scene else base_scene
    part_pos = spatial.scene_center(part_scene, world_id) if dest_scene else base_pos

    created = []
    for i in range(1, count + 1):
        leg_id = f"{env_id}_{sfx}_{base + i}"
        leg_name = f"{name}{part_name}"  # "一把椅子"+"腿"→"一把椅子腿"（无词典；感知按 bigram 命中）
        # ① 空间骨架行（实体，落在 part_scene）
        db.create_environment_entity(
            leg_id, world_id, part_scene, leg_name, "key_item",
            position=part_pos, orientation=0, is_anchor=0, source="runtime")
        # ② 环境卡行（状态/描述，state=initial_state）
        db.create_environment_card(
            leg_id, world_id, "item", leg_name,
            description=f"从{name}上拆下的{part_name}，原本属于{name}。",
            state={"state": "in_place", "holder": None, "current_place": part_scene,
                   "where": {"mode": "in_place", "holder": None, "scene": part_scene,
                             "position": part_pos, "orientation": 0, "anchored_to": None}})
        removed.append(leg_id)
        created.append(leg_id)

    # ③ 覆盖式更新原物体 parts（记录被改造后的状态：少了几条腿）
    legs["remaining"] = remaining - count
    legs["removed"] = removed
    parts["legs"] = legs
    st["parts"] = parts
    db.update_environment_state(env_id, json.dumps(st, ensure_ascii=False), world_id)

    # ④ 痕迹（scene 用部件实际落点房间；若拆到别的房间，旁白/感知据此得知部件去哪了）
    db.add_world_trace(session_id, tick, player, "disassemble", env_id, part_scene,
                       f"从{name}上拆下{count}根{part_name}，落在{part_scene}"
                       f"（{'、'.join(created)}）")
    # 玩家可见消息不裸吐 env_id（chair_leg_1）——具体 id 已记进 trace 供调试，
    # 旁白据此自然叙述"你拆下两根椅子腿"，避免英文 id 泄漏进游戏文本。
    dest_txt = f"，放到了{intent.spatial.get('dest_scene')}" if dest_scene else ""
    return {"outcome": "ok",
            "message": f"你从{name}上拆下了{count}根{part_name}{dest_txt}。",
            "changed": True, "scene": scene}


# 造物系统提示：让 LLM 把玩家的创造意图解析成"造一个有名字、有描述的实体"的规格。
# 铁律：LLM 只给语义（名称/描述/类型），不摸 id/坐标——id 由程序生成、坐标由 spatial 算，
# 维持"LLM 不摸坐标/不写数字"的铁律（呼应意图层同款约定）。
_CREATE_SYSTEM = (
    "你是文字冒险游戏的「造物规格解析器」。玩家凭行动创造/改造出一个新物体"
    "（如做一根绳子、把椅子腿削成匕首、织一块布）。请把这个新物体解析成 JSON：\n"
    '格式：{"name":"物品名","description":"一到两句描述","kind":"item|key_item|furniture"}\n'
    "只输出一个 JSON 对象，不要任何其他文字、不要 markdown 代码块。\n"
    "- name：物品名（如'绳子'、'木匕首'）——要具体、可被感知识别的名词；\n"
    "- description：一到两句它长什么样、由什么做成（供观察/搜尸叙述用）；\n"
    "- kind：物品类型，item=普通小物，key_item=关键物，furniture=家具类。\n"
)


def _exec_create(intent, session_id, tick, world_id, scene, player):
    """AI 自由度造物：玩家凭行动创造/改造出新物体 → LLM 生成规格 → 落库成可寻址实体。

    为什么这样做（用户理念）：不硬编码"会造出椅子腿/绳子/匕首"等每种可能——
    让 LLM 决定造什么（名称/描述/类型），程序只负责：
      ①生成唯一 env_id（crafted_<n>）；②落 environment_entity（空间骨架）+ environment_card
        （状态/描述，source=runtime 轮回即清）；③把新物当作普通实体（可拿/放/搬/搜）。
    这样"造物"是通用的、数据驱动的，玩家怎么造都由 LLM 解释，程序零特判。

    Returns: {outcome, message, changed, scene}。LLM 失败则降级记录痕迹不落库。
    """
    from .llm import DeepSeekClient
    spec = None
    try:
        raw = DeepSeekClient().chat([
            {"role": "system", "content": _CREATE_SYSTEM},
            {"role": "user", "content": f"玩家的造物行动：「{intent.target.get('hint', '') or intent.verb}」"},
        ])
        m = re.search(r"\{.*\}", raw or "", re.DOTALL)
        spec = json.loads(m.group(0)) if m else None
    except Exception as e:  # noqa: BLE001  LLM 失败不阻断：降级为"事做了但没造出具体物"
        logger.error("造物规格 LLM 失败：%s", e)
        db.add_world_trace(session_id, tick, player, "create", "", scene,
                           f"尝试{intent.verb or '制作'}，但没能做出具体的东西")
        return {"outcome": "ok",
                "message": f"你尝试{intent.verb or '制作'}，但没做成什么像样的东西。",
                "changed": False, "scene": scene}

    if not spec:
        db.add_world_trace(session_id, tick, player, "create", "", scene,
                           f"尝试{intent.verb or '制作'}但没有明确的产物")
        return {"outcome": "ok",
                "message": f"你{intent.verb or '制作'}，可一时没想好做成什么样。",
                "changed": False, "scene": scene}

    name = str(spec.get("name") or "东西")
    desc = str(spec.get("description") or "一件刚做出来的物件。")
    kind = str(spec.get("kind") or "item")
    # ① 唯一 env_id（本世界已有 crafted_ 前缀则递增）
    prefix = "crafted"
    n = 1
    while db.get_environment_state(f"{prefix}_{n}", world_id) is not None:
        n += 1
    env_id = f"{prefix}_{n}"
    # 落点：意图指定 room 则用它，否则玩家当前场景
    place_scene = (intent.spatial or {}).get("dest_scene") or scene
    pos = spatial.scene_center(place_scene, world_id)
    # ② 空间骨架 + 环境卡（source=runtime，轮回清掉；"出厂即当前"）
    db.create_environment_entity(env_id, world_id, place_scene, name, kind,
                                 position=pos, orientation=0, is_anchor=0, source="runtime")
    db.create_environment_card(
        env_id, world_id, kind, name, description=desc,
        state={"state": "in_place", "holder": None, "current_place": place_scene,
               "where": {"mode": "in_place", "holder": None, "scene": place_scene,
                         "position": pos, "orientation": 0, "anchored_to": None}})
    # ③ 痕迹
    db.add_world_trace(session_id, tick, player, "create", env_id, place_scene,
                       f"{intent.verb or '制作'}出了{name}（{env_id}）：{desc}")
    return {"outcome": "ok",
            "message": f"你{intent.verb or '制作'}出了{name}。",
            "changed": True, "scene": place_scene}


# ---------------------------------------------------------------------------
# NPC 环境交互（世界时序 v2 / T6）：NPC 决策 → 与玩家同一套执行器/写库
#
# 设计（环境系统文档 §2.6"同一套写库"的兑现）：玩家与环境交互的能力（拿/放/
# 倒/搬/开关门/移动），NPC 通过 agent.decide 的 8 类动作映射到同一批执行器——
# 差别只在三处：①位置权威是 npc_pos 而非 game_state['scene']；②移动用
# set_npc_pos；③"已在别人那里"的判定按行动者放宽（自己持有时可再放置）。
# 状态一律经 set_entity_where 双写同步旧字段，plan 前置判定不脱节。
# ---------------------------------------------------------------------------

def execute_npc_action(decision: dict, session_id: str, tick: int, world_id: str = "test") -> dict or None:
    """把 NPC 的 decision 落成真实世界状态（效果层；决策痕迹仍由 fate 统一写）。

    Args:
        decision: agent.decide 的产物 {"agent","action":{type,target,location,detail},"intent"}。
    Returns:
        执行结果 dict（同 execute_player_action 口径）；
        None = 本动作类型不涉及环境写库（speak/wait/observe/trigger_event 由 fate 记痕迹）。
    """
    actor = str(decision.get("agent", ""))
    action = decision.get("action") or {}
    atype = str(action.get("type", "wait"))
    target = str(action.get("target", "") or "")
    detail = str(action.get("detail", "") or "")

    if atype not in ("move", "use_item", "give_item", "interact"):
        return None  # speak/wait/observe/trigger_event：不写环境，fate 记痕迹即可

    scene = db.get_npc_pos(session_id, actor) or str(action.get("location", "") or "")

    # ---- 移动：可达性校验（与玩家 _exec_move 同一规则源 spatial.can_reach）----
    if atype == "move":
        dest = _extract_dest_scene(target, world_id)
        if not dest:
            return {"outcome": "blocked", "message": "没有可辨认的目的地", "changed": False, "scene": scene}
        if scene == dest:
            return {"outcome": "ok", "message": f"已经在{dest}", "changed": False, "scene": scene}
        if not spatial.can_reach(scene, dest, world_id):
            db.add_world_trace(session_id, tick, actor, "move", dest, scene,
                               f"试图从 {scene} 走向 {dest}，但路不通")
            return {"outcome": "blocked", "message": "路不通。", "changed": False, "scene": scene}
        db.set_npc_pos(session_id, actor, dest)
        db.add_world_trace(session_id, tick, actor, "move", dest, scene,
                           f"从 {scene} 移动到 {dest}")
        # 足迹系统（用户裁决）：NPC"知道的房间"=去过的房间；首次到访写初到印象记忆
        try:
            from . import mind_engine
            mind_engine.note_visited(session_id, actor, dest, world_id)
        except Exception:  # noqa: BLE001
            pass
        return {"outcome": "ok", "message": f"移动到{dest}", "changed": True, "scene": dest}

    # ---- 拿取（use_item）：与玩家 pick 同一执行器（holder=NPC）----
    # 同场景校验（防隔空取物）：物品必须在 NPC 可及范围（所在场景或已持有）
    if atype == "use_item" and target:
        item_where = spatial.current_where({"env_id": target, "scene": "",
                                            "state": _item_state_dict(target, world_id)})
        item_scene = (item_where or {}).get("scene", "")
        if scene and item_scene and item_scene != scene:
            db.add_world_trace(session_id, tick, actor, "use_item", target, scene,
                               f"试图拿取 {target}，但它在 {item_scene}，不在可及范围")
            return {"outcome": "blocked", "message": f"{target} 不在可及范围（在 {item_scene}）",
                    "changed": False, "scene": scene}
        intent = Intent(domain="spatial", side_effect="mutating",
                        target={"type": "entity", "id": target, "hint": target},
                        verb=detail or "拿取",
                        spatial={"op": "pick", "item": target})
        return _exec_mutate(intent, session_id, tick, world_id, scene, actor)

    # ---- 给予（give_item）：把自己的持有物转交他人 ----
    if atype == "give_item" and target:
        recipient = detail.strip() or "player"
        w = {"mode": "held", "holder": recipient, "scene": scene,
             "position": None, "orientation": 0, "anchored_to": None}
        db.set_entity_where(target, w, world_id)
        db.add_world_trace(session_id, tick, actor, "give_item", target, scene,
                           f"把{target}交给了{recipient}")
        return {"outcome": "ok", "message": f"交出{target}", "changed": True, "scene": scene}

    # ---- 交互（interact）：开关门走专用执行器，其余记痕迹（语义由旁白续写）----
    if atype == "interact":
        op = "open" if ("开" in detail and "关" not in detail) else ("close" if "关" in detail else "other")
        intent = Intent(domain="spatial", side_effect="mutating",
                        target={"type": "entity", "id": target, "hint": target},
                        verb=detail or "交互",
                        spatial={"op": op, "item": target})
        return _exec_mutate(intent, session_id, tick, world_id, scene, actor)

    return None
