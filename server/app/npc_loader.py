"""NPC 模组加载器：让「一个 NPC = 一个定义文件」自动注册进 AI 角色数据库。

架构定位（用户第1点·NPC 模组化）：
- 现状：NPC 语义散在 SQL seed（character_card/plans/secrets/relationships/npc_memory/...），
  加一个 NPC 要改 SQL、改 seed、还要前端另维护一份展示 json。
- 目标：新增一个 NPC = 在 `server/worlds/<world_id>/npcs/<id>.json` 放一个定义文件即可，
  不改任何代码、不手写 SQL。本模块扫描该目录，把每个文件幂等地 upsert 进 DB
  （character_card 主卡 + goals/plans/relationships/secrets/npc_memory/world_knowledge 若干从表）。

与 world_pack 的分工：
- world_pack 管「随模组变化的风格配置」（说书人/词表/提示词），纯文件、进内存缓存；
- 本模块管「随模组变化的 NPC 实体」，喂进 DB（AI 决策可读），同样按 world_id 隔离目录。

幂等设计：
- character_card 表无 UNIQUE(world_id,npc_id) 约束 → 先查存在再 INSERT/UPDATE；
- 有 UNIQUE 键的从表（goals/plans/relationships/secrets）用 ON DUPLICATE KEY UPDATE；
- npc_memory / world_knowledge 无业务唯一键 → 先删后插（seed 语义：定义文件是权威）。

用法：
    from app import npc_loader
    npc_loader.load_world("test")      # 注册/刷新 test 世界全部 NPC 定义文件
    npc_loader.load_all()              # 注册/刷新所有世界
    npc_loader.load_one("test", "test_robot")
"""
from __future__ import annotations

import json
from pathlib import Path

from . import db

_WORLDS_ROOT = Path(__file__).resolve().parents[1] / "worlds"
_NPC_DIR = "npcs"

# character_card 的业务唯一键（DB 无约束，用查询判重）
_CHAR_CARD_COLS = (
    "npc_id", "world_id", "name", "title", "personality", "background", "motivation",
    "forbidden", "knowledge_scope", "initial_scene", "appearance", "outfits",
    "personality_traits", "cognitive", "speech_style", "example_dialogue",
    "thinking_chain", "mental_model", "cooperation_profile", "is_active",
)


# --------------------------------------------------------------------------- #
# 文件扫描
# --------------------------------------------------------------------------- #
def _json_str(v, default=""):
    """把任意值规整成 JSON 字符串（字典/列表 -> json.dumps；None -> default）。"""
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


def _load_npc_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # noqa: BLE001
        db_log = f"npc_loader: 无法解析 {path}: {exc}"
        print(db_log)
        return {}
    return data if isinstance(data, dict) else {}


def _npc_dir(world_id: str) -> Path:
    return _WORLDS_ROOT / str(world_id or "").strip() / _NPC_DIR


def _iter_npc_files(world_id: str):
    """返回 (npc_id, dict) 列表。npc_id 优先用文件内 id，否则用文件名。"""
    root = _npc_dir(world_id)
    if not root.is_dir():
        return []
    out = []
    for f in sorted(root.glob("*.json")):
        data = _load_npc_file(f)
        if not data:
            continue
        npc_id = str(data.get("id") or f.stem).strip()
        if npc_id:
            out.append((npc_id, data))
    return out


# --------------------------------------------------------------------------- #
# 各表 upsert
# --------------------------------------------------------------------------- #
def _upsert_character_card(npc_id: str, world_id: str, npc: dict):
    card = {
        "npc_id": npc_id,
        "world_id": world_id,
        "name": str(npc.get("name") or npc_id),
        "title": str(npc.get("title") or ""),
        "personality": _json_str(npc.get("personality")),
        "background": _json_str(npc.get("background")),
        "motivation": _json_str(npc.get("motivation")),
        "forbidden": _json_str(npc.get("forbidden"), default="[]"),
        "knowledge_scope": _json_str(npc.get("knowledge_scope"), default="{}"),
        "initial_scene": _json_str(npc.get("initial_scene")),
        "appearance": _json_str(npc.get("appearance"), default="{}"),
        "outfits": _json_str(npc.get("outfits"), default="[]"),
        "personality_traits": _json_str(npc.get("personality_traits"), default="{}"),
        "cognitive": _json_str(npc.get("cognitive"), default="{}"),
        "speech_style": _json_str(npc.get("speech_style")),
        "example_dialogue": _json_str(npc.get("example_dialogue"), default="[]"),
        "thinking_chain": _json_str(npc.get("thinking_chain"), default="[]"),
        "mental_model": _json_str(npc.get("mental_model"), default="{}"),
        "cooperation_profile": _json_str(npc.get("cooperation_profile"), default="{}"),
        "is_active": 1 if npc.get("is_active", True) else 0,
    }
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        # character_card 无 UNIQUE(world_id,npc_id) 约束，先判重再 INSERT/UPDATE（幂等）
        cur.execute("SELECT id FROM character_card WHERE world_id=%s AND npc_id=%s",
                    (world_id, npc_id))
        row = cur.fetchone()
        if row:
            sets = ", ".join(f"`{c}`=%s" for c in _CHAR_CARD_COLS if c in card)
            params = tuple(card[c] for c in _CHAR_CARD_COLS if c in card) + (row[0],)
            cur.execute(f"UPDATE character_card SET {sets} WHERE id=%s", params)
        else:
            cols = ", ".join(f"`{c}`" for c in _CHAR_CARD_COLS if c in card)
            marks = ", ".join(["%s"] * sum(1 for c in _CHAR_CARD_COLS if c in card))
            params = tuple(card[c] for c in _CHAR_CARD_COLS if c in card)
            cur.execute(f"INSERT INTO character_card ({cols}) VALUES ({marks})", params)
        conn.commit()
    finally:
        conn.close()


