import datetime as dt
import html
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
UPLOAD_FOLDER = BASE_DIR / "user_uploads"
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", str(150 * 1024 * 1024)))
TOKEN_TTL_HOURS = int(os.environ.get("TOKEN_TTL_HOURS", "168"))
MASTER_PASSWORD = os.environ.get("MASTER_PASSWORD", "s@go₹")
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "super_secret_bothost_key_v3")
WATCHDOG_INTERVAL = int(os.environ.get("WATCHDOG_INTERVAL", "8"))
RESTART_COOLDOWN = int(os.environ.get("RESTART_COOLDOWN", "3"))
MAX_LOG_BYTES = int(os.environ.get("MAX_LOG_BYTES", str(48 * 1024)))
MAX_TEXT_BYTES = int(os.environ.get("MAX_TEXT_BYTES", str(256 * 1024)))

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass

app = Flask(__name__, static_folder=str(PUBLIC_DIR), static_url_path="/public")
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.secret_key = FLASK_SECRET_KEY
CORS(app, resources={r"/*": {"origins": "*"}})

BOOT_TIME = dt.datetime.now(dt.timezone.utc)

# active_sessions[token] = {created_at: datetime}
active_sessions: Dict[str, Dict[str, Any]] = {}
# running_processes[app_name] = metadata
running_processes: Dict[str, Dict[str, Any]] = {}
lock = threading.RLock()


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    if not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def cleanup_sessions() -> None:
    cutoff = utcnow() - dt.timedelta(hours=TOKEN_TTL_HOURS)
    expired = [token for token, meta in active_sessions.items() if not isinstance(meta.get("created_at"), dt.datetime) or meta["created_at"] < cutoff]
    for token in expired:
        active_sessions.pop(token, None)


def get_request_token() -> str:
    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return (
        request.headers.get("X-Auth-Token", "").strip()
        or (request.form.get("token", "").strip() if request.form else "")
        or ((request.get_json(silent=True) or {}).get("token", "").strip())
    )


def is_token_valid(token: str) -> bool:
    if not token:
        return False
    cleanup_sessions()
    meta = active_sessions.get(token)
    if not meta:
        return False
    created_at = meta.get("created_at")
    return isinstance(created_at, dt.datetime) and created_at >= utcnow() - dt.timedelta(hours=TOKEN_TTL_HOURS)


def require_auth():
    token = get_request_token()
    if is_token_valid(token):
        return None
    return jsonify({"error": "Unauthorized"}), 403


def login_with_password(password: str) -> Optional[str]:
    if password != MASTER_PASSWORD:
        return None
    token = secrets.token_urlsafe(32)
    active_sessions[token] = {"created_at": utcnow()}
    return token


def is_safe_path(base_dir: Path, target_path: Path) -> bool:
    base_resolved = base_dir.resolve()
    target_resolved = target_path.resolve()
    return target_resolved == base_resolved or str(target_resolved).startswith(str(base_resolved) + os.sep)


def safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            member_path = (dest_dir / member.filename)
            if not is_safe_path(dest_dir, member_path):
                raise ValueError(f"Unsafe path in zip: {member.filename}")
        zf.extractall(dest_dir)


def find_bot_file(folder_path: str) -> Tuple[Optional[str], Optional[str]]:
    priority_files = ["main.py", "bot.py", "app.py", "index.py", "start.py", "run.py"]
    folder = Path(folder_path)
    for filename in priority_files:
        candidate = folder / filename
        if candidate.exists():
            return str(candidate), str(folder)

    for root, _, files in os.walk(folder_path):
        root_path = Path(root)
        if "__pycache__" in root_path.parts or ".git" in root_path.parts:
            continue
        for filename in priority_files:
            if filename in files:
                candidate = root_path / filename
                return str(candidate), str(root_path)
        for filename in files:
            if filename.endswith(".py"):
                candidate = root_path / filename
                return str(candidate), str(root_path)
    return None, None


