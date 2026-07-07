import os, json, glob

SDK = "/home/booster/Workspace/booster_robotics_sdk"
HOME = "/home/booster"
OUT_TREE = "/home/booster/k1_tree.txt"
OUT_JSON = "/home/booster/k1_paths.json"
OUT_LIST = "/home/booster/k1_paths_list.txt"  # flat absolute paths, for the app TreeView

lines = []
def emit(s=""):
    lines.append(s)

def tree(root, maxdepth=4, prune=(), only_dirs_at=99, file_filter=None, label=None):
    base_depth = root.rstrip("/").count("/")
    emit((label or root))
    for dp, dn, fns in os.walk(root):
        depth = dp.rstrip("/").count("/") - base_depth
        # prune unwanted dirs
        dn[:] = sorted([d for d in dn if not any(p in (os.path.join(dp, d)) for p in prune)
                        and d not in (".git", "__pycache__")])
        if depth >= maxdepth:
            dn[:] = []
        rel = dp[len(root):].lstrip("/")
        indent = "  " * (depth)
        if rel:
            emit("%s%s/" % (indent, os.path.basename(dp)))
        show_files = True
        if depth >= only_dirs_at:
            show_files = False
        if show_files:
            files = sorted(fns)
            if file_filter:
                files = [f for f in files if file_filter(f)]
            for f in files:
                emit("%s  %s" % (indent, f))

emit("=" * 70)
emit("BOOSTER K1 - ROBOT FILE TREE  (generated on-robot)")
emit("=" * 70)

emit("\n##### /home/booster (top level) #####")
for name in sorted(os.listdir(HOME)):
    full = os.path.join(HOME, name)
    tag = "/" if os.path.isdir(full) else ""
    emit("  %s%s" % (name, tag))

emit("\n##### Workspace (top level) #####")
ws = os.path.join(HOME, "Workspace")
if os.path.isdir(ws):
    for name in sorted(os.listdir(ws)):
        emit("  %s/" % name)

emit("\n##### booster_robotics_sdk / include  (API headers - where things go) #####")
tree(SDK + "/include", maxdepth=8, label="include/")

emit("\n##### booster_robotics_sdk / example #####")
tree(SDK + "/example", maxdepth=4, label="example/")

emit("\n##### booster_robotics_sdk / python #####")
tree(SDK + "/python", maxdepth=3, label="python/")

emit("\n##### booster_robotics_sdk / lib #####")
tree(SDK + "/lib", maxdepth=3, label="lib/")

emit("\n##### booster_robotics_sdk / build  (binaries only) #####")
bd = SDK + "/build"
if os.path.isdir(bd):
    for name in sorted(os.listdir(bd)):
        full = os.path.join(bd, name)
        if os.path.isfile(full) and ("." not in name or name.endswith(".so")):
            x = " (exe)" if os.access(full, os.X_OK) and "." not in name else ""
            emit("  %s%s" % (name, x))

emit("\n##### /opt/booster (ROS install packages, depth 2) #####")
ob = "/opt/booster"
if os.path.isdir(ob):
    for name in sorted(os.listdir(ob)):
        emit("  %s/" % name)
        inst = os.path.join(ob, name, "install")
        if os.path.isdir(inst):
            for pkg in sorted(os.listdir(inst))[:60]:
                emit("    install/%s" % pkg)

# ---- key paths manifest ----
def first(globpat):
    g = sorted(glob.glob(globpat))
    return g[0] if g else ""

manifest = {
    "sdk_root": SDK,
    "include_dir": SDK + "/include",
    "example_high_level": SDK + "/example/high_level",
    "example_low_level": SDK + "/example/low_level",
    "build_dir": SDK + "/build",
    "loco_client_bin": SDK + "/build/b1_loco_example_client",
    "loco_client_src": SDK + "/example/high_level/b1_loco_example_client.cpp",
    "loco_client_py": SDK + "/example/high_level/b1_loco_example_client.py",
    "lib_static": first(SDK + "/lib/*/libbooster_robotics_sdk.a"),
    "python_module_so": first(SDK + "/build/booster_robotics_sdk_python*.so"),
    "b1_loco_client_hpp": SDK + "/include/booster/robot/b1/b1_loco_client.hpp",
    "b1_loco_api_hpp": SDK + "/include/booster/robot/b1/b1_loco_api.hpp",
    "x5_camera_client_hpp": SDK + "/include/booster/robot/x5_camera/x5_camera_client.hpp",
    "x5_camera_api_const_hpp": SDK + "/include/booster/robot/x5_camera/x5_camera_api_const.hpp",
    "channel_factory_hpp": SDK + "/include/booster/robot/channel/channel_factory.hpp",
    "ros_setup": "/opt/ros/humble/setup.bash",
    "booster_ros_setup": "/opt/booster/BoosterRos2/install/setup.bash",
    "home": HOME,
    "app_stream_cam": HOME + "/stream_cam.py",
    "app_run_stream": HOME + "/run_stream.sh",
    "app_run_loco": HOME + "/run_loco.sh",
    "app_enable_camera": HOME + "/enable_camera",
    "camera_topic_head_rgb": "/boostercamera/head/rgb",
}
manifest_exists = {k: (os.path.exists(v) if v and v.startswith("/") else False) for k, v in manifest.items()}

emit("\n##### KEY PATHS (exists?) #####")
for k in manifest:
    v = manifest[k]
    emit("  [%s] %-26s %s" % ("x" if manifest_exists.get(k) else " ", k, v))

# ---- flat absolute-path list (for the app TreeView): "DIR|path" / "FILE|path" ----
flat = []
def add_flat(root, maxdepth, prune=()):
    base = root.rstrip("/").count("/")
    if os.path.isdir(root):
        flat.append("DIR|" + root)
    for dp, dn, fns in os.walk(root):
        depth = dp.rstrip("/").count("/") - base
        dn[:] = sorted([d for d in dn if d not in (".git", "__pycache__")
                        and not any(p in os.path.join(dp, d) for p in prune)])
        if depth >= maxdepth:
            dn[:] = []
        for d in dn:
            flat.append("DIR|" + os.path.join(dp, d))
        for f in sorted(fns):
            flat.append("FILE|" + os.path.join(dp, f))

# SDK: include (deep), example, python, lib, build(top-level files only)
add_flat(SDK + "/include", 9)
add_flat(SDK + "/example", 4)
add_flat(SDK + "/python", 3)
add_flat(SDK + "/lib", 3)
flat.append("DIR|" + SDK + "/build")
bd = SDK + "/build"
if os.path.isdir(bd):
    for name in sorted(os.listdir(bd)):
        full = os.path.join(bd, name)
        if os.path.isfile(full) and ("." not in name or name.endswith(".so")):
            flat.append("FILE|" + full)
# /home/booster top-level (one level)
flat.append("DIR|" + HOME)
for name in sorted(os.listdir(HOME)):
    full = os.path.join(HOME, name)
    flat.append(("DIR|" if os.path.isdir(full) else "FILE|") + full)

open(OUT_LIST, "w").write("\n".join(flat))
open(OUT_TREE, "w").write("\n".join(lines))
json.dump({"paths": manifest, "exists": manifest_exists}, open(OUT_JSON, "w"), indent=2)
print("wrote", OUT_TREE, ",", OUT_JSON, ",", OUT_LIST)
print("tree lines:", len(lines), " flat paths:", len(flat))
