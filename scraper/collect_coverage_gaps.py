#!/usr/bin/env python3
"""Resumable, bounded Stack Exchange gap collection and lossless baseline merge.

Search matches are collection candidates, not task labels or verified coverage.
API credentials are used only in memory and never saved in state or logs.
"""
import argparse
import copy
import csv
import hashlib
import html
import json
import logging
import os
import re
import shutil
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
import so_scraper

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
CONFIG = PROJECT / 'config' / 'scraper'
LOG = logging.getLogger('coverage_collection')
RARE = {'dnachisel', 'doomgeneric', 'fpbase-api', 'httpstan', 'mobilesam',
        'pyknotid', 'rcsb-api-python', 'mtools', 'ncompress', 'vncsnapshot'}
ALIASES = {
    'caffe-prototxt': ['prototxt'], 'g-code': ['g-code', 'gcode'],
    'mujoco-mjcf': ['mujoco', 'mjcf'], 'mailman': ['mailman'],
    'povray': ['povray', 'pov-ray'], 'redcode-pmars': ['redcode', 'pmars', 'core war'],
    'stan': ['stan'], 'logic-gate-dsl-related': ['logic gate', 'nand', 'adder', 'netlist'],
    'gnucobol': ['gnucobol', 'gnu-cobol', 'opencobol'],
    'mtools': ['mtools', 'mcopy', 'mformat'], 'ncompress': ['ncompress'],
    'rust-collections': ['std::collections', 'HashMap', 'HashSet', 'BTreeMap', 'BTreeSet', 'BinaryHeap', 'VecDeque'],
    'rust-convert': ['std::convert', 'TryFrom', 'TryInto', 'AsRef', 'AsMut'],
}
EXTRA_QUERIES = {
    'dnachisel': ['"DNA Chisel"', 'DnaOptimizationProblem', 'EnforceGCContent'],
    'doomgeneric': ['doomgeneric_Create', 'DG_DrawFrame'],
    'fpbase-api': ['fpbase.org'], 'httpstan': ['httpstan'],
    'mobilesam': ['mobile_sam', '"Mobile SAM"'], 'pyknotid': ['pyknotid.spacecurves'],
    'rcsb-api-python': ['"rcsb-api"', 'rcsbapi.search'],
    'mtools': ['mcopy', 'mformat'], 'ncompress': ['"compress .Z"'],
    'vncsnapshot': ['vncsnapshot'],
    'rust-collections': ['"std::collections"', 'HashMap', 'HashSet', 'VecDeque'],
    'rust-convert': ['"std::convert"', 'TryFrom', 'TryInto'],
}


def now():
    return datetime.now(timezone.utc).isoformat()


