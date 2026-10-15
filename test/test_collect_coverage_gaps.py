"""Offline behavior tests for site isolation, answer provenance and resumable merge."""
import argparse
import contextlib
import copy
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SCRAPER_ROOT = Path(__file__).resolve().parents[1] / 'scraper'
sys.path.insert(0, str(SCRAPER_ROOT))

import collect_coverage_gaps as collect


def theme():
    return {'id': 'dnachisel', 'tag': '', 'strategy': 'keyword', 'terms': ['dnachisel'],
            'coverage_targets': [{'dimension': 'tool', 'canonical_name': 'DnaChisel'}],
            'sites': ['stackoverflow', 'bioinformatics'], 'query': 'DnaChisel', 'discovery_query': 'DnaChisel'}


def answer(aid=3, accepted=False, body=None):
    return {'answer_id': aid, 'question_id': 999999999, 'is_accepted': accepted, 'score': 2,
            'body': body or '<p>Use DnaChisel to reverse translate the protein sequence.</p><pre><code>import dnachisel\nreverse_translate(sequence)</code></pre>'}


def question(site='bioinformatics'):
    return {'site': site, 'question_id': 999999999, 'question_key': collect.question_key(site, 999999999), 'title': 'Reverse translation',
            'body': '<p>How can I translate a protein back into DNA?</p>', 'tags': [],
            'score': 0, 'answer_count': 1, 'search_hits': [{'theme_id': 'dnachisel'}]}


class MultisiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.args = argparse.Namespace(
            baseline=collect.PROJECT / 'data/coverage_gaps/so_data_r1.json',
            tags_file=collect.CONFIG / 'tags.json', gaps_file=collect.CONFIG / 'coverage_gaps.json',
            sites_file=collect.CONFIG / 'sites/terminal_lego_site_selection.tb2_181_tools.v3.json',
            aliases_file=collect.CONFIG / 'tags.zero-hit.retry.json',
            output_root=root / 'raw', merged_root=root / 'merged', run_id='test',
            resume=False, count=5000, max_requests=100, quota_reserve=5,
            max_hours=1, http_proxy=None, request_interval=0, page_size=50)

    def collector(self):
        return collect.Collector(self.args, [theme()], ['stackoverflow', 'bioinformatics'], '')

    def test_same_id_on_two_sites_remains_two_questions(self):
        records = {}
        collect.merge_question(records, question('stackoverflow'))
        collect.merge_question(records, question('bioinformatics'))
        self.assertEqual(len(records), 2)

    def test_multiple_hits_are_merged_without_changing_body(self):
        records = {}
        q = question()
        collect.merge_question(records, q)
        new = dict(q, body='new content', search_hits=[{'theme_id': 'another'}])
        collect.merge_question(records, new)
        merged = next(iter(records.values()))
        self.assertEqual(merged['body'], q['body'])
        self.assertEqual(len(merged['search_hits']), 2)

    def test_answer_only_match_is_retained_without_fake_acceptance(self):
        c = self.collector()
        q = dict(question(), answers=[answer()], answers_complete=True)
        c.classify(q)
        self.assertTrue(q['eligible_new_qa'])
        self.assertEqual(q['answer_status'], 'answered_review')
        self.assertEqual(q['selected_answer_id'], 3)
        self.assertNotIn('accepted_answer', q)
        self.assertEqual(q['target_match_evidence'][0]['matched_in'], 'answer')

    def test_unrelated_accepted_answer_does_not_hide_relevant_unaccepted_answer(self):
        c = self.collector()
        q = dict(question(), accepted_answer_id=4,
                 answers=[answer(4, True, '<p>Use a different tool for this task. This is an unrelated detailed explanation which does not name the requested library.</p>'), answer(3)])
        c.classify(q)
        self.assertEqual(q['answer_status'], 'answered_review')
        self.assertEqual(q['selected_answer_id'], 3)
        self.assertEqual(q['accepted_answer']['answer_id'], 4)

    def test_unanswered_and_failed_download_are_distinct(self):
        c = self.collector()
        q = dict(question(), title='DnaChisel usage', answer_count=0)
        c.classify(q)
        self.assertEqual(q['answer_status'], 'unanswered')
        failed = dict(q, answer_count=1)
        c.classify(failed)
        self.assertEqual(failed['answer_status'], 'fetch_pending')

    def test_link_only_answer_is_archived_not_eligible(self):
        c = self.collector()
        q = dict(question(), answers=[answer(body='<p>Try <a href="https://example.org">DnaChisel</a>.</p>')])
        c.classify(q)
        self.assertFalse(q['eligible_new_qa'])
        self.assertEqual(q['answer_status'], 'reference_only')

    def test_same_named_mongodb_mtools_is_not_a_fat_tool_match(self):
        t = dict(theme(), id='mtools', terms=['mtools', 'mcopy', 'mformat'], coverage_targets=[{'dimension': 'tool', 'canonical_name': 'mtools'}])
        q = dict(question(), title='How to install mtools by pip?', body='<p>I want to inspect MongoDB logs using mtools.</p>')
        self.assertEqual(collect.match_evidence(t, q, []), [])
        q['body'] = '<p>Use GNU mtools to copy files from a FAT32 disk image with mcopy.</p>'
        self.assertTrue(collect.match_evidence(t, q, []))

    def test_dependency_list_alone_does_not_count_as_ncompress(self):
        t = dict(theme(), terms=['ncompress'], coverage_targets=[{'dimension': 'tool', 'canonical_name': 'ncompress'}])
        q = dict(question(), title='Docker Python package problem', body='<p>Python imports fail.</p><pre>apt install gcc ncompress python3</pre>')
        self.assertEqual(collect.match_evidence(t, q, []), [])
        q['title'] = 'How to install ncompress on RHEL?'
        self.assertTrue(collect.match_evidence(t, q, []))

    def test_mcopy_variable_and_matrix_method_are_not_mtools(self):
        t = dict(theme(), id='mtools', terms=['mtools', 'mcopy', 'mformat'], coverage_targets=[{'dimension': 'tool', 'canonical_name': 'mtools'}])
        q = dict(question(), title='How to copy a matrix?', body='<pre>mCopy = matrix.copy(); mformat = "%.3f"</pre>')
        self.assertEqual(collect.match_evidence(t, q, []), [])
        q['body'] = '<pre>mcopy -i disk.img file.txt ::file.txt</pre>'
        self.assertTrue(collect.match_evidence(t, q, []))

    def test_rcsb_website_reference_without_api_context_is_not_an_api_candidate(self):
        t = dict(theme(), terms=['rcsb'], coverage_targets=[{'dimension': 'tool', 'canonical_name': 'RCSB PDB API'}])
        q = dict(question(), body='<p>Look up the protein structure on the RCSB website.</p>')
        self.assertEqual(collect.match_evidence(t, q, []), [])
        q['body'] = '<p>Use the RCSB REST API to query the structure as JSON.</p>'
        self.assertTrue(collect.match_evidence(t, q, []))

    def test_windows_global_flags_is_not_google_gflags(self):
        t = dict(theme(), terms=['gflags'], coverage_targets=[{'dimension': 'tool', 'canonical_name': 'gflags'}])
        q = dict(question(), title='How to enable PageHeap with GFlags?', body='<p>Use WinDbg and gflags.exe for heap corruption debugging.</p>', tags=['c++', 'windbg'])
        self.assertEqual(collect.match_evidence(t, q, []), [])
        q['body'] = '<pre>#include &lt;gflags/gflags.h&gt;\ngflags::ParseCommandLineFlags(argc, argv, true);</pre>'
        self.assertTrue(collect.match_evidence(t, q, []))

    def test_lua_cjson_is_not_the_native_cjson_library(self):
        t = dict(theme(), terms=['cjson'], coverage_targets=[{'dimension': 'tool', 'canonical_name': 'cJSON'}])
        q = dict(question(), title='Lua cjson decode', body='<pre>local cjson = require("cjson")\ncjson.decode(data)</pre>', tags=['lua', 'cjson'])
        self.assertEqual(collect.match_evidence(t, q, []), [])
        q.update(body='<pre>#include "cJSON.h"\ncJSON *r = cJSON_Parse(data);</pre>', tags=['c', 'cjson'])
        self.assertTrue(collect.match_evidence(t, q, []))

    def test_all_answers_are_fetched_from_the_source_site_with_pagination(self):
        c = self.collector()
        q = dict(question(), site='bioinformatics', answer_count=2)
        key = collect.question_key('bioinformatics', q['question_id'])
        c.state['records'][key] = q
        c.api.get = Mock(side_effect=[{'items': [answer(3)], 'has_more': True},
                                      {'items': [answer(4)], 'has_more': False}])
        c.fetch_answers('bioinformatics', [q['question_id']])
        self.assertEqual(len(q['answers']), 2)
        self.assertTrue(q['answers_complete'])
        self.assertEqual([call.args[1] for call in c.api.get.call_args_list], ['bioinformatics'] * 2)
        self.assertEqual([call.args[2]['page'] for call in c.api.get.call_args_list], [1, 2])

    def test_search_excerpts_are_replaced_with_full_question(self):
        c = self.collector()
        full = question()
        full['body'] = '<p>Full problem statement and input sequence.</p>'
        c.api.get = Mock(side_effect=[
            {'items': [{'question_id': full['question_id'], 'answer_id': 3, 'item_type': 'answer', 'body': 'snippet'}]},
            {'items': [full]}, {'items': [answer()], 'has_more': False}])
        t = {'theme_id': 'dnachisel', 'site': 'bioinformatics', 'tag': '', 'query': 'DnaChisel',
             'page': 1, 'phase': 'answer_discovery', 'endpoint': '/search/excerpts'}
        c.search(t)
        saved = c.state['records'][collect.question_key('bioinformatics', full['question_id'])]
        self.assertEqual(saved['body'], full['body'])
        self.assertTrue(saved['eligible_new_qa'])

    def test_resume_skips_completed_search_and_keeps_records(self):
        c = self.collector()
        deadline = c.api.deadline
        t = c.state['tasks'][0]
        c.state['completed'][collect.task_id(t)] = {'task': t, 'status': 'complete'}
        c.state['records']['bioinformatics:999999999'] = question()
        c.checkpoint()
        self.args.resume = True
        resumed = self.collector()
        self.assertAlmostEqual(resumed.api.deadline, deadline, places=3)
        resumed.search = Mock()
        resumed.state['tasks'] = [t]
        resumed.process_queue()
        resumed.search.assert_not_called()
        self.assertIn('bioinformatics:999999999', resumed.state['records'])

    def test_merge_preserves_1666_and_reports_exact_additions(self):
        c = self.collector()
        q = dict(question(), answers=[answer()], answers_complete=True, fetch_status='complete', link='https://bioinformatics.stackexchange.com/questions/999999999')
        c.state['records']['bioinformatics:999999999'] = q
        c.state.update(status='complete', stop_reason='planned_searches_completed')
        c.export()
        merged = json.loads((c.merged_dir / 'qa_merged.json').read_text())
        summary = json.loads((c.merged_dir / 'summary.json').read_text())
        self.assertEqual(len(merged['questions']), 1667)
        self.assertEqual(summary['added_answered_candidates'], 1)
        self.assertTrue(summary['baseline_preserved'])
        self.assertEqual(summary['targets'][0]['added_answered_candidates'], 1)
        self.assertEqual(collect.file_hash(self.args.baseline), c.baseline_hash)

    def test_resume_accepts_relocated_inputs_but_rejects_changed_content(self):
        root = Path(self.temp.name)
        config = root / 'config' / 'scraper'
        legacy = root / 'scraper'
        for field in ('tags_file', 'gaps_file', 'sites_file', 'aliases_file'):
            original = getattr(self.args, field)
            destination = config / original.relative_to(collect.CONFIG)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(original.read_bytes())
            setattr(self.args, field, destination)
        with patch.object(collect, 'ROOT', legacy), patch.object(collect, 'CONFIG', config):
            c = self.collector()
            c.state['input_hashes'] = {
                str(legacy / Path(path).relative_to(config)): digest
                for path, digest in c.state['input_hashes'].items()
            }
            c.checkpoint()
            self.args.resume = True
            resumed = self.collector()
            self.assertEqual(resumed.state['input_hashes'], c.state['input_hashes'])
            with self.args.tags_file.open('a') as stream:
                stream.write('\n')
            with self.assertRaisesRegex(AssertionError, 'Configuration changed since checkpoint'):
                self.collector()


