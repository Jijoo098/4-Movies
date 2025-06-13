from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import requests
import click
from flask.cli import with_appcontext
from werkzeug.utils import secure_filename
import os
from flask_login import LoginManager, login_user, logout_user, login_required, current_user, UserMixin
from datetime import datetime

# ------------------ Configuration ------------------ #
OMDB_API_KEY = 'eca1d09a'

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

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

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
    password = db.Column(db.String(150), nullable=False)
    profile_pic = db.Column(db.String(120), default='default.jpg')
    bio = db.Column(db.Text, default='')

    posts = db.relationship('Post', backref='author', lazy=True)
    comments = db.relationship('Comment', backref='author', lazy=True)
    watchlist = db.relationship('Watchlist', backref='user', lazy=True)
    lists = db.relationship('MovieList', backref='user', lazy=True)


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
    poster_url = db.Column(db.String(300))  # Optional, for movie poster
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)


class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    movie = db.Column(db.String(100), nullable=False)
    content = db.Column(db.Text, nullable=False)
    rating = db.Column(db.Integer, nullable=False)
    poster_url = db.Column(db.String(255))
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

# ------------------ Routes ------------------ #
@app.route("/")
def home():
    page = request.args.get('page', 1, type=int)
    posts = Post.query.order_by(Post.created_at.desc()).paginate(page=page, per_page=6)
    return render_template("home.html", posts=posts)

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username'].strip()
        email = request.form['email'].strip()
        password = request.form['password'].strip()

        if not username or not email or not password:
            flash("All fields are required.", "danger")
            return redirect(url_for('signup'))

        if User.query.filter_by(email=email).first():
            flash('Email already exists.', 'danger')
            return redirect(url_for('signup'))

        hashed_pw = generate_password_hash(password)
        user = User(username=username, email=email, password=hashed_pw)
        db.session.add(user)
        db.session.commit()
        flash('Signup successful! Please log in.', 'success')
        return redirect(url_for('login'))

    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email'].strip()
        password = request.form['password'].strip()
        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password, password):
            login_user(user)
            flash('Login successful!', 'success')
            return redirect(url_for('home'))

        flash('Invalid credentials.', 'danger')
        return redirect(url_for('login'))

    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Logged out.', 'info')
    return redirect(url_for('home'))

@app.route('/add', methods=['GET', 'POST'])
@login_required
def add_post():
    if request.method == 'POST':
        movie = request.form['movie'].strip()
        content = request.form['content'].strip()
        rating = float(request.form['rating'])
        poster_url = request.form.get('poster_url', '').strip()

        if not movie or not content or not rating:
            flash('All fields are required.', 'danger')
            return redirect(url_for('add_post'))

        post = Post(movie=movie, content=content, rating=rating, poster_url=poster_url, user_id=current_user.id)
        db.session.add(post)
        db.session.commit()
        flash('Review added!', 'success')
        return redirect(url_for('home'))

    return render_template('add_post.html')

@app.route('/add_diary', methods=['GET', 'POST'])
@login_required
def add_diary():
    if request.method == 'POST':
        title = request.form['title'].strip()
        rating = int(request.form['rating'])
        review = request.form['review'].strip()
        poster_url = request.form['poster_url']

        watched_date = datetime.strptime(request.form['watched_date'], '%Y-%m-%d').date()

        entry = DiaryEntry(
            title=title,
            rating=rating,
            review=review,
            watched_date=watched_date,
            poster_url=poster_url,
            user_id=current_user.id
        )
        db.session.add(entry)
        db.session.commit()
        flash("Diary entry added!", "success")
        return redirect(url_for('diary'))

    return render_template('add_diary.html')


@app.route('/like/<int:post_id>', methods=['POST'])
def like_post(post_id):
    post = Post.query.get_or_404(post_id)
    post.likes += 1
    db.session.commit()
    return redirect(url_for('home'))

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
    url = f"http://www.omdbapi.com/?s={query}&apikey=eca1d09a"
    response = requests.get(url)
    data = response.json()

    movies = []
    if data.get("Response") == "True":
        for item in data.get("Search", []):
            imdb_id = item["imdbID"]
            details_url = f"http://www.omdbapi.com/?i={imdb_id}&apikey=eca1d09a"
            details_resp = requests.get(details_url)
            movie_details = details_resp.json()
            movies.append(movie_details)

    return render_template("search_results.html", movies=movies, query=query)


@app.route('/profile/<username>')
def user_profile(username):
    user = User.query.filter_by(username=username).first_or_404()
    posts = Post.query.filter_by(user_id=user.id).order_by(Post.created_at.desc()).all()
    is_following = current_user.is_authenticated and current_user.is_following(user) and current_user != user
    return render_template('profile.html', user=user, posts=posts, is_following=is_following)

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
        post.rating = float(request.form['rating'])
        post.poster_url = request.form.get('poster_url', '').strip()
        db.session.commit()
        flash('Review updated.', 'success')
        return redirect(url_for('user_profile', username=current_user.username))

    return render_template('edit_post.html', post=post)

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
    return render_template('dashboard.html', user=current_user, posts=posts)

