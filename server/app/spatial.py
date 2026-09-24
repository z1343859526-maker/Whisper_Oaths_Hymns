"""空间模型：程序做「空间粗筛」，AI 做「语义精配」（环境管线 P3+P2 接地气版）。

核心定位（与需求第七章 / 工作日志第二篇对齐）：
  - 空间查询 = 有限视角的实现机制：『你能看到什么』=『你所在区域查询的结果』；
  - 不需要运行时 3D 场景——3D 模型是建模期一次性数据源，运行时查的是 DB 空间骨架；
  - 程序做坐标算术（床边 1.5m 半径内有什么，微秒级），LLM 做语义对齐（"窗边反光的东西"→银匣）。

本模块提供的程序侧能力（全部纯 SQL/字典查询，不碰 LLM）：
  entities_in(scene)           # 该场景有哪些实体（空间骨架上的"有什么"）
  connected_scenes(room)       # 该房间连通哪些房间（可达性，room_1↔2↔3、room_1↔3断）
  resolve(hint, scene)         # 把"床边/窗边/房间三角落"翻译成空间位置（锚点+方位词）
  build_perception_snapshot()  # 拼一段"你所在+这里有+状态+痕迹"的感知快照段（目标1）

与 environment_card 的分工：
  environment_entity 提供"静态骨架"（这场景有哪些实体、在哪、连通谁）；
  environment_card.state 提供"动态状态"（钥匙在不在、门开没开）；
  world_trace 提供"痕迹"（谁在这做过什么）。三者合并 = 完整感知。

规则：不让 LLM 算坐标，不让程序写叙述——本模块只给结构化事实，文字由上层 LLM 生成。
"""
import json

from . import db
from . import world_pack


# ---------------------------------------------------------------------------
# ① 实体查询：entities_in(scene) —— 该场景有哪些实体
# ---------------------------------------------------------------------------
def entities_in(scene, world_id="golden"):
    """查某场景当前【实际上】有什么实体（含房间自身 + 家具 + 关键物 + 建筑构件）。

    ⚠️ 修正（关键）：不再是"按静态 entity.scene 分组"，而是按【动态归属 where】分组。
      一个物品被拿起/搬到另一房间后，它在哪由 state.where.scene 决定（current_where），
      而不再由 entity.scene（出厂挂载）决定。这正是"把椅子搬到 room_1 后，
      entities_in(room_1) 要能查到它"的根因修复。

    返回 list[dict]，每个实体带 (env_id, scene, name, type, position, size, orientation,
    is_anchor, anchor_label, source, state)，并附上其环境卡的当前 state（若存在）。

    对观察者的裁剪在 build_perception_snapshot 里做；本函数是"客观上该场景当前有什么"。
    """
    rows = db.get_environment_entities(None, world_id)   # 该世界全部实体（含静态骨架）
    out = []
    for row in rows:
        d = _row_to_entity(row, world_id)
        # 房间本身永远"在"（它自己就是场景），只匹配自己那间；房间不收进别的房间
        if d["type"] == "room":
            if d["scene"] == scene:
                out.append(d)
            continue
        # 非房间实体：按动态归属判断"现在在不在这个房间"
        w = current_where(d)
        if w and w.get("mode") == "held":
            continue   # 在某人手上：不属于任何房间的原位物（观察者视角需另外感知"被拿着"）
        eff_scene = w.get("scene") if w else d["scene"]
        if eff_scene == scene:
            out.append(d)
    return out


# ---------------------------------------------------------------------------
# ①.5 背包数据源：get_held_items(world_id, holder) —— 玩家身上拿了什么
# ---------------------------------------------------------------------------
def get_held_items(world_id="golden", holder="player"):
    """列出当前被某持有者（典型=player）拿在身上的物品——背包系统的数据源。

    ⚠️ 为什么不能用 entities_in：entities_in 刻意跳过 mode=held 的实体（它们不在任何
    房间原位，见第 53 行 continue），而背包恰恰要的就是"被拿在手上"的那批。所以必须
    直接遍历全部环境实体卡，用 current_where 归一化后再按 holder 过滤。

    返回 list[dict]：{"env_id","name","type","holder"}，全部取自当前动态状态（where），
    与感知快照读的是同一套事实，保证"玩家看见自己拿了什么"与"世界认为他拿了什么"一致。
    """
    rows = db.get_environment_entities(None, world_id)
    out = []
    for row in rows:
        d = _row_to_entity(row, world_id)
        if d["type"] == "room":
            continue  # 房间是场景本身，不算可拿物品
        w = current_where(d)
        if w and w.get("mode") == "held" and w.get("holder") == holder:
            out.append({"env_id": d["env_id"], "name": d["name"],
                        "type": d["type"], "holder": w.get("holder")})
    return out


