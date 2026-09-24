"""数据库访问层：封装 pymysql 连接与查询，供上层业务复用。

职责：
- 从 config 读取 MySQL 连接参数（不在这里硬编码密码）；
- 提供 execute_query 通用查询 + 业务查询函数；
- 参数化查询，防止 SQL 注入。
"""
import json

import pymysql

from .config import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB


def get_connection():
    """创建一条 MySQL 连接。

    Returns:
        pymysql.Connection 连接对象（用完须 close）。
    """
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DB,
        charset="utf8mb4",  # 与建表字符集一致，中文不乱码
    )


def execute_query(sql, params=None):
    """执行查询 SQL 并返回全部结果。

    Args:
        sql: 查询语句（含 %s 占位符）。
        params: 参数元组/列表，与占位符一一对应，缺省 None 表示无参数。

    Returns:
        list[tuple]：查询结果，每行一个元组；无结果返回空列表。
    """
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()
    finally:
        conn.close()  # 无论成功失败都关连接，避免连接泄漏


def get_character_card(npc_id):
    """查某 NPC 的角色卡（人设/动机/禁区）。P4-A：让 /chat 认识"这个 NPC 是谁"，不再裸调。

    Returns:
        一行 (npc_id, name, title, personality, background, motivation, forbidden, knowledge_scope)；
        未找到或未启用时返回 None。
    """
    sql = (
        "SELECT npc_id, name, title, personality, background, motivation, forbidden, knowledge_scope "
        "FROM character_card WHERE npc_id=%s AND is_active=1"
    )
    rows = execute_query(sql, (npc_id,))
    return rows[0] if rows else None


def get_npc_observe_card(npc_id):
    """取 NPC 的「观察卡」：name/title/appearance(外貌)/personality(性格)/background(来历)。

    为什么独立成函数而不扩 get_character_card：后者按列号被 context_builder/agent 多处耦合
    （card[1]~card[7]），中间插列会静默错位；这里单独 SELECT 出观察要用的字段，互不干扰。
    （对应重构文档 §10.3：第 4 期改 agent 时统一解耦。）
    Returns:
        (name, title, appearance, personality, background)；未找到返回 None。
    """
    sql = ("SELECT name, title, appearance, personality, background "
           "FROM character_card WHERE npc_id=%s AND is_active=1")
    rows = execute_query(sql, (npc_id,))
    return rows[0] if rows else None


def get_memories_by_importance(npc_id):
    """查某 NPC 的记忆，按重要性降序（最重要的先看）。"""
    sql = (
        "SELECT id, memory_type, content, importance "
        "FROM npc_memory WHERE npc_id=%s ORDER BY importance DESC"
    )
    return execute_query(sql, (npc_id,))


def get_relations_by_trust(npc_id):
    """查某 NPC 对谁最信任，按信任值降序。"""
    sql = (
        "SELECT other_id, relation_type, trust "
        "FROM relationships WHERE npc_id=%s ORDER BY trust DESC"
    )
    return execute_query(sql, (npc_id,))

def get_relationship(npc_id, other_id, session_id):
    """查某会话里 NPC 对某对象的单条关系（trust/fear/affection + 定性 + 备注）。

    Args:
        session_id: 会话标识（M1.1 隔离维度）。会话创建时从 seed 复制过初始关系，
            此后各会话只读自己的行；'seed' = 初始模板本身。
            无默认值是刻意的：漏传就读错会话是静默 bug，强制调用方显式传。

    Returns:
        一行 (trust, fear, affection, relation_type, notes)；无记录返回 None。
    """
    sql = (
        "SELECT trust, fear, affection, relation_type, notes "
        "FROM relationships WHERE session_id=%s AND npc_id=%s AND other_id=%s"
    )
    rows = execute_query(sql, (session_id, npc_id, other_id))
    return rows[0] if rows else None

def update_relationship(npc_id, other_id, session_id, delta_trust=0, delta_affection=0, delta_fear=0):
    """在「本会话的」关系行上累加信任/好感/恐惧增量，数值自动夹在 0~100。

    Args:
        session_id: 会话标识——UPDATE 只动本会话的行，A 会话刷的好感
            不会串进 B 会话（R8 状态串扰在本表的根治点）。

    为什么用一条 UPDATE 的 GREATEST/LEAST 而不是"先查再写"：
    - 原子：读-改-写是三步，并发时两个请求会互相覆盖、丢更新；一条 SQL 由 MySQL 原子执行；
    - GREATEST(0, x) 取下限 0，LEAST(100, x) 取上限 100，两者一套就是"夹在 [0,100]"。
    """
    sql = (
        "UPDATE relationships SET "
        "trust = LEAST(100, GREATEST(0, trust + %s)), "
        "affection = LEAST(100, GREATEST(0, affection + %s)), "
        "fear = LEAST(100, GREATEST(0, fear + %s)), "
        "last_interaction_at = NOW() "
        "WHERE session_id=%s AND npc_id=%s AND other_id=%s"
    )
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (delta_trust, delta_affection, delta_fear, session_id, npc_id, other_id))
            affected = cursor.rowcount
        conn.commit()
        return affected  # 1=更新成功；0=本会话没有这条关系记录
    finally:
        conn.close()


def get_avg_ooc_by_npc(min_avg=0.8, min_samples=1):
    """按 NPC 分组统计平均 OOC 评分，只保留人设不稳（平均分过低）的 NPC。

    AGGREGATION：# 1. GROUP BY 按 NPC 分组
                 # 2. AVG/COUNT 聚合：ooc_score 越高越稳定
                 # 3. COUNT(列) 而非 COUNT(*)：只统计有分数的行，自动跳过 NULL
    HAVING：分组后过滤（WHERE 只能过滤行，不能过滤分组）。
    只统计 speaker='npc' 的发言（player 的 ooc_score 为 NULL，无需参与）。
    """
    sql = (
        "SELECT npc_id, COUNT(ooc_score) AS samples, AVG(ooc_score) AS avg_ooc "
        "FROM dialogue_log "
        "WHERE speaker=%s "
        "GROUP BY npc_id "
        "HAVING AVG(ooc_score) < %s AND COUNT(ooc_score) >= %s"
    )
    return execute_query(sql, ("npc", min_avg, min_samples))


def count_knowledge_by_access():
    """统计不同 access_level 的知识库条目数，按数量降序。

    GROUP BY + COUNT(*) 聚合 + ORDER BY 别名（MySQL 允许按 SELECT 别名排序）。
    """
    sql = (
        "SELECT access_level, COUNT(*) AS cnt "
        "FROM world_knowledge "
        "GROUP BY access_level "
        "ORDER BY cnt DESC"
    )
    return execute_query(sql)


def get_knowledge_candidates(npc_id, allowed_access, world_id="golden"):
    """查某 NPC 有权看到的知识候选：按世界过滤 + global 公共/该 NPC 私有先验 + access_level 以内。

    world_id：世界隔离维度（不同世界不同 RAG 库）。黄金乡=golden、测试世界=test。
    只有当前世界 world_id 的条目才进候选——测试角色【不会】检索到黄金乡公共知识，
    这是"测试世界不串乡"的根本保障。
    allowed_access 来自角色卡 knowledge_scope.can_access（如 ["public","faction","secret"]）。
    npc_id='global' = 当前世界的公共世界观；npc_id=该 NPC = 它"本来就知道"的私有先验。
    按 access_level 过滤：NPC 看不到的层级（如不该知道的 secret）根本不会进候选，
    也就不会被注入上下文，从源头实现"拒答不该知道的事"。
    """
    if not allowed_access:
        allowed_access = ["public"]
    placeholders = ",".join(["%s"] * len(allowed_access))
    sql = (
        "SELECT id, npc_id, category, title, content, access_level, embedding "
        "FROM world_knowledge "
        "WHERE world_id=%s "
        "AND (npc_id='global' OR npc_id=%s) "
        "AND access_level IN (" + placeholders + ") "
        "AND embedding IS NOT NULL"
    )
    return execute_query(sql, (world_id, npc_id, *allowed_access))


