from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timedelta
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import random
import re
import socket
import struct
import threading
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.poolmanager import PoolManager
import click
import json
from flask.cli import with_appcontext
from werkzeug.utils import secure_filename
import os
from flask_login import LoginManager, login_user, logout_user, login_required, current_user, UserMixin
from sqlalchemy import and_, func, or_, text
from sqlalchemy.exc import IntegrityError
from urllib.parse import quote_plus

# ------------------ Configuration ------------------ #
def _load_env_file():
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding='utf-8') as env_file:
        for raw in env_file:
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_env_file()

# Get a free key at https://www.themoviedb.org/settings/api
TMDB_API_KEY = os.environ.get('TMDB_API_KEY', '').strip()
TMDB_BASE = 'https://api.themoviedb.org/3'
TMDB_IMAGE_BASE = 'https://image.tmdb.org/t/p/w500'
# Discord (or similar) invite — text + voice/video for 100+ members
COMMUNITY_INVITE_URL = os.environ.get(
    'COMMUNITY_INVITE_URL',
    'https://discord.com/',
).strip()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///filmboxed.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = 'your_secret_key'
app.config['UPLOAD_FOLDER'] = 'static/profile_pics'
app.config['STACK_BANNER_FOLDER'] = 'static/stack_banners'
app.config['COMMUNITY_MEDIA_FOLDER'] = 'static/community_media'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB (community video uploads)
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
COMMUNITY_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
COMMUNITY_VIDEO_EXTENSIONS = {'mp4', 'webm', 'mov'}
COMMUNITY_MEDIA_EXTENSIONS = COMMUNITY_IMAGE_EXTENSIONS | COMMUNITY_VIDEO_EXTENSIONS

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Jio's resolver answers api.themoviedb.org with an address whose TLS handshake
# never completes. Ask 1.1.1.1 / 8.8.8.8 for the real address, then connect to
# that IP while keeping the hostname for certificate checks.
_PUBLIC_DNS_SERVERS = ('1.1.1.1', '8.8.8.8')
_PUBLIC_DNS_CACHE = {}
_PUBLIC_DNS_LOCK = threading.Lock()
_PUBLIC_DNS_TTL = 1800


def _dns_query_a(server, host):
    tid = random.randint(0, 65535)
    header = struct.pack('>HHHHHH', tid, 0x0100, 1, 0, 0, 0)
    qname = b''.join(
        bytes([len(label)]) + label.encode('ascii')
        for label in host.rstrip('.').split('.')
    ) + b'\x00'
    packet = header + qname + struct.pack('>HH', 1, 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.2)
    try:
        sock.sendto(packet, (server, 53))
        data, _ = sock.recvfrom(512)
    finally:
        sock.close()
    if len(data) < 12 or struct.unpack('>H', data[:2])[0] != tid:
        return None
    answer_count = struct.unpack('>H', data[6:8])[0]
    index = 12
    while index < len(data) and data[index] != 0:
        index += data[index] + 1
    index += 5
    for _ in range(answer_count):
        if index >= len(data):
            break
        if data[index] & 0xC0 == 0xC0:
            index += 2
        else:
            while index < len(data) and data[index] != 0:
                index += data[index] + 1
            index += 1
        if index + 10 > len(data):
            break
        rtype, _rclass, _ttl, rdlen = struct.unpack('>HHIH', data[index:index + 10])
        index += 10
        rdata = data[index:index + rdlen]
        index += rdlen
        if rtype == 1 and len(rdata) == 4:
            return socket.inet_ntoa(rdata)
    return None


def _resolve_public_ipv4(host):
    host = (host or '').rstrip('.')
    if not host:
        return None
    now = time.monotonic()
    with _PUBLIC_DNS_LOCK:
        cached = _PUBLIC_DNS_CACHE.get(host)
        if cached and cached[1] > now:
            return cached[0]
    for server in _PUBLIC_DNS_SERVERS:
        try:
            ip = _dns_query_a(server, host)
        except OSError:
            ip = None
        if ip:
            with _PUBLIC_DNS_LOCK:
                _PUBLIC_DNS_CACHE[host] = (ip, now + _PUBLIC_DNS_TTL)
            return ip
    # Lookup failed: keep using the last known address rather than stalling.
    if cached:
        with _PUBLIC_DNS_LOCK:
            _PUBLIC_DNS_CACHE[host] = (cached[0], now + 60)
        return cached[0]
    return None


class _PublicDNSHTTPSConnection(HTTPSConnection):
    def _new_conn(self):
        ip = _resolve_public_ipv4(self._dns_host)
        if not ip:
            return super()._new_conn()
        saved = self._dns_host
        self._dns_host = ip
        try:
            return super()._new_conn()
        finally:
            self._dns_host = saved


class _PublicDNSHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _PublicDNSHTTPSConnection


class _PublicDNSAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        self._pool_connections = connections
        self._pool_maxsize = maxsize
        self._pool_block = block
        manager = PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            **pool_kwargs,
        )
        manager.pool_classes_by_scheme = dict(manager.pool_classes_by_scheme)
        manager.pool_classes_by_scheme['https'] = _PublicDNSHTTPSConnectionPool
        self.poolmanager = manager


# Reuse TLS connections; run independent TMDB calls in parallel.
_TMDB_SESSION = requests.Session()
_TMDB_SESSION.mount('https://', _PublicDNSAdapter())
_TMDB_CACHE = {}
_TMDB_CACHE_LOCK = threading.Lock()
_TMDB_CACHE_TTL = 1800  # 30 min for list/discover
_TMDB_DETAIL_TTL = 7200  # 2h for movie/person pages
_HTTP_POOL = ThreadPoolExecutor(max_workers=10)
_JOB_POOL = ThreadPoolExecutor(max_workers=4)

# ------------------ Helpers ------------------ #
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def allowed_community_media(filename):
    if not filename or '.' not in filename:
        return False
    return filename.rsplit('.', 1)[1].lower() in COMMUNITY_MEDIA_EXTENSIONS


def community_media_type(filename):
    ext = filename.rsplit('.', 1)[1].lower()
    if ext in COMMUNITY_VIDEO_EXTENSIONS:
        return 'video'
    if ext in COMMUNITY_IMAGE_EXTENSIONS:
        return 'image'
    return None


def delete_community_media_file(media_url):
    if not media_url or 'community_media/' not in media_url:
        return
    filename = media_url.rsplit('community_media/', 1)[-1].split('?')[0]
    filepath = os.path.join(app.config['COMMUNITY_MEDIA_FOLDER'], filename)
    if os.path.isfile(filepath):
        try:
            os.remove(filepath)
        except OSError:
            pass


def purge_community_seed_data():
    """Remove development seed posts so the feed only shows real user content."""
    seed_posts = CommunityPost.query.filter_by(is_seed=True).all()
    if not seed_posts:
        return 0

    seed_ids = [post.id for post in seed_posts]
    CommunityPostLike.query.filter(CommunityPostLike.post_id.in_(seed_ids)).delete(
        synchronize_session=False,
    )
    CommunitySavedPost.query.filter(CommunitySavedPost.post_id.in_(seed_ids)).delete(
        synchronize_session=False,
    )

    cleanup_tables = (
        'community_comment',
        'community_repost',
        'community_poll_vote',
        'community_poll_option',
        'community_poll',
        'community_notification',
    )
    with db.engine.connect() as conn:
        for table in cleanup_tables:
            try:
                conn.execute(
                    text(f'DELETE FROM {table} WHERE post_id IN :ids'),
                    {'ids': tuple(seed_ids)},
                )
                conn.commit()
            except Exception:
                conn.rollback()

    for post in seed_posts:
        delete_community_media_file(post.media_url)
        db.session.delete(post)

    db.session.commit()
    return len(seed_ids)


def community_posts_query():
    """Community posts created by real users (exclude development seed rows)."""
    return CommunityPost.query.filter(
        db.or_(CommunityPost.is_seed.is_(False), CommunityPost.is_seed.is_(None)),
    )


def _tmdb_cache_key(path, params):
    items = tuple(sorted((str(k), str(v)) for k, v in params.items() if k != 'api_key'))
    return (path, items)


def tmdb_get(path, ttl=None, **params):
    """GET a TMDB v3 endpoint with session reuse + short in-memory cache."""
    if not TMDB_API_KEY:
        return None
    params = dict(params)
    if ttl is None:
        ttl = (
            _TMDB_DETAIL_TTL
            if path.startswith('/movie/') or path.startswith('/person/') or path.startswith('/tv/')
            else _TMDB_CACHE_TTL
        )
    key = _tmdb_cache_key(path, params)
    now = datetime.utcnow()
    stale = None
    with _TMDB_CACHE_LOCK:
        hit = _TMDB_CACHE.get(key)
        if hit and hit['data'] is not None:
            age = (now - hit['at']).total_seconds()
            if age < hit['ttl']:
                return hit['data']
            stale = hit['data']
        elif hit and (now - hit['at']).total_seconds() < hit['ttl']:
            return None  # recent failure: don't hammer TMDB

    # Stale-while-revalidate: answer instantly from the old copy and
    # refresh it in the background so visitors never wait on TMDB.
    if stale is not None:
        _tmdb_refresh_async(key, path, params, ttl)
        return stale

    data = _tmdb_fetch(path, params)
    _tmdb_store(key, data, ttl)
    return data


_TMDB_REFRESHING = set()


def _tmdb_fetch(path, params):
    query = dict(params)
    query['api_key'] = TMDB_API_KEY
    for attempt in range(2):
        try:
            # (connect, read) - fail fast instead of hanging for 8s+ on a bad connection.
            response = _TMDB_SESSION.get(f"{TMDB_BASE}{path}", params=query, timeout=(3.5, 6))
            if response.status_code == 200:
                return response.json()
            if response.status_code == 429:
                time.sleep(0.3)
                continue
            if response.status_code in (500, 502, 503, 504) and attempt == 0:
                continue
            return None
        except (requests.RequestException, ValueError):
            if attempt == 0:
                continue
            return None
    return None


def _tmdb_store(key, data, ttl):
    with _TMDB_CACHE_LOCK:
        if data is None:
            old = _TMDB_CACHE.get(key)
            if old and old['data'] is not None:
                # Keep serving the last good copy; retry again shortly.
                old['at'] = datetime.utcnow() - timedelta(seconds=max(old['ttl'] - 30, 0))
                return
        if len(_TMDB_CACHE) > 1500:
            oldest = sorted(_TMDB_CACHE.items(), key=lambda kv: kv[1]['at'])[:400]
            for old_key, _ in oldest:
                _TMDB_CACHE.pop(old_key, None)
        _TMDB_CACHE[key] = {
            'at': datetime.utcnow(),
            'ttl': ttl if data is not None else 20,
            'data': data,
        }


def _tmdb_refresh_async(key, path, params, ttl):
    with _TMDB_CACHE_LOCK:
        if key in _TMDB_REFRESHING:
            return
        _TMDB_REFRESHING.add(key)

    def _job():
        try:
            _tmdb_store(key, _tmdb_fetch(path, params), ttl)
        finally:
            with _TMDB_CACHE_LOCK:
                _TMDB_REFRESHING.discard(key)

    try:
        _JOB_POOL.submit(_job)
    except RuntimeError:
        with _TMDB_CACHE_LOCK:
            _TMDB_REFRESHING.discard(key)


def tmdb_get_many(calls):
    """Fetch many TMDB endpoints in parallel. calls: [(path, params_dict), ...]."""
    if not calls:
        return []
    if len(calls) == 1:
        path, params = calls[0]
        return [tmdb_get(path, **(params or {}))]
    return list(_HTTP_POOL.map(lambda c: tmdb_get(c[0], **(c[1] or {})), calls))


def tmdb_poster_url(poster_path, size='w500'):
    if not poster_path:
        return 'https://via.placeholder.com/300x450?text=No+Poster'
    if poster_path.startswith('http'):
        return poster_path
    return f"https://image.tmdb.org/t/p/{size}{poster_path}"


def pick_youtube_trailer(videos):
    """Pick the best YouTube clip from a TMDB videos payload."""
    results = (videos or {}).get('results') or []
    youtube = [clip for clip in results if clip.get('site') == 'YouTube' and clip.get('key')]
    if not youtube:
        return None

    type_rank = {
        'Trailer': 0,
        'Teaser': 1,
        'Clip': 2,
        'Featurette': 3,
        'Behind the Scenes': 4,
    }

    def sort_key(clip):
        official = 0 if clip.get('official') else 1
        return (official, type_rank.get(clip.get('type') or '', 9))

    youtube.sort(key=sort_key)
    return f"https://www.youtube.com/watch?v={youtube[0]['key']}"


def youtube_trailer_search_url(title, year=None):
    parts = [title or '', str(year) if year and str(year) != 'N/A' else '', 'official trailer']
    query = ' '.join(part for part in parts if part).strip()
    return f"https://www.youtube.com/results?search_query={quote_plus(query)}"


def fetch_tmdb_videos(media_type, tmdb_id):
    """Collect videos across language variants so trailers are less often missed."""
    if not tmdb_id or media_type not in ('movie', 'tv'):
        return {'results': []}
    collected = []
    seen_keys = set()
    all_langs = 'en,hi,ta,te,ml,kn,bn,mr,pa,gu,ur,ko,ja,zh,fr,es,de,it,pt,tr,th,ar,ru,null'
    for params in ({'language': 'en-US'}, {'include_video_language': all_langs}, {}):
        payload = tmdb_get(f'/{media_type}/{tmdb_id}/videos', **params) or {}
        for clip in payload.get('results') or []:
            key = clip.get('key')
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            collected.append(clip)
    return {'results': collected}


def resolve_trailer_url(media_type, tmdb_id, title, year, videos=None):
    """Always return a trailer URL: TMDB YouTube clip, or YouTube search fallback."""
    url = pick_youtube_trailer(videos)
    if url:
        return url
    url = pick_youtube_trailer(fetch_tmdb_videos(media_type, tmdb_id))
    if url:
        return url
    return youtube_trailer_search_url(title, year)


def person_initials(name):
    """Two-letter initials for placeholder avatars."""
    words = [w for w in re.split(r'[\s.\-]+', (name or '').strip()) if w]
    if not words:
        return '?'
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[-1][0]).upper()


def format_runtime_short(minutes):
    """142 -> '2h 22m'; 45 -> '45m'; 120 -> '2h'. Empty string when unknown."""
    try:
        total = int(minutes)
    except (TypeError, ValueError):
        return ''
    if total <= 0:
        return ''
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f'{hours}h {mins}m'
    if hours:
        return f'{hours}h'
    return f'{mins}m'


def pick_certification(release_dates, regions=('IN', 'US')):
    """Age rating (UA, A, PG-13...) from TMDB release_dates, India first."""
    results = (release_dates or {}).get('results') or []
    by_region = {}
    for entry in results:
        code = entry.get('iso_3166_1')
        if code:
            by_region[code] = entry.get('release_dates') or []
    for code in list(regions) + [c for c in by_region if c not in regions]:
        items = sorted(by_region.get(code) or [], key=lambda item: 0 if item.get('type') == 3 else 1)
        for item in items:
            cert = (item.get('certification') or '').strip()
            if cert:
                return cert
    return ''


def pick_title_logo(images):
    """Best English / language-neutral title logo from TMDB images, or None."""
    logos = [
        logo for logo in (images or {}).get('logos') or []
        if logo.get('file_path') and (logo.get('iso_639_1') or None) in ('en', None)
    ]
    if not logos:
        return None
    logos.sort(key=lambda logo: (
        0 if logo.get('iso_639_1') == 'en' else 1,
        -(logo.get('vote_average') or 0),
        -(logo.get('width') or 0),
    ))
    return tmdb_poster_url(logos[0]['file_path'], size='w500')


CREW_JOB_PRIORITY = (
    'Director', 'Screenplay', 'Writer', 'Story', 'Novel',
    'Director of Photography', 'Original Music Composer', 'Music',
    'Producer', 'Executive Producer', 'Editor', 'Costume Design', 'Production Design',
)


def build_cast_and_crew(credits, cast_limit=16, crew_limit=12):
    """Cast cards plus crew cards merged per person (one card, combined roles)."""
    credits = credits or {}

    def photo_for(person):
        path = person.get('profile_path')
        return tmdb_poster_url(path, size='w185') if path else None

    cast = []
    seen_cast = set()
    for person in credits.get('cast') or []:
        pid = person.get('id')
        if not pid or pid in seen_cast:
            continue
        seen_cast.add(pid)
        name = person.get('name') or 'Unknown'
        cast.append({
            'id': str(pid),
            'name': name,
            'character': person.get('character') or '',
            'photo': photo_for(person),
            'initials': person_initials(name),
        })
        if len(cast) >= cast_limit:
            break

    rank = {job: i for i, job in enumerate(CREW_JOB_PRIORITY)}
    people = {}
    for person in credits.get('crew') or []:
        pid = person.get('id')
        job = (person.get('job') or '').strip()
        if not pid or not job:
            continue
        entry = people.get(pid)
        if entry is None:
            name = person.get('name') or 'Unknown'
            entry = people[pid] = {
                'id': str(pid),
                'name': name,
                'photo': photo_for(person),
                'initials': person_initials(name),
                'roles': [],
            }
        if not entry['photo']:
            entry['photo'] = photo_for(person)
        if job not in entry['roles']:
            entry['roles'].append(job)

    def entry_rank(entry):
        ranks = [rank[r] for r in entry['roles'] if r in rank]
        return min(ranks) if ranks else len(rank)

    ordered = sorted(people.values(), key=lambda e: (entry_rank(e), e['name']))
    preferred = [e for e in ordered if entry_rank(e) < len(rank)]
    others = [e for e in ordered if entry_rank(e) >= len(rank)]
    picked = preferred[:crew_limit] if len(preferred) >= 6 else (preferred + others)[:crew_limit]

    crew = []
    for entry in picked:
        roles = sorted(entry['roles'], key=lambda r: (rank.get(r, 99), r))
        shown = [r for r in roles if r in rank] or roles[:2]
        crew.append({
            'id': entry['id'],
            'name': entry['name'],
            'photo': entry['photo'],
            'initials': entry['initials'],
            'roles': shown,
            'job': ' \u00b7 '.join(shown[:4]),
        })
    return cast, crew


LANGUAGE_FALLBACK_NAMES = {
    'en': 'English', 'hi': 'Hindi', 'ta': 'Tamil', 'te': 'Telugu', 'ml': 'Malayalam',
    'kn': 'Kannada', 'bn': 'Bengali', 'mr': 'Marathi', 'pa': 'Punjabi', 'gu': 'Gujarati',
    'ur': 'Urdu', 'ja': 'Japanese', 'ko': 'Korean', 'zh': 'Chinese', 'cn': 'Cantonese',
    'fr': 'French', 'es': 'Spanish', 'pt': 'Portuguese', 'de': 'German', 'it': 'Italian',
    'tr': 'Turkish', 'th': 'Thai', 'ar': 'Arabic', 'ru': 'Russian',
}


def pick_language_name(data):
    """'ta' -> 'Tamil'. Empty string when TMDB has no original language."""
    code = ((data or {}).get('original_language') or '').strip().lower()
    if not code:
        return ''
    for lang in (data or {}).get('spoken_languages') or []:
        if (lang.get('iso_639_1') or '').lower() == code and lang.get('english_name'):
            return lang['english_name']
    return LANGUAGE_FALLBACK_NAMES.get(code, code.upper())


def pick_writers(credits, limit=3):
    """Names of writers/screenplay credits (falls back to story credits)."""
    crew = (credits or {}).get('crew') or []
    for jobs in (('Writer', 'Screenplay'), ('Story', 'Novel')):
        names = []
        for person in crew:
            name = person.get('name')
            if person.get('job') in jobs and name and name not in names:
                names.append(name)
        if names:
            return ', '.join(names[:limit])
    return ''


def build_people_preview(cast, crew, limit=6, cast_slots=4):
    """A short Cast & Crew strip: top cast, then key crew, one entry per person (roles combined)."""
    merged = {}
    order = []

    def add(person, role):
        entry = merged.get(person['id'])
        if entry is None:
            entry = merged[person['id']] = {
                'id': person['id'],
                'name': person['name'],
                'photo': person.get('photo'),
                'initials': person.get('initials') or person_initials(person['name']),
                'roles': [],
            }
            order.append(entry)
        if not entry['photo'] and person.get('photo'):
            entry['photo'] = person['photo']
        if role and role not in entry['roles']:
            entry['roles'].append(role)

    for person in cast[:cast_slots]:
        add(person, person.get('character') or 'Cast')
    for person in crew:
        if person['id'] in merged:
            add(person, person.get('job') or '')
        elif len(order) < limit:
            add(person, person.get('job') or '')
    for person in cast[cast_slots:]:
        if len(order) >= limit:
            break
        add(person, person.get('character') or 'Cast')

    for entry in order:
        entry['role'] = ' \u00b7 '.join(entry['roles'][:2])
    return order[:limit]


VIDEO_TYPE_RANK = {'Trailer': 0, 'Teaser': 1, 'Clip': 2, 'Featurette': 3, 'Behind the Scenes': 4}


def build_media_videos(videos):
    """YouTube videos for the Media card. group: 'trailers' (Trailer, Teaser) or 'clips' (the rest).

    TMDB does not return durations, so none are invented here.
    """
    items = []
    seen = set()
    for clip in (videos or {}).get('results') or []:
        key = clip.get('key')
        if clip.get('site') != 'YouTube' or not key or key in seen:
            continue
        seen.add(key)
        kind = clip.get('type') or 'Clip'
        items.append({
            'key': key,
            'name': clip.get('name') or kind,
            'type': kind,
            'group': 'trailers' if kind in ('Trailer', 'Teaser') else 'clips',
            'official': bool(clip.get('official')),
            'url': f'https://www.youtube.com/watch?v={key}',
            'thumb': f'https://img.youtube.com/vi/{key}/hqdefault.jpg',
        })
    items.sort(key=lambda item: (VIDEO_TYPE_RANK.get(item['type'], 9), 0 if item['official'] else 1))
    return items


