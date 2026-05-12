"""
Standalone Poisson mesh builder — runs as a subprocess spawned by server.py.

Because this is a separate process (not a thread) Flask keeps its own GIL and
stays fully responsive to HTTP polling requests while this job runs.

Usage (called by server.py, not directly):
  python mesh_worker.py <room_id> <pc_path> <uploads_dir> <data_dir>
"""

import sys
import os
import json
import logging
import hashlib
import time
from pathlib import Path

# ─── Parse arguments ──────────────────────────────────────────────────────────
if len(sys.argv) != 5:
    print("Usage: mesh_worker.py <room_id> <pc_path> <uploads_dir> <data_dir>", file=sys.stderr)
    sys.exit(1)

room_id     = sys.argv[1]
pc_path     = Path(sys.argv[2])
uploads_dir = Path(sys.argv[3])
data_dir    = Path(sys.argv[4])

# ─── Logging (stdout so server.py can stream it) ──────────────────────────────
# MUST reconfigure stdout BEFORE logging.basicConfig on Windows — piped stdout
# is block-buffered by default even with -u; line_buffering=True flushes after
# every newline so server.py sees each log line in real time.
import io as _io
sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, line_buffering=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [INFO] %(message)s",
    stream=sys.stdout,
    force=True,
)

# ─── Encryption helpers (mirrors server.py) ───────────────────────────────────
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

def _load_file_key():
    key_file = data_dir / ".secret_key"
    secret = key_file.read_text().strip()
    return hashlib.sha256((secret + ":file-encryption").encode()).digest()

_FILE_KEY = _load_file_key()

def read_encrypted(path: Path) -> bytes:
    blob = path.read_bytes()
    nonce, ct = blob[:12], blob[12:]
    return AESGCM(_FILE_KEY).decrypt(nonce, ct, None)

# ─── Progress / status helpers ────────────────────────────────────────────────
status_path   = uploads_dir / "walls" / f"{room_id}_mesh.status"
progress_path = uploads_dir / "walls" / f"{room_id}_mesh.progress"
glb_path      = uploads_dir / "walls" / f"{room_id}_mesh.glb"

def _progress(pct: int, phase: str):
    try:
        progress_path.write_text(json.dumps({"pct": pct, "phase": phase}))
    except Exception:
        pass
    logging.info("[mesh] %s: %3d%%  %s", room_id, pct, phase)
    # Explicit flush — belt-and-suspenders on Windows piped stdout
    try:
        sys.stdout.flush()
    except Exception:
        pass

# ─── Main build ───────────────────────────────────────────────────────────────
_progress(0, "Starting reconstruction")
t0 = time.time()

try:
    import numpy as np
    import open3d as o3d
    import trimesh

    # 1. Decrypt + load
    _progress(2, "Decoding point cloud")
    raw = read_encrypted(pc_path)
    arr = np.frombuffer(raw, dtype=np.float32).reshape(-1, 6)
    xyz = arr[:, :3].astype(np.float64)
    rgb = np.clip(arr[:, 3:6], 0.0, 1.0).astype(np.float64)
    n_pts = len(xyz)
    logging.info("[mesh] %s: loaded %s points", room_id, f"{n_pts:,}")

    pcd_full = o3d.geometry.PointCloud()
    pcd_full.points = o3d.utility.Vector3dVector(xyz)
    pcd_full.colors = o3d.utility.Vector3dVector(rgb)

    # 2. Downsample — 15 mm grid keeps ~300–700 K points on a typical room scan,
    #    which is fast enough for a Surface Pro 3.  5 mm was producing 6.8 M
    #    points which held the GIL for minutes on normal estimation.
    _progress(10, f"Resampling {n_pts:,} points (15 mm grid)")
    pcd = pcd_full.voxel_down_sample(voxel_size=0.015)
    n_down = len(pcd.points)
    logging.info("[mesh] %s: downsampled to %s points", room_id, f"{n_down:,}")

