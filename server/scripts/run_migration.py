"""SQL 迁移执行器：按 .env 配置执行迁移脚本（M1.1 起 DDL 交付的标准跑法）。

为什么不用 `mysql -p < file`：交互输密码不可自动化，且密码进 shell 历史是坏习惯；
复用 app.db 的 .env 连接配置，命令行零敏感信息。

用法（在 server/ 目录下）：
    python scripts/run_migration.py sql/migrations/001_session_isolation.sql

注意：本执行器不跟踪已执行过的迁移（无 migrations 版本表）——
当前规模下"失败即停 + 报错可读"已够用；迁移数量成规模后再引入版本表。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import get_connection  # noqa: E402  复用 .env 里的连接配置


def main(path: str) -> None:
    sql = Path(path).read_text(encoding="utf-8")

    # 去 '--' 注释行后按分号拆语句；USE 语句跳过（连接已指定库）
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    statements = [s.strip() for s in "\n".join(lines).split(";") if s.strip()]

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            executed = 0
            for stmt in statements:
                if stmt.upper().startswith("USE "):
                    continue
                cur.execute(stmt)
                executed += 1
        conn.commit()
        print(f"迁移完成：{path}（执行 {executed} 条语句）")
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("用法：python scripts/run_migration.py <迁移脚本路径>")
        sys.exit(2)
    main(sys.argv[1])
