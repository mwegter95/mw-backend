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
    source_points: np.ndarray,  # (M, 3) float64 — raw/depth cloud used to build a visibility z-buffer
    snap_dir:     Path,         # directory containing <room_id>_snaps.json + JPEG files
    room_id:      str,
) -> tuple[np.ndarray, np.ndarray, dict] | None:
    """
    Returns:
        (colors, coverage, stats) where
            colors:   (N, 3) float64  RGB in [0, 1]
            coverage: (N,)   bool     True for vertices colored from a photo
            stats:    dict    projection diagnostics
        Returns None if no snapshot metadata file exists for this room.
    """
    meta_path = snap_dir / f"{room_id}_snaps.json"
    if not meta_path.exists():
        logging.info("[photo] %s: no snapshot metadata file found at %s", room_id, meta_path.name)
        return None

    try:
        snaps = json.loads(meta_path.read_text())
    except Exception as e:
        logging.warning("[photo] %s: failed to read snaps metadata: %s", room_id, e)
        return None

    if not snaps:
        logging.info("[photo] %s: snapshot metadata file is empty", room_id)
        return None

    try:
        from PIL import Image
    except ImportError:
        logging.warning("[photo] Pillow not installed - cannot do photo projection")
        return None

    logging.info("[photo] %s: loaded %d snapshots from metadata", room_id, len(snaps))

    N = len(mesh_verts)
    best_colors = np.zeros((N, 3), dtype=np.float64)
    best_score  = np.full(N, -np.inf)
    coverage_mask = np.zeros(N, dtype=bool)
    # Use a stable visibility subset so each photo gets a cheap z-buffer pass.
    if len(source_points) > 350_000:
        step = max(1, len(source_points) // 350_000)
        visibility_pts = source_points[::step]
    else:
        visibility_pts = source_points
    visibility_h = np.column_stack([visibility_pts, np.ones(len(visibility_pts))])

    # Normalise mesh normals once
    nlen = np.linalg.norm(mesh_normals, axis=1, keepdims=True)
    nlen = np.where(nlen > 1e-6, nlen, 1.0)
    normals_n = mesh_normals / nlen
    projected_per_snap = []
    won_per_snap = []

    def _map_uv_orientation(u: np.ndarray, v: np.ndarray, ori: int,
                            W: int, H: int, fw: float, fh: float) -> tuple[np.ndarray, np.ndarray]:
        """Map full-res camera-plane coordinates (u, v) into JPEG pixels for a tested orientation.

        ori: 0=0deg, 1=90deg CW, 2=180deg, 3=270deg CW
        """
        if ori == 0:
            return u * (W / fw), v * (H / fh)
        if ori == 1:
            return (fh - 1.0 - v) * (W / fh), u * (H / fw)
        if ori == 2:
            return (fw - 1.0 - u) * (W / fw), (fh - 1.0 - v) * (H / fh)
        return v * (W / fh), (fw - 1.0 - u) * (H / fw)

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

        if len(snap_meta.get("K", [])) != 9 or len(snap_meta.get("c2w", [])) != 16:
            logging.warning("[photo] %s: snapshot %s has invalid camera metadata", room_id, snap_meta.get("file"))
            continue

        # Intrinsics (column-major 3x3) in full-res camera-pixel coordinates.
        K  = np.array(snap_meta["K"], dtype=np.float64).reshape(3, 3, order="F")
        fw = float(snap_meta.get("fw", 0.0))
        fh = float(snap_meta.get("fh", 0.0))
        if fw <= 1.0 or fh <= 1.0:
            logging.warning("[photo] %s: snapshot %s missing fw/fh", room_id, snap_meta.get("file"))
            continue

        fx = K[0, 0]
        fy = K[1, 1]
        cx = K[0, 2]
        cy = K[1, 2]

        # ── Project all vertices ──────────────────────────────────────────────
        verts_h   = np.column_stack([mesh_verts, np.ones(N)])   # (N, 4)
        verts_cam = (w2c @ verts_h.T).T                         # (N, 4)
        vis_cam   = (w2c @ visibility_h.T).T                    # (M, 4)

        z_cam = verts_cam[:, 2]
        x_cam = verts_cam[:, 0]
        y_cam = verts_cam[:, 1]
        vis_z_cam = vis_cam[:, 2]
        vis_x_cam = vis_cam[:, 0]
        vis_y_cam = vis_cam[:, 1]

        # In ARKit camera space, camera looks along −Z: in-front ⟺ z_cam < 0
        in_front = z_cam < -0.05   # at least 5 cm in front
        vis_in_front = vis_z_cam < -0.05

        # Project into full-res camera coordinates first; orientation+scale are applied later.
        neg_z = np.where(in_front, -z_cam, 1.0)
        u = fx * x_cam / neg_z + cx
        v = fy * y_cam / neg_z + cy
        vis_neg_z = np.where(vis_in_front, -vis_z_cam, 1.0)
        vis_u = fx * vis_x_cam / vis_neg_z + cx
        vis_v = fy * vis_y_cam / vis_neg_z + cy

        # Auto-select image orientation by maximising in-bounds projection count.
        best_ori = 0
        best_ori_hits = -1
        best_px = None
        best_py = None
        best_vis_px = None
        best_vis_py = None
        for ori in (0, 1, 2, 3):
            cand_px, cand_py = _map_uv_orientation(u, v, ori, W, H, fw, fh)
            hits = int((in_front & (cand_px >= 0.0) & (cand_px < W - 1.0) & (cand_py >= 0.0) & (cand_py < H - 1.0)).sum())
            if hits > best_ori_hits:
                best_ori_hits = hits
                best_ori = ori
                best_px, best_py = cand_px, cand_py
                best_vis_px, best_vis_py = _map_uv_orientation(vis_u, vis_v, ori, W, H, fw, fh)

        px = best_px
        py = best_py
        vis_px = best_vis_px
        vis_py = best_vis_py

        # Leave 1-pixel border so bilinear never reads outside bounds.
        in_bounds = (px >= 0.0) & (px < W - 1.0) & (py >= 0.0) & (py < H - 1.0)
        vis_in_bounds = (vis_px >= 0.0) & (vis_px < W) & (vis_py >= 0.0) & (vis_py < H)

        # Visibility z-buffer: only let a photo color mesh vertices that land near the
        # front-most observed depth at that pixel. This prevents painting back walls
        # through sofas, lamps, and other foreground geometry.
        z_scale = 2.0
        z_w = max(1, int(np.ceil(W / z_scale)))
        z_h = max(1, int(np.ceil(H / z_scale)))
        zbuf = np.full((z_h, z_w), np.inf, dtype=np.float32)
        vis_mask = vis_in_front & vis_in_bounds
        if vis_mask.any():
            zx = np.clip(np.rint(vis_px[vis_mask] / z_scale).astype(np.int32), 0, z_w - 1)
            zy = np.clip(np.rint(vis_py[vis_mask] / z_scale).astype(np.int32), 0, z_h - 1)
            zdepth = (-vis_z_cam[vis_mask]).astype(np.float32)
            np.minimum.at(zbuf, (zy, zx), zdepth)

        # ── Score: favor face-on views and nearby cameras ─────────────────────
        cam_pos       = c2w[:3, 3]                                   # (3,)
        cam_to_vert   = mesh_verts - cam_pos                         # (N, 3)
        dist          = np.linalg.norm(cam_to_vert, axis=1, keepdims=True)
        cam_to_vert_n = cam_to_vert / np.where(dist > 1e-6, dist, 1.0)

        # abs(cos) so Poisson-flipped normals don't drop valid samples.
        facing_abs = np.abs((-normals_n * cam_to_vert_n).sum(axis=1))
        score = facing_abs / (dist.squeeze() + 0.2)

        pix_x = np.clip(np.rint(px / z_scale).astype(np.int32), 0, z_w - 1)
        pix_y = np.clip(np.rint(py / z_scale).astype(np.int32), 0, z_h - 1)
        z_ref = zbuf[pix_y, pix_x]
        depth = -z_cam
        # Depth tolerance grows with distance to absorb Poisson smoothing drift.
        visible = np.isfinite(z_ref) & (depth <= z_ref + np.maximum(0.08, 0.03 * z_ref))

        valid  = in_front & in_bounds & visible
        better = valid & (score > best_score)
        projected_per_snap.append(int(valid.sum()))
        won_per_snap.append(int(better.sum()))

        if not better.any():
            continue

        # ── Bilinear color sampling ───────────────────────────────────────────
        bv  = np.where(better)[0]
        bpx = px[bv]
        bpy = py[bv]

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
        best_score[bv]  = score[bv]
        coverage_mask[bv] = True

        logging.info(
            "[photo] %s: snap %d/%d ori=%d better=%s in_front=%s in_bounds=%s visible=%s",
            room_id,
            snap_idx + 1,
            len(snaps),
            best_ori,
            f"{int(better.sum()):,}",
            f"{int(in_front.sum()):,}",
            f"{int((in_front & in_bounds).sum()):,}",
            f"{int((in_front & in_bounds & visible).sum()):,}",
        )

    pct = 100.0 * coverage_mask.mean() if N > 0 else 0.0
    logging.info("[photo] %s: total photo coverage %s / %s verts (%.1f%%)",
                 room_id, f"{int(coverage_mask.sum()):,}", f"{N:,}", pct)

    stats = {
        "photoSnapshotsTotal": int(len(snaps)),
        "photoSnapshotsProjected": int(sum(1 for c in projected_per_snap if c > 0)),
        "photoSnapshotsWinning": int(sum(1 for c in won_per_snap if c > 0)),
        "photoProjectedVerts": int(coverage_mask.sum()),
        "photoTotalVerts": int(N),
        "photoCoveragePct": float(pct),
    }
    return best_colors, coverage_mask, stats
