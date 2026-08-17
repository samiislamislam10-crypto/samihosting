import os
import sqlite3
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_socketio import SocketIO
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # CORS সাপোর্ট যুক্ত করা হলো
app.secret_key = os.environ.get("SECRET_KEY", "sami-yt-secret-key-2026")

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Railway Container-এর জন্য Absolute Path ব্যবহার
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, 'panel.db')

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
        admin = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
        if not admin:
            conn.execute("INSERT INTO users (username, password, role) VALUES ('admin', 'changeme123', 'admin')")
        conn.commit()

init_db()

# ==================== PUBLIC & ADSENSE ROUTES ====================

@app.route('/ads.txt')
def ads_txt():
    return "google.com, pub-3618481233219616, DIRECT, f08c47fec0942fa0", 200, {'Content-Type': 'text/plain'}

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
    try:
        data = request.get_json(force=True) or {}
        username = data.get('username', '').strip()
        password = data.get('password', '')

        if not username or not password:
            return jsonify({'status': 'error', 'message': 'Username and password required'}), 400

        with get_db() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if user and user['password'] == password:
                session['user_id'] = user['id']
                session['username'] = user['username']
                session['role'] = user['role']
                return jsonify({'status': 'success', 'message': 'Logged in successfully'}), 200

        return jsonify({'status': 'error', 'message': 'Invalid username or password'}), 401
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Server Error: {str(e)}'}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)
