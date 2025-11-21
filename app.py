from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import yt_dlp
import requests
import re

app = Flask(__name__)

# --- HELPER FUNCTIONS ---

def search_deezer(query):
    url = f"https://api.deezer.com/search?q={query}"
    try:
        response = requests.get(url)
        data = response.json()
        songs = []
        if 'data' in data:
            for item in data['data']:
                songs.append({
                    'id': item['id'],
                    'title': item['title'],
                    'artist': item['artist']['name'],
                    'album': item['album']['title'],
                    'cover': item['album']['cover_medium'], 
                    'cover_xl': item['album']['cover_xl'],
                    'duration': item['duration']
                })
        return songs
    except Exception as e:
        print(f"Error searching Deezer: {e}")
        return []

def get_chart():
    url = "https://api.deezer.com/chart"
    try:
        response = requests.get(url)
        data = response.json()
        songs = []
        if 'tracks' in data and 'data' in data['tracks']:
            for item in data['tracks']['data']:
                songs.append({
                    'id': item['id'],
                    'title': item['title'],
                    'artist': item['artist']['name'],
                    'album': item['album']['title'],
                    'cover': item['album']['cover_medium'], 
                    'cover_xl': item['album']['cover_xl'],
                    'duration': item['duration']
                })
        return songs
    except Exception as e:
        return []

def get_youtube_stream_url(artist, title):
    query = f"{artist} - {title} audio"
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'noplaylist': True,
        'geo_bypass': True,
    }
    search_query = f"ytsearch1:{query}"
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(search_query, download=False)
            video = info['entries'][0] if 'entries' in info else info
            return {'url': video['url']}
        except Exception as e:
            print(f"Error extracting YouTube info: {e}")
            return None

def clean_string(s):
    # Removes (Remastered), [feat], etc.
    s = re.sub(r'\([^)]*\)', '', s)
    s = re.sub(r'\[[^]]*\]', '', s)
    return s.strip()

def fetch_lyrics(artist, title):
    """
    Advanced search strategy to find lyrics.
    """
    search_url = "https://lrclib.net/api/search"
    headers = {'User-Agent': 'PySpotifyClone/1.0'}
    
    # Strategy 1: Exact Match
    params = {'artist_name': artist, 'track_name': title}
    try:
        resp = requests.get(search_url, params=params, headers=headers)
        data = resp.json()
        if isinstance(data, list):
            for item in data:
                if item.get('syncedLyrics'): return item['syncedLyrics']
                
        # Strategy 2: Clean Title (Remove 'Remastered', 'Feat', etc)
        clean_title = clean_string(title)
        if clean_title != title:
            params['track_name'] = clean_title
            resp = requests.get(search_url, params=params, headers=headers)
            data = resp.json()
            if isinstance(data, list):
                for item in data:
                    if item.get('syncedLyrics'): return item['syncedLyrics']

        # Strategy 3: Search Title ONLY, then filter by Artist (Fuzzy)
        # This helps when Deezer says "The Weeknd" but Lyrics says "Weeknd"
        params = {'q': clean_title} 
        resp = requests.get(search_url, params=params, headers=headers)
        data = resp.json()
        if isinstance(data, list):
            for item in data:
                if item.get('syncedLyrics'):
                    # Simple check if artist name is roughly in the result artist name
                    if artist.lower() in item['artistName'].lower() or item['artistName'].lower() in artist.lower():
                        return item['syncedLyrics']

        return None
    except:
        return None

# --- ROUTES ---

@app.route('/')
def index(): return render_template('index.html')

@app.route('/search')
def search():
    query = request.args.get('q')
    if not query: return jsonify([])
    return jsonify(search_deezer(query))

@app.route('/chart')
def chart(): return jsonify(get_chart())

@app.route('/play')
def play():
    artist = request.args.get('artist')
    title = request.args.get('title')
    stream_data = get_youtube_stream_url(artist, title)
    if stream_data: return jsonify(stream_data)
    return jsonify({'error': 'Could not find song'}), 404

@app.route('/lyrics')
def lyrics():
    artist = request.args.get('artist')
    title = request.args.get('title')
    synced_lyrics = fetch_lyrics(artist, title)
    if synced_lyrics: return jsonify({'lyrics': synced_lyrics})
    return jsonify({'error': 'No lyrics found'}), 404

@app.route('/stream_proxy')
def stream_proxy():
    url = request.args.get('url')
    if not url: return "No URL provided", 400
    try:
        req = requests.get(url, stream=True)
        return Response(stream_with_context(req.iter_content(chunk_size=1024)),
                        content_type=req.headers.get('content-type', 'audio/mpeg'))
    except Exception as e:
        return f"Error: {e}", 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)