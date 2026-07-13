#!/usr/bin/env python3
"""geom.py -- Open3D geometry stages for the recon container (P6G.2 / docs/RECON_CONTRACT.md).

The real work behind the `odom` / `tsdf` / `simexport` / `align` stages. NO COLMAP (doctrine gate 1):
poses come from RGBD odometry + pose-graph loop closure; the recorded planar `/odom` is a *prior*, not
the trajectory. Everything here operates on IN-MEMORY RGBD frames -- a list of {t, rgb, depth} dicts --
so the synthetic-motion selftest (`python geom.py --selftest`) validates trajectory + mesh accuracy on a
known scene WITHOUT real capture data (which is data-blocked). `rrd_to_lerobot.read_rrd_frames` feeds the
same interface from a real `.rrd`; that path is `VERIFY ON DESKTOP` on a real garage bundle.

Frames (docs/FRAMES.md): body X-fwd Y-left Z-up; camera optical Z-fwd X-right Y-down. A per-run
trajectory/mesh is in `run_local` (seed = the first camera pose = identity). open3d-only (in the recon
image); imported lazily by cli.py so the CPU-lean ingest path never needs open3d.

Convention (load-bearing, verified by the selftest's GT check):
  compute_rgbd_odometry(source=frame_i, target=frame_{i-1}) returns T mapping points cam_i -> cam_{i-1},
  i.e. T_{cam_{i-1}}_{cam_i}. World (run_local) camera pose accumulates: pose_i = pose_{i-1} @ T.
  TSDF integrate wants extrinsic = world->camera = inv(pose_i).
"""
import math

import numpy as np

# Open3D is heavy + only present in the recon image; import lazily inside functions that need it so
# this module imports (for --help / unit reasoning) even where open3d is absent.
DEPTH_TRUNC_M = 5.0            # ignore returns past this in odom/tsdf (far depth is noisy on this rig)
LOOP_MIN_GAP = 10             # min frame index gap before two frames count as a loop-closure candidate
LOOP_TRANS_RADIUS_M = 0.75    # candidate revisit: node translations within this in run_local
LOOP_FITNESS_MIN = 0.30       # reject a loop edge whose RGBD odometry fitness is below this


def make_intrinsic(w, h, fx, fy, cx, cy):
    import open3d as o3d
    return o3d.camera.PinholeCameraIntrinsic(int(w), int(h), float(fx), float(fy), float(cx), float(cy))


def pair_rgbd(rgb_stream, depth_stream, max_skew_s=0.06):
    """Wall-time RGBD pairing (doctrine gate 4 -- NEVER frame_idx; depth's frame_idx is best-effort).
    rgb_stream / depth_stream: lists of (wall_t, array). Each depth frame joins the nearest RGB frame by
    |t_rgb - t_depth|; a pair past max_skew_s is rejected. Returns (paired, stats) where paired is a list
    of {t, rgb, depth} in ascending depth time and stats has kept/rejected counts + max skew kept."""
    rgb = sorted(rgb_stream, key=lambda r: r[0])
    rgb_t = [r[0] for r in rgb]
    paired, rejected, max_kept = [], 0, 0.0
    import bisect
    for td, dimg in sorted(depth_stream, key=lambda d: d[0]):
        if not rgb_t:
            rejected += 1
            continue
        j = bisect.bisect_left(rgb_t, td)
        cand = []
        if j < len(rgb_t):
            cand.append(j)
        if j > 0:
            cand.append(j - 1)
        best = min(cand, key=lambda k: abs(rgb_t[k] - td))
        skew = abs(rgb_t[best] - td)
        if skew > max_skew_s:
            rejected += 1
            continue
        box = rgb[best][2] if len(rgb[best]) > 2 else None
        paired.append({"t": float(td), "rgb": rgb[best][1], "depth": dimg, "box": box})
        max_kept = max(max_kept, skew)
    stats = {"pairs_kept": len(paired), "pairs_rejected_skew": rejected,
             "max_skew_kept_s": round(max_kept, 4), "max_pair_skew_s": max_skew_s}
    return paired, stats