def file_hash(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def text_of(body, without_code=False):
    if without_code:
        body = re.sub(r'<(?:pre|code)\b[^>]*>.*?</(?:pre|code)>', ' ', body, flags=re.S | re.I)
    return html.unescape(re.sub(r'<[^>]+>', ' ', body or ''))


def substantive(body):
    # Link-only recommendations remain raw references, not usable answer seeds.
    plain = text_of(body).strip()
    no_urls = re.sub(r'https?://\S+', ' ', plain)
    return bool(re.search(r'<(?:pre|code)\b', body, re.I)) or len(re.sub(r'\s+', '', no_urls)) >= 80


def non_english(body):
    plain = text_of(body, without_code=True)
    letters = sum(c.isalpha() for c in plain)
    non_latin = sum('\u0400' <= c <= '\u052f' or '\u4e00' <= c <= '\u9fff'
                    or '\u3040' <= c <= '\u30ff' or '\uac00' <= c <= '\ud7af' for c in plain)
    return letters > 30 and non_latin / letters > 0.35


def target_terms(theme, target):
    name = target['canonical_name']
    if name == 'MJCF':
        return ['mjcf', '<mujoco', 'mujoco xml']
    if name == 'Mailman 3':
        return ['mailman3', 'mailman 3', 'mailman-3', 'mailman.cfg']
    if name == 'Mailman configuration':
        return ['mailman.cfg', 'mailman configuration', 'mailman config', '[mta]', 'mailman3']
    return theme['terms']


def match_evidence(theme, q, answers):
    evidence = []
    docs = [('question', None, q.get('title', '') + '\n' + q.get('body', ''))]
    docs.extend(('answer', a['answer_id'], a.get('body', '')) for a in answers)
    combined = q.get('title', '') + '\n' + '\n'.join(body for _, _, body in docs)
    raw_context = html.unescape(combined).casefold()
    plain_context = text_of(combined).casefold()
    natural_context = text_of(combined, without_code=True).casefold()
    for target in theme['coverage_targets']:
        name = target['canonical_name']
        if name == 'gflags' and not re.search(
                r'gflags::|\b(?:define|declare)_[a-z]|parsecommandlineflags|libgflags|gflags\.h|github\.com/gflags/gflags|gflags\.github\.io|command[- ]line flags', raw_context):
            # Windows Global Flags/PageHeap is a different product.
            continue
        if name == 'cJSON' and not (re.search(
                r'\bcjson_(?:parse|print|create|delete|add|get|is)[a-z_]*\b|\bcjson\.h\b|davegamble/cjson', raw_context)
                or (set(q.get('tags', [])) & {'c', 'c++', 'objective-c'}
                    and not re.search(r'\blua\b|lua-cjson|cjson\.(?:decode|encode)', plain_context))):
            # Lua/Python cjson modules are not the benchmark's native cJSON library.
            continue
        if name == 'mtools' and not re.search(
                r'\b(?:fat12|fat16|fat32|floppy|msdos|ms-dos|minix)\b|fat\s+(?:file|volume|partition|disk)|disk image|gnu\s+mtools|\b(?:mcopy|mformat)\s+-[a-z]', plain_context):
            continue
        if name == 'ncompress' and not (re.search(r'\bncompress\b', natural_context)
                                      or re.search(r'\b(?:lzw|uncompress)\b|\.z\b', plain_context)):
            # A package name buried in an apt dependency list is not tool use.
            continue
        if name in {'FPbase API', 'RCSB PDB API'} and not re.search(
                r'\b(?:api|rest|graphql|curl|requests|json|endpoint|programmatically)\b|data\.rcsb\.org|search\.rcsb\.org|/api/', plain_context):
            continue
        if name.startswith('std::') and not ('rust' in q.get('tags', []) or re.search(r'\brust\b|std::|fn main|let mut|impl ', plain_context)):
            continue
        terms = target_terms(theme, target)
        if (theme['strategy'] == 'exact_tag' and theme.get('tag') in q.get('tags', [])
                and target['canonical_name'] != 'MJCF'):
            evidence.append({'target': target, 'matched_in': 'question', 'answer_id': None,
                             'terms': [f'tag:{theme["tag"]}'], 'status': 'tag_candidate_not_semantic_label',
                             'related_topic_only': False})
        for kind, answer_id, body in docs:
            plain = text_of(body).casefold()
            matched = [t for t in terms if re.search(r'(?<![\w])' + re.escape(t.casefold()) + r'(?![\w])', plain)]
            # HTML-language signature can be stripped by HTML cleaning.
            if '<mujoco' in terms and '<mujoco' in html.unescape(body).casefold():
                matched.append('<mujoco')
            if matched:
                evidence.append({'target': target, 'matched_in': kind, 'answer_id': answer_id,
                                 'terms': sorted(set(matched)), 'status': 'keyword_candidate_not_semantic_label',
                                 'related_topic_only': theme['strategy'] == 'related_topic_only'})
    return evidence


def question_key(site, qid):
    return f'{site}:{qid}'


def add_unique(items, additions):
    seen = {json.dumps(i, sort_keys=True, ensure_ascii=False) for i in items}
    for item in additions:
        signature = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if signature not in seen:
            items.append(copy.deepcopy(item))
            seen.add(signature)


def merge_question(records, incoming):
    key = question_key(incoming.get('site', 'stackoverflow'), incoming['question_id'])
    if key not in records:
        records[key] = copy.deepcopy(incoming)
    else:
        old = records[key]
        for field, value in incoming.items():
            if field in {'search_hits', 'coverage_targets', 'matched_targets', 'target_match_evidence'}:
                add_unique(old.setdefault(field, []), value)
            elif field == 'answers':
                by_id = {a['answer_id']: a for a in old.get('answers', [])}
                for answer in value:
                    previous = by_id.get(answer['answer_id'])
                    if not previous or not previous.get('body'):
                        by_id[answer['answer_id']] = copy.deepcopy(answer)
                old['answers'] = list(by_id.values())
            elif field in {'fetch_status', 'answers_complete', 'fetched_at'}:
                if not old.get('answers_complete') or field == 'fetched_at':
                    old[field] = value
            elif field == 'accepted_answer':
                if not old.get(field, {}).get('body'):
                    old[field] = copy.deepcopy(value)
            elif field not in old or old[field] is None:
                old[field] = copy.deepcopy(value)
    records[key]['site'] = incoming.get('site', 'stackoverflow')
    records[key]['question_key'] = key
    return records[key]


def make_themes(tags_file, gaps_file):
    searches = so_scraper.load_searches(tags_file)
    gaps = json.loads(Path(gaps_file).read_text())
    routes = {(d, r['canonical_name']): r['candidate_site_parameters']
              for d, field in [('language', 'languages'), ('tool', 'tools')]
              for r in gaps[field] if r['include_in_default_scraping']}
    themes = []
    for search in searches:
        targets = search['coverage_targets']
        sites = sorted({s for t in targets for s in routes[(t['dimension'], t['canonical_name'])]},
                       key=lambda s: (s != 'stackoverflow', s))
        if search['id'] == 'ncompress':
            sites = list(dict.fromkeys(sites + ['unix', 'superuser']))
        terms = ALIASES.get(search['id'], search.get('required_terms') or [search.get('query') or search['tag']])
        if search['id'] == 'mujoco-mjcf':
            terms = ['mujoco', 'mjcf']
        themes.append(dict(search, sites=sites, terms=terms,
                           discovery_query=search.get('query') or search['tag']))
    return themes


def build_tasks(themes):
    # One first page on every route before any route's second page.
    tasks = []
    max_sites = max(len(t['sites']) for t in themes)
    for index in range(max_sites):
        for theme in themes:
            if index >= len(theme['sites']):
                continue
            site = theme['sites'][index]
            tag = theme['tag'] if site == 'stackoverflow' else ''
            tasks.append({'theme_id': theme['id'], 'site': site, 'tag': tag,
                          'query': theme['query'] if site == 'stackoverflow' else theme['discovery_query'],
                          'page': 1, 'phase': 'primary', 'endpoint': '/search/advanced'})
    return tasks


def task_id(task):
    raw = json.dumps({k: task[k] for k in ['theme_id', 'site', 'tag', 'query', 'page', 'endpoint']}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


class StopCollection(Exception):
    pass


class RequestFailed(Exception):
    pass


class ApiClient:
    def __init__(self, key, proxies, state, max_requests, quota_reserve, deadline, interval=1.0):
        self.key = key
        self.proxies = proxies
        self.state = state
        self.max_requests = max_requests
        self.quota_reserve = quota_reserve
        self.deadline = deadline
        self.interval = interval
        self.last_request = 0
        self.method_ready = {}
        self.cache = {}
        self.session = requests.Session()

    def ensure_budget(self, discovery=False):
        if time.time() >= self.deadline:
            raise StopCollection('time_limit')
        left = self.max_requests - self.state['request_count']
        if left <= (50 if discovery else 0):
            raise StopCollection('request_budget_reserved_for_fetch' if discovery else 'request_budget')
        quota = self.state.get('quota_remaining')
        if quota is not None and quota <= self.quota_reserve + (50 if discovery else 0):
            raise StopCollection('quota_reserved_for_fetch' if discovery else 'quota_limit')

    def get(self, endpoint, site, params=None, discovery=False):
        method = re.sub(r'/[0-9;]+(?=/|$)', '/{ids}', endpoint)
        public = dict(params or {}, site=site)
        signature = json.dumps([endpoint, public], sort_keys=True)
        if signature in self.cache:
            return copy.deepcopy(self.cache[signature])
        for attempt in range(3):
            self.ensure_budget(discovery)
            ready = max(self.last_request + self.interval, self.method_ready.get(method, 0))
            while time.time() < ready:
                time.sleep(min(30, ready - time.time()))
                self.ensure_budget(discovery)
            request_params = dict(public)
            if self.key:
                request_params['key'] = self.key
            self.state['request_count'] += 1
            self.last_request = time.time()
            try:
                response = self.session.get(so_scraper.SO_API_BASE + endpoint, params=request_params,
                                            proxies=self.proxies, timeout=(10, 35))
                if response.status_code in {429, 500, 502, 503, 504}:
                    delay = max(float(response.headers.get('Retry-After', 0) or 0), 2 ** (attempt + 1))
                    self.method_ready[method] = time.time() + delay
                    LOG.warning('Retryable HTTP %s site=%s endpoint=%s attempt=%s', response.status_code, site, endpoint, attempt + 1)
                    continue
                data = response.json()
                if 'quota_remaining' in data:
                    self.state['quota_remaining'] = data['quota_remaining']
                    self.state['quota_max'] = data.get('quota_max')
                if data.get('backoff'):
                    self.method_ready[method] = time.time() + data['backoff']
                if response.status_code != 200 or data.get('error_id'):
                    error = data.get('error_name', 'http_error')
                    # Do not log response URLs or error bodies: either can contain a key.
                    if error == 'throttle_violation':
                        self.method_ready[method] = max(self.method_ready.get(method, 0), time.time() + 60)
                        continue
                    raise RequestFailed(f'HTTP {response.status_code}: {error}')
                self.cache[signature] = data
                return copy.deepcopy(data)
            except RequestFailed:
                raise
            except (requests.RequestException, ValueError) as exc:
                LOG.warning('Request failed site=%s endpoint=%s type=%s attempt=%s', site, endpoint, type(exc).__name__, attempt + 1)
                self.method_ready[method] = time.time() + 2 ** (attempt + 1)
        raise RequestFailed('retry_limit')


class Collector:
    def __init__(self, args, themes, allowed_sites, api_key):
        self.args = args
        self.themes = {t['id']: t for t in themes}
        self.allowed_sites = allowed_sites
        self.run_dir = args.output_root / args.run_id
        self.merged_dir = args.merged_root / args.run_id
        self.state_path = self.run_dir / 'state.json'
        self.baseline = json.loads(args.baseline.read_text())['questions']
        self.baseline_hash = file_hash(args.baseline)
        for q in self.baseline:
            q['site'] = q.get('site', 'stackoverflow')
            q['question_key'] = question_key(q['site'], q['question_id'])
        self.baseline_keys = {q['question_key'] for q in self.baseline}
        assert len(self.baseline) == len(self.baseline_keys) == 1666
        if args.resume and self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
            assert self.state['baseline_sha256'] == self.baseline_hash
            for p, expected in self.state['input_hashes'].items():
                source = Path(p).resolve()
                # Older checkpoints recorded configuration paths under scraper/.
                if not source.exists() and source.is_relative_to(ROOT):
                    source = CONFIG / source.relative_to(ROOT)
                assert file_hash(source) == expected, f'Configuration changed since checkpoint: {p}'
        else:
            assert not self.state_path.exists(), 'Existing run requires --resume'
            inputs = [args.tags_file, args.gaps_file, args.sites_file, args.aliases_file]
            self.state = {'schema_version': 'terminal-lego-multisite-run-v1', 'run_id': args.run_id,
                          'started_at': now(), 'status': 'running', 'baseline_sha256': self.baseline_hash,
                          'input_hashes': {str(p): file_hash(p) for p in inputs},
                          'request_count': 0, 'quota_remaining': None, 'tasks': build_tasks(themes),
                          'completed': {}, 'records': {}, 'fetch_errors': [], 'phase': 'primary',
                          'endpoint_pages': {}, 'pending_questions': {}, 'pending_answers': {},
                          'settings': {'count': args.count, 'max_requests': args.max_requests,
                                       'max_hours': args.max_hours, 'page_size': args.page_size,
                                       'quota_reserve': args.quota_reserve, 'allowed_sites': allowed_sites}}
        self.api = ApiClient(api_key, so_scraper.resolve_http_proxy(args.http_proxy), self.state,
                             args.max_requests, args.quota_reserve,
                             datetime.fromisoformat(self.state['started_at']).timestamp() + args.max_hours * 3600,
                             interval=args.request_interval)
        self.baseline_by_key = {q['question_key']: q for q in self.baseline}

    def checkpoint(self):
        self.state['updated_at'] = now()
        atomic_json(self.state_path, self.state)

    def ingest_question(self, site, q, hit):
        key = question_key(site, q['question_id'])
        item = dict(q, site=site, question_key=key, search_hits=[hit], coverage_targets=[],
                    matched_targets=[], target_match_evidence=[], fetched_at=now())
        if key in self.baseline_by_key:
            b = self.baseline_by_key[key]
            item['accepted_answer'] = copy.deepcopy(b['accepted_answer'])
            item['answers'] = [dict(b['accepted_answer'], is_accepted=True)]
        return merge_question(self.state['records'], item)

    def relevant_count(self, theme_id=None, new_only=False):
        count = 0
        for key, q in self.state['records'].items():
            if new_only and key in self.baseline_keys:
                continue
            if not q.get('eligible_new_qa'):
                continue
            if theme_id and not self.is_theme_candidate(q, theme_id):
                continue
            count += 1
        return count

    def is_theme_candidate(self, q, theme_id):
        return bool(q.get('eligible_new_qa') and any(t in q.get('matched_targets', [])
                                                   for t in self.themes[theme_id]['coverage_targets']))

    def classify(self, q):
        answers = [a for a in q.get('answers', []) if a.get('body')]
        if q.get('accepted_answer', {}).get('body'):
            answers_by_id = {a['answer_id']: a for a in answers}
            answers_by_id.setdefault(q['accepted_answer']['answer_id'], dict(q['accepted_answer'], is_accepted=True))
            answers = list(answers_by_id.values())
        evidence = []
        for theme_id in dict.fromkeys(h['theme_id'] for h in q.get('search_hits', [])):
            evidence.extend(match_evidence(self.themes[theme_id], q, answers))
        q['target_match_evidence'] = evidence
        q['matched_targets'] = []
        add_unique(q['matched_targets'], [e['target'] for e in evidence])
        q['coverage_targets'] = copy.deepcopy(q['matched_targets'])
        q_match = any(e['matched_in'] == 'question' for e in evidence)
        matching_answer_ids = {e['answer_id'] for e in evidence if e['matched_in'] == 'answer'}
        related = [a for a in answers if substantive(a['body']) and not non_english(a['body'])
                   and (q_match or a['answer_id'] in matching_answer_ids)]
        accepted_id = q.get('accepted_answer_id')
        accepted = next((a for a in answers if a['answer_id'] == accepted_id), None)
        if accepted:
            q.setdefault('accepted_answer', copy.deepcopy(accepted))
        selected = sorted(related, key=lambda a: (a['answer_id'] != accepted_id, -a.get('score', 0), a['answer_id']))
        q['selected_answer_id'] = selected[0]['answer_id'] if selected else None
        q['selected_answer'] = copy.deepcopy(selected[0]) if selected else None
        q['relevance_status'] = ('related_topic_candidate' if evidence and all(e['related_topic_only'] for e in evidence)
                                 else 'keyword_candidate_not_semantically_reviewed' if evidence else 'unmatched')
        q['answer_status'] = ('accepted_related' if selected and selected[0]['answer_id'] == accepted_id
                               else 'answered_review' if selected else 'reference_only' if answers
                               else 'unanswered' if q.get('answer_count') == 0 and q.get('body') else 'fetch_pending')
        q['eligible_new_qa'] = bool(selected and q.get('body') and q.get('score', 0) >= 0
                                  and not non_english(q.get('title', '') + q.get('body', '')))
        q['natural_language_status'] = 'english_site_candidate_not_semantically_verified'
        q['review_required'] = True

    def fetch_questions(self, site, ids):
        needed = sorted({qid for qid in ids if not self.state['records'].get(question_key(site, qid), {}).get('body')})
        for start in range(0, len(needed), 50):
            batch = needed[start:start + 50]
            try:
                data = self.api.get('/questions/' + ';'.join(map(str, batch)), site, {'filter': 'withbody', 'pagesize': 100})
                for item in data.get('items', []):
                    key = question_key(site, item['question_id'])
                    existing = self.state['records'][key]
                    # Excerpt title/body are snippets, so replace them with full question fields.
                    existing.update(item)
                    existing['site'], existing['question_key'] = site, key
                    self.state['pending_questions'].pop(key, None)
            except RequestFailed as e:
                for qid in batch:
                    self.state['pending_questions'][question_key(site, qid)] = str(e)
            self.checkpoint()

    def fetch_answers(self, site, ids):
        need = [qid for qid in sorted(set(ids))
                if not self.state['records'][question_key(site, qid)].get('answers_complete')
                and self.state['records'][question_key(site, qid)].get('answer_count', 1) > 0]
        for start in range(0, len(need), 15):
            batch = need[start:start + 15]
            endpoint = '/questions/' + ';'.join(map(str, batch)) + '/answers'
            page = self.state['endpoint_pages'].get(site + endpoint, 1)
            try:
                while True:
                    data = self.api.get(endpoint, site, {'filter': 'withbody', 'pagesize': 100,
                                                       'sort': 'votes', 'order': 'desc', 'page': page})
                    for a in data.get('items', []):
                        key = question_key(site, a['question_id'])
                        q = self.state['records'][key]
                        by_id = {x['answer_id']: x for x in q.get('answers', [])}
                        by_id.setdefault(a['answer_id'], a)
                        q['answers'] = list(by_id.values())
                    self.state['endpoint_pages'][site + endpoint] = page + 1
                    if not data.get('has_more'):
                        for qid in batch:
                            q = self.state['records'][question_key(site, qid)]
                            q['answers_complete'] = True
                            q['fetch_status'] = 'complete'
                            self.state['pending_answers'].pop(q['question_key'], None)
                            self.classify(q)
                        self.checkpoint()
                        break
                    self.checkpoint()
                    page += 1
            except RequestFailed as e:
                for qid in batch:
                    key = question_key(site, qid)
                    self.state['pending_answers'][key] = str(e)
                    self.state['records'][key]['fetch_status'] = 'answer_fetch_failed'
                self.checkpoint()
        for qid in ids:
            q = self.state['records'][question_key(site, qid)]
            if q.get('answer_count') == 0:
                q['answers_complete'] = True
                q['fetch_status'] = 'complete'
            self.classify(q)

    def search(self, task):
        theme, site = self.themes[task['theme_id']], task['site']
        assert site in self.allowed_sites
        self.api.ensure_budget(discovery=True)
        params = {'page': task['page'], 'pagesize': self.args.page_size,
                  'sort': 'relevance', 'order': 'desc', 'filter': 'withbody'}
        if task['query']:
            params['q'] = task['query']
        if task['tag']:
            params['tagged'] = task['tag']
        data = self.api.get(task['endpoint'], site, params, discovery=True)
        ids, raw_items = [], data.get('items', [])
        for item in raw_items:
            qid = item.get('question_id')
            if not qid:
                continue
            hit = dict(task, matched_item_type=item.get('item_type', 'question'), matched_answer_id=item.get('answer_id'))
            if task['endpoint'] == '/search/excerpts':
                # Do not use the excerpt as the question/answer body.
                q = {'question_id': qid, 'excerpt_hits': [dict(item)], 'fetch_status': 'question_fetch_pending'}
            else:
                q = item
            self.ingest_question(site, q, hit)
            ids.append(qid)
        raw_path = self.run_dir / 'responses' / (task_id(task) + '.json')
        atomic_json(raw_path, {'task': task, 'response': data, 'fetched_at': now()})
        self.checkpoint()
        self.fetch_questions(site, ids)
        self.fetch_answers(site, ids)
        for qid in ids:
            self.classify(self.state['records'][question_key(site, qid)])
        relevant = sum(self.is_theme_candidate(self.state['records'][question_key(site, qid)], theme['id']) for qid in set(ids))
        result = {'completed_at': now(), 'raw_items': len(raw_items), 'questions': len(set(ids)),
                  'relevant_answered_candidates': relevant, 'has_more': bool(data.get('has_more')),
                  'task': task, 'status': 'complete'}
        self.state['completed'][task_id(task)] = result
        self.checkpoint()
        LOG.info('phase=%s theme=%s site=%s endpoint=%s page=%s items=%s related_QA=%s new_QA=%s requests=%s quota=%s',
                 task['phase'], theme['id'], site, task['endpoint'], task['page'], len(raw_items), relevant,
                 self.relevant_count(new_only=True), self.state['request_count'], self.state.get('quota_remaining'))

    def append_task(self, task):
        known = {task_id(t) for t in self.state['tasks']}
        if task_id(task) not in known:
            self.state['tasks'].append(task)

    def add_aliases_and_excerpts(self):
        retry_searches = json.loads(self.args.aliases_file.read_text())['tags']
        aliases = defaultdict(list)
        for search in retry_searches:
            for target in search['coverage_targets']:
                for theme in self.themes.values():
                    if target in theme['coverage_targets']:
                        aliases[theme['id']].append(search['query'])
        for tid, theme in self.themes.items():
            # Every main route gets answer-body discovery. Identical queries are
            # cached within the run; accepted status is never used as a filter.
            for site in theme['sites']:
                self.append_task({'theme_id': tid, 'site': site, 'tag': theme['tag'] if site == 'stackoverflow' else '',
                                  'query': theme['query'] if site == 'stackoverflow' else theme['discovery_query'],
                                  'page': 1, 'phase': 'answer_discovery', 'endpoint': '/search/excerpts'})
            if tid in RARE or tid.startswith('rust-') or self.relevant_count(tid) == 0:
                queries = list(dict.fromkeys(aliases[tid] + EXTRA_QUERIES.get(tid, [])))
                for query in queries:
                    for site in theme['sites']:
                        self.append_task({'theme_id': tid, 'site': site, 'tag': 'rust' if tid.startswith('rust-') and site == 'stackoverflow' else '', 'query': query,
                                          'page': 1, 'phase': 'aliases', 'endpoint': '/search/excerpts'})

    def add_expansion(self):
        # Shallow global probes only for themes still missing a related answer.
        missing = [t for t in self.themes.values() if self.relevant_count(t['id']) == 0]
        for site in self.allowed_sites:
            for theme in missing:
                if site in theme['sites']:
                    continue
                self.append_task({'theme_id': theme['id'], 'site': site, 'tag': '',
                                  'query': theme['discovery_query'], 'page': 1,
                                  'phase': 'zero_hit_expansion', 'endpoint': '/search/excerpts'})

    def add_more_pages(self):
        counts = Counter()
        for r in self.state['completed'].values():
            if r.get('status') != 'complete':
                continue
            t = r['task']
            signature = (t['theme_id'], t['site'], t['tag'], t['query'], t['endpoint'])
            counts[signature] = max(counts[signature], t['page'])
        results = sorted(self.state['completed'].values(), key=lambda r: -r.get('relevant_answered_candidates', 0))
        for r in results:
            if r.get('status') != 'complete' or not r.get('has_more'):
                continue
            t = r['task']
            signature = (t['theme_id'], t['site'], t['tag'], t['query'], t['endpoint'])
            if t['page'] != counts[signature] or t['page'] >= 10:
                continue
            previous_zero = any(p.get('status') == 'complete' and p.get('relevant_answered_candidates') == 0
                                and all(p['task'][k] == t[k] for k in ['theme_id', 'site', 'tag', 'query', 'endpoint'])
                                and p['task']['page'] == t['page'] - 1 for p in results)
            if r['relevant_answered_candidates'] == 0 and previous_zero:
                continue
            if r['relevant_answered_candidates'] == 0 and t['page'] == 1:
                # Do not deep crawl broadly matching zero-yield searches.
                continue
            self.append_task(dict(t, page=t['page'] + 1, phase='productive_pagination'))

    def process_queue(self):
        for task in self.state['tasks']:
            if task_id(task) in self.state['completed']:
                continue
            if task['phase'] == 'zero_hit_expansion' and self.relevant_count(task['theme_id']) > 0:
                self.state['completed'][task_id(task)] = {'task': task, 'status': 'skipped_now_answered'}
                continue
            if self.relevant_count(new_only=True) >= self.args.count:
                raise StopCollection('target_count')
            try:
                self.search(task)
            except RequestFailed as e:
                self.state['completed'][task_id(task)] = {'task': task, 'status': 'search_failed', 'error': str(e)}
                LOG.warning('Search failed theme=%s site=%s reason=%s', task['theme_id'], task['site'], e)
                self.checkpoint()

    def retry_pending(self):
        by_site = defaultdict(set)
        for key in set(self.state['pending_questions']) | set(self.state['pending_answers']):
            site, qid = key.rsplit(':', 1)
            by_site[site].add(int(qid))
        for site, ids in by_site.items():
            self.fetch_questions(site, list(ids))
            self.fetch_answers(site, list(ids))

    def run(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.state['status'] = 'running'
        self.checkpoint()
        for source in [self.args.tags_file, self.args.gaps_file, self.args.sites_file, self.args.aliases_file]:
            destination = self.run_dir / 'inputs' / source.name
            destination.parent.mkdir(exist_ok=True)
            if not destination.exists():
                shutil.copy2(source, destination)
        for source in [Path(__file__).resolve(), ROOT / 'so_scraper.py']:
            digest = file_hash(source)
            destination = self.run_dir / 'inputs' / f'{source.stem}.{digest[:12]}.py'
            if not destination.exists():
                shutil.copy2(source, destination)
            add_unique(self.state.setdefault('code_snapshots', []), [{'filename': destination.name, 'sha256': digest}])
        reason = 'planned_searches_completed'
        try:
            for q in self.state['records'].values():
                self.classify(q)
            if self.args.resume:
                by_task = defaultdict(set)
                for key, q in self.state['records'].items():
                    for hit in q.get('search_hits', []):
                        if self.is_theme_candidate(q, hit['theme_id']):
                            by_task[task_id(hit)].add(key)
                for tid, result in self.state['completed'].items():
                    if result['status'] == 'complete':
                        result['relevant_answered_candidates'] = len(by_task[tid])
            # Resume recomputes candidate status from complete preserved bodies.
            # New alias definitions are idempotently added to an existing queue.
            if self.args.resume and self.state['phase'] != 'primary':
                self.add_aliases_and_excerpts()
            self.retry_pending()
            self.process_queue()
            if self.state['phase'] == 'primary':
                self.add_aliases_and_excerpts()
                self.state['phase'] = 'answer_discovery'
                self.checkpoint()
            self.process_queue()
            if self.state['phase'] == 'answer_discovery':
                self.add_expansion()
                self.state['phase'] = 'expansion'
                self.checkpoint()
            self.process_queue()
            self.state['phase'] = 'pagination'
            while True:
                before = len(self.state['tasks'])
                self.add_more_pages()
                if before == len(self.state['tasks']):
                    break
                self.checkpoint()
                self.process_queue()
            self.retry_pending()
        except StopCollection as e:
            reason = str(e)
            LOG.info('Collection stopping: %s', reason)
            try:
                self.retry_pending()
            except StopCollection:
                pass
        except KeyboardInterrupt:
            reason = 'interrupted'
        except Exception:
            reason = 'internal_error'
            LOG.error('Unexpected error; checkpoint and partial merge will be saved (%s)', sys.exc_info()[0].__name__)
            raise
        finally:
            self.state['status'] = 'complete' if reason in {'planned_searches_completed', 'target_count'} else 'partial'
            self.state['stop_reason'] = reason
            self.state['finished_at'] = now()
            self.checkpoint()
            self.export()

    def export(self):
        assert file_hash(self.args.baseline) == self.baseline_hash, 'Baseline changed during run'
        merged = {}
        for baseline in self.baseline:
            q = copy.deepcopy(baseline)
            q['source_batch'] = 'original_1666'
            q['legacy_coverage_targets'] = copy.deepcopy(q.get('coverage_targets', []))
            q['search_hits'] = [{'theme_id': q.get('search_id'), 'site': q['site'], 'phase': 'original_collection'}]
            q['answers'] = [dict(q['accepted_answer'], is_accepted=True)]
            merge_question(merged, q)
        all_raw = list(self.state['records'].values())
        for q in all_raw:
            self.classify(q)
            if q['question_key'] in self.baseline_keys or q.get('eligible_new_qa'):
                merge_question(merged, q)
        # Classify old data against available configured search IDs without treating
        # historical sampling-intent labels as verified task labels.
        for q in merged.values():
            q['search_hits'] = [h for h in q.get('search_hits', []) if h.get('theme_id') in self.themes]
            self.classify(q)
        questions = sorted(merged.values(), key=lambda q: (q['site'], q['question_id']))
        for q in self.baseline:
            merged_q = merged[q['question_key']]
            assert merged_q['body'] == q['body']
            assert merged_q['accepted_answer']['body'] == q['accepted_answer']['body']
        assert self.baseline_keys <= set(merged)
        new_keys = set(merged) - self.baseline_keys
        assert len(questions) == 1666 + len(new_keys)
        if self.state.get('stop_reason') == 'target_count' and len(new_keys) >= self.args.count:
            self.state['status'] = 'complete'
            self.checkpoint()
        unanswered = [q for q in all_raw if q.get('answer_status') == 'unanswered' and q.get('matched_targets')]
        pending = [q for q in all_raw if not q.get('body') or q.get('fetch_status') == 'answer_fetch_failed'
                   or (q.get('answer_count', 0) > 0 and not q.get('answers_complete'))]
        ready = [q for q in questions if q.get('eligible_new_qa') and q['answer_status'] == 'accepted_related'
                 and q.get('accepted_answer', {}).get('body')]
        ready_keys = {q['question_key'] for q in ready}
        review = [q for q in questions if q['question_key'] not in ready_keys]
        targets = []
        for theme in self.themes.values():
            for target in theme['coverage_targets']:
                if target not in targets:
                    targets.append(target)
        target_rows = []
        for target in targets:
            matching = [q for q in questions if target in q.get('matched_targets', [])]
            direct = [q for q in matching if any(e['target'] == target and not e['related_topic_only'] for e in q['target_match_evidence'])]
            old = [q for q in matching if q['question_key'] in self.baseline_keys]
            new = [q for q in matching if q['question_key'] in new_keys]
            missing_answer = [q for q in unanswered if target in q.get('matched_targets', [])]
            target_rows.append(dict(target, original_related_candidates=len(old), added_answered_candidates=len(new),
                                    merged_related_candidates=len(matching), direct_topic_candidates=len(direct),
                                    accepted_related=sum(q['answer_status'] == 'accepted_related' for q in matching),
                                    answered_review=sum(q['answer_status'] == 'answered_review' for q in matching),
                                    unanswered=len(missing_answer),
                                    first_answered_candidate=not old and bool(new),
                                    candidate_sites=sorted({q['site'] for q in matching})))
        fingerprints = defaultdict(list)
        for q in questions:
            normalized = re.sub(r'\s+', ' ', text_of(q['body'])).strip().casefold()
            if len(normalized) >= 80:
                fingerprints[hashlib.sha256(normalized.encode()).hexdigest()].append(q['question_key'])
        duplicate_groups = [v for v in fingerprints.values() if len(v) > 1]
        site_rows = [{'site': site, 'original': sum(q['site'] == site for q in self.baseline),
                      'added_answered_candidates': sum(q['site'] == site and q['question_key'] in new_keys for q in questions),
                      'merged': sum(q['site'] == site for q in questions)}
                     for site in sorted({q['site'] for q in questions})]
        summary = {'schema_version': 'terminal-lego-multisite-merge-v1', 'run_id': self.args.run_id,
                   'created_at': now(), 'status': self.state['status'], 'stop_reason': self.state['stop_reason'],
                   'original_questions': 1666, 'added_answered_candidates': len(new_keys), 'merged_questions': len(questions),
                   'accepted_ready_candidates': len(ready), 'answered_review_candidates': len(review),
                   'unanswered_questions': len(unanswered), 'pending_fetch_records': len(pending),
                   'raw_discovered_questions': len(all_raw), 'raw_answer_status_counts': dict(Counter(q['answer_status'] for q in all_raw)),
                   'requests': self.state['request_count'], 'quota_remaining': self.state.get('quota_remaining'),
                   'baseline_sha256': self.baseline_hash, 'baseline_preserved': True,
                   'target_count': len(targets), 'targets': target_rows, 'sites': site_rows,
                   'first_answered_candidate_targets': [t for t in target_rows if t['first_answered_candidate']],
                   'targets_without_direct_answered_candidates': [t for t in target_rows if t['direct_topic_candidates'] == 0],
                   'search_status_counts': dict(Counter(r['status'] for r in self.state['completed'].values())),
                   'planned_search_pages': len(self.state['tasks']), 'completed_or_skipped_search_pages': len(self.state['completed']),
                   'search_failures': [r for r in self.state['completed'].values() if r['status'] == 'search_failed'],
                   'uncompleted_search_pages': [t for t in self.state['tasks'] if task_id(t) not in self.state['completed']],
                   'suspected_duplicate_groups': duplicate_groups,
                   'limitations': ['Target matches are lexical collection candidates, not semantic training labels or verified task coverage.',
                                   'All original questions are retained, including historical off-target or low-quality records.',
                                   'Unaccepted answers remain review candidates and are never represented as accepted.',
                                   'A zero result is limited to executed queries, available API results and this request budget.',
                                   'English-site selection and script checks do not replace full natural-language review.'],
                   'paths': {'raw_run_directory': str(self.run_dir.resolve()), 'merged_directory': str(self.merged_dir.resolve())}}
        self.merged_dir.mkdir(parents=True, exist_ok=True)
        for name, rows in [('qa_merged.json', questions), ('qa_accepted_ready.json', ready),
                           ('qa_answered_review.json', review), ('questions_unanswered.json', unanswered), ('pending_fetch.json', pending)]:
            atomic_json(self.merged_dir / name, {'metadata': {'total': len(rows), 'run_id': self.args.run_id,
                                                           'with_answers': sum(bool(q.get('answers') or q.get('accepted_answer')) for q in rows)},
                                                 'questions': rows})
        atomic_json(self.run_dir / 'raw_questions.json', {'metadata': {'total': len(all_raw)}, 'questions': all_raw})
        atomic_json(self.merged_dir / 'summary.json', summary)
        with (self.merged_dir / 'qa_merged.preview.jsonl').open('w') as f:
            for q in questions:
                f.write(json.dumps({'site': q['site'], 'question_id': q['question_id'], 'question_key': q['question_key'],
                                    'title': q['title'], 'link': q.get('link'), 'matched_targets': q['matched_targets'],
                                    'answer_status': q['answer_status'], 'body_text': text_of(q['body'])[:1000],
                                    'answer_text': text_of((q.get('selected_answer') or q.get('accepted_answer') or {}).get('body', ''))[:2000]}, ensure_ascii=False) + '\n')
        with (self.merged_dir / 'target_distribution.csv').open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(target_rows[0]))
            writer.writeheader()
            for row in target_rows:
                writer.writerow(dict(row, candidate_sites='; '.join(row['candidate_sites'])))
        lines = [f'# Stack Exchange 补采整合：{self.args.run_id}', '',
                 f'原始 **1666** 条 + 新增 **{len(new_keys)}** 条 = 整合 **{len(questions)}** 条完整问答。', '',
                 f'可按现有采纳答案入口读取的相关候选：{len(ready)}；待审核问答：{len(review)}。',
                 f'另存无答案问题：{len(unanswered)}；待补取：{len(pending)}。',
                 f'状态：{self.state["status"]}；停止原因：{self.state["stop_reason"]}；请求：{self.state["request_count"]}；剩余配额：{self.state.get("quota_remaining")}。', '',
                 '原始文件SHA-256与1666条问题/采纳答案正文均校验通过。仅使用允许的英语Stack Exchange站点，未抓取外链文档或GitHub种子。', '',
                 '| 维度 | 目标 | 原有相关候选 | 本次新增 | 整合相关候选 | 相关采纳答案 | 未采纳答案候选 | 无答案 |',
                 '|---|---|---:|---:|---:|---:|---:|---:|']
        for row in target_rows:
            lines.append(f'| {row["dimension"]} | {row["canonical_name"]} | {row["original_related_candidates"]} | {row["added_answered_candidates"]} | {row["merged_related_candidates"]} | {row["accepted_related"]} | {row["answered_review"]} | {row["unanswered"]} |')
        lines += ['', '匹配是词面相关候选，不是语义打标、有效生成任务数或模型能力覆盖。原有1666条全部保留，未因本轮筛选删除。logic-gate DSL仅检索相关知识，不计作直接语法覆盖。', '',
                  '完整数据：qa_merged.json。预览JSONL截断正文，不能替代完整JSON用于生成。', '',
                  '复现：使用本轮inputs快照与state.json恢复；API密钥不保存在任何输出。']
        (self.merged_dir / 'report.md').write_text('\n'.join(lines) + '\n')
        atomic_json(self.args.merged_root / 'latest.json', {'run_id': self.args.run_id, 'directory': str(self.merged_dir.resolve()),
                                                         'summary': str((self.merged_dir / 'summary.json').resolve()),
                                                         'qa_merged': str((self.merged_dir / 'qa_merged.json').resolve())})
        self.state['last_export_counts'] = {'original': 1666, 'added': len(new_keys), 'merged': len(questions)}
        self.state['last_export_code_sha256'] = file_hash(Path(__file__).resolve())
        snapshot = self.run_dir / 'inputs' / f'collect_coverage_gaps.{self.state["last_export_code_sha256"][:12]}.py'
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        if not snapshot.exists():
            shutil.copy2(Path(__file__).resolve(), snapshot)
        add_unique(self.state.setdefault('code_snapshots', []), [{'filename': snapshot.name, 'sha256': self.state['last_export_code_sha256']}])
        self.checkpoint()
        LOG.info('MERGED original=1666 added=%s total=%s directory=%s', len(new_keys), len(questions), self.merged_dir)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--tags-file', type=Path, default=CONFIG / 'tags.json')
    p.add_argument('--gaps-file', type=Path, default=CONFIG / 'coverage_gaps.json')
    p.add_argument('--sites-file', type=Path, default=CONFIG / 'sites/terminal_lego_site_selection.tb2_181_tools.v3.json')
    p.add_argument('--aliases-file', type=Path, default=CONFIG / 'tags.zero-hit.retry.json')
    p.add_argument('--baseline', type=Path, default=PROJECT / 'data/coverage_gaps/so_data_r1.json')
    p.add_argument('--output-root', type=Path, default=PROJECT / 'data/coverage_gaps_multisite')
    p.add_argument('--merged-root', type=Path, default=PROJECT / 'data/coverage_gaps_merged')
    p.add_argument('--run-id', default=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    p.add_argument('--api-key-file', type=Path)
    p.add_argument('--http-proxy')
    p.add_argument('--count', type=int, default=5000)
    p.add_argument('--max-requests', type=int, default=3000)
    p.add_argument('--quota-reserve', type=int, default=100)
    p.add_argument('--max-hours', type=float, default=6)
    p.add_argument('--page-size', type=int, default=50)
    p.add_argument('--request-interval', type=float, default=1)
    p.add_argument('--resume', action='store_true')
    p.add_argument('--merge-only', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', args.run_id) or args.run_id in {'.', '..'}:
        p.error('run-id must be a simple directory name')
    if args.count < 1 or args.max_requests < 1 or args.max_hours <= 0 or not 1 <= args.page_size <= 100 or args.request_interval < 0:
        p.error('invalid collection limits')
    selection = json.loads(args.sites_file.read_text())
    allowed = selection['default_site_parameters']
    assert len(allowed) == len(set(allowed)) == 47
    themes = make_themes(args.tags_file, args.gaps_file)
    assert all(set(t['sites']) <= set(allowed) for t in themes)
    if args.dry_run:
        print(json.dumps({'themes': len(themes), 'targets': sum(len(t['coverage_targets']) for t in themes),
                          'allowed_sites': len(allowed), 'primary_site_query_pairs': len(build_tasks(themes)),
                          'baseline_questions': len(json.loads(args.baseline.read_text())['questions']),
                          'max_requests': args.max_requests, 'primary_tasks': build_tasks(themes)}, ensure_ascii=False, indent=2))
        return
    api_key = args.api_key_file.read_text().strip() if args.api_key_file else os.environ.get('SO_API_KEY', '')
    run_dir = args.output_root / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.StreamHandler(), logging.FileHandler(run_dir / 'run.log')])
    collector = Collector(args, themes, allowed, api_key)
    if args.merge_only:
        if not args.resume:
            p.error('--merge-only requires --resume')
        collector.export()
    else:
        collector.run()


if __name__ == '__main__':
    main()
