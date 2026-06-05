"""
ComfyUI Gallery Manager v5 — bundled with comfyui-anime-bootstrap.

Drop-in custom node that registers HTTP routes on ComfyUI's aiohttp server
once the module is imported at startup:

    /gallery/                       - Gallery UI page
    /gallery                        - 308 redirect to /gallery/  (no-slash fix)
    /gallery/api/list               - List files + folders (?subfolder=, ?show_hidden=1)
    /gallery/api/thumb/{name}       - ffmpeg JPEG thumbnail for video files
    /gallery/api/metadata           - Extract PNG workflow metadata
    /gallery/api/index              - Rebuild JSONL metadata database
    /gallery/api/search             - Search indexed metadata
    /gallery/api/mkdir              - Create folder
    /gallery/api/delete             - Delete files/folders
    /gallery/api/rename             - Rename file/folder

Features
--------
- Path-traversal guard (safe_join)
- Dot-file filtering: hidden by default, toggle with ?show_hidden=1
- Folder browsing with recursive thumbnail discovery
- Placeholder filtering (0-byte files)
- Video support: MP4/WebM/MOV/AVI/MKV detection + ffmpeg thumbnail generation
- PNG metadata extraction: model, LoRA, seed, steps, CFG, sampler,
  scheduler, prompts (traced via KSampler connections)
- JSONL sidecar database (.metadata.jsonl)

OUTPUT_DIR resolution
---------------------
Tries the standard ComfyUI output locations in order:
    1. /opt/ComfyUI/output          (this image's install path)
    2. /workspace/ComfyUI/output    (legacy / older images)
    3. $COMFYUI_DIR/output          (env override)

Restart requirement
-------------------
Route registrations are not hot-reloaded. After modifying this file, restart
ComfyUI and `rm -rf custom_nodes/ComfyUI_GalleryManager/__pycache__/` to
avoid stale bytecode serving the old routes.
"""
import os
import json
import subprocess
import server  # ComfyUI's aiohttp server module
from aiohttp import web

# WEB_DIRECTORY tells ComfyUI to serve files under ./web at
# /extensions/ComfyUI_GalleryManager/. We do NOT use that — we register
# our own /gallery route so the page is at a stable URL regardless of
# ComfyUI's extension-static-file handling. (The extension path would put
# the page at /extensions/ComfyUI_GalleryManager/web/index.html which is
# not what users expect.)
WEB_DIRECTORY = "./web"


def _resolve_output_dir():
    """Pick the first ComfyUI output dir that exists, in priority order."""
    comfyui_dir = os.environ.get("COMFYUI_DIR", "")
    candidates = [
        "/opt/ComfyUI/output",                       # this image
        "/workspace/ComfyUI/output",                 # older /opt-less installs
        os.path.join(comfyui_dir, "output") if comfyui_dir else "",
    ]
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    # Fall back to the first candidate even if it doesn't exist — ComfyUI
    # creates the output dir on first save.
    return candidates[0]


OUTPUT_DIR = _resolve_output_dir()
DB_PATH = os.path.join(OUTPUT_DIR, ".metadata.jsonl")
THUMB_DIR = os.path.join(OUTPUT_DIR, ".thumbs")


# ── Helpers ──────────────────────────────────────────────────────────────────

def is_image(name):
    return name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"))


def is_video(name):
    return name.lower().endswith((".mp4", ".webm", ".mov", ".avi", ".mkv"))


def ensure_thumb(video_path, name):
    """Generate a JPEG thumbnail for a video using ffmpeg. Returns thumb path or None."""
    thumb_path = os.path.join(THUMB_DIR, name + ".jpg")
    if os.path.exists(thumb_path):
        return thumb_path
    os.makedirs(THUMB_DIR, exist_ok=True)
    for ts in ["00:00:01", "00:00:00.500"]:
        cmd = [
            "ffmpeg", "-y", "-i", video_path,
            "-ss", ts, "-vframes", "1",
            "-q:v", "2", thumb_path,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=30)
            if os.path.exists(thumb_path):
                return thumb_path
        except Exception:
            continue
    return None


def get_entries(directory, rel_subfolder="", show_hidden=False):
    """List files and folders in a directory. Skip dot-files unless show_hidden."""
    items = []
    try:
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            is_dir = os.path.isdir(path)
            if name.startswith(".") and not show_hidden:
                continue
            if os.path.isfile(path) and os.path.getsize(path) == 0:
                continue
            if name == "_output_images_will_be_put_here":
                continue
            item = {"name": name, "is_dir": is_dir, "mtime": os.path.getmtime(path)}
            if is_dir:
                thumb = None
                for root, _, files in os.walk(path):
                    for f in sorted(files):
                        if is_image(f) and os.path.getsize(os.path.join(root, f)) > 0:
                            inner_rel = os.path.relpath(
                                os.path.join(root, f), OUTPUT_DIR
                            ).replace("\\", "/")
                            parts = inner_rel.split("/")
                            fn = parts[-1]
                            sf = "/".join(parts[:-1]) if len(parts) > 1 else ""
                            thumb = f"/view?filename={fn}&type=output&subfolder={sf}"
                            break
                    if thumb:
                        break
                item["thumbnail_url"] = thumb
                item["size"] = 0
            else:
                item["size"] = os.path.getsize(path)
                sf = rel_subfolder or ""
                item["is_video"] = is_video(name)
                item["url"] = f"/view?filename={name}&type=output&subfolder={sf}"
                if item["is_video"]:
                    item["thumbnail_url"] = f"/gallery/api/thumb/{name}?subfolder={sf}"
            items.append(item)
    except Exception:
        pass
    return items


