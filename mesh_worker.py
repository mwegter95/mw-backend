"""
Standalone Poisson mesh builder - runs as a subprocess spawned by server.py.

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

# --- Parse arguments --------------------------------------------------------
if len(sys.argv) != 5:
    print("Usage: mesh_worker.py <room_id> <pc_path> <uploads_dir> <data_dir>", file=sys.stderr)
    sys.exit(1)

room_id     = sys.argv[1]
pc_path     = Path(sys.argv[2])
uploads_dir = Path(sys.argv[3])
data_dir    = Path(sys.argv[4])

# --- Logging (stdout so server.py can stream it) ----------------------------
# MUST reconfigure stdout BEFORE logging.basicConfig on Windows - piped stdout
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

# --- Encryption helpers (mirrors server.py) ---------------------------------
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

# --- Progress / status helpers ----------------------------------------------
status_path   = uploads_dir / "walls" / f"{room_id}_mesh.status"
progress_path = uploads_dir / "walls" / f"{room_id}_mesh.progress"
glb_path      = uploads_dir / "walls" / f"{room_id}_mesh.glb"

def _progress(pct: int, phase: str, extra: dict = None):
    try:
        data = {"pct": pct, "phase": phase}
        if extra:
            data.update(extra)
        progress_path.write_text(json.dumps(data))
    except Exception:
        pass
    logging.info("[mesh] %s: %3d%%  %s", room_id, pct, phase)
    # Explicit flush - belt-and-suspenders on Windows piped stdout
    try:
        sys.stdout.flush()
    except Exception:
        pass

# --- Main build -------------------------------------------------------------
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

    # 2. Downsample - 5 mm voxel grid.
    # 5 mm on a 10M-point room scan produces ~800K-1.5M points: enough density
    # for depth=9 Poisson to resolve all visible room features, while keeping
    # the build under 60-90 s on a Surface Pro 3.
    # The previous 2mm/depth=11 combo produced 10M input points and 485s -
    # that's 64× more work than depth=9 for a 2× linear resolution gain.
    VOXEL = 0.005
    _progress(10, f"Resampling {n_pts:,} pts (5 mm voxel)...")
    pcd    = pcd_full.voxel_down_sample(voxel_size=VOXEL)
    n_down = len(pcd.points)
    logging.info("[mesh] %s: 5 mm voxel -> %s pts", room_id, f"{n_down:,}")

    # Hard cap: safety net for unusually dense scans.
    MAX_PTS = 1_200_000
    if n_down > MAX_PTS:
        pcd    = pcd.random_down_sample(MAX_PTS / n_down)
        n_down = len(pcd.points)
        logging.info("[mesh] %s: capped to %s pts", room_id, f"{n_down:,}")

    # 3. Normals - 3 cm radius at 5 mm spacing = ~30 neighbours in a disc.
    _progress(20, f"Estimating normals ({n_down:,} pts)...")
    pcd.estimate_normals(
        search_param=o3d.geometry.KDTreeSearchParamHybrid(radius=0.03, max_nn=50)
    )
    _progress(32, "Orienting normals (towards scan centroid)")
    centroid = np.asarray(pcd.points).mean(axis=0)
    pcd.orient_normals_towards_camera_location(centroid)

    # 4. Poisson depth=9: ~1 mm surface resolution at 5 m room scale.
    # linear_fit=True uses a linear interpolant inside each octree cell -
    # produces sharper edges on planar surfaces like walls.
    _progress(42, "Running Screened Poisson (depth=9)...")
    mesh, densities = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
        pcd, depth=9, linear_fit=True
    )
    n_verts_raw = len(mesh.vertices)
    n_faces_raw = len(mesh.triangles)
    logging.info("[mesh] %s: Poisson produced %s verts, %s faces",
                 room_id, f"{n_verts_raw:,}", f"{n_faces_raw:,}")

    # 5. Trim - 0.5 %: remove only the very lowest-density phantom geometry.
    _progress(62, "Trimming low-density exterior")
    d = np.asarray(densities)
    mesh.remove_vertices_by_mask(d < np.percentile(d, 0.5))
    mesh.remove_degenerate_triangles()
    mesh.remove_duplicated_vertices()
    mesh.remove_non_manifold_edges()
    logging.info("[mesh] %s: after trim - %s verts, %s faces",
                 room_id, f"{len(mesh.vertices):,}", f"{len(mesh.triangles):,}")
    # NOTE: No Laplacian smoothing - at 5 mm / depth=9 density the surface is
    # already smooth; smoothing would blur the wall texture we want to preserve.

    # 5c. Remove any NaN/Inf vertices introduced by Poisson or smoothing.
    mesh_pts_check = np.asarray(mesh.vertices)
    bad_mask = ~np.isfinite(mesh_pts_check).all(axis=1)
    n_bad = int(bad_mask.sum())
    if n_bad > 0:
        logging.warning("[mesh] %s: removing %d non-finite vertices before color transfer", room_id, n_bad)
        mesh.remove_vertices_by_mask(bad_mask)
        mesh.remove_degenerate_triangles()
        mesh.remove_duplicated_vertices()

    # 6. Color transfer.
    # PRIORITY: use photo projection from high-res RGB snapshots if available.
    # Fallback: IDW from the low-res LiDAR depth-sensor colors.
    # Photo snapshots give ~50× better color resolution (12MP RGB vs ~256×192
    # LiDAR color) and eliminate the orange warmth / blurry gradient artifacts.
    _progress(70, "Transferring colors...")
    from scipy.spatial import cKDTree
    pcd_pts = np.asarray(pcd_full.points)
    pcd_rgb = np.asarray(pcd_full.colors)
    mesh_pts = np.asarray(mesh.vertices)
    if len(mesh_pts) == 0:
        raise ValueError("Mesh has no vertices after cleaning")
    mesh.compute_vertex_normals()
    mesh_nrm = np.asarray(mesh.vertex_normals)

    # Try photo projection first
    snap_dir = uploads_dir / "walls"
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from photo_project import project_photos
        result = project_photos(mesh_pts, mesh_nrm, pcd_pts, snap_dir, room_id)
    except Exception as _pe:
        logging.warning("[mesh] %s: photo_project failed (%s) - using IDW fallback", room_id, _pe)
        result = None

    if result is not None:
        photo_colors, coverage = result
        n_photo = int(coverage.sum())
        logging.info("[mesh] %s: photo projection covered %s / %s verts (%.1f%%)",
                     room_id, f"{n_photo:,}", f"{len(mesh_pts):,}", 100 * coverage.mean())
        # IDW for vertices not reached by any photo
        n_fallback = int((~coverage).sum())
        if n_fallback > 0:
            _progress(74, f"IDW fallback for {n_fallback:,} uncovered vertices")
            kd = cKDTree(pcd_pts)
            fb_idx = np.where(~coverage)[0]
            dists, idxs = kd.query(mesh_pts[fb_idx], k=3, workers=1)
            w = 1.0 / (dists + 1e-6)
            w /= w.sum(axis=1, keepdims=True)
            photo_colors[fb_idx] = (pcd_rgb[idxs] * w[:, :, None]).sum(axis=1)
        vtx_colors = photo_colors
        color_method = "photo-RGB"
    else:
        _progress(70, f"IDW from {n_pts:,}-point LiDAR cloud (k=3)")
        kd = cKDTree(pcd_pts)
        _progress(74, "KD-tree built - querying nearest neighbours")
        dists, idxs = kd.query(mesh_pts, k=3, workers=1)
        w = 1.0 / (dists + 1e-6)
        w /= w.sum(axis=1, keepdims=True)
        vtx_colors = (pcd_rgb[idxs] * w[:, :, None]).sum(axis=1)
        color_method = "LiDAR sensor (IDW)"

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
    build_stats = {"rawPts": n_pts, "poissonPts": n_down,
                   "meshVerts": len(verts), "meshFaces": len(faces),
                   "voxelMm": int(VOXEL * 1000), "poissonDepth": 9,
                   "colorMethod": color_method}
    _progress(100, f"Done - {len(verts):,} verts, {len(faces):,} faces, "
                   f"{len(glb_bytes)//1024} KB, {elapsed:.0f}s",
                   extra=build_stats)
    status_path.write_text("ready")
    logging.info("[mesh] %s: COMPLETE in %.1fs - %s verts, %s faces, %d KB",
                 room_id, elapsed, f"{len(verts):,}", f"{len(faces):,}", len(glb_bytes)//1024)

except Exception:
    import traceback
    logging.error("[mesh] %s: Poisson reconstruction FAILED", room_id)
    logging.error(traceback.format_exc())
    _progress(0, "Build failed - check server logs")
    status_path.write_text("failed")
