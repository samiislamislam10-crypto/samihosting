import os, sys, sqlite3, secrets, hashlib, subprocess, threading, shutil, time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_socketio import SocketIO

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "panel.db"
USERS_DIR = BASE / "user_data"
RUNTIME_ROOT = BASE / "runtime_users"
USERS_DIR.mkdir(exist_ok=True)
RUNTIME_ROOT.mkdir(exist_ok=True)
os.environ["PYTHONUNBUFFERED"] = "1"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

runtimes = {}
lock = threading.RLock()

EXCLUDE = {
    "panel.db", "panel.db-shm", "panel.db-wal", "user_data", "runtime_users",
    "__pycache__", ".git", ".idea", "templates", "static"
}

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def db():
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def hash_password(p):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", p.encode(), salt, 210000)
    return salt.hex()+"$"+digest.hex()

def verify_password(p, stored):
    try:
        salt, digest = stored.split("$",1)
        got = hashlib.pbkdf2_hmac("sha256", p.encode(), bytes.fromhex(salt), 210000).hex()
        return secrets.compare_digest(got,digest)
    except Exception:
        return False

def init_db():
    c=db()
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user',
        created_at TEXT NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS bot_accounts(
        user_id INTEGER PRIMARY KEY,
        bot_name TEXT NOT NULL DEFAULT 'SAMI',
        uid TEXT NOT NULL DEFAULT '',
        bot_password TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS apis(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        base_url TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1
    )""")
    if not c.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone():
        cur=c.execute("INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)",
                      ("admin",hash_password("changeme123"),"admin",now_iso()))
        c.execute("INSERT INTO bot_accounts(user_id,updated_at) VALUES(?,?)",(cur.lastrowid,now_iso()))
    c.commit(); c.close()

def user_runtime_dir(uid):
    p=RUNTIME_ROOT/str(uid)
    p.mkdir(parents=True,exist_ok=True)
    return p

def refresh_runtime(uid):
    """Copy the supplied bot project into an isolated per-user directory."""
    dst=user_runtime_dir(uid)
    # Clear old runtime files so removed/changed resources don't linger.
    for child in dst.iterdir():
        try:
            if child.is_dir(): shutil.rmtree(child)
            else: child.unlink()
        except OSError:
            pass
    for child in BASE.iterdir():
        if child.name in EXCLUDE or child.name == dst.name:
            continue
        target=dst/child.name
        if child.is_dir():
            shutil.copytree(child,target)
        elif child.is_file():
            shutil.copy2(child,target)
    return dst

def account(uid):
    c=db()
    r=c.execute("SELECT * FROM bot_accounts WHERE user_id=?",(uid,)).fetchone()
    c.close()
    return dict(r) if r else {"bot_name":"SAMI","uid":"","bot_password":""}

def save_account(uid,name,user_uid,password):
    c=db()
    c.execute("""INSERT INTO bot_accounts(user_id,bot_name,uid,bot_password,updated_at)
                 VALUES(?,?,?,?,?)
                 ON CONFLICT(user_id) DO UPDATE SET bot_name=excluded.bot_name,
                 uid=excluded.uid,bot_password=excluded.bot_password,updated_at=excluded.updated_at""",
              (uid,name,user_uid,password,now_iso()))
    c.commit(); c.close()
    rd=refresh_runtime(uid)
    (rd/"SAMI.txt").write_text(f"uid={user_uid}\npassword={password}\n",encoding="utf-8")
    (rd/"MAX").write_text(name or "SAMI",encoding="utf-8")
    return rd

def emit_log(uid,text):
    socketio.emit("console_log",{"user_id":uid,"text":text,"time":datetime.now().strftime("%H:%M:%S")})

def process_alive(r):
    p=r.get("proc") if r else None
    return bool(p and p.poll() is None)

def reader(uid,proc):
    try:
        for line in iter(proc.stdout.readline,""):
            if line:
                emit_log(uid,line.rstrip())
        proc.stdout.close()
    except Exception as e:
        emit_log(uid,f"[PANEL ERROR] log reader: {e}")
    finally:
        code=proc.poll()
        if code is None:
            try: code=proc.wait(timeout=2)
            except Exception: code="unknown"
        with lock:
            r=runtimes.get(uid)
            if r and r.get("proc") is proc:
                r["running"]=False
        emit_log(uid,f"[PROCESS] main.py exited with code {code}")
        socketio.emit("runtime_status",{"user_id":uid,"running":False,"exit_code":code})

def start_bot(uid):
    with lock:
        r=runtimes.get(uid)
        if process_alive(r):
            raise RuntimeError("Your bot is already running.")
    a=account(uid)
    if not a["uid"] or not a["bot_password"]:
        raise ValueError("UID and password are required. Save them first.")
    rd=save_account(uid,a["bot_name"],a["uid"],a["bot_password"])
    main_file=rd/"main.py"
    if not main_file.exists():
        raise FileNotFoundError("main.py is missing from the project.")
    env=os.environ.copy()
    env["PYTHONUNBUFFERED"]="1"
    env["SAMI_CREDENTIALS_FILE"]=str(rd/"SAMI.txt")
    env["SAMI_DATA_DIR"]=str(rd)
    env["SAMI_PANEL_USER_ID"]=str(uid)
    emit_log(uid,"[SYSTEM] Preparing isolated runtime...")
    proc=subprocess.Popen(
        [sys.executable,"-u","main.py"],
        cwd=str(rd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    with lock:
        runtimes[uid]={"proc":proc,"running":True,"started":time.time(),"runtime":str(rd)}
    emit_log(uid,"[SYSTEM] main.py started")
    emit_log(uid,f"[SYSTEM] SAMI.txt: {rd/'SAMI.txt'}")
    threading.Thread(target=reader,args=(uid,proc),daemon=True).start()
    socketio.emit("runtime_status",{"user_id":uid,"running":True})
    return True

def stop_bot(uid):
    with lock:
        r=runtimes.get(uid)
        p=r.get("proc") if r else None
    if not p or p.poll() is not None:
        return False
    try:
        p.terminate()
    except Exception as e:
        emit_log(uid,f"[PANEL ERROR] {e}")
    emit_log(uid,"[SYSTEM] Stop requested")
    return True

def login_required(f):
    @wraps(f)
    def w(*a,**kw):
        if not session.get("user_id"): return redirect(url_for("login"))
        return f(*a,**kw)
    return w

def admin_required(f):
    @wraps(f)
    def w(*a,**kw):
        if session.get("role")!="admin": return jsonify(status="error",message="Admin only"),403
        return f(*a,**kw)
    return w

@app.get("/login")
def login(): return render_template("login.html") if not session.get("user_id") else redirect("/")

@app.get("/register")
def register(): return render_template("register.html") if not session.get("user_id") else redirect("/")

@app.post("/api/register")
def register_api():
    d=request.get_json(silent=True) or {}
    u=(d.get("username") or "").strip(); p=d.get("password") or ""
    if len(u)<3 or len(u)>32 or len(p)<6:
        return jsonify(status="error",message="Username 3-32 characters; password 6+ characters.")
    if any(ch.isspace() for ch in u):
        return jsonify(status="error",message="Username cannot contain spaces.")
    c=db()
    try:
        cur=c.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",(u,hash_password(p),now_iso()))
        c.execute("INSERT INTO bot_accounts(user_id,updated_at) VALUES(?,?)",(cur.lastrowid,now_iso()))
        c.commit()
    except sqlite3.IntegrityError:
        c.rollback(); c.close()
        return jsonify(status="error",message="Username already exists.")
    c.close()
    return jsonify(status="success")

@app.post("/api/login_auth")
def login_auth():
    d=request.get_json(silent=True) or {}
    c=db(); u=c.execute("SELECT * FROM users WHERE username=?",(d.get("username",""),)).fetchone(); c.close()
    if not u or not verify_password(d.get("password",""),u["password_hash"]):
        return jsonify(status="error",message="Invalid username or password.")
    session.clear(); session["user_id"]=u["id"]; session["username"]=u["username"]; session["role"]=u["role"]
    return jsonify(status="success")

@app.get("/logout")
def logout(): session.clear(); return redirect("/login")

@app.get("/")
@login_required
def index(): return render_template("index.html",username=session["username"],role=session["role"])

@app.get("/api/me")
@login_required
def me():
    a=account(session["user_id"]); r=runtimes.get(session["user_id"])
    return jsonify(username=session["username"],role=session["role"],account=a,running=process_alive(r))

@app.post("/api/account")
@login_required
def account_api():
    d=request.get_json(silent=True) or {}
    name=(d.get("name") or "SAMI").strip() or "SAMI"
    uid=(d.get("uid") or "").strip(); pw=d.get("password") or ""
    if not uid or not pw: return jsonify(status="error",message="UID and password are required.")
    rd=save_account(session["user_id"],name,uid,pw)
    emit_log(session["user_id"],"[SYSTEM] SAMI.txt updated")
    return jsonify(status="success",path=str(rd/"SAMI.txt"))

@app.post("/api/control")
@login_required
def control():
    action=(request.get_json(silent=True) or {}).get("action")
    try:
        if action=="start": start_bot(session["user_id"])
        elif action=="stop": stop_bot(session["user_id"])
        elif action=="restart":
            stop_bot(session["user_id"]); time.sleep(1); start_bot(session["user_id"])
        else: return jsonify(status="error",message="Unknown action.")
        return jsonify(status="success",action=action)
    except Exception as e:
        emit_log(session["user_id"],f"[ERROR] {e}")
        return jsonify(status="error",message=str(e))

@app.get("/api/status")
@login_required
def status():
    r=runtimes.get(session["user_id"])
    return jsonify(running=process_alive(r),started=(r.get("started") if r else None))

@app.get("/api/admin/users")
@admin_required
def users():
    c=db(); rows=c.execute("SELECT id,username,role,created_at FROM users ORDER BY id DESC").fetchall(); c.close()
    out=[]
    for x in rows:
        out.append({**dict(x),"running":process_alive(runtimes.get(x["id"]))})
    return jsonify(out)

@app.get("/api/admin/apis")
@admin_required
def apis():
    c=db(); rows=c.execute("SELECT id,name,base_url,enabled FROM apis ORDER BY id DESC").fetchall(); c.close()
    return jsonify([dict(x) for x in rows])

@app.post("/api/admin/apis")
@admin_required
def api_add():
    d=request.get_json(silent=True) or {}; n=(d.get("name") or "").strip(); b=(d.get("base_url") or "").strip()
    if not n or not b: return jsonify(status="error",message="Name and URL required.")
    c=db(); cur=c.execute("INSERT INTO apis(name,base_url,enabled) VALUES(?,?,1)",(n,b.rstrip("/"))); c.commit(); c.close()
    return jsonify(status="success",id=cur.lastrowid)

@app.put("/api/admin/apis/<int:i>")
@admin_required
def api_edit(i):
    d=request.get_json(silent=True) or {}; c=db()
    c.execute("UPDATE apis SET name=?,base_url=?,enabled=? WHERE id=?",
              ((d.get("name") or "").strip(),(d.get("base_url") or "").strip().rstrip("/"),1 if d.get("enabled",True) else 0,i))
    c.commit(); c.close(); return jsonify(status="success")

@app.delete("/api/admin/apis/<int:i>")
@admin_required
def api_delete(i):
    c=db(); c.execute("DELETE FROM apis WHERE id=?",(i,)); c.commit(); c.close(); return jsonify(status="success")

@app.post("/api/admin/user/<int:i>/stop")
@admin_required
def admin_stop(i): stop_bot(i); return jsonify(status="success")

init_db()

if __name__=="__main__":
    port=int(os.environ.get("PORT","10000"))
    socketio.run(app,host="0.0.0.0",port=port,allow_unsafe_werkzeug=True)