@app.route('/diary')
@login_required
def diary():
    entries = DiaryEntry.query.filter_by(user_id=current_user.id).order_by(DiaryEntry.watched_date.desc()).all()
    return render_template('diary.html', entries=entries)

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
        entry.rating = int(request.form['rating'])
        entry.review = request.form['review'].strip()
        entry.watched_date = datetime.strptime(request.form['watched_date'], '%Y-%m-%d').date()

        db.session.commit()
        flash("Diary entry updated!", "success")
        return redirect(url_for('diary'))

    return render_template('edit_diary.html', entry=entry)

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        new_email = request.form['email'].strip()
        current_password = request.form['current_password']
        new_password = request.form['new_password']

        # Change email
        if new_email and new_email != current_user.email:
            if User.query.filter_by(email=new_email).first():
                flash("Email already taken.", "danger")
            else:
                current_user.email = new_email
                flash("Email updated.", "success")

        # Change password
        if current_password and new_password:
            if check_password_hash(current_user.password, current_password):
                current_user.password = generate_password_hash(new_password)
                flash("Password changed successfully!", "success")
            else:
                flash("Current password is incorrect.", "danger")

        db.session.commit()
        return redirect(url_for('settings'))

    return render_template('settings.html')


@app.route('/edit_profile', methods=['GET', 'POST'])
@login_required
def edit_profile():
    if request.method == 'POST':
        username = request.form['username']
        bio = request.form['bio']
        file = request.files['profile_pic']

        current_user.username = username
        current_user.bio = bio

        if file and file.filename != '':
            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            current_user.profile_pic = filename

        db.session.commit()
        flash("Profile updated successfully!", "success")
        return redirect(url_for('dashboard'))

    return render_template('edit_profile.html')


@app.route('/feed')
@login_required
def followers_feed():
    followed_users = current_user.followed.all()
    followed_ids = [user.id for user in followed_users]
    posts = Post.query.filter(Post.user_id.in_(followed_ids)).order_by(Post.created_at.desc()).all()
    return render_template('feed.html', posts=posts)

@app.route('/lists')
@login_required
def lists():
    user_lists = MovieList.query.filter_by(user_id=current_user.id).all()
    return render_template('lists.html', lists=user_lists)

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

    return render_template('create_list.html')


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

    return render_template('edit_list.html', movie_list=movie_list)

@app.route('/lists/<int:list_id>/delete', methods=['POST'])
@login_required
def delete_list(list_id):
    movie_list = MovieList.query.get_or_404(list_id)

    if movie_list.user_id != current_user.id:
        flash("You are not allowed to delete this list.", "danger")
        return redirect(url_for('lists'))

    # Also delete the movies inside this list
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

    return render_template('view_list.html', movie_list=movie_list)

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


# 🔥 New: Popular Movies Route
@app.route('/popular')
def popular():
    from sqlalchemy import func

    # Query top reviewed movies with avg rating and count
    popular_data = db.session.query(
        Post.movie,
        func.count(Post.id).label('review_count'),
        func.avg(Post.rating).label('avg_rating'),
        func.max(Post.poster_url).label('poster_url')
    ).group_by(Post.movie).order_by(func.count(Post.id).desc()).limit(10).all()

    # Format results
    popular_movies = []
    for movie, review_count, avg_rating, poster_url in popular_data:
        popular_movies.append({
            'movie': movie,
            'review_count': review_count,
            'avg_rating': round(avg_rating, 1),
            'poster_url': poster_url
        })

    return render_template('popular.html', movies=popular_movies)

@app.route('/add_to_watchlist', methods=['POST'])
@login_required
def add_to_watchlist():
    title = request.form['title']
    poster_url = request.form['poster_url']

    # Check if it's already added
    existing = Watchlist.query.filter_by(user_id=current_user.id, title=title).first()
    if existing:
        flash("Movie already in your watchlist.", "info")
        return redirect(url_for('watchlist'))

    # Save new movie
    movie = Watchlist(title=title, poster_url=poster_url, user_id=current_user.id)
    db.session.add(movie)
    db.session.commit()
    flash("Movie added to your watchlist!", "success")
    return redirect(url_for('watchlist'))


@app.route('/watchlist')
@login_required
def watchlist():
    movies = Watchlist.query.filter_by(user_id=current_user.id).all()
    return render_template('watchlist.html', movies=movies)

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
    if not query:
        return jsonify([])

    response = requests.get("https://www.omdbapi.com/", params={
        'apikey': OMDB_API_KEY,
        's': query
    })

    data = response.json()
    results = []

    if data.get('Response') == 'True':
        for item in data.get('Search', []):
            results.append({
                'Title': item.get('Title'),
                'Year': item.get('Year'),
                'Poster': item.get('Poster') if item.get('Poster') != 'N/A' else 'https://via.placeholder.com/40x60?text=No+Image'
            })

    return jsonify(results)


@app.cli.command("init-db")
@app.route('/add_review/<movie_title>', methods=['GET', 'POST'])
@login_required
def add_review(movie_title):
    omdb_url = f"http://www.omdbapi.com/?apikey={OMDB_API_KEY}&t={movie_title}"
    response = requests.get(omdb_url)
    movie_data = response.json()

    if movie_data.get("Response") != "True":
        flash("Movie not found.", "danger")
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        rating = request.form['rating']
        content = request.form['content']
        poster_url = movie_data.get('Poster', '')

        post = Post(
            movie=movie_title,
            rating=rating,
            content=content,
            poster_url=poster_url,
            user_id=current_user.id
        )
        db.session.add(post)
        db.session.commit()
        flash("Review added!", "success")
        return redirect(url_for('dashboard'))

    return render_template('add_review.html', movie=movie_data)

@with_appcontext
def init_db():
    db.create_all()
    click.echo("✅ Database initialized successfully!")

if __name__ == '__main__':
    app.run(debug=True)