def update_knowledge_embedding(kid, embedding_json):
    """把某条知识的向量（JSON 字符串）写回 embedding 字段。"""
    sql = "UPDATE world_knowledge SET embedding=%s WHERE id=%s"
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (embedding_json, kid))
        conn.commit()
    finally:
        conn.close()


def get_dialogue_page(page, page_size, session_id=None):
    """分页查询对话日志，按 id 升序截取一页（可按会话过滤）。

    LIMIT+OFFSET 分页：LIMIT 每页条数，OFFSET 偏移量 = (page-1)*page_size。
    page 从 1 开始。参数化传值，防止 SQL 注入。
    session_id=None 表示跨会话全量（评测/看板视角）；传会话 id 则只看该局对话。
    """
    offset = (page - 1) * page_size
    if session_id:
        sql = (
            "SELECT npc_id, speaker, content, ooc_score "
            "FROM dialogue_log WHERE session_id=%s "
            "ORDER BY id LIMIT %s OFFSET %s"
        )
        return execute_query(sql, (session_id, page_size, offset))
    sql = (
        "SELECT npc_id, speaker, content, ooc_score "
        "FROM dialogue_log "
        "ORDER BY id "
        "LIMIT %s OFFSET %s"
    )
    return execute_query(sql, (page_size, offset))


def get_memory_chain(npc_id):
    """查某 NPC 的记忆及每条记忆的父记忆（自关联：npc_memory 自己连自己）。

    同一张表既当 parent 又当 child —— 用 LEFT JOIN 把每条记忆(id)连到它的父记忆(parent_id)。
    LEFT JOIN 而非 INNER JOIN：parent_id 为 NULL 的"根记忆"也要查出来，父列显示 NULL。
    """
    sql = (
        "SELECT child.id, child.memory_type, child.content AS child_content, "
        "       parent.content AS parent_content "
        "FROM npc_memory AS child "
        "LEFT JOIN npc_memory AS parent ON child.parent_id = parent.id "
        "WHERE child.npc_id=%s "
        "ORDER BY child.id"
    )
    return execute_query(sql, (npc_id,))


def get_npc_score_summary(npc_id):
    """查某 NPC 的平均分 + 历史最高/最低单条分（相关子查询）。

    子查询里 WHERE npc_id=d.npc_id 引用了外层别名 d —— 对外层每一行 d 都单独算一次，
    取该 NPC 自身的历史极值做对照，这就是「相关子查询」(correlated subquery)。
    """
    sql = (
        "SELECT d.npc_id, AVG(d.ooc_score) AS avg_ooc, "
        "       (SELECT MAX(ooc_score) FROM dialogue_log "
        "        WHERE npc_id=d.npc_id AND speaker='npc') AS best, "
        "       (SELECT MIN(ooc_score) FROM dialogue_log "
        "        WHERE npc_id=d.npc_id AND speaker='npc') AS worst "
        "FROM dialogue_log d "
        "WHERE d.npc_id=%s AND d.speaker='npc' "
        "GROUP BY d.npc_id"
    )
    return execute_query(sql, (npc_id,))


def get_schedule(npc_id):
    """查某 NPC 的排班（时间线原计划基线），按时间升序。

    Returns:
        list[(time, location, action, intent)]，time 为 'HH:MM' 字符串。
    """
    sql = (
        "SELECT time, location, action, intent "
        "FROM schedule WHERE npc_id=%s ORDER BY time"
    )
    return execute_query(sql, (npc_id,))


def get_all_npc_ids(world_id=None):
    """查某世界已启用的 NPC 标识（涌现引擎每 tick 遍历用）。

    world_id：世界隔离维度（M1.5，003 加列后启用）。
    黄金乡=golden（缺省）、测试世界=test。各世界独立角色池——
    测试世界模拟只会遍历 test_man/test_woman，绝不串进黄金乡角色（防 OOC）。
    world_id=None 返回全部（兼容旧调用，如 verify_m1_2 的契约断言不依赖此函数）。
    """
    if world_id is None:
        sql = "SELECT npc_id FROM character_card WHERE is_active=1"
        params = None
    else:
        sql = "SELECT npc_id FROM character_card WHERE is_active=1 AND world_id=%s"
        params = (world_id,)
    return [row[0] for row in execute_query(sql, params)]


# =====================================================================
# M1.5 泛化：世界作为数据驱动维度（004 迁移后）
#   world 表 = 世界清单（世界名从 DB 读，代码不再硬编码 _WORLD_NAMES）
#   environment_card 带 world_id：环境卡按世界隔离，测试角色看不到黄金乡环境
# =====================================================================

def get_world_meta(world_id):
    """查某世界的元数据（world_name/description）；未登记返回 None。"""
    sql = "SELECT world_id, world_name, description FROM world WHERE world_id=%s AND is_active=1"
    rows = execute_query(sql, (world_id,))
    return rows[0] if rows else None


def get_world_name(world_id):
    """按 world_id 取世界显示名；未登记返回 None（调用方兜底）。"""
    meta = get_world_meta(world_id)
    return meta[1] if meta else None


def list_worlds():
    """列出所有已启用世界（world_id, world_name）。供世界选择/诊断。"""
    return execute_query("SELECT world_id, world_name FROM world WHERE is_active=1 ORDER BY id")


def get_environment_cards(world_id="golden"):
    """查某世界的全部环境卡（地点 + 关键物），供涌现引擎组装感知快照。

    world_id：世界隔离维度（004 加列后启用）。测试世界只返回 test 的环境卡，
    绝不带出黄金乡的地窖/书房——这是"换任何世界观环境不串台"的根本保障。

    Returns:
        list[(env_id, kind, name, description, state, perception)]。
    """
    sql = (
        "SELECT env_id, kind, name, description, state, perception "
        "FROM environment_card WHERE world_id=%s"
    )
    return execute_query(sql, (world_id,))


def get_recent_traces(session_id, tick, limit=20):
    """查某会话在指定 tick 之前（含）最近的世界痕迹，倒序（最新在前）。

    倒序 + LIMIT：NPC 只需"最近发生了什么"，不必全量回溯，控制 token。
    """
    sql = (
        "SELECT tick, actor, action_type, target, location, detail, visible_to "
        "FROM world_trace WHERE session_id=%s AND tick<=%s "
        "ORDER BY tick DESC, id DESC LIMIT %s"
    )
    return execute_query(sql, (session_id, tick, limit))


def add_world_trace(session_id, tick, actor, action_type, target="", location="", detail=None, visible_to=None):
    """追加一条世界痕迹（只增不删，世界痕迹=历史流水）。

    visible_to 为 dict（谁能感知），转 JSON 存库；None 表示全可见。
    """
    import json
    vis = json.dumps(visible_to, ensure_ascii=False) if visible_to is not None else None
    sql = (
        "INSERT INTO world_trace "
        "(session_id, tick, actor, action_type, target, location, detail, visible_to) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
    )
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (session_id, tick, actor, action_type, target, location, detail, vis))
        conn.commit()
    finally:
        conn.close()


def update_environment_state(env_id, state_json, world_id="golden"):
    """更新某环境卡的 state（覆盖式，环境卡=当前状态，可被覆盖）。

    world_id：环境卡世界维度（004 迁移后）。同一 env_id 可存在于不同世界
    （如黄金乡和测试世界都可能有 knife），必须按 (world_id, env_id) 精确定位。
    """
    sql = "UPDATE environment_card SET state=%s WHERE env_id=%s AND world_id=%s"
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (state_json, env_id, world_id))
        conn.commit()
    finally:
        conn.close()


def get_environment_state(env_id, world_id="golden"):
    """查某环境卡的 state（JSON 字符串，可能为 NULL）；无此卡返回 None。

    world_id：按 (world_id, env_id) 定位，避免跨世界读取同 id 环境的状态。
    """
    sql = "SELECT state FROM environment_card WHERE env_id=%s AND world_id=%s"
    rows = execute_query(sql, (env_id, world_id))
    return rows[0][0] if rows else None


