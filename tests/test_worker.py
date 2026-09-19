from __future__ import annotations

import unittest

from addon import worker


class WorkerPayloadTests(unittest.TestCase):
    def test_build_engine_input_preserves_topology_and_guide_metadata(self):
        payload = {
            "mesh": {
                "vertices": ((0, 0, 0), (1, 0, 0), (0, 1, 0)),
                "faces": ((0, 1, 2),),
                "hard_edges": (),
            },
            "settings": {
                "target_quad_count": 4,
                "symmetry_axes": (),
                "hard_edge_angle_degrees": 45.0,
                "guide_curve_names": ("REMESH_GUIDE_LOOP_eye",),
                "density_attribute_name": "remesh_density",
                "density_scale": 1.0,
                "topology_mode": "STRUCTURED",
            },
            "guide_curves": (
                {
                    "name": "REMESH_GUIDE_LOOP_eye",
                    "splines": (((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 0)),),
                    "kind": ("LOOP",),
                    "closed": (True,),
                },
            ),
            "density_values": (),
        }

        engine_input = worker._build_engine_input(payload)

        self.assertEqual(engine_input.settings.topology_mode, "STRUCTURED")
        self.assertEqual(engine_input.guide_curves[0].kind, ("LOOP",))
        self.assertEqual(engine_input.guide_curves[0].closed, (True,))

    def test_build_engine_input_accepts_legacy_guide_payload(self):
        payload = {
            "mesh": {
                "vertices": ((0, 0, 0), (1, 0, 0), (0, 1, 0)),
                "faces": ((0, 1, 2),),
                "hard_edges": (),
            },
            "settings": {
                "target_quad_count": 4,
                "symmetry_axes": (),
                "hard_edge_angle_degrees": 45.0,
                "guide_curve_names": ("REMESH_GUIDE_axis",),
                "density_attribute_name": "remesh_density",
                "density_scale": 1.0,
            },
            "guide_curves": (
                {
                    "name": "REMESH_GUIDE_axis",
                    "splines": (((0, 0, 0), (1, 0, 0)),),
                },
            ),
            "density_values": (),
        }

        engine_input = worker._build_engine_input(payload)

        self.assertEqual(engine_input.settings.topology_mode, "AUTO")
        self.assertEqual(engine_input.guide_curves[0].kind, ("DIRECTION",))
        self.assertEqual(engine_input.guide_curves[0].closed, (False,))


if __name__ == "__main__":
    unittest.main()