def _row_to_entity(row, world_id="golden"):
    """把 environment_entity 的一行（tuple）转成 dict，并附上环境卡当前 state。

    state 合并进来供 current_where / build_perception_snapshot 判断"此刻怎样"。
    """
    env_id, rscene, name, etype, pos, size, orient, bounds, frame, conn, anchor, anchor_label, source = row
    d = {
        "env_id": env_id,
        "scene": rscene,
        "name": name,
        "type": etype,
        "position": _load(pos),
        "size": _load(size),
        "orientation": orient,
        "is_anchor": bool(anchor),
        "anchor_label": anchor_label,
        "source": source,
    }
    state_raw = db.get_environment_state(env_id, world_id)
    d["state"] = _load(state_raw) if state_raw else {}
    return d


def current_where(entity):
    """归一化某物品的【当前空间归属 where】。

    优先级：state.where（新模型）→ 旧字段推导（holder / current_place）→ entity.scene（出厂）。

    ⚠️ 为何要统一在这里归一化：原实现里"物品在哪"分散在 holder/current_place/scene
    三个字段，下游各读各的，导致"搬到别处"无法统一反映。这里收口成一张 where，
    所有下游（entities_in / build_perception_snapshot / resolve）都读它，一处修正全局生效。
    返回 dict：{"mode","holder","scene","position","orientation","anchored_to"}。
    """
    st = entity.get("state") or {}
    w = st.get("where")
    if isinstance(w, dict):
        return {
            "mode": w.get("mode", "in_place"),
            "holder": w.get("holder"),
            "scene": w.get("scene") or entity.get("scene"),
            "position": w.get("position"),
            "orientation": w.get("orientation", entity.get("orientation", 0)),
            "anchored_to": w.get("anchored_to"),
        }
    # 旧字段推导（兼容既有 seed 与 plan_executor 旧写回：state=held, holder=xxx）
    holder = st.get("holder")
    if holder:
        return {"mode": "held", "holder": holder, "scene": None,
                "position": None, "orientation": entity.get("orientation", 0), "anchored_to": None}
    cp = st.get("current_place")
    if cp:
        return {"mode": "in_place", "holder": None, "scene": cp,
                "position": None, "orientation": entity.get("orientation", 0), "anchored_to": None}
    return {"mode": "in_place", "holder": None, "scene": entity.get("scene"),
            "position": None, "orientation": entity.get("orientation", 0), "anchored_to": None}


def _load(raw):
    """把 JSON 字符串/字典解成 Python 对象；失败安全返回 None。"""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# ② 连通性：connected_scenes(room) / can_reach(from, to)
# ---------------------------------------------------------------------------
def connected_scenes(room, world_id="golden"):
    """查某房间连通哪些场景（可达性）。room_1↔2、room_2↔3，room_1↔3 不通。"""
    return db.get_connected_scenes(room, world_id)


def can_reach(frm, to, world_id="golden"):
    """判断能否从 frm 直接到 to（考虑门开不开）。

    先看空间连通性（connected_to 里是否有 to）；若房间连通但门关了，
    返回 False（门的开关由环境卡 state['door_open'] 决定）。
    这是"空间逻辑能被交互感知验证"（目标3）的核心：连不通/门关着都走不了。
    """
    if to not in connected_scenes(frm, world_id):
        return False
    # 门状态：环境卡 state['door_open'] 若显式为 False 则门关，不可过
    state = _load(db.get_environment_state(frm, world_id)) or {}
    if state.get("door_open") is False:
        return False
    return True


