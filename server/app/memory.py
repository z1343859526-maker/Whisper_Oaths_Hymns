"""记忆系统：写入 + 按 importance 阈值召回（生成器批处理 + summary 优先）。

四存储职责分工（P0-b，防重复写入/定位清晰）：
- **记忆表（本模块 npc_memory）= 长期情节**：write_memory(npc_id, memory_type=event/impression/relation,
  content, importance, summary, related_entity, session_id)。导演分析的认知/印象 → "event/impression"
  且必须给 importance/related_entity。
- **working_memory = 工作记忆（热态）**：由 mind_engine 管（npc_mental_state.working_memory），
  "此刻正在想的事"，会衰减/替换，不持久。
- **world_trace = 客观流水**：db.add_world_trace，只增不删的全量历史，供"最近做过什么"。
- **beliefs = 信念**：npc_mental_state.beliefs，对世界的持久判断（信任/信心调节）。

认知类数据写入规范：**只进记忆表**（含 importance/related_entity），绝不再重复写
working_memory 或另起一套痕迹，避免"同一认知多份拷贝、口径漂移"。

设计要点：
- 写入（write_memory）：INSERT 一条记忆，插入后显式 commit；
- 召回（recall_memories）：用**生成器(yield)** 逐条产出记忆，而不是一次性把全部行装进列表，
  做到"惰性求值"——调用方拿到想要的可随时中断，不必加载整批，降低峰值内存。
  这是技术清单「生成器+yield」在"记忆批处理"的落位（呼应 0.5 流式回复的 chat_stream）。
- import 阈值：只召回 importance >= min_importance 的记忆（防低价值记忆刷屏）。
- summary 优先：有摘要先给摘要，没摘要才给全文，省 token（贴合 P4 提示词预算）。
"""
from . import db


def write_memory(npc_id, memory_type, content, importance=5, summary="", related_entity="", session_id=""):
    """写入一条 NPC 记忆。

    Args:
        npc_id: 记忆归属的 NPC。
        memory_type: event/impression/relation。
        content: 记忆原文。
        importance: 重要性 0~10，越高越优先召回。
        summary: 一句话摘要（有摘要则召回时优先给摘要）。
        related_entity: 关联实体 id（人/地/物）。
        session_id: 归属会话（M1.1 隔离维度）。运行时产生的记忆传当前会话 id；
            种子先验记忆用 'seed'。空串仅限遗留调用，新代码禁止（召回侧按运行时处理）。
    """
    sql = (
        "INSERT INTO npc_memory "
        "(npc_id, session_id, memory_type, content, importance, summary, related_entity) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)"
    )
    conn = db.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (npc_id, session_id, memory_type, content, importance, summary, related_entity))
        conn.commit()  # INSERT 必须显式提交，否则关闭连接时回滚
    finally:
        conn.close()


def recall_memories(npc_id, min_importance=4, session_id="seed"):
    """召回某 NPC 的高重要度记忆，**生成器**逐条 yield。

    会话语义（M1.1）：可见 = 先验('seed') + 指定会话两段。
    为什么是双段可见：先验记忆是 NPC 出生自带的（夫人知道钥匙在花盆），
    轮回后必须仍在；运行时记忆是本轮发生的，轮回/别的会话不可见。
    session_id 缺省 'seed'：无会话上下文的调用（演示脚本）只见先验，安全降级。

    生成器特性：函数遇到 yield 即暂停并把值交给调用方，调用方继续迭代时才恢复执行。
    这里用游标 fetchone 配合 while，做到"查一行、吐一行"，而不是先 fetchall 全量加载。

    Yields:
        dict: {"id","memory_type","importance","text"}，text=summary 优先、无摘要取全文。
    """
    sql = (
        "SELECT id, memory_type, content, importance, summary "
        "FROM npc_memory "
        "WHERE npc_id=%s AND importance>=%s AND session_id IN (%s, 'seed') "
        "ORDER BY importance DESC"
    )
    conn = db.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (npc_id, min_importance, session_id))
            while True:
                row = cursor.fetchone()      # 一次只取一行
                if row is None:              # fetchone 到底返回 None，结束
                    break
                text = row[4] if row[4] else row[2]  # summary 优先，无摘要用 content
                yield {
                    "id": row[0],
                    "memory_type": row[1],
                    "importance": row[3],
                    "text": text,
                }
    finally:
        conn.close()

def remember_conversation(npc_id, player_text, reply, session_id=""):
    """把一轮对话写回 NPC 记忆（P4-B 写回侧，先规则版）。

    session_id：记忆归属会话——不传则落空串（遗留行为），
    /chat 主链路会显式传当前会话，保证轮回后记忆随会话隔离。

    为什么放在 memory.py 而不是 main.py：
    main.py 是"薄入口"（只做接请求→调下层→回结果），不该写业务判断；
    "值不值得记 + 怎么组装记忆"是记忆系统的业务，归这里，职责单一。

    为什么先"规则版"、后"LLM 提炼"：
    - 规则版零成本零延迟，先跑通「聊→存→下次召回」的完整闭环；
    - 每条都记会把寒暄("嗯""好的")也存进去，噪声灌满记忆表、干扰召回；
    - 真正的"提炼要点 + 给 importance 打分"要再调一次 LLM（reflection），
      那是 P5 的升级项，本轮先不引入，保证任务小而完整。
    """
    # 1. 空 npc_id = 环境旁白，不产生 NPC 记忆，直接返回
    if not npc_id:
        return

    # 2. 太短的回复当寒暄，不值得记（防噪声灌满记忆表）
    if len(reply.strip()) < 8:
        return

    # 3. content 存完整上下文：玩家说了啥 + 我回了啥（NPC 记住"这次发生了什么"）
    content = f"玩家说：“{player_text}”，我回答：“{reply}”"
    # 4. summary 必须保留「玩家说了什么」——召回时 recall_memories 是 summary 优先，
    #    若只截自己的回答（旧版 reply[:50]），玩家原话会丢，NPC 下一句就接不上话（用户实证 09-08）。
    #    规则版只能截断拼接两段；真正的语义摘要留给 P5 的 LLM reflection
    summary = f"玩家说：「{player_text.strip()[:30]}」，我回：「{reply.strip()[:20]}」"

    write_memory(npc_id, "event", content, importance=5, summary=summary, session_id=session_id)


if __name__ == "__main__":
    print("== 2.6 记忆生成器召回演示（importance 阈值 + summary 优先）==")

    print("\n写入一条新记忆（importance=9，带摘要）：")
    write_memory("prince_adrian", "event", "书房争吵后王子独自饮酒。", importance=9,
                 summary="王子酒后失态", related_entity="study")
    print("  已写入")

    print("\n召回王子 importance >= 6 的记忆（生成器逐个产出，含新写入的 summary）：")
    for m in recall_memories("prince_adrian", min_importance=6):
        print(f"  [{m['importance']}分][{m['memory_type']}] {m['text']}")
