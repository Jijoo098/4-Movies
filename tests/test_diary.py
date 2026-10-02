"""Diary sync tests. Run from the project folder:  python -m unittest tests.test_diary -v

Uses a throw-away SQLite file and a stubbed TMDB lookup, so it needs no network and never
touches instance/filmboxed.db.
"""
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_TMP, 'test.db').replace('\\', '/')
os.environ['TMDB_API_KEY'] = 'test-key'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as film_app  # noqa: E402
from app import (  # noqa: E402
    Comment, DiaryEntry, Post, Recommendation, User, WatchedFilm, db,
    backfill_diary_from_posts, run_in_transaction, save_film_log,
)
from sqlalchemy.exc import IntegrityError  # noqa: E402

AJAX = {'X-Requested-With': 'fetch'}


from flask.testing import FlaskClient  # noqa: E402


class IsolatedClient(FlaskClient):
    """The tests keep an app context open for DB asserts; Flask would then share flask.g (and Flask-Login's
    cached user) between requests. Reset it so every request authenticates from its own cookie."""

    def open(self, *args, **kwargs):
        from flask import g
        g.pop('_login_user', None)
        return super().open(*args, **kwargs)


film_app.app.test_client_class = IsolatedClient


def fake_movie(tmdb_id):
    return {'Title': f'Film {tmdb_id}', 'Poster': 'https://img/p.jpg', 'Year': '2020', 'imdbID': str(tmdb_id)}


class DiaryTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        film_app.app.config['TESTING'] = True
        cls.patch = mock.patch.object(film_app, 'fetch_movie_by_id', side_effect=fake_movie)
        cls.patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()

    def setUp(self):
        self.ctx = film_app.app.app_context()
        self.ctx.push()
        db.drop_all()
        film_app.ensure_schema()
        self.a = User(username='alice', email='a@x.io', password='x')
        self.b = User(username='bob', email='b@x.io', password='x')
        db.session.add_all([self.a, self.b])
        db.session.commit()
        self.client_a = self.login(self.a)
        self.client_b = self.login(self.b)

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def login(self, user):
        client = film_app.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(user.id)
        return client

    def entries(self, user=None):
        query = DiaryEntry.query
        if user:
            query = query.filter_by(user_id=user.id)
        return query.all()

    # ---- sync ----
    def test_rating_creates_entry_and_marks_watched(self):
        self.client_a.post('/rate/100', data={'rating': '8'})
        entry = self.entries(self.a)
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry[0].rating, 8)
        self.assertIsNone(entry[0].review)
        self.assertEqual(entry[0].watched_date, date.today())
        self.assertEqual(WatchedFilm.query.filter_by(user_id=self.a.id, tmdb_id='100').count(), 1)

    def test_review_updates_same_entry_without_duplicates(self):
        self.client_a.post('/rate/100', data={'rating': '6'})
        self.client_a.post('/rate/100', data={'rating': '9', 'content': 'Great', 'review_form': '1',
                                              'watched_date': date.today().isoformat()})
        self.assertEqual(len(self.entries(self.a)), 1)
        self.assertEqual(Post.query.filter_by(user_id=self.a.id).count(), 1)
        entry = self.entries(self.a)[0]
        self.assertEqual((entry.rating, entry.review), (9, 'Great'))

    def test_editing_review_keeps_watched_date_unless_changed(self):
        past = (date.today() - timedelta(days=40)).isoformat()
        self.client_a.post('/rate/100', data={'rating': '8', 'content': 'v1', 'review_form': '1',
                                              'watched_date': past})
        self.assertEqual(self.entries(self.a)[0].watched_date.isoformat(), past)
        self.client_a.post('/rate/100', data={'rating': '8'})  # quick star click, no date
        self.client_a.post('/rate/100', data={'rating': '8', 'content': 'v2', 'review_form': '1',
                                              'watched_date': past})
        entry = self.entries(self.a)[0]
        self.assertEqual((entry.review, entry.watched_date.isoformat()), ('v2', past))
        self.client_a.post('/rate/100', data={'rating': '8'})
        self.assertEqual(self.entries(self.a)[0].watched_date.isoformat(), past)

    def test_future_watched_date_rejected(self):
        future = (date.today() + timedelta(days=2)).isoformat()
        self.client_a.post('/rate/100', data={'rating': '8', 'review_form': '1', 'watched_date': future})
        self.assertEqual(self.entries(), [])
        self.assertEqual(Post.query.count(), 0)

    def test_unique_constraint_blocks_duplicates(self):
        db.session.add(DiaryEntry(user_id=self.a.id, title='X', imdb_id='5', watched_date=date.today()))
        db.session.commit()
        db.session.add(DiaryEntry(user_id=self.a.id, title='X', imdb_id='5', watched_date=date.today()))
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_double_submit_creates_one_entry(self):
        for _ in range(3):
            run_in_transaction(lambda: save_film_log(self.a.id, imdb_id='7', title='Film 7', rating=7))
        self.assertEqual(len(self.entries(self.a)), 1)
        self.assertEqual(Post.query.filter_by(user_id=self.a.id).count(), 1)

    def test_unique_race_is_retried(self):
        """A concurrent request inserting the same film first makes our insert fail once; we retry as an update."""
        calls = {'n': 0}
        real = film_app.save_film_log

        def racing(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                db.session.add(DiaryEntry(user_id=self.a.id, title='Film 9', imdb_id='9', watched_date=date.today()))
                db.session.flush()
                # simulate another transaction committing the same row first
                db.session.add(DiaryEntry(user_id=self.a.id, title='Film 9', imdb_id='9', watched_date=date.today()))
                db.session.flush()
            return real(*args, **kwargs)

        run_in_transaction(lambda: racing(self.a.id, imdb_id='9', title='Film 9', rating=5))
        self.assertEqual(len(self.entries(self.a)), 1)

    # ---- delete / remove ----
    def test_delete_review_keeps_rating_entry(self):
        self.client_a.post('/rate/100', data={'rating': '8', 'content': 'Nice', 'review_form': '1'})
        post = Post.query.filter_by(user_id=self.a.id).one()
        self.client_a.get(f'/delete/{post.id}')
        entry = self.entries(self.a)[0]
        self.assertEqual((entry.rating, entry.review), (8, None))
        self.assertEqual(Post.query.filter_by(user_id=self.a.id).one().rating, 8)

    def test_delete_rating_only_post_removes_entry(self):
        self.client_a.post('/rate/100', data={'rating': '8'})
        post = Post.query.filter_by(user_id=self.a.id).one()
        self.client_a.get(f'/delete/{post.id}')
        self.assertEqual(self.entries(self.a), [])

    def test_remove_from_diary_deletes_rating_review_and_updates_stats(self):
        self.client_a.post('/rate/100', data={'rating': '8', 'content': 'Nice', 'review_form': '1'})
        self.client_b.post('/rate/100', data={'rating': '4'})
        post = Post.query.filter_by(user_id=self.a.id).one()
        db.session.add(Comment(text='hi', user_id=self.b.id, post_id=post.id))
        db.session.commit()
        entry_id = self.entries(self.a)[0].id
        resp = self.client_a.delete(f'/api/diary/{entry_id}', headers=AJAX)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.entries(self.a), [])
        self.assertEqual(Post.query.filter_by(user_id=self.a.id).count(), 0)
        ratings = [p.rating for p in film_app.film_reviews_for_title('Film 100', '100')]
        self.assertEqual(ratings, [4])  # film stats now only count bob

    # ---- ownership / auth ----
    def test_other_users_entries_are_untouchable(self):
        self.client_a.post('/rate/100', data={'rating': '8', 'content': 'mine', 'review_form': '1'})
        entry_id = self.entries(self.a)[0].id
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        patch = self.client_b.patch(f'/api/diary/{entry_id}', json={'watched_date': yesterday}, headers=AJAX)
        delete = self.client_b.delete(f'/api/diary/{entry_id}', headers=AJAX)
        self.assertEqual((patch.status_code, delete.status_code), (404, 404))
        self.client_b.post(f'/delete_diary/{entry_id}')
        entry = DiaryEntry.query.get(entry_id)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.watched_date, date.today())

    def test_bobs_actions_do_not_touch_alices_diary(self):
        self.client_a.post('/rate/100', data={'rating': '8'})
        self.client_b.post('/rate/100', data={'rating': '2', 'content': 'bad', 'review_form': '1'})
        self.assertEqual(self.entries(self.a)[0].rating, 8)
        self.assertEqual(len(self.entries(self.b)), 1)

    def test_logged_out_api_is_401_and_leaks_nothing(self):
        self.client_a.post('/rate/100', data={'rating': '8'})
        entry_id = self.entries(self.a)[0].id
        anon = film_app.app.test_client()
        for resp in (
            anon.delete(f'/api/diary/{entry_id}', headers=AJAX),
            anon.patch(f'/api/diary/{entry_id}', json={'watched_date': '2020-01-01'}, headers=AJAX),
        ):
            self.assertEqual(resp.status_code, 401)
            self.assertNotIn(b'Film 100', resp.data)
        page = anon.get('/diary')
        self.assertEqual(page.status_code, 302)
        self.assertIn('/login', page.headers['Location'])
        self.assertIn('next=', page.headers['Location'])
        self.assertIsNotNone(DiaryEntry.query.get(entry_id))

    def test_patch_validates_dates_and_header(self):
        self.client_a.post('/rate/100', data={'rating': '8'})
        entry_id = self.entries(self.a)[0].id
        future = (date.today() + timedelta(days=1)).isoformat()
        self.assertEqual(self.client_a.patch(f'/api/diary/{entry_id}', json={'watched_date': future},
                                             headers=AJAX).status_code, 400)
        self.assertEqual(self.client_a.patch(f'/api/diary/{entry_id}', json={'watched_date': 'nope'},
                                             headers=AJAX).status_code, 400)
        self.assertEqual(self.client_a.patch(f'/api/diary/{entry_id}',
                                             json={'watched_date': '2024-02-03'}).status_code, 400)  # no header
        ok = self.client_a.patch(f'/api/diary/{entry_id}', json={'watched_date': '2024-02-03'}, headers=AJAX)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.entries(self.a)[0].watched_date, date(2024, 2, 3))

    # ---- recommend ----
    def test_recommend_toggle_mirrors_on_entry(self):
        self.client_a.post('/rate/100', data={'rating': '8'})
        self.client_a.post('/film/100/recommend', headers=AJAX)
        self.assertTrue(self.entries(self.a)[0].recommend)
        self.client_a.post('/film/100/recommend', headers=AJAX)
        self.assertIsNone(self.entries(self.a)[0].recommend)

    # ---- backfill ----
    def test_backfill_is_idempotent(self):
        old = datetime(2023, 5, 17, 12, 0)
        db.session.add_all([
            Post(movie='Old', content='Loved it', rating=9, imdb_id='11', user_id=self.a.id, created_at=old),
            Post(movie='Rated only', content='Rated Rated only.', rating=5, imdb_id='12', user_id=self.a.id,
                 created_at=old),
            Post(movie='No id', content='Hm', rating=4, user_id=self.b.id, created_at=old),
        ])
        db.session.commit()
        self.assertEqual(backfill_diary_from_posts(), 3)
        self.assertEqual(backfill_diary_from_posts(), 0)
        self.assertEqual(len(self.entries()), 3)
        old_entry = DiaryEntry.query.filter_by(imdb_id='11').one()
        self.assertEqual(old_entry.watched_date, date(2023, 5, 17))
        self.assertEqual(old_entry.review, 'Loved it')
        self.assertIsNone(DiaryEntry.query.filter_by(imdb_id='12').one().review)

    def test_script_text_is_stored_verbatim_for_escaping_at_render(self):
        payload = '<script>alert(1)</script>'
        self.client_a.post('/rate/100', data={'rating': '8', 'content': payload, 'review_form': '1'})
        self.assertEqual(self.entries(self.a)[0].review, payload)

    def test_review_length_limit(self):
        self.client_a.post('/rate/100', data={'rating': '8', 'content': 'x' * 1001, 'review_form': '1'})
        self.assertEqual(self.entries(), [])


if __name__ == '__main__':
    unittest.main()