def normalize_tmdb_movie(data, credits=None, videos=None, recommendations=None):
    """Map TMDB movie JSON into the template-friendly shape used across the app."""
    if not data or not data.get('id'):
        return None

    release = data.get('release_date') or ''
    year = release[:4] if release else 'N/A'
    genres = data.get('genres') or []
    genre_names = ', '.join(g.get('name', '') for g in genres if g.get('name')) or 'N/A'

    credits = credits or data.get('credits') or {}
    director = 'N/A'
    for person in credits.get('crew') or []:
        if person.get('job') == 'Director':
            director = person.get('name', 'N/A')
            break

    cast, crew = build_cast_and_crew(credits)

    trailer_url = resolve_trailer_url(
        'movie',
        str(data['id']),
        data.get('title') or data.get('original_title') or 'Untitled',
        year,
        videos or data.get('videos'),
    )

    similar = []
    for item in ((recommendations or data.get('recommendations') or {}).get('results') or [])[:12]:
        if not item.get('id'):
            continue
        release_item = item.get('release_date') or ''
        similar.append({
            'Title': item.get('title') or item.get('original_title') or 'Untitled',
            'Year': release_item[:4] if release_item else 'N/A',
            'Poster': tmdb_poster_url(item.get('poster_path')),
            'imdbID': str(item['id']),
            'tmdb_id': str(item['id']),
        })

    runtime = data.get('runtime')
    runtime_label = f"{runtime} min" if runtime else 'N/A'
    vote = data.get('vote_average')
    tmdb_id = str(data['id'])

    release_label = None
    release_short = None
    if release:
        try:
            parsed_release = datetime.strptime(release, '%Y-%m-%d')
            release_label = parsed_release.strftime('%B %d, %Y')
            release_short = parsed_release.strftime('%d %b %Y')
        except ValueError:
            release_label = release
            release_short = release

    countries = [c.get('name') for c in (data.get('production_countries') or []) if c.get('name')]
    media_videos = build_media_videos(videos or data.get('videos'))

    genre_list = [g.get('name') for g in genres if g.get('name')]
    certification = pick_certification(data.get('release_dates'))
    runtime_short = format_runtime_short(runtime)
    meta_parts = []
    if year and year != 'N/A':
        meta_parts.append(year)
    meta_parts.extend(genre_list[:3])
    if certification:
        meta_parts.append(certification)
    if runtime_short:
        meta_parts.append(runtime_short)

    images = data.get('images') or {}
    backdrop_path = data.get('backdrop_path')
    backdrop_gallery = [
        {
            'thumb': tmdb_poster_url(item['file_path'], size='w780'),
            'full': tmdb_poster_url(item['file_path'], size='w1280'),
        }
        for item in (images.get('backdrops') or [])[:40]
        if item.get('file_path')
    ]
    poster_gallery = [
        {
            'thumb': tmdb_poster_url(item['file_path'], size='w342'),
            'full': tmdb_poster_url(item['file_path'], size='w780'),
        }
        for item in (images.get('posters') or [])[:30]
        if item.get('file_path')
    ]

    return {
        'Title': data.get('title') or data.get('original_title') or 'Untitled',
        'Year': year,
        'ReleaseDate': release,
        'ReleaseLabel': release_label,
        'Status': data.get('status') or '',
        'Poster': tmdb_poster_url(data.get('poster_path')),
        'Plot': data.get('overview') or 'No plot available.',
        'Genre': genre_names,
        'Director': director,
        'Runtime': runtime_label,
        'imdbRating': f"{vote:.1f}" if isinstance(vote, (int, float)) else 'N/A',
        'imdbID': tmdb_id,
        'tmdb_id': tmdb_id,
        'Backdrop': tmdb_poster_url(data.get('backdrop_path'), size='original') if data.get('backdrop_path') else None,
        'Cast': cast,
        'Crew': crew,
        'Trailer': trailer_url,
        'Similar': similar,
        'Tagline': (data.get('tagline') or '').strip(),
        'Logo': pick_title_logo(images),
        'Certification': certification,
        'RuntimeShort': runtime_short,
        'GenreList': genre_list,
        'MetaParts': meta_parts,
        'BackdropW780': tmdb_poster_url(backdrop_path, size='w780') if backdrop_path else None,
        'BackdropW1280': tmdb_poster_url(backdrop_path, size='w1280') if backdrop_path else None,
        'Backdrops': backdrop_gallery,
        'Posters': poster_gallery,
        'Language': pick_language_name(data),
        'Country': ', '.join(countries[:2]),
        'Writers': pick_writers(credits),
        'ReleaseShort': release_short,
        'PeoplePreview': build_people_preview(cast, crew, limit=12, cast_slots=8),
        'Videos': media_videos,
    }


def fetch_watch_providers(tmdb_id, regions=('IN', 'US'), media_type='movie'):
    """OTT / rent / buy availability from TMDB (JustWatch). Prefer India, then US."""
    path_type = 'tv' if media_type == 'tv' else 'movie'
    data = tmdb_get(f'/{path_type}/{tmdb_id}/watch/providers')
    if not data:
        return None

    results = data.get('results') or {}
    region = next((code for code in regions if code in results), None)
    if not region and results:
        region = next(iter(results))
    if not region:
        return {
            'region': None,
            'stream': [],
            'free': [],
            'ads': [],
            'rent': [],
            'buy': [],
            'link': None,
        }

    block = results.get(region) or {}

    def map_providers(items):
        providers = []
        for item in items or []:
            logo = item.get('logo_path')
            providers.append({
                'name': item.get('provider_name') or 'Unknown',
                'logo': tmdb_poster_url(logo, size='w92') if logo else None,
            })
        return providers

    return {
        'region': region,
        'stream': map_providers(block.get('flatrate')),
        'free': map_providers(block.get('free')),
        'ads': map_providers(block.get('ads')),
        'rent': map_providers(block.get('rent')),
        'buy': map_providers(block.get('buy')),
        'link': block.get('link'),
    }


def _provider_base_name(name):
    """'Amazon Prime Video with Ads' -> 'Amazon Prime Video' so one service = one card."""
    cleaned = re.sub(r'\s+(standard\s+)?with\s+ads$', '', (name or '').strip(), flags=re.I)
    cleaned = re.sub(r'\s+ad[-\s]?supported$', '', cleaned, flags=re.I)
    return cleaned.strip() or (name or '').strip()


def build_watch_online_cards(watch_providers, limit=8):
    """Flatten TMDB watch providers into one card per streaming service."""
    if not watch_providers:
        return []

    link = watch_providers.get('link') or '#'
    buckets = (
        ('stream', 'Subscription'),
        ('free', 'Free'),
        ('ads', 'Free with ads'),
        ('rent', 'Rent'),
        ('buy', 'Buy'),
    )
    groups = {}
    for key, label in buckets:
        for provider in watch_providers.get(key) or []:
            raw_name = (provider.get('name') or '').strip()
            if not raw_name:
                continue
            base = _provider_base_name(raw_name)
            group = groups.get(base.lower())
            if group is None:
                group = groups[base.lower()] = {
                    'name': base,
                    'logo': provider.get('logo'),
                    'labels': [],
                    'url': link,
                }
            elif not group['logo'] and provider.get('logo'):
                group['logo'] = provider.get('logo')
            if label not in group['labels']:
                group['labels'].append(label)

    cards = [
        {
            'name': group['name'],
            'logo': group['logo'],
            'label': ' \u00b7 '.join(group['labels']),
            'url': group['url'],
        }
        for group in groups.values()
    ]
    return cards[:limit]


# Preferred JustWatch regions when looking up OTT logos for a language.
LANG_WATCH_REGIONS = {
    'hi': ('IN', 'US'),
    'ta': ('IN', 'US'),
    'te': ('IN', 'US'),
    'ml': ('IN', 'US'),
    'kn': ('IN', 'US'),
    'bn': ('IN', 'US'),
    'mr': ('IN', 'US'),
    'pa': ('IN', 'US'),
    'gu': ('IN', 'US'),
    'ur': ('IN', 'US'),
    'en': ('US', 'GB', 'IN'),
    'ja': ('JP', 'US'),
    'ko': ('KR', 'US'),
    'zh': ('HK', 'TW', 'US'),
    'cn': ('HK', 'TW', 'US'),
    'fr': ('FR', 'US'),
    'es': ('ES', 'MX', 'US'),
    'pt': ('BR', 'PT', 'US'),
    'de': ('DE', 'US'),
    'it': ('IT', 'US'),
    'tr': ('TR', 'US'),
    'th': ('TH', 'US'),
    'ar': ('AE', 'SA', 'US'),
}


def fetch_stream_logos(tmdb_id, regions=('IN', 'US'), limit=4, media_type='movie'):
    """Compact stream-only provider logos for schedule OTT cards."""
    info = fetch_watch_providers(tmdb_id, regions=regions, media_type=media_type)
    if not info:
        return []
    logos = []
    for provider in info.get('stream') or []:
        if not provider.get('logo'):
            continue
        logos.append({
            'name': provider['name'],
            'logo': provider['logo'],
        })
        if len(logos) >= limit:
            break
    return logos


def attach_ott_logos(cards, lang_code=None, max_cards=16):
    """Add stream platform logos onto OTT schedule cards (capped + parallel)."""
    regions = LANG_WATCH_REGIONS.get((lang_code or '').lower(), ('IN', 'US'))
    subset = list(cards[:max_cards])

    def one(card):
        media = 'tv' if (card.get('media') or card.get('media_type')) == 'tv' else 'movie'
        card['ott_logos'] = fetch_stream_logos(card.get('id'), regions=regions, media_type=media)
        return card

    if len(subset) == 1:
        one(subset[0])
    elif subset:
        list(_HTTP_POOL.map(one, subset))
    for card in cards[max_cards:]:
        card.setdefault('ott_logos', [])
    return cards


def fetch_movie_by_id(tmdb_id):
    if not tmdb_id:
        return None
    data = tmdb_get(
        f'/movie/{tmdb_id}',
        append_to_response='credits,videos,recommendations,images,release_dates',
        include_image_language='en,null',
    )
    # Heavy append_to_response can fail intermittently — fall back to separate calls
    if not data:
        data = tmdb_get(f'/movie/{tmdb_id}')
    if not data:
        return None

    credits = data.get('credits')
    videos = data.get('videos')
    recommendations = data.get('recommendations')
    if not credits or not videos or not recommendations:
        extras = tmdb_get_many([
            (f'/movie/{tmdb_id}/credits', {}),
            (f'/movie/{tmdb_id}/videos', {}),
            (f'/movie/{tmdb_id}/recommendations', {}),
        ])
        credits = credits or extras[0] or {}
        videos = videos or extras[1] or {}
        recommendations = recommendations or extras[2] or {}

    return normalize_tmdb_movie(
        data,
        credits=credits,
        videos=videos,
        recommendations=recommendations,
    )


def fetch_person_by_id(person_id):
    """TMDB person profile + movie filmography for the cast/crew detail page."""
    if not person_id:
        return None
    data = tmdb_get(
        f'/person/{person_id}',
        append_to_response='movie_credits',
        language='en-US',
    )
    if not data or not data.get('id'):
        profile, credits = tmdb_get_many([
            (f'/person/{person_id}', {'language': 'en-US'}),
            (f'/person/{person_id}/movie_credits', {'language': 'en-US'}),
        ])
        if not profile or not profile.get('id'):
            return None
        data = dict(profile)
        data['movie_credits'] = credits or {}

    credits = data.get('movie_credits') or {}
    films_by_id = {}

    for credit in credits.get('cast') or []:
        mid = credit.get('id')
        if not mid:
            continue
        mid = str(mid)
        release = credit.get('release_date') or ''
        role = credit.get('character') or 'Cast'
        entry = films_by_id.get(mid)
        if entry:
            if role and role not in entry['roles']:
                entry['roles'].append(role)
            continue
        films_by_id[mid] = {
            'id': mid,
            'title': credit.get('title') or credit.get('original_title') or 'Untitled',
            'year': release[:4] if release else '',
            'release_date': release,
            'poster': tmdb_poster_url(credit.get('poster_path')) if credit.get('poster_path') else None,
            'roles': [role] if role else ['Cast'],
            'department': 'Acting',
            'popularity': credit.get('popularity') or 0,
        }

    for credit in credits.get('crew') or []:
        mid = credit.get('id')
        if not mid:
            continue
        mid = str(mid)
        release = credit.get('release_date') or ''
        job = credit.get('job') or credit.get('department') or 'Crew'
        entry = films_by_id.get(mid)
        if entry:
            if job and job not in entry['roles']:
                entry['roles'].append(job)
            continue
        films_by_id[mid] = {
            'id': mid,
            'title': credit.get('title') or credit.get('original_title') or 'Untitled',
            'year': release[:4] if release else '',
            'release_date': release,
            'poster': tmdb_poster_url(credit.get('poster_path')) if credit.get('poster_path') else None,
            'roles': [job],
            'department': credit.get('department') or 'Crew',
            'popularity': credit.get('popularity') or 0,
        }

    filmography = list(films_by_id.values())
    filmography.sort(
        key=lambda f: (f.get('release_date') or '', f.get('popularity') or 0),
        reverse=True,
    )

    birthday = data.get('birthday') or ''
    deathday = data.get('deathday') or ''
    birthday_label = None
    if birthday:
        try:
            birthday_label = datetime.strptime(birthday, '%Y-%m-%d').strftime('%B %d, %Y')
        except ValueError:
            birthday_label = birthday

    return {
        'id': str(data['id']),
        'name': data.get('name') or 'Unknown',
        'bio': (data.get('biography') or '').strip(),
        'birthday': birthday,
        'birthday_label': birthday_label,
        'deathday': deathday,
        'place_of_birth': data.get('place_of_birth') or '',
        'known_for': data.get('known_for_department') or '',
        'photo': tmdb_poster_url(data.get('profile_path'), size='w500') if data.get('profile_path') else None,
        'filmography': filmography,
        'film_count': len(filmography),
    }


def fetch_movie_by_title(title):
    if not title:
        return None
    results = search_tmdb_movies(title, limit=1)
    if not results:
        return None
    return fetch_movie_by_id(results[0]['imdbID'])


def search_tmdb_movies(query, limit=8):
    if not query:
        return []
    data = tmdb_get('/search/movie', query=query, include_adult='false')
    if not data:
        return []
    movies = []
    for item in data.get('results', [])[:limit]:
        release = item.get('release_date') or ''
        movies.append({
            'Title': item.get('title') or item.get('original_title') or 'Untitled',
            'Year': release[:4] if release else 'N/A',
            'Poster': tmdb_poster_url(item.get('poster_path')),
            'Plot': item.get('overview') or '',
            'Genre': 'N/A',
            'Director': 'N/A',
            'Runtime': 'N/A',
            'imdbRating': f"{item['vote_average']:.1f}" if item.get('vote_average') is not None else 'N/A',
            'imdbID': str(item['id']),
            'tmdb_id': str(item['id']),
        })
    return movies


def search_tmdb_series(query, limit=12):
    """Search TMDB TV shows only — same shape as film search results."""
    if not query:
        return []
    data = tmdb_get('/search/tv', query=query, include_adult='false')
    if not data:
        return []
    shows = []
    for item in data.get('results', [])[:limit]:
        first_air = item.get('first_air_date') or ''
        vote = item.get('vote_average')
        tmdb_id = str(item['id'])
        shows.append({
            'Title': item.get('name') or item.get('original_name') or 'Untitled',
            'Year': first_air[:4] if first_air else 'N/A',
            'Poster': tmdb_poster_url(item.get('poster_path')),
            'Plot': item.get('overview') or '',
            'Genre': 'TV Series',
            'Runtime': 'N/A',
            'FirstAir': first_air if first_air else 'N/A',
            'imdbRating': f"{vote:.1f}" if isinstance(vote, (int, float)) else 'N/A',
            'imdbID': tmdb_id,
            'tmdb_id': tmdb_id,
        })
    return shows


def normalize_tmdb_tv(data):
    """Map TMDB TV JSON into a template-friendly shape."""
    if not data or not data.get('id'):
        return None

    first_air = data.get('first_air_date') or ''
    year = first_air[:4] if first_air else 'N/A'
    genre_names = ', '.join(
        g.get('name', '') for g in (data.get('genres') or []) if g.get('name')
    ) or 'N/A'

    seasons = []
    for season in data.get('seasons') or []:
        season_number = season.get('season_number')
        if season_number is None or season_number < 1:
            continue
        seasons.append({
            'number': season_number,
            'name': season.get('name') or f'Season {season_number}',
            'episode_count': season.get('episode_count') or 0,
            'poster': tmdb_poster_url(season.get('poster_path')) if season.get('poster_path') else None,
        })

    vote = data.get('vote_average')
    tmdb_id = str(data['id'])
    next_ep = data.get('next_episode_to_air') or {}

    trailer_url = resolve_trailer_url(
        'tv',
        tmdb_id,
        data.get('name') or data.get('original_name') or 'Untitled',
        year,
        data.get('videos'),
    )

    # Cast, crew, media, similar shows and the "meta" strip used by the redesigned detail page.
    credits = data.get('credits') or {}
    cast, crew = build_cast_and_crew(credits)
    genre_list = [g.get('name') for g in (data.get('genres') or []) if g.get('name')]
    certification = ''
    for entry in (data.get('content_ratings') or {}).get('results') or []:
        if entry.get('iso_3166_1') in ('IN', 'US') and (entry.get('rating') or '').strip():
            certification = entry['rating'].strip()
            if entry['iso_3166_1'] == 'IN':
                break

    runtimes = [r for r in (data.get('episode_run_time') or []) if r]
    ep_runtime = runtimes[0] if runtimes else None
    if not ep_runtime and (data.get('last_episode_to_air') or {}).get('runtime'):
        ep_runtime = data['last_episode_to_air']['runtime']
    runtime_short = format_runtime_short(ep_runtime)

    season_count = data.get('number_of_seasons') or len(seasons)
    meta_parts = []
    if year and year != 'N/A':
        meta_parts.append(year)
    meta_parts.extend(genre_list[:3])
    if certification:
        meta_parts.append(certification)
    if season_count:
        meta_parts.append(f"{season_count} season{'' if season_count == 1 else 's'}")
    if runtime_short:
        meta_parts.append(runtime_short)

    first_air_short = None
    if first_air:
        try:
            first_air_short = datetime.strptime(first_air, '%Y-%m-%d').strftime('%d %b %Y')
        except ValueError:
            first_air_short = first_air

    similar = []
    for item in ((data.get('recommendations') or {}).get('results') or [])[:12]:
        if not item.get('id'):
            continue
        item_air = item.get('first_air_date') or ''
        similar.append({
            'Title': item.get('name') or item.get('original_name') or 'Untitled',
            'Year': item_air[:4] if item_air else 'N/A',
            'Poster': tmdb_poster_url(item.get('poster_path')),
            'tmdb_id': str(item['id']),
        })

    images = data.get('images') or {}
    backdrop_path = data.get('backdrop_path')
    backdrop_gallery = [
        {
            'thumb': tmdb_poster_url(item['file_path'], size='w780'),
            'full': tmdb_poster_url(item['file_path'], size='w1280'),
        }
        for item in (images.get('backdrops') or [])[:40]
        if item.get('file_path')
    ]
    poster_gallery = [
        {
            'thumb': tmdb_poster_url(item['file_path'], size='w342'),
            'full': tmdb_poster_url(item['file_path'], size='w780'),
        }
        for item in (images.get('posters') or [])[:30]
        if item.get('file_path')
    ]
    creators = ', '.join(c.get('name') for c in (data.get('created_by') or [])[:3] if c.get('name'))
    networks = ', '.join(n.get('name') for n in (data.get('networks') or [])[:2] if n.get('name'))
    countries = [c for c in (data.get('origin_country') or []) if c]
    if not countries:
        countries = [c.get('name') for c in (data.get('production_countries') or []) if c.get('name')]

    return {
        'Cast': cast,
        'Crew': crew,
        'PeoplePreview': build_people_preview(cast, crew, limit=12, cast_slots=8),
        'Similar': similar,
        'Videos': build_media_videos(data.get('videos')),
        'Backdrops': backdrop_gallery,
        'Posters': poster_gallery,
        'BackdropW780': tmdb_poster_url(backdrop_path, size='w780') if backdrop_path else None,
        'BackdropW1280': tmdb_poster_url(backdrop_path, size='w1280') if backdrop_path else None,
        'Logo': pick_title_logo(images),
        'Tagline': (data.get('tagline') or '').strip(),
        'GenreList': genre_list,
        'MetaParts': meta_parts,
        'Certification': certification,
        'RuntimeShort': runtime_short,
        'Language': pick_language_name(data),
        'Country': ', '.join(countries[:2]),
        'Creators': creators,
        'Networks': networks,
        'FirstAirShort': first_air_short,
        'episode_total': data.get('number_of_episodes') or 0,
        'Title': data.get('name') or data.get('original_name') or 'Untitled',
        'Year': year,
        'FirstAirDate': first_air,
        'NextAirDate': next_ep.get('air_date') or '',
        'Poster': tmdb_poster_url(data.get('poster_path')),
        'Backdrop': tmdb_poster_url(data.get('backdrop_path'), size='original') if data.get('backdrop_path') else None,
        'Plot': data.get('overview') or 'No overview available.',
        'Genre': genre_names,
        'imdbRating': f"{vote:.1f}" if isinstance(vote, (int, float)) else 'N/A',
        'tmdb_id': tmdb_id,
        'number_of_seasons': data.get('number_of_seasons') or len(seasons),
        'seasons': seasons,
        'status': data.get('status') or '',
        'Trailer': trailer_url,
    }


def fetch_tv_by_id(tmdb_id):
    if not tmdb_id:
        return None
    data = tmdb_get(
        f'/tv/{tmdb_id}',
        append_to_response='videos,credits,recommendations,images,content_ratings',
        include_image_language='en,null',
        language='en-US',
    )
    if not data:
        data = tmdb_get(f'/tv/{tmdb_id}', append_to_response='videos')
    if not data:
        data = tmdb_get(f'/tv/{tmdb_id}')
    if not data:
        return None
    # Heavy append_to_response can fail intermittently: fill gaps with separate calls.
    if not data.get('credits') or not data.get('recommendations'):
        extras = tmdb_get_many([
            (f'/tv/{tmdb_id}/credits', {'language': 'en-US'}),
            (f'/tv/{tmdb_id}/recommendations', {'language': 'en-US'}),
        ])
        data['credits'] = data.get('credits') or extras[0] or {}
        data['recommendations'] = data.get('recommendations') or extras[1] or {}
    # If videos were missing from the primary payload, fetch them separately
    if not (data.get('videos') or {}).get('results'):
        videos = tmdb_get(f'/tv/{tmdb_id}/videos', language='en-US') or tmdb_get(f'/tv/{tmdb_id}/videos')
        if videos:
            data['videos'] = videos
    return normalize_tmdb_tv(data)


def fetch_tv_season(tmdb_id, season_number):
    data = tmdb_get(f'/tv/{tmdb_id}/season/{season_number}', language='en-US')
    if not data:
        return None
    episodes = []
    for item in data.get('episodes') or []:
        ep_num = item.get('episode_number')
        if ep_num is None:
            continue
        air = item.get('air_date') or ''
        episodes.append({
            'number': ep_num,
            'title': item.get('name') or f'Episode {ep_num}',
            'overview': (item.get('overview') or '')[:220],
            'air_date': air,
            'air_label': _format_schedule_date(air) if air else 'TBA',
            'still': tmdb_poster_url(item.get('still_path')) if item.get('still_path') else None,
            'vote_average': item.get('vote_average'),
        })
    return {
        'number': season_number,
        'name': data.get('name') or f'Season {season_number}',
        'episodes': episodes,
    }