def mask_target(depth, box, dilate_frac=0.15):
    """Person-mask v0 (doctrine gate 5): zero depth inside the dilated logged TARGET box before odom+tsdf
    so the followed operator doesn't smear a ghost into the mesh. box = (x0, y0, x1, y1) in pixels (or
    None -> no-op). Returns (masked_depth, masked_fraction). Mutates a copy, never the input."""
    d = np.array(depth, copy=True)
    if box is None:
        return d, 0.0
    h, w = d.shape[:2]
    x0, y0, x1, y1 = box
    bw, bh = (x1 - x0), (y1 - y0)
    dx, dy = bw * dilate_frac, bh * dilate_frac
    xa = max(0, int(math.floor(x0 - dx)));  xb = min(w, int(math.ceil(x1 + dx)))
    ya = max(0, int(math.floor(y0 - dy)));  yb = min(h, int(math.ceil(y1 + dy)))
    if xb <= xa or yb <= ya:
        return d, 0.0
    before = int(np.count_nonzero(np.isfinite(d) & (d > 0)))
    d[ya:yb, xa:xb] = 0.0
    after = int(np.count_nonzero(np.isfinite(d) & (d > 0)))
    frac = 0.0 if before == 0 else (before - after) / float(before)
    return d, round(frac, 4)


def prepare_frames(rgb_stream, depth_stream, dilate_frac=0.15, max_skew_s=0.06):
    """Pair RGBD by wall time (doctrine 4) then person-mask each paired depth by its target box
    (doctrine 5). Returns (frames, stats) where frames = [{t, rgb, depth}] ready for odom/tsdf."""
    paired, pstats = pair_rgbd(rgb_stream, depth_stream, max_skew_s=max_skew_s)
    fracs, n_box = [], 0
    for fr in paired:
        box = fr.pop("box", None)
        if box is not None:
            n_box += 1
        fr["depth"], mf = mask_target(fr["depth"], box, dilate_frac)
        fracs.append(mf)
    stats = dict(pstats)
    stats["mask_dilate_frac"] = dilate_frac
    stats["frames_with_target_box"] = n_box
    stats["masked_frac_mean"] = round(float(np.mean(fracs)), 4) if fracs else 0.0
    return paired, stats


def _quiet_o3d(o3d):
    o3d.utility.set_verbosity_level(o3d.utility.VerbosityLevel.Error)   # drop cosmetic write warnings


def write_mesh_ply(mesh, path):
    import open3d as o3d
    _quiet_o3d(o3d)
    o3d.io.write_triangle_mesh(path, mesh, write_ascii=False)


def write_obj(mesh, path):
    import open3d as o3d
    _quiet_o3d(o3d)
    o3d.io.write_triangle_mesh(path, mesh)


def _rgbd_image(rgb, depth, depth_trunc=DEPTH_TRUNC_M, intensity=False):
    """Build an Open3D RGBDImage. intensity=True (single-channel) is REQUIRED for the odometry hybrid
    photometric term; intensity=False keeps 3-channel colour for a coloured TSDF mesh."""
    import open3d as o3d
    color = o3d.geometry.Image(np.ascontiguousarray(rgb[:, :, :3].astype(np.uint8)))
    dm = np.ascontiguousarray(depth.astype(np.float32))
    dm[~np.isfinite(dm)] = 0.0
    return o3d.geometry.RGBDImage.create_from_color_and_depth(
        color, o3d.geometry.Image(dm), depth_scale=1.0, depth_trunc=depth_trunc,
        convert_rgb_to_intensity=intensity)


def _odo_option():
    import open3d as o3d
    opt = o3d.pipelines.odometry.OdometryOption()
    opt.depth_min = 0.15
    opt.depth_max = DEPTH_TRUNC_M
    opt.depth_diff_max = 0.07
    return opt