def install_requirements(folder_path: str) -> None:
    folder = Path(folder_path)
    req_path = folder / "requirements.txt"
    if not req_path.exists():
        for root, _, files in os.walk(folder_path):
            if "requirements.txt" in files:
                req_path = Path(root) / "requirements.txt"
                break

    if req_path.exists():
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(req_path)],
                check=False,
                cwd=str(req_path.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


def cleanup_finished_processes() -> None:
    with lock:
        dead = []
        for app_name, pdata in running_processes.items():
            proc = pdata["process"]
            if proc.poll() is not None:
                dead.append(app_name)
        for app_name in dead:
            pdata = running_processes.pop(app_name, None)
            if pdata and pdata.get("log_handle"):
                try:
                    pdata["log_handle"].close()
                except Exception:
                    pass


def read_tail(file_path: Path, limit: int = MAX_LOG_BYTES) -> str:
    if not file_path.exists():
        return "Waiting for logs..."
    try:
        with file_path.open("r", errors="ignore") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(size - limit, 0))
            return f.read() or "Waiting for logs..."
    except Exception:
        return "Waiting for logs..."


def stop_process_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except Exception:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def start_bot_process(app_name: str) -> Tuple[bool, str]:
    app_name = secure_filename(app_name)
    app_dir = UPLOAD_FOLDER / app_name
    if not app_dir.exists():
        return False, "App folder not found"

    script_path, script_dir = find_bot_file(str(app_dir))
    if not script_path:
        return False, "No Python file found"

    pid_key = f"bot_{app_name}"
    with lock:
        if pid_key in running_processes and running_processes[pid_key]["process"].poll() is None:
            return True, "Already running"

        log_file_path = app_dir / "logs.txt"
        log_handle = log_file_path.open("a", buffering=1, encoding="utf-8", errors="ignore")
        try:
            my_env = os.environ.copy()
            my_env.setdefault("PYTHONUNBUFFERED", "1")
            popen_kwargs = dict(
                args=[sys.executable, "-u", os.path.basename(script_path)],
                cwd=script_dir,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                env=my_env,
            )
            if os.name != "nt":
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(**popen_kwargs)
            running_processes[pid_key] = {
                "process": proc,
                "start_time": utcnow(),
                "log_handle": log_handle,
                "script_path": script_path,
                "script_dir": script_dir,
                "app_dir": str(app_dir),
                "restart_count": 0,
                "last_restart": 0.0,
            }
            return True, "Bot started"
        except Exception as e:
            try:
                log_handle.close()
            except Exception:
                pass
            return False, f"Start failed: {e}"


def auto_restart_watchdog() -> None:
    while True:
        time.sleep(WATCHDOG_INTERVAL)
        with lock:
            for app_name, pdata in list(running_processes.items()):
                proc = pdata["process"]
                if proc.poll() is None:
                    continue
                # restart if the bot crashed unexpectedly
                now = time.time()
                if now - float(pdata.get("last_restart", 0.0)) < RESTART_COOLDOWN:
                    continue
                app_dir = Path(pdata.get("app_dir", ""))
                if not app_dir.exists():
                    continue
                script_path, script_dir = find_bot_file(str(app_dir))
                if not script_path:
                    continue
                log_handle = pdata.get("log_handle")
                try:
                    if log_handle:
                        log_handle.write(f"\n[watchdog] Process exited. Restarting {app_name}...\n")
                except Exception:
                    pass
                try:
                    if log_handle:
                        log_handle.close()
                except Exception:
                    pass
                try:
                    new_log = (app_dir / "logs.txt").open("a", buffering=1, encoding="utf-8", errors="ignore")
                    my_env = os.environ.copy()
                    my_env.setdefault("PYTHONUNBUFFERED", "1")
                    popen_kwargs = dict(
                        args=[sys.executable, "-u", os.path.basename(script_path)],
                        cwd=script_dir,
                        stdout=new_log,
                        stderr=subprocess.STDOUT,
                        text=True,
                        env=my_env,
                    )
                    if os.name != "nt":
                        popen_kwargs["start_new_session"] = True
                    new_proc = subprocess.Popen(**popen_kwargs)
                    pdata.update({
                        "process": new_proc,
                        "start_time": utcnow(),
                        "log_handle": new_log,
                        "script_path": script_path,
                        "script_dir": script_dir,
                        "app_dir": str(app_dir),
                        "restart_count": int(pdata.get("restart_count", 0)) + 1,
                        "last_restart": now,
                    })
                except Exception:
                    try:
                        if new_log:
                            new_log.write(f"[watchdog] Restart failed for {app_name}\n")
                            new_log.close()
                    except Exception:
                        pass


def collect_proc_stats(pid: Optional[int]) -> Dict[str, Any]:
    if not pid:
        return {"memory_mb": 0.0, "cpu_percent": 0.0}
    try:
        p = psutil.Process(pid)
        mem = p.memory_info().rss / (1024 * 1024)
        cpu = p.cpu_percent(interval=None)
        return {"memory_mb": round(mem, 2), "cpu_percent": round(cpu, 1)}
    except Exception:
        return {"memory_mb": 0.0, "cpu_percent": 0.0}