class ClientTests(unittest.TestCase):
    def client(self, count=0, quota=100):
        state = {'request_count': count, 'quota_remaining': quota}
        return collect.ApiClient('private-test-value', None, state, 100, 5, time.time() + 60, interval=0)

    def test_cache_is_scoped_to_site(self):
        client = self.client()
        r = Mock(status_code=200)
        r.json.return_value = {'items': [], 'quota_remaining': 90}
        client.session.get = Mock(return_value=r)
        client.get('/search/advanced', 'stackoverflow', {'q': 'x'})
        client.get('/search/advanced', 'stackoverflow', {'q': 'x'})
        client.get('/search/advanced', 'unix', {'q': 'x'})
        self.assertEqual(client.session.get.call_count, 2)

    def test_quota_stops_before_a_request(self):
        client = self.client(quota=5)
        client.session.get = Mock()
        with self.assertRaises(collect.StopCollection):
            client.get('/search/advanced', 'unix')
        client.session.get.assert_not_called()

    def test_backoff_is_shared_across_answer_id_batches(self):
        client = self.client()
        r = Mock(status_code=200)
        r.json.return_value = {'items': [], 'quota_remaining': 90, 'backoff': 20}
        client.session.get = Mock(return_value=r)
        client.get('/questions/1/answers', 'unix')
        self.assertGreater(client.method_ready['/questions/{ids}/answers'], time.time())

    def test_http_errors_do_not_expose_credentials(self):
        client = self.client()
        r = Mock(status_code=400)
        r.json.return_value = {'error_name': 'bad_parameter', 'error_message': 'private-test-value'}
        client.session.get = Mock(return_value=r)
        with self.assertRaises(collect.RequestFailed) as error:
            client.get('/search/advanced', 'unix')
        self.assertNotIn('private-test-value', str(error.exception))


if __name__ == '__main__':
    unittest.main()