def safe_join(subfolder):
    """Resolve subfolder path, rejecting path-traversal attempts."""
    target = os.path.abspath(os.path.join(OUTPUT_DIR, subfolder))
    if not target.startswith(os.path.abspath(OUTPUT_DIR)):
        return None
    return target


def get_text_from_node(wf, node_id):
    """Return text input from a CLIPTextEncode node by ID."""
    node = wf.get(str(node_id))
    if not node:
        return None
    return node.get("inputs", {}).get("text", None)


def extract_metadata(filepath, rel_path=""):
    """Extract ComfyUI workflow metadata from a PNG file."""
    try:
        from PIL import Image
        img = Image.open(filepath)
        raw_prompt = img.info.get("prompt", "")
        raw_workflow = img.info.get("workflow", "")
        metadata = {
            "filename": os.path.basename(filepath),
            "rel_path": rel_path,
            "timestamp": os.path.getmtime(filepath),
            "size_bytes": os.path.getsize(filepath),
            "width": img.width,
            "height": img.height,
            "prompt_json": raw_prompt,
            "workflow_json": raw_workflow,
        }
        if raw_prompt:
            try:
                wf = json.loads(raw_prompt)
                pos_node_id = None
                neg_node_id = None
                for nid, node in wf.items():
                    cls = node.get("class_type", "")
                    if cls == "KSampler":
                        inputs = node.get("inputs", {})
                        metadata["seed"] = inputs.get("seed")
                        metadata["steps"] = inputs.get("steps")
                        metadata["cfg"] = inputs.get("cfg")
                        metadata["sampler"] = inputs.get("sampler_name")
                        metadata["scheduler"] = inputs.get("scheduler")
                        pos_conn = inputs.get("positive")
                        neg_conn = inputs.get("negative")
                        if isinstance(pos_conn, list) and len(pos_conn) >= 2:
                            pos_node_id = str(pos_conn[0])
                        if isinstance(neg_conn, list) and len(neg_conn) >= 2:
                            neg_node_id = str(neg_conn[0])
                    elif cls == "CheckpointLoaderSimple":
                        metadata["model"] = node.get("inputs", {}).get("ckpt_name")
                    elif cls == "LoraLoader":
                        metadata["lora"] = node.get("inputs", {}).get("lora_name")
                        metadata["lora_strength"] = node.get("inputs", {}).get("strength_model")
                    elif cls == "EmptyLatentImage":
                        metadata["width"] = node.get("inputs", {}).get("width", metadata.get("width"))
                        metadata["height"] = node.get("inputs", {}).get("height", metadata.get("height"))
                if pos_node_id:
                    metadata["positive_prompt"] = get_text_from_node(wf, pos_node_id)
                if neg_node_id:
                    metadata["negative_prompt"] = get_text_from_node(wf, neg_node_id)
            except json.JSONDecodeError:
                pass
        return metadata
    except Exception as e:
        return {"filename": os.path.basename(filepath), "rel_path": rel_path, "error": str(e)}


def load_db():
    """Load metadata database as list of dicts."""
    if not os.path.exists(DB_PATH):
        return []
    entries = []
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    except Exception:
        pass
    return entries


def append_db(record):
    """Append a single record to the JSONL database."""
    try:
        with open(DB_PATH, "a", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False)
            f.write("\n")
    except Exception:
        pass


def index_directory(directory, rel=""):
    """Recursively index all PNGs and write to DB."""
    indexed = 0
    for root, _, files in os.walk(directory):
        local_rel = os.path.relpath(root, directory)
        if local_rel == ".":
            local_rel = ""
        sub_rel = (rel + "/" + local_rel).strip("/") if rel else local_rel
        for f in sorted(files):
            if is_image(f):
                fp = os.path.join(root, f)
                rel_path = (sub_rel + "/" + f).strip("/") if sub_rel else f
                meta = extract_metadata(fp, rel_path)
                append_db(meta)
                indexed += 1
    return indexed


# ── HTTP Routes ──────────────────────────────────────────────────────────────

# 1) Gallery UI page (with trailing slash — required for the catch-all pattern)
@server.PromptServer.instance.routes.get("/gallery/")
async def gallery_page(request):
    html_path = os.path.join(os.path.dirname(__file__), "web", "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html")
    return web.Response(text="Gallery UI not found", status=404)


