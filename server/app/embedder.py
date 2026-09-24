"""文本向量化：中文字符 n-gram 哈希 + numpy 随机投影（零外部 embedding API 依赖）。

为什么不用真实的 embedding 模型（如 BGE/m3e）：
- DeepSeek 不保证提供 embedding 接口，真实 embedding 要额外拉模型、花钱、联网；
- 本项目的世界观条目都是短文本（几十字），检索时"关键词重叠"足够命中相关条目；
- 字符 n-gram 对中文尤其合适——中文没有空格分词，按字切 n-gram 天然覆盖了"词"的局部信息；
- 随机投影是经典降维手段（Johnson-Lindenstrauss 引理保证近似保距），
  把稀疏高维的 n-gram 空间压到低维稠密向量后，仍能做余弦相似度检索。

技术清单落位：
- np.random.RandomState(seed).randn(...) → 生成固定随机投影矩阵（可复现）
- x @ proj → 矩阵乘法做投影
- reshape / 列表转数组 → 见 vector_store.py（这里产出向量，那边消费向量）
"""
import hashlib

import numpy as np


class NGramEmbedder:
    """把一段中文文本压成 dim 维、L2 归一化的稠密向量。"""

    def __init__(self, dim=1024, hash_space=4096, n=2, seed=42):
        self.dim = dim
        self.n = n
        self.hash_space = hash_space
        # 固定 seed 的随机投影矩阵：同一段文本每次生成的向量完全一致（可复现、可回放）。
        # randn 生成标准正态分布，是随机投影里最常用的投影方向分布。
        # dim=1024（M1.3 从 128 上调）：128 维把 4096 维稀疏 gram 压得太狠，中文短文本
        # 的真实字面重叠信号被投影噪声淹没（实测：问"宵禁几点"，无字面重叠的条目
        # 反而排第 1）。JL 保距精度随维度上升——1024 维下关键词重叠能稳定进前列。
        # ⚠️ 维度是隐式契约：backfill（vectorize_knowledge 用本默认值）与检索端
        # （context_builder._EMBED_DIM）必须一致，漂移即检索静默失效（维度不匹配会炸，
        # 更隐蔽的是改了一处没改另一处的历史库存量向量）。
        self.proj = np.random.RandomState(seed).randn(hash_space, dim)

    def _ngrams(self, text: str) -> list[str]:
        """把文本切成字符 n-gram（中文按字切，覆盖局部词信息）。"""
        text = text.strip()
        if len(text) < self.n:
            return [text] if text else []
        return [text[i:i + self.n] for i in range(len(text) - self.n + 1)]

    def _sparse_vec(self, text: str) -> np.ndarray:
        """把文本转成 hash_space 维的稀疏计数向量（hashing trick：gram 哈希进桶）。"""
        v = np.zeros(self.hash_space, dtype=np.float32)
        for gram in self._ngrams(text):
            h = int(hashlib.md5(gram.encode("utf-8")).hexdigest(), 16)
            v[h % self.hash_space] += 1.0
        return v

    def embed(self, text: str) -> np.ndarray:
        """文本 -> dim 维 L2 归一化向量。"""
        x = self._sparse_vec(text)            # (hash_space,)
        v = x @ self.proj                      # (hash_space,) @ (hash_space, dim) -> (dim,)
        norm = np.linalg.norm(v)
        if norm == 0:
            return v.astype(np.float32)
        return (v / norm).astype(np.float32)   # L2 归一化：模长=1，点积即余弦相似度