def fetch_series_catalog(limit=24):
    """Popular, trending, and on-air TV for the Series browse page."""
    payloads = tmdb_get_many([
        ('/trending/tv/week', {'language': 'en-US'}),
        ('/tv/popular', {'language': 'en-US', 'page': 1}),
        ('/tv/on_the_air', {'language': 'en-US', 'page': 1}),
    ])
    cards = []
    seen = set()
    for payload in payloads:
        for item in (payload or {}).get('results') or []:
            tid = str(item.get('id'))
            if not tid or tid in seen:
                continue
            seen.add(tid)
            cards.append(_schedule_card(item, kind='ott', media='tv'))
    cards.sort(key=lambda c: (-c['popularity'], c['title']))
    return cards[:limit]


def user_episode_ratings(user_id, series_tmdb_id):
    if not user_id:
        return {}
    rows = EpisodeRating.query.filter_by(
        user_id=user_id,
        series_tmdb_id=str(series_tmdb_id),
    ).all()
    return {(row.season_number, row.episode_number): row.rating for row in rows}


def user_series_overall_rating(user_id, series_tmdb_id):
    ratings = list(user_episode_ratings(user_id, series_tmdb_id).values())
    if not ratings:
        return None, 0
    return round(sum(ratings) / len(ratings), 1), len(ratings)


def _parse_release_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def _format_schedule_date(value):
    parsed = value if isinstance(value, date) else _parse_release_date(value)
    if not parsed:
        return 'TBA'
    return parsed.strftime('%b %d, %Y')


UPCOMING_MOVIE_STATUSES = {
    'rumored', 'planned', 'in production', 'post production',
}
UPCOMING_SERIES_STATUSES = {
    'planned', 'in production', 'returning series', 'pilot',
}
RELEASED_MOVIE_STATUSES = {'released', 'canceled'}
ENDED_SERIES_STATUSES = {'ended', 'canceled', 'cancelled'}


def get_upcoming_catalog_index():
    """Cached TMDB ids for titles in the release schedule / upcoming lists."""
    cached = getattr(get_upcoming_catalog_index, '_cache', None)
    now = datetime.utcnow()
    if cached and (now - cached['at']).total_seconds() < 1800:
        return cached['data']

    movies = set()
    tv = set()

    try:
        schedule = fetch_release_schedule()
        for key in ('upcoming', 'ott_upcoming', 'announced', 'ott_announced', 'tv_upcoming', 'tv_announced'):
            for card in schedule.get(key) or []:
                media = card.get('media') or 'movie'
                item_id = str(card.get('id') or '')
                if not item_id:
                    continue
                if media == 'tv':
                    tv.add(item_id)
                else:
                    movies.add(item_id)
    except Exception:
        pass

    if TMDB_API_KEY:
        today_iso = date.today().isoformat()
        for page in range(1, 5):
            payload = tmdb_get(
                '/discover/movie',
                language='en-US',
                sort_by='popularity.desc',
                **{'primary_release_date.gte': today_iso, 'page': page},
            )
            for item in (payload or {}).get('results') or []:
                if item.get('id'):
                    movies.add(str(item['id']))

        for page in (1, 2, 3):
            payload = tmdb_get('/movie/upcoming', language='en-US', page=page)
            for item in (payload or {}).get('results') or []:
                if item.get('id'):
                    movies.add(str(item['id']))

        for page in range(1, 4):
            payload = tmdb_get(
                '/discover/tv',
                language='en-US',
                sort_by='popularity.desc',
                **{'first_air_date.gte': today_iso, 'page': page},
            )
            for item in (payload or {}).get('results') or []:
                if item.get('id'):
                    tv.add(str(item['id']))

    index = {'movie': movies, 'tv': tv}
    get_upcoming_catalog_index._cache = {'at': now, 'data': index}
    return index


def is_in_upcoming_catalog(tmdb_id, media_type):
    if not tmdb_id:
        return False
    index = get_upcoming_catalog_index()
    bucket_key = 'tv' if media_type == 'tv' else 'movie'
    return str(tmdb_id) in index.get(bucket_key, set())


def is_upcoming_movie(movie, tmdb_id=None):
    """True when the film has not released yet."""
    if not movie:
        return False

    tmdb_id = str(tmdb_id or movie.get('tmdb_id') or movie.get('imdbID') or '')
    today = date.today()

    status = (movie.get('Status') or '').strip().lower()
    if status == 'canceled':
        return False

    release = _parse_release_date(movie.get('ReleaseDate'))
    if release:
        return release >= today

    if status in RELEASED_MOVIE_STATUSES:
        return False
    if status in UPCOMING_MOVIE_STATUSES:
        return True

    if tmdb_id and is_in_upcoming_catalog(tmdb_id, 'movie'):
        return True

    year = movie.get('Year')
    if year and year != 'N/A':
        try:
            year_num = int(year)
            if year_num > today.year:
                return True
            if year_num >= today.year and not release:
                if status in UPCOMING_MOVIE_STATUSES:
                    return True
                if tmdb_id and is_in_upcoming_catalog(tmdb_id, 'movie'):
                    return True
        except (TypeError, ValueError):
            pass

    return False


def is_upcoming_series(show, tmdb_id=None):
    """True when the series is planned, in production, or has future air dates."""
    if not show:
        return False

    tmdb_id = str(tmdb_id or show.get('tmdb_id') or '')
    today = date.today()
    status = (show.get('status') or '').strip().lower()

    if status in ENDED_SERIES_STATUSES:
        return False

    if status in UPCOMING_SERIES_STATUSES:
        first_air = _parse_release_date(show.get('FirstAirDate'))
        next_air = _parse_release_date(show.get('NextAirDate'))
        if status in ('planned', 'in production', 'pilot'):
            return True
        if next_air and next_air >= today:
            return True
        if first_air and first_air >= today:
            return True
        if tmdb_id and is_in_upcoming_catalog(tmdb_id, 'tv'):
            return True

    first_air = _parse_release_date(show.get('FirstAirDate'))
    if first_air and first_air >= today:
        return True

    next_air = _parse_release_date(show.get('NextAirDate'))
    if next_air and next_air >= today:
        return True

    if tmdb_id and is_in_upcoming_catalog(tmdb_id, 'tv'):
        return True

    year = show.get('Year')
    if year and year != 'N/A':
        try:
            if int(year) > today.year:
                return True
        except (TypeError, ValueError):
            pass

    return False


def resolve_upcoming_movie(movie, tmdb_id):
    """Best-effort upcoming check with a TMDB refresh if the cached payload is thin."""
    if is_upcoming_movie(movie, tmdb_id=tmdb_id):
        return True
    if not TMDB_API_KEY or not tmdb_id:
        return False
    raw = tmdb_get(f'/movie/{tmdb_id}')
    if not raw:
        return False
    if raw.get('status'):
        movie['Status'] = raw.get('status')
    if raw.get('release_date'):
        movie['ReleaseDate'] = raw.get('release_date')
        try:
            movie['Year'] = raw['release_date'][:4]
        except (TypeError, IndexError):
            pass
    return is_upcoming_movie(movie, tmdb_id=tmdb_id)


def resolve_upcoming_series(show, tmdb_id):
    """Best-effort upcoming check with a TMDB refresh if the cached payload is thin."""
    if is_upcoming_series(show, tmdb_id=tmdb_id):
        return True
    if not TMDB_API_KEY or not tmdb_id:
        return False
    raw = tmdb_get(f'/tv/{tmdb_id}')
    if not raw:
        return False
    if raw.get('status'):
        show['status'] = raw.get('status')
    if raw.get('first_air_date'):
        show['FirstAirDate'] = raw.get('first_air_date')
    next_ep = raw.get('next_episode_to_air') or {}
    if next_ep.get('air_date'):
        show['NextAirDate'] = next_ep.get('air_date')
    return is_upcoming_series(show, tmdb_id=tmdb_id)


def format_fan_count(count):
    """Compact fan count label, e.g. 8200 -> 8.2K."""
    count = int(count or 0)
    if count >= 1_000_000:
        label = f'{count / 1_000_000:.1f}M'
        return label.replace('.0M', 'M')
    if count >= 1000:
        label = f'{count / 1000:.1f}K'
        return label.replace('.0K', 'K')
    return str(count)


def build_cant_miss_context(media_type, tmdb_id, viewer_id=None):
    """Fan count, avatars, and viewer state for the Can't Miss widget."""
    media_type = (media_type or '').strip().lower()
    tmdb_id = str(tmdb_id or '')
    if media_type not in ('movie', 'tv') or not tmdb_id:
        return None

    fan_count = CantMissFan.query.filter_by(
        media_type=media_type,
        tmdb_id=tmdb_id,
    ).count()

    marked_by_me = False
    if viewer_id:
        marked_by_me = CantMissFan.query.filter_by(
            user_id=viewer_id,
            media_type=media_type,
            tmdb_id=tmdb_id,
        ).first() is not None

    recent_rows = (
        CantMissFan.query.filter_by(media_type=media_type, tmdb_id=tmdb_id)
        .order_by(CantMissFan.created_at.desc())
        .limit(3)
        .all()
    )
    avatars = []
    for row in recent_rows:
        user = row.user
        if not user:
            continue
        pic = user.profile_pic if user.profile_pic and user.profile_pic != 'default.jpg' else None
        avatars.append({
            'initial': user.username[:1].upper(),
            'pic': pic,
            'mock': False,
        })

    mock_palette = (
        ('F', 32),
        ('M', 12),
        ('C', 280),
    )
    seed = sum(ord(ch) for ch in tmdb_id) or 42
    idx = 0
    while len(avatars) < 3:
        label, hue = mock_palette[(seed + idx) % len(mock_palette)]
        avatars.append({
            'initial': label,
            'pic': None,
            'mock': True,
            'hue': hue + (idx * 18),
        })
        idx += 1

    return {
        'fan_count': fan_count,
        'fan_count_label': format_fan_count(fan_count),
        'marked_by_me': marked_by_me,
        'avatars': avatars,
    }


def group_diary_by_month(entries):
    """Group diary entries into month sections for the diary page."""
    groups = {}
    order = []
    for entry in entries:
        key = entry.watched_date.strftime('%Y-%m')
        if key not in groups:
            groups[key] = {
                'label': entry.watched_date.strftime('%B %Y'),
                'entries': [],
            }
            order.append(key)
        groups[key]['entries'].append(entry)
    return [groups[key] for key in order]


def time_ago(value):
    """Short relative timestamp for community cards."""
    if not value:
        return ''
    now = datetime.utcnow()
    dt = value if isinstance(value, datetime) else datetime.combine(value, datetime.min.time())
    seconds = max(0, int((now - dt).total_seconds()))
    if seconds < 60:
        return 'just now'
    minutes = seconds // 60
    if minutes < 60:
        return f'{minutes}m'
    hours = minutes // 60
    if hours < 24:
        return f'{hours}h'
    days = hours // 24
    if days < 7:
        return f'{days}d'
    return dt.strftime('%b %d, %Y')


def build_community_feed(viewer_id=None, feed_filter='for-you', limit=50):
    """User community posts with sidebar filters."""
    liked_ids = set()
    saved_ids = set()
    if viewer_id:
        liked_ids = {
            row.post_id for row in CommunityPostLike.query.filter_by(user_id=viewer_id).all()
        }
        saved_ids = {
            row.post_id for row in CommunitySavedPost.query.filter_by(user_id=viewer_id).all()
        }

    like_counts = dict(
        db.session.query(CommunityPostLike.post_id, func.count(CommunityPostLike.id))
        .group_by(CommunityPostLike.post_id)
        .all()
    )

    if feed_filter == 'reviews':
        return []

    if feed_filter == 'following':
        if not viewer_id:
            return []
        viewer = User.query.get(viewer_id)
        followed_ids = [user.id for user in viewer.followed.all()] if viewer else []
        if not followed_ids:
            return []
        posts = community_posts_query().filter(
            CommunityPost.user_id.in_(followed_ids),
        ).order_by(CommunityPost.created_at.desc()).limit(limit).all()
    elif feed_filter == 'my-posts':
        if not viewer_id:
            return []
        posts = community_posts_query().filter_by(
            user_id=viewer_id,
        ).order_by(CommunityPost.created_at.desc()).limit(limit).all()
    elif feed_filter == 'saved':
        if not viewer_id or not saved_ids:
            return []
        posts = community_posts_query().filter(
            CommunityPost.id.in_(saved_ids),
        ).order_by(CommunityPost.created_at.desc()).limit(limit).all()
    else:
        posts = community_posts_query().order_by(
            CommunityPost.created_at.desc(),
        ).limit(limit * 2).all()
        if feed_filter == 'trending':
            posts.sort(
                key=lambda post: like_counts.get(post.id, 0),
                reverse=True,
            )
            posts = posts[:limit]
        else:
            posts = posts[:limit]

    feed = []
    for post in posts:
        feed.append({
            'id': post.id,
            'user': post.author,
            'content': post.content,
            'media_url': post.media_url,
            'media_type': post.media_type,
            'created_at': post.created_at,
            'time_label': time_ago(post.created_at),
            'likes': like_counts.get(post.id, 0),
            'liked_by_me': post.id in liked_ids,
            'saved_by_me': post.id in saved_ids,
        })
    return feed


def user_series_diary_entries(user_id):
    """Build month-groupable diary cards from a user's episode ratings."""
    rows = EpisodeRating.query.filter_by(user_id=user_id).order_by(
        EpisodeRating.updated_at.desc()
    ).all()
    grouped = {}
    for row in rows:
        sid = str(row.series_tmdb_id)
        bucket = grouped.setdefault(sid, {'ratings': [], 'last_at': row.updated_at})
        bucket['ratings'].append(row.rating)
        if row.updated_at and (not bucket['last_at'] or row.updated_at > bucket['last_at']):
            bucket['last_at'] = row.updated_at

    entries = []
    for sid, data in grouped.items():
        show = fetch_tv_by_id(sid)
        ratings = data['ratings']
        overall = round(sum(ratings) / len(ratings), 1) if ratings else None
        last_at = data['last_at']
        watched_date = last_at.date() if last_at else date.today()
        entries.append(SimpleNamespace(
            series_tmdb_id=sid,
            title=show['Title'] if show else f'Series {sid}',
            poster_url=show['Poster'] if show else None,
            rating=overall,
            episode_count=len(ratings),
            watched_date=watched_date,
            is_series=True,
        ))

    entries.sort(key=lambda item: item.watched_date, reverse=True)
    return entries


def _schedule_card(item, release_date=None, regions=None, kind='theatrical', media='movie'):
    if media == 'tv':
        release = release_date or _parse_release_date(item.get('first_air_date'))
        title = item.get('name') or item.get('original_name') or 'Untitled'
        poster_path = item.get('poster_path')
    else:
        release = release_date or _parse_release_date(item.get('release_date'))
        title = item.get('title') or item.get('original_title') or 'Untitled'
        poster_path = item.get('poster_path')
    return {
        'id': str(item.get('id')),
        'title': title,
        'poster': tmdb_poster_url(poster_path),
        'overview': (item.get('overview') or '')[:220],
        'release_date': release,
        'release_label': _format_schedule_date(release),
        'year': release.year if release else None,
        'regions': sorted(regions) if regions else [],
        'kind': kind,
        'media': media,
        'popularity': item.get('popularity') or 0,
        'original_language': (item.get('original_language') or '').lower(),
        'platform_label': 'On OTT' if kind == 'ott' else 'In Theatre',
    }


def group_schedule_by_date(cards, reverse=False):
    """Group schedule cards into date rails with Moctale-style badges."""
    buckets = {}
    for card in cards:
        key = card.get('release_date')
        buckets.setdefault(key, []).append(card)

    dated = sorted([k for k in buckets if k is not None], reverse=reverse)
    keys = dated + ([None] if None in buckets else [])

    groups = []
    for release_day in keys:
        films = buckets[release_day]
        if release_day is None:
            groups.append({
                'iso': '',
                'weekday': 'TBA',
                'day_num': '—',
                'month': '',
                'films': films,
            })
        else:
            groups.append({
                'iso': release_day.isoformat(),
                'weekday': release_day.strftime('%a').upper(),
                'day_num': release_day.strftime('%d'),
                'month': release_day.strftime('%b').upper(),
                'films': films,
            })
    return groups


def fetch_tv_schedule_slice(endpoint, kind, limit=24):
    data = tmdb_get(endpoint, page=1)
    cards = []
    for item in (data or {}).get('results') or []:
        cards.append(_schedule_card(item, kind=kind, media='tv'))
    cards.sort(key=lambda c: (c['release_date'] or date.max, -c['popularity']))
    return cards[:limit]


def fetch_tv_discover(date_gte, date_lte, kind='ott', limit=28):
    cards = []
    seen = set()
    payloads = tmdb_get_many([
        ('/discover/tv', {
            'air_date.gte': date_gte.isoformat(),
            'air_date.lte': date_lte.isoformat(),
            'sort_by': 'popularity.desc',
            'include_adult': 'false',
            'page': page,
            'language': 'en-US',
        })
        for page in (1, 2)
    ])
    for data in payloads:
        for item in (data or {}).get('results') or []:
            mid = str(item.get('id'))
            if not mid or mid in seen:
                continue
            seen.add(mid)
            cards.append(_schedule_card(item, kind=kind, media='tv'))
    cards.sort(key=lambda c: (c['release_date'] or date.max, -c['popularity']))
    return cards[:limit]


# Country → preferred watch languages (ISO 639-1). India listed first with major film industries.
LANGUAGE_GROUPS = [
    {
        'country': 'India',
        'flag': 'IN',
        'languages': [
            ('hi', 'Hindi'),
            ('ta', 'Tamil'),
            ('te', 'Telugu'),
            ('ml', 'Malayalam'),
            ('kn', 'Kannada'),
            ('bn', 'Bengali'),
            ('mr', 'Marathi'),
            ('pa', 'Punjabi'),
            ('gu', 'Gujarati'),
            ('ur', 'Urdu'),
        ],
    },
    {
        'country': 'United States / UK',
        'flag': 'US',
        'languages': [
            ('en', 'English'),
        ],
    },
    {
        'country': 'Japan',
        'flag': 'JP',
        'languages': [
            ('ja', 'Japanese'),
        ],
    },
    {
        'country': 'South Korea',
        'flag': 'KR',
        'languages': [
            ('ko', 'Korean'),
        ],
    },
    {
        'country': 'China / Taiwan / HK',
        'flag': 'CN',
        'languages': [
            ('zh', 'Chinese'),
            ('cn', 'Cantonese'),
        ],
    },
    {
        'country': 'France',
        'flag': 'FR',
        'languages': [
            ('fr', 'French'),
        ],
    },
    {
        'country': 'Spain / LatAm',
        'flag': 'ES',
        'languages': [
            ('es', 'Spanish'),
        ],
    },
    {
        'country': 'Brazil / Portugal',
        'flag': 'BR',
        'languages': [
            ('pt', 'Portuguese'),
        ],
    },
    {
        'country': 'Germany',
        'flag': 'DE',
        'languages': [
            ('de', 'German'),
        ],
    },
    {
        'country': 'Italy',
        'flag': 'IT',
        'languages': [
            ('it', 'Italian'),
        ],
    },
    {
        'country': 'Turkey',
        'flag': 'TR',
        'languages': [
            ('tr', 'Turkish'),
        ],
    },
    {
        'country': 'Thailand',
        'flag': 'TH',
        'languages': [
            ('th', 'Thai'),
        ],
    },
    {
        'country': 'Arabic-speaking',
        'flag': 'AR',
        'languages': [
            ('ar', 'Arabic'),
        ],
    },
]


def language_label(code):
    code = (code or '').lower()
    for group in LANGUAGE_GROUPS:
        for lang_code, name in group['languages']:
            if lang_code == code:
                return name
    return (code or '').upper() or 'Unknown'


def all_language_codes():
    codes = set()
    for group in LANGUAGE_GROUPS:
        for lang_code, _ in group['languages']:
            codes.add(lang_code)
    return codes


def _merge_region_lists(region_results, kind, only_from=None, only_until=None, limit=36):
    """Merge TMDB lists from several regions into unique cards with region tags."""
    merged = {}
    for region, payload in region_results:
        for item in (payload or {}).get('results') or []:
            release = _parse_release_date(item.get('release_date'))
            if only_from and release and release < only_from:
                continue
            if only_until and release and release > only_until:
                continue
            mid = str(item.get('id'))
            if not mid:
                continue
            if mid not in merged:
                merged[mid] = _schedule_card(item, release, {region} if region else set(), kind)
            else:
                if region:
                    regions = set(merged[mid]['regions'])
                    regions.add(region)
                    merged[mid]['regions'] = sorted(regions)
                existing = merged[mid]['release_date']
                if release and (not existing or release < existing):
                    merged[mid]['release_date'] = release
                    merged[mid]['release_label'] = _format_schedule_date(release)
                    merged[mid]['year'] = release.year
                if (item.get('popularity') or 0) > merged[mid]['popularity']:
                    merged[mid]['popularity'] = item.get('popularity') or 0
                    if item.get('poster_path'):
                        merged[mid]['poster'] = tmdb_poster_url(item.get('poster_path'))

    cards = list(merged.values())
    cards.sort(key=lambda c: (c['release_date'] or date.max, -c['popularity']))
    return cards[:limit]


def fetch_discover_releases(
    release_types,
    date_gte,
    date_lte,
    sort_by='popularity.desc',
    pages=2,
    kind='digital',
    original_language=None,
    region='US',
):
    """Discover movies by release window (region-aware so OTT/theatrical stay current)."""
    cards = []
    seen = set()
    today_year = date.today().year
    calls = []
    for page in range(1, pages + 1):
        params = {
            'with_release_type': release_types,
                'release_date.gte': date_gte.isoformat(),
                'release_date.lte': date_lte.isoformat(),
            'sort_by': sort_by,
            'include_adult': 'false',
            'include_video': 'false',
            'page': page,
            'language': 'en-US',
            'region': region,
        }
        if kind == 'ott':
            params['vote_count.gte'] = 5
        if original_language:
            params['with_original_language'] = original_language
        calls.append(('/discover/movie', params))
    for data in tmdb_get_many(calls):
        if not data:
            continue
        for item in data.get('results') or []:
            mid = str(item.get('id'))
            if not mid or mid in seen:
                continue
            release = _parse_release_date(item.get('release_date'))
            if not release:
                continue
            if kind == 'ott':
                # Discover already scoped type-4 dates; result.release_date is often theatrical.
                if release.year < today_year - 1:
                    continue
            elif release < date_gte or release > date_lte:
                continue
            seen.add(mid)
            cards.append(_schedule_card(item, release_date=release, kind=kind))

    cards.sort(key=lambda c: (
        c['release_date'] or date.max,
        -c['popularity'],
    ))
    return cards