def _upsert_goals(npc_id: str, goals, db_conn):
    if not goals:
        return
    for g in goals:
        db_conn.cursor().execute(
            "INSERT INTO goals (npc_id, goal_id, priority, type, plan, note) VALUES (%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE priority=VALUES(priority), type=VALUES(type), plan=VALUES(plan), note=VALUES(note)",
            (npc_id, str(g.get("goal_id") or ""), int(g.get("priority", 50)),
             str(g.get("type") or "short"), _json_str(g.get("plan"), default="[]"),
             _json_str(g.get("note"))),
        )


def _upsert_plans(session_id, npc_id, goal_id, plan, db_conn):
    """plans 唯一键 (session_id,npc_id,goal_id,version)；version 固定 1（定义文件是模板）。"""
    if not plan:
        return
    steps = plan.get("steps") or []
    db_conn.cursor().execute(
        "INSERT INTO plans (session_id, npc_id, goal_id, goal, stickiness, status, current_step, version, steps, source) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE goal=VALUES(goal), stickiness=VALUES(stickiness), "
        "status=VALUES(status), current_step=VALUES(current_step), steps=VALUES(steps), source=VALUES(source)",
        (session_id, npc_id, str(goal_id or ""), str(plan.get("goal") or ""),
         str(plan.get("stickiness") or "normal"), str(plan.get("status") or "active"),
         int(plan.get("current_step", 1)), 1,
         _json_str(steps, default="[]"), str(plan.get("source") or "handwritten")),
    )


def _upsert_relationships(session_id, npc_id, rels, db_conn):
    if not rels:
        return
    for r in rels:
        db_conn.cursor().execute(
            "INSERT INTO relationships (session_id, npc_id, other_id, relation_type, trust, fear, affection, notes) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE relation_type=VALUES(relation_type), trust=VALUES(trust), "
            "fear=VALUES(fear), affection=VALUES(affection), notes=VALUES(notes)",
            (session_id, npc_id, str(r.get("other_id") or "player"),
             str(r.get("relation_type") or "neutral"),
             int(r.get("trust", 0)), int(r.get("fear", 0)),
             int(r.get("affection", 0)), _json_str(r.get("notes"))),
        )


def _upsert_secrets(npc_id: str, secrets, db_conn):
    if not secrets:
        return
    for s in secrets:
        db_conn.cursor().execute(
            "INSERT INTO secrets (npc_id, secret_id, topic, reveal_level, related_belief, active_triggers, passive_triggers, detected_response) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE topic=VALUES(topic), reveal_level=VALUES(reveal_level), "
            "related_belief=VALUES(related_belief), active_triggers=VALUES(active_triggers), "
            "passive_triggers=VALUES(passive_triggers), detected_response=VALUES(detected_response)",
            (npc_id, str(s.get("secret_id") or ""), str(s.get("topic") or ""),
             str(s.get("reveal_level") or "guard"),
             _json_str(s.get("related_belief")), _json_str(s.get("active_triggers"), default="[]"),
             _json_str(s.get("passive_triggers"), default="[]"),
             _json_str(s.get("detected_response"), default="[]")),
        )


def _replace_memories(session_id, npc_id, memories, db_conn):
    """npc_memory 无业务唯一键 → 删该会话该 NPC 的旧记忆再插（定义文件是权威）。"""
    db_conn.cursor().execute("DELETE FROM npc_memory WHERE session_id=%s AND npc_id=%s",
                             (session_id, npc_id))
    if not memories:
        return
    for m in memories:
        db_conn.cursor().execute(
            "INSERT INTO npc_memory (npc_id, session_id, memory_type, content, importance, summary) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (npc_id, session_id, str(m.get("memory_type") or "impression"),
             str(m.get("content") or ""), int(m.get("importance", 0)),
             str(m.get("summary") or "")[:255]),
        )


def _replace_knowledge(world_id, npc_id, items, db_conn):
    """world_knowledge 无业务唯一键 → 删该世界该 NPC 的私有先验再插（npc 私有 + global）。"""
    db_conn.cursor().execute("DELETE FROM world_knowledge WHERE world_id=%s AND npc_id=%s",
                             (world_id, npc_id))
    if not items:
        return
    for k in items:
        db_conn.cursor().execute(
            "INSERT INTO world_knowledge (world_id, npc_id, category, title, content, tags, access_level, spoiler_level) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (world_id, npc_id, str(k.get("category") or "rule"), str(k.get("title") or ""),
             str(k.get("content") or ""), str(k.get("tags") or ""),
             str(k.get("access_level") or "public"), int(k.get("spoiler_level", 0))),
        )