def get_environment_card_meta(env_id, world_id="golden"):
    """查某环境卡的元数据（name / description 简述 / detail 极详 / state）。用于对象级观察。

    为什么单独建一个而非复用 get_environment_state：观察需要"厚描述 + 深细节 + 当前状态"
    三者一起拼 prompt；若拆开查多次，代码散；这里一次取齐 name/description/detail/state。
    detail 为空（NULL）表示该物未写深层细节，观察只给简述。

    Returns:
        dict{"name","description","detail","state"}；无此卡返回 None。
    """
    sql = "SELECT name, description, detail, state FROM environment_card WHERE env_id=%s AND world_id=%s"
    rows = execute_query(sql, (env_id, world_id))
    if not rows:
        return None
    name, description, detail, state = rows[0]
    try:
        state = json.loads(state) if state else {}
    except (ValueError, TypeError):
        state = {}
    return {"name": name, "description": description, "detail": detail, "state": state}


def get_object_events(session_id, env_id, world_id="golden", limit=8):
    """查某物体的【过程历史】（world_trace 里 target 命中该 env_id 的事件，倒序）。

    用于观察"此物现在怎样"时结合"它经历过什么"——即"物体受影响后基于变化记录的合理推理"。
    world_trace.target 存的是操作作用对象 env_id（pick/place/move_body/disassemble/interact…都写它），
    所以按 target=env_id 就能捞到该物体被搬/被改/被用的完整流水。
    Returns: list[(tick, actor, action_type, location, detail)]。
    """
    sql = ("SELECT tick, actor, action_type, location, detail "
           "FROM world_trace WHERE session_id=%s AND target=%s "
           "ORDER BY id DESC LIMIT %s")
    return execute_query(sql, (session_id, env_id, limit))