# ---------------------------------------------------------------------------
# ③ 方位解析：resolve(hint, scene) —— 把"床边/窗边/房间三角落"翻译成位置
#    只做程序能确定的两级（锚点/方位词），LLM 语义对齐兜底后续可加（reside in intent.target.hint）
# ---------------------------------------------------------------------------
def resolve(hint, scene, world_id="golden"):
    """玩家/NPC 说"房间三角落""床边"→ 翻译成空间区域，用于区域查询。

    返回 dict：{"anchor": 命中的锚点实体 env_id 或 "", "region": 描述, "scene": scene}
    轻量版：在 scene 的实体里按 anchor_label/名字 匹配 hint 里的方位词，
    命中锚点就返回该锚点（下游可再查"锚点附近有什么"）；未命中返回空锚点。

    Args:
        hint: 自然语言方位提示（如"房间三角落""床边"）。
        scene: 所在场景 id。
    Returns:
        {"anchor": env_id|"", "region": str, "scene": str}。
    """
    if not hint:
        return {"anchor": "", "region": "", "scene": scene, "position": None}

    # ① 锚点命中：遍历"当前在该场景的实体"，按锚点标签/名字匹配
    for e in entities_in(scene, world_id):
        if e["type"] == "room":
            continue  # 房间自身不作锚点目标（它本身就是场景）
        if e.get("anchor_label") and e["anchor_label"] in hint:
            return {"anchor": e["env_id"], "region": e.get("anchor_label") or e["name"],
                    "scene": scene, "position": _resolve_relative(e, hint)}
        if e.get("name") and e["name"][:2] in hint:
            return {"anchor": e["env_id"], "region": e.get("anchor_label") or e["name"],
                    "scene": scene, "position": _resolve_relative(e, hint)}

    # ② 场景级方位（无锚点，但 hint 提到"墙角/西南角/墙根"等 → 用房间 bounds 算坐标）
    pos = _resolve_scene_region(hint, scene, world_id)
    if pos is not None:
        return {"anchor": "", "region": hint, "scene": scene, "position": pos}

    # ③ 未命中：开放世界兜底，交给上层 LLM 语义精配
    return {"anchor": "", "region": "", "scene": scene, "position": None}


def _resolve_relative(anchor_entity, hint):
    """以锚点为基准，把 hint 里的相对方位词翻译成坐标偏移点。

    ⚠️ 这是"程序做相对位置推理"的核心（对应需求③）：
       LLM 只输出语义（"椅子放在锚点东边"），程序算坐标。铁律：LLM 不算坐标。
    返回一个候选坐标 [x,y,z]（无锚点坐标则 None，交由场景级方位兜底）。
    """
    base = anchor_entity.get("position")
    if not base:
        return None
    # 方位词 → 单位偏移（X=东，Y=北，Z=上；测试世界东西向排布）
    dx, dy = 0.0, 0.0
    if any(k in hint for k in ("东", "right", "右", "向东")):
        dx += 1.0
    if any(k in hint for k in ("西", "left", "左", "向西")):
        dx -= 1.0
    if any(k in hint for k in ("北", "后", "后边", "向北")):
        dy += 1.0
    if any(k in hint for k in ("南", "前", "前边", "向南")):
        dy -= 1.0
    if any(k in hint for k in ("边", "旁", "附近", "周围", "旁边")):
        # 无明确方向 → 就落在锚点旁默认偏移
        dx = dx or 0.6
        dy = dy or 0.6
    return [round(base[0] + dx, 2), round(base[1] + dy, 2), base[2] if len(base) > 2 else 0.0]


def _resolve_scene_region(hint, scene, world_id):
    """场景级相对方位：用房间的 bounds（AABB）把"墙角/西南角/墙根"翻译成坐标点。

    测试世界坐标系：+X=东、-X=西、+Y=北、-Y=南、+Z=上（东向排布，见 seed_test_spatial）。
    返回 [x,y,z] 或被当作提示词未命中任何方位 → None（交由上层 LLM 兜底）。
    """
    rows = db.get_environment_entities(scene, world_id)
    room_row = next((r for r in rows if r[0] == scene and r[3] == "room"), None)
    if not room_row or not room_row[7]:
        return None
    b = _load(room_row[7]) or {}
    if not b:
        return None
    xmin, xmax = b.get("x_min"), b.get("x_max")
    ymin, ymax = b.get("y_min"), b.get("y_max")
    zmax = b.get("z_max", 3.0)
    has_corner = any(k in hint for k in ("角", "角落", "墙角", "墙角根"))
    has_wall = any(k in hint for k in ("墙", "墙根", "墙边", "靠墙"))
    if not (has_corner or has_wall):
        return None
    # 方向分量
    x = _pick_axis(hint, "东", "西", xmin, xmax)
    y = _pick_axis(hint, "北", "南", ymin, ymax)
    if x is None and y is None:
        return None
    x = x if x is not None else 0.0
    y = y if y is not None else 0.0
    return [round(x, 2), round(y, 2), 0.0]


