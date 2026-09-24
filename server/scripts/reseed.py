"""种子重灌工具（M1.3 偿还 D-05 债务；M1.4 增补后置自检；M1.5 增补测试世界）：
seed.sql 幂等重灌 + 向量 backfill + 行数对账 + 种子计划质检 + 测试世界重灌。

用法（server/ 目录下）：
    python scripts/reseed.py

五步流程：
  1. 执行 sql/seed.sql（TRUNCATE+INSERT，天然幂等，可反复跑）；
  2. 调 scripts/vectorize_knowledge.py 给 world_knowledge 全量 backfill 向量——
     关键坑：context_builder._knowledge_block 会静默跳过 embedding 为 NULL 的条目，
     不补向量 = 新灌的知识条目在 RAG 里"不存在"；
  3. 关键表行数对账（EXPECTED 硬编码预期值）：灌完即验，行数漂移当场暴露，
     而不是等到 M1.5 执行器跑飞了再回头排查数据；
  4. 种子计划质检（validate_seed.py 后置自检，M1.4）：行数对账只证明"数量对"，
     证明不了"语义对"（计划的场景/目标/前置是否真能在初始世界满足）。灌完的库
     必须再过质检器，任一不过 exit 1——带病种子不许流到 M1.5 执行器。
  5. 重灌测试世界（seed_test_world.sql + 补向量）：seed.sql 对 world_knowledge 等表
     做 TRUNCATE，会清掉测试世界数据——所以测试世界必须在黄金乡重灌之后补灌，
     否则"跑一次 reseed 测试世界就没了"。本步把测试世界纳入闭环，一次 reseed 全齐。

为什么不直接用 mysql CLI source：Windows 下 mysql 不一定在 PATH；且工具要串 backfill
与对账，单文件闭环比"手册式多步操作"可靠（少一次人为漏步）。
"""
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]   # server/
sys.path.insert(0, str(BASE))

from app import db  # noqa: E402

SEED_FILE = BASE / "sql" / "seed.sql"
TEST_WORLD_FILE = BASE / "sql" / "seed_test_world.sql"

# 测试世界计划归属的 NPC（plans 无 world_id 列，删计划靠它；角色/环境卡已按 world_id 删）
# M1.5 泛化：character_card/environment_card 按 world_id 整段清，这里的 TEST_NPCS 仅用于 plans。
TEST_NPCS = ("test_man", "test_woman")

# 行数对账预期值：与 seed.sql 内容同步维护（改种子必须改这里，对账就是这么逼你同步的）
EXPECTED = {
    "character_card": 7,     # 7 NPC：王子/管家/大公/骑士/使者/女仆/夫人
    "schedule": 36,          # 全员日程骨架（M1.3）
    "world_knowledge": 15,   # global 8 + isabella 私有 7（M1.3 原子化拆分后）
    "environment_card": 14,  # 11 地点 + 3 物品（golden 世界；test 世界第 5 步另灌，对账在此步只验黄金乡）
    "goals": 5,
    "plans": 1,              # 夫人 poison_duke v1（golden；test 世界计划由第 5 步灌入）
    "world": 1,              # world 清单：第 1 步 seed.sql 插 golden；test 第 5 步补插（对账在第 3 步，此时仅 golden）
}


def load_statements(path: Path) -> list[str]:
    """读一个 SQL 文件 → 去整行注释 → 按分号切分为可执行语句列表。

    只处理「行首 --」注释（seed 的既有约定，字符串内无行首注释歧义）；
    语句以行尾分号收口累积——中文正文用全角标点，不会出现半角分号切断字符串。
    """
    stmts, buf = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("--"):
            continue
        buf.append(line)
        if line.rstrip().endswith(";"):
            sql = "\n".join(buf).strip()
            if sql:
                stmts.append(sql)
            buf = []
    return stmts


def run_seed() -> int:
    stmts = load_statements(SEED_FILE)
    conn = db.get_connection()
    try:
        with conn.cursor() as cursor:
            for s in stmts:
                cursor.execute(s)
        conn.commit()
    finally:
        conn.close()
    return len(stmts)


