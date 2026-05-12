"""
photo_project.py — Project high-res RGB iPhone photos onto Poisson mesh vertices.

Called by mesh_worker.py after Poisson reconstruction to replace the blurry
low-res LiDAR depth-sensor vertex colors with crisp 12MP iPhone RGB camera colors.

WHY THIS MATTERS
----------------
The point cloud's embedded colors come from the LiDAR depth sensor color readout,
which samples the RGB camera through a ~256×192 depth map → ~50K effective color
samples for a 10M-point cloud.  A 25%-resolution JPEG from the iPhone RGB camera
is 1008×756 = 761K pixels — 15× more color resolution per photo, and we have
10–15 of them covering the whole room.

ALGORITHM
---------
For each mesh vertex:
  1. Project the vertex into every snapshot's image plane.
  2. Score each snapshot by how "face-on" the camera is to the vertex surface
     (dot product of vertex normal with camera-to-vertex direction).
  3. The best-scoring snapshot wins.  Sample its pixel with bilinear interpolation.
  4. Vertices not visible in any photo fall back to LiDAR IDW colors (caller
     handles the fallback).

ARKit coordinate conventions
-----------------------------
- camera.transform   = column-major 4×4 camera-to-world transform
- camera.intrinsics  = column-major 3×3: cols are (fx,0,0), (0,fy,0), (cx,cy,1)
  stored as 9 floats: [col0.x, col0.y, col0.z, col1.x, col1.y, col1.z, col2.x, col2.y, col2.z]
- Camera looks along −Z in camera space (positive Z = behind camera)
"""

import json
import logging
from pathlib import Path

import numpy as np


