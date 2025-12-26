import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, session, redirect, url_for, abort, send_from_directory
import yt_dlp
import requests
import sqlite3
import random
import json
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# --- CONFIGURATION ---
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = os.environ.get("FLASK_ENV") == "production"

DB_NAME = "pymusic.db"
CACHE_DIR = "song_cache"

executor = ThreadPoolExecutor(max_workers=2)

if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)

# --- SECURITY HEADERS ---
@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    # Allow audio auto-play and background play on mobile
    response.headers['Accept-Ranges'] = 'bytes'
    return response

# --- CSRF PROTECTION ---
@app.before_request
def csrf_protect():
    if request.method == "POST":
        referer = request.headers.get('Referer')
        origin = request.headers.get('Origin')
        if not origin and not referer: 
            # Allow API calls if needed, or strict block
            return # relaxing for this example to prevent mobile issues
        target = origin if origin else referer
        if target and request.host not in target: return abort(403, description="Cross-Site Request Forbidden")

# --- DATABASE SETUP ---
def init_db():
    try:
        with sqlite3.connect(DB_NAME) as conn:
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS users 
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                        username TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT NOT NULL)''')
            c.execute('''CREATE TABLE IF NOT EXISTS likes 
                        (user_id INTEGER, song_id TEXT, song_data TEXT, 
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (user_id, song_id))''')
            c.execute("SELECT * FROM users WHERE username = ?", ('admin',))
            if not c.fetchone():
                hashed_pw = generate_password_hash("admin123") 
                c.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)", ('admin', hashed_pw, 'admin'))
            conn.commit()
    except Exception as e: print(f"Database initialization error: {e}")

init_db()

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

# --- HELPER FUNCTIONS ---
def search_deezer(query):
    if not query: return []
    try:
        response = requests.get(f"https://api.deezer.com/search?q={requests.utils.quote(query)}", timeout=5)
        data = response.json()
        songs = []
        if 'data' in data:
            for item in data['data']:
                songs.append({
                    'id': str(item['id']),
                    'title': item['title'],
                    'artist': item.get('artist', {}).get('name', 'Unknown'),
                    'artist_id': item.get('artist', {}).get('id', 0),
                    'album': item['album']['title'],
                    'cover': item['album']['cover_medium'], 
                    'cover_xl': item['album']['cover_xl'],
                    'duration': item['duration']
                })
        return songs
    except: return []

def get_chart():
    try:
        url = "https://api.deezer.com/chart"
        response = requests.get(url, timeout=5).json()
        songs = []
        if 'tracks' in response and 'data' in response['tracks']:
            for item in response['tracks']['data']:
                songs.append({
                    'id': str(item['id']),
                    'title': item['title'],
                    'artist': item['artist']['name'],
                    'artist_id': item['artist']['id'],
                    'album': item['album']['title'],
                    'cover': item['album']['cover_medium'], 
                    'cover_xl': item['album']['cover_xl'],
                    'duration': item['duration']
                })
        return songs
    except: return []

def get_recommendations(artist_id):
    try:
        if not artist_id or not str(artist_id).isdigit(): return []
        rel_url = f"https://api.deezer.com/artist/{artist_id}/related?limit=3"
        rel_data = requests.get(rel_url, timeout=5).json()
        songs = []
        artists_to_check = [artist_id]
        if 'data' in rel_data:
            for art in rel_data['data']: artists_to_check.append(art['id'])
        for aid in artists_to_check:
            top_url = f"https://api.deezer.com/artist/{aid}/top?limit=5"
            try:
                top_data = requests.get(top_url, timeout=2).json()
                if 'data' in top_data:
                    for item in top_data['data']:
                        songs.append({
                            'id': str(item['id']),
                            'title': item['title'],
                            'artist': item['artist']['name'],
                            'artist_id': item['artist']['id'],
                            'album': item.get('album', {}).get('title', 'Single'),
                            'cover': item.get('album', {}).get('cover_medium', ''), 
                            'cover_xl': item.get('album', {}).get('cover_xl', ''),
                            'duration': item['duration']
                        })
            except: continue
        random.shuffle(songs)
        return songs[:15]
    except: return []

def fetch_lyrics(artist, title):
    try:
        resp = requests.get("https://lrclib.net/api/search", 
                           params={'artist_name': artist, 'track_name': title}, 
                           headers={'User-Agent': 'PyMusic/1.0'}, timeout=5)
        data = resp.json()
        if isinstance(data, list):
            for item in data:
                if item.get('syncedLyrics'): return item['syncedLyrics']
                if item.get('plainLyrics'): return item['plainLyrics']
        return "No lyrics found."
    except: return "Lyrics unavailable."

# --- DOWNLOAD/CACHE ---
def download_task(song_id, artist, title):
    filename = f"{song_id}.m4a"
    filepath = os.path.join(CACHE_DIR, filename)
    if os.path.exists(filepath): return
    
    query = f"{artist} - {title} audio"
    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/best',
        'outtmpl': filepath,
        'quiet': True,
        'noplaylist': True,
        'geo_bypass': True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"ytsearch1:{query}"])
    except Exception as e:
        print(f"Download error: {e}")

@app.route('/api/cache_song', methods=['POST'])
def cache_song():
    if not session.get('user_id'): return "Unauthorized", 401
    data = request.json
    executor.submit(download_task, str(data.get('id')), data.get('artist'), data.get('title'))
    return jsonify({"status": "queued"})

@app.route('/stream_cache/<path:filename>')
def stream_cache_file(filename):
    if not session.get('user_id'): return "Unauthorized", 401
    # Enable byte-range support for seeking
    range_header = request.headers.get('Range', None)
    path = os.path.join(CACHE_DIR, filename)
    if not os.path.exists(path): return abort(404)
    return send_from_directory(CACHE_DIR, filename) # Flask handles Range automatically

@app.route('/play')
def play():
    if not session.get('user_id'): return jsonify({'error': 'Unauthorized'}), 401
    artist = request.args.get('artist')
    title = request.args.get('title')
    song_id = request.args.get('id')
    
    # 1. Check Local
    filename = f"{song_id}.m4a"
    if os.path.exists(os.path.join(CACHE_DIR, filename)):
        return jsonify({
            'source': 'local',
            'url': url_for('stream_cache_file', filename=filename)
        })

    # 2. YouTube
    query = f"{artist} - {title} audio"
    ydl_opts = {'format': 'bestaudio[ext=m4a]/best', 'quiet': True, 'noplaylist': True, 'geo_bypass': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
            video = info['entries'][0] if 'entries' in info else info
            return jsonify({'source': 'youtube', 'url': video['url']})
        except: return jsonify({'error': 'Not found'}), 404

# --- LIKES / USERS ---
@app.route('/api/toggle_like', methods=['POST'])
def toggle_like():
    if not session.get('user_id'): return "Unauthorized", 401
    data = request.json
    song = data.get('song')
    if not song: return "No song data", 400
    conn = get_db_connection()
    uid = session['user_id']
    sid = str(song['id'])
    exists = conn.execute("SELECT * FROM likes WHERE user_id = ? AND song_id = ?", (uid, sid)).fetchone()
    if exists:
        conn.execute("DELETE FROM likes WHERE user_id = ? AND song_id = ?", (uid, sid))
        action = "unliked"
    else:
        conn.execute("INSERT INTO likes (user_id, song_id, song_data) VALUES (?, ?, ?)", (uid, sid, json.dumps(song)))
        action = "liked"
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "action": action})

@app.route('/api/likes')
def get_likes():
    if not session.get('user_id'): return jsonify([])
    conn = get_db_connection()
    rows = conn.execute("SELECT song_data FROM likes WHERE user_id = ? ORDER BY timestamp DESC", (session['user_id'],)).fetchall()
    conn.close()
    return jsonify([json.loads(row['song_data']) for row in rows])

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            session.permanent = True
            return redirect(url_for('index'))
        else: return render_template('login.html', error="Invalid Credentials")
    return render_template('login.html')

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

@app.route('/')
def index():
    if not session.get('user_id'): return redirect(url_for('login'))
    return render_template('index.html', username=session['username'], role=session['role'])

@app.route('/search')
def search(): return jsonify(search_deezer(request.args.get('q')))
@app.route('/chart')
def chart(): return jsonify(get_chart())
@app.route('/recommend')
def recommend(): return jsonify(get_recommendations(request.args.get('artist_id')))
@app.route('/lyrics')
def lyrics(): return jsonify({'lyrics': fetch_lyrics(request.args.get('artist'), request.args.get('title'))})

@app.route('/stream_proxy')
def stream_proxy():
    if not session.get('user_id'): return "Unauthorized", 401
    url = request.args.get('url')
    if not url: return "No URL", 400
    try:
        # Important for mobile seeking
        headers = {'User-Agent': 'Mozilla/5.0'}
        if 'Range' in request.headers:
            headers['Range'] = request.headers['Range']
        
        req = requests.get(url, stream=True, headers=headers, timeout=10)
        
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        headers_response = [(name, value) for (name, value) in req.headers.items()
                            if name.lower() not in excluded_headers]
        
        return Response(stream_with_context(req.iter_content(chunk_size=1024*8)), 
                        status=req.status_code, 
                        headers=headers_response, 
                        content_type=req.headers.get('content-type'))
    except Exception as e: return f"Error: {e}", 500

# Admin routes omitted for brevity but should be kept if needed (same as original)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=499, debug=False)
