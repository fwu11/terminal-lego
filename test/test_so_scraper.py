"""Offline checks for targeted Stack Overflow collection; no live API calls."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SCRAPER_ROOT = Path(__file__).resolve().parents[1] / 'scraper'
sys.path.insert(0, str(SCRAPER_ROOT))

import so_scraper as scraper

CONFIG = scraper.CONFIG_DIR


def question(qid, score=0, title='MobileSAM installation', body='<p>import mobile_sam</p>', accepted=True):
    return {
        'question_id': qid, 'title': title, 'body': body, 'score': score,
        'link': f'https://stackoverflow.com/questions/{qid}',
        'accepted_answer_id': qid + 1000 if accepted else None, 'tags': ['python'],
    }


def response(items, has_more=False):
    result = Mock(status_code=200)
    result.json.return_value = {'items': items, 'has_more': has_more, 'quota_remaining': 10000}
    return result


class SearchConfigurationTests(unittest.TestCase):
    def load(self, config):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tags.json'
            path.write_text(json.dumps(config))
            return scraper.load_searches(path)

    def test_original_backup_is_still_supported(self):
        path = CONFIG / 'archive/tags.backup.20261012.json'
        old = json.loads(path.read_text())
        self.assertEqual(scraper.load_tags(path), [(x['tag'], x['weight']) for x in old['tags']])

    def test_queries_can_share_a_tag_and_override_defaults(self):
        searches = self.load({
            'defaults': {'min_score': 0, 'start_page': 1, 'max_pages': 10, 'sort': 'votes'},
            'tags': [
                {'id': 'collections', 'tag': 'rust', 'query': 'std::collections', 'weight': 100},
                {'id': 'convert', 'tag': 'rust', 'query': 'std::convert', 'weight': 100, 'max_pages': 2},
                {'id': 'dna', 'query': 'DnaChisel', 'weight': 100},
            ],
        })
        self.assertEqual(searches[0]['start_page'], 1)
        self.assertEqual(searches[1]['max_pages'], 2)
        self.assertEqual(searches[2]['tag'], '')

    def test_invalid_searches_fail_before_collection(self):
        invalid = [
            {'tags': []},
            {'tags': [{'tag': '', 'weight': 1}]},
            {'tags': [{'tag': 'rust', 'weight': True}]},
            {'tags': [{'tag': 'rust', 'weight': 1}, {'tag': 'rust', 'weight': 1}]},
            {'tags': [{'id': 'x', 'tag': 'rust', 'weight': 1}, {'id': 'x', 'tag': 'python', 'weight': 1}]},
            {'defaults': {'start_page': 0}, 'tags': [{'tag': 'rust', 'weight': 1}]},
            {'tags': [{'tag': 'rust', 'weight': 1, 'required_terms': 'std::convert'}]},
        ]
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.load(config)

    def test_profiles_match_the_gap_inventory(self):
        master = json.loads((CONFIG / 'coverage_gaps.json').read_text())
        targets = {(dimension, r['canonical_name']) for dimension, key in [('language', 'languages'), ('tool', 'tools')]
                   for r in master[key] if r['include_in_default_scraping']}
        profiles = {}
        for name in ('tags.json', 'tags.languages.json', 'tags.tools.json'):
            profiles[name] = scraper.load_searches(CONFIG / name)
        actual = {(t['dimension'], t['canonical_name']) for search in profiles['tags.json'] for t in search['coverage_targets']}
        self.assertEqual(actual, targets)
        self.assertEqual(len(actual), master['source']['active_language_targets'] + master['source']['active_tool_targets'])
        for name, dimension in [('tags.json', None), ('tags.languages.json', 'language'), ('tags.tools.json', 'tool')]:
            metadata = json.loads((CONFIG / name).read_text())['metadata']
            self.assertEqual(len(profiles[name]), metadata['search_count'])
            profile_targets = {(t['dimension'], t['canonical_name']) for search in profiles[name] for t in search['coverage_targets']}
            self.assertEqual(profile_targets, {t for t in targets if dimension is None or t[0] == dimension})
        for name in master['source']['excluded_confirmed_training_use']:
            self.assertNotIn(('tool', name), actual)
        self.assertNotIn(('tool', 'PyMuPDF'), actual)
        self.assertEqual(sum(s['strategy'] == 'related_topic_only' for s in profiles['tags.json']), 1)


class RequestSelectionTests(unittest.TestCase):
    @patch.object(scraper.time, 'sleep')
    @patch.object(scraper.requests, 'get')
    def test_keyword_only_request_filters_and_deduplicates(self, get, sleep):
        get.return_value = response([
            question(1, accepted=False), question(2, title='Other topic', body='unrelated'),
            question(3, score=-1), question(4), question(5), question(5), question(6),
        ], has_more=True)
        excluded = {4}
        with contextlib.redirect_stdout(io.StringIO()):
            questions, stop = scraper.get_questions_by_tag(
                '', '', min_score=0, sort='votes', start_page=1, query='MobileSAM',
                required_terms=['mobilesam', 'mobile_sam'], exclude_ids=excluded, max_questions=2,
            )
        self.assertEqual([q['question_id'] for q in questions], [5, 6])
        self.assertFalse(stop)
        self.assertEqual(excluded, {4})
        self.assertEqual(get.call_count, 1)
        args, kwargs = get.call_args
        self.assertTrue(args[0].endswith('/search/advanced'))
        self.assertNotIn('tagged', kwargs['params'])
        self.assertEqual(kwargs['params']['q'], 'MobileSAM')
        self.assertEqual(kwargs['params']['accepted'], 'true')
        self.assertEqual(kwargs['params']['min'], 0)

    @patch.object(scraper.time, 'sleep')
    @patch.object(scraper.requests, 'get')
    def test_activity_score_threshold_is_client_side(self, get, sleep):
        get.return_value = response([question(1, score=19), question(2, score=20)])
        with contextlib.redirect_stdout(io.StringIO()):
            questions, _ = scraper.get_questions_by_tag('rust', '', min_score=20, sort='activity', max_pages=1)
        self.assertEqual([q['question_id'] for q in questions], [2])
        self.assertNotIn('min', get.call_args.kwargs['params'])
        self.assertTrue(get.call_args.args[0].endswith('/questions'))

    @patch.object(scraper.time, 'sleep')
    @patch.object(scraper.requests, 'get')
    def test_page_one_and_budget_cap(self, get, sleep):
        get.side_effect = [response([question(1)], has_more=True), response([question(2), question(3)], has_more=True)]
        with contextlib.redirect_stdout(io.StringIO()):
            questions, _ = scraper.get_questions_by_tag('mujoco', '', min_score=0, sort='votes', query='', start_page=1, max_questions=2)
        self.assertEqual([q['question_id'] for q in questions], [1, 2])
        self.assertEqual([c.kwargs['params']['page'] for c in get.call_args_list], [1, 2])
        self.assertEqual(get.call_args.kwargs['params']['tagged'], 'mujoco')
        self.assertNotIn('q', get.call_args.kwargs['params'])

    @patch.object(scraper.requests, 'get', side_effect=AssertionError('dry run must not request the API'))
    def test_dry_run_does_not_create_data(self, get):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'not-created'
            buffer = io.StringIO()
            with patch.object(scraper.sys, 'argv', ['so_scraper.py', '--dry-run', '--output', str(output)]), contextlib.redirect_stdout(buffer):
                scraper.main()
            plan = json.loads(buffer.getvalue())
            self.assertEqual(len(plan['searches']), len(scraper.load_searches(CONFIG / 'tags.json')))
            self.assertFalse(output.exists())
            get.assert_not_called()

    @patch.object(scraper.time, 'sleep')
    @patch.object(scraper, 'get_answers_batch')
    @patch.object(scraper, 'get_questions_by_tag')
    def test_collection_passes_settings_and_saves_intent(self, get_questions, get_answers, sleep):
        get_questions.return_value = ([question(11)], False)
        get_answers.return_value = {1011: {'answer_id': 1011, 'body': '<p>answer</p>', 'score': 1}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'search.json'
            config.write_text(json.dumps({'defaults': {'min_score': 0, 'sort': 'votes', 'start_page': 1, 'max_pages': 2}, 'tags': [
                {'id': 'mobile', 'query': 'MobileSAM', 'weight': 1, 'required_terms': ['mobilesam'],
                 'coverage_targets': [{'dimension': 'tool', 'canonical_name': 'MobileSAM'}]},
            ]}))
            argv = ['so_scraper.py', '--count', '1', '--tags-file', str(config), '--output', str(root / 'data')]
            with patch.object(scraper.sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()):
                scraper.main()
            kwargs = get_questions.call_args.kwargs
            self.assertEqual(kwargs['query'], 'MobileSAM')
            self.assertEqual(kwargs['min_score'], 0)
            self.assertEqual(kwargs['start_page'], 1)
            self.assertEqual(kwargs['max_questions'], 1)
            saved = json.loads((root / 'data/so_data_r1.jsonl').read_text())
            self.assertEqual(saved['search_id'], 'mobile')
            self.assertEqual(saved['search_query'], 'MobileSAM')
            self.assertEqual(saved['coverage_targets'][0]['canonical_name'], 'MobileSAM')


if __name__ == '__main__':
    unittest.main()
