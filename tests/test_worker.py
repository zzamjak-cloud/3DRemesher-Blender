from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

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

    def test_build_engine_input_reads_binary_geometry_buffer(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = _binary_payload(Path(directory))

            engine_input = worker._build_engine_input(payload, Path(directory))

        self.assertEqual(engine_input.mesh.vertices, ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)))
        self.assertEqual(engine_input.mesh.faces, ((0, 1, 2),))
        self.assertEqual(engine_input.mesh.hard_edges, frozenset({(0, 1)}))
        self.assertEqual(engine_input.density_values, (0.25, 0.5, 1.0))
        self.assertEqual(engine_input.settings.topology_mode, "STRUCTURED")

    def test_binary_geometry_buffer_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = _binary_payload(Path(directory))
            payload["geometry_buffer"]["filename"] = "../geometry.bin"
            payload["input_fingerprint"] = worker._payload_fingerprint(payload)

            with self.assertRaisesRegex(ValueError, "temp dir"):
                worker._build_engine_input(payload, Path(directory))

    def test_binary_geometry_buffer_rejects_fingerprint_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = _binary_payload(Path(directory))
            payload["settings"]["target_quad_count"] = 8

            with self.assertRaisesRegex(ValueError, "fingerprint"):
                worker._build_engine_input(payload, Path(directory))

    def test_binary_geometry_buffer_rejects_byte_order_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = _binary_payload(Path(directory))
            payload["geometry_buffer"]["byte_order"] = "big"
            payload["input_fingerprint"] = worker._payload_fingerprint(payload)

            with self.assertRaisesRegex(ValueError, "byte_order"):
                worker._build_engine_input(payload, Path(directory))

    def test_binary_geometry_buffer_rejects_section_dtype_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = _binary_payload(Path(directory))
            payload["geometry_buffer"]["sections"]["vertices"]["dtype"] = "float32"
            payload["input_fingerprint"] = worker._payload_fingerprint(payload)

            with self.assertRaisesRegex(ValueError, "dtype"):
                worker._build_engine_input(payload, Path(directory))

    def test_binary_geometry_buffer_rejects_section_count_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = _binary_payload(Path(directory))
            payload["geometry_buffer"]["sections"]["faces"]["index_count"] = 2
            payload["input_fingerprint"] = worker._payload_fingerprint(payload)

            with self.assertRaisesRegex(ValueError, "개수"):
                worker._build_engine_input(payload, Path(directory))

    def test_binary_geometry_buffer_rejects_overlapping_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = _binary_payload(Path(directory))
            payload["geometry_buffer"]["sections"]["hard_edges"]["offset"] = payload["geometry_buffer"]["sections"]["faces"]["offset"]
            payload["input_fingerprint"] = worker._payload_fingerprint(payload)

            with self.assertRaisesRegex(ValueError, "겹칩니다"):
                worker._build_engine_input(payload, Path(directory))

    def test_binary_geometry_buffer_rejects_size_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = _binary_payload(Path(directory))
            path = Path(directory) / payload["geometry_buffer"]["filename"]
            path.write_bytes(path.read_bytes() + b"x")

            with self.assertRaisesRegex(ValueError, "크기"):
                worker._build_engine_input(payload, Path(directory))

    def test_binary_geometry_buffer_rejects_size_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = _binary_payload(Path(directory))
            payload["geometry_buffer"]["size"] = worker._MAX_GEOMETRY_BUFFER_SIZE + 1
            payload["input_fingerprint"] = worker._payload_fingerprint(payload)

            with self.assertRaisesRegex(ValueError, "상한"):
                worker._build_engine_input(payload, Path(directory))

    def test_binary_geometry_buffer_rejects_sha_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = _binary_payload(Path(directory))
            path = Path(directory) / payload["geometry_buffer"]["filename"]
            data = bytearray(path.read_bytes())
            data[-1] = (data[-1] + 1) % 256
            path.write_bytes(data)

            with self.assertRaisesRegex(ValueError, "SHA-256"):
                worker._build_engine_input(payload, Path(directory))

    def test_binary_geometry_buffer_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside_directory:
            payload = _binary_payload(Path(directory))
            path = Path(directory) / payload["geometry_buffer"]["filename"]
            outside_path = Path(outside_directory) / "outside.bin"
            outside_path.write_bytes(path.read_bytes())
            path.unlink()
            path.symlink_to(outside_path)

            with self.assertRaisesRegex(ValueError, "밖"):
                worker._build_engine_input(payload, Path(directory))


def _binary_payload(directory: Path) -> dict:
    vertices = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    faces = ((0, 1, 2),)
    hard_edges = ((0, 1),)
    density_values = (0.25, 0.5, 1.0)
    data = bytearray()
    data.extend(worker._GEOMETRY_BUFFER_MAGIC)
    data.extend(worker._PACK_UINT32.pack(worker._GEOMETRY_PROTOCOL_VERSION))
    sections = {}
    sections["vertices"] = {"offset": len(data), "count": len(vertices), "components": 3, "dtype": "float64"}
    for vertex in vertices:
        data.extend(worker._PACK_VECTOR3.pack(*vertex))
    sections["faces"] = {"offset": len(data), "count": len(faces), "index_count": sum(len(face) for face in faces), "dtype": "uint32_varlen"}
    for face in faces:
        data.extend(worker._PACK_UINT32.pack(len(face)))
        for index in face:
            data.extend(worker._PACK_UINT32.pack(index))
    sections["hard_edges"] = {"offset": len(data), "count": len(hard_edges), "components": 2, "dtype": "uint32"}
    for edge in hard_edges:
        for index in edge:
            data.extend(worker._PACK_UINT32.pack(index))
    sections["density_values"] = {"offset": len(data), "count": len(density_values), "components": 1, "dtype": "float64"}
    for value in density_values:
        data.extend(worker._PACK_FLOAT64.pack(value))

    filename = "geometry.bin"
    (directory / filename).write_bytes(data)
    payload = {
        "protocol_version": worker._GEOMETRY_PROTOCOL_VERSION,
        "settings": {
            "target_quad_count": 4,
            "symmetry_axes": (),
            "hard_edge_angle_degrees": 45.0,
            "guide_curve_names": (),
            "density_attribute_name": "remesh_density",
            "density_scale": 1.0,
            "topology_mode": "STRUCTURED",
        },
        "guide_curves": (),
        "geometry_buffer": {
            "filename": filename,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "byte_order": "little",
            "sections": sections,
        },
    }
    payload["input_fingerprint"] = worker._payload_fingerprint(json.loads(json.dumps(payload)))
    return payload


if __name__ == "__main__":
    unittest.main()
