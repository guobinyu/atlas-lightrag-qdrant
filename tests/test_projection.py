import numpy as np
import pytest

from lab.projection import Projection


def test_query_uses_same_transform_and_background_does_not_move():
    vectors = [[1, 0, 0], [.9, .1, 0], [0, 1, 0], [0, 0, 1]]
    projection = Projection.fit(vectors, 3)
    before = projection.transform(vectors)
    q = projection.transform([vectors[1]])
    np.testing.assert_allclose(q[0], before[1])
    np.testing.assert_allclose(projection.transform(vectors), before)
    np.testing.assert_allclose(projection.transform([[9., 1., 0.]])[0], before[1])


@pytest.mark.parametrize("vectors,dimension", [([], 3), ([[1, 0, 0]], 3), ([[1, 0], [1, 0]], 2), ([[1], [-1]], 1)])
def test_projection_degenerate_cases(vectors, dimension):
    projection = Projection.fit(vectors, dimension)
    coords = projection.transform(vectors)
    assert coords.shape == (len(vectors), 2)
    assert np.isfinite(coords).all()
    assert np.isfinite(projection.variance)


@pytest.mark.parametrize("vector", [[1, 2], [np.nan, 1, 2], [0, 0, 0], [np.inf, 1, 2]])
def test_projection_rejects_invalid_vectors(vector):
    p = Projection.fit([[1, 0, 0], [0, 1, 0]], 3)
    with pytest.raises(ValueError):
        p.transform([vector])