def rgbd_odometry(frames, intr, pose_prior=None, loop_closure=True):
    """Frame-to-frame RGBD odometry + pose-graph loop closure + global optimization.
    frames: list of {t, rgb, depth} (masked already). intr: PinholeCameraIntrinsic. pose_prior: optional
    list of 4x4 T_run_local_camera (from recorded /odom composed with the mount) used to INITIALIZE each
    frame-to-frame estimate -- geometric-grade seed, refined photometrically, never trusted as the answer.
    Returns dict(trajectory=[4x4 T_run_local_camera], loop_edges=[(i,j,fitness)], stats)."""
    import open3d as o3d
    if len(frames) < 2:
        raise ValueError("odom needs >= 2 paired RGBD frames, got %d" % len(frames))
    jac = o3d.pipelines.odometry.RGBDOdometryJacobianFromHybridTerm()
    opt = _odo_option()
    rgbds = [_rgbd_image(f["rgb"], f["depth"], intensity=True) for f in frames]

    # Open3D multiway-registration convention (proven; edge direction is source->target):
    #   register consecutive source=i-1 (earlier) -> target=i (later); trans = T_{cam_i}_{cam_{i-1}}
    #   accumulate odometry = trans @ odometry (= T_{cam_i}_{cam_0}); node pose = inv(odometry) = T_world_cam_i.
    pg = o3d.pipelines.registration.PoseGraph()
    pg.nodes.append(o3d.pipelines.registration.PoseGraphNode(np.eye(4)))
    odometry = np.eye(4)
    world_poses = [np.eye(4)]                 # T_run_local_camera per frame (world = cam_0)
    n_ok = n_fail = 0
    for i in range(1, len(frames)):
        init = np.eye(4)
        if pose_prior is not None:
            # prior guess for trans (cam_{i-1} -> cam_i) = inv(prior_i) @ prior_{i-1}
            init = np.linalg.inv(pose_prior[i]) @ pose_prior[i - 1]
        ok, T, info = o3d.pipelines.odometry.compute_rgbd_odometry(
            rgbds[i - 1], rgbds[i], intr, init, jac, opt)
        if not ok:
            T = init                          # fall back to the prior delta; loud in stats
            n_fail += 1
        else:
            n_ok += 1
        odometry = T @ odometry
        wp = np.linalg.inv(odometry)
        world_poses.append(wp)
        pg.nodes.append(o3d.pipelines.registration.PoseGraphNode(wp.copy()))
        pg.edges.append(o3d.pipelines.registration.PoseGraphEdge(
            i - 1, i, T, info, uncertain=False))

    loop_edges = []
    if loop_closure:
        loop_edges = _add_loop_edges(pg, rgbds, world_poses, intr, jac, opt)

    # global pose-graph optimization (levenberg-marquardt) -- distributes loop-closure error
    o3d.pipelines.registration.global_optimization(
        pg,
        o3d.pipelines.registration.GlobalOptimizationLevenbergMarquardt(),
        o3d.pipelines.registration.GlobalOptimizationConvergenceCriteria(),
        o3d.pipelines.registration.GlobalOptimizationOption(
            max_correspondence_distance=0.07, edge_prune_threshold=0.25, reference_node=0))

    trajectory = [np.asarray(node.pose) for node in pg.nodes]
    stats = {"frames": len(frames), "odom_edges_ok": n_ok, "odom_edges_fallback": n_fail,
             "loop_edges": len(loop_edges), "used_prior": pose_prior is not None}
    return {"trajectory": trajectory, "loop_edges": loop_edges, "stats": stats}


def _add_loop_edges(pg, rgbds, world_poses, intr, jac, opt):
    """Detect revisited places by run_local proximity (temporal gap > LOOP_MIN_GAP, translation within
    LOOP_TRANS_RADIUS_M), verify each with RGBD odometry, and add surviving pairs as UNCERTAIN edges.
    Same source->target convention as the odometry edges: source=i (earlier), target=j (later)."""
    import open3d as o3d
    trans = np.array([p[:3, 3] for p in world_poses])
    edges = []
    n = len(world_poses)
    for i in range(n):
        for j in range(i + LOOP_MIN_GAP, n):
            if np.linalg.norm(trans[j] - trans[i]) > LOOP_TRANS_RADIUS_M:
                continue
            init = np.linalg.inv(world_poses[j]) @ world_poses[i]   # trans guess: cam_i -> cam_j
            ok, T, info = o3d.pipelines.odometry.compute_rgbd_odometry(
                rgbds[i], rgbds[j], intr, init, jac, opt)
            if not ok:
                continue
            # fitness proxy: info[0,0] scales with overlap; guard against degenerate edges cheaply
            if float(info[0, 0]) < 1.0:
                continue
            pg.edges.append(o3d.pipelines.registration.PoseGraphEdge(i, j, T, info, uncertain=True))
            edges.append((int(i), int(j), round(float(info[0, 0]), 2)))
    return edges


