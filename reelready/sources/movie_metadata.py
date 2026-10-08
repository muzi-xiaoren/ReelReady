"""Normalize public movie information; keep theatrical and digital dates separate."""
from urllib.parse import urlsplit


def person(item: dict, tmdb: bool = False) -> dict:
    avatar = None
    if tmdb and str(item.get('profile_path') or '').startswith('/'):
        avatar = 'https://image.tmdb.org/t/p/w185' + item['profile_path']
    elif not tmdb:
        avatars = item.get('avatars') or item.get('avatar') or {}
        url = avatars.get('normal') or avatars.get('large') if isinstance(avatars, dict) else None
        if url and urlsplit(url).scheme == 'https' and (urlsplit(url).hostname or '').endswith('.doubanio.com'):
            avatar = url
    return {'name': item.get('name') or item.get('name_en') or '', 'role': item.get('character') or item.get('role') or '', 'avatar': avatar}


def tmdb_details(data: dict) -> dict:
    credits = data.get('credits') or {}
    crew = credits.get('crew') or []
    releases = []
    for region in (data.get('release_dates') or {}).get('results') or []:
        for entry in region.get('release_dates') or []:
            if entry.get('type') in (2, 3) and entry.get('release_date'):
                releases.append({'date': entry['release_date'][:10], 'region': region.get('iso_3166_1', ''), 'certification': entry.get('certification', '')})
    releases = sorted({(r['date'], r['region']): r for r in releases}.values(), key=lambda r: r['date'])
    return {
        'release_date': data.get('release_date'), 'releases': releases,
        'genres': [g['name'] for g in data.get('genres') or [] if g.get('name')],
        'runtime': data.get('runtime'), 'tagline': data.get('tagline'),
        'countries': [c['name'] for c in data.get('production_countries') or [] if c.get('name')],
        'languages': [v.get('name') or v.get('english_name') for v in data.get('spoken_languages') or []],
        'companies': [c['name'] for c in data.get('production_companies') or [] if c.get('name')],
        'directors': [person(c, True) for c in crew if c.get('job') == 'Director'],
        'writers': [person(c, True) for c in crew if c.get('job') in ('Writer', 'Screenplay', 'Story')],
        'cast': [person(c, True) for c in (credits.get('cast') or [])[:60]],
        'budget': data.get('budget'), 'revenue': data.get('revenue'),
    }


def douban_details(data: dict) -> dict:
    pubdates = data.get('pubdate') or data.get('pubdates') or []
    if isinstance(pubdates, str):
        pubdates = [pubdates]
    return {
        'release_date': data.get('release_date'), 'pubdates': pubdates,
        'genres': data.get('genres') or [], 'countries': data.get('countries') or [],
        'languages': data.get('languages') or [], 'durations': data.get('durations') or [],
        'directors': [person(p) for p in data.get('directors') or []],
        'writers': [person(p) for p in data.get('writers') or []],
        'cast': [person(p) for p in (data.get('actors') or [])[:60]],
        'aliases': data.get('aka') or [],
    }
