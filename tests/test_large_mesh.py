from __future__ import annotations

import unittest

from addon.core import MeshData
from addon.large_mesh import needs_preprocessing


class LargeMeshRoutingTests(unittest.TestCase):
    def test_small_mesh_keeps_existing_engine(self):
        mesh = MeshData(((0, 0, 0), (1, 0, 0), (0, 1, 0)), ((0, 1, 2),))
        self.assertFalse(needs_preprocessing(mesh))

    def test_face_limit_routes_to_preprocessing(self):
        mesh = MeshData(((0, 0, 0), (1, 0, 0), (0, 1, 0)), ((0, 1, 2),) * 10001)
        self.assertTrue(needs_preprocessing(mesh))

    def test_triangulated_budget_also_routes_to_preprocessing(self):
        mesh = MeshData(tuple((float(i), 0, 0) for i in range(9)), (tuple(range(9)),) * 3000)
        self.assertTrue(needs_preprocessing(mesh))

    def test_vertex_limit_routes_to_preprocessing(self):
        mesh = MeshData(((0, 0, 0),) * 20001, ((0, 1, 2),))
        self.assertTrue(needs_preprocessing(mesh))


if __name__ == "__main__":
    unittest.main()