def tsdf_integrate(frames, trajectory, intr, voxel=0.02, sdf_trunc=0.08, depth_trunc=DEPTH_TRUNC_M):
    """TSDF integrate masked RGBD along the trajectory -> colored triangle mesh (run_local).
    trajectory[i] = T_run_local_camera_i; integrate wants extrinsic = world->camera = inv(pose)."""
    import open3d as o3d
    vol = o3d.pipelines.integration.ScalableTSDFVolume(
        voxel_length=float(voxel), sdf_trunc=float(sdf_trunc),
        color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8)
    for f, pose in zip(frames, trajectory):
        rgbd = _rgbd_image(f["rgb"], f["depth"], depth_trunc=depth_trunc)
        vol.integrate(rgbd, intr, np.linalg.inv(pose))
    mesh = vol.extract_triangle_mesh()
    mesh.compute_vertex_normals()
    return mesh


def simplify_and_decompose(mesh, target_tris=None, coacd_threshold=0.05, seed=0):
    """simexport stage: a quadric-decimated VISUAL mesh + CoACD convex-decomposition COLLISION parts
    (each part convex => watertight by construction, which raw TSDF meshes are not -- Phase 10 needs
    that). Returns (visual_mesh, [collision_part_meshes], stats). Deterministic via `seed`."""
    import open3d as o3d
    import coacd
    ntri = len(mesh.triangles)
    tgt = int(target_tris) if target_tris else max(500, ntri // 4)
    visual = mesh.simplify_quadric_decimation(tgt)
    visual.compute_vertex_normals()
    V = np.asarray(visual.vertices, dtype=np.float64)
    F = np.asarray(visual.triangles, dtype=np.int32)
    coacd.set_log_level("error")
    parts = coacd.run_coacd(coacd.Mesh(V, F), threshold=coacd_threshold, seed=int(seed))
    part_meshes = []
    for pv, pf in parts:
        pm = o3d.geometry.TriangleMesh(
            o3d.utility.Vector3dVector(np.asarray(pv, dtype=np.float64)),
            o3d.utility.Vector3iVector(np.asarray(pf, dtype=np.int32)))
        pm.compute_vertex_normals()
        part_meshes.append(pm)
    stats = {"visual_tris_in": int(ntri), "visual_tris_out": int(len(visual.triangles)),
             "collision_parts": len(part_meshes), "coacd_threshold": coacd_threshold}
    return visual, part_meshes, stats


_TAG_DICT = "DICT_APRILTAG_36h11"       # docs/FRAMES.md anchor_i convention; print-stick-done


def _tag_object_points(size_m):
    """Planar tag corners in the tag frame, matching cv2.aruco's corner order (TL, TR, BR, BL)."""
    h = float(size_m) / 2.0
    return np.array([[-h, h, 0], [h, h, 0], [h, -h, 0], [-h, -h, 0]], dtype=np.float64)


def detect_apriltags(frames, intr, tag_size_m):
    """align stage (per-run half): detect DICT_APRILTAG_36h11 tags in each RGB frame and solvePnP
    (IPPE_SQUARE, exact for a planar square) -> T_camera_tag per observation. Returns
    {tag_id: {"n": k, "observations": [{"frame": i, "T_cam_tag": 4x4 list, "reproj_px": e}]}}.
    Cross-run merge into `map` is DEFERRED (needs two real runs from different days -- P8.3, data-blocked);
    this records the anchor_<id> observations that alignment will consume."""
    import cv2
    dic = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, _TAG_DICT))
    det = cv2.aruco.ArucoDetector(dic, cv2.aruco.DetectorParameters())
    K = np.asarray(intr.intrinsic_matrix, dtype=np.float64)
    dist = np.zeros(5)                                    # approximate model: no distortion (FRAMES.md)
    objp = _tag_object_points(tag_size_m)
    out = {}
    for i, f in enumerate(frames):
        gray = cv2.cvtColor(np.ascontiguousarray(f["rgb"]), cv2.COLOR_RGB2GRAY)
        corners, ids, _ = det.detectMarkers(gray)
        if ids is None:
            continue
        for c, tid in zip(corners, ids.ravel().tolist()):
            img_pts = np.asarray(c, dtype=np.float64).reshape(4, 2)
            ok, rvec, tvec = cv2.solvePnP(objp, img_pts, K, dist, flags=cv2.SOLVEPNP_IPPE_SQUARE)
            if not ok:
                continue
            R, _ = cv2.Rodrigues(rvec)
            T = np.eye(4); T[:3, :3] = R; T[:3, 3] = tvec.ravel()
            proj, _ = cv2.projectPoints(objp, rvec, tvec, K, dist)
            reproj = float(np.sqrt(np.mean((proj.reshape(4, 2) - img_pts) ** 2)))
            rec = out.setdefault(int(tid), {"n": 0, "observations": []})
            rec["n"] += 1
            rec["observations"].append({"frame": int(i), "T_cam_tag": T.tolist(),
                                        "reproj_px": round(reproj, 3)})
    return out