# 3. Normals — orient towards the centroid of the scan.
        # orient_normals_towards_camera_location is O(N) vs O(N log N) for the
        # MST approach.  For a LiDAR room scan taken from one interior position
        # every visible surface already faces inward toward the scanner, so the
        # centroid is a good proxy for the camera location.
        _progress(20, f"Estimating normals ({n_down:,} pts)")
        pcd.estimate_normals(
            search_param=o3d.geometry.KDTreeSearchParamHybrid(radius=0.06, max_nn=30)
        )
        _progress(32, "Orienting normals (towards scan centroid)")
        centroid = np.asarray(pcd.points).mean(axis=0)
        pcd.orient_normals_towards_camera_location(centroid)

    # 4. Poisson — depth=8 is 4× faster than depth=9 with still-excellent
    #    ~2 mm resolution at 5 m scale.  Appropriate for Surface Pro 3.
    _progress(42, "Running Screened Poisson (depth=8)…")
    mesh, densities = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
        pcd, depth=8, linear_fit=False
    )
    n_verts_raw = len(mesh.vertices)
    n_faces_raw = len(mesh.triangles)
    logging.info("[mesh] %s: Poisson produced %s verts, %s faces",
                 room_id, f"{n_verts_raw:,}", f"{n_faces_raw:,}")

    # 5. Trim
    _progress(62, "Trimming low-density exterior")
    d = np.asarray(densities)
    mesh.remove_vertices_by_mask(d < np.percentile(d, 2))
    mesh.remove_degenerate_triangles()
    mesh.remove_duplicated_vertices()
    mesh.remove_non_manifold_edges()
    logging.info("[mesh] %s: after trim — %s verts, %s faces",
                 room_id, f"{len(mesh.vertices):,}", f"{len(mesh.triangles):,}")

    # 6. Color transfer — workers=1 avoids subprocess spawning on Windows
    _progress(70, f"Transferring colors from {n_pts:,}-point cloud")
    from scipy.spatial import cKDTree
    pcd_pts = np.asarray(pcd_full.points)
    pcd_rgb = np.asarray(pcd_full.colors)
    mesh_pts = np.asarray(mesh.vertices)
    kd = cKDTree(pcd_pts)
    _progress(74, "KD-tree built — querying nearest neighbors")
    _, idxs = kd.query(mesh_pts, k=5, workers=1)
    vtx_colors = pcd_rgb[idxs].mean(axis=1)
    mesh.vertex_colors = o3d.utility.Vector3dVector(vtx_colors)
    logging.info("[mesh] %s: color transfer done", room_id)

    # 7. Export GLB
    _progress(88, "Exporting GLB")
    verts = np.asarray(mesh.vertices).astype(np.float32)
    faces = np.asarray(mesh.triangles).astype(np.uint32)
    colors_u8 = (np.clip(vtx_colors, 0, 1) * 255).astype(np.uint8)
    colors_rgba = np.concatenate(
        [colors_u8, np.full((len(colors_u8), 1), 255, dtype=np.uint8)], axis=1
    )
    tm = trimesh.Trimesh(vertices=verts, faces=faces, vertex_colors=colors_rgba, process=False)
    glb_bytes = tm.export(file_type="glb")
    _progress(96, "Writing GLB to disk")
    glb_path.write_bytes(glb_bytes)

    elapsed = time.time() - t0
    _progress(100, f"Done — {len(verts):,} verts, {len(faces):,} faces, "
                   f"{len(glb_bytes)//1024} KB, {elapsed:.0f}s")
    status_path.write_text("ready")
    logging.info("[mesh] %s: COMPLETE in %.1fs — %s verts, %s faces, %d KB",
                 room_id, elapsed, f"{len(verts):,}", f"{len(faces):,}", len(glb_bytes)//1024)

except Exception:
    import traceback
    logging.error("[mesh] %s: Poisson reconstruction FAILED", room_id)
    logging.error(traceback.format_exc())
    _progress(0, "Build failed — check server logs")
    status_path.write_text("failed")
