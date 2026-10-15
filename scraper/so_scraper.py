#!/usr/bin/env python3
"""
StackOverflow Question Scraper

Crawls StackOverflow questions with accepted answers for use as seed data
in the Terminal-Lego task generation pipeline.

Usage:
    python so_scraper.py --round 1 --output ./data --count 5000
"""

import json
import requests
import time
import random
import re
import sys
import argparse
import os
import html as html_lib
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlsplit


SO_API_BASE = "https://api.stackexchange.com/2.3"

CONFIG_DIR = Path(__file__).resolve().parents[1] / 'config' / 'scraper'
DEFAULT_TAGS_FILE = CONFIG_DIR / 'tags.json'


def load_searches(tags_file: Path) -> List[Dict[str, Any]]:
    """Load tag and/or keyword searches, retaining per-search collection settings."""
    try:
        config = json.loads(tags_file.read_text(encoding='utf-8'))
    except json.JSONDecodeError as e:
        raise ValueError(
            f'{tags_file}: invalid JSON at line {e.lineno}, column {e.colno}.'
        ) from None

    if not isinstance(config, dict) or not isinstance(config.get('tags'), list):
        raise ValueError(f'{tags_file}: expected an object with a "tags" array.')
    if not config['tags']:
        raise ValueError(f'{tags_file}: "tags" must not be empty.')

    defaults = config.get('defaults', {})
    if not isinstance(defaults, dict):
        raise ValueError(f'{tags_file}: "defaults" must be an object.')

    searches = []
    seen = set()
    seen_ids = set()
    for index, entry in enumerate(config['tags'], 1):
        if not isinstance(entry, dict):
            raise ValueError(f'{tags_file}: entry {index} must be an object.')
        tag = entry.get('tag', '')
        query = entry.get('query')
        weight = entry.get('weight')
        if not isinstance(tag, str):
            raise ValueError(f'{tags_file}: entry {index} needs a string "tag".')
        tag = tag.strip()
        if query is not None:
            if not isinstance(query, str):
                raise ValueError(f'{tags_file}: entry {index} "query" must be a string.')
            query = query.strip()
        if not tag and not query:
            raise ValueError(f'{tags_file}: entry {index} needs a tag or a non-empty query.')
        if type(weight) is not int or weight <= 0:
            raise ValueError(f'{tags_file}: entry {index} needs a positive integer "weight".')
        signature = (tag, query or '')
        if signature in seen:
            raise ValueError(f'{tags_file}: duplicate search for tag {tag!r}, query {query!r}.')
        seen.add(signature)
        search_id = entry.get('id', tag if not query else f'{tag}:{query}')
        if not isinstance(search_id, str) or not search_id.strip() or search_id in seen_ids:
            raise ValueError(f'{tags_file}: entry {index} needs a unique non-empty search id.')
        seen_ids.add(search_id)
        search = dict(defaults)
        search.update(entry)
        search.update(tag=tag, weight=weight, query=query, id=search_id)
        for field in ('start_page', 'max_pages'):
            if field in search and (type(search[field]) is not int or search[field] <= 0):
                raise ValueError(f'{tags_file}: entry {index} "{field}" must be a positive integer.')
        if 'min_score' in search and type(search['min_score']) is not int:
            raise ValueError(f'{tags_file}: entry {index} "min_score" must be an integer.')
        if 'sort' in search and search['sort'] not in ('votes', 'activity', 'creation'):
            raise ValueError(f'{tags_file}: entry {index} "sort" must be votes, activity, or creation.')
        terms = search.get('required_terms', [])
        if not isinstance(terms, list) or any(not isinstance(term, str) or not term.strip() for term in terms):
            raise ValueError(f'{tags_file}: entry {index} "required_terms" must be a list of non-empty strings.')
        searches.append(search)

    return searches


def load_tags(tags_file: Path) -> List[Tuple[str, int]]:
    """Compatibility view for callers interested only in tag/weight pairs."""
    return [(search['tag'], search['weight']) for search in load_searches(tags_file)]


