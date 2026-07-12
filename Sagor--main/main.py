import datetime
import os
import secrets
import shutil
import signal
import subprocess
import sys
import zipfile
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
UPLOAD_FOLDER = BASE_DIR / "user_uploads"
MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", str(150 * 1024 * 1024)))  # 150 MB
TOKEN_TTL_HOURS = int(os.environ.get("TOKEN_TTL_HOURS", "168"))  # 7 days

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass

MASTER_PASSWORD = os.environ.get("MASTER_PASSWORD", "s@go₹")

app = Flask(
    __name__,
    static_folder=str(PUBLIC_DIR),
    static_url_path="/public",
)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "super_secret_bothost_key_v2")

CORS(app, resources={r"/*": {"origins": "*"}})

UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

# running_processes:
# {
#   "bot_name": {
#       "process": Popen,
#       "start_time": datetime,
#       "log_handle": file,
#       "script_path": str,
#       "script_dir": str
#   }
# }
running_processes = {}

# token -> {"created_at": datetime.datetime}
active_sessions = {}


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def cleanup_sessions() -> None:
    expired = []
    cutoff = now_utc() - datetime.timedelta(hours=TOKEN_TTL_HOURS)
    for token, meta in active_sessions.items():
        created_at = meta.get("created_at")
        if not isinstance(created_at, datetime.datetime) or created_at < cutoff:
            expired.append(token)
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
    session = active_sessions.get(token)
    if not session:
        return False
    created_at = session.get("created_at")
    if not isinstance(created_at, datetime.datetime):
        return False
    return created_at >= now_utc() - datetime.timedelta(hours=TOKEN_TTL_HOURS)


def require_auth():
    token = get_request_token()
    if is_token_valid(token):
        return None
    return jsonify({"error": "Unauthorized"}), 403


def login_with_password(password: str):
    if password != MASTER_PASSWORD:
        return None
    token = secrets.token_urlsafe(32)
    active_sessions[token] = {"created_at": now_utc()}
    return token


def safe_join(base_dir: Path, *paths: str) -> Path:
    return Path(base_dir, *paths).resolve()


def is_safe_path(base_dir: Path, target_path: Path) -> bool:
    base_resolved = base_dir.resolve()
    target_resolved = target_path.resolve()
    return str(target_resolved).startswith(str(base_resolved) + os.sep) or target_resolved == base_resolved


def safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            member_path = dest_dir / member.filename
            if not is_safe_path(dest_dir, member_path):
                raise ValueError(f"Unsafe path in zip: {member.filename}")
        zf.extractall(dest_dir)


def find_bot_file(folder_path: str):
    priority_files = ["main.py", "bot.py", "app.py", "index.py", "start.py", "run.py"]

    folder = Path(folder_path)
    for filename in priority_files:
        candidate = folder / filename
        if candidate.exists():
            return str(candidate), str(folder)

    for root, dirs, files in os.walk(folder_path):
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


def install_requirements(folder_path: str):
    folder = Path(folder_path)
    req_path = folder / "requirements.txt"

    if not req_path.exists():
        for root, dirs, files in os.walk(folder_path):
            if "requirements.txt" in files:
                req_path = Path(root) / "requirements.txt"
                break

    if req_path.exists():
        print(f"[*] Found requirements.txt at {req_path}. Installing...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(req_path)],
                check=False,
                cwd=str(req_path.parent),
            )
        except Exception as e:
            print(f"[!] Error installing requirements: {e}")


def cleanup_finished_processes():
    dead = []
    for pid, pdata in running_processes.items():
        proc = pdata["process"]
        if proc.poll() is not None:
            dead.append(pid)
    for pid in dead:
        pdata = running_processes.pop(pid, None)
        if pdata and pdata.get("log_handle"):
            try:
                pdata["log_handle"].close()
            except Exception:
                pass


def read_tail(file_path: Path, limit: int = 4000) -> str:
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


def stop_process_tree(proc: subprocess.Popen):
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


@app.route("/")
def serve_index():
    return send_from_directory(str(PUBLIC_DIR), "index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/status")
def status():
    return jsonify(
        {
            "status": "Online",
            "message": "Bot Host BD Backend V2 is Active",
            "system": "Advanced Bot Runner",
            "render_ready": True,
        }
    )


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
        try:
            shutil.rmtree(app_dir)
        except Exception:
            pass
    app_dir.mkdir(parents=True, exist_ok=True)

    save_path = app_dir / filename
    file.save(str(save_path))

    if ext == ".zip":
        try:
            safe_extract_zip(save_path, app_dir)
            save_path.unlink(missing_ok=True)
            install_requirements(str(app_dir))
        except Exception as e:
            return jsonify({"error": f"Zip failed: {str(e)}"}), 500

    return jsonify({"message": "Deploy Successful!", "app_name": app_name})


@app.route("/my_apps", methods=["POST"])
def my_apps():
    auth_error = require_auth()
    if auth_error:
        return auth_error

    cleanup_finished_processes()

    apps = []
    if not UPLOAD_FOLDER.exists():
        return jsonify({"apps": []})

    for app_name in sorted(os.listdir(UPLOAD_FOLDER)):
        full_path = UPLOAD_FOLDER / app_name
        if not full_path.is_dir():
            continue

        pid = f"bot_{app_name}"
        is_running = False
        start_time = None

        if pid in running_processes:
            p_data = running_processes[pid]
            proc = p_data["process"]
            if proc.poll() is None:
                is_running = True
                start_time = p_data["start_time"].astimezone(datetime.timezone.utc).isoformat()
            else:
                pdata = running_processes.pop(pid, None)
                if pdata and pdata.get("log_handle"):
                    try:
                        pdata["log_handle"].close()
                    except Exception:
                        pass

        logs = read_tail(full_path / "logs.txt", limit=6000)

        apps.append(
            {
                "name": app_name,
                "running": is_running,
                "logs": logs,
                "start_time": start_time,
            }
        )

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
        if pid in running_processes and running_processes[pid]["process"].poll() is None:
            return jsonify({"message": "Already Running!"})

        if not app_dir.exists():
            return jsonify({"error": "App folder not found"}), 404

        script_path, script_dir = find_bot_file(str(app_dir))
        if not script_path:
            return jsonify({"error": "No Python file found!"}), 404

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

            running_processes[pid] = {
                "process": proc,
                "start_time": datetime.datetime.now(datetime.timezone.utc),
                "log_handle": log_handle,
                "script_path": script_path,
                "script_dir": script_dir,
            }
            return jsonify({"message": "Bot Started!"})
        except Exception as e:
            try:
                log_handle.close()
            except Exception:
                pass
            return jsonify({"error": f"Start failed: {str(e)}"}), 500

    if act == "stop":
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

    if act == "delete":
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