def _pick_axis(hint, pos_word, neg_word, lo, hi):
    """按方位词在 lo/hi 之间挑一个轴坐标；未命中返回 None。"""
    if any(k in hint for k in (pos_word,)):
        return hi
    if any(k in hint for k in (neg_word,)):
        return lo
    return None


# ---------------------------------------------------------------------------
# ④ 感知快照：build_perception_snapshot(scene) —— 拼一段给 LLM 的环境感知段（目标1）
#    这是"共享感知地基"：玩家问环境用、玩家对话时给同房间感知用、未来 NPC 决策用
# ---------------------------------------------------------------------------
def build_perception_snapshot(scene, session_id="seed", world_id="golden", observer="player", extra_state=None):
    """把某场景的「当前状态」拼成一段注入 prompt 的自然语言感知快照。

    读三层：
      ① 场景本身（environment_entity bounds / environment_card 厚描述与当前状态）
      ② 该场景实体（entities_in，各自当前 state）
      ③ 该场景最近痕迹（world_trace 里 location=scene 的）
    再加一层观察者裁剪（observer）：对玩家，已被某人拿走的物品（holder 非空）不再显示
    为"还在原位"——这是"玩家到一个地方看到的不是全知的"的最小实现（目标1/3）。

    返回一段文字，供上层拼进 system/user prompt。文字由程序粗筛拼出人话，
    不让 LLM 自己算"这里有什么"（它不可靠）；"你看见什么样子"的润色交给后续 LLM。

    Args:
        scene: 场景 id（如 room_2；空则返回空段）。
        session_id: 会话/轮回标识——痕迹按会话读（世界痕迹单轮内）。
        world_id: 世界维度。
        observer: "player" 或 npc_id。player 简化处理：被拿走的物品不再显示为原位。
        extra_state: 额外状态 dict，可注入（如玩家刚从某处来）。
    Returns:
        str：感知快照段（多行），空场景返回 ""。
    """
    if not scene:
        return ""

    lines = [f"【你所在】{_scene_label(scene, world_id)}"]

    # ①.5 场景厚描述（环境卡 description）——防幻觉：给 LLM 的环境"固定结构/陈设"设定。
    # 此前快照只给场景名+实体状态，LLM 不知道"这房间没有窗、只有门和墙"这类结构设定，
    # 便凭"房间"常识自由发挥（如旁白冒出"窗框"）。注入环境卡厚描述，让叙述有据可依。
    # 只描述房间自身的结构/氛围，不碰物品动态状态（物品由【这里有】按当前 state 反映）。
    sc_desc = _scene_description(scene, world_id)
    if sc_desc:
        lines.append("【这里的样子】" + sc_desc)
    # 差异标注（目标③，09-08 用户反馈）：原属本场景的可移动物若已离开原位（held/placed），
    # 点明其"不在这"。否则 LLM 会被静态 description（如"角落躺着一把刀"）误导，
    # 叙述与【这里有】（动态，空）自相矛盾——出戏根因B。
    departed = _departed_items(scene, world_id, observer)
    if departed:
        lines.append("（本场景原本惯常摆放的物件已不在原位：" + "；".join(departed)
                     + "。【这里的样子】只是房间惯常陈设，不代表这些物件此刻仍在。）")

    # ① 场景实体（含各自当前 state，已按观察者裁剪）
    entities = entities_in(scene, world_id)
    visible = [e for e in entities if _visible_to(e, observer)]
    desc = []
    for e in visible:
        if e["type"] == "room":
            continue  # 房间自身已在"你所在"，不重复列
        desc.append(_describe_entity(e))
    # ①.5 在场的人/尸体（NPC 也是可感知对象；dead → 标记为尸体）。
    # 用 db 直接查（npc_pos==scene），与 observation._npcs_in_scene 同源但独立实现，
    # 避免 spatial→observation 的潜在循环依赖。让感知层知道"这躺着一个人（或一具尸体）"。
    present = _npcs_present(session_id, scene, world_id, observer)
    # 把"实体"+"在场的人"合进同一条【这里有】，避免"似乎空无一物"与"有个男人/一把刀"
    # 同时存在的自相矛盾（09-08：场景有人或有物时不该说空无一物）。真的啥都没有才说空无一物。
    items = desc + present
    if items:
        lines.append("【这里有】" + "；".join(items))
    else:
        lines.append("【这里有】似乎空无一物")

    # ② 最近痕迹（该场景发生过什么）——按当前会话读
    # 09-08 叙述去破墙：对【玩家】观察者，不再机械拼 "actor：detail"（会拆成多条、带系统腔，
    # 如 "test_man：缓步走向…；test_man：从 room_1 移到 room_2"），而是按 actor 分组、把同一人
    # 的多条动作并成一句自然陈述。对 NPC 观察者（其感知 prompt）保留原始 actor：detail 便于判断。
    # 感知分层（09-08 问题3/第二步）：传 observer，按 visible_to 裁剪"不同人看到不同版本"。
    # 隐藏行动/暗处动作（visible_to={"hide":true}）对他人不可见 → 认知局限生效。
    traces = db.get_recent_traces_by_scene(session_id, scene, limit=5, observer=observer)
    if traces:
        if observer == "player":
            # 按 actor 分组；同一人的多条痕迹去重后合并成"他做了X，又做了Y"
            grouped: dict = {}
            order: list = []
            for t in traces:
                actor = t[1]
                detail = (t[5] or t[2] or "").strip()
                if not actor or not detail:
                    continue
                # 09-08 去元叙述/自查：actor==fate（齿轮 phase / 导演 director）是"世界元叙述"，
                # 前端已单独渲染为旁白/二级等待词，不应作为"你注意到"的场景事实，更不能被
                # narrate_scene 文学化进旁白；actor==player 是玩家自己做的，无需作为"你注意到"。
                if actor == "fate":
                    continue
                if actor == "player":
                    continue
                dname = _observer_npc_label(session_id, actor, observer, world_id)
                if dname not in grouped:
                    grouped[dname] = []
                    order.append(dname)
                if detail not in grouped[dname]:
                    grouped[dname].append(detail)
            seen = []
            for dname in order:
                acts = "，".join(grouped[dname])
                seen.append(f"{dname} {acts}")
        else:
            # NPC 观察者：保留 actor：detail 便于它判断（但仍剔除 fate 元叙述；玩家痕迹保留，
            # 因为 NPC 应当感知"玩家做了某件事"，这是玩家行动对它可见的部分）。
            seen = [f"{t[1]}：{t[5] or t[2]}" for t in traces
                    if t[1] and (t[5] or t[2]) and t[1] != "fate"]
        if seen:
            lines.append("【你注意到】" + "；".join(seen))

    # ③ 额外状态注入（可选）
    if extra_state:
        for k, v in extra_state.items():
            lines.append(f"【状态】{k}：{v}")

    return "\n".join(lines)


