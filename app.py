from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor
import threading
import requests
import click
import json
from flask.cli import with_appcontext
from werkzeug.utils import secure_filename
import os
from flask_login import LoginManager, login_user, logout_user, login_required, current_user, UserMixin
from sqlalchemy import func, text

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
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///filmboxed.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = 'your_secret_key'
app.config['UPLOAD_FOLDER'] = 'static/profile_pics'
app.config['STACK_BANNER_FOLDER'] = 'static/stack_banners'
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB max (banners)
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Reuse TLS connections; run independent TMDB calls in parallel.
_TMDB_SESSION = requests.Session()
_TMDB_CACHE = {}
_TMDB_CACHE_LOCK = threading.Lock()
_TMDB_CACHE_TTL = 1800  # 30 min for list/discover
_TMDB_DETAIL_TTL = 7200  # 2h for movie/person pages
_HTTP_POOL = ThreadPoolExecutor(max_workers=10)
_JOB_POOL = ThreadPoolExecutor(max_workers=4)

# ------------------ Helpers ------------------ #
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _tmdb_cache_key(path, params):
    items = tuple(sorted((str(k), str(v)) for k, v in params.items() if k != 'api_key'))
    return (path, items)


def tmdb_get(path, ttl=None, **params):
    """GET a TMDB v3 endpoint with session reuse + short in-memory cache."""
    if not TMDB_API_KEY:
        return None
    params = dict(params)
    if ttl is None:
        ttl = _TMDB_DETAIL_TTL if path.startswith('/movie/') or path.startswith('/person/') else _TMDB_CACHE_TTL
    key = _tmdb_cache_key(path, params)
    now = datetime.utcnow()
    with _TMDB_CACHE_LOCK:
        hit = _TMDB_CACHE.get(key)
        if hit and (now - hit['at']).total_seconds() < hit['ttl']:
            return hit['data']

    query = dict(params)
    query['api_key'] = TMDB_API_KEY
    data = None
    for attempt in range(2):
        try:
            response = _TMDB_SESSION.get(f"{TMDB_BASE}{path}", params=query, timeout=8)
            if response.status_code == 200:
                data = response.json()
                break
            if response.status_code == 429:
                continue
            if response.status_code in (500, 502, 503, 504) and attempt == 0:
                continue
            break
        except requests.RequestException:
            if attempt == 0:
                continue
            break

    store_ttl = ttl if data is not None else 45
    with _TMDB_CACHE_LOCK:
        if len(_TMDB_CACHE) > 800:
            _TMDB_CACHE.clear()
        _TMDB_CACHE[key] = {'at': datetime.utcnow(), 'ttl': store_ttl, 'data': data}
    return data


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

    cast = []
    for person in (credits.get('cast') or [])[:10]:
        if not person.get('id'):
            continue
        cast.append({
            'id': str(person['id']),
            'name': person.get('name') or 'Unknown',
            'character': person.get('character') or '',
            'photo': tmdb_poster_url(person.get('profile_path'), size='w185') if person.get('profile_path') else None,
        })

    # Prefer key production roles; keep order by importance, unique by name+job
    preferred_jobs = (
        'Director', 'Screenplay', 'Writer', 'Story', 'Novel',
        'Director of Photography', 'Original Music Composer', 'Music',
        'Producer', 'Executive Producer', 'Editor', 'Costume Design', 'Production Design',
    )
    job_rank = {job: i for i, job in enumerate(preferred_jobs)}
    crew_candidates = []
    seen = set()
    for person in credits.get('crew') or []:
        if not person.get('id'):
            continue
        job = person.get('job') or ''
        if job not in job_rank:
            continue
        key = (person.get('id'), job)
        if key in seen:
            continue
        seen.add(key)
        crew_candidates.append({
            'id': str(person['id']),
            'name': person.get('name') or 'Unknown',
            'job': job,
            'photo': tmdb_poster_url(person.get('profile_path'), size='w185') if person.get('profile_path') else None,
            '_rank': job_rank[job],
        })
    crew_candidates.sort(key=lambda item: (item['_rank'], item['name']))
    crew = []
    for item in crew_candidates[:12]:
        item.pop('_rank', None)
        crew.append(item)
    # Fallback: if preferred list is thin, add first remaining crew
    if len(crew) < 6:
        for person in credits.get('crew') or []:
            if not person.get('id'):
                continue
            job = person.get('job') or 'Crew'
            key = (person.get('id'), job)
            if key in seen:
                continue
            seen.add(key)
            crew.append({
                'id': str(person['id']),
                'name': person.get('name') or 'Unknown',
                'job': job,
                'photo': tmdb_poster_url(person.get('profile_path'), size='w185') if person.get('profile_path') else None,
            })
            if len(crew) >= 12:
                break

    trailer_url = None
    for clip in ((videos or data.get('videos') or {}).get('results') or []):
        if clip.get('site') == 'YouTube' and clip.get('type') in ('Trailer', 'Teaser') and clip.get('key'):
            trailer_url = f"https://www.youtube.com/watch?v={clip['key']}"
            break

    similar = []
    for item in ((recommendations or data.get('recommendations') or {}).get('results') or [])[:10]:
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
    if release:
        try:
            release_label = datetime.strptime(release, '%Y-%m-%d').strftime('%B %d, %Y')
        except ValueError:
            release_label = release

    return {
        'Title': data.get('title') or data.get('original_title') or 'Untitled',
        'Year': year,
        'ReleaseDate': release,
        'ReleaseLabel': release_label,
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
    }


