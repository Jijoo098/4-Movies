"""Populate community tables with realistic seed data (development only)."""

from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

from community.seed_data import (
    ATTACHMENTS,
    RECOMMENDED_DISCUSSIONS,
    SEED_POSTS,
    SEED_USERS,
    TRENDING_TOPIC_SEEDS,
)


def seed_community_if_needed(db, models):
    """Insert seed users/posts once. Safe to call on every startup."""
    User = models['User']
    CommunityPost = models['CommunityPost']
    CommunityComment = models['CommunityComment']
    CommunityPoll = models['CommunityPoll']
    CommunityPollOption = models['CommunityPollOption']
    CommunityNotification = models['CommunityNotification']

    if CommunityPost.query.filter_by(is_seed=True).count() >= len(SEED_POSTS):
        return False

    password_hash = generate_password_hash('filmboxed-dev')
    user_map = {}
    for spec in SEED_USERS:
        user = User.query.filter_by(username=spec['username']).first()
        if not user:
            user = User(
                username=spec['username'],
                email=spec['email'],
                password=password_hash,
                bio=spec['bio'],
                display_name=spec.get('display_name'),
                is_verified=spec.get('is_verified', False),
                is_seed_user=True,
            )
            db.session.add(user)
            db.session.flush()
        else:
            user.bio = spec['bio']
            user.display_name = spec.get('display_name', user.username)
            user.is_verified = spec.get('is_verified', False)
            user.is_seed_user = True
        user_map[spec['username']] = user

    # Clear old seed posts to avoid duplicates on re-seed during dev
    old_seed = CommunityPost.query.filter_by(is_seed=True).all()
    for post in old_seed:
        CommunityComment.query.filter_by(post_id=post.id).delete()
        poll = CommunityPoll.query.filter_by(post_id=post.id).first()
        if poll:
            CommunityPollOption.query.filter_by(poll_id=poll.id).delete()
            db.session.delete(poll)
        db.session.delete(post)
    db.session.flush()

    dune_post = None
    for spec in SEED_POSTS:
        author = user_map[spec['author']]
        created_at = datetime.utcnow() - timedelta(hours=spec['hours_ago'])
        attachment = ATTACHMENTS.get(spec.get('attachment')) if spec.get('attachment') else None

        post = CommunityPost(
            user_id=author.id,
            post_type=spec['post_type'],
            content=spec['content'],
            title=spec.get('title'),
            rating=spec.get('rating'),
            spoiler=spec.get('spoiler', False),
            hashtags=','.join(spec.get('hashtags', [])),
            link_url=spec.get('link_url'),
            link_title=spec.get('link_title'),
            is_seed=True,
            created_at=created_at,
        )
        if attachment:
            post.attachment_kind = attachment['kind']
            post.attachment_title = attachment['title']
            post.attachment_year = attachment['year']
            post.attachment_genre = attachment['genre']
            post.attachment_poster = attachment['poster']
            if attachment['kind'] == 'movie':
                post.movie_tmdb_id = attachment['tmdb_id']
            else:
                post.series_tmdb_id = attachment['tmdb_id']

        db.session.add(post)
        db.session.flush()

        if spec['post_type'] == 'poll' and spec.get('poll'):
            poll = CommunityPoll(post_id=post.id, question=spec['poll']['question'])
            db.session.add(poll)
            db.session.flush()
            for label, pct in spec['poll']['options']:
                votes = max(1, int(pct * 3))
                db.session.add(CommunityPollOption(
                    poll_id=poll.id,
                    label=label,
                    vote_count=votes,
                ))

        # Seed likes count via CommunityPostLike from other seed users (approximate)
        like_targets = list(user_map.values())[: min(spec.get('likes', 0) // 40 + 1, 4)]
        for liker in like_targets:
            if liker.id != author.id:
                models['CommunityPostLike'].query.filter_by(
                    user_id=liker.id, post_id=post.id
                ).delete()
                db.session.add(models['CommunityPostLike'](
                    user_id=liker.id,
                    post_id=post.id,
                ))

        if attachment and attachment.get('title') == 'Dune: Part Two':
            dune_post = post

        if spec.get('comments', 0) >= 2 and dune_post is None and 'Dune' in spec.get('content', ''):
            dune_post = post

    if dune_post:
        c1_author = user_map['Cinephile_11']
        c2_author = user_map['MovieBuff']
        c1 = CommunityComment(
            post_id=dune_post.id,
            user_id=c1_author.id,
            content='The sound design in that final sequence was insane.',
            created_at=datetime.utcnow() - timedelta(hours=1, minutes=20),
        )
        db.session.add(c1)
        db.session.flush()
        db.session.add(CommunityComment(
            post_id=dune_post.id,
            user_id=c2_author.id,
            parent_id=c1.id,
            content='Exactly! Hans Zimmer absolutely nailed it.',
            created_at=datetime.utcnow() - timedelta(hours=1),
        ))

    moviebuff = user_map.get('MovieBuff')
    cinetalks = user_map.get('CineTalks')
    if moviebuff and cinetalks:
        db.session.add(CommunityNotification(
            user_id=moviebuff.id,
            actor_id=cinetalks.id,
            kind='like',
            message='CineTalks liked your post about Dune: Part Two',
            is_read=False,
            created_at=datetime.utcnow() - timedelta(minutes=45),
        ))

    db.session.commit()
    return True


def trending_topics_from_posts(posts):
    """Derive hashtag counts from post data."""
    counts = {}
    for tag, fallback in TRENDING_TOPIC_SEEDS:
        counts[tag] = fallback
    for post in posts:
        if not post.get('hashtags'):
            continue
        for tag in post['hashtags']:
            counts[tag] = counts.get(tag, 0) + max(post.get('likes', 0), 1)
    ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    return [{'tag': tag, 'count': count} for tag, count in ranked[:6]]


def recommended_discussions():
    return RECOMMENDED_DISCUSSIONS
