import unittest
from reelready.models import Movie
from reelready.services.movies import apply_douban, apply_tmdb
from reelready.sources.douban import DoubanMovie
from reelready.sources.tmdb import TMDBMovie
from reelready.sources.movie_metadata import tmdb_details, douban_details


class MovieMetadataTests(unittest.TestCase):
    def test_credits_and_theatrical_dates_do_not_include_digital_release(self):
        data = {'runtime': 120, 'genres': [{'name': '剧情'}], 'credits': {'cast': [{'name': '演员', 'character': '主角', 'profile_path': None}], 'crew': [{'name': '导演', 'job': 'Director', 'profile_path': '/director.jpg'}]}, 'release_dates': {'results': [{'iso_3166_1': 'CN', 'release_dates': [{'type': 3, 'release_date': '2026-08-11T00:00:00Z'}, {'type': 4, 'release_date': '2026-10-11T00:00:00Z'}]}]}}
        info = tmdb_details(data)
        self.assertEqual(info['releases'], [{'date': '2026-08-11', 'region': 'CN', 'certification': ''}])
        self.assertEqual(info['cast'][0]['role'], '主角')
        self.assertIsNone(info['cast'][0]['avatar'])
        self.assertEqual(info['directors'][0]['name'], '导演')

    def test_douban_dates_and_missing_values(self):
        info = douban_details({'pubdate': '2026-08-11(中国大陆)', 'actors': [{'name': '演员'}], 'durations': ['120分钟']})
        self.assertEqual(info['pubdates'], ['2026-08-11(中国大陆)'])
        self.assertEqual(info['cast'][0]['name'], '演员')
        self.assertEqual(tmdb_details({})['cast'], [])

    def test_partial_data_preserves_overview_and_roles(self):
        movie = Movie(title='电影', overview='已有简介', details={})
        apply_tmdb(movie, TMDBMovie(1, 'Movie', release_date='2026-08-11', details={'cast': [{'name': '演员', 'role': '主角'}], 'runtime': 120}))
        apply_douban(movie, DoubanMovie('1', '电影', details={'cast': [{'name': '演员', 'role': ''}], 'runtime': None}))
        self.assertEqual(movie.details['cast'][0]['role'], '主角')
        self.assertEqual(movie.overview, '已有简介')
        self.assertEqual(movie.details['runtime'], 120)
        self.assertEqual(movie.details['release_date'], '2026-08-11')