def fetch_language_releases(lang_code, limit=24):
    """Language-specific releases from this week onwards: theatrical + upcoming OTT."""
    empty = {'theatrical': [], 'ott': []}
    lang_code = (lang_code or '').lower().strip()
    if not lang_code or lang_code not in all_language_codes():
        return empty

    cache_attr = f'_lang_cache_v4_{lang_code}'
    cached = getattr(fetch_language_releases, cache_attr, None)
    now = datetime.utcnow()
    if cached and (now - cached['at']).total_seconds() < 1800:
        return cached['data']

    today = date.today()
    week_start = today - timedelta(days=today.weekday())  # Monday of this week
    until = today + timedelta(days=120)

    theatrical = []
    seen_theatrical = set()
    theatrical_payloads = tmdb_get_many([
        ('/discover/movie', {
            'with_original_language': lang_code,
            'with_release_type': '2|3',
            'sort_by': 'primary_release_date.asc',
            'include_adult': 'false',
            'page': page,
            'language': 'en-US',
            'primary_release_date.gte': week_start.isoformat(),
            'primary_release_date.lte': until.isoformat(),
        })
        for page in (1, 2)
    ])
    for data in theatrical_payloads:
        for item in (data or {}).get('results') or []:
            mid = str(item.get('id'))
            if not mid or mid in seen_theatrical:
                continue
            release = _parse_release_date(item.get('release_date'))
            if release and release < week_start:
                continue
            seen_theatrical.add(mid)
            theatrical.append(_schedule_card(item, release, kind='theatrical'))

    theatrical.sort(key=lambda c: (c['release_date'] or date.max, -c['popularity']))
    theatrical = theatrical[:limit]

    ott = fetch_discover_releases(
        '4',
        week_start,
        until,
        sort_by='primary_release_date.asc',
        pages=2,
        kind='ott',
        original_language=lang_code,
    )
    # Keep OTT titles distinct from theatrical list when possible
    theatrical_ids = {c['id'] for c in theatrical}
    ott = [c for c in ott if c['id'] not in theatrical_ids][:limit]
    attach_ott_logos(ott, lang_code)

    data = {'theatrical': theatrical, 'ott': ott}
    setattr(fetch_language_releases, cache_attr, {'at': now, 'data': data})
    return data


def fetch_new_theatrical_window(days_back=14, limit=80):
    """Fresh theatrical releases day-by-day (strong India language coverage)."""
    today = date.today()
    date_gte = today - timedelta(days=days_back)
    merged = {}

    def absorb(payload, region=None):
        for item in (payload or {}).get('results') or []:
            mid = str(item.get('id') or '')
            if not mid:
                continue
            release = _parse_release_date(item.get('release_date'))
            if not release or release < date_gte or release > today:
                continue
            if mid not in merged:
                regions = {region} if region else set()
                merged[mid] = _schedule_card(item, release, regions, kind='theatrical')
            else:
                if region:
                    regions = set(merged[mid]['regions'])
                    regions.add(region)
                    merged[mid]['regions'] = sorted(regions)
                if (item.get('popularity') or 0) > merged[mid]['popularity']:
                    merged[mid]['popularity'] = item.get('popularity') or 0
                    if item.get('poster_path'):
                        merged[mid]['poster'] = tmdb_poster_url(item.get('poster_path'))

    calls = []
    regions_for_calls = []
    for region in ('IN', 'US', 'GB'):
        for page in (1, 2):
            calls.append(('/discover/movie', {
                'primary_release_date.gte': date_gte.isoformat(),
                'primary_release_date.lte': today.isoformat(),
                'sort_by': 'primary_release_date.desc',
                'with_release_type': '2|3',
                'include_adult': 'false',
                'region': region,
                'page': page,
                'language': 'en-US',
            }))
            regions_for_calls.append(region)

    for lang in ('hi', 'ta', 'te', 'ml', 'kn', 'bn', 'mr', 'pa'):
        calls.append(('/discover/movie', {
            'primary_release_date.gte': date_gte.isoformat(),
            'primary_release_date.lte': today.isoformat(),
            'sort_by': 'popularity.desc',
            'with_original_language': lang,
            'with_release_type': '2|3',
            'include_adult': 'false',
            'page': 1,
            'language': 'en-US',
        }))
        regions_for_calls.append('IN')

    for region in ('IN', 'US'):
        calls.append(('/movie/now_playing', {'region': region, 'page': 1, 'language': 'en-US'}))
        regions_for_calls.append(region)

    for payload, region in zip(tmdb_get_many(calls), regions_for_calls):
        absorb(payload, region=region)

    cards = list(merged.values())
    cards.sort(key=lambda c: (c['release_date'] or date.min, c['popularity']), reverse=True)
    return cards[:limit]


def fetch_new_ott_window(days_back=14, days_forward=0, limit=90, from_tomorrow=False):
    """Dense day-by-day digital/OTT releases (mirrors theatrical window coverage)."""
    today = date.today()
    if from_tomorrow:
        date_gte = today + timedelta(days=1)
        date_lte = today + timedelta(days=days_forward)
    else:
        date_gte = today - timedelta(days=days_back)
        date_lte = today + timedelta(days=days_forward)
    merged = {}

    def absorb(payload, region=None):
        for item in (payload or {}).get('results') or []:
            mid = str(item.get('id') or '')
            if not mid:
                continue
            release = _parse_release_date(item.get('release_date'))
            if not release:
                continue
            # Discover type-4 can return older primary dates; keep current cycle only
            if release.year < today.year - 1:
                continue
            if from_tomorrow:
                if release < date_gte or release > date_lte:
                    continue
            elif days_forward == 0 and (release < date_gte or release > today):
                # For "released on OTT", prefer titles whose listed date is in-window
                # but still keep popular digital drops discovered in-region
                if release < date_gte - timedelta(days=7) or release > today:
                    continue
            elif days_forward > 0 and release < today - timedelta(days=3):
                continue
            if mid not in merged:
                regions = {region} if region else set()
                card = _schedule_card(item, release, regions, kind='ott')
                merged[mid] = card
            else:
                if region:
                    regions = set(merged[mid]['regions'])
                    regions.add(region)
                    merged[mid]['regions'] = sorted(regions)
                if (item.get('popularity') or 0) > merged[mid]['popularity']:
                    merged[mid]['popularity'] = item.get('popularity') or 0
                    if item.get('poster_path'):
                        merged[mid]['poster'] = tmdb_poster_url(item.get('poster_path'))

    sort_by = (
        'primary_release_date.asc' if from_tomorrow else (
            'popularity.desc' if days_forward else 'primary_release_date.desc'
        )
    )
    calls = []
    regions_for_calls = []
    for region in ('IN', 'US', 'GB'):
        for page in (1, 2):
            calls.append(('/discover/movie', {
                'release_date.gte': date_gte.isoformat(),
                'release_date.lte': date_lte.isoformat(),
                'with_release_type': '4',
                'sort_by': sort_by,
                'include_adult': 'false',
                'region': region,
                'page': page,
                'language': 'en-US',
            }))
            regions_for_calls.append(region)

    for lang in ('hi', 'ta', 'te', 'ml', 'kn', 'bn', 'mr', 'pa', 'en'):
        calls.append(('/discover/movie', {
            'release_date.gte': date_gte.isoformat(),
            'release_date.lte': date_lte.isoformat(),
            'with_release_type': '4',
            'with_original_language': lang,
            'sort_by': 'primary_release_date.asc' if from_tomorrow else 'popularity.desc',
            'include_adult': 'false',
            'page': 1,
            'language': 'en-US',
            'region': 'IN' if lang != 'en' else 'US',
        }))
        regions_for_calls.append('IN' if lang != 'en' else 'US')

    for payload, region in zip(tmdb_get_many(calls), regions_for_calls):
        absorb(payload, region=region)

    cards = list(merged.values())
    if from_tomorrow or days_forward > 0:
        floor = today + timedelta(days=1) if from_tomorrow else today
        cards = [c for c in cards if c.get('release_date') and c['release_date'] >= floor]
        cards.sort(key=lambda c: (c['release_date'] or date.max, -c['popularity']))
    else:
        cards = [
            c for c in cards
            if c.get('release_date') and date_gte <= c['release_date'] <= today
        ]
        cards.sort(key=lambda c: (c['release_date'] or date.min, c['popularity']), reverse=True)
    return cards[:limit]


def fetch_upcoming_theatrical_window(days_forward=60, limit=90):
    """Theatrical openings from tomorrow onward (date-accurate, not /movie/upcoming)."""
    today = date.today()
    date_gte = today + timedelta(days=1)  # always start tomorrow
    date_lte = today + timedelta(days=days_forward)
    merged = {}

    def absorb(payload, region=None):
        for item in (payload or {}).get('results') or []:
            mid = str(item.get('id') or '')
            if not mid:
                continue
            release = _parse_release_date(item.get('release_date'))
            if not release or release < date_gte or release > date_lte:
                continue
            if mid not in merged:
                regions = {region} if region else set()
                merged[mid] = _schedule_card(item, release, regions, kind='theatrical')
            else:
                if region:
                    regions = set(merged[mid]['regions'])
                    regions.add(region)
                    merged[mid]['regions'] = sorted(regions)
                if (item.get('popularity') or 0) > merged[mid]['popularity']:
                    merged[mid]['popularity'] = item.get('popularity') or 0
                    if item.get('poster_path'):
                        merged[mid]['poster'] = tmdb_poster_url(item.get('poster_path'))

    calls = []
    regions_for_calls = []
    for region in ('IN', 'US', 'GB'):
        for page in (1, 2):
            calls.append(('/discover/movie', {
                'primary_release_date.gte': date_gte.isoformat(),
                'primary_release_date.lte': date_lte.isoformat(),
                'sort_by': 'primary_release_date.asc',
                'with_release_type': '2|3',
                'include_adult': 'false',
                'region': region,
                'page': page,
                'language': 'en-US',
            }))
            regions_for_calls.append(region)

    for lang in ('hi', 'ta', 'te', 'ml', 'kn', 'bn', 'mr', 'pa', 'en'):
        calls.append(('/discover/movie', {
            'primary_release_date.gte': date_gte.isoformat(),
            'primary_release_date.lte': date_lte.isoformat(),
            'sort_by': 'primary_release_date.asc',
            'with_original_language': lang,
            'with_release_type': '2|3',
            'include_adult': 'false',
            'page': 1,
            'language': 'en-US',
        }))
        regions_for_calls.append('IN' if lang != 'en' else 'US')

    for payload, region in zip(tmdb_get_many(calls), regions_for_calls):
        absorb(payload, region=region)

    cards = list(merged.values())
    cards.sort(key=lambda c: (c['release_date'] or date.max, -c['popularity']))
    return cards[:limit]


def fetch_release_schedule():
    """Worldwide-ish theatrical + digital/OTT release calendar from TMDB."""
    today = date.today()
    cached = getattr(fetch_release_schedule, '_cache', None)
    now = datetime.utcnow()
    if (
        cached
        and cached['data'].get('as_of') == today
        and (now - cached['at']).total_seconds() < 1800
        and cached['data'].get('tv_released') is not None
        and cached['data'].get('version') == 8
    ):
        return cached['data']

    upcoming_f = _JOB_POOL.submit(fetch_upcoming_theatrical_window, 60, 90)
    now_playing_f = _JOB_POOL.submit(fetch_new_theatrical_window, 14, 90)
    ott_recent_f = _JOB_POOL.submit(fetch_new_ott_window, 14, 0, 90, False)
    ott_upcoming_f = _JOB_POOL.submit(fetch_new_ott_window, 0, 60, 80, True)
    tv_released_f = _JOB_POOL.submit(fetch_tv_schedule_slice, '/tv/on_the_air', 'ott', 24)
    tv_upcoming_f = _JOB_POOL.submit(
        fetch_tv_discover,
        today + timedelta(days=1),
        today + timedelta(days=60),
        'ott',
        28,
    )

    data = {
        'upcoming': upcoming_f.result(),
        'now_playing': now_playing_f.result(),
        'ott_upcoming': ott_upcoming_f.result(),
        'ott_recent': ott_recent_f.result(),
        'announced': [],
        'ott_announced': [],
        'tv_released': tv_released_f.result(),
        'tv_upcoming': tv_upcoming_f.result(),
        'tv_announced': [],
        'as_of': today,
        'fetched_at': now,
        'version': 8,
    }
    fetch_release_schedule._cache = {'at': now, 'data': data}
    return data


def poster_or_placeholder(poster):
    if poster and poster != 'N/A':
        return poster
    return 'https://via.placeholder.com/300x450?text=No+Poster'


def parse_rating(value, default=None):
    try:
        rating = int(float(value))
    except (TypeError, ValueError):
        return default
    if rating < 1 or rating > 10:
        return default
    return rating


# ---------------------------------------------------------------------------
# Diary: one write path for ratings, reviews and diary entries
#
# Post (rating + review text) stays the record the film page, stats and community
# read from. DiaryEntry is the user's personal log and mirrors rating/review from
# that Post. Both are only ever written by save_film_log(), inside ONE transaction,
# and a unique index on (user_id, imdb_id) blocks duplicate entries.
# ---------------------------------------------------------------------------
REVIEW_PLACEHOLDER = 'Rated '
REVIEW_MAX_LENGTH = 1000


def real_review_text(post):
    """The user's written review, or None when the Post only holds the auto 'Rated X.' text."""
    body = ((post.content if post else '') or '').strip()
    if not body or body.startswith(REVIEW_PLACEHOLDER):
        return None
    return body


def parse_watched_date(raw, default=None):
    """Return (date, error). Blank -> default. Future or malformed dates are rejected."""
    raw = (raw or '').strip()
    if not raw:
        return default, None
    try:
        value = datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        return None, 'Enter a valid date.'
    if value > date.today():
        return None, 'The watched date cannot be in the future.'
    return value, None


def find_diary_entry(user_id, imdb_id, title=None):
    query = DiaryEntry.query.filter_by(user_id=user_id)
    if imdb_id:
        entry = query.filter_by(imdb_id=str(imdb_id)).first()
        if entry or not title:
            return entry
        # Older rows were saved without a film id; adopt them by title.
        return query.filter(DiaryEntry.title == title, DiaryEntry.imdb_id.is_(None)).first()
    if title:
        return query.filter(DiaryEntry.title == title).first()
    return None


def find_user_post(user_id, imdb_id, title=None):
    conditions = []
    if imdb_id:
        conditions.append(Post.imdb_id == str(imdb_id))
    if title:
        conditions.append(Post.movie == title)
    if not conditions:
        return None
    return (
        Post.query.filter(Post.user_id == user_id, db.or_(*conditions))
        .order_by(Post.created_at.desc(), Post.id.desc())
        .first()
    )


def run_in_transaction(work):
    """Run work() then commit. A unique-index race (double click) is retried once, then re-raised."""
    for attempt in range(2):
        try:
            result = work()
            db.session.commit()
            return result
        except IntegrityError:
            db.session.rollback()
            if attempt:
                raise


def save_film_log(user_id, *, imdb_id, title, poster=None, year=None, rating=None,
                  review=None, is_rewatch=None, has_spoilers=None, watched_date=None,
                  post=None, entry=None):
    """Create or update the user's rating/review Post, diary entry and watched mark together.

    Arguments left as None are not touched. review='' clears the written review (rating stays).
    The caller commits (use run_in_transaction) so everything lands in one transaction.
    """
    film_id = str(imdb_id) if imdb_id else None
    poster = poster_or_placeholder(poster)
    review_text = None if review is None else review.strip()
    if review_text is not None and len(review_text) > REVIEW_MAX_LENGTH:
        raise ValueError(f'Reviews are limited to {REVIEW_MAX_LENGTH} characters.')
    if rating is not None and not 1 <= int(rating) <= 10:
        raise ValueError('Rating must be between 1 and 10.')

    post = post or find_user_post(user_id, film_id, title)
    if rating is not None:
        if post is None:
            post = Post(
                movie=title, content=review_text or f'{REVIEW_PLACEHOLDER}{title}.',
                rating=rating, poster_url=poster, imdb_id=film_id, user_id=user_id,
            )
            db.session.add(post)
        else:
            post.rating = rating
            post.poster_url = poster
            if film_id:
                post.imdb_id = film_id
    if post is not None:
        if review_text is not None:
            post.content = review_text or f'{REVIEW_PLACEHOLDER}{title}.'
        if is_rewatch is not None:
            post.is_rewatch = bool(is_rewatch)
        if has_spoilers is not None:
            post.has_spoilers = bool(has_spoilers) and bool(real_review_text(post))
        elif not real_review_text(post):
            post.has_spoilers = False

    entry = entry or find_diary_entry(user_id, film_id, title)
    if entry is None:
        entry = DiaryEntry(
            user_id=user_id, title=title, imdb_id=film_id, is_rewatch=False,
            watched_date=watched_date or date.today(),
        )
        db.session.add(entry)
    elif watched_date:
        entry.watched_date = watched_date
    entry.title = title
    entry.poster_url = poster
    if film_id:
        entry.imdb_id = film_id
    if year:
        entry.release_year = str(year)[:4]
    if is_rewatch is not None:
        entry.is_rewatch = bool(is_rewatch)
    if post is not None:
        entry.rating = post.rating
        entry.review = real_review_text(post)
    elif review_text is not None:
        entry.review = review_text or None
    if film_id:
        entry.recommend = True if Recommendation.query.filter_by(
            user_id=user_id, media_type='movie', tmdb_id=film_id
        ).first() else None

    if film_id and not WatchedFilm.query.filter_by(user_id=user_id, tmdb_id=film_id).first():
        db.session.add(WatchedFilm(user_id=user_id, tmdb_id=film_id, title=title, poster_url=poster))
    return entry


def remove_film_log(user_id, entry):
    """'Remove from diary': drops the entry plus this user's rating and review of the film."""
    posts = Post.query.filter(Post.user_id == user_id)
    conditions = [Post.movie == entry.title]
    if entry.imdb_id:
        conditions.append(Post.imdb_id == str(entry.imdb_id))
    for post in posts.filter(db.or_(*conditions)).all():
        Comment.query.filter_by(post_id=post.id).delete()
        PostLike.query.filter_by(post_id=post.id).delete()
        db.session.delete(post)
    db.session.delete(entry)


def resync_diary_after_post_change(user_id, imdb_id, title):
    """After a Post was cleared/deleted: mirror what is left onto the diary entry (or drop it)."""
    entry = find_diary_entry(user_id, imdb_id, title)
    if entry is None:
        return
    post = find_user_post(user_id, imdb_id or entry.imdb_id, title)
    if post is not None:
        entry.rating = post.rating
        entry.review = real_review_text(post)
    elif entry.rating is not None and entry.review:
        pass  # manual diary entry that never had a Post: keep as logged
    else:
        db.session.delete(entry)


def diary_entry_json(entry):
    return {
        'id': entry.id,
        'film_id': entry.imdb_id,
        'title': entry.title,
        'year': entry.release_year,
        'poster': entry.poster_url,
        'rating': entry.rating,
        'review': entry.review,
        'recommend': entry.recommend,
        'is_rewatch': bool(entry.is_rewatch),
        'watched_date': entry.watched_date.isoformat() if entry.watched_date else None,
        'created_at': entry.created_at.isoformat() if entry.created_at else None,
        'updated_at': entry.updated_at.isoformat() if entry.updated_at else None,
    }


def backfill_diary_from_posts():
    """Create a diary entry for every existing rating/review. Idempotent: existing entries are skipped."""
    created = 0
    seen = set()
    posts = Post.query.order_by(Post.created_at.desc(), Post.id.desc()).all()
    for post in posts:
        key = (post.user_id, str(post.imdb_id) if post.imdb_id else post.movie)
        if key in seen:
            continue
        seen.add(key)
        if find_diary_entry(post.user_id, post.imdb_id, post.movie):
            continue
        posted = post.created_at or datetime.utcnow()
        db.session.add(DiaryEntry(
            user_id=post.user_id, title=post.movie, poster_url=post.poster_url,
            imdb_id=str(post.imdb_id) if post.imdb_id else None,
            rating=post.rating, review=real_review_text(post),
            watched_date=min(posted.date(), date.today()),
            is_rewatch=bool(post.is_rewatch),
            recommend=True if post.imdb_id and Recommendation.query.filter_by(
                user_id=post.user_id, media_type='movie', tmdb_id=str(post.imdb_id)).first() else None,
            created_at=posted, updated_at=posted,
        ))
        if post.imdb_id and not WatchedFilm.query.filter_by(
                user_id=post.user_id, tmdb_id=str(post.imdb_id)).first():
            db.session.add(WatchedFilm(
                user_id=post.user_id, tmdb_id=str(post.imdb_id), title=post.movie,
                poster_url=post.poster_url, watched_at=posted,
            ))
        created += 1
    db.session.commit()
    return created


def user_profile_stats(user_id):
    """Accurate per-user counts for profile headers (not limited list lengths)."""
    return {
        'review_count': Post.query.filter_by(user_id=user_id).count(),
        'watched_count': WatchedFilm.query.filter_by(user_id=user_id).count(),
        'like_count': FilmLike.query.filter_by(user_id=user_id).count(),
        'diary_count': DiaryEntry.query.filter_by(user_id=user_id).count(),
    }


def sync_user_film_logs(user_id):
    """Backfill this user's watched rows from their diary. Never touches other users.

    Reviews and ratings do NOT create diary entries; the diary only holds films the user logged on purpose.
    """
    entries = DiaryEntry.query.filter_by(user_id=user_id).all()
    for entry in entries:
        film_id = str(entry.imdb_id) if entry.imdb_id else None
        if not film_id:
            continue
        if WatchedFilm.query.filter_by(user_id=user_id, tmdb_id=film_id).first():
            continue
        db.session.add(WatchedFilm(
            user_id=user_id,
            tmdb_id=film_id,
            title=entry.title,
            poster_url=entry.poster_url,
        ))

    db.session.commit()