def run_test_world() -> int:
    """重灌测试世界：先清空 test 命名空间，再灌 seed_test_world.sql。

    为什么必须先清空：seed_test_world.sql 对 character_card/environment_card/plans
    是 INSERT（部分带 ON DUPLICATE KEY UPDATE），但对 world_knowledge 是纯 INSERT——
    重复执行会撞主键。先 DELET 掉 test 命名空间再灌，避免"二次跑 reseed 报重复"。
    注意：golden 世界 TRUNCATE 已在 run_seed 里完成，这里只清 test。

    Returns: 成功执行的非 USE 语句数。
    """
    conn = db.get_connection()
    try:
        with conn.cursor() as cursor:
            # 清空 test 世界命名空间（按 world_id='test' 整体清理，不碰黄金乡）。
            # M1.5 泛化：character_card/environment_card 有 world_id 列，按世界维度删；
            # plans 无 world_id（挂在 NPC 上），测试世界计划均属 test_man/test_woman。
            cursor.execute("DELETE FROM character_card WHERE world_id='test'")
            cursor.execute("DELETE FROM environment_card WHERE world_id='test'")
            cursor.execute("DELETE FROM plans WHERE npc_id IN (%s,%s) AND session_id='seed'", TEST_NPCS)
            cursor.execute("DELETE FROM world_knowledge WHERE world_id='test'")
            conn.commit()
            # 重灌
            n = 0
            for s in load_statements(TEST_WORLD_FILE):
                if s.upper().startswith("USE "):
                    continue
                cursor.execute(s)
                n += 1
            conn.commit()
    finally:
        conn.close()
    return n


def vectorize_test_world() -> int:
    """给测试世界知识（world_id='test'）补向量。

    复用 vectorize_knowledge.py 的同一套逻辑（NGramEmbedder + update_knowledge_embedding），
    但只限定 test 世界——golden 的向量第 2 步已全量 backfill，无需重算。
    为什么单独补：seed_test_world.sql 是第 5 步才灌入，此时 golden 早已向量化完；
    若不补 test 知识向量，context_builder._knowledge_block 的 `embedding IS NOT NULL`
    会把它滤掉，测试角色检索不到自己的知识。

    Returns: 向量化的条数。
    """
    from app.embedder import NGramEmbedder
    embedder = NGramEmbedder()
    rows = db.execute_query(
        "SELECT id, title, content FROM world_knowledge WHERE world_id='test'")
    for kid, title, content in rows:
        import json
        vec = embedder.embed(f"{title} {content}")
        db.update_knowledge_embedding(kid, json.dumps(vec.tolist()))
    return len(rows)


def run_vectorize():
    """subprocess 调既有脚本而非复制逻辑：backfill 的单一事实源在 vectorize_knowledge.py。

    PYTHONIOENCODING=utf-8：Windows 子进程 stdout 默认 GBK，父进程按 utf-8 解会炸
    （中文标题混 [OK] 标记时必踩）。钉死子进程输出编码 = 两端一致，不依赖控制台代码页。
    """
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [sys.executable, str(BASE / "scripts" / "vectorize_knowledge.py")],
        cwd=str(BASE), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise RuntimeError("vectorize_knowledge.py 失败，见上方输出")
    return result.stdout


def run_validate():
    """种子计划质检（单一事实源在 validate_seed.py，这里只编排、不复制逻辑）。

    与 run_vectorize 同一复用哲学，差异在输出策略：不做 capture，子进程 stdout
    直接继承本进程终端——改种子后重灌时，操作者要能实时看见质检明细（哪条计划
    哪步引用悬空/前置不达），而不是被归纳吞掉。本函数只消费 returncode 判成败。
    """
    result = subprocess.run(
        [sys.executable, str(BASE / "scripts" / "validate_seed.py")],
        cwd=str(BASE))
    return result.returncode == 0


def check_counts() -> bool:
    ok = True
    for table, expected in EXPECTED.items():
        got = db.execute_query(f"SELECT COUNT(*) FROM {table}")[0][0]
        mark = "[OK]" if got == expected else "[FAIL]"
        if got != expected:
            ok = False
        print(f"  {mark} {table:<18} 预期 {expected:>3} 实际 {got:>3}")
    return ok


def main():
    print(f"[1/5] 执行 {SEED_FILE.name} ……")
    n = run_seed()
    print(f"      共执行 {n} 条语句")

    print("[2/5] world_knowledge 向量 backfill ……")
    out = run_vectorize()
    vec_lines = [l for l in out.splitlines() if l.startswith("[OK]")]
    print(f"      向量化 {len(vec_lines)} 条知识")

    print("[3/5] 行数对账：")
    if not check_counts():
        print("\n[FAIL] 行数对账不通过——请核对 seed.sql 与本脚本 EXPECTED 是否同步。")
        sys.exit(1)

    print("[4/5] 种子计划质检（validate_seed.py 后置自检）：")
    if not run_validate():
        print("\n[FAIL] 计划质检未通过——带病种子不得进入 M1.5，修正 seed 数据后重跑本脚本。")
        sys.exit(1)

    print("[5/5] 重灌测试世界（seed_test_world.sql + 补向量）：")
    test_n = run_test_world()
    test_vec = vectorize_test_world()
    print(f"      测试世界执行 {test_n} 条语句，知识向量化 {test_vec} 条")

    print("\n重灌完成：行数对账 + 计划质检 + 测试世界全部就绪。")


if __name__ == "__main__":
    main()