def _visible_to(entity, observer) -> bool:
    """观察者裁剪：这个实体此刻在这个观察者视角下"看得到/在原地"。

    统一读 current_where：mode=held（被某人拿着）→ 不在原位，玩家看不到它躺原处。
    这与 entities_in 的分组逻辑一致（held 不进任何房间的清单）。
    """
    w = current_where(entity)
    if entity["type"] in ("key_item", "furniture", "building") and w and w.get("mode") == "held":
        return False
    return True


def _state_summary(st) -> str:
    """把实体当前 state 里的关键位翻译成一句状态尾巴（供感知快照展示）。"""
    if not st:
        return ""
    parts = []
    if st.get("state") == "held":
        parts.append("已被拿走")
    elif st.get("state") == "planted":
        parts.append("不在原位")
    if st.get("stained"):
        parts.append("已沾血")
    if st.get("door_open") is False:
        parts.append("门紧闭")
    return "（" + "，".join(parts) + "）" if parts else ""


def _scene_label(scene, world_id) -> str:
    """查场景的名字（供"你所在【X】"）。"""
    rows = db.get_environment_entities(scene, world_id)
    for r in rows:
        if r[0] == scene and r[3] == "room":
            return r[2]
    # 无实体记录就退回 id
    return scene


def _scene_description(scene, world_id) -> str:
    """查该场景环境卡的厚描述（description 简述）——房间的结构/氛围设定。

    感知快照注入用：让 LLM 知道"这房间长什么样/有没有窗/几面墙"，从而在叙述时
    以环境设定为准，而非凭"房间"常识自由发挥（这正是旁白冒出"窗"的根因）。
    只取 room 自身（kind=location）的 description；找不到返回空。
    """
    try:
        for env_id, _kind, _name, desc, _state, _p in db.get_environment_cards(world_id):
            if env_id == scene and _kind == "location" and desc:
                return str(desc)
    except Exception:  # noqa: BLE001  无环境骨架的世界安静跳过
        return ""
    return ""