def update_environment_detail(env_id, detail, world_id="golden"):
    """写某环境卡的 detail 列（观测级极详厚描述；对象级观察懒生成后回写持久）。"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE environment_card SET detail=%s WHERE env_id=%s AND world_id=%s",
                           (detail, env_id, world_id))
        conn.commit()
        return True
    finally:
        conn.close()


# =====================================================================
# 环境管线 · 本局观察快照（对象级观察的"记忆缓存"，game_state KV 承载）
#
# 为什么放 game_state 而不是新表：
#   观察快照是"这一局轮回"内的东西——玩家观察过一次物品，之后只要物品没变，就返回原话；
#   轮回（reset 换新会话）自然重置。game_state 本就是按会话隔离的 KV，放这里零迁移。
#
# 快照结构（value）：
#   {"content": "<该物上一次的观察叙述>", "fingerprint": "<该物当时 state 的指纹>", "version": 1}
#   指纹一致 ⇒ 物品没变 ⇒ 直接返回 content（前/后一致）；指纹变了 ⇒ 重新观察。
# =====================================================================

def get_observation_snapshot(session_id, env_id, world_id="golden"):
    """读本局某物品的观察快照；无返回 None。"""
    raw = get_game_state_map(session_id).get(f"obs:{env_id}:{world_id}")
    return dict(raw) if isinstance(raw, dict) else None


def save_observation_snapshot(session_id, env_id, world_id, content, fingerprint, version=1):
    """写入/刷新本局观察快照。content=观察叙述；fingerprint=物品 state 指纹；version=自增档号。"""
    upsert_game_state(session_id, f"obs:{env_id}:{world_id}",
                      {"content": content, "fingerprint": fingerprint, "version": version})
    return f"obs:{env_id}:{world_id}"


# =====================================================================
# 玩家运行时属性（game_state KV 'player_attrs'）
#   观察力 perception 影响"对象级观察"能展开多深的细节（>门槛才可看 detail 层）。
# =====================================================================

def get_player_attrs(session_id):
    """读玩家运行时属性 dict（观察力等）；无记录返回空 dict。"""
    raw = get_game_state_map(session_id).get("player_attrs")
    return dict(raw) if isinstance(raw, dict) else {}


def set_player_attr(session_id, key, value):
    """写玩家一个属性（合并进 player_attrs），如 perception。"""
    attrs = get_player_attrs(session_id)
    attrs[key] = value
    upsert_game_state(session_id, "player_attrs", attrs)
    return attrs


def patch_environment_state(env_id, key, value, world_id="golden"):
    """单 key 打补丁：读旧 state → 改一个 key → 写回（M1.5 效果落库专用）。

    为什么不用 update_environment_state（整卡覆盖）：计划的效果是"改单一状态位"，
    如把 knife.state 从 in_place 改成 held——若用整卡覆盖，就必须把整张物品卡
    的状态 JSON 重新拼一遍，容易丢其他字段（如 holder/current_place）。
    patch 语义只动一个 key，其余字段原样保留——贴近"世界被改了一小步"的现实。
    world_id：环境卡世界维度，与 get_environment_state 语义对齐。
    """
    state_str = get_environment_state(env_id, world_id)
    try:
        state = json.loads(state_str) if state_str else {}
    except (ValueError, TypeError):
        state = {}
    state[key] = value
    # 反向同步（T6 双写胶水的另一半）：patch 旧字段时同步派生 where——
    # 保证 where 读者（环境执行器/空间快照）与旧字段读者（计划前置）不脱节
    if key == 'holder':
        w = state.get('where') if isinstance(state.get('where'), dict) else {}
        w['mode'] = 'held' if value else w.get('mode', 'in_place')
        if value:
            w['holder'] = value
        else:
            w['mode'] = 'in_place'
            w.pop('holder', None)
        state['where'] = w
    elif key == 'current_place':
        w = state.get('where') if isinstance(state.get('where'), dict) else {}
        w['scene'] = value
        state['where'] = w
    update_environment_state(env_id, json.dumps(state, ensure_ascii=False), world_id)


# =====================================================================
# 环境管线 · 物品空间归属 where（统一空间位置模型，覆盖式写回）
#
# 为什么单独做这套而不是复用 patch_environment_state：
#   patch 是"单 key 打补丁"，适合"改一个状态位"（如把 knife.state 改 held）；
#   但"把椅子搬到房间一、放倒、放到墙角"是一次【位置+朝向+场景】的复合变化，
#   必须"一次写回覆盖旧的 where"——这就是物体改变后记录改变后的状态、覆盖原有。
#
# where 结构（写在 environment_card.state['where']，动态层，与静态 entity.scene 分离）：
#   {
#     "mode":"in_place|held|placed",       # 空间三态
#     "holder":"player|<npc_id>|null",     # mode=held 时
#     "scene":"room_1",                     # mode=in_place/placed 时的归属场景
#     "position":[x,y,z]|null,             # placed 时的具体坐标
#     "orientation":0|90,                  # 姿态角（放倒=90）
#     "anchored_to":"<env_id>|null"        # 依托的锚点
#   }
#
# 约定（贯穿 environment/spatial/context_builder）：
#   - entity.scene = 出厂挂载（静态骨架，它本来在哪）；
#   - state.where  = 实时归属（动态，被拿起/搬走后以它为准）；
#   - 读取"物品现在在哪个房间"一律读 where.scene（或旧字段推导），见 spatial.current_where。
# =====================================================================

def get_entity_where(env_id, world_id="golden"):
    """读某物品当前的空间归属 where（environment_card.state['where']）；无则返回 None。

    world_id：按 (world_id, env_id) 定位，与 get_environment_state 语义对齐。
    返回 None 表示该物品还没有写通过 where（可能是旧数据 seed / 未被操作过的实体），
    调用方（spatial.current_where）应回退用 entity.scene 等静态/旧字段推导。
    """
    st = get_environment_state(env_id, world_id)
    if not st:
        return None
    try:
        s = json.loads(st)
    except (ValueError, TypeError):
        return None
    where = s.get("where")
    return where if isinstance(where, dict) else None


def set_entity_where(env_id, where, world_id="golden"):
    """覆盖式写入某物品的空间归属 where（物体改变后记录新状态、覆盖原有）。

    Args:
        env_id: 物品/实体业务 id（如 knife、chair）。
        where: 新空间归属 dict（结构见上方模块注释）。
        world_id: 世界维度。

    Returns:
        写入后的 where 原样返回（供验证/日志）。

    为什么"整个 where 覆盖"而不是 patch 单 key：
    "搬到一个新坐标并放倒"是多个字段的联动变化（scene/position/orientation/mode），
    拆成多次 patch 会留下中间态（如 scene 已变但 position 还是旧的），
    违反"记录改变后的状态覆盖原有"的要求。整块覆盖保证 where 永远自洽。
    """
    st_str = get_environment_state(env_id, world_id)
    try:
        s = json.loads(st_str) if st_str else {}
    except (ValueError, TypeError):
        s = {}
    s["where"] = where
    # ---- 双写同步（T6 对接胶水）----
    # where 是环境管线（玩家/NPC 执行器）的新格式；state/holder/current_place 是
    # plan_executor 种子效果与前置判定（has_item 等）读的旧字段。两套读者并存，
    # 任何一边单写都会造成"玩家拿走了刀、计划前置却以为刀还在"的判定脱节。
    # 因此在唯一写入口处同步派生旧字段：mode=held → state=held+holder；
    # in_place/placed → state=in_place + holder 清空 + current_place=scene。
    mode = str(where.get("mode", ""))
    if mode == "held":
        s["state"] = "held"
        s["holder"] = where.get("holder")
    elif mode in ("in_place", "placed"):
        s["state"] = "in_place" if mode == "in_place" else s.get("state", "in_place")
        s["holder"] = None
        if where.get("scene"):
            s["current_place"] = where.get("scene")
    update_environment_state(env_id, json.dumps(s, ensure_ascii=False), world_id)
    return where


# =====================================================================
# 环境管线 · 运行时造物原语（路径A：拆解/分解出独立实体）
#
# 为什么需要（你问的"拆下两条椅子腿变独立物体"）：
#   现有环境卡/实体都是 seed 预置的，对象永远是"整张卡"。但"把椅子的腿拆下来"
#   是"从既有物体分解出新的独立物体"——必须运行时在 DB 里新插两张卡/实体行，
#   椅子本体再记录"少了部件"。这是一套全新的写入原语，之前只有"读/改归属"没有"建"。
#
# source 约定：
#   seed      = 作者拍的出厂物（静态骨架，reset 保留）；
#   runtime   = 玩家在某一轮里拆解/造出的物（属于"这一轮世界状态"，轮回 reset 须清掉）。
# reset 轮回时：先 delete_runtime_entities(清 runtime 造物) → 再恢复各卡 state=initial_state。
# =====================================================================

def create_environment_entity(env_id, world_id, scene, name, etype, position=None,
                              size=None, orientation=0, bounds=None, frame=None,
                              connected_to=None, is_anchor=0, anchor_label="", source="runtime"):
    """运行时插入一条环境实体（空间骨架行）。

    ⚠️ 这是"造物"的空间层：让"椅子腿"这种从既有物分解出的部件，
    成为能被 entities_in / resolve / set_entity_where 独立寻址的实体。
    source 默认 runtime（玩家这轮造出的，轮回时清掉）。

    Returns:
        插入成功返回 True；env_id 已存在则不覆盖（返回 False，调用方改用唯一 id）。
    """
    if get_environment_entity(env_id, world_id):
        return False
    sql = (
        "INSERT INTO environment_entity "
        "(env_id, world_id, scene, name, type, position, size, orientation, bounds, frame, "
        " connected_to, is_anchor, anchor_label, source) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    )
    params = (env_id, world_id, scene, name, etype,
              json.dumps(position) if position else None,
              json.dumps(size) if size else None,
              orientation,
              json.dumps(bounds) if bounds else None,
              json.dumps(frame) if frame else None,
              json.dumps(connected_to) if connected_to else None,
              int(is_anchor), anchor_label, source)
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
        conn.commit()
        return True
    finally:
        conn.close()


def create_environment_card(env_id, world_id, kind, name, description="", state=None, perception=None):
    """运行时插入一张环境卡（厚描述 + 初始/当前状态）。

    与 create_environment_entity 配套：造一个物体 = 实体(空间骨架) + 卡(状态/描述)两条。
    state 与 initial_state 同写：新造的物"出厂即当前"（初始状态就是它被拆出来那一刻的样子），
    这样本轮回里它始终有 where 可读；轮回 reset 时它会被 delete_runtime_entities 整行删掉。
    source 不留列（environment_card 无 source）——但靠 reset 前按实体 source=runtime 删除配套卡。

    Args:
        kind: 'location' | 'item'（新造的部件一般是 key_item 级物品→'item'）。
        state: 初始/当前状态 dict（常含 where：{mode:'in_place', scene, position, ...}）。
    Returns:
        插入成功 True；已存在返回 False。
    """
    if get_environment_state(env_id, world_id) is not None:
        return False
    state_json = json.dumps(state, ensure_ascii=False) if state is not None else None
    perception_json = json.dumps(perception, ensure_ascii=False) if perception else None
    sql = (
        "INSERT INTO environment_card "
        "(env_id, world_id, kind, name, description, state, initial_state, perception) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
    )
    params = (env_id, world_id, kind, name, description, state_json, state_json, perception_json)
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
        conn.commit()
        return True
    finally:
        conn.close()


def delete_runtime_entities(world_id):
    """删除某世界里所有 source=runtime 的运行时造物（实体 + 配套环境卡）。

    轮回 reset 前置：被玩家拆出的"椅子腿"属于这一轮，轮回=回到出厂，必须清掉，
    否则椅子腿残留会让下一轮出现"上一轮拆的腿还在"。先删实体行（空间骨架），
    再删对应环境卡行（状态/描述）——配套的两行一并清，避免孤儿卡。
    环境卡无 source 列，故按"该世界 environment_entity 里 source=runtime 的 env_id"反查删卡。
    Returns: 删除的实体行数。
    """
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT env_id FROM environment_entity WHERE world_id=%s AND source='runtime'",
                (world_id,))
            runtime_ids = [r[0] for r in cursor.fetchall()]
            if not runtime_ids:
                return 0
            # 删配套环境卡（用参数化，IN 长度=行数）
            placeholders = ",".join(["%s"] * len(runtime_ids))
            cursor.execute(
                f"DELETE FROM environment_card WHERE world_id=%s AND env_id IN ({placeholders})",
                (world_id, *runtime_ids))
            # 删实体行
            cursor.execute(
                f"DELETE FROM environment_entity WHERE world_id=%s AND env_id IN ({placeholders})",
                (world_id, *runtime_ids))
        conn.commit()
        return len(runtime_ids)
    finally:
        conn.close()


def get_environment_entities(scene=None, world_id="golden"):
    """查某世界的环境实体（空间骨架）；scene 指定则只查该场景的实体。

    world_id：世界隔离维度（005 迁移后）。测试世界只返回 test 的实体，
    绝不带出黄金乡——"换任何世界观环境不串台"在空间层的体现。

    Args:
        scene: 场景 id（如 room_2）；None 返回该世界全部实体。
    Returns:
        list[tuple]：每行 (env_id, scene, name, type, position, size, orientation,
                     bounds, frame, connected_to, is_anchor, anchor_label, source)
    """
    if scene:
        sql = (
            "SELECT env_id, scene, name, type, position, size, orientation, "
            "       bounds, frame, connected_to, is_anchor, anchor_label, source "
            "FROM environment_entity WHERE world_id=%s AND scene=%s"
        )
        return execute_query(sql, (world_id, scene))
    sql = (
        "SELECT env_id, scene, name, type, position, size, orientation, "
        "       bounds, frame, connected_to, is_anchor, anchor_label, source "
        "FROM environment_entity WHERE world_id=%s"
    )
    return execute_query(sql, (world_id,))


def get_environment_entity(env_id, world_id="golden"):
    """查单个环境实体（空间骨架）；无此实体返回 None。

    world_id：按 (world_id, env_id) 定位，与 get_environment_state 语义对齐。
    """
    sql = (
        "SELECT env_id, scene, name, type, position, size, orientation, "
        "       bounds, frame, connected_to, is_anchor, anchor_label, source "
        "FROM environment_entity WHERE env_id=%s AND world_id=%s"
    )
    rows = execute_query(sql, (env_id, world_id))
    return rows[0] if rows else None


def get_recent_traces_by_scene(session_id, scene, tick=None, limit=10, observer=None):
    """查某会话在指定场景发生的最近痕迹（下探：谁在哪个房间做过什么）。

    用于感知快照的"你注意到"段——NPC/玩家在某场景，看到的是该场景的历史痕迹，
    不是全世界的痕迹。tick 可选：传 tick 则只取 <= tick 的（按时间序而非真相序）。

    ★ 感知分层（09-08 问题3/第二步）：observer 传入时，按 visible_to 过滤——
       visible_to 为 NULL=全可见；为 dict 时按"谁能感知"裁剪：
         {"visible": [id...]}      # 白名单：仅这些 id 可见
         {"hide_from": [id...]}    # 黑名单：排除这些 id
         {"hide": true}            # 仅 actor 自己可见（隐藏行动）
       这实现"不同人看到不同版本"的认知局限；全可见（visible_to=NULL）保留旧行为。
    """
    if tick is None:
        sql = (
            "SELECT tick, actor, action_type, target, location, detail, visible_to "
            "FROM world_trace WHERE session_id=%s AND location=%s "
            "ORDER BY id DESC LIMIT %s"
        )
        rows = execute_query(sql, (session_id, scene, limit))
    else:
        sql = (
            "SELECT tick, actor, action_type, target, location, detail, visible_to "
            "FROM world_trace WHERE session_id=%s AND location=%s AND tick<=%s "
            "ORDER BY id DESC LIMIT %s"
        )
        rows = execute_query(sql, (session_id, scene, tick, limit))
    if observer is None:
        return [r[:-1] for r in rows]   # 去掉 visible_to 列，保持原返回契约（6 列）
    return [r[:-1] for r in rows if _trace_visible(r[6], r[1], observer)]


def _trace_visible(visible_to, actor, observer):
    """判断一条痕迹在 observer 视角下是否可见（感知分层/隐藏行动）。

    约定：
      None                     → 全可见
      {"visible": [ids]}       → 仅白名单内 ids 可见（actor 自己在白名单）
      {"hide_from": [ids]}     → 排除黑名单 ids（其余可见）
      {"hide": true}           → 仅 actor 自己可见（隐藏行动/暗处动作）
    其余未知结构 → 全可见（容错，不误伤）。
    """
    if visible_to is None:
        return True
    try:
        v = json.loads(visible_to) if isinstance(visible_to, str) else visible_to
    except (ValueError, TypeError):
        return True
    if not isinstance(v, dict):
        return True
    if v.get("hide") is True:
        return observer == actor
    whitelist = v.get("visible")
    if isinstance(whitelist, list):
        return observer == actor or observer in [str(x) for x in whitelist]
    blacklist = v.get("hide_from")
    if isinstance(blacklist, list):
        return observer not in [str(x) for x in blacklist] or observer == actor
    return True


def get_connected_scenes(env_id, world_id="golden"):
    """查某房间连接到哪些场景（connected_to，JSON 数组）。

    world_id：按 (world_id, env_id) 定位房间实体。用于"能不能从这到那"的可达性判断。
    Returns:
        list[str]：相邻场景 id 列表；无此实体或房间未设连通性返回空列表。
    """
    row = get_environment_entity(env_id, world_id)
    if not row or row[9] is None:
        return []
    try:
        return [str(x) for x in json.loads(row[9])]
    except (ValueError, TypeError):
        return []


def reset_environment_cards(world_id):
    """重置某世界全部环境卡到「出厂状态」（initial_state），轮回/模拟开始时调用。

    为什么需要（M1.5 泛化）：environment_card 是全局单例表（D-01），不按会话隔离。
    第一轮模拟男人拿刀 → knife.state 被改成 held（全局生效）；第二轮新模拟，男人
    拿刀前置要 in_place，但环境卡已是 held → 拿刀失败 blocked，计划卡死。
    初始状态需要保存，reset 才能恢复——所以 004 给环境卡加了 initial_state 列。
    本函数 = 通用版环境卡复位：任何世界都从自己的 initial_state 恢复，
    不再像旧 _reset_test_world_environment 那样在代码里硬编码各环境 id 和初始状态。
    initial_state 为 NULL 的卡跳过（旧数据/未设定出厂状态，不强行动）。
    Returns: 复位了多少张环境卡。

    轮回真义（09-07 修正）：轮回=回到出厂。玩家上轮拆出/造出的 runtime 物（如椅子腿）
    属于"那一轮的世界状态"，轮回必须清掉，否则下一轮还残留上轮拆的腿。故先删 runtime
    实体+配套卡，再恢复各卡 state=initial_state。
    """
    # 先清掉本轮玩家拆解/造出的 runtime 物（如椅子腿），保证轮回=干净出厂
    try:
        delete_runtime_entities(world_id)
    except Exception:
        pass  # 清理失败不阻断恢复，避免一次卡死整轮回
    sql = "UPDATE environment_card SET state=initial_state WHERE world_id=%s AND initial_state IS NOT NULL"
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (world_id,))
            affected = cursor.rowcount
        conn.commit()
        return affected
    finally:
        conn.close()


def delete_dynamic_lies() -> int:
    """删除全部「运行时动态谎言」秘密（secret_id 以 sec_lie_ 开头）。

    为什么（09-09 用户：谎称过记录应每次游戏重启重置）：secrets 表存两类——
    ① 角色卡的预设核心秘密（sec_alliance 等 seed.sql 灌入），是世界观常设，必须保留；
    ② NPC 在某一局里说的谎被注册的 sec_lie_*（mind_engine record_self_action 生成），
    属于"这一局运行时产生的状态"，随开局/轮回结束就应清空——否则上一局测试反复生成的
    "我谎称过"跨局永久累积（09-08 实测 test_man 积了 19 条，均为此类）。

    为什么只在显式新会话清（start_session）而不在 ensure_session：ensure_session 是
    半永久/adhoc（保住旧前端连续对话不打断），清谎会破坏正在进行的记忆；只有"新开一局"
    （/session/start）才代表轮回开始，此刻清动态谎语义正确。

    Returns: 删除了多少条动态谎秘密。
    """
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM secrets WHERE secret_id LIKE 'sec_lie_%'")
            affected = cursor.rowcount
        conn.commit()
        return affected
    finally:
        conn.close()


# =====================================================================
# M1.5 计划执行器的世界状态 DAO：NPC 位置 / 存活（game_state KV 承载）
# =====================================================================

def set_npc_pos(session_id, npc_id, env_id):
    """记录 NPC 当前所在位置（game_state['npc_pos:{npc_id}'] = env_id）。

    位置是"运行时世界状态"：M1.7 感知/移动可达性都要读它。
    为什么放 game_state 而不是 environment_card：位置属于"谁在哪"的动态事实，
    按会话隔离（一个世界同一时刻只能有一个"官方位置"），
    与环境卡的"房间本身是什么样"（静态+可变状态）职责分离。
    """
    upsert_game_state(session_id, f"npc_pos:{npc_id}", env_id)


def get_npc_pos(session_id, npc_id):
    """查 NPC 当前所在位置；未知返回 None。"""
    return get_game_state_map(session_id).get(f"npc_pos:{npc_id}")


def set_npc_status(session_id, npc_id, key, value):
    """写 NPC 状态位（game_state['npc_status:{npc_id}'] 是 dict，按 key 合并）。

    典型用法：女人死亡 = set_npc_status(sess, 'test_woman', 'dead', True)。
    key 的语义由调用方定（dead/alive/...），dict 合并保证一次只改一个维度。

    dead 布尔标准化：LLM（导演/反应仲裁）常把值写成字符串 "true"/"false"，而
    environment.execute_player_action 落 Python True/False —— 不统一会导致
    get_npc_status(...).get("dead") 有时是 "true"(str) 有时是 True(bool)，
    各处 bool()/== True 判断出现隐性错位。统一转成 bool。
    """
    if key == "dead":
        value = str(value).strip().lower() in ("true", "1", "yes", "是", "死亡", "died")
    state_key = f"npc_status:{npc_id}"
    raw = get_game_state_map(session_id).get(state_key)
    status = dict(raw) if isinstance(raw, dict) else {}
    status[key] = value
    upsert_game_state(session_id, state_key, status)


def get_npc_status(session_id, npc_id):
    """查 NPC 状态 dict（dead/alive/...）；无记录返回空 dict，视作"默认活着"。"""
    raw = get_game_state_map(session_id).get(f"npc_status:{npc_id}")
    return {**raw} if isinstance(raw, dict) else {}


def get_player_scene(session_id):
    """查玩家当前所在场景（game_state['scene']）；未开局返回 None。

    与 set_npc_pos 的位置语义对齐：位置属于"谁在哪"的动态事实，按会话隔离，
    与环境卡的"房间本身"（静态）职责分离。玩家场景存 game_state['scene']（见 sessions.INITIAL_STATE）。
    """
    return get_game_state_map(session_id).get("scene")


# =====================================================================
# M1.1 会话管理：game_state 读写 / 对话日志 / seed 关系复制
# =====================================================================

def upsert_game_state(session_id, state_key, state_value):
    """写入/更新某会话的一个状态键（KV 式，存在则覆盖）。

    ON DUPLICATE KEY UPDATE 依赖表的 UNIQUE(session_id, state_key)：
    一条 SQL 完成"有则更新无则插入"，避免"先查后写"两步的竞态窗口。
    state_value 任意可 JSON 序列化的值（int/str/dict/...）。
    """
    sql = (
        "INSERT INTO game_state (session_id, state_key, state_value) "
        "VALUES (%s, %s, %s) "
        "ON DUPLICATE KEY UPDATE state_value=VALUES(state_value)"
    )
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (session_id, state_key, json.dumps(state_value, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()


def get_game_state_map(session_id):
    """拉某会话的全部状态键，拼成 dict。

    约定：会话由 start_session 创建时必写初始 KV，因此空 dict 即"会话不存在"。
    state_value 列是 JSON 类型，逐键反序列化；解析失败的键原样返回（不炸整批）。
    """
    sql = "SELECT state_key, state_value FROM game_state WHERE session_id=%s"
    rows = execute_query(sql, (session_id,))
    out = {}
    for key, val in rows:
        try:
            out[key] = json.loads(val) if val is not None else None
        except (ValueError, TypeError):
            out[key] = val
    return out


def log_dialogue(session_id, npc_id, speaker, content):
    """追加一条对话日志（可观测 + 评测语料；M1.1 起接入 /chat）。

    speaker：player / npc / system。一问一答 = 两条行。
    此前该表只有结构没有写入方（可观测性欠债 D-04），本函数即偿还。
    """
    sql = (
        "INSERT INTO dialogue_log (session_id, npc_id, speaker, content) "
        "VALUES (%s, %s, %s, %s)"
    )
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (session_id, npc_id, speaker, content))
        conn.commit()
    finally:
        conn.close()


def clone_seed_relationships(new_session_id):
    """把 seed 初始关系复制成新会话的关系行（复制式隔离的"复制"步）。

    为什么复制而不是共享：relationships 是 UPDATE 累加表——各会话必须改
    自己的行，否则 A 会话刷的好感会串进 B 会话。复制后旧行保留，
    每轮关系数据留档，M1.13 统计回归才有"20 轮关系演化"的原料。
    last_interaction_at 故意不复制：新会话尚未发生互动。
    Returns: 复制的行数。
    """
    sql = (
        "INSERT INTO relationships "
        "(session_id, npc_id, other_id, relation_type, trust, fear, affection, tags, notes) "
        "SELECT %s, npc_id, other_id, relation_type, trust, fear, affection, tags, notes "
        "FROM relationships WHERE session_id='seed'"
    )
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (new_session_id,))
            affected = cursor.rowcount
        conn.commit()
        return affected
    finally:
        conn.close()


def execute_transaction(statements):
    """在单个事务里依次执行多条写语句（要么全成、要么全不落库）。

    Args:
        statements: list[(sql, params)]，按序执行；params 可为 None。
    Returns:
        list[int]：每条语句各自影响的行数。
    Raises:
        任何一条失败即 rollback 并向上抛——调用方看到的要么全成功、要么零变更。

    为什么需要它（M1.2）：plans.replace_plan 要求"旧行置 replaced + 新行插入"
    两条语句原子完成——半程失败会让 NPC 变成"无 active 计划裸奔"，且旧行
    已离开 active 态导致重试无法自愈。凡是"多行必须同时成立"的写操作
    （复制、状态迁移、版本替换），一律走这里，不连续调用单条写函数。
    """
    conn = get_connection()
    try:
        results = []
        with conn.cursor() as cursor:
            for sql, params in statements:
                cursor.execute(sql, params)
                results.append(cursor.rowcount)
        conn.commit()
        return results
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def clone_seed_plans(new_session_id):
    """把 seed 出厂计划复制成新会话的运行副本（M1.2，与 clone_seed_relationships 同构）。

    为什么复制而不是共享：运行副本的 status/current_step 会被引擎 UPDATE——
    各会话必须改自己的行，否则 A 会话推进到第 3 步会串进 B 会话（R8 状态串扰）。
    复制后各轮 plan 版本链完整留档，M1.13 统计回归才有"20 轮重规划路径分布"的原料。
    Returns: 复制的行数。
    """
    sql = (
        "INSERT INTO plans "
        "(session_id, npc_id, goal_id, goal, stickiness, status, current_step, version, steps, source) "
        "SELECT %s, npc_id, goal_id, goal, stickiness, status, current_step, version, steps, source "
        "FROM plans WHERE session_id='seed'"
    )
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (new_session_id,))
            affected = cursor.rowcount
        conn.commit()
        return affected
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 心智引擎（v0.2 重构，006 迁移）：mental_model 冷态 + npc_mental_state 热态
# 铁律：这些函数返回的数值只进 code（mental.py 纯函数），LLM 只见状态词。
# ---------------------------------------------------------------------------

def get_secrets(npc_id):
    """读某 NPC 的全部秘密（v4§13 双链：主动链触发 + 被动链触发 + 识破反应策略）。

    Returns:
        list[dict]：[{secret_id, topic, reveal_level, active_triggers,
                      passive_triggers, detected_response}]；无秘密返回 []。
        旧 isabella seed 的阈值是 0~1 尺度，归一化交给 mental.py（归一责任在纯函数层）。
    """
    rows = execute_query(
        "SELECT secret_id, topic, reveal_level, active_triggers, passive_triggers, "
        "detected_response FROM secrets WHERE npc_id=%s", (npc_id,))
    out = []
    for r in rows:
        def _load(v):
            if v is None:
                return []
            try:
                data = json.loads(v) if isinstance(v, str) else v
                return data if isinstance(data, list) else []
            except (ValueError, TypeError):
                return []
        out.append({
            "secret_id": r[0], "topic": r[1], "reveal_level": r[2],
            "active_triggers": _load(r[3]), "passive_triggers": _load(r[4]),
            "detected_response": _load(r[5]),
        })
    return out


def save_secret_reveal_level(npc_id, secret_id, level):
    """回写秘密的泄露等级（主动链升级后的落库，供统计/回归审计）。"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE secrets SET reveal_level=%s WHERE npc_id=%s AND secret_id=%s",
                (level, npc_id, secret_id))
            affected = cursor.rowcount
        conn.commit()
        return affected
    finally:
        conn.close()


