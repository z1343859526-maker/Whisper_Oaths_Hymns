"""离线脚本：把 world_knowledge 的 title+content 向量化，写回 embedding 字段。

为什么离线跑一次、而非每次检索都现算：
- 知识库是静态的（条目不会每轮对话都变），向量化一次存库，
  之后检索只做「读向量 + 算相似度」；
- 若每轮请求都现算，等于把全部条目重新向量化，白白浪费 CPU；
- 「离线预计算 + 在线只检索」是 RAG 的标准工程姿势。

用法（在 server/ 目录下）：
    python scripts/vectorize_knowledge.py
"""
import json
import os
import sys

# 让脚本能 import 到 app 包（脚本在 scripts/ 下，app 在上一级）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.embedder import NGramEmbedder
from app import db


def main():
    embedder = NGramEmbedder()
    rows = db.execute_query("SELECT id, title, content FROM world_knowledge")
    if not rows:
        print("world_knowledge 无数据，请先执行 sql/seed.sql 灌数据。")
        return

    for row in rows:
        kid, title, content = row
        # 标题 + 正文一起向量化：标题往往承载关键词，正文提供上下文
        vec = embedder.embed(f"{title} {content}")
        db.update_knowledge_embedding(kid, json.dumps(vec.tolist()))
        print(f"[OK] id={kid} 《{title}》 -> dim={len(vec)}")

    print(f"\n完成：共向量化 {len(rows)} 条知识。")


if __name__ == "__main__":
    main()