def _departed_items(scene, world_id, observer="player"):
    """原属本场景的可移动物，此刻已离开原位（held/placed）——感知差异标注。

    为什么需要（09-08 用户反馈"房间说刀在角落、这里却空无一物"）：环境卡 description
    （【这里的样子】）是静态陈设，不随物品被拿走/挪动而更新。若物品已被人拿走，LLM 仍按
    "这房间该有把刀"叙述，与【这里有】的动态事实（无刀）自相矛盾，非常出戏。
    本函数列出"出厂属于本场景、但当前已不在原位"的可移动物，供 build_perception_snapshot
    注入差异说明，让叙述自然体现"空落落/刀不见了"，而不是坚持"刀还在角落"。

    只对玩家视角生效（observer=player）：玩家看到"这房间少了什么"；NPC 感知对话走
    独立路径，不在此标注（且 NPC 决策框架需要的是原始事实而非玩家视角差异）。

    Returns:
        list[str]，如 ["「一把刀」已不在原位（在「你」手中）"]；正常无变化返回空列表。
    """
    if observer != "player":
        return []
    out = []
    try:
        for row in db.get_environment_entities(None, world_id):
            if row[0] is None:
                continue
            rscene, name, etype = row[1], row[2], row[3]
            if etype in ("room", "building"):
                continue  # 房间/构件是场景结构本身，不属"可移动物"
            if rscene != scene:
                continue  # 出厂不属于本场景的物不标注
            d = _row_to_entity(row, world_id)
            w = current_where(d) or {}
            if w.get("mode") not in ("held", "placed"):
                continue  # 还在原位（in_place / 无 where）→ 不视为"离开"
            holder = w.get("holder")
            if holder == "player":
                where = "在「你」手中"
            elif holder:
                where = f"在「{holder}」手中"
            else:
                where = "被挪到别处"
            out.append(f"「{name}」已不在原位（{where}）")
    except Exception:  # noqa: BLE001  空间层异常不阻塞感知快照
        return []
    return out


def _describe_entity(e) -> str:
    """描述一个实体的【当前空间状态】（感知快照用）。

    ⚠️ 升级（对应目标②）：读 current_where 归一化后的动态归属，能体现"改变后状态"——
       如"倒着的椅子（在坐标[-13,3,0]）"，而非只会显示粗粒度"（不在原位）"。
      · 物品不在原位（placed/held）→ 用动态位置（坐标/持有者）表达，不用会失效的静态锚点标签；
      · 物品在原位（in_place）       → 用静态锚点标签 + 状态尾巴。
    """
    w = current_where(e) or {}
    name = e["name"]
    pose = "倒着的" if w.get("orientation") == 90 else ""
    mode = w.get("mode", "in_place")
    tails = []
    if mode in ("placed", "held"):
        # 已不在原位：优先给坐标/持有者（动态，准确）
        if w.get("position"):
            tails.append(f"在坐标{w.get('position')}")
        if w.get("holder"):
            tails.append(f"在{'你' if w.get('holder') == 'player' else w.get('holder')}手中")
    else:
        # 原位：用出厂锚点标签（静态，此时仍准确）
        if e.get("anchor_label"):
            tails.append(e["anchor_label"])
    st = _state_summary(e.get("state"))
    if st:
        tails.append(st)
    # 缺部件（你新增：物体被拆解后记录在 state.parts，如椅子少了两条腿）
    parts = (e.get("state") or {}).get("parts")
    if isinstance(parts, dict) and isinstance(parts.get("legs"), dict):
        n_removed = len(parts["legs"].get("removed") or [])
        if n_removed:
            tails.append(f"缺了{n_removed}条腿")
    body = "，".join([t for t in [pose] + tails if t])
    return f"{name}（{body}）" if body else name


def _display_label(npc_id: str, world_id: str = "golden") -> str:
    """把 npc_id 转成面向玩家的显示名（角色卡 name，如 test_man → 测试男）。

    09-08 去破墙：玩家感知快照里"你注意到"若直接用 npc_id（test_man）会带系统腔。
    取角色卡的第 2 列显示名；取不到则退回 id 本身。
    """
    try:
        card = db.get_character_card(npc_id)
        return card[1] if card and card[1] else npc_id
    except Exception:  # noqa: BLE001  角色卡缺失不阻断
        return npc_id