def fetch_watch_providers(tmdb_id, regions=('IN', 'US')):
    """OTT / rent / buy availability from TMDB (JustWatch). Prefer India, then US."""
    data = tmdb_get(f'/movie/{tmdb_id}/watch/providers')
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
        'rent': map_providers(block.get('rent')),
        'buy': map_providers(block.get('buy')),
        'link': block.get('link'),
    }


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


def fetch_stream_logos(tmdb_id, regions=('IN', 'US'), limit=4):
    """Compact stream-only provider logos for schedule OTT cards."""
    info = fetch_watch_providers(tmdb_id, regions=regions)
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
        card['ott_logos'] = fetch_stream_logos(card.get('id'), regions=regions)
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
        append_to_response='credits,videos,recommendations'
    )
    # Heavy append_to_response can fail intermittently — fall back to separate calls
    if not data:
        data = tmdb_get(f'/movie/{tmdb_id}')
        if not data:
            return None
        extras = tmdb_get_many([
            (f'/movie/{tmdb_id}/credits', {}),
            (f'/movie/{tmdb_id}/videos', {}),
            (f'/movie/{tmdb_id}/recommendations', {}),
        ])
        return normalize_tmdb_movie(
            data,
            credits=extras[0] or {},
            videos=extras[1] or {},
            recommendations=extras[2] or {},
        )
    return normalize_tmdb_movie(
        data,
        credits=data.get('credits'),
        videos=data.get('videos'),
        recommendations=data.get('recommendations'),
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


def upsert_diary_for_rating(user_id, title, poster, imdb_id, rating, review=None, watched_date=None):
    """Create or update today's diary entry when a film is rated/reviewed."""
    watched_date = watched_date or date.today()
    review_text = (review or '').strip() or None
    film_id = str(imdb_id) if imdb_id else None

    entry = None
    if film_id:
        entry = DiaryEntry.query.filter_by(
            user_id=user_id,
            imdb_id=film_id,
            watched_date=watched_date,
        ).first()
    if not entry:
        entry = DiaryEntry.query.filter_by(
            user_id=user_id,
            title=title,
            watched_date=watched_date,
        ).first()

    if entry:
        entry.rating = rating
        if review_text:
            entry.review = review_text
        entry.poster_url = poster
        if film_id:
            entry.imdb_id = film_id
    else:
        prior_q = DiaryEntry.query.filter_by(user_id=user_id)
        if film_id:
            prior = prior_q.filter(
                db.or_(DiaryEntry.imdb_id == film_id, DiaryEntry.title == title)
            ).first()
        else:
            prior = prior_q.filter_by(title=title).first()
        entry = DiaryEntry(
            title=title,
            rating=rating,
            review=review_text,
            watched_date=watched_date,
            poster_url=poster,
            user_id=user_id,
            imdb_id=film_id,
            is_rewatch=bool(prior),
        )
        db.session.add(entry)

    if film_id and not WatchedFilm.query.filter_by(user_id=user_id, tmdb_id=film_id).first():
        db.session.add(WatchedFilm(
            user_id=user_id,
            tmdb_id=film_id,
            title=title,
            poster_url=poster,
        ))

    return entry


def render_view(view, **context):
    """Render the single combined template for a named view."""
    return render_template('app.html', view=view, **context)


def ensure_schema():
    """Create tables and add columns introduced after the first release."""
    db.create_all()
    os.makedirs(app.config['STACK_BANNER_FOLDER'], exist_ok=True)
    alterations = [
        ("diary_entry", "imdb_id", "VARCHAR(20)"),
        ("diary_entry", "is_rewatch", "BOOLEAN DEFAULT 0"),
        ("post", "imdb_id", "VARCHAR(20)"),
        ("list_movie", "year", "VARCHAR(10)"),
        ("list_movie", "note", "TEXT"),
        ("list_movie", "imdb_id", "VARCHAR(20)"),
        ("list_movie", "position", "INTEGER DEFAULT 0"),
        ("movie_list", "banner_url", "VARCHAR(300)"),
        ("movie_list", "updated_at", "DATETIME"),
        ("movie_list", "is_public", "BOOLEAN DEFAULT 0"),
    ]
    with db.engine.connect() as conn:
        for table, column, coltype in alterations:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))
                conn.commit()
            except Exception:
                conn.rollback()


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