def project_photos(
    mesh_verts:   np.ndarray,   # (N, 3) float64 — vertex positions in ARKit world space
    mesh_normals: np.ndarray,   # (N, 3) float64 — vertex normals in ARKit world space
    snap_dir:     Path,         # directory containing <room_id>_snaps.json + JPEG files
    room_id:      str,
) -> tuple[np.ndarray, np.ndarray] | None:
    """
    Returns:
        (colors, coverage) where
            colors:   (N, 3) float64  RGB in [0, 1]
            coverage: (N,)   bool     True for vertices colored from a photo
        Returns None if no snapshot metadata file exists for this room.
    """
    meta_path = snap_dir / f"{room_id}_snaps.json"
    if not meta_path.exists():
        return None

    try:
        snaps = json.loads(meta_path.read_text())
    except Exception as e:
        logging.warning("[photo] %s: failed to read snaps metadata: %s", room_id, e)
        return None

    if not snaps:
        return None

    try:
        from PIL import Image
        import io as _io
    except ImportError:
        logging.warning("[photo] Pillow not installed — cannot do photo projection")
        return None

    N = len(mesh_verts)
    best_colors = np.zeros((N, 3), dtype=np.float64)
    best_score  = np.full(N, -np.inf)

    # Normalise mesh normals once
    nlen = np.linalg.norm(mesh_normals, axis=1, keepdims=True)
    nlen = np.where(nlen > 1e-6, nlen, 1.0)
    normals_n = mesh_normals / nlen

    for snap_idx, snap_meta in enumerate(snaps):
        jpeg_path = snap_dir / snap_meta["file"]
        if not jpeg_path.exists():
            logging.warning("[photo] %s: snapshot file missing: %s", room_id, snap_meta["file"])
            continue

        try:
            img = np.array(
                Image.open(jpeg_path).convert("RGB"),
                dtype=np.float32
            ) / 255.0
        except Exception as e:
            logging.warning("[photo] %s: failed to load %s: %s", room_id, snap_meta["file"], e)
            continue

        H, W = img.shape[:2]

        # ── Camera matrices ───────────────────────────────────────────────────
        # ARKit stores matrices column-major; reshape(4,4,order='F') gives the
        # standard row-major layout where c2w[:3, 3] = camera position.
        c2w = np.array(snap_meta["c2w"], dtype=np.float64).reshape(4, 4, order="F")
        try:
            w2c = np.linalg.inv(c2w)
        except np.linalg.LinAlgError:
            continue

        # Intrinsics (column-major 3×3) → standard row-major:
        #   K[0,0]=fx  K[1,1]=fy  K[0,2]=cx  K[1,2]=cy
        K  = np.array(snap_meta["K"], dtype=np.float64).reshape(3, 3, order="F")
        fw = float(snap_meta["fw"])   # full image width the intrinsics were calibrated for
        fh = float(snap_meta["fh"])   # full image height

        # Scale intrinsics to the actual JPEG resolution (25% of full res)
        sx = W / fw
        sy = H / fh
        fx = K[0, 0] * sx
        fy = K[1, 1] * sy
        cx = K[0, 2] * sx
        cy = K[1, 2] * sy

        # ── Project all vertices ──────────────────────────────────────────────
        verts_h   = np.column_stack([mesh_verts, np.ones(N)])   # (N, 4)
        verts_cam = (w2c @ verts_h.T).T                         # (N, 4)

        z_cam = verts_cam[:, 2]
        x_cam = verts_cam[:, 0]
        y_cam = verts_cam[:, 1]

        # In ARKit camera space, camera looks along −Z: in-front ⟺ z_cam < 0
        in_front = z_cam < -0.05   # at least 5 cm in front

        # Project (safe denominator avoids div-by-zero outside in_front mask)
        neg_z = np.where(in_front, -z_cam, 1.0)
        px = fx * x_cam / neg_z + cx
        py = fy * y_cam / neg_z + cy

        # Leave 1-pixel border so bilinear never reads outside bounds
        in_bounds = (px >= 0.0) & (px < W - 1.0) & (py >= 0.0) & (py < H - 1.0)

        # ── Facing / visibility score ─────────────────────────────────────────
        cam_pos       = c2w[:3, 3]                                   # (3,)
        cam_to_vert   = mesh_verts - cam_pos                         # (N, 3)
        dist          = np.linalg.norm(cam_to_vert, axis=1, keepdims=True)
        cam_to_vert_n = cam_to_vert / np.where(dist > 1e-6, dist, 1.0)

        # Positive facing score when vertex normal opposes the cam→vertex ray
        # (surface is facing the camera)
        facing = (-normals_n * cam_to_vert_n).sum(axis=1)     # (N,)

        valid  = in_front & in_bounds & (facing > 0.05)        # 5° from grazing
        better = valid & (facing > best_score)

        if not better.any():
            continue

        # ── Bilinear color sampling ───────────────────────────────────────────
        bv     = np.where(better)[0]
        bpx    = px[bv]
        bpy    = py[bv]

        x0 = bpx.astype(np.int32)
        y0 = bpy.astype(np.int32)
        x1 = np.minimum(x0 + 1, W - 1)
        y1 = np.minimum(y0 + 1, H - 1)

        dx = (bpx - x0)[:, None]
        dy = (bpy - y0)[:, None]

        sampled = (img[y0, x0] * (1 - dx) * (1 - dy) +
                   img[y0, x1] * dx        * (1 - dy) +
                   img[y1, x0] * (1 - dx)  * dy       +
                   img[y1, x1] * dx        * dy)

        best_colors[bv] = sampled
        best_score[bv]  = facing[bv]

        logging.info("[photo] %s: snap %d/%d projected onto %s vertices",
                     room_id, snap_idx + 1, len(snaps), f"{int(better.sum()):,}")

    coverage = best_score > -np.inf
    pct = 100.0 * coverage.mean() if N > 0 else 0.0
    logging.info("[photo] %s: total photo coverage %s / %s verts (%.1f%%)",
                 room_id, f"{int(coverage.sum()):,}", f"{N:,}", pct)

    return best_colors, coverage
