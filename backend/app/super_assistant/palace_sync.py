"""记忆宫殿「文件夹同步」端点（独立子路由）。

两套路由、同前缀 /api/v2/super-assistant：
- management_router：浏览器侧（JWT 鉴权，menu_guard 由 main.py 挂载声明），
  负责令牌生成/重置与同步脚本下发（脚本内嵌 BASE_URL 与当前令牌）。
- sync_router：用户本机脚本侧（X-Palace-Sync-Token 长效令牌鉴权，不挂
  menu_guard——依赖内部同样校验 super_assistant 菜单权限），端点是
  palace 文件/目录读写能力的薄封装，全部复用 palace_service 既有函数，
  与浏览器路径同一套配额、白名单与抽取派发语义。

脚本上传的文件名经独立 form 字段 filename 传递（multipart 文件部分的
filename 恒为 ASCII 占位），规避跨平台 multipart 文件名编码差异——
非 ASCII 文件名在 form 字段里按 UTF-8 文本可靠解码。
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from app.auth.crypto import decrypt_value, encrypt_value
from app.auth.models import User
from app.auth.permissions import user_has_menu_access
from app.deps import get_current_user, get_db
from app.shared.config import settings
from app.super_assistant import palace_service
from app.super_assistant.models import SuperAssistantPalaceSyncToken
from app.super_assistant.palace_service import PalaceFolderCreate

management_router = APIRouter()
sync_router = APIRouter()

_sync_token_header = APIKeyHeader(name="X-Palace-Sync-Token", auto_error=False)

_MAX_FILENAME_LENGTH = 255


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_sync_token(db: Session, owner_id: str) -> str:
    """生成（或重置）每用户一条的同步令牌；明文仅此一次返回。

    重置覆盖 token_hash/token_encrypted 两列，旧令牌立即失效。
    """
    token = f"pal_sync_{secrets.token_urlsafe(32)}"
    row = db.get(SuperAssistantPalaceSyncToken, owner_id)
    if row is None:
        row = SuperAssistantPalaceSyncToken(owner_id=owner_id)
        db.add(row)
    row.token_hash = _hash_token(token)
    row.token_encrypted = encrypt_value(token)
    row.last_used_at = None
    db.commit()
    return token


def current_sync_token(db: Session, owner_id: str) -> str:
    """脚本下发用：返回当前令牌明文（无则现场生成），重复下载不轮换。"""
    row = db.get(SuperAssistantPalaceSyncToken, owner_id)
    if row is None:
        return generate_sync_token(db, owner_id)
    try:
        return decrypt_value(row.token_encrypted)
    except Exception as exc:
        # ENCRYPTION_KEY 轮换后旧密文不可解（令牌鉴权走 hash 不受影响，
        # 已下发脚本仍可用）；显式引导重置而非裸 500。
        raise HTTPException(
            409,
            "加密密钥已变更，无法还原既有同步令牌；请先重置同步令牌再下载脚本",
        ) from exc


def get_palace_sync_user(
    token: str | None = Depends(_sync_token_header),
    db: Session = Depends(get_db),
) -> User:
    if not token:
        raise HTTPException(status_code=401, detail="Invalid sync token")
    row = (
        db.query(SuperAssistantPalaceSyncToken)
        .filter(SuperAssistantPalaceSyncToken.token_hash == _hash_token(token))
        .first()
    )
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid sync token")
    user = db.get(User, row.owner_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid sync token")
    if not user_has_menu_access(db, user, "super_assistant"):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "MENU_ACCESS_DENIED",
                "message": "当前角色无权访问此功能",
                "menu_key": "super_assistant",
            },
        )
    # last_used_at 节流写入：大批量同步是数百次连发请求，逐请求提交只添
    # 写放大；60 秒内的重复调用不再触碰该列（检测信号精度足够）。
    stamp = row.last_used_at
    if stamp is not None and stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if stamp is None or (now - stamp).total_seconds() > 60:
        row.last_used_at = now
        db.commit()
    return user


def _apply_filename_override(upload: UploadFile, filename: str) -> None:
    """脚本以 UTF-8 form 字段提供真实文件名，覆盖 multipart 占位名。"""
    cleaned = (filename or "").strip().replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not cleaned:
        return
    if len(cleaned) > _MAX_FILENAME_LENGTH:
        raise HTTPException(400, f"文件名过长（最多 {_MAX_FILENAME_LENGTH} 字符）")
    upload.filename = cleaned


# ---------------------------------------------------------------------------
# 本机脚本侧：X-Palace-Sync-Token 鉴权的同步端点（palace_service 薄封装）
# ---------------------------------------------------------------------------


@sync_router.get("/palace/sync/files")
def sync_list_palace_files(
    db: Session = Depends(get_db),
    user: User = Depends(get_palace_sync_user),
):
    return palace_service.list_files(db, user.id)


@sync_router.get("/palace/sync/folders")
def sync_list_palace_folders(
    db: Session = Depends(get_db),
    user: User = Depends(get_palace_sync_user),
):
    return palace_service.list_folders(db, user.id)


@sync_router.post("/palace/sync/files", status_code=201)
def sync_upload_palace_file(
    file: UploadFile = File(...),
    folder_path: str = Form(""),
    filename: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_palace_sync_user),
):
    _apply_filename_override(file, filename)
    return palace_service.upload_file(db, user, file, folder_path)


@sync_router.post("/palace/sync/files/{file_id}/replace")
def sync_replace_palace_file(
    file_id: str,
    file: UploadFile = File(...),
    filename: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_palace_sync_user),
):
    _apply_filename_override(file, filename)
    return palace_service.replace_file(db, user.id, file_id, file)


@sync_router.delete("/palace/sync/files/{file_id}", status_code=204)
def sync_delete_palace_file(
    file_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_palace_sync_user),
):
    palace_service.delete_palace_file(db, user.id, file_id)


@sync_router.post("/palace/sync/folders", status_code=201)
def sync_create_palace_folder(
    body: PalaceFolderCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_palace_sync_user),
):
    return palace_service.create_folder(db, user.id, body.path)


@sync_router.delete("/palace/sync/folders/{folder_id}", status_code=204)
def sync_delete_palace_folder(
    folder_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_palace_sync_user),
):
    palace_service.delete_folder(db, user.id, folder_id)


# ---------------------------------------------------------------------------
# 浏览器侧：令牌管理 + 脚本下发（JWT 鉴权）
# ---------------------------------------------------------------------------


@management_router.post("/palace/sync/token")
def reset_palace_sync_token(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """重置同步令牌（旧令牌立即失效，已下载的旧脚本需重新下载）。明文仅此一次返回。"""
    token = generate_sync_token(db, current_user.id)
    return {"token": token}


@management_router.get("/palace/sync/script")
def download_palace_sync_script(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """渲染并下发文件夹同步脚本（内嵌 BASE_URL 与当前同步令牌）。

    下载交互依赖浏览器副作用，按 AGENTS.md §5 副作用验收标准：前端 E2E
    必须断言下载文件内容，不能只断言"提示出现"。
    """
    token = current_sync_token(db, current_user.id)
    base_url = (getattr(settings, "pipeline_file_public_api_base_url", "") or "").strip().rstrip("/")
    script = render_sync_script(
        base_url=base_url,
        token=token,
        allowed_extensions=sorted(palace_service._palace_allowed_extensions()),
        max_upload_mb=int(settings.max_upload_mb),
        max_in_flight=int(settings.super_assistant_palace_max_in_flight),
    )
    return Response(
        content=script,
        media_type="text/x-python",
        headers={
            "Content-Disposition": 'attachment; filename="palace_sync.py"',
            # 响应体内嵌令牌明文：禁止任何中间层/浏览器缓存
            "Cache-Control": "no-store",
        },
    )


def render_sync_script(
    *,
    base_url: str,
    token: str,
    allowed_extensions: list[str],
    max_upload_mb: int,
    max_in_flight: int,
) -> str:
    return (
        _SYNC_SCRIPT_TEMPLATE
        .replace("__BASE_URL__", repr(base_url))
        .replace("__TOKEN__", repr(token))
        .replace("__ALLOWED_EXTENSIONS__", repr(sorted(allowed_extensions)))
        .replace("__MAX_UPLOAD_MB__", repr(int(max_upload_mb)))
        .replace("__IN_FLIGHT_LIMIT__", repr(int(max_in_flight)))
    )


# ---------------------------------------------------------------------------
# 客户端脚本模板：stdlib-only、Python 3.8+、跨 Windows/Linux/macOS。
# 嵌入值经 __SENTINEL__ 占位替换（不使用 str.format，脚本体内的花括号
# 无需转义）；扩展名白名单与单文件上限渲染时取自当前后端配置，保证
# 客户端/服务端口径一致。
# ---------------------------------------------------------------------------

_SYNC_SCRIPT_TEMPLATE = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenOntology 知识图谱「文件夹同步」脚本（由平台自动生成）。

把 CONFIG 里 LOCAL_DIR 指定的本地文件夹镜像同步到平台知识图谱（记忆宫殿）
的 synced/ 子树：新增/变更文件上传后自动触发图谱抽取；本地删除/改名的
文件会同步删除平台侧对应文件（镜像语义；只作用于 synced/ 子树，不影响
你在平台上手动上传的文件）。可重复运行，幂等。

用法（Windows / Linux / macOS，需 Python 3.8+，无需安装任何依赖）：
  python palace_sync.py                 # 同步 CONFIG['LOCAL_DIR']
  python palace_sync.py --folder 路径    # 临时指定本地文件夹
  python palace_sync.py --dry-run       # 只预览将上传/删除的清单，不做改动
  python palace_sync.py --yes           # 跳过删除确认（供定时任务使用）

定时任务建议加锁避免重叠运行（如 flock）：重叠或"已入库但响应丢失"的
重试会产生重复文件行（平台按路径不去重）。REMOTE_ROOT 必须位于 synced/
子树内，这是"不影响手动上传文件"的范围保证。

隐私说明：脚本只上传扩展名在白名单内的文档/图片；隐藏目录与隐藏文件
（如 .git、.plan.md）、node_modules、Office 临时锁文件、系统文件与超大
文件默认跳过，跳过原因会在运行时逐条打印。同步令牌已内嵌，请勿把本
脚本分享给他人。
"""