class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    movie = db.Column(db.String(100), nullable=False)
    content = db.Column(db.Text, nullable=False)
    rating = db.Column(db.Integer, nullable=False)
    poster_url = db.Column(db.String(255))
    imdb_id = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    likes = db.Column(db.Integer, default=0)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)


class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), nullable=False)


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
    """Send members to the live lounge (Discord: text + voice/video, 100+ seats)."""
    return redirect(COMMUNITY_INVITE_URL)


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
        flash('Welcome to Filmboxed!', 'success')
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
            if next_page and next_page.startswith('/'):
                return redirect(next_page)
            return redirect(url_for('home'))

        flash('Invalid email or password.', 'danger')
        return redirect(url_for('login'))

    return render_view('login')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Logged out.', 'info')
    return redirect(url_for('home'))


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
    watch_providers = fetch_watch_providers(tmdb_id)

    in_watchlist = False
    user_rating = None
    user_review = ''
    liked = False
    watched = False
    user_lists = []
    if current_user.is_authenticated:
        in_watchlist = Watchlist.query.filter_by(
            user_id=current_user.id, title=title
        ).first() is not None
        own = Post.query.filter_by(user_id=current_user.id, movie=title).order_by(
            Post.created_at.desc()
        ).first()
        if own:
            user_rating = own.rating
            # Hide auto placeholder text in the form
            if own.content and not own.content.startswith('Rated '):
                user_review = own.content
        liked = FilmLike.query.filter_by(user_id=current_user.id, tmdb_id=str(tmdb_id)).first() is not None
        watched = WatchedFilm.query.filter_by(user_id=current_user.id, tmdb_id=str(tmdb_id)).first() is not None
        user_lists = MovieList.query.filter_by(user_id=current_user.id).order_by(MovieList.name).all()

    return render_view(
        'film',
        movie=movie,
        reviews=reviews,
        histogram=histogram,
        like_count=like_count,
        in_watchlist=in_watchlist,
        user_rating=user_rating,
        user_review=user_review,
        liked=liked,
        watched=watched,
        user_lists=user_lists,
        watch_providers=watch_providers,
    )


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
        watched_raw = request.form.get('watched_date') or date.today().isoformat()
        try:
            watched_date = datetime.strptime(watched_raw, '%Y-%m-%d').date()
        except ValueError:
            watched_date = date.today()

        title = movie.get('Title')
        poster = poster_or_placeholder(movie.get('Poster'))

        entry = DiaryEntry(
            title=title,
            rating=rating,
            review=review or None,
            watched_date=watched_date,
            poster_url=poster,
            user_id=current_user.id,
            imdb_id=imdb_id,
            is_rewatch=is_rewatch
        )
        db.session.add(entry)

        if not WatchedFilm.query.filter_by(user_id=current_user.id, tmdb_id=str(imdb_id)).first():
            db.session.add(WatchedFilm(
                user_id=current_user.id,
                tmdb_id=str(imdb_id),
                title=title,
                poster_url=poster,
            ))

        if review and rating:
            post = Post(
                movie=title,
                content=review,
                rating=rating,
                poster_url=poster,
                imdb_id=imdb_id,
                user_id=current_user.id
            )
            db.session.add(post)

        # Remove from watchlist if present
        existing = Watchlist.query.filter_by(user_id=current_user.id, title=title).first()
        if existing:
            db.session.delete(existing)

        db.session.commit()
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
    poster = poster_or_placeholder(movie.get('Poster'))
    review_text = request.form.get('content', '').strip()
    content = review_text or f"Rated {title}."

    post = Post.query.filter_by(user_id=current_user.id, movie=title).order_by(
        Post.created_at.desc()
    ).first()
    if post:
        post.rating = rating
        post.poster_url = poster
        post.imdb_id = imdb_id
        if review_text:
            post.content = review_text
    else:
        post = Post(
            movie=title,
            content=content,
            rating=rating,
            poster_url=poster,
            imdb_id=imdb_id,
            user_id=current_user.id
        )
        db.session.add(post)

    upsert_diary_for_rating(
        user_id=current_user.id,
        title=title,
        poster=poster,
        imdb_id=imdb_id,
        rating=rating,
        review=review_text,
    )

    db.session.commit()
    flash("Saved to your diary." if review_text else "Rating saved to your diary.", "success")
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

        post = Post(
            movie=movie,
            content=content,
            rating=rating,
            poster_url=poster_url,
            imdb_id=imdb_id,
            user_id=current_user.id
        )
        db.session.add(post)
        upsert_diary_for_rating(
            user_id=current_user.id,
            title=movie,
            poster=poster_or_placeholder(poster_url),
            imdb_id=imdb_id,
            rating=rating,
            review=content,
        )
        db.session.commit()
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
        watched_date = datetime.strptime(request.form['watched_date'], '%Y-%m-%d').date()

        entry = DiaryEntry(
            title=title,
            rating=rating,
            review=review,
            watched_date=watched_date,
            poster_url=poster_url,
            user_id=current_user.id,
            imdb_id=imdb_id,
            is_rewatch=is_rewatch
        )
        db.session.add(entry)
        db.session.commit()
        flash("Diary entry added!", "success")
        return redirect(url_for('diary'))

    return render_view('add_diary', today=date.today().isoformat())


