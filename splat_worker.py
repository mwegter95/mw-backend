"""
Standalone 3D Gaussian Splat trainer — runs as a subprocess spawned by server.py.

Because this is a separate process (not a thread) Flask keeps its own GIL and
stays fully responsive to HTTP polling requests while OpenSplat trains.

Usage (called by server.py, not directly):
  python splat_worker.py <room_id> [--steps N]
"""

import sys
import os
import io as _io
import re
import json
import shutil
import struct
import hashlib
import logging
import subprocess
import argparse
from pathlib import Path

# MUST reconfigure stdout BEFORE logging.basicConfig — piped stdout is
# block-buffered by default even with -u; line_buffering=True flushes after
# every newline so server.py sees each log line in real time.
# errors='replace' prevents UnicodeEncodeError on Windows (cp1252 pipe) when
# log messages contain characters outside the system code page (e.g. arrows).
sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, line_buffering=True,
                               encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [INFO] %(message)s",
    stream=sys.stdout,
    force=True,
)

# --- Load .env so os.getenv() picks up OPENSPLAT_BIN etc. --------------------
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

# --- Encryption helpers (mirrors server.py) ----------------------------------
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_BASE_DIR = Path(__file__).parent
_DATA_DIR = _BASE_DIR / "data"

def _load_file_key() -> bytes:
    key_file = _DATA_DIR / ".secret_key"
    secret = key_file.read_text().strip()
    return hashlib.sha256((secret + ":file-encryption").encode()).digest()

_FILE_KEY = _load_file_key()

def decrypt_bytes(blob: bytes) -> bytes:
    nonce, ct = blob[:12], blob[12:]
    return AESGCM(_FILE_KEY).decrypt(nonce, ct, None)

# --- Path helpers ------------------------------------------------------------

def _uploads_walls() -> Path:
    return _DATA_DIR / "uploads" / "walls"

def _splat_dir() -> Path:
    d = _DATA_DIR / "splats"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _status_path(room_id: str) -> Path:
    return _splat_dir() / f"{room_id}_splat.status"

def _progress_path(room_id: str) -> Path:
    return _splat_dir() / f"{room_id}_splat.progress"

def _work_dir(room_id: str) -> Path:
    return _splat_dir() / f"{room_id}_work"

# --- Progress / status helpers -----------------------------------------------

def _write_status(room_id: str, status: str):
    try:
        _status_path(room_id).write_text(status)
    except Exception:
        pass

def _write_progress(room_id: str, pct: int, phase: str):
    try:
        _progress_path(room_id).write_text(json.dumps({"pct": pct, "phase": phase}))
    except Exception:
        pass
    logging.info("[splat] %s: %3d%%  %s", room_id, pct, phase)
    try:
        sys.stdout.flush()
    except Exception:
        pass

# --- PLY -> .splat converter -------------------------------------------------
# OpenSplat 1.1.5 removed --output-type and always writes a standard 3DGS PLY.
# This function converts that to the compact 32-byte antimatter15 .splat format
# that GaussianSplatViewer.jsx expects.
#
# .splat layout (32 bytes / Gaussian, little-endian):
#   [0..11]  xyz position  float32 × 3
#   [12..23] log-scale xyz float32 × 3  (stored as-is from PLY scale_*)
#   [24..27] RGBA colour   uint8  × 4   (sigmoid + SH-DC transform)
#   [28..31] quat xyzw     uint8  × 4   (encoded: byte = q*127 + 128)