import argparse
import hashlib
import http.client
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

CONFIG = {
    "BASE_URL": __BASE_URL__,  # 平台地址（形如 http://your-host:8000）；部署在远端时请按实际地址修改
    "TOKEN": __TOKEN__,        # 同步令牌（已内嵌；若失效请在平台「外部集成 → 文件夹同步」重新下载）
    "LOCAL_DIR": "",           # 要同步的本地文件夹，例如 %USERPROFILE%/Documents/mydocs 或 ~/Documents/mydocs
    "REMOTE_ROOT": "",         # 平台侧目标子树；留空自动使用 synced/<本地文件夹名>
}

ALLOWED_EXTENSIONS = set(__ALLOWED_EXTENSIONS__)
MAX_UPLOAD_BYTES = __MAX_UPLOAD_MB__ * 1024 * 1024
IN_FLIGHT_THRESHOLD = max(1, __IN_FLIGHT_LIMIT__ - 4)  # 预留余量，避免贴着上限被 429
POLL_SECONDS = int(os.environ.get("PALACE_SYNC_POLL_SECONDS", "10"))
HOURLY_WAIT_SECONDS = int(os.environ.get("PALACE_SYNC_HOURLY_WAIT_SECONDS", "120"))
# 全轮共享的"等待配额释放"总预算（秒）：防止抽取队列停滞/小时限流把脚本
# 无限挂起，也防止逐文件重置预算导致整体运行时间无界
WAIT_BUDGET_SECONDS = 30 * 60
HTTP_TIMEOUT = 300

