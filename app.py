from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timedelta
import requests
import click
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

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///filmboxed.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = 'your_secret_key'
app.config['UPLOAD_FOLDER'] = 'static/profile_pics'
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024  # 2MB max
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# ------------------ Helpers ------------------ #
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def tmdb_get(path, **params):
    """GET a TMDB v3 endpoint. Returns JSON dict or None."""
    if not TMDB_API_KEY:
        return None
    params = dict(params)
    params['api_key'] = TMDB_API_KEY
    try:
        response = requests.get(f"{TMDB_BASE}{path}", params=params, timeout=12)
        if response.status_code != 200:
            return None
        return response.json()
    except requests.RequestException:
        return None


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
        cast.append({
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
        job = person.get('job') or ''
        if job not in job_rank:
            continue
        key = (person.get('id') or person.get('name'), job)
        if key in seen:
            continue
        seen.add(key)
        crew_candidates.append({
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
            job = person.get('job') or 'Crew'
            key = (person.get('id') or person.get('name'), job)
            if key in seen:
                continue
            seen.add(key)
            crew.append({
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
        'Backdrop': tmdb_poster_url(data.get('backdrop_path'), size='w780') if data.get('backdrop_path') else None,
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


def fetch_movie_by_id(tmdb_id):
    if not tmdb_id:
        return None
    data = tmdb_get(
        f'/movie/{tmdb_id}',
        append_to_response='credits,videos,recommendations'
    )
    if not data:
        return None
    return normalize_tmdb_movie(
        data,
        credits=data.get('credits'),
        videos=data.get('videos'),
        recommendations=data.get('recommendations'),
    )


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


def _schedule_card(item, release_date=None, regions=None, kind='theatrical'):
    release = release_date or _parse_release_date(item.get('release_date'))
    return {
        'id': str(item.get('id')),
        'title': item.get('title') or item.get('original_title') or 'Untitled',
        'poster': tmdb_poster_url(item.get('poster_path')),
        'overview': (item.get('overview') or '')[:220],
        'release_date': release,
        'release_label': _format_schedule_date(release),
        'year': release.year if release else None,
        'regions': sorted(regions) if regions else [],
        'kind': kind,
        'popularity': item.get('popularity') or 0,
    }


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


def fetch_discover_releases(release_types, date_gte, date_lte, sort_by='popularity.desc', pages=2, kind='digital'):
    cards = []
    seen = set()
    for page in range(1, pages + 1):
        data = tmdb_get(
            '/discover/movie',
            with_release_type=release_types,
            **{
                'release_date.gte': date_gte.isoformat(),
                'release_date.lte': date_lte.isoformat(),
            },
            sort_by=sort_by,
            include_adult='false',
            page=page,
            language='en-US',
        )
        if not data:
            break
        for item in data.get('results') or []:
            mid = str(item.get('id'))
            if not mid or mid in seen:
                continue
            seen.add(mid)
            cards.append(_schedule_card(item, kind=kind))
    cards.sort(key=lambda c: (
        c['release_date'] or date.max,
        -c['popularity'],
    ))
    return cards


def fetch_release_schedule():
    """Worldwide-ish theatrical + digital/OTT release calendar from TMDB."""
    cached = getattr(fetch_release_schedule, '_cache', None)
    now = datetime.utcnow()
    if cached and (now - cached['at']).total_seconds() < 3600:
        return cached['data']

    today = date.today()
    upcoming_until = today + timedelta(days=120)
    digital_until = today + timedelta(days=90)
    recent_from = today - timedelta(days=45)
    regions = ('US', 'IN', 'GB', 'JP', 'FR', 'DE', 'BR', 'KR', 'AU', 'CA')

    upcoming_payloads = [
        (region, tmdb_get('/movie/upcoming', region=region, page=1))
        for region in regions
    ]
    now_payloads = [
        (region, tmdb_get('/movie/now_playing', region=region, page=1))
        for region in regions[:6]
    ]

    upcoming = _merge_region_lists(
        upcoming_payloads,
        kind='theatrical',
        only_from=today,
        only_until=upcoming_until,
        limit=40,
    )
    now_playing = _merge_region_lists(
        now_payloads,
        kind='theatrical',
        only_until=today,
        limit=24,
    )
    now_playing.sort(key=lambda c: -c['popularity'])

    ott_upcoming = fetch_discover_releases(
        '4',
        today,
        digital_until,
        sort_by='popularity.desc',
        pages=2,
        kind='ott',
    )[:28]

    ott_recent = fetch_discover_releases(
        '4',
        recent_from,
        today,
        sort_by='popularity.desc',
        pages=2,
        kind='ott',
    )[:28]

    data = {
        'upcoming': upcoming,
        'now_playing': now_playing,
        'ott_upcoming': ott_upcoming,
        'ott_recent': ott_recent,
        'as_of': today,
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
    alterations = [
        ("diary_entry", "imdb_id", "VARCHAR(20)"),
        ("diary_entry", "is_rewatch", "BOOLEAN DEFAULT 0"),
        ("post", "imdb_id", "VARCHAR(20)"),
    ]
    with db.engine.connect() as conn:
        for table, column, coltype in alterations:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))
                conn.commit()
            except Exception:
                conn.rollback()


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
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    movies = db.relationship('ListMovie', backref='list', lazy=True)


class ListMovie(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    poster_url = db.Column(db.String(300))
    list_id = db.Column(db.Integer, db.ForeignKey('movie_list.id'), nullable=False)


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
    page = request.args.get('page', 1, type=int)
    posts = Post.query.order_by(Post.created_at.desc()).paginate(page=page, per_page=8)
    popular_movies = get_popular_movies(limit=10)
    featured_lists = MovieList.query.order_by(MovieList.id.desc()).limit(6).all()
    return render_view(
        'home',
        posts=posts,
        popular_movies=popular_movies,
        featured_lists=featured_lists
    )


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
    exists = ListMovie.query.filter_by(list_id=movie_list.id, title=title).first()
    if exists:
        flash('Already on that list.', 'info')
    else:
        db.session.add(ListMovie(
            title=title,
            poster_url=movie.get('Poster'),
            list_id=movie_list.id,
        ))
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
        # Enrich top results with full details (director, runtime, genres)
        for item in search_tmdb_movies(query, limit=8):
            details = fetch_movie_by_id(item['tmdb_id'])
            movies.append(details or item)
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
    data = fetch_release_schedule()
    return render_view(
        'schedule',
        upcoming=data['upcoming'],
        now_playing=data['now_playing'],
        ott_upcoming=data['ott_upcoming'],
        ott_recent=data['ott_recent'],
        as_of=data['as_of'],
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
    return render_view('diary', entries=entries)


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
@login_required
def lists():
    user_lists = MovieList.query.filter_by(user_id=current_user.id).all()
    return render_view('lists', lists=user_lists)


@app.route('/lists/create', methods=['GET', 'POST'])
@login_required
def create_list():
    if request.method == 'POST':
        name = request.form['name']
        description = request.form['description']

        new_list = MovieList(name=name, description=description, user_id=current_user.id)
        db.session.add(new_list)
        db.session.commit()
        flash("List created successfully!", "success")
        return redirect(url_for('lists'))

    return render_view('create_list')


@app.route('/lists/<int:list_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_list(list_id):
    movie_list = MovieList.query.get_or_404(list_id)

    if movie_list.user_id != current_user.id:
        flash("You are not allowed to edit this list.", "danger")
        return redirect(url_for('lists'))

    if request.method == 'POST':
        movie_list.name = request.form['name']
        movie_list.description = request.form['description']
        db.session.commit()
        flash("List updated successfully!", "success")
        return redirect(url_for('lists'))

    return render_view('edit_list', movie_list=movie_list)


@app.route('/lists/<int:list_id>/delete', methods=['POST'])
@login_required
def delete_list(list_id):
    movie_list = MovieList.query.get_or_404(list_id)

    if movie_list.user_id != current_user.id:
        flash("You are not allowed to delete this list.", "danger")
        return redirect(url_for('lists'))

    ListMovie.query.filter_by(list_id=movie_list.id).delete()
    db.session.delete(movie_list)
    db.session.commit()
    flash("List deleted successfully!", "success")
    return redirect(url_for('lists'))


@app.route('/lists/<int:list_id>')
@login_required
def view_list(list_id):
    movie_list = MovieList.query.get_or_404(list_id)

    if movie_list.user_id != current_user.id:
        flash("Access denied.", "danger")
        return redirect(url_for('lists'))

    return render_view('view_list', movie_list=movie_list)


@app.route('/lists/<int:list_id>/add_movie', methods=['POST'])
@login_required
def add_movie_to_list(list_id):
    movie_list = MovieList.query.get_or_404(list_id)

    if movie_list.user_id != current_user.id:
        flash("You can’t add movies to someone else’s list.", "danger")
        return redirect(url_for('lists'))

    title = request.form['title']
    poster_url = request.form['poster_url']

    movie = ListMovie(title=title, poster_url=poster_url, list_id=movie_list.id)
    db.session.add(movie)
    db.session.commit()

    flash("Movie added!", "success")
    return redirect(url_for('view_list', list_id=list_id))


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