def _load_one_into_db(world_id: str, npc_id: str, npc: dict):
    """把单个 NPC 定义文件 upsert 进 DB。返回 str（正常返回 None，异常返回错误信息）。"""
    try:
        _upsert_character_card(npc_id, world_id, npc)
        conn = db.get_connection()
        try:
            _upsert_goals(npc_id, npc.get("goals"), conn)      # noqa: F821  defined below
            _upsert_plans("seed", npc_id, (npc.get("plans") or {}).get("goal_id", ""),
                          npc.get("plans"), conn)
            _upsert_relationships("seed", npc_id, npc.get("relationships"), conn)
            _upsert_secrets(npc_id, npc.get("secrets"), conn)
            _replace_memories("seed", npc_id, npc.get("memories"), conn)
            _replace_knowledge(world_id, npc_id, npc.get("knowledge"), conn)
            conn.commit()
        finally:
            conn.close()
        return None
    except Exception as exc:  # noqa: BLE001
        return f"npc_loader: 注册 {world_id}/{npc_id} 失败: {exc}"


def load_world(world_id: str):
    """注册/刷新某世界全部 NPC 定义文件。返回 (成功数, [错误列表])。"""
    files = _iter_npc_files(world_id)
    ok, errs = 0, []
    for npc_id, data in files:
        err = _load_one_into_db(world_id, npc_id, data)
        if err:
            errs.append(err)
        else:
            ok += 1
    return ok, errs


def load_one(world_id: str, npc_id: str):
    """注册/刷新单个 NPC 定义文件。返回错误信息或 None。"""
    for nid, data in _iter_npc_files(world_id):
        if nid == npc_id:
            return _load_one_into_db(world_id, nid, data)
    return f"npc_loader: {world_id}/{npc_id} 没有定义文件"


def load_all():
    """注册/刷新所有世界。返回 (成功数, 错误列表, 世界列表)。"""
    ok_all, errs_all, worlds = 0, [], []
    for root in sorted(_WORLDS_ROOT.iterdir()):
        if not root.is_dir():
            continue
        wid = root.name
        if not _npc_dir(wid).is_dir():
            continue
        worlds.append(wid)
        ok, errs = load_world(wid)
        ok_all += ok
        errs_all.extend(errs)
    return ok_all, errs_all, worlds


def read_world_npcs(world_id: str) -> dict:
    """读某世界全部 NPC 定义文件（id -> dict），供 /world/npcs 端点回传前端展示卡。

    展示字段（name/status/opening/replies/option_rules）不落 DB、文件即权威；
    与 DB 里的 AI 卡（character_card 等）同源同一份文件——「一个 NPC = 一个定义文件」。
    """
    return {nid: data for nid, data in _iter_npc_files(world_id)}


def npc_file_schema() -> dict:
    """返回统一 NPC 定义文件的 schema 说明（供文档/前端参考）。"""
    return {
        "id": "NPC id（缺省=文件名）",
        "name": "显示名",
        "title": "头衔/身份",
        "personality": "性格特点",
        "background": "背景故事",
        "motivation": "核心动机",
        "forbidden": ["禁区：不能说破的秘密"],
        "knowledge_scope": {"knows": [], "does_not_know": [], "can_access": ["public"]},
        "initial_scene": "出生地场景 id",
        "appearance": {"age": "", "tells": []},
        "outfits": [{"id": "", "name": "", "occasion": "", "description": "", "state": "", "default": True}],
        "personality_traits": {},
        "cognitive": {"suspicion": 50, "perception": 50, "composure": 50},
        "speech_style": "",
        "example_dialogue": [],
        "thinking_chain": [],
        "mental_model": {"kernel": {}, "perception": {}, "emotion": {}, "planning": {}, "expression": {}},
        "cooperation_profile": {"obedience": 50, "ladder_ceiling": 2, "hard_limits": [], "unlock_conditions": []},
        "goals": [{"goal_id": "", "priority": 50, "type": "short", "plan": [], "note": ""}],
        "plans": {"goal_id": "", "goal": "", "stickiness": "normal", "status": "active",
                  "current_step": 1, "steps": [], "source": "handwritten"},
        "secrets": [{"secret_id": "", "topic": "", "reveal_level": "guard", "related_belief": "",
                     "active_triggers": [], "passive_triggers": [], "detected_response": []}],
        "relationships": [{"other_id": "player", "relation_type": "neutral",
                           "trust": 0, "fear": 0, "affection": 0, "notes": ""}],
        "memories": [{"memory_type": "impression", "content": "", "importance": 0, "summary": ""}],
        "knowledge": [{"category": "rule", "title": "", "content": "", "tags": "",
                       "access_level": "public", "spoiler_level": 0}],
        # ---- 前端展示字段（供 /world/npcs 端点回传，客户端渲染用） ----
        "status": "",
        "opening": "",
        "replies": [],
        "option_rules": [],
    }
