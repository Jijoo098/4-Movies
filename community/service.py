"""Community feed, search, and interaction services."""

from datetime import datetime

from sqlalchemy import or_

from community.seed import recommended_discussions, trending_topics_from_posts


def _parse_hashtags(raw):
    if not raw:
        return []
    return [tag.strip() for tag in raw.split(',') if tag.strip()]


def _time_ago(value):
    if not value:
        return ''
    now = datetime.utcnow()
    seconds = max(0, int((now - value).total_seconds()))
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
    return value.strftime('%b %d, %Y')


def _viewer_sets(viewer_id, models):
    if not viewer_id:
        return set(), set(), set()
    liked = {
        row.post_id for row in models['CommunityPostLike'].query.filter_by(user_id=viewer_id).all()
    }
    saved = {
        row.post_id for row in models['CommunitySavedPost'].query.filter_by(user_id=viewer_id).all()
    }
    reposted = {
        row.post_id for row in models['CommunityRepost'].query.filter_by(user_id=viewer_id).all()
    }
    return liked, saved, reposted


def _counts_for_post(post_id, models):
    return {
        'likes': models['CommunityPostLike'].query.filter_by(post_id=post_id).count(),
        'comments': models['CommunityComment'].query.filter_by(post_id=post_id).count(),
        'reposts': models['CommunityRepost'].query.filter_by(post_id=post_id).count(),
    }


def _post_to_dict(post, models, *, liked_ids=None, saved_ids=None, reposted_ids=None):
    liked_ids = liked_ids or set()
    saved_ids = saved_ids or set()
    reposted_ids = reposted_ids or set()
    author = post.author
    counts = _counts_for_post(post.id, models)

    poll_data = None
    if post.poll:
        total = sum(opt.vote_count for opt in post.poll.options) or 1
        poll_data = {
            'id': post.poll.id,
            'question': post.poll.question,
            'options': [
                {
                    'id': opt.id,
                    'label': opt.label,
                    'votes': opt.vote_count,
                    'pct': round(opt.vote_count * 100 / total),
                }
                for opt in post.poll.options
            ],
            'total_votes': total,
        }

    attachment = None
    if post.attachment_title:
        attachment = {
            'kind': post.attachment_kind or 'movie',
            'tmdb_id': post.movie_tmdb_id or post.series_tmdb_id,
            'title': post.attachment_title,
            'year': post.attachment_year,
            'genre': post.attachment_genre,
            'poster': post.attachment_poster,
            'discussions': counts['likes'] + counts['comments'],
        }

    return {
        'id': post.id,
        'post_type': post.post_type or 'text',
        'user': author,
        'display_name': getattr(author, 'display_name', None) or author.username,
        'is_verified': getattr(author, 'is_verified', False),
        'content': post.content,
        'title': post.title,
        'rating': post.rating,
        'spoiler': post.spoiler,
        'media_url': post.media_url,
        'media_type': post.media_type,
        'link_url': post.link_url,
        'link_title': post.link_title,
        'attachment': attachment,
        'hashtags': _parse_hashtags(post.hashtags),
        'poll': poll_data,
        'created_at': post.created_at,
        'time_label': _time_ago(post.created_at),
        'likes': counts['likes'],
        'comments_count': counts['comments'],
        'reposts': counts['reposts'],
        'liked_by_me': post.id in liked_ids,
        'saved_by_me': post.id in saved_ids,
        'reposted_by_me': post.id in reposted_ids,
        'is_seed': post.is_seed,
    }


def get_community_feed(
    models,
    *,
    viewer_id=None,
    feed_filter='for-you',
    sort='latest',
    query=None,
    limit=50,
):
    CommunityPost = models['CommunityPost']
    User = models['User']

    q = CommunityPost.query

    if query:
        like = f'%{query.strip()}%'
        q = q.filter(or_(
            CommunityPost.content.ilike(like),
            CommunityPost.title.ilike(like),
            CommunityPost.hashtags.ilike(like),
            CommunityPost.attachment_title.ilike(like),
        ))

    if feed_filter == 'following':
        if not viewer_id:
            return []
        viewer = User.query.get(viewer_id)
        followed_ids = [u.id for u in viewer.followed.all()] if viewer else []
        if not followed_ids:
            return []
        q = q.filter(CommunityPost.user_id.in_(followed_ids))

    elif feed_filter == 'reviews':
        q = q.filter(CommunityPost.post_type == 'review')

    elif feed_filter == 'discussions':
        q = q.filter(CommunityPost.post_type.in_([
            'movie-discussion', 'series-discussion', 'text', 'link',
        ]))

    elif feed_filter == 'saved':
        if not viewer_id:
            return []
        saved_ids = [
            row.post_id for row in models['CommunitySavedPost'].query.filter_by(user_id=viewer_id).all()
        ]
        if not saved_ids:
            return []
        q = q.filter(CommunityPost.id.in_(saved_ids))

    elif feed_filter == 'my-posts':
        if not viewer_id:
            return []
        q = q.filter(CommunityPost.user_id == viewer_id)

    if feed_filter == 'trending':
        sort = 'trending'

    posts = q.all()
    liked_ids, saved_ids, reposted_ids = _viewer_sets(viewer_id, models)
    feed = [
        _post_to_dict(post, models, liked_ids=liked_ids, saved_ids=saved_ids, reposted_ids=reposted_ids)
        for post in posts
    ]

    if sort == 'liked':
        feed.sort(key=lambda p: p['likes'], reverse=True)
    elif sort == 'discussed':
        feed.sort(key=lambda p: p['comments_count'], reverse=True)
    elif sort == 'trending':
        feed.sort(
            key=lambda p: p['likes'] * 2 + p['comments_count'] * 3 + p['reposts'],
            reverse=True,
        )
    else:
        feed.sort(key=lambda p: p['created_at'] or datetime.min, reverse=True)

    return feed[:limit]