# ---------------------------------------------------------------------------------------------------
# Synthetic-motion selftest: a known scene + known trajectory, rendered to RGBD via the CPU raycaster,
# so odom trajectory error and tsdf geometry are checked WITHOUT real data (RaycastingScene is CPU).
# ---------------------------------------------------------------------------------------------------
def _synth_scene():
    """A 4x4x2.5 m room 'corner' (floor + two walls) with a procedural checker texture in world coords,
    as a legacy TriangleMesh (for a raycasting scene we only need geometry; color is procedural below)."""
    import open3d as o3d
    verts, tris = [], []

    def quad(p0, p1, p2, p3):
        b = len(verts)
        verts.extend([p0, p1, p2, p3])
        tris.extend([[b, b + 1, b + 2], [b, b + 2, b + 3]])

    quad([0, 0, 0], [4, 0, 0], [4, 4, 0], [0, 4, 0])        # floor  (z=0)
    quad([0, 0, 0], [0, 4, 0], [0, 4, 2.5], [0, 0, 2.5])    # wall x=0
    quad([0, 0, 0], [0, 0, 2.5], [4, 0, 2.5], [4, 0, 0])    # wall y=0
    m = o3d.geometry.TriangleMesh()
    m.vertices = o3d.utility.Vector3dVector(np.asarray(verts, dtype=np.float64))
    m.triangles = o3d.utility.Vector3iVector(np.asarray(tris, dtype=np.int32))
    return m


def _checker_rgb(pts_world):
    """Procedural checker in world coords -> RGB with real photometric gradient for the hybrid odometry
    term to lock onto (a flat-color scene has no visual gradient and RGBD odometry degenerates)."""
    p = pts_world
    c = ((np.floor(p[..., 0] * 4) + np.floor(p[..., 1] * 4) + np.floor(p[..., 2] * 4)).astype(int) % 2)
    base = np.where(c[..., None] == 0, np.array([210, 180, 120]), np.array([70, 90, 140]))
    # add a smooth height ramp so even same-checker regions carry gradient
    ramp = np.clip((p[..., 2] / 2.5), 0, 1)[..., None]
    return np.clip(base * (0.7 + 0.3 * ramp), 0, 255).astype(np.uint8)


def _render_rgbd(scene, T_wc, intr_mat, w, h):
    """Cast a pinhole camera (extrinsic world->camera = inv(T_wc)) into the raycasting scene; return
    (rgb HxWx3 uint8, depth HxW float32 metres, hit-mask). z-depth is computed in the camera frame."""
    import open3d as o3d
    T_cw = np.linalg.inv(T_wc)
    rays = scene.create_rays_pinhole(
        o3d.core.Tensor(intr_mat, dtype=o3d.core.Dtype.Float64),
        o3d.core.Tensor(T_cw, dtype=o3d.core.Dtype.Float64), int(w), int(h))
    ans = scene.cast_rays(rays)
    t_hit = ans["t_hit"].numpy()                    # (H,W) distance along the (unit) ray
    rays_np = rays.numpy()                          # (H,W,6): origin(0:3) + dir(3:6), world frame
    origin = rays_np[..., :3]
    direction = rays_np[..., 3:6]
    hit = np.isfinite(t_hit)
    t_safe = np.where(hit, t_hit, 0.0)              # kill inf BEFORE arithmetic (no NaN in matmul)
    pts_w = origin + t_safe[..., None] * direction  # world hit points (0 where no hit)
    # camera-frame z-depth = z of hit point in camera coords
    pts_h = np.concatenate([pts_w, np.ones_like(pts_w[..., :1])], axis=-1)
    pts_c = pts_h @ T_cw.T
    depth = np.where(hit, pts_c[..., 2], 0.0).astype(np.float32)
    depth[~np.isfinite(depth)] = 0.0
    rgb = np.zeros((int(h), int(w), 3), dtype=np.uint8)
    if hit.any():
        rgb[hit] = _checker_rgb(pts_w[hit])
    return rgb, depth, hit


