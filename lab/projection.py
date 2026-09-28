from dataclasses import dataclass

import numpy as np
from sklearn.decomposition import PCA


def normalized(vectors, dimension):
    values = np.asarray(vectors, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != dimension:
        raise ValueError(f"向量维度不一致，需要 {dimension} 维。")
    if not np.isfinite(values).all():
        raise ValueError("向量包含 NaN 或无穷值。")
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    if np.any(norms < 1e-12):
        raise ValueError("遇到零向量，无法计算可靠的 cosine 投影。")
    return values / norms


@dataclass
class Projection:
    dimension: int
    mean: np.ndarray
    components: np.ndarray
    variance: float
    note: str

    @classmethod
    def fit(cls, vectors, dimension):
        if not len(vectors):
            return cls(dimension, np.zeros(dimension), np.eye(dimension)[:2], 0.0, "工作区尚无向量")
        values = normalized(vectors, dimension)
        mean = values.mean(axis=0)
        if len(values) == 1 or np.allclose(values, mean):
            return cls(dimension, mean, np.eye(dimension)[:2], 0.0, "样本过少或向量重叠，坐标仅作占位")
        pca = PCA(n_components=min(2, len(values), dimension), svd_solver="full")
        pca.fit(values)
        return cls(dimension, pca.mean_, pca.components_, float(pca.explained_variance_ratio_.sum()), "")

    def transform(self, vectors):
        if not len(vectors):
            return np.empty((0, 2))
        values = normalized(vectors, self.dimension)
        result = (values - self.mean) @ self.components.T
        return np.pad(result, ((0, 0), (0, 2 - result.shape[1])))