def _observer_npc_label(session_id, npc_id, observer, world_id) -> str:
    """按"观察者是否认识该 NPC"决定称呼：认识→名字，陌生→性别/身份指代。

    用于【你注意到】段对动作者的称呼，与 _npcs_present 的【这里有】认识分层保持一致：
    · 玩家视角 + 不认识 → "一个男人/女人"（初次见面不该直呼其名）；
    · 其它（玩家已认识 / NPC 观察者看人）→ 中文显示名。
    """
    if observer == "player" and not _is_acquainted(session_id, "player", npc_id, world_id):
        return _stranger_label(npc_id, world_id)
    return _display_label(npc_id, world_id)


def _is_acquainted(session_id, observer, npc_id, world_id) -> bool:
    """观察者（典型=玩家）是否已"认识"该 NPC（能叫出名字）。

    认识 ≠ 同场景（同场景只是"看到脸"，不等于认识）。这里的"认识"指：观察者与该 NPC
    有过直接互动（对话/观察对方/对方对自己说话），此后再提到它就用名字而不是"有个男人"。
    数据源：world_trace 里观察者与该 npc 之间的 speak/observe 双向痕迹。

    为什么用痕迹而不是关系表：relationships seed 里 test_man/test_woman 对玩家就是
    "初次照面的陌生人"（关系存在≠认识）。只有真正互动过才算认识，线索在痕迹流水里。
    """
    if observer == "player":
        pass
    elif observer == npc_id:
        return True   # 自己当然认识自己
    try:
        rows = db.execute_query(
            "SELECT 1 FROM world_trace WHERE session_id=%s AND "
            "((actor=%s AND target=%s) OR (actor=%s AND target=%s)) AND "
            "action_type IN ('speak','observe') LIMIT 1",
            (session_id, observer, npc_id, npc_id, observer))
        return bool(rows)
    except Exception:  # noqa: BLE001  痕迹查询失败按不认识兜底，不阻断
        return False


def _stranger_label(npc_id, world_id) -> str:
    """观察者与 NPC 尚陌生时，用"性别/身份"陌生指代而非名字（如"一个男人"）。

    初次见到不该直呼其名，应用"有个男人/女人"这类陌生描述，才能体现"我不认识他"的
    有限视角；认识后（_is_acquainted=True）才叫名字。性别从角色名里的性别字推断，
    推断不出退化成"一个人"。
    """
    try:
        nm = world_pack.npc_id_to_name(npc_id, world_id) or npc_id
    except Exception:  # noqa: BLE001
        nm = npc_id
    if any(w in nm for w in ("男", "先生", "骑士", "管家", "公爵", "王子", "勋爵", "兄")):
        return "一个男人"
    if any(w in nm for w in ("女", "小姐", "夫人", "女士", "公主", "娘")):
        return "一个女人"
    return "一个人"


def _npcs_present(session_id, scene, world_id="golden", observer="player") -> list:
    """当前场景「在场的人/尸体」的可感知描述（供 build_perception_snapshot 用）。

    与 observation._npcs_in_scene 同源（npc_pos==scene 判在场），但独立实现、避免
    spatial→observation 循环依赖：只读 db，拼成"一个活人/一具尸体"的自然描述。

    ★ 认识分层（问题3，09-08）：对玩家观察者，按"是否已认识"决定称呼——
       · 认识：直接叫名字（测试男）；
       · 陌生：用性别/身份陌生指代（一个男人）——初次见面不该直呼其名，体现有限视角。
       对 NPC 观察者（它看别人）保留名字：NPC 之间往往已见过/彼此留意，用名字更自然。

    尸体（dead npc）：npc_pos 不变、状态 dead → 标记为【尸体】，让感知层知道"这躺着
    一个人（已死）"。观察者=自己（player）时，把它自己也排除（不该感知到自己）。
    """
    out = []
    try:
        for npc_id in db.get_all_npc_ids(world_id):
            if db.get_npc_pos(session_id, npc_id) != scene:
                continue
            if observer == "player" and npc_id == "player":
                continue
            card = db.get_character_card(npc_id)
            name = card[1] if card else npc_id
            # 认识分层：玩家视角陌生 → 性别指代；其余（认识者/NPC 观察者）→ 名字
            if observer == "player" and not _is_acquainted(session_id, "player", npc_id, world_id):
                name = _stranger_label(npc_id, world_id)
            dead = bool(db.get_npc_status(session_id, npc_id).get("dead"))
            # 去破墙标注（09-08）：不写"（人，活着）/（已死）"这类系统标签。
            # 死者直接说成"物品化的尸体"（自然融入场景），活人只报名字——像 DM 描述一样。
            if dead:
                out.append(f"{name}（已然是一具冰冷的尸体）")
                continue
            # 观察他人情绪/神情（P0-a，用户拍板纳入）：同场景活人是"可感知对象"，
            # 神情从心智热态派生只读快照（read_emotion_snapshot），且只给"有明显情绪"
            # （非平静）的注入——平静不编造，"神情=平静"是废话。单一事实源：绝不读 npc_status.mood。
            try:
                from . import mind_engine
                snap_emo = mind_engine.read_emotion_snapshot(session_id, npc_id)
                mood_word = snap_emo.get("word", "")
                if mood_word and mood_word != "平静":
                    # 观察者与目标不同时，补充"他神情=XX"，让同场景的人能看到彼此情绪
                    out.append(f"{name}（神情{mood_word}）")
                    continue
            except Exception:  # noqa: BLE001  神情派生失败不阻断（至少报名字）
                pass
            out.append(f"{name}")
    except Exception:  # noqa: BLE001  感知层失败不阻断场景描述
        pass
    return out