def list_app_files(app_dir: Path, rel_path: str = "") -> List[Dict[str, Any]]:
    target = (app_dir / rel_path).resolve()
    if not is_safe_path(app_dir, target):
        raise ValueError("Unsafe path")
    if not target.exists():
        raise FileNotFoundError("Path not found")
    items: List[Dict[str, Any]] = []
    for entry in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        st = entry.stat()
        items.append({
            "name": entry.name,
            "path": str(entry.relative_to(app_dir)).replace("\\", "/"),
            "type": "dir" if entry.is_dir() else "file",
            "size": st.st_size,
            "mtime": dt.datetime.fromtimestamp(st.st_mtime, tz=dt.timezone.utc).isoformat(),
        })
    return items


def is_text_file(path: Path) -> bool:
    return path.suffix.lower() in {
        ".py", ".txt", ".md", ".json", ".yaml", ".yml", ".html", ".css", ".js", ".ini", ".env", ".cfg", ".toml"
    }


@app.route("/")
def serve_index():
    return send_from_directory(str(PUBLIC_DIR), "index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/status")
def status():
    running = 0
    with lock:
        cleanup_finished_processes()
        running = sum(1 for pdata in running_processes.values() if pdata["process"].poll() is None)
    uptime = utcnow() - BOOT_TIME
    return jsonify({
        "status": "Online",
        "message": "Bot Host BD Backend V3 is Active",
        "system": "Advanced Bot Runner",
        "render_ready": True,
        "uptime": human_duration(uptime.total_seconds()),
        "running_bots": running,
    })


@app.route("/api/overview", methods=["POST"])
def overview():
    auth_error = require_auth()
    if auth_error:
        return auth_error

    cleanup_finished_processes()
    total_apps = 0
    running_apps = 0
    stats = []
    with lock:
        for app_name in sorted([p.name for p in UPLOAD_FOLDER.iterdir() if p.is_dir()]):
            total_apps += 1
            pid_key = f"bot_{app_name}"
            pdata = running_processes.get(pid_key)
            proc = pdata["process"] if pdata and pdata["process"].poll() is None else None
            if proc:
                running_apps += 1
                start_time = pdata["start_time"]
                uptime_secs = (utcnow() - start_time).total_seconds()
                proc_stats = collect_proc_stats(proc.pid)
            else:
                start_time = None
                uptime_secs = 0
                proc_stats = {"memory_mb": 0.0, "cpu_percent": 0.0}
            stats.append({
                "name": app_name,
                "running": bool(proc),
                "uptime": human_duration(uptime_secs),
                "uptime_seconds": int(uptime_secs),
                "memory_mb": proc_stats["memory_mb"],
                "cpu_percent": proc_stats["cpu_percent"],
            })

    try:
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage(str(BASE_DIR))
        system_cpu = psutil.cpu_percent(interval=None)
    except Exception:
        vm = None
        disk = None
        system_cpu = 0.0

    return jsonify({
        "server_uptime": human_duration((utcnow() - BOOT_TIME).total_seconds()),
        "total_apps": total_apps,
        "running_apps": running_apps,
        "sessions": len(active_sessions),
        "memory": {
            "percent": round(vm.percent, 1) if vm else 0,
            "used_gb": round(vm.used / (1024**3), 2) if vm else 0,
            "total_gb": round(vm.total / (1024**3), 2) if vm else 0,
        },
        "disk": {
            "percent": round(disk.percent, 1) if disk else 0,
            "used_gb": round(disk.used / (1024**3), 2) if disk else 0,
            "total_gb": round(disk.total / (1024**3), 2) if disk else 0,
        },
        "cpu": round(system_cpu, 1),
        "apps": stats,
    })


@app.route("/auth", methods=["POST"])
def auth():
    data = request.get_json(silent=True) or {}
    password = str(data.get("password", "")).strip()
    token = login_with_password(password)
    if token:
        return jsonify({"success": True, "message": "Access Granted", "token": token})
    return jsonify({"success": False, "message": "Wrong Password!"}), 401


