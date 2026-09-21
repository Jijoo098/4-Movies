"""Session abstraction: swap mock/dev actor for authenticated user later."""

DEV_USER_USERNAME = 'MovieBuff'


class CommunityActor:
    __slots__ = (
        'id', 'username', 'display_name', 'bio', 'profile_pic',
        'is_verified', 'is_authenticated', 'is_dev_session',
    )

    def __init__(
        self,
        user_id,
        username,
        display_name=None,
        bio='',
        profile_pic=None,
        is_verified=False,
        is_authenticated=False,
        is_dev_session=False,
    ):
        self.id = user_id
        self.username = username
        self.display_name = display_name or username
        self.bio = bio or ''
        self.profile_pic = profile_pic
        self.is_verified = is_verified
        self.is_authenticated = is_authenticated
        self.is_dev_session = is_dev_session

    @property
    def can_interact(self):
        return self.is_authenticated or self.is_dev_session

    @property
    def initials(self):
        parts = self.display_name.split()
        if len(parts) >= 2:
            return (parts[0][0] + parts[1][0]).upper()
        return (self.display_name[:2] or self.username[:2]).upper()

    @classmethod
    def from_user(cls, user, *, is_authenticated=True, is_dev_session=False):
        if not user:
            return None
        verified = getattr(user, 'is_verified', False)
        display = getattr(user, 'display_name', None) or user.username
        return cls(
            user_id=user.id,
            username=user.username,
            display_name=display,
            bio=getattr(user, 'bio', '') or '',
            profile_pic=getattr(user, 'profile_pic', None),
            is_verified=verified,
            is_authenticated=is_authenticated,
            is_dev_session=is_dev_session,
        )


def resolve_community_actor(current_user_proxy, user_model):
    """Single entry point for who is acting in Community."""
    if current_user_proxy.is_authenticated:
        return CommunityActor.from_user(current_user_proxy, is_authenticated=True)
    dev_user = user_model.query.filter_by(username=DEV_USER_USERNAME).first()
    if dev_user:
        return CommunityActor.from_user(dev_user, is_authenticated=False, is_dev_session=True)
    return None


def actor_user_id(actor):
    return actor.id if actor else None
