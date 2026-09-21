"""Realistic entertainment seed profiles and post templates."""

SEED_USERS = [
    {
        'username': 'MovieBuff',
        'email': 'moviebuff@filmboxed.dev',
        'display_name': 'MovieBuff',
        'bio': 'IMAX enthusiast. Sci-fi, epics, and anything with a Hans Zimmer score.',
        'is_verified': True,
    },
    {
        'username': 'CineTalks',
        'email': 'cinetalks@filmboxed.dev',
        'display_name': 'CineTalks',
        'bio': 'Breaking down films frame by frame. Weekly deep dives on new releases.',
        'is_verified': True,
    },
    {
        'username': 'ScreenGraph',
        'email': 'screengraph@filmboxed.dev',
        'display_name': 'ScreenGraph',
        'bio': 'Visual storytelling, cinematography, and production design.',
        'is_verified': False,
    },
    {
        'username': 'TheFilmCorner',
        'email': 'thefilmcorner@filmboxed.dev',
        'display_name': 'The Film Corner',
        'bio': 'Indie gems, festival picks, and under-seen masterpieces.',
        'is_verified': False,
    },
    {
        'username': 'CinemaVerse',
        'email': 'cinemaverse@filmboxed.dev',
        'display_name': 'CinemaVerse',
        'bio': 'MCU, DC, and every cinematic universe in between.',
        'is_verified': True,
    },
    {
        'username': 'Cinephile_11',
        'email': 'cinephile11@filmboxed.dev',
        'display_name': 'Cinephile_11',
        'bio': 'Letterboxd energy, Filmboxed heart. Always chasing the perfect shot.',
        'is_verified': False,
    },
]

# TMDB ids for attachments (movies / series)
ATTACHMENTS = {
    'dune2': {
        'kind': 'movie', 'tmdb_id': '693134', 'title': 'Dune: Part Two',
        'year': '2024', 'genre': 'Sci-Fi', 'rating_label': '4.8',
        'poster': 'https://image.tmdb.org/t/p/w500/1pdfLvkbp9I5uyzP1X6ANrH7xF.jpg',
    },
    'interstellar': {
        'kind': 'movie', 'tmdb_id': '157336', 'title': 'Interstellar',
        'year': '2014', 'genre': 'Sci-Fi', 'rating_label': '4.7',
        'poster': 'https://image.tmdb.org/t/p/w500/gEU2QniE6E77NI6lCU6MxlNBvIx.jpg',
    },
    'inception': {
        'kind': 'movie', 'tmdb_id': '27205', 'title': 'Inception',
        'year': '2010', 'genre': 'Sci-Fi', 'rating_label': '4.6',
        'poster': 'https://image.tmdb.org/t/p/w500/9gk7adHYeDvHkCQSjpWFhtNZ2s.jpg',
    },
    'dark_knight': {
        'kind': 'movie', 'tmdb_id': '155', 'title': 'The Dark Knight',
        'year': '2008', 'genre': 'Action', 'rating_label': '4.9',
        'poster': 'https://image.tmdb.org/t/p/w500/qJ2tW6WMUDux911r6m7haRef0WH.jpg',
    },
    'oppenheimer': {
        'kind': 'movie', 'tmdb_id': '872585', 'title': 'Oppenheimer',
        'year': '2023', 'genre': 'Drama', 'rating_label': '4.5',
        'poster': 'https://image.tmdb.org/t/p/w500/8Gxv8gSFCU0XGDykEGv7zR1n2ua.jpg',
    },
    'mirzapur': {
        'kind': 'series', 'tmdb_id': '84105', 'title': 'Mirzapur',
        'year': '2018', 'genre': 'Crime', 'rating_label': '4.2',
        'poster': 'https://image.tmdb.org/t/p/w500/1rxLUFVrtTo82OxhbDXJDiJVkwL.jpg',
    },
}