@app.route("/upload", methods=["POST"])
def upload():
    auth_error = require_auth()
    if auth_error:
        return auth_error
    if "file" not in request.files:
        return jsonify({"error": "No file sent"}), 400

    file = request.files["file"]
    filename = secure_filename(file.filename or "")
    if not filename:
        return jsonify({"error": "Invalid filename"}), 400

    ext = os.path.splitext(filename)[1].lower()
    if ext not in [".zip", ".py"]:
        return jsonify({"error": "Invalid file type. Only .zip or .py"}), 400

    app_name = os.path.splitext(filename)[0]
    app_dir = UPLOAD_FOLDER / app_name
    if app_dir.exists():
        shutil.rmtree(app_dir, ignore_errors=True)
    app_dir.mkdir(parents=True, exist_ok=True)

    save_path = app_dir / filename
    file.save(str(save_path))

    if ext == ".zip":
        try:
            safe_extract_zip(save_path, app_dir)
            save_path.unlink(missing_ok=True)
            install_requirements(str(app_dir))
        except Exception as e:
            shutil.rmtree(app_dir, ignore_errors=True)
            return jsonify({"error": f"Zip failed: {str(e)}"}), 500

    return jsonify({"message": "Deploy Successful!", "app_name": app_name})


@app.route("/my_apps", methods=["POST"])
def my_apps():
    auth_error = require_auth()
    if auth_error:
        return auth_error

    cleanup_finished_processes()
    apps = []
    for full_path in sorted([p for p in UPLOAD_FOLDER.iterdir() if p.is_dir()], key=lambda p: p.name.lower()):
        app_name = full_path.name
        pid = f"bot_{app_name}"
        pdata = running_processes.get(pid)
        proc = pdata["process"] if pdata and pdata["process"].poll() is None else None
        start_time = None
        uptime = "0s"
        mem = 0.0
        cpu = 0.0
        restart_count = int(pdata.get("restart_count", 0)) if pdata else 0
        if proc and pdata:
            start_time = pdata["start_time"].astimezone(dt.timezone.utc).isoformat()
            uptime_seconds = (utcnow() - pdata["start_time"]).total_seconds()
            uptime = human_duration(uptime_seconds)
            stats = collect_proc_stats(proc.pid)
            mem = stats["memory_mb"]
            cpu = stats["cpu_percent"]

        files_count = 0
        try:
            files_count = sum(1 for _ in full_path.rglob("*") if _.is_file())
        except Exception:
            files_count = 0

        apps.append({
            "name": app_name,
            "running": bool(proc),
            "logs": read_tail(full_path / "logs.txt", limit=MAX_LOG_BYTES),
            "start_time": start_time,
            "uptime": uptime,
            "memory_mb": mem,
            "cpu_percent": cpu,
            "files_count": files_count,
            "restart_count": restart_count,
        })

    return jsonify({"apps": apps})


@app.route("/action", methods=["POST"])
def action():
    auth_error = require_auth()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}
    act = str(data.get("action", "")).strip().lower()
    app_name = secure_filename(str(data.get("app_name", "")).strip())
    if not app_name:
        return jsonify({"error": "Missing app name"}), 400

    pid = f"bot_{app_name}"
    app_dir = UPLOAD_FOLDER / app_name

    if act == "start":
        ok, msg = start_bot_process(app_name)
        if ok:
            return jsonify({"message": msg})
        return jsonify({"error": msg}), 404 if msg == "App folder not found" else 500

    if act == "stop":
        with lock:
            pdata = running_processes.get(pid)
            if pdata:
                proc = pdata["process"]
                stop_process_tree(proc)
                try:
                    pdata["log_handle"].close()
                except Exception:
                    pass
                running_processes.pop(pid, None)
                return jsonify({"message": "Stopped!"})
        return jsonify({"error": "Not running"}), 404

    if act == "restart":
        with lock:
            pdata = running_processes.get(pid)
            if pdata:
                proc = pdata["process"]
                stop_process_tree(proc)
                try:
                    pdata["log_handle"].close()
                except Exception:
                    pass
                running_processes.pop(pid, None)
        ok, msg = start_bot_process(app_name)
        if ok:
            return jsonify({"message": "Restarted!"})
        return jsonify({"error": msg}), 500

    if act == "delete":
        with lock:
            pdata = running_processes.get(pid)
            if pdata:
                stop_process_tree(pdata["process"])
                try:
                    pdata["log_handle"].close()
                except Exception:
                    pass
                running_processes.pop(pid, None)
        if app_dir.exists():
            shutil.rmtree(app_dir, ignore_errors=True)
            return jsonify({"message": "Deleted!"})
        return jsonify({"error": "Not found"}), 404

    return jsonify({"error": "Bad Request"}), 400