API_PREFIX = "/api/v2/super-assistant/palace/sync"

_IGNORED_DIRS_LOWER = {
    "$recycle.bin", "system volume information", "node_modules",
    "__pycache__", ".git", ".hg", ".svn",
}
_IGNORED_FILE_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
_IGNORED_FILE_SUFFIXES = (".tmp", ".swp", ".crdownload", ".part", ".partial")

_state = {"in_flight": 0, "wait_budget": WAIT_BUDGET_SECONDS}


def log(message):
    print(message, flush=True)


def die(message):
    log("[错误] " + message)
    sys.exit(1)


class ApiError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


def _read_error_detail(exc):
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:
        return ""
    try:
        payload = json.loads(raw)
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, dict):
            return str(detail.get("message") or detail)
    except Exception:
        pass
    return raw[:300]


def api(method, path, json_body=None, data=None, content_type=None):
    url = CONFIG["BASE_URL"].rstrip("/") + path
    headers = {"X-Palace-Sync-Token": CONFIG["TOKEN"], "Accept": "application/json"}
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif data is not None:
        body = data
        headers["Content-Type"] = content_type
    last_error = None
    for attempt in range(3):
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                payload = response.read()
                if not payload:
                    return None
                try:
                    return json.loads(payload.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    raise ApiError(response.status, "响应不是有效 JSON: %s" % payload[:120])
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, _read_error_detail(exc))
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            last_error = exc
            wait = min(60, 5 * (2 ** attempt))
            log("[网络] 请求失败（%s），%d 秒后重试" % (exc, wait))
            time.sleep(wait)
    raise ApiError(0, "网络请求持续失败: %s" % last_error)