def add_secret(npc_id, secret_id, topic, reveal_level="guard", detected_response=None):
    """注册一条新秘密（运行时动态产生，如"我说过的谎"——秘密随游戏演化）。

    ON DUPLICATE 只更新话题文本，**不覆盖已演化的 reveal_level**——
    同 id 重复注册（同一句谎被再说）不应回退已升级的口径。
    """
    resp_json = json.dumps(detected_response or ["deny", "confess"], ensure_ascii=False)
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO secrets (npc_id, secret_id, topic, reveal_level, detected_response) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE topic=VALUES(topic)",
                (npc_id, secret_id, topic, reveal_level, resp_json))
            affected = cursor.rowcount
        conn.commit()
        return affected
    finally:
        conn.close()


def get_initial_scene(npc_id):
    """读角色卡出生地（008 迁移新列；NULL=未设定）。"""
    rows = execute_query(
        "SELECT initial_scene FROM character_card WHERE npc_id=%s AND is_active=1", (npc_id,))
    return (rows[0][0] or "") if rows else ""


def get_mental_model(npc_id):
    """读某 NPC 的 mental_model（心智参数唯一入口，006 迁移新列）。

    与 get_character_card 的关系：**故意独立成函数而不扩旧 SELECT**——
    旧函数按列号索引被 context_builder/agent 多处耦合（card[1]~card[7]），
    中间插列会引发静默错位（重构文档 §10.3 决策：第 4 期改造 agent 时统一解耦）。

    Returns:
        dict：mental_model JSON 解析结果（kernel/perception/emotion/planning/expression/depth）；
        None：未灌值的角色（旧角色/未迁移），调用方据此走默认参数兜底。
    """
    rows = execute_query(
        "SELECT mental_model FROM character_card WHERE npc_id=%s AND is_active=1",
        (npc_id,),
    )
    if not rows or not rows[0][0]:
        return None
    try:
        return json.loads(rows[0][0]) if isinstance(rows[0][0], str) else rows[0][0]
    except (ValueError, TypeError):
        return None  # 脏数据按缺失处理，不崩主流程


