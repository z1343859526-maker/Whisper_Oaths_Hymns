"""向量存储抽象 + numpy 实现（余弦相似度检索）。

为什么抽一层 VectorStore 抽象基类：
- 现在用 numpy 手写先跑通闭环；以后换 Faiss/Chroma 或真 embedding，
  只需新写子类实现 add/search，上层 context_builder 不用改；
- 「面向接口编程」：上层依赖抽象，不依赖具体实现。

余弦相似度为什么退化成点积：
- 向量都已 L2 归一化（模长=1），cos(a,b) = a·b / (|a||b|) = a·b，
  直接做一次点积（矩阵乘法）就得到查询与所有候选的相似度。

技术清单落位：
- np.asarray 列表转数组、reshape(1,dim) 整形、q @ matrix.T 相似度矩阵、np.argsort 取 top_k。
"""
from abc import ABC, abstractmethod

import numpy as np


class VectorStore(ABC):
    """向量存储抽象基类：定义 add/search 两个接口，具体实现可替换。"""

    @abstractmethod
    def add(self, id_, vector, meta: dict) -> None:
        """写入一条向量及其元信息。"""

    @abstractmethod
    def search(self, query, top_k: int) -> list[dict]:
        """检索与 query 最相似的 top_k 条，返回 [{"id","score","meta"}]。"""


class NumpyVectorStore(VectorStore):
    """纯 numpy 实现：把所有向量堆成一个矩阵，用矩阵乘法做相似度检索。"""

    def __init__(self, dim: int):
        self.dim = dim
        self.ids: list = []
        self.metas: list[dict] = []
        self.matrix = None  # (N, dim)，懒创建

    def add(self, id_, vector, meta: dict) -> None:
        v = np.asarray(vector, dtype=np.float32).reshape(1, self.dim)
        if self.matrix is None:
            self.matrix = v
        else:
            self.matrix = np.vstack([self.matrix, v])
        self.ids.append(id_)
        self.metas.append(meta)

    def search(self, query, top_k: int) -> list[dict]:
        if self.matrix is None:
            return []
        q = np.asarray(query, dtype=np.float32).reshape(1, self.dim)
        sims = (q @ self.matrix.T).reshape(-1)          # 一次矩阵乘法算出全部相似度
        top_k = min(top_k, len(sims))
        order = np.argsort(sims)[::-1][:top_k]          # 降序取 top_k
        return [
            {"id": self.ids[i], "score": float(sims[i]), "meta": self.metas[i]}
            for i in order
        ]