# 2) No-slash redirect: /gallery → /gallery/
# CRITICAL: without this, browsers typing /gallery (no trailing slash) hit a
# 404. The skill docs (SKILL.md §6.11) flag this as a top-3 pod-deploy bug.
@server.PromptServer.instance.routes.get("/gallery")
async def gallery_redirect(request):
    return web.HTTPFound("/gallery/")


# 3) API: list files + folders
@server.PromptServer.instance.routes.get("/gallery/api/list")
async def gallery_list(request):
    sub = request.query.get("subfolder", "")
    show_hidden = request.query.get("show_hidden", "") == "1"
    target = safe_join(sub)
    if target is None:
        return web.json_response({"error": "invalid path"}, status=400)
    entries = get_entries(target, sub, show_hidden)
    files = [e for e in entries if not e.get("is_dir")]
    folders = [e for e in entries if e.get("is_dir")]
    return web.json_response({"files": files, "folders": folders, "current_path": sub})


# 4) API: video thumbnail via ffmpeg
@server.PromptServer.instance.routes.get("/gallery/api/thumb/{name}")
async def gallery_thumb(request):
    name = request.match_info["name"]
    sub = request.query.get("subfolder", "")
    target = safe_join(os.path.join(sub, name))
    if target is None or not os.path.isfile(target) or not is_video(name):
        return web.json_response({"error": "not found"}, status=404)
    thumb = ensure_thumb(target, name)
    if not thumb:
        return web.json_response({"error": "thumbnail failed"}, status=500)
    return web.FileResponse(thumb)


# 5) API: PNG metadata
@server.PromptServer.instance.routes.get("/gallery/api/metadata")
async def gallery_metadata(request):
    sub = request.query.get("subfolder", "")
    name = request.query.get("name", "")
    if not name:
        return web.json_response({"error": "name required"}, status=400)
    target = safe_join(os.path.join(sub, name))
    if target is None or not os.path.isfile(target):
        return web.json_response({"error": "file not found"}, status=404)
    meta = extract_metadata(target, (sub + "/" + name).strip("/") if sub else name)
    return web.json_response(meta)


# 6) API: rebuild JSONL index
@server.PromptServer.instance.routes.post("/gallery/api/index")
async def gallery_index(request):
    try:
        with open(DB_PATH, "w", encoding="utf-8") as f:
            f.write("")
        count = index_directory(OUTPUT_DIR)
        return web.json_response({"ok": True, "indexed": count})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# 7) API: search indexed metadata
@server.PromptServer.instance.routes.get("/gallery/api/search")
async def gallery_search(request):
    query = request.query.get("q", "").lower()
    if not query:
        return web.json_response({"results": []})
    entries = load_db()
    results = []
    for e in entries:
        haystack = " ".join(
            str(v) for v in e.values() if isinstance(v, (str, int, float))
        ).lower()
        if query in haystack:
            results.append(e)
    return web.json_response({"results": results[:100]})


# 8) API: mkdir
@server.PromptServer.instance.routes.post("/gallery/api/mkdir")
async def gallery_mkdir(request):
    try:
        data = await request.json()
        sub = data.get("subfolder", "")
        name = data.get("name", "").strip()
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return web.json_response({"error": "invalid folder name"}, status=400)
        target = safe_join(os.path.join(sub, name))
        if target is None:
            return web.json_response({"error": "invalid path"}, status=400)
        os.makedirs(target, exist_ok=True)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# 9) API: delete files/folders
@server.PromptServer.instance.routes.post("/gallery/api/delete")
async def gallery_delete(request):
    try:
        data = await request.json()
        sub = data.get("subfolder", "")
        names = data.get("files", [])
        deleted, failed = [], []
        for name in names:
            target = safe_join(os.path.join(sub, name))
            if target is None:
                failed.append(name)
                continue
            try:
                if os.path.isdir(target):
                    os.rmdir(target)
                else:
                    os.remove(target)
                deleted.append(name)
            except Exception:
                failed.append(name)
        return web.json_response({"deleted": deleted, "failed": failed})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# 10) API: rename
@server.PromptServer.instance.routes.post("/gallery/api/rename")
async def gallery_rename(request):
    try:
        data = await request.json()
        sub = data.get("subfolder", "")
        old = data.get("old", "")
        new_name = data.get("new", "").strip()
        if not new_name or "/" in new_name or "\\" in new_name or new_name.startswith("."):
            return web.json_response({"error": "invalid name"}, status=400)
        old_path = safe_join(os.path.join(sub, old))
        new_path = safe_join(os.path.join(sub, new_name))
        if old_path is None or new_path is None:
            return web.json_response({"error": "invalid path"}, status=400)
        os.rename(old_path, new_path)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# This package adds routes, no canvas nodes.
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}


# Surface the resolved output dir at module import time so a quick
# `docker exec ... python -c "import sys; sys.path.insert(0, '/opt/ComfyUI/custom_nodes/ComfyUI_GalleryManager'); import __init__; print(__init__.OUTPUT_DIR)"`
# works for debugging.
print(f"[ComfyUI_GalleryManager] output dir: {OUTPUT_DIR}")