def normalize_rel(path):
    parts = []
    for piece in str(path).replace("\\\\", "/").split("/"):
        piece = piece.strip()
        if piece and piece != ".":
            parts.append(piece)
    return "/".join(parts)


_SAFE_NAME_RE = re.compile(r"[^\\w.()\\-\\u4e00-\\u9fff]+", re.UNICODE)


def safe_name(name):
    """与服务端 steward/workspace.safe_filename 同构的文件名归一。

    服务端落库的是归一化后的名字；diff 键若用本地原始名，含空格等
    字符的文件会每次运行都"删除+重传+重抽"（churn），必须两侧同口径。
    """
    raw = str(name).replace("\\\\", "/").rsplit("/", 1)[-1].strip()
    cleaned = _SAFE_NAME_RE.sub("_", raw).strip("._ ")
    if not cleaned:
        cleaned = "file"
    return cleaned[:180]


def skip_reason(name, full):
    lower = name.lower()
    if name.startswith("."):
        return "隐藏文件"
    if lower in _IGNORED_FILE_NAMES:
        return "系统文件"
    if name.startswith("~$"):
        return "Office 临时锁文件"
    if "." not in name:
        return "无扩展名（不在白名单）"
    extension = lower.rsplit(".", 1)[1]
    if extension not in ALLOWED_EXTENSIONS:
        return "类型不在白名单（.%s）" % extension
    for suffix in _IGNORED_FILE_SUFFIXES:
        if lower.endswith(suffix):
            return "临时文件"
    if lower.endswith("~"):
        return "临时文件"
    try:
        if os.path.getsize(full) > MAX_UPLOAD_BYTES:
            return "超过单文件 %dMB 上限" % (MAX_UPLOAD_BYTES // (1024 * 1024))
    except OSError as exc:
        return "无法读取（%s）" % exc
    return None


def scan_folder(root):
    entries = []
    skipped = []
    local_dirs = set()
    seen_rels = set()
    for dirpath, dirnames, filenames in os.walk(root):
        kept = []
        for name in dirnames:
            if name.startswith(".") or name.lower() in _IGNORED_DIRS_LOWER:
                skipped.append(("忽略目录", normalize_rel(os.path.relpath(os.path.join(dirpath, name), root))))
            else:
                kept.append(name)
        dirnames[:] = sorted(kept)
        rel_dir = normalize_rel(os.path.relpath(dirpath, root))
        if rel_dir:
            local_dirs.add(rel_dir)
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            display = (rel_dir + "/" + normalize_rel(name)) if rel_dir else normalize_rel(name)
            reason = skip_reason(name, full)
            if reason:
                skipped.append((reason, display))
                continue
            try:
                size = os.path.getsize(full)
            except OSError as exc:
                skipped.append(("无法读取（%s）" % exc, display))
                continue
            # 目录段服务端原样保留；文件名段必须用归一化名，与服务端落库名一致
            sync_name = safe_name(name)
            rel = (rel_dir + "/" + sync_name) if rel_dir else sync_name
            if rel in seen_rels:
                skipped.append(("与其它文件归一化后重名（%s）" % sync_name, display))
                continue
            seen_rels.add(rel)
            entries.append({"rel": rel, "full": full, "size": size})
    return entries, skipped, local_dirs


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def remote_state(remote_root):
    """返回 (子树文件表[相对 remote_root 的路径]，子树目录集合, 全局在途抽取数)。"""
    files = api("GET", API_PREFIX + "/files") or []
    folders = api("GET", API_PREFIX + "/folders") or []
    prefix = remote_root + "/"
    file_map = {}
    in_flight = 0
    for item in files:
        folder = item.get("path") or ""
        full = (folder + "/" + item["filename"]) if folder else item["filename"]
        if full.startswith(prefix) and len(full) > len(prefix):
            file_map[full[len(prefix):]] = item
        if item.get("status") in ("pending", "building"):
            in_flight += 1
    folder_paths = set()
    for item in folders:
        path = item.get("path") or ""
        if path == remote_root or path.startswith(prefix):
            folder_paths.add(path)
    return file_map, folder_paths, in_flight


def refresh_in_flight():
    files = api("GET", API_PREFIX + "/files") or []
    _state["in_flight"] = sum(1 for item in files if item.get("status") in ("pending", "building"))


def wait_for_capacity():
    while _state["in_flight"] >= IN_FLIGHT_THRESHOLD:
        if _state["wait_budget"] <= 0:
            die("等待平台抽取队列释放超时（%d 分钟）：请在「知识图谱」弹窗确认构建是否停滞，恢复后重新运行脚本即可续传" % (WAIT_BUDGET_SECONDS // 60))
        _state["wait_budget"] -= POLL_SECONDS
        log("  [等待] 平台抽取队列在途 %d（阈值 %d），%d 秒后重查…" % (_state["in_flight"], IN_FLIGHT_THRESHOLD, POLL_SECONDS))
        time.sleep(POLL_SECONDS)
        try:
            refresh_in_flight()
        except ApiError as exc:
            log("  [等待] 状态刷新失败（%s），继续等待" % exc.message)


def multipart_body(fields, file_path, file_name):
    boundary = "----PalaceSync" + uuid.uuid4().hex
    chunks = []
    for name, value in fields:
        chunks.append(
            ("--%s\\r\\nContent-Disposition: form-data; name=\\"%s\\"\\r\\n\\r\\n%s\\r\\n"
             % (boundary, name, value)).encode("utf-8"))
    mime = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    # multipart 文件部分名恒为 ASCII 占位；真实（可能是中文的）文件名走
    # 独立 form 字段 filename，由服务端覆盖，规避 multipart 文件名编码差异
    chunks.append(
        ("--%s\\r\\nContent-Disposition: form-data; name=\\"file\\"; filename=\\"payload\\"\\r\\n"
         "Content-Type: %s\\r\\n\\r\\n" % (boundary, mime)).encode("utf-8"))
    with open(file_path, "rb") as handle:
        chunks.append(handle.read())
    chunks.append(("\\r\\n--%s--\\r\\n" % boundary).encode("utf-8"))
    return b"".join(chunks), "multipart/form-data; boundary=" + boundary


def _handle_rate_limit(exc, label):
    """429 按语义退避（共享等待预算防挂起）；返回 True 表示可重试。"""
    message = exc.message or ""
    if "队列已满" in message or "频繁" in message:
        wait = POLL_SECONDS if "队列已满" in message else HOURLY_WAIT_SECONDS
        if _state["wait_budget"] <= 0:
            die("等待平台配额释放超时（%d 分钟）：稍后重新运行脚本即可续传剩余文件" % (WAIT_BUDGET_SECONDS // 60))
        _state["wait_budget"] -= wait
        log("  [等待] %s: %s（%d 秒后重试）" % (label, message, wait))
        time.sleep(wait)
        if "队列已满" in message:
            try:
                refresh_in_flight()
            except ApiError:
                pass  # 刷新失败不阻断：下一轮 429 会再次触发等待
        return True
    if "上限" in message:
        die("平台配额已达上限: %s（请删除部分文件或联系管理员调整配额后重新运行）" % message)
    return False


def upload_one(entry, replace_file_id, remote_root):
    label = entry["rel"]
    name = label.rsplit("/", 1)[-1]
    rel_folder = label.rsplit("/", 1)[0] if "/" in label else ""
    folder = (remote_root + "/" + rel_folder) if rel_folder else remote_root
    fields = [("filename", name)]
    if replace_file_id is None:
        fields.append(("folder_path", folder))
    body, content_type = multipart_body(fields, entry["full"], name)
    if replace_file_id is None:
        path = API_PREFIX + "/files"
    else:
        path = API_PREFIX + "/files/%s/replace" % urllib.parse.quote(replace_file_id, safe="")
    while True:
        try:
            api("POST", path, data=body, content_type=content_type)
            log("  [完成] " + label)
            _state["in_flight"] += 1
            return True
        except ApiError as exc:
            if exc.status == 429 and _handle_rate_limit(exc, label):
                continue
            log("  [失败] %s: %s" % (label, exc.message))
            return False


def cleanup_empty_folders(remote_root, local_dirs):
    """镜像收尾：删除子树内本地已不存在、且已为空的目录（自底向上，失败即止步）。"""
    try:
        folders = api("GET", API_PREFIX + "/folders") or []
    except ApiError as exc:
        log("  [跳过] 目录清理失败: %s" % exc.message)
        return
    prefix = remote_root + "/"
    candidates = []
    for item in folders:
        path = item.get("path") or ""
        if not path.startswith(prefix):
            continue
        if path[len(prefix):] in local_dirs:
            continue
        candidates.append(item)
    for item in sorted(candidates, key=lambda one: one.get("path") or "", reverse=True):
        try:
            api("DELETE", API_PREFIX + "/folders/%s" % urllib.parse.quote(item["id"], safe=""))
            log("  [清理] 空目录 " + (item.get("path") or ""))
        except ApiError:
            continue  # 目录非空等服务端拒绝，属预期，继续清理其余


def remote_label(item):
    folder = item.get("path") or ""
    return (folder + "/" + item["filename"]) if folder else item["filename"]


def main():
    parser = argparse.ArgumentParser(description="OpenOntology 知识图谱文件夹同步")
    parser.add_argument("--folder", help="本地文件夹路径（缺省用 CONFIG['LOCAL_DIR']）")
    parser.add_argument("--root", help="平台侧目标子树（缺省用 CONFIG['REMOTE_ROOT'] 或 synced/<文件夹名>）")
    parser.add_argument("--dry-run", action="store_true", help="只预览计划，不执行任何改动")
    parser.add_argument("--yes", action="store_true", help="跳过删除确认（供无人值守场景）")
    args = parser.parse_args()

    base_url = (CONFIG["BASE_URL"] or "").strip()
    token = (CONFIG["TOKEN"] or "").strip()
    if not base_url or not base_url.lower().startswith(("http://", "https://")):
        die("CONFIG['BASE_URL'] 未配置：请填写平台地址（例如 http://your-host:8000）")
    if not token:
        die("CONFIG['TOKEN'] 未配置：请在平台「外部集成 → 文件夹同步」重新下载脚本")
    lowered = base_url.lower()
    if lowered.startswith("http://") and "localhost" not in lowered and "127." not in lowered:
        log("[提示] 当前经未加密 HTTP 传输，建议平台侧启用 HTTPS 后改用 https 地址")

    local_dir = (args.folder or CONFIG["LOCAL_DIR"] or "").strip()
    if not local_dir:
        die("未指定本地文件夹：请填写 CONFIG['LOCAL_DIR'] 或使用 --folder 参数")
    local_dir = os.path.abspath(os.path.expanduser(local_dir))
    if not os.path.isdir(local_dir):
        die("本地文件夹不存在: %s" % local_dir)

    remote_root = normalize_rel(args.root or CONFIG["REMOTE_ROOT"] or ("synced/" + os.path.basename(local_dir)))
    if not remote_root:
        die("REMOTE_ROOT 不能为空")
    if remote_root != "synced" and not remote_root.startswith("synced/"):
        die("REMOTE_ROOT 必须位于 synced/ 子树内（当前: %s）：这是「不影响手动上传文件」的范围保证" % remote_root)

    log("同步: %s  ->  平台 %s/" % (local_dir, remote_root))
    log("扫描本地文件…")
    entries, skipped, local_dirs = scan_folder(local_dir)
    for reason, rel in skipped:
        log("  [跳过] %s（%s）" % (rel, reason))

    local_map = {}
    if entries:
        log("计算文件指纹（%d 个文件）…" % len(entries))
        for entry in entries:
            try:
                entry["sha256"] = sha256_of(entry["full"])
            except OSError as exc:
                skipped.append(("读取失败（%s）" % exc, entry["rel"]))
                continue
            local_map[entry["rel"]] = entry

    try:
        file_map, folder_paths, in_flight = remote_state(remote_root)
    except ApiError as exc:
        if exc.status == 401:
            die("令牌无效或已被重置：请在平台「外部集成 → 文件夹同步」重新下载脚本")
        die("连接平台失败: %s（请检查 CONFIG['BASE_URL'] 是否指向正确地址）" % exc.message)
    _state["in_flight"] = in_flight

    uploads = []
    replaces = []
    for rel in sorted(local_map):
        remote = file_map.get(rel)
        if remote is None:
            uploads.append(local_map[rel])
        elif (remote.get("sha256") or "") != local_map[rel]["sha256"]:
            replaces.append((remote, local_map[rel]))
    deletes = [file_map[rel] for rel in sorted(set(file_map) - set(local_map))]

    log("计划: 上传 %d，更新 %d，删除 %d，跳过 %d" % (len(uploads), len(replaces), len(deletes), len(skipped)))
    if args.dry_run:
        for entry in uploads:
            log("  [将上传] " + entry["rel"])
        for remote, entry in replaces:
            log("  [将更新] " + entry["rel"])
        for remote in deletes:
            log("  [将删除] " + remote_label(remote))
        log("dry-run 结束，未做任何改动")
        return

    failures = []

    missing_dirs = sorted(
        remote_root + "/" + rel
        for rel in local_dirs
        if (remote_root + "/" + rel) not in folder_paths
    )
    for path in missing_dirs:
        try:
            api("POST", API_PREFIX + "/folders", json_body={"path": path})
        except ApiError as exc:
            failures.append("创建目录失败 %s: %s" % (path, exc.message))

    if deletes:
        log("以下 %d 个平台侧文件在本地已不存在，将按镜像语义删除:" % len(deletes))
        for remote in deletes:
            log("  [删除] " + remote_label(remote))
        if sys.stdin.isatty() and not args.yes:
            try:
                answer = input("确认删除？输入 y 继续，其他输入跳过删除: ").strip().lower()
            except EOFError:
                answer = ""
            if answer != "y":
                log("已跳过删除（下次可用 --yes 直接执行，或 --dry-run 预览）")
                deletes = []

    log("开始传输（在途抽取达 %d 自动等待）…" % IN_FLIGHT_THRESHOLD)
    for entry in uploads:
        wait_for_capacity()
        if not upload_one(entry, None, remote_root):
            failures.append("上传失败 " + entry["rel"])
    for remote, entry in replaces:
        wait_for_capacity()
        if not upload_one(entry, remote.get("id"), remote_root):
            failures.append("更新失败 " + entry["rel"])

    for remote in deletes:
        try:
            api("DELETE", API_PREFIX + "/files/%s" % urllib.parse.quote(remote["id"], safe=""))
            log("  [已删除] " + remote_label(remote))
        except ApiError as exc:
            failures.append("删除失败 %s: %s" % (remote_label(remote), exc.message))

    cleanup_empty_folders(remote_root, local_dirs)

    log("完成: 上传 %d，更新 %d，删除 %d，失败 %d" % (len(uploads), len(replaces), len(deletes), len(failures)))
    if failures:
        for item in failures:
            log("  [失败] " + item)
        sys.exit(1)
    log("图谱抽取将在平台后台自动进行，可在「知识图谱」弹窗查看构建进度。")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
'''