def _ply_to_splat(ply_path: Path, splat_path: Path):
    import numpy as np
    import re as _re

    with open(ply_path, "rb") as f:
        # Parse ASCII header
        header_lines: list[str] = []
        while True:
            line = f.readline().decode("ascii", errors="replace").rstrip()
            header_lines.append(line)
            if line == "end_header":
                break
        header = "\n".join(header_lines)

        m = _re.search(r"element vertex (\d+)", header)
        if not m:
            raise ValueError("PLY missing 'element vertex'")
        n = int(m.group(1))

        # Collect float properties in declaration order
        props = _re.findall(r"property float (\w+)", header)
        if not props:
            raise ValueError("PLY has no float properties")
        pidx = {p: i for i, p in enumerate(props)}

        raw = np.frombuffer(f.read(n * len(props) * 4),
                            dtype="<f4").reshape(n, len(props))

    def _g(name: str) -> np.ndarray:
        i = pidx.get(name)
        if i is None:
            raise KeyError(f"PLY missing property '{name}'")
        return raw[:, i].copy()

    # Position (pass-through)
    x, y, z = _g("x"), _g("y"), _g("z")

    # Scale — PLY stores log-scale, viewer expects log-scale: pass-through
    s0, s1, s2 = _g("scale_0"), _g("scale_1"), _g("scale_2")

    # Colour: colour = clip(0.5 + SH_C0 * f_dc, 0, 1)
    SH_C0 = 0.28209479177388
    cr = np.clip(0.5 + SH_C0 * _g("f_dc_0"), 0.0, 1.0)
    cg = np.clip(0.5 + SH_C0 * _g("f_dc_1"), 0.0, 1.0)
    cb = np.clip(0.5 + SH_C0 * _g("f_dc_2"), 0.0, 1.0)
    ca = 1.0 / (1.0 + np.exp(-_g("opacity")))   # sigmoid

    # Quaternion: PLY rot_0=w, rot_1=x, rot_2=y, rot_3=z -> .splat xyzw
    qw = _g("rot_0"); qx = _g("rot_1"); qy = _g("rot_2"); qz = _g("rot_3")
    qnorm = np.sqrt(qx**2 + qy**2 + qz**2 + qw**2)
    qnorm = np.where(qnorm < 1e-8, 1.0, qnorm)
    qx /= qnorm; qy /= qnorm; qz /= qnorm; qw /= qnorm

    # Pack into structured array (32 bytes per Gaussian, no padding)
    dtype = np.dtype([
        ("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
        ("sx","<f4"), ("sy","<f4"), ("sz","<f4"),
        ("r", "u1"),  ("g", "u1"),  ("b", "u1"),  ("a", "u1"),
        ("qx","u1"),  ("qy","u1"),  ("qz","u1"),  ("qw","u1"),
    ])
    out = np.empty(n, dtype=dtype)
    out["x"] = x;  out["y"] = y;  out["z"] = z
    out["sx"] = s0; out["sy"] = s1; out["sz"] = s2
    out["r"]  = np.clip(cr * 255, 0, 255).astype(np.uint8)
    out["g"]  = np.clip(cg * 255, 0, 255).astype(np.uint8)
    out["b"]  = np.clip(cb * 255, 0, 255).astype(np.uint8)
    out["a"]  = np.clip(ca * 255, 0, 255).astype(np.uint8)
    # Quaternion encoding: byte = q * 127 + 128  (decode: q = (b - 128) / 127)
    out["qx"] = np.clip(qx * 127 + 128, 0, 255).astype(np.uint8)
    out["qy"] = np.clip(qy * 127 + 128, 0, 255).astype(np.uint8)
    out["qz"] = np.clip(qz * 127 + 128, 0, 255).astype(np.uint8)
    out["qw"] = np.clip(qw * 127 + 128, 0, 255).astype(np.uint8)

    splat_path.write_bytes(out.tobytes())
    logging.info("[splat] PLY->splat: %d gaussians, %.1f MB",
                 n, splat_path.stat().st_size / 1e6)


# --- transforms.json builder -------------------------------------------------

def _col_major_to_row_major_4x4(flat: list) -> list:
    """Convert a 16-element column-major (ARKit) flat list to a 4×4 row-major list."""
    # flat[col*4 + row] → out[row][col]
    return [
        [flat[0], flat[4], flat[8],  flat[12]],
        [flat[1], flat[5], flat[9],  flat[13]],
        [flat[2], flat[6], flat[10], flat[14]],
        [flat[3], flat[7], flat[11], flat[15]],
    ]

def _build_transforms(snaps: list, images_dir: Path) -> dict:
    """Build a nerfstudio/OpenSplat transforms.json from snapshot metadata."""
    frames = []
    for i, s in enumerate(snaps):
        src_file = _uploads_walls() / s["file"]
        dst_name = f"frame_{i:04d}.jpg"
        dst_path = images_dir / dst_name
        shutil.copy2(str(src_file), str(dst_path))

        c2w = s.get("c2w", [])       # 16 floats, column-major
        K   = s.get("K", [])         # 9 floats, column-major
        fw  = int(s.get("fw", 0))
        fh  = int(s.get("fh", 0))

        if len(c2w) != 16 or len(K) != 9:
            logging.warning("[splat] snapshot %d has bad c2w/K — skipping", i)
            continue

        transform_matrix = _col_major_to_row_major_4x4(c2w)

        # K is column-major SIMD-padded: [fx,0,0, 0,fy,0, cx,cy,1]
        # Indices: 0=fx, 4=fy, 6=cx, 7=cy
        fl_x = float(K[0])
        fl_y = float(K[4])
        cx   = float(K[6])
        cy   = float(K[7])

        frames.append({
            "file_path": f"images/{dst_name}",
            "transform_matrix": transform_matrix,
            "fl_x": fl_x,
            "fl_y": fl_y,
            "cx": cx,
            "cy": cy,
            "w": fw,
            "h": fh,
        })

    return {"camera_model": "PINHOLE", "frames": frames}

# --- PLY init cloud writer ---------------------------------------------------

_MAX_INIT_POINTS = 300_000

def _write_init_ply(raw_pts: bytes, out_path: Path):
    """Decrypt raw bytes (N×24: 6×float32 x,y,z,r,g,b) and write a binary PLY."""
    bytes_per_point = 24  # 6 × float32
    n_total = len(raw_pts) // bytes_per_point

    # Subsample if too large
    if n_total > _MAX_INIT_POINTS:
        stride   = n_total // _MAX_INIT_POINTS
        indices  = range(0, n_total, stride)
        raw_pts  = b"".join(
            raw_pts[i * bytes_per_point : (i + 1) * bytes_per_point]
            for i in indices
        )
        n_total = len(raw_pts) // bytes_per_point
        logging.info("[splat] init cloud subsampled to %d pts", n_total)

    header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        f"element vertex {n_total}\n"
        "property float x\n"
        "property float y\n"
        "property float z\n"
        "property uchar red\n"
        "property uchar green\n"
        "property uchar blue\n"
        "end_header\n"
    ).encode("ascii")

    # Convert each point: float32 x,y,z then uint8 r,g,b
    point_buf = bytearray()
    for i in range(n_total):
        offset = i * bytes_per_point
        x, y, z, rf, gf, bf = struct.unpack_from("<ffffff", raw_pts, offset)
        r = max(0, min(255, int(rf * 255)))
        g = max(0, min(255, int(gf * 255)))
        b = max(0, min(255, int(bf * 255)))
        point_buf += struct.pack("<fffBBB", x, y, z, r, g, b)

    out_path.write_bytes(header + bytes(point_buf))
    logging.info("[splat] wrote init PLY: %s (%d pts, %.1f MB)",
                 out_path.name, n_total, out_path.stat().st_size / 1e6)