@app.template_filter('compact')
def compact_number(value):
    """1234 -> '1.2K', 4200000 -> '4.2M'."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return '0'
    for limit, suffix in ((1e9, 'B'), (1e6, 'M'), (1e3, 'K')):
        if abs(number) >= limit:
            text = f'{number / limit:.1f}'
            if text.endswith('.0'):
                text = text[:-2]
            return text + suffix
    return str(int(number))


def render_view(view, **context):
    """Render the single combined template for a named view."""
    return render_template('app.html', view=view, **context)


def ensure_schema():
    """Create tables and add columns introduced after the first release."""
    db.create_all()
    os.makedirs(app.config['STACK_BANNER_FOLDER'], exist_ok=True)
    os.makedirs(app.config['COMMUNITY_MEDIA_FOLDER'], exist_ok=True)
    alterations = [
        ("diary_entry", "imdb_id", "VARCHAR(20)"),
        ("diary_entry", "is_rewatch", "BOOLEAN DEFAULT 0"),
        ("post", "imdb_id", "VARCHAR(20)"),
        ("post", "is_rewatch", "BOOLEAN DEFAULT 0"),
        ("post", "has_spoilers", "BOOLEAN DEFAULT 0"),
        ("list_movie", "year", "VARCHAR(10)"),
        ("list_movie", "note", "TEXT"),
        ("list_movie", "imdb_id", "VARCHAR(20)"),
        ("list_movie", "position", "INTEGER DEFAULT 0"),
        ("movie_list", "banner_url", "VARCHAR(300)"),
        ("movie_list", "updated_at", "DATETIME"),
        ("movie_list", "is_public", "BOOLEAN DEFAULT 0"),
        ("community_post", "media_url", "VARCHAR(300)"),
        ("community_post", "media_type", "VARCHAR(10)"),
        ("community_post", "is_seed", "BOOLEAN DEFAULT 0"),
        # Watchlist can now hold series too; old rows are films.
        ("watchlist", "media_type", "VARCHAR(10) DEFAULT 'movie'"),
        ("watchlist", "tmdb_id", "VARCHAR(20)"),
        # Diary: one auto-synced entry per user per film
        ("diary_entry", "recommend", "BOOLEAN"),
        ("diary_entry", "release_year", "VARCHAR(4)"),
        ("diary_entry", "created_at", "DATETIME"),
        ("diary_entry", "updated_at", "DATETIME"),
    ]
    with db.engine.connect() as conn:
        for table, column, coltype in alterations:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))
                conn.commit()
            except Exception:
                conn.rollback()
        # Old rows: created/updated = the day they were watched.
        try:
            conn.execute(text(
                "UPDATE diary_entry SET created_at = watched_date WHERE created_at IS NULL"
            ))
            conn.execute(text(
                "UPDATE diary_entry SET updated_at = COALESCE(created_at, watched_date) WHERE updated_at IS NULL"
            ))
            conn.commit()
        except Exception:
            conn.rollback()
        # Indexes. The unique one cannot be built if old data already holds duplicates;
        # nothing is deleted in that case, the app logic still prevents new duplicates.
        for statement in (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_diary_user_film ON diary_entry (user_id, imdb_id)",
            "CREATE INDEX IF NOT EXISTS ix_diary_user_watched ON diary_entry (user_id, watched_date)",
        ):
            try:
                conn.execute(text(statement))
                conn.commit()
            except Exception as exc:
                conn.rollback()
                print(f'[schema] skipped index ({exc.__class__.__name__}): {statement}')
    purge_community_seed_data()
    # One-time backfill: every existing rating/review gets a diary entry.
    if not db.session.get(SchemaFlag, 'diary_backfill_v1'):
        added = backfill_diary_from_posts()
        db.session.add(SchemaFlag(name='diary_backfill_v1'))
        db.session.commit()
        print(f'[schema] diary backfill created {added} entries')


def relative_stack_updated(when):
    """Human label like 'Updated yesterday' for stack meta."""
    if not when:
        return 'Updated recently'
    now = datetime.utcnow()
    delta = now - when
    days = delta.days
    if days <= 0:
        hours = delta.seconds // 3600
        if hours < 1:
            return 'Updated just now'
        if hours == 1:
            return 'Updated 1 hour ago'
        return f'Updated {hours} hours ago'
    if days == 1:
        return 'Updated yesterday'
    if days < 7:
        return f'Updated {days} days ago'
    return f"Updated {when.strftime('%b %d, %Y')}"


def touch_stack(movie_list):
    movie_list.updated_at = datetime.utcnow()


def stack_banner_for(movie_list, movies=None):
    """Best available banner: custom upload, else first poster, else fallback."""
    if movie_list.banner_url:
        return movie_list.banner_url
    if movies is None:
        movies = (
            ListMovie.query
            .filter_by(list_id=movie_list.id)
            .order_by(ListMovie.position.asc(), ListMovie.id.asc())
            .limit(1)
            .all()
        )
    for m in movies or []:
        if m.poster_url:
            return m.poster_url
    return url_for('static', filename='stack-bg.jpg')


def build_stack_gallery_cards(movie_lists):
    """Card payloads for the public stacks grid."""
    cards = []
    for lst in movie_lists:
        movies = sorted(lst.movies or [], key=lambda m: (m.position or 0, m.id or 0))
        owner = lst.user
        cards.append({
            'id': lst.id,
            'name': lst.name,
            'banner': stack_banner_for(lst, movies),
            'item_count': len(movies),
            'like_count': len(lst.likes or []),
            'owner_username': owner.username if owner else 'Unknown',
            'owner_pic': (owner.profile_pic if owner and owner.profile_pic else 'default.jpg'),
            'is_public': bool(lst.is_public),
        })
    return cards


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


@app.context_processor
def inject_user():
    return dict(current_user=current_user)


@app.url_defaults
def add_static_version(endpoint, values):
    """Append ?v=<file mtime> to static URLs so browsers pick up CSS/JS edits right away."""
    if endpoint != 'static' or 'v' in values:
        return
    filename = values.get('filename')
    if not filename:
        return
    try:
        values['v'] = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
    except (OSError, TypeError):
        pass


# ------------------ Models ------------------ #
follows = db.Table('follows',
    db.Column('follower_id', db.Integer, db.ForeignKey('user.id')),
    db.Column('followed_id', db.Integer, db.ForeignKey('user.id'))
)


class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    profile_pic = db.Column(db.String(120), default='default.jpg')
    bio = db.Column(db.Text, default='')

    posts = db.relationship('Post', backref='author', lazy=True)
    comments = db.relationship('Comment', backref='author', lazy=True)
    watchlist = db.relationship('Watchlist', backref='user', lazy=True)
    lists = db.relationship('MovieList', backref='user', lazy=True)
    favorites = db.relationship(
        'FavoriteFilm',
        backref='user',
        lazy=True,
        order_by='FavoriteFilm.position',
        cascade='all, delete-orphan'
    )
    diary_entries = db.relationship('DiaryEntry', backref='user', lazy=True)

    followed = db.relationship(
        'User', secondary=follows,
        primaryjoin=(follows.c.follower_id == id),
        secondaryjoin=(follows.c.followed_id == id),
        backref=db.backref('followers', lazy='dynamic'), lazy='dynamic'
    )

    def follow(self, user):
        if not self.is_following(user):
            self.followed.append(user)

    def unfollow(self, user):
        if self.is_following(user):
            self.followed.remove(user)

    def is_following(self, user):
        return user in self.followed


class Watchlist(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    poster_url = db.Column(db.String(300))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    media_type = db.Column(db.String(10), default='movie')  # 'movie' or 'tv'
    tmdb_id = db.Column(db.String(20))


class Recommendation(db.Model):
    """One 'I recommend this' mark per user per film or series."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    media_type = db.Column(db.String(10), nullable=False)  # 'movie' or 'tv'
    tmdb_id = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint('user_id', 'media_type', 'tmdb_id', name='uq_recommendation'),
    )


class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    movie = db.Column(db.String(100), nullable=False)
    content = db.Column(db.Text, nullable=False)
    rating = db.Column(db.Integer, nullable=False)
    poster_url = db.Column(db.String(255))
    imdb_id = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    likes = db.Column(db.Integer, default=0)
    is_rewatch = db.Column(db.Boolean, default=False)    # "I've watched this before"
    has_spoilers = db.Column(db.Boolean, default=False)  # review text is hidden until revealed
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)


class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), nullable=False)


class CommunityPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False, default='')
    media_url = db.Column(db.String(300))
    media_type = db.Column(db.String(10))
    is_seed = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    author = db.relationship('User', backref=db.backref('community_posts', lazy=True))