def get_mental_state(session_id, npc_id):
    """读某会话里某 NPC 的运行时心智热态（006 迁移新表 npc_mental_state）。

    与 mental_model 的分工：那边是"这个人生来什么样"（冷/静态，全会话共享），
    这边是"这个人此刻心里怎么样"（热/动态，按会话隔离——A 会话的恐惧不串进 B 会话）。

    Returns:
        dict：{"emotion":{valence,arousal,dominance}, "emotion_word", "emotion_intensity",
               "beliefs":[...], "noticed":[...], "working_memory":[...],
               "last_observation":{...}, "updated_tick"}；
        None：本会话还没写过热态（调用方应以"开局平静"初始化，见 save_mental_state）。
    """
    rows = execute_query(
        "SELECT emotion, emotion_word, emotion_intensity, beliefs, noticed, "
        "working_memory, last_observation, updated_tick "
        "FROM npc_mental_state WHERE session_id=%s AND npc_id=%s",
        (session_id, npc_id),
    )
    if not rows:
        return None

    def _load(v):
        if v is None:
            return None
        try:
            return json.loads(v) if isinstance(v, str) else v
        except (ValueError, TypeError):
            return None

    emotion = _load(rows[0][0]) or {}
    return {
        "emotion": emotion,
        "emotion_word": rows[0][1] or "",
        "emotion_intensity": float(rows[0][2]) if rows[0][2] is not None else None,
        "beliefs": _load(rows[0][3]) or [],
        "noticed": _load(rows[0][4]) or [],
        "working_memory": _load(rows[0][5]) or [],
        "last_observation": _load(rows[0][6]),
        "updated_tick": rows[0][7] or 0,
    }