@app.route("/files/list", methods=["POST"])
def files_list():
    auth_error = require_auth()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}
    app_name = secure_filename(str(data.get("app_name", "")).strip())
    rel_path = str(data.get("path", "")).strip().replace("\\", "/")
    if not app_name:
        return jsonify({"error": "Missing app name"}), 400
    app_dir = (UPLOAD_FOLDER / app_name).resolve()
    if not app_dir.exists():
        return jsonify({"error": "App not found"}), 404
    try:
        items = list_app_files(app_dir, rel_path)
        return jsonify({"path": rel_path, "items": items})
    except FileNotFoundError:
        return jsonify({"error": "Path not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/files/read", methods=["POST"])
def files_read():
    auth_error = require_auth()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}
    app_name = secure_filename(str(data.get("app_name", "")).strip())
    rel_path = str(data.get("path", "")).strip().replace("\\", "/")
    if not app_name or not rel_path:
        return jsonify({"error": "Missing parameters"}), 400
    app_dir = (UPLOAD_FOLDER / app_name).resolve()
    target = (app_dir / rel_path).resolve()
    if not is_safe_path(app_dir, target):
        return jsonify({"error": "Unsafe path"}), 400
    if not target.exists() or not target.is_file():
        return jsonify({"error": "File not found"}), 404
    if target.stat().st_size > MAX_TEXT_BYTES:
        return jsonify({"error": "File too large"}), 400
    if not is_text_file(target):
        return jsonify({"error": "Not a text file"}), 400

    try:
        content = target.read_text(encoding="utf-8", errors="ignore")
        return jsonify({"path": rel_path, "content": content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/files/write", methods=["POST"])
def files_write():
    auth_error = require_auth()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}
    app_name = secure_filename(str(data.get("app_name", "")).strip())
    rel_path = str(data.get("path", "")).strip().replace("\\", "/")
    content = str(data.get("content", ""))
    if not app_name or not rel_path:
        return jsonify({"error": "Missing parameters"}), 400
    app_dir = (UPLOAD_FOLDER / app_name).resolve()
    target = (app_dir / rel_path).resolve()
    if not is_safe_path(app_dir, target):
        return jsonify({"error": "Unsafe path"}), 400
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text(content, encoding="utf-8")
        return jsonify({"message": "Saved"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/files/delete", methods=["POST"])
def files_delete():
    auth_error = require_auth()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}
    app_name = secure_filename(str(data.get("app_name", "")).strip())
    rel_path = str(data.get("path", "")).strip().replace("\\", "/")
    if not app_name or not rel_path:
        return jsonify({"error": "Missing parameters"}), 400
    app_dir = (UPLOAD_FOLDER / app_name).resolve()
    target = (app_dir / rel_path).resolve()
    if not is_safe_path(app_dir, target):
        return jsonify({"error": "Unsafe path"}), 400
    if not target.exists():
        return jsonify({"error": "Not found"}), 404
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return jsonify({"message": "Deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/files/mkdir", methods=["POST"])
def files_mkdir():
    auth_error = require_auth()
    if auth_error:
        return auth_error
    data = request.get_json(silent=True) or {}
    app_name = secure_filename(str(data.get("app_name", "")).strip())
    rel_path = str(data.get("path", "")).strip().replace("\\", "/")
    if not app_name or not rel_path:
        return jsonify({"error": "Missing parameters"}), 400
    app_dir = (UPLOAD_FOLDER / app_name).resolve()
    target = (app_dir / rel_path).resolve()
    if not is_safe_path(app_dir, target):
        return jsonify({"error": "Unsafe path"}), 400
    try:
        target.mkdir(parents=True, exist_ok=True)
        return jsonify({"message": "Folder created"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/logout", methods=["POST"])
def logout():
    token = get_request_token()
    if token:
        active_sessions.pop(token, None)
    return jsonify({"message": "Logged out"})


@app.errorhandler(404)
def not_found(_):
    # Let the frontend handle unknown browser routes, but keep API responses sane.
    if request.path.startswith(("/api", "/auth", "/upload", "/action", "/files", "/logout", "/health")):
        return jsonify({"error": "Not found"}), 404
    return send_from_directory(str(PUBLIC_DIR), "index.html")


threading.Thread(target=auto_restart_watchdog, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