def get_post_comments(models, post_id):
    CommunityComment = models['CommunityComment']
    rows = CommunityComment.query.filter_by(
        post_id=post_id, parent_id=None
    ).order_by(CommunityComment.created_at.asc()).all()
    comments = []
    for row in rows:
        replies = CommunityComment.query.filter_by(
            parent_id=row.id
        ).order_by(CommunityComment.created_at.asc()).all()
        comments.append({
            'id': row.id,
            'user': row.author,
            'user_id': row.user_id,
            'display_name': getattr(row.author, 'display_name', None) or row.author.username,
            'content': row.content,
            'time_label': _time_ago(row.created_at),
            'likes': row.like_count or 0,
            'replies': [
                {
                    'id': rep.id,
                    'user': rep.author,
                    'user_id': rep.user_id,
                    'display_name': getattr(rep.author, 'display_name', None) or rep.author.username,
                    'content': rep.content,
                    'time_label': _time_ago(rep.created_at),
                    'likes': rep.like_count or 0,
                }
                for rep in replies
            ],
        })
    return comments


def get_people_to_follow(models, viewer_id=None, limit=5):
    User = models['User']
    q = User.query.filter_by(is_seed_user=True)
    people = q.all()
    if viewer_id:
        viewer = User.query.get(viewer_id)
        following_ids = {u.id for u in viewer.followed.all()} if viewer else set()
        people = [u for u in people if u.id != viewer_id and u.id not in following_ids]
    return people[:limit]


def get_notifications(models, user_id, limit=8):
    rows = models['CommunityNotification'].query.filter_by(user_id=user_id).order_by(
        models['CommunityNotification'].created_at.desc()
    ).limit(limit).all()
    return [
        {
            'id': row.id,
            'message': row.message,
            'kind': row.kind,
            'is_read': row.is_read,
            'time_label': _time_ago(row.created_at),
        }
        for row in rows
    ]


def search_community(models, query):
    query = (query or '').strip().lstrip('#')
    if not query:
        return {'users': [], 'posts': [], 'hashtags': []}

    User = models['User']
    CommunityPost = models['CommunityPost']
    like = f'%{query}%'

    users = User.query.filter(or_(
        User.username.ilike(like),
        User.display_name.ilike(like),
    )).limit(6).all()

    posts = CommunityPost.query.filter(or_(
        CommunityPost.content.ilike(like),
        CommunityPost.attachment_title.ilike(like),
        CommunityPost.hashtags.ilike(like),
    )).limit(8).all()

    tags = []
    for post in CommunityPost.query.filter(CommunityPost.hashtags.isnot(None)).all():
        for tag in _parse_hashtags(post.hashtags):
            if query.lower() in tag.lower() and tag not in tags:
                tags.append(tag)

    return {
        'users': users,
        'posts': [_post_to_dict(p, models) for p in posts],
        'hashtags': tags[:6],
    }


def resolve_viewer_id(actor, feed_filter):
    if not actor:
        return None
    if actor.is_authenticated:
        return actor.id
    if actor.is_dev_session and feed_filter in ('my-posts', 'saved'):
        return actor.id
    return None


def community_page_context(models, *, actor, feed_filter, sort, query, expanded_post_id=None):
    viewer_id = resolve_viewer_id(actor, feed_filter)
    if feed_filter in ('saved', 'my-posts', 'following') and not viewer_id:
        feed = []
    else:
        feed = get_community_feed(
            models,
            viewer_id=viewer_id if feed_filter in ('following', 'saved', 'my-posts') else (
                actor.id if actor and actor.can_interact else None
            ),
            feed_filter=feed_filter,
            sort=sort,
            query=query,
        )

    following_ids = set()
    if actor and actor.can_interact:
        user = models['User'].query.get(actor.id)
        following_ids = {u.id for u in user.followed.all()} if user else set()

    expanded_comments = get_post_comments(models, expanded_post_id) if expanded_post_id else []

    notifications = []
    if actor and actor.can_interact:
        notifications = get_notifications(models, actor.id)

    return {
        'feed': feed,
        'following_ids': following_ids,
        'people': get_people_to_follow(
            models,
            actor.id if actor and actor.can_interact else None,
        ),
        'trending_topics': trending_topics_from_posts(feed),
        'recommended': recommended_discussions(),
        'notifications': notifications,
        'expanded_post_id': expanded_post_id,
        'expanded_comments': expanded_comments,
        'feed_filter': feed_filter,
        'sort': sort,
        'query': query or '',
        'actor': actor,
    }


def community_models_registry(db):
    """Late-bound model registry to keep community package decoupled."""
    from app import (
        CommunityComment,
        CommunityNotification,
        CommunityPoll,
        CommunityPollOption,
        CommunityPollVote,
        CommunityPost,
        CommunityPostLike,
        CommunityRepost,
        CommunitySavedPost,
        User,
    )
    return {
        'CommunityPost': CommunityPost,
        'CommunityPostLike': CommunityPostLike,
        'CommunityComment': CommunityComment,
        'CommunitySavedPost': CommunitySavedPost,
        'CommunityRepost': CommunityRepost,
        'CommunityNotification': CommunityNotification,
        'CommunityPoll': CommunityPoll,
        'CommunityPollOption': CommunityPollOption,
        'CommunityPollVote': CommunityPollVote,
        'User': User,
    }