def save_mental_state(session_id, npc_id, patch, tick=0):
    """按"读-改-写 patch"语义保存心智热态（没有行则整行新建）。

    Args:
        patch: dict，只含要更新的键，合法键：
            emotion / emotion_word / emotion_intensity / beliefs / noticed /
            working_memory / last_observation。非法键直接忽略（防脏数据入库）。
            emotion/beliefs/noticed/working_memory/last_observation 序列化为 JSON 落库。
        tick: 当前游戏 tick（写 updated_tick，供回放审计"这个情绪是几号 tick 产生的"）。

    为什么整行覆盖而不是逐列 UPDATE：热态是"单写者"结构——每 tick 由
    心智循环对同一 NPC 读写一次（§3.2 全局统一状态），没有并发竞争，
    INSERT ... ON DUPLICATE KEY UPDATE 整行覆盖比拼 SQL 片段更简单、
    也天然防"部分字段漏写"。

    Returns: 写入的合法键列表（供验证脚本断言）。
    """
    allowed_json = ("emotion", "beliefs", "noticed", "working_memory", "last_observation")
    allowed_plain = ("emotion_word", "emotion_intensity")
    cols, vals = [], []
    for key, value in patch.items():
        if key in allowed_json:
            cols.append(key)
            vals.append(json.dumps(value, ensure_ascii=False))
        elif key in allowed_plain:
            cols.append(key)
            vals.append(value)
    if not cols:
        return []  # 空 patch（或全是非法键）：不产生空行

    # INSERT ... ON DUPLICATE KEY UPDATE：新会话首写=插入，后续=整行覆盖。
    all_cols = cols + ["updated_tick"]
    all_vals = vals + [tick]
    update_clause = ", ".join(f"{c}=VALUES({c})" for c in all_cols)
    sql = (
        f"INSERT INTO npc_mental_state (session_id, npc_id, {', '.join(all_cols)}) "
        f"VALUES (%s, %s, {', '.join(['%s'] * len(all_vals))}) "
        f"ON DUPLICATE KEY UPDATE {update_clause}"
    )
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (session_id, npc_id, *all_vals))
        conn.commit()
    finally:
        conn.close()
    return cols


if __name__ == "__main__":
    # 演示：查王子最重视的记忆 + 他最信任谁
    print("王子最重要的记忆（importance 降序）：")
    for row in get_memories_by_importance("prince_adrian"):
        print(f"  [{row[3]}分] ({row[1]}) {row[2]}")

    print("\n王子信任的人（trust 降序）：")
    for row in get_relations_by_trust("prince_adrian"):
        print(f"  trust={row[2]} -> {row[0]} ({row[1]})")

    # 2.3 新增：聚合/分组/HAVING/分页演示
    print("\n各 NPC 平均 OOC 评分（AVG < 0.8 视为人设不稳，GROUP BY + HAVING）：")
    for row in get_avg_ooc_by_npc(min_avg=0.8):
        print(f"  {row[0]}: 样本={row[1]} 平均={float(row[2]):.2f}")

    print("\n世界观知识库按权限统计（GROUP BY + COUNT + 别名排序）：")
    for row in count_knowledge_by_access():
        print(f"  {row[0]}: {row[1]} 条")

    print("\n对话日志分页（第1页，每页3条，LIMIT+OFFSET）：")
    for row in get_dialogue_page(1, 3):
        print(f"  {row[0]} [{row[1]}] ooc={row[3]}：{row[2]}")

    # 2.4 新增：自关联 + 相关子查询演示
    print("\n王子的记忆链（自关联：每条记忆 + 它的父记忆）：")
    for row in get_memory_chain("prince_adrian"):
        parent = row[3] if row[3] else "（无父记忆，是根基记忆）"
        print(f"  id={row[0]} [{row[1]}] {row[2]}  <- 父记忆: {parent}")

    print("\n王子的评分摘要（相关子查询取历史最高/最低）：")
    for row in get_npc_score_summary("prince_adrian"):
        print(f"  {row[0]}: 平均={float(row[1]):.2f} 最高={float(row[2]):.2f} 最低={float(row[3]):.2f}")


# ---------------------------------------------------------------------------
# 世界时序 v2 / T6：目标查询、痕迹增量、封锁计划（重规划器数据源）
# ---------------------------------------------------------------------------

def get_goals(npc_id):
    """读某 NPC 的目标（Desire 层，goals 表）。

    Returns:
        list[dict]：[{goal_id, priority, type, plan, note}]，按优先级降序；无则 []。
        供重规划器"有 Desire 无 Intention"的从 0 起规划使用。
    """
    rows = execute_query(
        "SELECT goal_id, priority, type, plan, note FROM goals "
        "WHERE npc_id=%s ORDER BY priority DESC", (npc_id,))
    out = []
    for r in rows:
        plan_val = r[3]
        try:
            plan_val = json.loads(plan_val) if isinstance(plan_val, str) else plan_val
        except (ValueError, TypeError):
            pass
        out.append({"goal_id": r[0], "priority": r[1], "type": r[2],
                    "plan": plan_val, "note": r[4]})
    return out