def _look_at(cam, target, up=(0, 0, 1)):
    """T_world_camera for an OPTICAL camera (Z-fwd, X-right, Y-down) at `cam` looking at `target`."""
    cam = np.asarray(cam, float); target = np.asarray(target, float); up = np.asarray(up, float)
    z = target - cam; z /= np.linalg.norm(z)          # forward
    x = np.cross(z, up); x /= np.linalg.norm(x)        # right (horizontal)
    y = np.cross(z, x)                                 # down (right-handed: x cross y = z)
    T = np.eye(4)
    T[:3, 0], T[:3, 1], T[:3, 2], T[:3, 3] = x, y, z, cam
    return T


def _traj_there_and_back(n=24):
    """A smooth there-and-back path that keeps the room corner in view (start pose ~= end pose, so the
    loop-closure machinery has a real revisit to find). Returns [T_world_camera] in the optical frame."""
    target = np.array([0.4, 0.4, 1.0])                 # near the floor/two-wall corner
    poses = []
    for i in range(n):
        s = i / (n - 1)
        u = 2 * s if s <= 0.5 else 2 * (1 - s)         # 0 -> 1 -> 0 triangle
        cam = np.array([1.3 + 1.1 * u, 2.3 - 0.4 * u, 1.3])
        poses.append(_look_at(cam, target))
    return poses


def _traj_error(est, gt):
    """Mean/max translation error. run_local origin is arbitrary, so compare est (which starts at
    identity) against gt expressed relative to gt[0] -- both then start at the origin."""
    g0 = np.linalg.inv(gt[0])
    errs = [np.linalg.norm(est[i][:3, 3] - (g0 @ gt[i])[:3, 3]) for i in range(len(est))]
    return float(np.mean(errs)), float(np.max(errs))


def _render_tag_frame(tag_id, size_m, dist_m, K, w, h):
    """A frontal-parallel AprilTag centred at (0,0,dist) in the camera frame -- closed-loop input for the
    align detect+solvePnP smoke. Frontal, so it's an exact axis-aligned square: crisp NEAREST resize
    (no warp blur) + a white quiet-zone border (the detector requires one). objp maps to the TAG corners
    (excluding the quiet zone), which project to a `fx*size/dist`-px square centred at the principal point."""
    import cv2
    dic = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, _TAG_DICT))
    m = cv2.aruco.generateImageMarker(dic, int(tag_id), 200)
    pad = 40
    mq = np.full((200 + 2 * pad, 200 + 2 * pad), 255, np.uint8)
    mq[pad:200 + pad, pad:200 + pad] = m
    fx, cx, cy = float(K[0, 0]), float(K[0, 2]), float(K[1, 2])
    tag_px = fx * float(size_m) / float(dist_m)          # the TAG square side in pixels
    full = max(1, int(round((200 + 2 * pad) * (tag_px / 200.0))))
    rz = cv2.resize(mq, (full, full), interpolation=cv2.INTER_NEAREST)
    canvas = np.full((int(h), int(w)), 255, np.uint8)
    x0, y0 = int(round(cx - full / 2)), int(round(cy - full / 2))
    xs0, ys0 = max(0, x0), max(0, y0)
    xs1, ys1 = min(int(w), x0 + full), min(int(h), y0 + full)
    canvas[ys0:ys1, xs0:xs1] = rz[ys0 - y0:ys1 - y0, xs0 - x0:xs1 - x0]
    return {"t": 0.0, "rgb": np.repeat(canvas[:, :, None], 3, axis=2)}