@app.route('/like/<int:post_id>', methods=['POST'])
def like_post(post_id):
    post = Post.query.get_or_404(post_id)
    post.likes += 1
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
    tab = request.args.get('tab', 'reviews')
    posts = Post.query.filter_by(user_id=user.id).order_by(Post.created_at.desc()).all()
    favorites = FavoriteFilm.query.filter_by(user_id=user.id).order_by(FavoriteFilm.position).all()
    diary_entries = DiaryEntry.query.filter_by(user_id=user.id).order_by(DiaryEntry.watched_date.desc()).limit(24).all()
    watched_films = WatchedFilm.query.filter_by(user_id=user.id).order_by(WatchedFilm.watched_at.desc()).limit(48).all()
    liked_films = FilmLike.query.filter_by(user_id=user.id).order_by(FilmLike.created_at.desc()).limit(48).all()
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
    return redirect(url_for('user_profile', username=username))


@app.route('/unfollow/<username>', methods=['POST'])
@login_required
def unfollow_user(username):
    target_user = User.query.filter_by(username=username).first_or_404()
    if current_user != target_user and current_user.is_following(target_user):
        current_user.unfollow(target_user)
        db.session.commit()
        flash(f"You unfollowed {username}.", "info")
    return redirect(url_for('user_profile', username=username))