def append_player_intent(session_id, entry):
    """玩家意图入池（场景导演的玩家一侧——改变环境的行动登记制）。"""
    key = f"pending_intents:{session_id}"
    m = get_game_state_map(session_id)
    pool = m.get(key)
    pool = pool if isinstance(pool, list) else []
    pool.append(entry)
    upsert_game_state(session_id, key, pool)
    return len(pool)


def take_player_intents(session_id):
    """排干玩家意图池（tick 结算调用；排出后清空）。"""
    key = f"pending_intents:{session_id}"
    m = get_game_state_map(session_id)
    pool = m.get(key)
    pool = pool if isinstance(pool, list) else []
    if pool:
        upsert_game_state(session_id, key, [])
    return pool


# --- 即时提示池（09-10 用户拍板：让"后台在忙什么"能被玩家看见） ------------------
# 为什么需要它：解析层"再解析一次"发生在 /chat 这个【同步阻塞】请求内部——请求没返回，
# 前端就拿不到中途进度；而这一格的世界结算可能还要等几十秒。
# 解法：后端把提示先入池，前端在忙等期间轮询 /session/notices 取走并显示，
# 玩家就知道"系统在重试、不是卡死了"。复用 player_intents 的 game_state 列表池范式。


def push_notice(session_id, text, kind="info"):
    """推送一条即时提示（入池，等前端来取）。

    Args:
        text: 给玩家看的文案（不要出现"解析/校验/op"等技术名词）。
        kind: 提示类型（info/warn），供前端决定样式。
    Returns: 入池后的条数。
    """
    key = f"pending_notices:{session_id}"
    m = get_game_state_map(session_id)
    pool = m.get(key)
    pool = pool if isinstance(pool, list) else []
    pool.append({"text": str(text), "kind": str(kind or "info"),
                 "tick": int(m.get("current_tick", 0) or 0)})
    upsert_game_state(session_id, key, pool)
    return len(pool)


def take_notices(session_id):
    """排干即时提示池（前端轮询时调用；取走即清空，避免同一条重复显示）。"""
    key = f"pending_notices:{session_id}"
    m = get_game_state_map(session_id)
    pool = m.get(key)
    pool = pool if isinstance(pool, list) else []
    if pool:
        upsert_game_state(session_id, key, [])
    return pool


# --- 对话邀请池 + 对话会话状态（对话系统 v0.4） -----------------------------------
# 复用玩家意图池的"game_state 列表池"范式：邀请是"发起者想跟谁说话"的登记，仲裁阶段一次性排干；
# 会话状态是"已同意、正在进行"的对话挂起态，期间 world.advance_one 被 active_conv 挂起（不推进 tick）。


def append_conversation_invite(session_id, entry):
    """对话邀请入池（仲裁层输入侧——发起者想跟谁说话、第一句是什么）。

    entry 形如 {"initiator","target","scene","tick","topic","first_line"}。
    Returns: 入池后的数量。
    """
    key = f"pending_conv_invites:{session_id}"
    m = get_game_state_map(session_id)
    pool = m.get(key)
    pool = pool if isinstance(pool, list) else []
    pool.append(entry)
    upsert_game_state(session_id, key, pool)
    return len(pool)


def take_conversation_invites(session_id):
    """排干对话邀请池（tick 结算的仲裁阶段调用；排出后清空，只处理本 tick 的新邀请）。"""
    key = f"pending_conv_invites:{session_id}"
    m = get_game_state_map(session_id)
    pool = m.get(key)
    pool = pool if isinstance(pool, list) else []
    if pool:
        upsert_game_state(session_id, key, [])
    return pool


def start_conversation(session_id, initiator, target, scene, tick,
                       first_line, topic="", max_rounds=5):
    """建立对话会话（双方同意后、进入逐句流式前的初始态）。

    会话占据本 tick 的行动位；期间 world.advance_one 被挂起（不额外推进 tick）。

    09-09：建新会话前清掉上一场对话遗留的 conv_end 旁白缓存（避免新对话读到旧文案串场）。
    在 db 层统一清理，保证 /conversation/start 与 /conversation/accept 两个入口都不漏。
    Returns: 会话 dict（即写入 game_state 的 active_conv:<session_id>）。
    """
    clear_conv_end_text(session_id)   # 防止读到上一场对话的旧转场旁白（串场保护）
    conv = {
        "initiator": initiator,
        "target": target,
        "scene": scene,
        "start_tick": tick,
        "round": 0,
        "max_rounds": max_rounds,
        "topic": topic,
        "first_line": first_line,
        "history": [],
        "status": "active",
    }
    upsert_game_state(session_id, f"active_conv:{session_id}", conv)
    return conv


def get_conversation(session_id):
    """取当前对话会话（无则返回 None）。"""
    return get_game_state_map(session_id).get(f"active_conv:{session_id}")


def update_conversation(session_id, conv):
    """整体写回对话会话（round/history/status 每次发言后更新）。"""
    upsert_game_state(session_id, f"active_conv:{session_id}", conv)


def end_conversation(session_id):
    """结案对话会话（主动结束或达到 max_rounds 后清空，让 advance_one 恢复推进）。"""
    upsert_game_state(session_id, f"active_conv:{session_id}", None)


def set_conv_end_text(session_id, text):
    """落库"结束对话回场景"的专属转场旁白（进程B在发起对话那一格结算完成后预生成）。

    在会话结束人回来之前就写好——素材（该格场景快照 + 对话对象）在那格结算完成时已齐备，
    对话结束回场景直接读缓存秒出，不必再等 LLM 现调。覆盖写（新对话覆盖旧）。"""
    upsert_game_state(session_id, f"conv_end_text:{session_id}", str(text or ""))


def get_conv_end_text(session_id):
    """读已预生成的"结束对话回场景"转场旁白；无则返回空串（前端据此兜底现调 /scene/inspect）。"""
    return str(get_game_state_map(session_id).get(f"conv_end_text:{session_id}") or "")


def clear_conv_end_text(session_id):
    """清空该会话的 conv_end 旁白缓存（发起新对话前清理，避免串场）。"""
    upsert_game_state(session_id, f"conv_end_text:{session_id}", None)


def get_traces_since(session_id, since_tick, limit=100):
    """读某会话 since_tick 之后（不含）的世界痕迹——玩家挂机回来时的"世界变化提示"数据源。"""
    return execute_query(
        "SELECT tick, actor, action_type, target, location, detail FROM world_trace "
        "WHERE session_id=%s AND tick>%s ORDER BY tick ASC LIMIT %s",
        (session_id, since_tick, limit))

def get_scene_traces_since(session_id, scene, observer, since_tick, limit=100, until_tick=None):
    """读某会话 since_tick 之后、发生在【指定场景】、且 observer 可感知的世界痕迹。

    与 get_traces_since（全场景、不感知）不同：给玩家"世界变化提示"用——玩家只应
    知道自己所在场景里发生、且他能感知的事（认知局限）。两重过滤：
    ① location=scene（只留本场景）；② _trace_visible（visible_to 感知分层：
    NULL=全可见 / 白名单 / 黑名单 / 隐藏动作，observer 视角）——"不同人看到不同版本"，
    与 get_recent_traces_by_scene 同一套约定。
    until_tick（09-10 方向2 闭环）：可选上界，仅取 since_tick < tick <= until_tick ——
    结束对话回场景只拉【发起对话那一格】的增量（治 A/B/C 多 tick 堆叠）。None = 不限上界。
    """
    if until_tick is not None:
        rows = execute_query(
            "SELECT tick, actor, action_type, target, location, detail, visible_to "
            "FROM world_trace WHERE session_id=%s AND tick>%s AND tick<=%s AND location=%s "
            "ORDER BY tick ASC LIMIT %s",
            (session_id, since_tick, until_tick, scene, limit))
    else:
        rows = execute_query(
            "SELECT tick, actor, action_type, target, location, detail, visible_to "
            "FROM world_trace WHERE session_id=%s AND tick>%s AND location=%s "
            "ORDER BY tick ASC LIMIT %s",
            (session_id, since_tick, scene, limit))
    return [r[:-1] for r in rows if _trace_visible(r[6], r[1], observer)]