def resolve_http_proxy(http_proxy: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Resolve a stage-one HTTP proxy without changing process-wide settings."""
    proxy_url = http_proxy or next(
        (os.environ[name] for name in (
            'SO_HTTP_PROXY', 'https_proxy', 'HTTPS_PROXY', 'http_proxy', 'HTTP_PROXY'
        ) if os.environ.get(name)),
        None,
    )
    if not proxy_url:
        return None

    proxy_url = proxy_url.strip()
    error_message = 'HTTP proxy must be http://HOST[:PORT] or https://HOST[:PORT] with a valid port.'
    try:
        proxy = urlsplit(proxy_url)
        valid = (
            proxy.scheme in ('http', 'https')
            and bool(proxy.hostname)
            and proxy.port != 0
            and proxy.path in ('', '/')
            and not proxy.query
            and not proxy.fragment
            and not any(char.isspace() for char in proxy_url)
        )
    except ValueError:
        raise ValueError(error_message) from None
    if not valid:
        raise ValueError(error_message)

    return {'http': proxy_url, 'https': proxy_url}


def proxy_endpoint(proxies: Dict[str, str]) -> str:
    """Describe the configured proxy without exposing authentication details."""
    proxy = urlsplit(proxies['https'])
    host = proxy.hostname
    if ':' in host:
        host = f'[{host}]'
    port = proxy.port or (443 if proxy.scheme == 'https' else 80)
    return f'{proxy.scheme}://{host}:{port}'


def request_error_message(error: Exception, proxies: Optional[Dict[str, str]]) -> str:
    # Request exception text can contain the full URL, including the API key.
    message = type(error).__name__
    if isinstance(error, requests.exceptions.HTTPError) and error.response is not None:
        message += f' (HTTP {error.response.status_code})'
    if proxies:
        message += f' (proxy={proxy_endpoint(proxies)})'
    return message


def get_questions_by_tag(
    tag: str,
    api_key: str,
    min_score: int = 10,
    page_size: int = 100,
    max_pages: int = 20,
    sort: str = 'activity',
    start_page: int = 1,
    exclude_ids: set = None,
    proxies: Optional[Dict[str, str]] = None,
    query: Optional[str] = None,
    required_terms: Optional[List[str]] = None,
    max_questions: Optional[int] = None,
    site: str = 'stackoverflow',
    accepted_only: bool = True,
) -> tuple:
    questions = []
    should_stop = False
    if exclude_ids is None:
        exclude_ids = set()
    collected_ids = set(exclude_ids)

    for page in range(start_page, start_page + max_pages):
        params = {
            'order': 'desc',
            'sort': sort,
            'site': site,
            'filter': 'withbody',
            'pagesize': page_size,
            'page': page,
            'key': api_key,
        }
        if tag:
            params['tagged'] = tag
        # API min is a score only for sort=votes; it is a timestamp otherwise.
        if sort == 'votes':
            params['min'] = min_score
        endpoint = '/questions'
        if query is not None:
            endpoint = '/search/advanced'
            if accepted_only:
                params['accepted'] = 'true'
            if query:
                params['q'] = query

        try:
            response = requests.get(
                f"{SO_API_BASE}{endpoint}",
                params=params,
                timeout=30,
                proxies=proxies,
            )

            if response.status_code == 429:
                print(f"  Rate limited, waiting 60s...")
                time.sleep(60)
                continue

            if response.status_code == 400:
                print(f"  API rejected search (tag={tag!r}, query={query!r}), skipping")
                return questions, False

            response.raise_for_status()
            data = response.json()

            if 'backoff' in data:
                print(f"  Backoff {data['backoff']}s...")
                time.sleep(data['backoff'])

            if 'items' not in data:
                break

            for item in data['items']:
                if accepted_only and not item.get('accepted_answer_id'):
                    continue
                if item.get('score', 0) < min_score:
                    continue
                if item['question_id'] in collected_ids:
                    continue
                if required_terms:
                    searchable = html_lib.unescape(
                        item.get('title', '') + '\n' + item.get('body', '')
                    ).casefold()
                    if not any(term.casefold() in searchable for term in required_terms):
                        continue

                questions.append({
                    'question_id': item['question_id'],
                    'title': item['title'],
                    'link': item['link'],
                    'score': item['score'],
                    'view_count': item.get('view_count', 0),
                    'answer_count': item.get('answer_count', 0),
                    'accepted_answer_id': item.get('accepted_answer_id'),
                    'tags': item.get('tags', []),
                    'body': item.get('body', ''),
                    'creation_date': item.get('creation_date'),
                })
                collected_ids.add(item['question_id'])
                if max_questions is not None and len(questions) >= max_questions:
                    break

            quota = data.get('quota_remaining', 0)
            print(f"    page {page}: +{len(data['items'])} items, quota={quota}")

            if quota < 100:
                print("  Quota low, stopping")
                return questions, True

            if max_questions is not None and len(questions) >= max_questions:
                break

            if not data.get('has_more', False):
                break

        except Exception as e:
            print(f"  Error: {request_error_message(e, proxies)}")
            break

        time.sleep(0.5)

    return questions, should_stop


def get_answers_batch(
    answer_ids: List[int],
    api_key: str,
    proxies: Optional[Dict[str, str]] = None,
    site: str = 'stackoverflow',
) -> Dict[int, Dict]:
    """Fetch answers in batch (max 30 per request)"""
    results = {}
    ids_str = ';'.join(str(aid) for aid in answer_ids)
    params = {
        'site': site,
        'filter': 'withbody',
        'key': api_key,
    }

    try:
        response = requests.get(
            f"{SO_API_BASE}/answers/{ids_str}",
            params=params,
            timeout=30,
            proxies=proxies,
        )
        if response.status_code == 200:
            data = response.json()
            if 'backoff' in data:
                time.sleep(data['backoff'])
            for item in data.get('items', []):
                results[item['answer_id']] = {
                    'answer_id': item['answer_id'],
                    'body': item.get('body', ''),
                    'score': item.get('score', 0),
                }
    except Exception as e:
        print(f"  Failed to fetch answers batch: {request_error_message(e, proxies)}")

    return results


def clean_html(html_text: str) -> str:
    clean = re.sub(r'<[^>]+>', '', html_text)
    clean = clean.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
    clean = clean.replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
    return clean.strip()


def save_data(questions, output_path, jsonl_path):
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump({
            'metadata': {
                'total': len(questions),
                'with_answers': sum(1 for q in questions if q.get('accepted_answer')),
                'updated': time.strftime('%Y-%m-%d %H:%M:%S'),
            },
            'questions': questions
        }, f, ensure_ascii=False, indent=2)

    with open(jsonl_path, 'w', encoding='utf-8') as f:
        for q in questions:
            item = {
                'question_id': q['question_id'],
                'title': q['title'],
                'link': q['link'],
                'score': q['score'],
                'tags': q['tags'],
                'search_tag': q.get('search_tag', ''),
                'search_id': q.get('search_id', ''),
                'search_query': q.get('search_query', ''),
                'coverage_targets': q.get('coverage_targets', []),
                'body_text': clean_html(q.get('body', ''))[:1000],
                'answer_text': clean_html(q.get('accepted_answer', {}).get('body', ''))[:2000] if q.get('accepted_answer') else '',
            }
            f.write(json.dumps(item, ensure_ascii=False) + '\n')

    print(f"Saved {len(questions)} questions to {output_path}")


def load_existing_ids(dedup_dir: Path) -> set:
    """Load question IDs from all JSON files in dedup_dir for deduplication"""
    exclude_ids = set()
    if not dedup_dir or not dedup_dir.exists():
        return exclude_ids

    for f in sorted(dedup_dir.glob("*.json")):
        try:
            with open(f, 'r') as fh:
                old_data = json.load(fh)
            old_qs = old_data.get('questions', [])
            exclude_ids.update(q['question_id'] for q in old_qs)
            print(f"Dedup loaded: {f.name} ({len(old_qs)} questions)")
        except Exception as e:
            print(f"Skip: {f}: {e}")

    return exclude_ids


def main():
    parser = argparse.ArgumentParser(description='StackOverflow Question Scraper')
    parser.add_argument('--round', '-r', type=int, default=1, help='Round number')
    parser.add_argument('--output', '-o', type=str, default='./data', help='Output directory')
    parser.add_argument('--count', '-c', type=int, default=5000, help='Target question count')
    parser.add_argument('--dedup-dir', type=str, default=None, help='Directory with existing JSON files for deduplication')
    parser.add_argument('--api-key', type=str, default=None, help='StackOverflow API key')
    parser.add_argument(
        '--tags-file', type=Path, default=DEFAULT_TAGS_FILE,
        help='Tag/weight JSON configuration (default: config/scraper/tags.json)',
    )
    parser.add_argument(
        '--http-proxy', type=str, default=None,
        help='HTTP proxy URL (defaults to SO_HTTP_PROXY, then HTTPS/HTTP proxy environment variables)',
    )
    parser.add_argument('--dry-run', action='store_true', help='Validate and display the collection plan without network requests or output files')

    args = parser.parse_args()

    try:
        proxies = resolve_http_proxy(args.http_proxy)
        searches = load_searches(args.tags_file)
    except (OSError, ValueError) as e:
        parser.error(str(e))

    if args.count <= 0:
        parser.error('--count must be positive.')
    if args.dry_run:
        total_weight = sum(search['weight'] for search in searches)
        print(json.dumps({
            'tags_file': str(args.tags_file.resolve()),
            'target_count': args.count,
            'searches': [dict(search, relative_target=max(1, int(search['weight'] * args.count / total_weight))) for search in searches],
        }, ensure_ascii=False, indent=2))
        return

    api_key = args.api_key or os.environ.get('SO_API_KEY', '')
    if not api_key:
        print("Warning: No SO API key provided. Rate limits will be strict.")
        print("Get one at https://stackapps.com/ and pass via --api-key or SO_API_KEY env var.")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    output_json = output_dir / f"so_data_r{args.round}.json"
    output_jsonl = output_dir / f"so_data_r{args.round}.jsonl"

    random.shuffle(searches)

    dedup_dir = Path(args.dedup_dir) if args.dedup_dir else output_dir
    exclude_ids = load_existing_ids(dedup_dir)

    all_questions = []
    seen_ids = set(exclude_ids)
    target_count = args.count

    print("=" * 60)
    print(f"Round {args.round}: Crawling {target_count} StackOverflow questions")
    print(f"Excluding {len(exclude_ids)} existing IDs")
    print(f"Searches: {len(searches)} (shuffled)")
    print(f"Tags file: {args.tags_file.resolve()}")
    print(f"Output: {output_json}")
    if proxies:
        print(f"HTTP proxy: {proxy_endpoint(proxies)}")
    print("=" * 60)

    total_weight = sum(search['weight'] for search in searches)

    for i, search in enumerate(searches, 1):
        tag, weight = search['tag'], search['weight']
        if len(all_questions) >= target_count:
            print(f"\nReached target {target_count}!")
            break

        remaining = target_count - len(all_questions)
        tag_target = min(max(1, int(weight * target_count / total_weight)), remaining)

        print(f"\n[{i}/{len(searches)}] Search: {search['id']} (tag={tag!r}, query={search['query']!r}, target: ~{tag_target}, total: {len(all_questions)})")

        sort_method = search.get('sort', random.choice(['activity', 'votes', 'creation']))
        start_page = search.get('start_page', random.randint(1, 5))

        if tag_target > 500:
            min_score, max_pages = 15, 25
        elif tag_target > 200:
            min_score, max_pages = 20, 15
        else:
            min_score, max_pages = 30, 10
        min_score = search.get('min_score', min_score)
        max_pages = search.get('max_pages', max_pages)

        questions, should_stop = get_questions_by_tag(
            tag=tag,
            api_key=api_key,
            min_score=min_score,
            max_pages=max_pages,
            sort=sort_method,
            start_page=start_page,
            exclude_ids=seen_ids,
            proxies=proxies,
            query=search['query'],
            required_terms=search.get('required_terms'),
            max_questions=tag_target,
        )

        new_count = 0
        for q in questions:
            if q['question_id'] not in seen_ids:
                seen_ids.add(q['question_id'])
                q['search_tag'] = tag
                q['search_id'] = search['id']
                q['search_query'] = search['query'] or ''
                q['coverage_targets'] = search.get('coverage_targets', [])
                all_questions.append(q)
                new_count += 1
                if len(all_questions) >= target_count:
                    break

        print(f"  Found {len(questions)}, added {new_count} (sort={sort_method}, page={start_page})")

        if len(all_questions) % 1000 == 0 and len(all_questions) > 0:
            save_data(all_questions, str(output_json), str(output_jsonl))

        if should_stop:
            print("\nQuota low, saving and exiting")
            break

        time.sleep(1)

    # Batch fetch accepted answers
    print(f"\nFetching accepted answers for {len(all_questions)} questions...")

    need_answers = [(i, q['accepted_answer_id']) for i, q in enumerate(all_questions)
                    if q.get('accepted_answer_id') and not q.get('accepted_answer')]

    batch_size = 30
    fetched = 0
    for batch_start in range(0, len(need_answers), batch_size):
        batch = need_answers[batch_start:batch_start + batch_size]
        answer_ids = [aid for _, aid in batch]

        results = get_answers_batch(answer_ids, api_key, proxies=proxies)

        for idx, aid in batch:
            if aid in results:
                all_questions[idx]['accepted_answer'] = results[aid]
                fetched += 1

        if (batch_start // batch_size) % 10 == 0:
            print(f"  Processed {batch_start + len(batch)}/{len(need_answers)}, fetched {fetched}")

        if (batch_start // batch_size) % 50 == 0 and batch_start > 0:
            save_data(all_questions, str(output_json), str(output_jsonl))

        time.sleep(0.5)

    save_data(all_questions, str(output_json), str(output_jsonl))

    with_answers = sum(1 for q in all_questions if q.get('accepted_answer'))
    print(f"\nDone! Crawled {len(all_questions)} questions ({with_answers} with answers)")
    print(f"Saved to: {output_json}")


if __name__ == "__main__":
    main()