SEED_POSTS = [
    {
        'author': 'MovieBuff',
        'post_type': 'movie-discussion',
        'hours_ago': 2,
        'content': (
            'Just finished watching Dune: Part Two in IMAX. What a visual masterpiece! '
            'The sound, the scale, the story—everything was perfect.'
        ),
        'attachment': 'dune2',
        'hashtags': ['DunePartTwo', 'IMAX', 'DenisVilleneuve'],
        'likes': 342, 'comments': 68, 'reposts': 27,
    },
    {
        'author': 'CineTalks',
        'post_type': 'review',
        'hours_ago': 5,
        'title': 'A love letter to time and grief',
        'content': (
            'Interstellar still hits differently on a rewatch. The docking scene alone '
            'is worth the price of admission. Nolan at his most emotional.'
        ),
        'attachment': 'interstellar',
        'rating': 9,
        'hashtags': ['Interstellar', 'ChristopherNolan'],
        'likes': 218, 'comments': 41, 'reposts': 19,
        'spoiler': False,
    },
    {
        'author': 'ScreenGraph',
        'post_type': 'text',
        'hours_ago': 8,
        'content': (
            'Hot take: the best cinematography of the decade isn\'t in a blockbuster—it\'s in '
            'the quiet frames between dialogue. What\'s your pick for most beautiful film of the 2020s?'
        ),
        'hashtags': ['MovieRecommendations', 'Cinematography'],
        'likes': 156, 'comments': 92, 'reposts': 11,
    },
    {
        'author': 'CinemaVerse',
        'post_type': 'poll',
        'hours_ago': 12,
        'content': 'Which Christopher Nolan movie is your all-time favorite?',
        'poll': {
            'question': 'Which Christopher Nolan movie is your all-time favorite?',
            'options': [
                ('Interstellar', 42),
                ('Inception', 31),
                ('The Dark Knight', 20),
                ('Oppenheimer', 7),
            ],
        },
        'hashtags': ['ChristopherNolan', 'Poll'],
        'likes': 89, 'comments': 34, 'reposts': 8,
    },
    {
        'author': 'TheFilmCorner',
        'post_type': 'series-discussion',
        'hours_ago': 18,
        'content': (
            'Mirzapur S3 is finally delivering on the promise of the first season. '
            'The power dynamics feel earned again. Who else is bingeing this weekend?'
        ),
        'attachment': 'mirzapur',
        'hashtags': ['Mirzapur', 'BollywoodTalks', 'Series'],
        'likes': 124, 'comments': 47, 'reposts': 15,
    },
    {
        'author': 'Cinephile_11',
        'post_type': 'review',
        'hours_ago': 24,
        'title': 'Heath Ledger forever',
        'content': (
            'Rewatched The Dark Knight last night. The Joker interrogation scene still '
            'gives me chills. Ledger\'s performance is untouchable.'
        ),
        'attachment': 'dark_knight',
        'rating': 10,
        'spoiler': True,
        'hashtags': ['TheDarkKnight', 'DC'],
        'likes': 267, 'comments': 53, 'reposts': 22,
    },
    {
        'author': 'MovieBuff',
        'post_type': 'link',
        'hours_ago': 30,
        'content': 'Great breakdown of Oppenheimer\'s sound design—worth a read before your next rewatch.',
        'link_url': 'https://www.filmschoolrejects.com/',
        'link_title': 'How Oppenheimer Uses Silence as a Weapon',
        'hashtags': ['Oppenheimer', 'SoundDesign'],
        'likes': 74, 'comments': 12, 'reposts': 6,
    },
    {
        'author': 'CineTalks',
        'post_type': 'text',
        'hours_ago': 36,
        'content': (
            'Unpopular opinion: the best movie-going experience is a nearly empty matinee '
            'screen with perfect seat acoustics. No phones, no chatter, just the film.'
        ),
        'hashtags': ['MovieRecommendations'],
        'likes': 193, 'comments': 28, 'reposts': 9,
    },
]

SEED_COMMENTS = {
    'dune_post': [
        ('Cinephile_11', 'The sound design in that final sequence was insane.', 3),
        ('MovieBuff', 'Exactly! Hans Zimmer absolutely nailed it.', 2, 'dune_reply'),
    ],
}

TRENDING_TOPIC_SEEDS = [
    ('DunePartTwo', 1200),
    ('Interstellar', 856),
    ('MovieRecommendations', 642),
    ('BollywoodTalks', 498),
    ('ChristopherNolan', 411),
    ('IMAX', 287),
]

RECOMMENDED_DISCUSSIONS = [
    {'title': 'Dune: Part Two', 'attachment': 'dune2', 'posts': 1200},
    {'title': 'Interstellar', 'attachment': 'interstellar', 'posts': 856},
    {'title': 'Mirzapur', 'attachment': 'mirzapur', 'posts': 498},
]