# ---------------------------------------------------------------------------
# ⑤ 默认落点推理（你新增需求：玩家只说"放房间1"没说放哪 → 也给出合适落点+坐标）
#    职责分界（维持铁律）：
#      · 程序：筛出该场景可作落点的锚点候选、挑兜底默认、算坐标；
#      · LLM（意图层挑）：从候选里"挑放哪个锚点旁边最合理"——只挑语义、绝不摸坐标。
#    据此，"把椅子腿放房间1"（无方位）→ 程序给锚点候选 → LLM 挑"床头旁"→ 程序算坐标。
# ---------------------------------------------------------------------------
def anchor_candidates(scene, world_id="golden"):
    """该场景可作落点参照的锚点候选（程序粗筛）。

    只收"有坐标的锚点"（is_anchor=1 且 position 非空），供默认落点挑。
    Returns: list[dict]：{env_id, name, anchor_label, position}。
    """
    out = []
    for e in entities_in(scene, world_id):
        if e["type"] == "room":
            continue  # 房间自身不作为落点参照
        if e.get("is_anchor") and e.get("position"):
            out.append({"env_id": e["env_id"], "name": e["name"],
                        "anchor_label": e.get("anchor_label") or e["name"],
                        "position": e["position"]})
    return out


def pick_default_anchor(scene, world_id="golden"):
    """程序兜底：LLM 不可用/挑不出时，挑一个确定性默认落点锚点（取第一个）。

    Returns: (env_id|"", position|None)。无锚点返回 ("", None)，调用方退回场景中央。
    """
    cands = anchor_candidates(scene, world_id)
    if not cands:
        return "", None
    return cands[0]["env_id"], cands[0]["position"]


def scene_center(scene, world_id="golden"):
    """查房间中心坐标（bounds 几何中心）；无 bounds 返回 None。

    「无锚点时的默认落点」兜底：往房间中央放总是合理的。
    """
    rows = db.get_environment_entities(scene, world_id)
    room = next((r for r in rows if r[0] == scene and r[3] == "room"), None)
    if not room or len(room) <= 7:
        return None
    b = _load(room[7]) or {}
    if not b:
        return None
    cx = (b.get("x_min", 0) + b.get("x_max", 0)) / 2
    cy = (b.get("y_min", 0) + b.get("y_max", 0)) / 2
    return [round(cx, 2), round(cy, 2), 0.0]


def near_position(anchor_env_id, world_id="golden", offset=0.6):
    """给定锚点 env_id，返回"放在它旁边"的一个坐标（程序算，默认偏移 0.6 米）。

    用于意图已挑好 ref_anchor（如"床头旁"）后，翻译成具体坐标。
    Returns: [x,y,z] 或 None（锚点无坐标/不存在）。
    """
    ent = db.get_environment_entity(anchor_env_id, world_id)
    if not ent or len(ent) <= 5:
        return None
    base = _load(ent[5])
    if not base or len(base) < 2:
        return None
    return [round(base[0] + offset, 2), round(base[1] + offset, 2),
            base[2] if len(base) > 2 else 0.0]