class CommunityPostLike(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey('community_post.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('user_id', 'post_id', name='uq_community_post_like'),)


class CommunitySavedPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey('community_post.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('user_id', 'post_id', name='uq_community_saved_post'),)


class CantMissFan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    media_type = db.Column(db.String(10), nullable=False)
    tmdb_id = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship('User', backref=db.backref('cant_miss_marks', lazy=True))
    __table_args__ = (
        db.UniqueConstraint('user_id', 'media_type', 'tmdb_id', name='uq_cant_miss_fan'),
    )


class PostLike(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('user_id', 'post_id', name='uq_post_like'),)


class DiaryEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    poster_url = db.Column(db.String(300))
    review = db.Column(db.Text)
    rating = db.Column(db.Integer)
    watched_date = db.Column(db.Date, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    imdb_id = db.Column(db.String(20))
    is_rewatch = db.Column(db.Boolean, default=False)
    recommend = db.Column(db.Boolean)            # NULL = no opinion recorded
    release_year = db.Column(db.String(4))       # lets the diary render without TMDB calls
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # One entry per user per film (TMDB id). Title-only legacy rows have NULL imdb_id and are exempt.
    __table_args__ = (
        db.Index('uq_diary_user_film', 'user_id', 'imdb_id', unique=True),
        db.Index('ix_diary_user_watched', 'user_id', 'watched_date'),
    )


class SchemaFlag(db.Model):
    """Marks one-off data migrations (e.g. the diary backfill) as done."""
    name = db.Column(db.String(60), primary_key=True)
    done_at = db.Column(db.DateTime, default=datetime.utcnow)


class FavoriteFilm(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    poster_url = db.Column(db.String(300))
    imdb_id = db.Column(db.String(20))
    position = db.Column(db.Integer, nullable=False)


class MovieList(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text)
    banner_url = db.Column(db.String(300))
    is_public = db.Column(db.Boolean, default=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    movies = db.relationship('ListMovie', backref='list', lazy=True)
    likes = db.relationship('StackLike', backref='list', lazy=True, cascade='all, delete-orphan')
    comments = db.relationship('StackComment', backref='list', lazy=True, cascade='all, delete-orphan')


class ListMovie(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    poster_url = db.Column(db.String(300))
    year = db.Column(db.String(10))
    note = db.Column(db.Text)
    imdb_id = db.Column(db.String(20))
    position = db.Column(db.Integer, default=0)
    list_id = db.Column(db.Integer, db.ForeignKey('movie_list.id'), nullable=False)


class StackLike(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    list_id = db.Column(db.Integer, db.ForeignKey('movie_list.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('user_id', 'list_id', name='uq_stack_like'),)


class StackComment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    list_id = db.Column(db.Integer, db.ForeignKey('movie_list.id'), nullable=False)
    author = db.relationship('User', lazy=True)


class FilmLike(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    tmdb_id = db.Column(db.String(20), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    poster_url = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class WatchedFilm(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    tmdb_id = db.Column(db.String(20), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    poster_url = db.Column(db.String(300))
    watched_at = db.Column(db.DateTime, default=datetime.utcnow)


class EpisodeRating(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    series_tmdb_id = db.Column('tv_id', db.String(20), nullable=False)
    season_number = db.Column(db.Integer, nullable=False)
    episode_number = db.Column(db.Integer, nullable=False)
    rating = db.Column(db.Integer, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint(
            'user_id', 'tv_id', 'season_number', 'episode_number',
            name='uq_episode_rating',
        ),
    )


def get_popular_movies(limit=12):
    popular_data = db.session.query(
        Post.movie,
        func.count(Post.id).label('review_count'),
        func.avg(Post.rating).label('avg_rating'),
        func.max(Post.poster_url).label('poster_url'),
        func.max(Post.imdb_id).label('imdb_id')
    ).group_by(Post.movie).order_by(func.count(Post.id).desc()).limit(limit).all()

    popular_movies = []
    for movie, review_count, avg_rating, poster_url, imdb_id in popular_data:
        popular_movies.append({
            'movie': movie,
            'review_count': review_count,
            'avg_rating': round(avg_rating or 0, 1),
            'poster_url': poster_url,
            'imdb_id': imdb_id
        })
    return popular_movies


def fetch_weekly_trending_movies(limit=16):
    """Hero banner: this week's releases, India first, then world cinema."""
    today = date.today()
    cached = getattr(fetch_weekly_trending_movies, '_cache', None)
    now = datetime.utcnow()
    if (
        cached
        and cached.get('as_of') == today
        and cached.get('version') == 3
        and cached.get('data')
        and (now - cached['at']).total_seconds() < 1800
    ):
        return cached['data'][:limit]

    week_gte = (today - timedelta(days=7)).isoformat()
    week_lte = (today + timedelta(days=7)).isoformat()
    recent_gte = (today - timedelta(days=45)).isoformat()
    indian_langs = ('hi', 'ta', 'te', 'ml', 'kn', 'bn', 'mr', 'pa')

    def discover(gte, lte, **extra):
        params = {
            'primary_release_date.gte': gte,
            'primary_release_date.lte': lte,
            'sort_by': extra.pop('sort_by', 'primary_release_date.desc'),
            'include_adult': 'false',
            'page': extra.pop('page', 1),
            'language': 'en-US',
        }
        params.update(extra)
        return ('/discover/movie', params)

    calls = (
        [discover(week_gte, week_lte, region='IN', sort_by='popularity.desc', page=1)]
        + [discover(week_gte, week_lte, region='IN', sort_by='popularity.desc', page=2)]
        + [
            discover(week_gte, week_lte, with_original_language=lang, region='IN', sort_by='popularity.desc')
            for lang in indian_langs
        ]
        + [('/movie/now_playing', {'region': 'IN', 'page': 1, 'language': 'en-US'})]
        + [discover(week_gte, week_lte, region='US', sort_by='popularity.desc')]
        + [discover(week_gte, week_lte, region='GB', sort_by='popularity.desc')]
        + [('/trending/movie/week', {'language': 'en-US'})]
        + [
            discover(recent_gte, week_lte, with_original_language=lang, region='IN', sort_by='popularity.desc')
            for lang in indian_langs[:4]
        ]
    )

    payloads = tmdb_get_many(calls)
    india_week_n = 2 + len(indian_langs) + 1  # IN p1/p2 + langs + now_playing
    india_week_payloads = payloads[:india_week_n]
    world_week_payloads = payloads[india_week_n:india_week_n + 3]
    india_fill_payloads = payloads[india_week_n + 3:]

    def cards_from(payloads, origin, take=20, prefer_week=False):
        out = []
        seen = set()
        week_start = today - timedelta(days=7)
        week_end = today + timedelta(days=7)
        for payload in payloads:
            for item in (payload or {}).get('results') or []:
                tmdb_id = item.get('id')
                if not tmdb_id or str(tmdb_id) in seen:
                    continue
                backdrop = item.get('backdrop_path')
                poster = item.get('poster_path')
                if not backdrop and not poster:
                    continue
                release = _parse_release_date(item.get('release_date'))
                if prefer_week and (not release or not (week_start <= release <= week_end)):
                    continue
                lang = (item.get('original_language') or '').lower()
                card_origin = 'India' if lang in indian_langs else 'World'
                seen.add(str(tmdb_id))
                out.append({
                    'id': str(tmdb_id),
                    'title': item.get('title') or item.get('original_title') or 'Untitled',
                    'poster': tmdb_poster_url(poster),
                    'image': tmdb_poster_url(backdrop, size='w1280') if backdrop else tmdb_poster_url(poster),
                    'vote_average': item.get('vote_average') or 0,
                    'release_label': _format_schedule_date(release),
                    'origin': card_origin,
                    'original_language': lang,
                    'this_week': bool(release and week_start <= release <= week_end),
                })
                if len(out) >= take:
                    return out
        return out

    india_week_raw = cards_from(india_week_payloads, 'India', take=20, prefer_week=True)
    world_week = cards_from(world_week_payloads, 'World', take=8, prefer_week=True)
    india_week = [c for c in india_week_raw if c['origin'] == 'India']
    world_week = [c for c in india_week_raw if c['origin'] == 'World'] + world_week
    india_fill = [c for c in cards_from(india_fill_payloads + india_week_payloads, 'India', take=12) if c['origin'] == 'India']
    world_fill = cards_from(world_week_payloads, 'World', take=8)

    movies = []
    seen = set()

    def take(pool):
        for card in pool:
            if card['id'] in seen:
                continue
            seen.add(card['id'])
            movies.append(card)

    take(india_week)
    # Mix remaining this-week world with extra India, India-heavy
    wi, ww = 0, 0
    while len(movies) < limit and (wi < len(india_fill) or ww < len(world_week) or ww < len(world_fill)):
        added = False
        if wi < len(india_fill) and india_fill[wi]['id'] not in seen:
            seen.add(india_fill[wi]['id'])
            movies.append(india_fill[wi])
            added = True
        wi += 1
        if ww < len(world_week) and world_week[ww]['id'] not in seen and len(movies) < limit:
            seen.add(world_week[ww]['id'])
            movies.append(world_week[ww])
            added = True
        elif ww < len(world_fill) and world_fill[ww]['id'] not in seen and len(movies) < limit:
            seen.add(world_fill[ww]['id'])
            movies.append(world_fill[ww])
            added = True
        ww += 1
        if not added and wi >= len(india_fill) and ww >= max(len(world_week), len(world_fill)):
            break

    if not movies:
        if cached and cached.get('data'):
            return cached['data'][:limit]
        return []

    fetch_weekly_trending_movies._cache = {
        'at': now,
        'as_of': today,
        'version': 3,
        'data': movies,
    }
    return movies[:limit]


def fetch_weekly_trending_series(limit=16):
    """Hero banner: trending and newly aired TV, India-first mix."""
    today = date.today()
    cached = getattr(fetch_weekly_trending_series, '_cache', None)
    now = datetime.utcnow()
    if (
        cached
        and cached.get('as_of') == today
        and cached.get('data')
        and (now - cached['at']).total_seconds() < 1800
    ):
        return cached['data'][:limit]

    week_gte = (today - timedelta(days=7)).isoformat()
    week_lte = (today + timedelta(days=14)).isoformat()
    recent_gte = (today - timedelta(days=60)).isoformat()
    indian_langs = ('hi', 'ta', 'te', 'ml', 'kn', 'bn', 'mr', 'pa')

    def tv_discover(gte, lte, **extra):
        params = {
            'first_air_date.gte': gte,
            'first_air_date.lte': lte,
            'sort_by': extra.pop('sort_by', 'popularity.desc'),
            'include_adult': 'false',
            'page': extra.pop('page', 1),
            'language': 'en-US',
        }
        params.update(extra)
        return ('/discover/tv', params)

    calls = (
        [tv_discover(week_gte, week_lte, watch_region='IN', sort_by='popularity.desc', page=1)]
        + [tv_discover(week_gte, week_lte, watch_region='IN', sort_by='popularity.desc', page=2)]
        + [
            tv_discover(week_gte, week_lte, with_original_language=lang, watch_region='IN', sort_by='popularity.desc')
            for lang in indian_langs
        ]
        + [('/tv/on_the_air', {'language': 'en-US', 'page': 1})]
        + [('/tv/airing_today', {'language': 'en-US', 'page': 1})]
        + [('/trending/tv/week', {'language': 'en-US'})]
        + [
            tv_discover(recent_gte, week_lte, with_original_language=lang, watch_region='IN', sort_by='popularity.desc')
            for lang in indian_langs[:4]
        ]
    )

    def cards_from(payloads, take=24):
        out = []
        seen = set()
        for payload in payloads:
            for item in (payload or {}).get('results') or []:
                tmdb_id = item.get('id')
                if not tmdb_id or str(tmdb_id) in seen:
                    continue
                backdrop = item.get('backdrop_path')
                poster = item.get('poster_path')
                if not backdrop and not poster:
                    continue
                first_air = _parse_release_date(item.get('first_air_date'))
                lang = (item.get('original_language') or '').lower()
                seen.add(str(tmdb_id))
                out.append({
                    'id': str(tmdb_id),
                    'title': item.get('name') or item.get('original_name') or 'Untitled',
                    'poster': tmdb_poster_url(poster),
                    'image': tmdb_poster_url(backdrop, size='w1280') if backdrop else tmdb_poster_url(poster),
                    'vote_average': item.get('vote_average') or 0,
                    'release_label': _format_schedule_date(first_air),
                    'origin': 'India' if lang in indian_langs else 'World',
                })
                if len(out) >= take:
                    return out
        return out

    payloads = tmdb_get_many(calls)
    raw = cards_from(payloads, take=limit * 2)
    india = [c for c in raw if c['origin'] == 'India']
    world = [c for c in raw if c['origin'] == 'World']

    series = []
    seen = set()
    for pool in (india, world):
        for card in pool:
            if card['id'] in seen:
                continue
            seen.add(card['id'])
            series.append(card)
            if len(series) >= limit:
                break
        if len(series) >= limit:
            break

    if not series:
        if cached and cached.get('data'):
            return cached['data'][:limit]
        return []

    fetch_weekly_trending_series._cache = {
        'at': now,
        'as_of': today,
        'data': series,
    }
    return series[:limit]


def get_popular_series(limit=12):
    """Series ranked by community episode ratings."""
    popular_data = db.session.query(
        EpisodeRating.series_tmdb_id,
        func.count(EpisodeRating.id).label('rating_count'),
        func.count(func.distinct(EpisodeRating.user_id)).label('reviewer_count'),
        func.avg(EpisodeRating.rating).label('avg_rating'),
    ).group_by(
        EpisodeRating.series_tmdb_id,
    ).order_by(
        func.count(func.distinct(EpisodeRating.user_id)).desc(),
        func.count(EpisodeRating.id).desc(),
    ).limit(limit).all()

    series = []
    for tmdb_id, rating_count, reviewer_count, avg_rating in popular_data:
        show = fetch_tv_by_id(tmdb_id)
        series.append({
            'title': show['Title'] if show else f'Series {tmdb_id}',
            'tmdb_id': str(tmdb_id),
            'poster_url': show['Poster'] if show else None,
            'avg_rating': round(avg_rating or 0, 1),
            'rating_count': rating_count,
            'reviewer_count': reviewer_count,
        })
    return series


def fetch_cinema_discussion_posts(limit=3, lookback_days=7):
    """Pick recent community review posts as discussion starters."""
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    candidates = (
        Post.query
        .filter(Post.created_at >= cutoff)
        .order_by(Post.likes.desc(), Post.created_at.desc())
        .limit(limit)
        .all()
    )

    posts = []
    for p in candidates:
        content = (p.content or '').strip()
        snippet = content[:180] + ('...' if len(content) > 180 else '')
        imdb_id = getattr(p, 'imdb_id', None)
        movie_url = url_for('film_page', imdb_id=imdb_id) if imdb_id else None
        posts.append({
            'id': p.id,
            'movie_title': p.movie,
            'imdb_id': imdb_id,
            'poster_url': p.poster_url,
            'snippet': snippet,
            'created_at': p.created_at,
            'rating': p.rating,
            'movie_url': movie_url,
        })
    return posts


def film_reviews_for_title(title, imdb_id=None):
    query = Post.query
    if imdb_id:
        query = query.filter((Post.imdb_id == imdb_id) | (Post.movie == title))
    else:
        query = query.filter(Post.movie == title)
    return query.order_by(Post.created_at.desc()).all()


def rating_histogram(reviews):
    """Five star buckets from 1–10 integer ratings."""
    buckets = [0, 0, 0, 0, 0]
    for post in reviews:
        if not post.rating:
            continue
        star = max(1, min(5, int(round(post.rating / 2.0))))
        buckets[star - 1] += 1
    total = sum(buckets) or 1
    return [
        {'stars': i + 1, 'count': buckets[i], 'pct': round(buckets[i] * 100 / total)}
        for i in range(5)
    ]


# ------------------ Routes ------------------ #
@app.route("/")
def home():
    popular_movies = get_popular_movies(limit=10)
    weekly_trending_movies = fetch_weekly_trending_movies(limit=18)
    return render_view(
        'home',
        popular_movies=popular_movies,
        weekly_trending_movies=weekly_trending_movies,
        tmdb_configured=bool(TMDB_API_KEY),
        community_url=url_for('community'),
    )


@app.route('/community')
def community():
    feed_filter = request.args.get('filter', 'for-you').strip().lower()
    valid_filters = {'for-you', 'following', 'trending', 'reviews', 'my-posts', 'saved'}
    if feed_filter not in valid_filters:
        feed_filter = 'for-you'

    viewer_id = current_user.id if current_user.is_authenticated else None
    feed = build_community_feed(viewer_id=viewer_id, feed_filter=feed_filter, limit=50)
    following_ids = set()
    if current_user.is_authenticated:
        following_ids = {user.id for user in current_user.followed.all()}
    return render_view(
        'community',
        feed=feed,
        following_ids=following_ids,
        feed_filter=feed_filter,
    )


@app.route('/community/post', methods=['POST'])
@login_required
def community_post():
    content = (request.form.get('content') or '').strip()
    file = request.files.get('media')

    media_url = None
    media_type = None
    if file and file.filename:
        if not allowed_community_media(file.filename):
            flash('Use JPG, PNG, GIF, WebP, MP4, WebM, or MOV.', 'danger')
            return redirect(url_for('community'))
        media_type = community_media_type(file.filename)
        os.makedirs(app.config['COMMUNITY_MEDIA_FOLDER'], exist_ok=True)
        ext = file.filename.rsplit('.', 1)[1].lower()
        filename = secure_filename(f"post_{current_user.id}_{int(datetime.utcnow().timestamp())}.{ext}")
        filepath = os.path.join(app.config['COMMUNITY_MEDIA_FOLDER'], filename)
        file.save(filepath)
        media_url = url_for('static', filename=f'community_media/{filename}')

    if not content and not media_url:
        flash('Add text, an image, or a video before posting.', 'warning')
        return redirect(url_for('community', filter=request.form.get('filter') or 'for-you'))
    if len(content) > 500:
        flash('Posts are limited to 500 characters.', 'warning')
        return redirect(url_for('community', filter=request.form.get('filter') or 'for-you'))

    db.session.add(CommunityPost(
        content=content,
        media_url=media_url,
        media_type=media_type,
        is_seed=False,
        user_id=current_user.id,
    ))
    db.session.commit()
    flash('Posted to the community.', 'success')
    return redirect(url_for('community', filter=request.form.get('filter') or 'for-you'))


@app.route('/community/thought/<int:post_id>/like', methods=['POST'])
@login_required
def community_like_thought(post_id):
    post = CommunityPost.query.get_or_404(post_id)
    existing = CommunityPostLike.query.filter_by(user_id=current_user.id, post_id=post.id).first()
    if existing:
        db.session.delete(existing)
    else:
        db.session.add(CommunityPostLike(user_id=current_user.id, post_id=post.id))
    db.session.commit()
    return redirect(request.referrer or url_for('community'))


@app.route('/community/thought/<int:post_id>/save', methods=['POST'])
@login_required
def community_save_thought(post_id):
    post = CommunityPost.query.get_or_404(post_id)
    existing = CommunitySavedPost.query.filter_by(user_id=current_user.id, post_id=post.id).first()
    if existing:
        db.session.delete(existing)
        flash('Removed from saved posts.', 'info')
    else:
        db.session.add(CommunitySavedPost(user_id=current_user.id, post_id=post.id))
        flash('Post saved.', 'success')
    db.session.commit()
    return redirect(request.referrer or url_for('community'))


@app.route('/community/thought/<int:post_id>/delete', methods=['POST'])
@login_required
def community_delete_thought(post_id):
    post = CommunityPost.query.get_or_404(post_id)
    if post.user_id != current_user.id:
        flash('You can only delete your own posts.', 'danger')
        return redirect(url_for('community'))
    delete_community_media_file(post.media_url)
    CommunityPostLike.query.filter_by(post_id=post.id).delete()
    CommunitySavedPost.query.filter_by(post_id=post.id).delete()
    db.session.delete(post)
    db.session.commit()
    flash('Post removed.', 'info')
    return redirect(url_for('community'))


@app.route('/newsletter', methods=['POST'])
def newsletter_subscribe():
    email = (request.form.get('email') or '').strip()
    if email and '@' in email:
        flash('You’re on the list — new reviews and journal stories are coming.', 'success')
    else:
        flash('Enter a valid email to stay in the loop.', 'warning')
    return redirect(url_for('home') + '#landing-footer')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('home'))

    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''

        if not username or not email or not password:
            flash("All fields are required.", "danger")
            return redirect(url_for('signup'))

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "danger")
            return redirect(url_for('signup'))

        if User.query.filter(func.lower(User.email) == email).first():
            flash('Email already exists. Try logging in.', 'danger')
            return redirect(url_for('signup'))

        if User.query.filter(func.lower(User.username) == username.lower()).first():
            flash('Username already taken. Pick another.', 'danger')
            return redirect(url_for('signup'))

        try:
            hashed_pw = generate_password_hash(password)
            user = User(username=username, email=email, password=hashed_pw)
            db.session.add(user)
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash('Could not create account. Please try again.', 'danger')
            return redirect(url_for('signup'))

        login_user(user)
        flash('Welcome to 4 Movies!', 'success')
        return redirect(url_for('home'))

    return render_view('signup')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('home'))

    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        user = User.query.filter(func.lower(User.email) == email).first()

        if user and check_password_hash(user.password, password):
            login_user(user)
            flash('Login successful!', 'success')
            next_page = request.args.get('next')
            if next_page and next_page.startswith('/') and not next_page.startswith(('//', '/\\')):
                return redirect(next_page)
            return redirect(url_for('home'))

        flash('Invalid email or password.', 'danger')
        return redirect(url_for('login', next=request.args.get('next')) if request.args.get('next') else url_for('login'))

    return render_view('login')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Logged out.', 'info')
    return redirect(url_for('home'))


# ------------------ Recommend + Watchlist (films and series) ------------------ #

def watchlist_match(media_type, tmdb_id, title=None):
    """SQL condition matching one title in Watchlist. Older film rows only have a title, so match those too."""
    tmdb_id = str(tmdb_id)
    if media_type == 'tv':
        return and_(Watchlist.media_type == 'tv', Watchlist.tmdb_id == tmdb_id)
    options = [Watchlist.tmdb_id == tmdb_id]
    if title:
        options.append(and_(Watchlist.tmdb_id.is_(None), Watchlist.title == title))
    return and_(or_(Watchlist.media_type.is_(None), Watchlist.media_type != 'tv'), or_(*options))


def engage_copy(kind, active, count):
    """Text for the Recommend / Watchlist buttons. Used by the templates and by the JSON replies."""
    number = compact_number(count)
    if kind == 'recommend':
        if count:
            sub = f"{number} {'person recommends' if count == 1 else 'people recommend'} it"
        else:
            sub = 'Be the first to recommend'
        label = 'Recommended' if active else 'Recommend'
    else:
        sub = f"{'Fan' if count == 1 else 'Fans'} added to watchlist"
        label = 'In your Watchlist' if active else 'Watchlist'
    return {'active': bool(active), 'count': count, 'value': number, 'label': label, 'sub': sub}


app.jinja_env.globals['engage_copy'] = engage_copy


def engage_state(media_type, tmdb_id, title=None):
    """Counts plus the signed-in user's own state for the Recommend and Watchlist buttons."""
    tmdb_id = str(tmdb_id)
    recommend_count = Recommendation.query.filter_by(media_type=media_type, tmdb_id=tmdb_id).count()
    watch_filter = watchlist_match(media_type, tmdb_id, title)
    watchlist_count = db.session.query(func.count(func.distinct(Watchlist.user_id))).filter(watch_filter).scalar() or 0
    recommended = on_watchlist = False
    if current_user.is_authenticated:
        recommended = Recommendation.query.filter_by(
            user_id=current_user.id, media_type=media_type, tmdb_id=tmdb_id
        ).first() is not None
        on_watchlist = Watchlist.query.filter(Watchlist.user_id == current_user.id, watch_filter).first() is not None
    return {
        'recommend': engage_copy('recommend', recommended, recommend_count),
        'watchlist': engage_copy('watchlist', on_watchlist, watchlist_count),
    }


def wants_json():
    return request.headers.get('X-Requested-With') == 'fetch' or request.accept_mimetypes.best == 'application/json'


def toggle_engagement(media_type, action, tmdb_id):
    """Flip the current user's Recommend or Watchlist mark. Replies with JSON for fetch(), a redirect otherwise."""
    is_tv = media_type == 'tv'
    back = url_for('series_page', tmdb_id=tmdb_id) if is_tv else url_for('film_page', imdb_id=tmdb_id)

    if not current_user.is_authenticated:
        login_url = url_for('login', next=back)
        if wants_json():
            return jsonify(ok=False, login=login_url, message='Sign in to do that.'), 401
        return redirect(login_url)

    item = fetch_tv_by_id(tmdb_id) if is_tv else fetch_movie_by_id(tmdb_id)
    if not item:
        if wants_json():
            return jsonify(ok=False, message='Not found.'), 404
        flash('Series not found.' if is_tv else 'Film not found.', 'danger')
        return redirect(url_for('series_index') if is_tv else url_for('home'))

    title = item.get('Title')
    tmdb_id = str(tmdb_id)

    if action == 'recommend':
        row = Recommendation.query.filter_by(
            user_id=current_user.id, media_type=media_type, tmdb_id=tmdb_id
        ).first()
        if row:
            db.session.delete(row)
            message = 'Recommendation removed.'
            recommended_now = False
        else:
            db.session.add(Recommendation(user_id=current_user.id, media_type=media_type, tmdb_id=tmdb_id))
            recommended_now = True
            message = 'Thanks! Marked as recommended.'
        if media_type == 'movie':
            # Keep the diary's recommend flag in step with the Recommend button.
            logged = DiaryEntry.query.filter_by(user_id=current_user.id, imdb_id=tmdb_id).first()
            if logged:
                logged.recommend = True if recommended_now else None
    else:
        rows = Watchlist.query.filter(
            Watchlist.user_id == current_user.id, watchlist_match(media_type, tmdb_id, title)
        ).all()
        if rows:
            for row in rows:
                db.session.delete(row)
            message = 'Removed from your watchlist.'
        else:
            db.session.add(Watchlist(
                title=title,
                poster_url=item.get('Poster'),
                user_id=current_user.id,
                media_type=media_type,
                tmdb_id=tmdb_id,
            ))
            message = 'Added to your watchlist.'
    db.session.commit()

    state = engage_state(media_type, tmdb_id, title)[action]
    if wants_json():
        return jsonify(ok=True, action=action, message=message, **state)
    flash(message, 'success')
    return redirect(back)


@app.route('/film/<tmdb_id>/recommend', methods=['POST'])
def film_recommend(tmdb_id):
    return toggle_engagement('movie', 'recommend', tmdb_id)


@app.route('/film/<tmdb_id>/watchlist', methods=['POST'])
def film_watchlist(tmdb_id):
    return toggle_engagement('movie', 'watchlist', tmdb_id)


@app.route('/series/<tmdb_id>/recommend', methods=['POST'])
def series_recommend(tmdb_id):
    return toggle_engagement('tv', 'recommend', tmdb_id)


@app.route('/series/<tmdb_id>/watchlist', methods=['POST'])
def series_watchlist(tmdb_id):
    return toggle_engagement('tv', 'watchlist', tmdb_id)


@app.route('/film/<imdb_id>')
def film_page(imdb_id):
    if not TMDB_API_KEY:
        flash('Set TMDB_API_KEY to load film pages from TMDB.', 'warning')
        return redirect(url_for('home'))

    movie = fetch_movie_by_id(imdb_id)
    if not movie:
        flash("Film not found.", "danger")
        return redirect(url_for('home'))

    title = movie.get('Title')
    tmdb_id = movie.get('tmdb_id') or imdb_id
    reviews = film_reviews_for_title(title, tmdb_id)
    histogram = rating_histogram(reviews)
    like_count = FilmLike.query.filter_by(tmdb_id=str(tmdb_id)).count()
    # Phase 1 stats come from existing data; Phase 2 moves this behind /api/film/<id>/stats.
    rated_values = [post.rating for post in reviews if post.rating]
    # Star buckets for the "Rating Breakdown" card: stored 1-10 (half stars) -> 1-5 stars, rounded up.
    distribution = {star: 0 for star in range(1, 6)}
    for value in rated_values:
        distribution[min(5, max(1, int(-(-value // 2))))] += 1
    film_stats = {
        'distribution': distribution,
        'rating_count': len(rated_values),
        'avg_rating': round(sum(rated_values) / len(rated_values), 1) if rated_values else None,
        'recommend_pct': None,
    }
    engage = engage_state('movie', tmdb_id, title)
    film_stats['watchlist_count'] = engage['watchlist']['count']
    review_previews = [
        post for post in reviews
        if post.content and not post.content.startswith('Rated ')
    ]
    watch_providers = fetch_watch_providers(tmdb_id)

    in_watchlist = False
    user_rating = None
    user_review = ''
    user_rewatch = False
    user_spoilers = False
    diary_entry = None
    liked = False
    watched = False
    user_lists = []
    if current_user.is_authenticated:
        in_watchlist = engage['watchlist']['active']
        own = find_user_post(current_user.id, tmdb_id, title)
        diary_entry = find_diary_entry(current_user.id, tmdb_id, title)
        if own:
            user_rating = own.rating
            user_rewatch = bool(own.is_rewatch)
            user_spoilers = bool(own.has_spoilers)
            # Hide auto placeholder text in the form
            if own.content and not own.content.startswith('Rated '):
                user_review = own.content
        liked = FilmLike.query.filter_by(user_id=current_user.id, tmdb_id=str(tmdb_id)).first() is not None
        watched = WatchedFilm.query.filter_by(user_id=current_user.id, tmdb_id=str(tmdb_id)).first() is not None
        user_lists = MovieList.query.filter_by(user_id=current_user.id).order_by(MovieList.name).all()

    upcoming = resolve_upcoming_movie(movie, tmdb_id)
    cant_miss = build_cant_miss_context('movie', tmdb_id, viewer_id=current_user.id if current_user.is_authenticated else None) if upcoming else None
    watch_online = build_watch_online_cards(watch_providers) if not upcoming else []

    return render_view(
        'film',
        movie=movie,
        reviews=reviews,
        histogram=histogram,
        like_count=like_count,
        in_watchlist=in_watchlist,
        user_rating=user_rating,
        user_review=user_review,
        user_rewatch=user_rewatch,
        user_spoilers=user_spoilers,
        diary_entry=diary_entry,
        today_iso=date.today().isoformat(),
        liked=liked,
        watched=watched,
        user_lists=user_lists,
        watch_providers=watch_providers,
        watch_online=watch_online,
        is_upcoming=upcoming,
        cant_miss=cant_miss,
        stats=film_stats,
        engage=engage,
        review_previews=review_previews,
    )


@app.route('/series')
def series_index():
    if not TMDB_API_KEY:
        flash('Set TMDB_API_KEY to load series from TMDB.', 'warning')
        return redirect(url_for('home'))

    query = request.args.get('query', '').strip()
    weekly_trending_series = []
    popular_series = []
    if query:
        series_list = search_tmdb_series(query, limit=24)
    else:
        weekly_trending_series = fetch_weekly_trending_series(limit=18)
        popular_series = get_popular_series(limit=10)
        series_list = fetch_series_catalog(limit=30)
    return render_view(
        'series',
        series_list=series_list,
        query=query,
        weekly_trending_series=weekly_trending_series,
        popular_series=popular_series,
        tmdb_configured=bool(TMDB_API_KEY),
    )


@app.route('/series/<tmdb_id>')
def series_page(tmdb_id):
    if not TMDB_API_KEY:
        flash('Set TMDB_API_KEY to load series pages from TMDB.', 'warning')
        return redirect(url_for('home'))

    show = fetch_tv_by_id(tmdb_id)
    if not show:
        flash('Series not found.', 'danger')
        return redirect(url_for('series_index'))

    seasons = show.get('seasons') or []
    if not seasons:
        flash('No seasons available for this series yet.', 'warning')
        return redirect(url_for('series_index'))

    try:
        selected_season = int(request.args.get('season') or seasons[0]['number'])
    except (TypeError, ValueError):
        selected_season = seasons[0]['number']

    valid_numbers = {s['number'] for s in seasons}
    if selected_season not in valid_numbers:
        selected_season = seasons[0]['number']

    season_data = fetch_tv_season(tmdb_id, selected_season)
    if not season_data:
        flash('Could not load episodes for this season.', 'warning')
        return redirect(url_for('series_index'))

    episode_ratings = {}
    overall_rating = None
    rated_count = 0
    if current_user.is_authenticated:
        episode_ratings = user_episode_ratings(current_user.id, tmdb_id)
        overall_rating, rated_count = user_series_overall_rating(current_user.id, tmdb_id)

    for episode in season_data.get('episodes') or []:
        episode['user_rating'] = episode_ratings.get((selected_season, episode['number']))

    upcoming = resolve_upcoming_series(show, tmdb_id)
    cant_miss = build_cant_miss_context('tv', tmdb_id, viewer_id=current_user.id if current_user.is_authenticated else None) if upcoming else None
    watch_providers = fetch_watch_providers(tmdb_id, media_type='tv')
    watch_online = build_watch_online_cards(watch_providers) if not upcoming else []

    return render_view(
        'series_detail',
        show=show,
        seasons=seasons,
        season=season_data,
        selected_season=selected_season,
        overall_rating=overall_rating,
        rated_count=rated_count,
        episode_ratings=episode_ratings,
        is_upcoming=upcoming,
        cant_miss=cant_miss,
        watch_online=watch_online,
        engage=engage_state('tv', show.get('tmdb_id') or tmdb_id, show.get('Title')),
    )


@app.route('/series/<tmdb_id>/rate/<int:season>/<int:episode>', methods=['POST'])
@login_required
def rate_episode(tmdb_id, season, episode):
    show = fetch_tv_by_id(tmdb_id)
    if not show:
        flash('Series not found.', 'danger')
        return redirect(url_for('series_index'))

    rating = parse_rating(request.form.get('rating'))
    if not rating:
        flash('Pick a rating between 1 and 10 stars (half steps).', 'warning')
        return redirect(url_for('series_page', tmdb_id=tmdb_id, season=season, _anchor='episodes'))

    row = EpisodeRating.query.filter_by(
        user_id=current_user.id,
        series_tmdb_id=str(tmdb_id),
        season_number=season,
        episode_number=episode,
    ).first()
    if row:
        row.rating = rating
        row.updated_at = datetime.utcnow()
    else:
        db.session.add(EpisodeRating(
            user_id=current_user.id,
            series_tmdb_id=str(tmdb_id),
            season_number=season,
            episode_number=episode,
            rating=rating,
        ))
    db.session.commit()

    overall, count = user_series_overall_rating(current_user.id, tmdb_id)
    stars = overall / 2 if overall else 0
    flash(
        f'Episode saved. Your series score is {overall:.1f}/10 ({stars:.1f}/5) from {count} rated episode{"s" if count != 1 else ""}.',
        'success',
    )
    return redirect(url_for('series_page', tmdb_id=tmdb_id, season=season, _anchor='episodes'))


@app.route('/person/<person_id>')
def person_page(person_id):
    if not TMDB_API_KEY:
        flash('Set TMDB_API_KEY to load cast & crew pages.', 'warning')
        return redirect(url_for('home'))

    person = fetch_person_by_id(person_id)
    if not person:
        flash('Person not found.', 'danger')
        return redirect(url_for('home'))

    return render_view('person', person=person)


@app.route('/cant-miss/toggle', methods=['POST'])
@login_required
def cant_miss_toggle():
    media_type = (request.form.get('media_type') or '').strip().lower()
    tmdb_id = (request.form.get('tmdb_id') or '').strip()
    next_url = request.form.get('next') or request.referrer or url_for('home')

    if media_type not in ('movie', 'tv') or not tmdb_id:
        flash('Could not update your fan mark.', 'warning')
        return redirect(next_url)

    existing = CantMissFan.query.filter_by(
        user_id=current_user.id,
        media_type=media_type,
        tmdb_id=tmdb_id,
    ).first()

    if existing:
        db.session.delete(existing)
        flash("Removed from Can't Miss This.", 'info')
    else:
        db.session.add(CantMissFan(
            user_id=current_user.id,
            media_type=media_type,
            tmdb_id=tmdb_id,
        ))
        flash("Marked as Can't Miss This!", 'success')

    db.session.commit()
    return redirect(next_url)


@app.route('/film/<imdb_id>/like', methods=['POST'])
@login_required
def like_film(imdb_id):
    movie = fetch_movie_by_id(imdb_id)
    if not movie:
        flash('Film not found.', 'danger')
        return redirect(url_for('home'))

    existing = FilmLike.query.filter_by(user_id=current_user.id, tmdb_id=str(imdb_id)).first()
    if existing:
        db.session.delete(existing)
        flash('Removed like.', 'info')
    else:
        db.session.add(FilmLike(
            user_id=current_user.id,
            tmdb_id=str(imdb_id),
            title=movie['Title'],
            poster_url=movie.get('Poster'),
        ))
        flash('Liked this film.', 'success')
    db.session.commit()
    return redirect(url_for('film_page', imdb_id=imdb_id))


@app.route('/film/<imdb_id>/watched', methods=['POST'])
@login_required
def mark_watched(imdb_id):
    movie = fetch_movie_by_id(imdb_id)
    if not movie:
        flash('Film not found.', 'danger')
        return redirect(url_for('home'))

    existing = WatchedFilm.query.filter_by(user_id=current_user.id, tmdb_id=str(imdb_id)).first()
    if existing:
        db.session.delete(existing)
        flash('Marked as unwatched.', 'info')
    else:
        db.session.add(WatchedFilm(
            user_id=current_user.id,
            tmdb_id=str(imdb_id),
            title=movie['Title'],
            poster_url=movie.get('Poster'),
        ))
        # Drop from watchlist when marked watched
        wl = Watchlist.query.filter_by(user_id=current_user.id, title=movie['Title']).first()
        if wl:
            db.session.delete(wl)
        flash('Marked as watched.', 'success')
    db.session.commit()
    return redirect(url_for('film_page', imdb_id=imdb_id))


@app.route('/film/<imdb_id>/add_to_list', methods=['POST'])
@login_required
def add_film_to_user_list(imdb_id):
    movie = fetch_movie_by_id(imdb_id)
    if not movie:
        flash('Film not found.', 'danger')
        return redirect(url_for('home'))

    list_id = request.form.get('list_id', type=int)
    movie_list = MovieList.query.get_or_404(list_id)
    if movie_list.user_id != current_user.id:
        flash('Not your list.', 'danger')
        return redirect(url_for('film_page', imdb_id=imdb_id))

    title = movie['Title']
    exists = ListMovie.query.filter(
        ListMovie.list_id == movie_list.id,
        (ListMovie.imdb_id == imdb_id) | (ListMovie.title == title),
    ).first()
    if exists:
        flash('Already on that stack.', 'info')
    else:
        max_pos = db.session.query(func.max(ListMovie.position)).filter_by(list_id=movie_list.id).scalar()
        db.session.add(ListMovie(
            title=title,
            poster_url=movie.get('Poster'),
            year=(movie.get('Year') or '')[:4] or None,
            imdb_id=imdb_id,
            position=(max_pos or 0) + 1,
            list_id=movie_list.id,
        ))
        touch_stack(movie_list)
        db.session.commit()
        flash(f'Added to “{movie_list.name}”.', 'success')
    return redirect(url_for('film_page', imdb_id=imdb_id))


@app.route('/members')
def members():
    users = User.query.order_by(User.username).all()
    member_cards = []
    for user in users:
        member_cards.append({
            'user': user,
            'review_count': Post.query.filter_by(user_id=user.id).count(),
            'diary_count': DiaryEntry.query.filter_by(user_id=user.id).count(),
            'follower_count': user.followers.count(),
            'favorites': FavoriteFilm.query.filter_by(user_id=user.id).order_by(FavoriteFilm.position).limit(4).all(),
        })
    return render_view('members', members=member_cards)


@app.route('/film/title/<path:title>')
def film_by_title(title):
    movie = fetch_movie_by_title(title)
    if movie and movie.get('imdbID'):
        return redirect(url_for('film_page', imdb_id=movie['imdbID']))
    flash("Film not found.", "danger")
    return redirect(url_for('search_movie', query=title))


@app.route('/log/<imdb_id>', methods=['GET', 'POST'])
@login_required
def log_film(imdb_id):
    movie = fetch_movie_by_id(imdb_id)
    if not movie:
        flash("Film not found.", "danger")
        return redirect(url_for('home'))

    if request.method == 'POST':
        rating = parse_rating(request.form.get('rating'))
        review = request.form.get('review', '').strip()
        is_rewatch = bool(request.form.get('is_rewatch'))
        watched_date, date_error = parse_watched_date(request.form.get('watched_date'), date.today())
        if date_error or len(review) > REVIEW_MAX_LENGTH:
            flash(date_error or f"Reviews are limited to {REVIEW_MAX_LENGTH} characters.", "warning")
            return redirect(url_for('log_film', imdb_id=imdb_id))

        title = movie.get('Title')

        def work():
            # One diary entry per film: logging it again updates the entry and marks it a rewatch.
            already = find_diary_entry(current_user.id, imdb_id, title) is not None
            save_film_log(
                current_user.id,
                imdb_id=imdb_id,
                title=title,
                poster=movie.get('Poster'),
                year=movie.get('Year'),
                rating=rating,
                review=review or None,
                is_rewatch=is_rewatch or already,
                watched_date=watched_date,
            )
            # Remove from watchlist if present
            existing = Watchlist.query.filter_by(user_id=current_user.id, title=title).first()
            if existing:
                db.session.delete(existing)

        run_in_transaction(work)
        flash("Film logged.", "success")
        return redirect(url_for('film_page', imdb_id=imdb_id))

    return render_view('log_film', movie=movie, today=date.today().isoformat())


@app.route('/rate/<imdb_id>', methods=['POST'])
@login_required
def rate_film(imdb_id):
    movie = fetch_movie_by_id(imdb_id)
    if not movie:
        flash("Film not found.", "danger")
        return redirect(url_for('home'))

    rating = parse_rating(request.form.get('rating'))
    if not rating:
        flash("Pick a rating between 1 and 10 stars (half steps).", "warning")
        return redirect(url_for('film_page', imdb_id=imdb_id))

    title = movie.get('Title')
    review_text = request.form.get('content', '').strip()
    # Letterboxd-style options; only present on the full review form, so quick star ratings leave them alone.
    from_review_form = request.form.get('review_form') == '1'
    is_rewatch = (request.form.get('is_rewatch') == '1') if from_review_form else None
    has_spoilers = (request.form.get('has_spoilers') == '1') if from_review_form else None

    if len(review_text) > REVIEW_MAX_LENGTH:
        flash(f"Reviews are limited to {REVIEW_MAX_LENGTH} characters.", "warning")
        return redirect(url_for('film_page', imdb_id=imdb_id))
    watched_date, date_error = parse_watched_date(request.form.get('watched_date'))
    if date_error:
        flash(date_error, "warning")
        return redirect(url_for('film_page', imdb_id=imdb_id))

    # Rating, review, diary entry and watched mark are saved together in one transaction.
    run_in_transaction(lambda: save_film_log(
        current_user.id,
        imdb_id=imdb_id,
        title=title,
        poster=movie.get('Poster'),
        year=movie.get('Year'),
        rating=rating,
        review=review_text if from_review_form else (review_text or None),
        is_rewatch=is_rewatch,
        has_spoilers=has_spoilers,
        watched_date=watched_date,
    ))
    flash("Review saved to your diary." if review_text else "Rating saved to your diary.", "success")
    return redirect(url_for('film_page', imdb_id=imdb_id))


@app.route('/add', methods=['GET', 'POST'])
@login_required
def add_post():
    if request.method == 'POST':
        movie = request.form['movie'].strip()
        content = request.form['content'].strip()
        rating = parse_rating(request.form.get('rating'))
        poster_url = request.form.get('poster_url', '').strip()
        imdb_id = request.form.get('imdb_id', '').strip() or None

        if not movie or not content or not rating:
            flash('All fields are required.', 'danger')
            return redirect(url_for('add_post'))

        if len(content) > REVIEW_MAX_LENGTH:
            flash(f'Reviews are limited to {REVIEW_MAX_LENGTH} characters.', 'danger')
            return redirect(url_for('add_post'))

        run_in_transaction(lambda: save_film_log(
            current_user.id,
            imdb_id=imdb_id,
            title=movie,
            poster=poster_url,
            rating=rating,
            review=content,
        ))
        flash('Review added to your diary!', 'success')
        if imdb_id:
            return redirect(url_for('film_page', imdb_id=imdb_id))
        return redirect(url_for('home'))

    return render_view('add_post')


@app.route('/add_diary', methods=['GET', 'POST'])
@login_required
def add_diary():
    if request.method == 'POST':
        title = request.form['title'].strip()
        rating = parse_rating(request.form.get('rating'))
        review = request.form['review'].strip()
        poster_url = request.form.get('poster_url', '')
        imdb_id = request.form.get('imdb_id', '').strip() or None
        is_rewatch = bool(request.form.get('is_rewatch'))
        watched_date, date_error = parse_watched_date(request.form.get('watched_date'), date.today())
        if date_error or len(review) > REVIEW_MAX_LENGTH or not title:
            flash(date_error or f"Reviews are limited to {REVIEW_MAX_LENGTH} characters.", "danger")
            return redirect(url_for('add_diary'))

        def work():
            already = find_diary_entry(current_user.id, imdb_id, title) is not None
            save_film_log(
                current_user.id,
                imdb_id=imdb_id,
                title=title,
                poster=poster_url,
                rating=rating,
                review=review or None,
                is_rewatch=is_rewatch or already,
                watched_date=watched_date,
            )

        run_in_transaction(work)
        flash("Diary entry saved!", "success")
        return redirect(url_for('diary'))

    return render_view('add_diary', today=date.today().isoformat())


@app.route('/like/<int:post_id>', methods=['POST'])
@login_required
def like_post(post_id):
    post = Post.query.get_or_404(post_id)
    existing = PostLike.query.filter_by(user_id=current_user.id, post_id=post.id).first()
    if existing:
        db.session.delete(existing)
        post.likes = max(0, (post.likes or 0) - 1)
    else:
        db.session.add(PostLike(user_id=current_user.id, post_id=post.id))
        post.likes = (post.likes or 0) + 1
    db.session.commit()
    return redirect(request.referrer or url_for('home'))


@app.route('/comment/<int:post_id>', methods=['POST'])
@login_required
def add_comment(post_id):
    text = request.form['comment'].strip()
    if not text:
        flash("Comment cannot be empty.", "warning")
        return redirect(url_for('home'))

    comment = Comment(text=text, user_id=current_user.id, post_id=post_id)
    db.session.add(comment)
    db.session.commit()
    flash('Comment added.', 'success')
    return redirect(url_for('home'))


@app.route('/search_movie')
def search_movie():
    query = request.args.get('query')
    movies = []
    missing_key = not TMDB_API_KEY
    if query and not missing_key:
        movies = search_tmdb_movies(query, limit=8)
    elif query and missing_key:
        flash('Set TMDB_API_KEY to enable film search.', 'warning')

    return render_view(
        'search',
        movies=movies,
        query=query,
        missing_key=missing_key
    )


@app.route('/schedule')
def schedule():
    if not TMDB_API_KEY:
        flash('Set TMDB_API_KEY to load the release schedule.', 'warning')
        return redirect(url_for('home'))

    # Legacy tab param still accepted; prefer status/platform/media.
    legacy_tab = request.args.get('tab', '').strip().lower()
    status = request.args.get('status', '').strip().lower()
    platform = request.args.get('platform', '').strip().lower()
    media = request.args.get('media', '').strip().lower()

    if not status:
        if legacy_tab in {'now-playing', 'ott-recent'}:
            status = 'released'
        elif legacy_tab in {'theaters', 'ott-upcoming', 'announced'}:
            status = 'upcoming'
        elif legacy_tab == 'languages':
            status = 'languages'
        else:
            status = 'released'
    if status == 'announced':
        status = 'upcoming'
    if status not in {'released', 'upcoming', 'languages'}:
        status = 'released'

    if not platform:
        if legacy_tab in {'ott-upcoming', 'ott-recent'}:
            platform = 'ott'
        else:
            platform = 'theaters'
    if platform not in {'theaters', 'ott'}:
        platform = 'theaters'

    if media not in {'all', 'movies', 'shows'}:
        media = 'all'

    # Shows don't use theatre/OTT — keep OTT-style TV schedule.
    if media == 'shows':
        platform = 'ott'

    # All + Released defaults to theatrical new movies (dense date board).
    if media == 'all' and status == 'released' and platform not in {'ott'}:
        platform = 'theaters'

    lang = request.args.get('lang', '').strip().lower()
    if lang and lang not in all_language_codes():
        lang = ''

    lang_kind = request.args.get('kind', 'theatrical').strip().lower()
    if lang_kind not in {'theatrical', 'ott'}:
        lang_kind = 'theatrical'

    if lang:
        status = 'languages'

    data = fetch_release_schedule()
    lang_data = (
        fetch_language_releases(lang)
        if status == 'languages' and lang
        else {'theatrical': [], 'ott': []}
    )
    lang_theatrical_groups = group_schedule_by_date(lang_data['theatrical']) if lang else []
    lang_ott_groups = group_schedule_by_date(lang_data['ott']) if lang else []

    movie_pool = []
    tv_pool = []
    reverse_dates = False

    if status == 'released':
        reverse_dates = True
        if platform == 'theaters':
            movie_pool = list(data['now_playing'])
        else:
            movie_pool = list(data['ott_recent'])
        tv_pool = list(data['tv_released'])
    elif status == 'upcoming':
        if platform == 'theaters':
            movie_pool = list(data['upcoming'])
        else:
            movie_pool = list(data['ott_upcoming'])
        tv_pool = list(data['tv_upcoming'])

    if media == 'movies':
        selected = movie_pool
    elif media == 'shows':
        selected = tv_pool
    else:
        # All → dense movie day-board for both Theatres and OTT
        selected = list(movie_pool)
        if status == 'released' and platform in {'theaters', 'ott'}:
            pass
        else:
            selected = movie_pool + tv_pool
            selected.sort(key=lambda c: (c['release_date'] or date.max, -c['popularity']))

    if reverse_dates:
        selected = sorted(
            selected,
            key=lambda c: (c['release_date'] is None, -(c['release_date'].toordinal() if c['release_date'] else 0), -c['popularity']),
        )

    schedule_groups = (
        group_schedule_by_date(selected, reverse=reverse_dates)
        if status != 'languages'
        else []
    )

    return render_view(
        'schedule',
        upcoming=data['upcoming'],
        now_playing=data['now_playing'],
        ott_upcoming=data['ott_upcoming'],
        ott_recent=data['ott_recent'],
        as_of=data['as_of'],
        active_tab=legacy_tab or status,
        schedule_status=status,
        schedule_platform=platform,
        schedule_media=media,
        schedule_groups=schedule_groups,
        language_groups=LANGUAGE_GROUPS,
        selected_lang=lang,
        selected_lang_label=language_label(lang) if lang else '',
        lang_kind=lang_kind,
        lang_theatrical=lang_data['theatrical'],
        lang_ott=lang_data['ott'],
        lang_theatrical_groups=lang_theatrical_groups,
        lang_ott_groups=lang_ott_groups,
    )


@app.route('/profile/<username>')
def user_profile(username):
    user = User.query.filter_by(username=username).first_or_404()
    sync_user_film_logs(user.id)
    tab = request.args.get('tab', 'reviews')
    posts = Post.query.filter_by(user_id=user.id).order_by(Post.created_at.desc()).all()
    favorites = FavoriteFilm.query.filter_by(user_id=user.id).order_by(FavoriteFilm.position).all()
    diary_entries = DiaryEntry.query.filter_by(user_id=user.id).order_by(DiaryEntry.watched_date.desc()).limit(24).all()
    watched_films = WatchedFilm.query.filter_by(user_id=user.id).order_by(WatchedFilm.watched_at.desc()).limit(48).all()
    liked_films = FilmLike.query.filter_by(user_id=user.id).order_by(FilmLike.created_at.desc()).limit(48).all()
    stats = user_profile_stats(user.id)
    is_following = current_user.is_authenticated and current_user.is_following(user) and current_user != user
    return render_view(
        'profile',
        user=user,
        posts=posts,
        favorites=favorites,
        diary_entries=diary_entries,
        diary_months=group_diary_by_month(diary_entries),
        watched_films=watched_films,
        liked_films=liked_films,
        stats=stats,
        tab=tab,
        is_following=is_following
    )


@app.route('/follow/<username>', methods=['POST'])
@login_required
def follow_user(username):
    target_user = User.query.filter_by(username=username).first_or_404()
    if current_user != target_user and not current_user.is_following(target_user):
        current_user.follow(target_user)
        db.session.commit()
        flash(f"You are now following {username}!", "success")
    return redirect(request.referrer or url_for('user_profile', username=username))


@app.route('/unfollow/<username>', methods=['POST'])
@login_required
def unfollow_user(username):
    target_user = User.query.filter_by(username=username).first_or_404()
    if current_user != target_user and current_user.is_following(target_user):
        current_user.unfollow(target_user)
        db.session.commit()
        flash(f"You unfollowed {username}.", "info")
    return redirect(request.referrer or url_for('user_profile', username=username))


@app.route('/edit/<int:post_id>', methods=['GET', 'POST'])
@login_required
def edit_post(post_id):
    post = Post.query.get_or_404(post_id)
    if post.user_id != current_user.id:
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('home'))

    if request.method == 'POST':
        new_title = request.form['movie'].strip()
        new_content = request.form['content'].strip()
        if len(new_content) > REVIEW_MAX_LENGTH:
            flash(f'Reviews are limited to {REVIEW_MAX_LENGTH} characters.', 'danger')
            return render_view('edit_post', post=post)

        def work():
            save_film_log(
                current_user.id,
                imdb_id=request.form.get('imdb_id', '').strip() or post.imdb_id,
                title=new_title,
                poster=request.form.get('poster_url', '').strip() or post.poster_url,
                rating=parse_rating(request.form.get('rating'), post.rating),
                review=new_content,
                post=post,
            )

        run_in_transaction(work)
        flash('Review updated.', 'success')
        return redirect(url_for('user_profile', username=current_user.username))

    return render_view('edit_post', post=post)


@app.route('/delete/<int:post_id>')
@login_required
def delete_post(post_id):
    post = Post.query.get_or_404(post_id)
    if post.user_id != current_user.id:
        flash("You can't delete someone else's post!", 'danger')
        return redirect(url_for('home'))

    film_id, title = post.imdb_id, post.movie
    Comment.query.filter_by(post_id=post.id).delete()
    if real_review_text(post):
        # Deleting the written review keeps the star rating (and so the rating-only diary entry).
        post.content = f'{REVIEW_PLACEHOLDER}{post.movie}.'
        post.has_spoilers = False
        flash('Review deleted. Your rating is kept.', 'success')
    else:
        PostLike.query.filter_by(post_id=post.id).delete()
        db.session.delete(post)
        flash('Rating removed.', 'success')
    db.session.flush()
    resync_diary_after_post_change(current_user.id, film_id, title)
    db.session.commit()
    return redirect(request.referrer or url_for('home'))


@app.route('/dashboard')
@login_required
def dashboard():
    posts = Post.query.filter_by(user_id=current_user.id).order_by(Post.created_at.desc()).all()
    return render_view('dashboard', user=current_user, posts=posts)


@app.route('/diary')
@login_required
def diary():
    diary_type = request.args.get('type', 'film').strip().lower()
    if diary_type not in {'film', 'series'}:
        diary_type = 'film'

    if diary_type == 'series':
        entries = user_series_diary_entries(current_user.id)
    else:
        entries = DiaryEntry.query.filter_by(user_id=current_user.id).order_by(
            DiaryEntry.watched_date.desc()
        ).all()

    return render_view(
        'diary',
        entries=entries,
        diary_months=group_diary_by_month(entries),
        diary_type=diary_type,
    )


@app.route('/delete_diary/<int:entry_id>', methods=['POST'])
@login_required
def delete_diary(entry_id):
    entry = DiaryEntry.query.get_or_404(entry_id)
    if entry.user_id != current_user.id:
        flash("Not authorized to delete this entry.", "danger")
        return redirect(url_for('diary'))

    remove_film_log(current_user.id, entry)
    db.session.commit()
    flash("Removed from your diary, along with your rating and review.", "info")
    return redirect(url_for('diary'))


@app.route('/edit_diary/<int:entry_id>', methods=['GET', 'POST'])
@login_required
def edit_diary(entry_id):
    entry = DiaryEntry.query.get_or_404(entry_id)

    if entry.user_id != current_user.id:
        flash("Not authorized to edit this entry.", "danger")
        return redirect(url_for('diary'))

    if request.method == 'POST':
        new_title = request.form['title'].strip()
        new_review = request.form['review'].strip()
        watched_date, date_error = parse_watched_date(request.form.get('watched_date'), entry.watched_date)
        if date_error or not new_title or len(new_review) > REVIEW_MAX_LENGTH:
            flash(date_error or f"Reviews are limited to {REVIEW_MAX_LENGTH} characters.", "danger")
            return render_view('edit_diary', entry=entry)

        def work():
            save_film_log(
                current_user.id,
                imdb_id=request.form.get('imdb_id', '').strip() or entry.imdb_id,
                title=new_title,
                poster=request.form['poster_url'].strip() or entry.poster_url,
                rating=parse_rating(request.form.get('rating'), entry.rating),
                review=new_review,
                is_rewatch=bool(request.form.get('is_rewatch')),
                watched_date=watched_date,
                entry=entry,
            )

        try:
            run_in_transaction(work)
        except IntegrityError:
            flash("You already have that film in your diary.", "danger")
            return render_view('edit_diary', entry=db.session.get(DiaryEntry, entry_id))
        flash("Diary entry updated!", "success")
        return redirect(url_for('diary'))

    return render_view('edit_diary', entry=entry)


@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        new_email = request.form['email'].strip()
        current_password = request.form['current_password']
        new_password = request.form['new_password']

        if new_email and new_email != current_user.email:
            if User.query.filter_by(email=new_email).first():
                flash("Email already taken.", "danger")
            else:
                current_user.email = new_email
                flash("Email updated.", "success")

        if current_password and new_password:
            if check_password_hash(current_user.password, current_password):
                current_user.password = generate_password_hash(new_password)
                flash("Password changed successfully!", "success")
            else:
                flash("Current password is incorrect.", "danger")

        db.session.commit()
        return redirect(url_for('settings'))

    return render_view('settings')


@app.route('/edit_profile', methods=['GET', 'POST'])
@login_required
def edit_profile():
    if request.method == 'POST':
        username = request.form['username']
        bio = request.form['bio']
        file = request.files.get('profile_pic')

        current_user.username = username
        current_user.bio = bio

        if file and file.filename != '':
            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            current_user.profile_pic = filename

        # Favorites (up to 4)
        FavoriteFilm.query.filter_by(user_id=current_user.id).delete()
        for pos in range(1, 5):
            title = request.form.get(f'fav_title_{pos}', '').strip()
            poster = request.form.get(f'fav_poster_{pos}', '').strip()
            imdb_id = request.form.get(f'fav_imdb_{pos}', '').strip() or None
            if title:
                db.session.add(FavoriteFilm(
                    user_id=current_user.id,
                    title=title,
                    poster_url=poster or None,
                    imdb_id=imdb_id,
                    position=pos
                ))

        db.session.commit()
        flash("Profile updated successfully!", "success")
        return redirect(url_for('user_profile', username=current_user.username))

    favorites = FavoriteFilm.query.filter_by(user_id=current_user.id).order_by(FavoriteFilm.position).all()
    fav_map = {f.position: f for f in favorites}
    return render_view('edit_profile', fav_map=fav_map)


@app.route('/feed')
@login_required
def followers_feed():
    followed_users = current_user.followed.all()
    followed_ids = [user.id for user in followed_users]

    posts = []
    diary_entries = []
    if followed_ids:
        posts = Post.query.filter(Post.user_id.in_(followed_ids)).order_by(Post.created_at.desc()).limit(40).all()
        diary_entries = DiaryEntry.query.filter(
            DiaryEntry.user_id.in_(followed_ids)
        ).order_by(DiaryEntry.watched_date.desc()).limit(40).all()

    activity = []
    for post in posts:
        activity.append({
            'kind': 'review',
            'user': post.author,
            'title': post.movie,
            'poster_url': post.poster_url,
            'rating': post.rating,
            'text': post.content,
            'imdb_id': post.imdb_id,
            'when': post.created_at,
        })
    for entry in diary_entries:
        activity.append({
            'kind': 'log',
            'user': entry.user,
            'title': entry.title,
            'poster_url': entry.poster_url,
            'rating': entry.rating,
            'text': entry.review,
            'imdb_id': entry.imdb_id,
            'when': datetime.combine(entry.watched_date, datetime.min.time()),
            'is_rewatch': entry.is_rewatch,
        })

    activity.sort(key=lambda item: item['when'] or datetime.min, reverse=True)
    return render_view('feed', activity=activity[:50])


@app.route('/reviews')
def reviews():
    posts = Post.query.order_by(Post.created_at.desc()).limit(50).all()
    return render_view('reviews', posts=posts)


@app.route('/lists')
def lists():
    public_lists = (
        MovieList.query
        .filter_by(is_public=True)
        .order_by(MovieList.updated_at.desc(), MovieList.id.desc())
        .limit(48)
        .all()
    )
    public_cards = build_stack_gallery_cards(public_lists)

    my_lists = []
    my_cards = []
    if current_user.is_authenticated:
        my_lists = (
            MovieList.query
            .filter_by(user_id=current_user.id)
            .order_by(MovieList.updated_at.desc(), MovieList.id.desc())
            .all()
        )
        my_cards = build_stack_gallery_cards(my_lists)

    return render_view(
        'lists',
        public_cards=public_cards,
        my_cards=my_cards,
        lists=my_lists,
    )


@app.route('/lists/create', methods=['GET', 'POST'])
@login_required
def create_list():
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip() or 'Untitled stack'
        description = request.form.get('description') or ''
        is_public = request.form.get('is_public') in ('1', 'on', 'true', 'yes')

        new_list = MovieList(
            name=name,
            description=description,
            is_public=is_public,
            user_id=current_user.id,
            updated_at=datetime.utcnow(),
        )
        db.session.add(new_list)
        db.session.commit()
        flash("Stack created — add films below.", "success")
        return redirect(url_for('view_list', list_id=new_list.id))

    return render_view('create_list')


@app.route('/lists/<int:list_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_list(list_id):
    movie_list = MovieList.query.get_or_404(list_id)

    if movie_list.user_id != current_user.id:
        flash("You are not allowed to edit this stack.", "danger")
        return redirect(url_for('lists'))

    if request.method == 'POST':
        movie_list.name = (request.form.get('name') or '').strip() or movie_list.name
        movie_list.description = request.form.get('description') or ''
        movie_list.is_public = request.form.get('is_public') in ('1', 'on', 'true', 'yes')
        touch_stack(movie_list)
        db.session.commit()
        flash("Stack updated.", "success")
        return redirect(url_for('view_list', list_id=list_id))

    return render_view('edit_list', movie_list=movie_list)


@app.route('/lists/<int:list_id>/visibility', methods=['POST'])
@login_required
def toggle_stack_visibility(list_id):
    movie_list = MovieList.query.get_or_404(list_id)
    if movie_list.user_id != current_user.id:
        flash("You can’t change this stack’s visibility.", "danger")
        return redirect(url_for('lists'))

    movie_list.is_public = not bool(movie_list.is_public)
    touch_stack(movie_list)
    db.session.commit()
    flash(
        'Stack is now public.' if movie_list.is_public else 'Stack is now private.',
        'success',
    )
    return redirect(url_for('view_list', list_id=list_id))


@app.route('/lists/<int:list_id>/banner', methods=['POST'])
@login_required
def upload_stack_banner(list_id):
    movie_list = MovieList.query.get_or_404(list_id)
    if movie_list.user_id != current_user.id:
        flash("You can’t change this banner.", "danger")
        return redirect(url_for('view_list', list_id=list_id))

    file = request.files.get('banner')
    if not file or file.filename == '':
        flash('Choose an image for the banner.', 'warning')
        return redirect(url_for('view_list', list_id=list_id))

    if not allowed_file(file.filename):
        flash('Use a JPG, PNG, GIF, or WebP image.', 'danger')
        return redirect(url_for('view_list', list_id=list_id))

    os.makedirs(app.config['STACK_BANNER_FOLDER'], exist_ok=True)
    ext = file.filename.rsplit('.', 1)[1].lower()
    filename = secure_filename(f"stack_{list_id}_{int(datetime.utcnow().timestamp())}.{ext}")
    filepath = os.path.join(app.config['STACK_BANNER_FOLDER'], filename)
    file.save(filepath)
    movie_list.banner_url = url_for('static', filename=f'stack_banners/{filename}')
    touch_stack(movie_list)
    db.session.commit()
    flash('Banner updated.', 'success')
    return redirect(url_for('view_list', list_id=list_id))


@app.route('/lists/<int:list_id>/delete', methods=['POST'])
@login_required
def delete_list(list_id):
    movie_list = MovieList.query.get_or_404(list_id)

    if movie_list.user_id != current_user.id:
        flash("You are not allowed to delete this stack.", "danger")
        return redirect(url_for('lists'))

    ListMovie.query.filter_by(list_id=movie_list.id).delete()
    StackLike.query.filter_by(list_id=movie_list.id).delete()
    StackComment.query.filter_by(list_id=movie_list.id).delete()
    db.session.delete(movie_list)
    db.session.commit()
    flash("Stack deleted.", "success")
    return redirect(url_for('lists'))


@app.route('/lists/<int:list_id>')
def view_list(list_id):
    movie_list = MovieList.query.get_or_404(list_id)
    owner = db.session.get(User, movie_list.user_id)
    is_owner = current_user.is_authenticated and movie_list.user_id == current_user.id

    if not movie_list.is_public and not is_owner:
        flash('This stack is private.', 'warning')
        return redirect(url_for('lists'))

    movies = (
        ListMovie.query
        .filter_by(list_id=movie_list.id)
        .order_by(ListMovie.position.asc(), ListMovie.id.asc())
        .all()
    )

    like_count = StackLike.query.filter_by(list_id=movie_list.id).count()
    user_liked = False
    if current_user.is_authenticated:
        user_liked = (
            StackLike.query.filter_by(user_id=current_user.id, list_id=movie_list.id).first()
            is not None
        )

    comments = (
        StackComment.query
        .filter_by(list_id=movie_list.id)
        .order_by(StackComment.created_at.desc())
        .all()
    )

    banner = stack_banner_for(movie_list, movies)

    return render_view(
        'view_list',
        movie_list=movie_list,
        stack_movies=movies,
        stack_owner=owner,
        is_owner=is_owner,
        like_count=like_count,
        user_liked=user_liked,
        stack_comments=comments,
        comment_count=len(comments),
        stack_banner=banner,
        updated_label=relative_stack_updated(movie_list.updated_at),
    )


@app.route('/lists/<int:list_id>/like', methods=['POST'])
@login_required
def like_stack(list_id):
    movie_list = MovieList.query.get_or_404(list_id)
    existing = StackLike.query.filter_by(user_id=current_user.id, list_id=list_id).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
        liked = False
    else:
        db.session.add(StackLike(user_id=current_user.id, list_id=list_id))
        db.session.commit()
        liked = True

    count = StackLike.query.filter_by(list_id=list_id).count()
    if request.headers.get('X-Requested-With') == 'fetch':
        return jsonify({'ok': True, 'liked': liked, 'count': count})
    return redirect(url_for('view_list', list_id=list_id))


@app.route('/lists/<int:list_id>/comments', methods=['POST'])
@login_required
def add_stack_comment(list_id):
    MovieList.query.get_or_404(list_id)
    text = (request.form.get('text') or '').strip()
    if not text:
        flash('Comment cannot be empty.', 'warning')
        return redirect(url_for('view_list', list_id=list_id) + '#stack-comments')

    db.session.add(StackComment(text=text, user_id=current_user.id, list_id=list_id))
    db.session.commit()
    flash('Comment added.', 'success')
    return redirect(url_for('view_list', list_id=list_id) + '#stack-comments')


@app.route('/lists/<int:list_id>/add_movie', methods=['POST'])
@login_required
def add_movie_to_list(list_id):
    movie_list = MovieList.query.get_or_404(list_id)

    if movie_list.user_id != current_user.id:
        flash("You can’t add movies to someone else’s stack.", "danger")
        return redirect(url_for('lists'))

    wants_json = request.headers.get('X-Requested-With') == 'fetch' or request.is_json
    films = []

    if request.is_json:
        payload = request.get_json(silent=True) or {}
        raw = payload.get('movies') or []
        if isinstance(raw, list):
            films = raw
    elif request.form.get('movies_json'):
        try:
            raw = json.loads(request.form.get('movies_json') or '[]')
            if isinstance(raw, list):
                films = raw
        except Exception:
            films = []

    if not films:
        title = (request.form.get('title') or '').strip()
        if title:
            films = [{
                'title': title,
                'poster_url': request.form.get('poster_url') or '',
                'year': (request.form.get('year') or '').strip() or None,
                'imdb_id': (request.form.get('imdb_id') or '').strip() or None,
            }]

    if not films:
        msg = 'Pick at least one film from search first.'
        if wants_json:
            return jsonify({'ok': False, 'error': msg}), 400
        flash(msg, 'warning')
        return redirect(url_for('view_list', list_id=list_id))

    # Soft cap so one request can't dump hundreds
    films = films[:50]

    max_pos = db.session.query(func.max(ListMovie.position)).filter_by(list_id=movie_list.id).scalar()
    next_pos = (max_pos or 0) + 1
    added = 0
    skipped = 0
    seen_keys = set()

    for item in films:
        if not isinstance(item, dict):
            skipped += 1
            continue
        title = (item.get('title') or '').strip()
        if not title:
            skipped += 1
            continue
        poster_url = item.get('poster_url') or item.get('poster') or ''
        year = (str(item.get('year') or '').strip() or None)
        if year:
            year = year[:10]
        imdb_id = (str(item.get('imdb_id') or item.get('imdbID') or '').strip() or None)

        key = imdb_id or title.lower()
        if key in seen_keys:
            skipped += 1
            continue
        seen_keys.add(key)

        if imdb_id:
            exists = ListMovie.query.filter_by(list_id=movie_list.id, imdb_id=imdb_id).first()
        else:
            exists = ListMovie.query.filter_by(list_id=movie_list.id, title=title).first()
        if exists:
            skipped += 1
            continue

        db.session.add(ListMovie(
            title=title,
            poster_url=poster_url,
            year=year,
            imdb_id=imdb_id,
            position=next_pos,
            list_id=movie_list.id,
        ))
        next_pos += 1
        added += 1

    if added:
        touch_stack(movie_list)
    db.session.commit()

    if wants_json:
        return jsonify({'ok': True, 'added': added, 'skipped': skipped})

    if added and skipped:
        flash(f'Added {added} film{"s" if added != 1 else ""}. Skipped {skipped} duplicate{"s" if skipped != 1 else ""}.', 'success')
    elif added:
        flash(f'Added {added} film{"s" if added != 1 else ""} to stack.', 'success')
    else:
        flash('Those films are already in this stack.', 'info')
    return redirect(url_for('view_list', list_id=list_id))


@app.route('/lists/<int:list_id>/movies/<int:movie_id>/remove', methods=['POST'])
@login_required
def remove_movie_from_list(list_id, movie_id):
    movie_list = MovieList.query.get_or_404(list_id)
    if movie_list.user_id != current_user.id:
        flash("Access denied.", "danger")
        return redirect(url_for('lists'))

    entry = ListMovie.query.filter_by(id=movie_id, list_id=list_id).first_or_404()
    db.session.delete(entry)
    touch_stack(movie_list)
    db.session.commit()
    flash("Film removed.", "success")
    return redirect(url_for('view_list', list_id=list_id))


@app.route('/lists/<int:list_id>/movies/<int:movie_id>/note', methods=['POST'])
@login_required
def update_list_movie_note(list_id, movie_id):
    movie_list = MovieList.query.get_or_404(list_id)
    if movie_list.user_id != current_user.id:
        flash("Access denied.", "danger")
        return redirect(url_for('lists'))

    entry = ListMovie.query.filter_by(id=movie_id, list_id=list_id).first_or_404()
    entry.note = (request.form.get('note') or '').strip() or None
    touch_stack(movie_list)
    db.session.commit()
    flash("Note saved.", "success")
    return redirect(url_for('view_list', list_id=list_id))


@app.route('/lists/<int:list_id>/reorder', methods=['POST'])
@login_required
def reorder_list_movies(list_id):
    movie_list = MovieList.query.get_or_404(list_id)
    if movie_list.user_id != current_user.id:
        return jsonify({'ok': False}), 403

    order = request.json.get('order') if request.is_json else None
    if not isinstance(order, list):
        return jsonify({'ok': False}), 400

    for index, movie_id in enumerate(order, start=1):
        entry = ListMovie.query.filter_by(id=int(movie_id), list_id=list_id).first()
        if entry:
            entry.position = index
    touch_stack(movie_list)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/popular')
def popular():
    return render_view('popular', movies=get_popular_movies(limit=24))


@app.route('/series/popular')
def popular_series_page():
    return render_view('popular_series', series=get_popular_series(limit=24))


@app.route('/add_to_watchlist', methods=['POST'])
@login_required
def add_to_watchlist():
    title = request.form['title']
    poster_url = request.form['poster_url']
    imdb_id = request.form.get('imdb_id')

    existing = Watchlist.query.filter_by(user_id=current_user.id, title=title).first()
    if existing:
        flash("Movie already in your watchlist.", "info")
        if imdb_id:
            return redirect(url_for('film_page', imdb_id=imdb_id))
        return redirect(url_for('watchlist'))

    movie = Watchlist(
        title=title, poster_url=poster_url, user_id=current_user.id,
        media_type='movie', tmdb_id=str(imdb_id) if imdb_id else None,
    )
    db.session.add(movie)
    db.session.commit()
    flash("Movie added to your watchlist!", "success")
    if imdb_id:
        return redirect(url_for('film_page', imdb_id=imdb_id))
    return redirect(url_for('watchlist'))


@app.route('/watchlist')
@login_required
def watchlist():
    movies = Watchlist.query.filter_by(user_id=current_user.id).all()
    return render_view('watchlist', movies=movies)


@app.route('/watchlist/remove/<int:movie_id>', methods=['POST'])
@login_required
def remove_from_watchlist(movie_id):
    movie = Watchlist.query.get_or_404(movie_id)
    if movie.user_id != current_user.id:
        flash("Not authorized!", "danger")
        return redirect(url_for('watchlist'))

    db.session.delete(movie)
    db.session.commit()
    flash("Movie removed from watchlist", "success")
    return redirect(url_for('watchlist'))


@app.route('/upload_profile_pic', methods=['POST'])
@login_required
def upload_profile_pic():
    if 'profile_pic' not in request.files:
        flash('No file part')
        return redirect(url_for('dashboard'))

    file = request.files['profile_pic']
    if file.filename == '':
        flash('No selected file')
        return redirect(url_for('dashboard'))

    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        current_user.profile_pic = filename
        db.session.commit()
        flash('Profile picture updated!')
    else:
        flash('Invalid file format. Please upload an image.', 'danger')

    return redirect(url_for('dashboard'))


@app.route('/search_preview')
def search_preview():
    query = request.args.get('query', '')
    if not query or not TMDB_API_KEY:
        return jsonify([])

    results = []
    for item in search_tmdb_movies(query, limit=8):
        results.append({
            'Title': item['Title'],
            'Year': item['Year'],
            'imdbID': item['imdbID'],
            'tmdbID': item['tmdb_id'],
            'Poster': item['Poster'] if item['Poster'] else 'https://via.placeholder.com/40x60?text=No+Image'
        })

    return jsonify(results)


@app.route('/api/search')
def api_search():
    """Navbar suggestions: films, series and people from TMDB multi-search (cached by tmdb_get).

    Without a query it returns this week's trending titles, so focusing the box already shows ideas.
    """
    query = (request.args.get('q') or '').strip()[:80]
    if not TMDB_API_KEY:
        return jsonify({'query': query, 'results': [], 'trending': not query})

    if query:
        data = tmdb_get('/search/multi', query=query, include_adult='false', language='en-US')
    else:
        data = tmdb_get('/trending/all/day', language='en-US')

    results = []
    for item in (data or {}).get('results') or []:
        kind = item.get('media_type')
        if kind not in ('movie', 'tv', 'person') or not item.get('id'):
            continue
        item_id = str(item['id'])
        if kind == 'movie':
            title = item.get('title') or item.get('original_title')
            date_text = item.get('release_date') or ''
            image = item.get('poster_path')
            subtitle = ' · '.join(p for p in ('Film', date_text[:4]) if p)
            url = url_for('film_page', imdb_id=item_id)
        elif kind == 'tv':
            title = item.get('name') or item.get('original_name')
            date_text = item.get('first_air_date') or ''
            image = item.get('poster_path')
            subtitle = ' · '.join(p for p in ('Series', date_text[:4]) if p)
            url = url_for('series_page', tmdb_id=item_id)
        else:
            if query == '' or not item.get('profile_path') and (item.get('popularity') or 0) < 5:
                continue
            title = item.get('name')
            image = item.get('profile_path')
            subtitle = item.get('known_for_department') or 'Person'
            url = url_for('person_page', person_id=item_id)
        if not title:
            continue
        results.append({
            'type': kind,
            'id': item_id,
            'title': title,
            'subtitle': subtitle,
            'image': tmdb_poster_url(image, size='w92') if image else None,
            'url': url,
        })
        if len(results) >= 8:
            break

    return jsonify({'query': query, 'results': results, 'trending': not query})


@app.route('/add_review/<movie_title>', methods=['GET', 'POST'])
@login_required
def add_review(movie_title):
    movie_data = fetch_movie_by_title(movie_title)

    if not movie_data:
        flash("Movie not found.", "danger")
        return redirect(url_for('dashboard'))

    imdb_id = movie_data.get('imdbID')
    if request.method == 'GET' and imdb_id:
        # Prefer the film hub; keep POST path for direct review submit
        pass

    if request.method == 'POST':
        rating = parse_rating(request.form.get('rating'))
        content = request.form.get('content', '').strip()
        poster_url = poster_or_placeholder(movie_data.get('Poster'))
        title = movie_data.get('Title') or movie_title

        if not rating:
            flash("Please choose a rating.", "warning")
            return render_view('add_review', movie=movie_data)
        if not content:
            flash("Write a few words about the film.", "warning")
            return render_view('add_review', movie=movie_data)

        if len(content) > REVIEW_MAX_LENGTH:
            flash(f"Reviews are limited to {REVIEW_MAX_LENGTH} characters.", "warning")
            return render_view('add_review', movie=movie_data)

        run_in_transaction(lambda: save_film_log(
            current_user.id,
            imdb_id=imdb_id,
            title=title,
            poster=poster_url,
            year=movie_data.get('Year'),
            rating=rating,
            review=content,
        ))
        flash("Review added to your diary!", "success")
        if imdb_id:
            return redirect(url_for('film_page', imdb_id=imdb_id))
        return redirect(url_for('dashboard'))

    return render_view('add_review', movie=movie_data)


@login_manager.unauthorized_handler
def handle_unauthorized():
    """JSON 401 for /api/*, the usual redirect-to-login (with ?next=) for pages."""
    if request.path.startswith('/api/'):
        return jsonify(ok=False, message='Sign in required.'), 401
    flash(login_manager.login_message, login_manager.login_message_category)
    return redirect(url_for('login', next=request.full_path.rstrip('?')))


def api_error(message, status):
    return jsonify(ok=False, message=message), status


def api_guard():
    """None when OK, else an error response. Mutating calls must come from our own JS (CSRF guard)."""
    if not current_user.is_authenticated:
        return api_error('Sign in required.', 401)
    if request.method in ('PATCH', 'PUT', 'DELETE', 'POST') and request.headers.get('X-Requested-With') != 'fetch':
        return api_error('Missing X-Requested-With header.', 400)
    return None


def own_diary_entry_or_none(entry_id):
    """The entry only if it belongs to the signed-in user (404 for anyone else's, so ids don't leak)."""
    return DiaryEntry.query.filter_by(id=entry_id, user_id=current_user.id).first()


@app.route('/api/diary/<int:entry_id>', methods=['PATCH'])
def api_diary_update(entry_id):
    blocked = api_guard()
    if blocked:
        return blocked
    entry = own_diary_entry_or_none(entry_id)
    if entry is None:
        return api_error('Diary entry not found.', 404)
    payload = request.get_json(silent=True) or {}
    if 'watched_date' not in payload:
        return api_error('watched_date is required.', 400)
    watched_date, date_error = parse_watched_date(str(payload.get('watched_date') or ''))
    if date_error or watched_date is None:
        return api_error(date_error or 'watched_date is required.', 400)
    entry.watched_date = watched_date
    db.session.commit()
    return jsonify(ok=True, entry=diary_entry_json(entry))


@app.route('/api/diary/<int:entry_id>', methods=['DELETE'])
def api_diary_delete(entry_id):
    blocked = api_guard()
    if blocked:
        return blocked
    entry = own_diary_entry_or_none(entry_id)
    if entry is None:
        return api_error('Diary entry not found.', 404)
    film_id = entry.imdb_id
    remove_film_log(current_user.id, entry)
    db.session.commit()
    return jsonify(ok=True, film_id=film_id)


@app.cli.command("backfill-diary")
@with_appcontext
def backfill_diary_command():
    """Create diary entries for any rating/review that lacks one (safe to run repeatedly)."""
    click.echo(f"Created {backfill_diary_from_posts()} diary entries.")


@app.cli.command("init-db")
@with_appcontext
def init_db():
    ensure_schema()
    click.echo("Database initialized successfully!")


with app.app_context():
    ensure_schema()


def _prewarm_tmdb_cache():
    """Load the data the landing pages need so the first visit isn't cold."""
    if not TMDB_API_KEY:
        return
    jobs = (
        lambda: get_popular_movies(limit=10),
        lambda: fetch_weekly_trending_movies(limit=18),
        lambda: fetch_weekly_trending_series(limit=16),
        lambda: get_popular_series(limit=12),
        lambda: fetch_series_catalog(),
        lambda: tmdb_get('/trending/all/day', language='en-US'),
    )
    with app.app_context():
        for job in jobs:
            try:
                job()
            except Exception:
                pass


if __name__ == '__main__':
    # With the debug reloader only the serving child should warm the cache.
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        threading.Thread(target=_prewarm_tmdb_cache, daemon=True).start()
    app.run(debug=True)
