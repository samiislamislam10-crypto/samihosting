import os
import sqlite3
import time
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "sami-yt-secret-key-2026")

# SocketIO setup with threading fallback to prevent crashes on Railway
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

DATABASE = 'panel.db'

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT DEFAULT 'user',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS accounts (
                user_id INTEGER PRIMARY KEY,
                bot_name TEXT,
                uid TEXT,
                bot_password TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS apis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                enabled INTEGER DEFAULT 1
            )
        ''')
        admin = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
        if not admin:
            conn.execute("INSERT INTO users (username, password, role) VALUES ('admin', 'changeme123', 'admin')")
        conn.commit()

init_db()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated_function

# ==================== ADSENSE VERIFICATION ROUTE ====================

@app.route('/ads.txt')
def ads_txt():
    return "google.com, pub-3618481233219616, DIRECT, f08c47fec0942fa0", 200, {'Content-Type': 'text/plain'}

# ==================== MAIN PAGE ROUTES ====================

@app.route('/')
def index():
    if 'user_id' not in session:
        return render_template('login.html')
    return render_template('index.html', username=session.get('username'), role=session.get('role'))

@app.route('/login')
def login_page():
    if 'user_id' in session:
        return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/register')
def register_page():
    if 'user_id' in session:
        return redirect(url_for('index'))
    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))

# ==================== API ENDPOINTS ====================

@app.route('/api/login_auth', methods=['POST'])
def login_auth():
    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '')

    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user and user['password'] == password:
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            return jsonify({'status': 'success', 'message': 'Logged in successfully'})

    return jsonify({'status': 'error', 'message': 'Invalid username or password'}), 401

@app.route('/api/register', methods=['POST'])
def register_auth():
    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '')

    if not username or not password:
        return jsonify({'status': 'error', 'message': 'Missing fields'}), 400

    try:
        with get_db() as conn:
            conn.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, password))
            conn.commit()
        return jsonify({'status': 'success', 'message': 'Registration successful'})
    except sqlite3.IntegrityError:
        return jsonify({'status': 'error', 'message': 'Username already exists'}), 400

@app.route('/api/me')
@login_required
def get_me():
    user_id = session['user_id']
    with get_db() as conn:
        acc = conn.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,)).fetchone()
        account_data = {
            'bot_name': acc['bot_name'] if acc else 'SAMI',
            'uid': acc['uid'] if acc else '',
            'bot_password': acc['bot_password'] if acc else ''
        }
    return jsonify({
        'user_id': user_id,
        'username': session['username'],
        'role': session['role'],
        'running': False,
        'account': account_data
    })

@app.route('/api/account', methods=['POST'])
@login_required
def save_account():
    user_id = session['user_id']
    data = request.json or {}
    name = data.get('name', 'SAMI')
    uid = data.get('uid', '')
    password = data.get('password', '')

    with get_db() as conn:
        conn.execute('''
            INSERT INTO accounts (user_id, bot_name, uid, bot_password)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
            bot_name=excluded.bot_name,
            uid=excluded.uid,
            bot_password=excluded.bot_password
        ''', (user_id, name, uid, password))
        conn.commit()

    return jsonify({'status': 'success', 'message': 'Account saved successfully'})

@app.route('/api/control', methods=['POST'])
@login_required
def process_control():
    data = request.json or {}
    action = data.get('action')
    return jsonify({'status': 'success', 'action': action})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)