# --- OpenSplat progress parsing ----------------------------------------------

_ITER_RE = re.compile(r"[Ii]teration[s]?\s+(\d+)(?:\s*/\s*(\d+))?", re.IGNORECASE)

def _parse_progress(line: str, num_steps: int):
    """Return (current_iter, total_iter) or None if line has no iteration info."""
    m = _ITER_RE.search(line)
    if not m:
        return None
    cur = int(m.group(1))
    tot = int(m.group(2)) if m.group(2) else num_steps
    return cur, tot

# --- Main --------------------------------------------------------------------

def main(room_id: str, num_steps: int):
    work_dir  = _work_dir(room_id)
    images_dir = work_dir / "images"
    splat_dir  = _splat_dir()

    _write_status(room_id, "processing")
    _write_progress(room_id, 0, "Queued…")

    try:
        # ── 1. Load snapshot metadata ─────────────────────────────────────
        _write_progress(room_id, 2, "Loading snapshot metadata")
        meta_path = _uploads_walls() / f"{room_id}_snaps.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"Snapshot metadata not found: {meta_path}")

        snaps = json.loads(meta_path.read_text())
        if not snaps:
            raise ValueError("No snapshots in metadata file")
        logging.info("[splat] %s: found %d snapshots", room_id, len(snaps))

        # ── 2. Create work directory and copy images ───────────────────────
        _write_progress(room_id, 5, f"Copying {len(snaps)} snapshot images")
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        images_dir.mkdir(parents=True, exist_ok=True)

        # ── 3. Build transforms.json ───────────────────────────────────────
        _write_progress(room_id, 10, "Building transforms.json")
        transforms = _build_transforms(snaps, images_dir)
        if not transforms["frames"]:
            raise ValueError("No valid frames produced from snapshot metadata")
        (work_dir / "transforms.json").write_text(json.dumps(transforms, indent=2))
        logging.info("[splat] %s: wrote transforms.json (%d frames)",
                     room_id, len(transforms["frames"]))

        # ── 4. Optional: decrypt LiDAR init cloud ─────────────────────────
        pc_path   = _uploads_walls() / f"{room_id}_pointcloud.bin"
        ply_path  = work_dir / "init_cloud.ply"
        use_ply   = False

        if pc_path.exists():
            _write_progress(room_id, 15, "Decrypting LiDAR point cloud for init")
            try:
                raw_encrypted = pc_path.read_bytes()
                raw_pts = decrypt_bytes(raw_encrypted)
                _write_init_ply(raw_pts, ply_path)
                use_ply = True
            except Exception as e:
                logging.warning("[splat] %s: failed to build init PLY (%s) — proceeding without it", room_id, e)
                use_ply = False
        else:
            logging.info("[splat] %s: no point cloud found — SfM init will be used", room_id)

        # ── 5. Build OpenSplat command ─────────────────────────────────────
        # OpenSplat 1.1.5 removed --output-type; it always writes a .ply.
        # We convert PLY -> .splat ourselves after training (see step 7).
        opensplat_bin = os.environ.get("OPENSPLAT_BIN", "opensplat")
        output_ply    = work_dir / f"{room_id}_output.ply"
        final_splat   = splat_dir / f"{room_id}.splat"

        cmd = [
            opensplat_bin,
            str(work_dir),          # nerfstudio project dir (contains transforms.json)
            "--num-iters", str(num_steps),
            "-o", str(output_ply),
        ]
        # Optional: force CPU execution (set OPENSPLAT_CPU=1 in .env on GPU-less machines)
        if os.environ.get("OPENSPLAT_CPU", "").lower() in ("1", "true", "yes"):
            cmd += ["--cpu"]

        logging.info("[splat] %s: running: %s", room_id, " ".join(cmd))

        # ── Pre-flight: make sure the binary actually exists ───────────────
        resolved = shutil.which(opensplat_bin)
        if resolved is None:
            msg = (
                f"OpenSplat binary not found: '{opensplat_bin}'. "
                "Download a Windows build from https://github.com/pierotofy/OpenSplat/releases "
                "then set OPENSPLAT_BIN=C:\\path\\to\\opensplat.exe in your .env file."
            )
            logging.error("[splat] %s: %s", room_id, msg)
            _write_progress(room_id, 0, f"Setup needed: {msg}")
            _write_status(room_id, "error")
            sys.exit(1)

        logging.info("[splat] %s: resolved binary -> %s", room_id, resolved)
        _write_status(room_id, "training")
        _write_progress(room_id, 20, "Starting OpenSplat…")

        # ── 6. Run OpenSplat, stream progress ─────────────────────────────
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc  = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(work_dir),
            creationflags=flags,
        )

        last_pct = 20
        for raw in iter(proc.stdout.readline, b""):
            try:
                line = raw.decode("utf-8", errors="replace").rstrip()
            except Exception:
                line = repr(raw)
            if line:
                logging.info("[opensplat] %s", line)
                parsed = _parse_progress(line, num_steps)
                if parsed:
                    cur, tot = parsed
                    frac = cur / max(1, tot)
                    pct  = 20 + int(frac * 75)
                    if pct != last_pct:
                        _write_progress(room_id, pct,
                                        f"Training: {cur}/{tot} iterations")
                        last_pct = pct

        proc.wait()
        if proc.returncode != 0:
            # 0xC0000135 = Windows STATUS_DLL_NOT_FOUND: binary runs but
            # can't find LibTorch / OpenCV DLLs at runtime.
            if proc.returncode in (3221225781, -1073741515):
                raise RuntimeError(
                    "OpenSplat exited with 0xC0000135 (DLL not found). "
                    "Copy all .dll files from C:\\libtorch\\lib\\ and "
                    "C:\\opencv\\build\\x64\\vc16\\bin\\ into the same "
                    "folder as opensplat.exe, then retry."
                )
            raise RuntimeError(f"OpenSplat exited with code {proc.returncode}")

        # ── 7. Convert PLY -> .splat ───────────────────────────────────────
        # OpenSplat outputs a standard 3DGS PLY; our viewer needs the compact
        # 32-byte-per-Gaussian antimatter15 .splat binary.
        _write_progress(room_id, 95, "Converting PLY to .splat…")
        if not output_ply.exists():
            raise FileNotFoundError(f"OpenSplat did not produce output: {output_ply}")
        _ply_to_splat(output_ply, final_splat)
        logging.info("[splat] %s: final splat -> %s (%.1f MB)",
                     room_id, final_splat.name, final_splat.stat().st_size / 1e6)

        _write_progress(room_id, 100, "Done")
        _write_status(room_id, "ready")
        logging.info("[splat] %s: COMPLETE", room_id)

    except Exception as exc:
        logging.exception("[splat] %s: FAILED — %s", room_id, exc)
        _write_progress(room_id, 0, f"Failed: {exc}")
        _write_status(room_id, "failed")
        # Cleanup happens in finally block
        sys.exit(1)

    finally:
        # Always remove the work directory, even on failure
        shutil.rmtree(str(work_dir), ignore_errors=True)
        logging.info("[splat] %s: cleaned up work dir", room_id)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("room_id")
    p.add_argument("--steps", type=int, default=7000)
    args = p.parse_args()
    main(args.room_id, args.steps)