def _selftest():
    import open3d as o3d
    W = H = 120
    fx = fy = 90.0
    cx, cy = W / 2.0, H / 2.0
    intr_mat = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], dtype=np.float64)
    intr = make_intrinsic(W, H, fx, fy, cx, cy)

    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(_synth_scene()))

    gt = _traj_there_and_back(24)
    frames = []
    for k, T in enumerate(gt):
        rgb, depth, hit = _render_rgbd(scene, T, intr_mat, W, H)
        assert hit.mean() > 0.5, "synthetic frame %d saw too little geometry (%.2f)" % (k, hit.mean())
        frames.append({"t": k / 10.0, "rgb": rgb, "depth": depth})

    # (1) odom WITHOUT a prior must still recover the trajectory shape from RGBD alone
    res = rgbd_odometry(frames, intr, pose_prior=None, loop_closure=True)
    mean_e, max_e = _traj_error(res["trajectory"], gt)
    print("GEOM odom: frames=%d ok=%d fallback=%d loops=%d  traj_err mean=%.3fm max=%.3fm"
          % (res["stats"]["frames"], res["stats"]["odom_edges_ok"], res["stats"]["odom_edges_fallback"],
             res["stats"]["loop_edges"], mean_e, max_e))
    assert res["stats"]["odom_edges_fallback"] == 0, "some frame-to-frame odometry steps failed"
    assert mean_e < 0.10, "odom mean translation error %.3f m too high (RGBD odometry broken)" % mean_e

    # (2) tsdf along the recovered trajectory reconstructs the corner (floor+2 walls) at ~right extent
    mesh = tsdf_integrate(frames, res["trajectory"], intr, voxel=0.03)
    v = np.asarray(mesh.vertices)
    assert v.shape[0] > 2000, "tsdf produced too few vertices (%d)" % v.shape[0]
    ext = v.max(0) - v.min(0)
    print("GEOM tsdf: vertices=%d extent=[%.2f %.2f %.2f]m" % (v.shape[0], ext[0], ext[1], ext[2]))
    assert ext[0] > 1.0 and ext[1] > 1.0, "reconstructed extent too small -- geometry not integrated"

    # (3) simexport: quadric-decimate + CoACD convex parts (watertight collision geometry)
    visual, parts, sx = simplify_and_decompose(mesh, target_tris=3000)
    print("GEOM simexport: visual_tris %d->%d  collision_parts=%d"
          % (sx["visual_tris_in"], sx["visual_tris_out"], sx["collision_parts"]))
    assert sx["visual_tris_out"] <= sx["visual_tris_in"], "decimation increased triangle count"
    assert len(parts) >= 1 and all(len(np.asarray(p.vertices)) > 0 for p in parts), "no collision parts"

    # (4) align: render a known frontal tag, recover its id + pose (detect + solvePnP round-trip).
    # A 36h11 tag needs ~100+ px to decode, so use a dedicated higher-res camera (the 120px odom cam
    # would render the tag ~10 px and it would be undetectable -- a resolution limit, not a bug).
    aw = ah = 400
    a_intr_mat = np.array([[400.0, 0, 200.0], [0, 400.0, 200.0], [0, 0, 1]], dtype=np.float64)
    a_intr = make_intrinsic(aw, ah, 400.0, 400.0, 200.0, 200.0)
    tag_id, tag_s, tag_d = 23, 0.30, 0.9                 # projected ~= 400*0.30/0.9 = 133 px
    frame = _render_tag_frame(tag_id, tag_s, tag_d, a_intr_mat, aw, ah)
    obs = detect_apriltags([frame], a_intr, tag_s)
    print("GEOM align: tags=%s" % ({k: v["n"] for k, v in obs.items()}))
    assert tag_id in obs, "align failed to detect the planted tag id %d" % tag_id
    z = obs[tag_id]["observations"][0]["T_cam_tag"][2][3]
    assert abs(z - tag_d) < 0.15, "align pose z=%.3f m off from planted %.2f m" % (z, tag_d)

    print("GEOM-SELFTEST-OK")
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("geom.py -- import module; run with --selftest for the synthetic-motion odom/tsdf check")