@app.route('/edit/<int:post_id>', methods=['GET', 'POST'])
@login_required
def edit_post(post_id):
    post = Post.query.get_or_404(post_id)
    if post.user_id != current_user.id:
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('home'))

    if request.method == 'POST':
        post.movie = request.form['movie'].strip()
        post.content = request.form['content'].strip()
        post.rating = parse_rating(request.form.get('rating'), post.rating)
        post.poster_url = request.form.get('poster_url', '').strip()
        post.imdb_id = request.form.get('imdb_id', '').strip() or post.imdb_id
        db.session.commit()
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

    Comment.query.filter_by(post_id=post.id).delete()
    db.session.delete(post)
    db.session.commit()
    flash('Review deleted successfully.', 'success')
    return redirect(url_for('home'))


@app.route('/dashboard')
@login_required
def dashboard():
    posts = Post.query.filter_by(user_id=current_user.id).order_by(Post.created_at.desc()).all()
    return render_view('dashboard', user=current_user, posts=posts)


@app.route('/diary')
@login_required
def diary():
    entries = DiaryEntry.query.filter_by(user_id=current_user.id).order_by(DiaryEntry.watched_date.desc()).all()
    return render_view('diary', entries=entries, diary_months=group_diary_by_month(entries))


@app.route('/delete_diary/<int:entry_id>', methods=['POST'])
@login_required
def delete_diary(entry_id):
    entry = DiaryEntry.query.get_or_404(entry_id)
    if entry.user_id != current_user.id:
        flash("Not authorized to delete this entry.", "danger")
        return redirect(url_for('diary'))

    db.session.delete(entry)
    db.session.commit()
    flash("Diary entry deleted.", "info")
    return redirect(url_for('diary'))


@app.route('/edit_diary/<int:entry_id>', methods=['GET', 'POST'])
@login_required
def edit_diary(entry_id):
    entry = DiaryEntry.query.get_or_404(entry_id)

    if entry.user_id != current_user.id:
        flash("Not authorized to edit this entry.", "danger")
        return redirect(url_for('diary'))

    if request.method == 'POST':
        entry.title = request.form['title'].strip()
        entry.poster_url = request.form['poster_url'].strip()
        entry.rating = parse_rating(request.form.get('rating'), entry.rating)
        entry.review = request.form['review'].strip()
        entry.watched_date = datetime.strptime(request.form['watched_date'], '%Y-%m-%d').date()
        entry.is_rewatch = bool(request.form.get('is_rewatch'))
        entry.imdb_id = request.form.get('imdb_id', '').strip() or entry.imdb_id
        db.session.commit()
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

    movie = Watchlist(title=title, poster_url=poster_url, user_id=current_user.id)
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

        post = Post(
            movie=title,
            rating=rating,
            content=content,
            poster_url=poster_url,
            imdb_id=imdb_id,
            user_id=current_user.id
        )
        db.session.add(post)
        upsert_diary_for_rating(
            user_id=current_user.id,
            title=title,
            poster=poster_url,
            imdb_id=imdb_id,
            rating=rating,
            review=content,
        )
        db.session.commit()
        flash("Review added to your diary!", "success")
        if imdb_id:
            return redirect(url_for('film_page', imdb_id=imdb_id))
        return redirect(url_for('dashboard'))

    return render_view('add_review', movie=movie_data)


@app.cli.command("init-db")
@with_appcontext
def init_db():
    ensure_schema()
    click.echo("Database initialized successfully!")


with app.app_context():
    ensure_schema()


if __name__ == '__main__':
    app.run(debug=True)
