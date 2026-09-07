"""Portable, bounded folder manifests and streamed bodies (no archive extraction)."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import time
import unicodedata
import uuid

from .storage import safe_name

MAX_ENTRIES = 2048
MAX_TOTAL = 2 * 1024**3
CHUNK = 256 * 1024


def portable_path(value):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise ValueError("文件夹路径为空或过长")
    parts = value.split("/")
    if len(parts) > 32 or any(p in ("", ".", "..") for p in parts) or "\\" in value or ":" in value:
        raise ValueError("文件夹路径必须是安全的相对路径，使用 / 分隔")
    return "/".join(safe_name(p) for p in parts)


def validate_manifest(entries):
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise ValueError("文件夹最多包含 2048 项")
    result, used, spelling, total = [], {}, {}, 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("无效文件夹清单")
        path = portable_path(entry.get("path"))
        key = unicodedata.normalize("NFC", path).casefold()
        if key in used:
            raise ValueError("文件夹存在跨平台重名，需先重命名")
        kind = entry.get("kind")
        if kind not in ("file", "directory"):
            raise ValueError("不支持链接或特殊文件")
        if kind == "file":
            size, sha = entry.get("size"), entry.get("sha256")
            if type(size) is not int or not 0 <= size <= MAX_TOTAL:
                raise ValueError("无效文件大小")
            if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
                raise ValueError("无效文件校验值")
            total += size
        used[key] = kind
        spelling[key] = path
        result.append({**entry, "path": path})
    if total > MAX_TOTAL:
        raise ValueError("文件夹总大小超过 2 GiB")
    for entry in result:
        parts = entry["path"].split("/")
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i]).casefold()
            if used.get(parent) != "directory" or spelling.get(parent) != "/".join(parts[:i]):
                raise ValueError("文件夹清单缺少父目录或存在路径冲突")
    return result, total


def is_link(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def manifest(root):
    root = Path(root)
    if is_link(root) or not root.is_dir():
        raise ValueError("请选择普通文件夹，不支持符号链接")
    entries, sources = [], []
    def walk_error(error):
        raise error
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            if is_link(path):
                raise ValueError("文件夹包含符号链接，请先移除链接")
            meta = path.stat()
            entry = {"path": path.relative_to(root).as_posix()}
            if stat.S_ISDIR(meta.st_mode):
                entry["kind"] = "directory"
            elif stat.S_ISREG(meta.st_mode):
                if meta.st_size > MAX_TOTAL:
                    raise ValueError("文件超过 2 GiB")
                with path.open("rb") as source:
                    sha = hashlib.file_digest(source, "sha256").hexdigest()
                entry.update(kind="file", size=meta.st_size, sha256=sha)
                sources.append(path)
            else:
                raise ValueError("文件夹包含特殊文件")
            entries.append(entry)
            if len(entries) > MAX_ENTRIES:
                raise ValueError("文件夹最多包含 2048 项")
    validated, total = validate_manifest(entries)
    return validated, sources, total


def receive_folder(service, conn, request, peer):
    from .protocol import read_exact, send_json
    name = safe_name(request.get("name"))
    entries, total = validate_manifest(request.get("entries"))
    if shutil.disk_usage(service.state.inbox).free < total + 16 * 1024**2:
        raise ValueError("接收磁盘空间不足")
    batch = service.state.inbox / uuid.uuid4().hex[:12]
    staging = batch / ".partial"
    target = batch / name
    if target == staging:
        raise ValueError("无效文件夹名称")
    # Fail early on Windows installations without long-path support.
    if os.name == "nt" and any(len(str(staging / e["path"])) > 240 for e in entries):
        raise ValueError("接收路径过长，请缩短文件夹层级或数据目录路径")
    staging.mkdir(parents=True, mode=0o700)
    complete = False
    try:
        for entry in sorted(entries, key=lambda e: e["path"].count("/")):
            if entry["kind"] == "directory":
                (staging / entry["path"]).mkdir(mode=0o700)
        send_json(conn, {"ok": True, "ready": True})
        deadline = time.monotonic() + 3600
        for entry in entries:
            if entry["kind"] != "file":
                continue
            sha, remaining = hashlib.sha256(), entry["size"]
            with (staging / entry["path"]).open("xb") as out:
                while remaining:
                    if service.stop_event.is_set() or time.monotonic() > deadline:
                        raise TimeoutError("传输已超时或停止")
                    block = read_exact(conn, min(CHUNK, remaining))
                    out.write(block)
                    sha.update(block)
                    remaining -= len(block)
                out.flush()
                os.fsync(out.fileno())
            if sha.hexdigest() != entry["sha256"]:
                raise ValueError("文件夹内文件校验失败，请重试")
        staging.rename(target)
        service.state.record("folder", peer["name"], str(target))
        complete = True
    finally:
        if not complete:
            shutil.rmtree(batch)


def send_folder(service, peer, path, progress):
    from .protocol import send_json, response
    from .security import pinned_socket
    path = Path(path)
    entries, sources, total = manifest(path)
    with pinned_socket(peer["host"], peer["port"], peer["fingerprint"]) as conn:
        send_json(conn, {"version": 1, "op": "folder", "id": service.state.identity["id"],
                         "token": peer["token"], "name": path.name, "entries": entries})
        response(conn)
        sent = 0
        source_iter = iter(sources)
        for entry in entries:
            if entry["kind"] != "file":
                continue
            source_path = next(source_iter)
            if is_link(source_path) or any(is_link(p) for p in source_path.parents):
                raise ValueError("源路径在发送前变成了链接")
            with source_path.open("rb") as source:
                remaining = entry["size"]
                while remaining:
                    block = source.read(min(CHUNK, remaining))
                    if not block:
                        raise ValueError("文件在传输时被修改，请重试")
                    conn.sendall(block)
                    remaining -= len(block)
                    sent += len(block)
                    if progress:
                        progress(int(sent * 100 / max(total, 1)))
        response(conn)
        if progress:
            progress(100)
