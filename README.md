<h1 align="center"> Terminal-Lego: What Makes Interaction Trajectories Effective for Training Terminal Agents? </h1>

<p align="center">
<a href="https://huggingface.co/datasets/StephYang/Terminal-Lego-15k" > 🤗 Instances </a>
•
<a href="https://huggingface.co/datasets/SWE-Lego/Terminal-Lego-Traj-Deepseek-V3-2-15k" > 🤗 Trajectories </a>
•
<a href="https://huggingface.co/StephYang/Terminal-Lego-Qwen3-8B" > 🤗 Terminal-Lego-Qwen3-8B/32B</a>
•
<a href="https://github.com/SWE-Lego/terminal-lego" > 🧑‍💻 Code</a>
•
<a href="https://arxiv.org/pdf/2606.03461v1" > 📖 Paper</a>
</p>
<p align="center">
<a href="https://stephen0808.github.io/terminal-lego.github.io/#" > 📖 Project</a>
</p>

## 1. 🔭 Pipeline Overview

 <p align="center">
    <img src="./pipeline.jpg" width="600">
  </p>

## 2. 🚀 Quick Start

### 2.1 Install dependencies

```bash
pip install -r requirements.txt
```

### 2.2 Configure

```bash
cp .env.example .env
# Edit .env with your API keys
```

Required environment variables:
- `OPENAI_API_KEY` — API key for an OpenAI-compatible LLM service
- `OPENAI_API_BASE` — API base URL (default: `https://api.openai.com/v1`)
- `SO_API_KEY` — StackOverflow API key ([get one here](https://stackapps.com/))
- `MODEL_NAME` — Model to use (default: `claude-opus-4-6`)

Optional environment variable:
- `SO_HTTP_PROXY` — HTTP proxy for StackOverflow scraping, such as `http://10.200.50.67:7897`; only the scraper reads this variable.

### 2.3 Run the full pipeline

```bash
bash run_pipeline.sh
```

This runs in a loop: scrape → generate → validate → next round.

### 2.4 Run tests

The offline regression tests live in `test/` and use Python's standard-library
`unittest`. Run them from the repository root:

```bash
python -m unittest discover -s test -p 'test_*.py' -v
```

API calls are mocked, and test outputs are written to temporary directories.
The multisite tests require the local 1666-question baseline at
`data/coverage_gaps/so_data_r1.json`.

## 3. 🔧 Running Steps Individually

### 3.1 Scrape StackOverflow

The legacy `so_scraper.py` command collects **Stack Overflow questions with
accepted answers**. The coverage-gap collector below additionally searches the
47 allowed English Stack Exchange communities, including unaccepted answers.
Official documentation, tutorials, GitHub issues/PRs and sites outside that
allowlist are outside the seed collection scope. Links inside source answers
are preserved; the collector does not follow them to gather external seeds.

The standalone scraper does not load `.env` automatically. Export `SO_API_KEY`
in the current shell, or load your edited `.env` before running it:

```bash
set -a
source .env
set +a
```

```bash
python scraper/so_scraper.py \
    --round 1 \
    --output ./data \
    --count 5000 \
    --http-proxy http://10.200.50.67:7897
```

The scraper reads the API key from `SO_API_KEY`; `--api-key` is still supported.
Proxy precedence is `--http-proxy` → `SO_HTTP_PROXY` → `https_proxy` →
`HTTPS_PROXY` → `http_proxy` → `HTTP_PROXY`. With `SO_HTTP_PROXY` exported, you
can omit `--http-proxy`. Both question and answer requests use the selected HTTP
proxy, including HTTPS requests to the API. Proxy URLs must use `http://` or
`https://`; these specify how to connect to the proxy, while the API remains HTTPS.
If no HTTP proxy is selected, Requests retains its normal environment-based behavior.

#### Tag configuration

Collection scripts live in `scraper/`; their configuration and site-reference
files live in `config/scraper/`. Collected data stays in `data/`, and regression
tests live in `test/`.

| Location under `config/scraper/` | Purpose | Required for default collection? |
|---|---|---|
| `tags.json` | Combined search configuration | Yes, for both scrapers |
| `coverage_gaps.json`, `tags.zero-hit.retry.json`, `sites/terminal_lego_site_selection.tb2_181_tools.v3.json` | Target routes, aliases and the allowed-site selection | Yes, for the multisite collector |
| `tags.languages.json`, `tags.tools.json` | Language-only or tool-only searches | Optional profiles |
| Other files in `sites/` | Site catalog, review exports and derived subsets | Reference material |
| `archive/` | Original tag backup and v1/v2 site reviews | Historical comparison and legacy-format tests |

The files were moved without changing their contents. Paths embedded in JSON
metadata record where evidence lived when it was produced; they are provenance,
not runtime configuration. Historical scripts under `../../analysis_outputs/`
may need their recorded paths updated before being rerun.

The scraper loads its tag configuration from `config/scraper/tags.json` by default,
regardless of the current working directory. Edit this file to add or adjust tags:

```json
{
  "tags": [
    {"tag": "linux", "weight": 800},
    {"tag": "python", "weight": 800},
    {"tag": "docker", "weight": 400}
  ]
}
```

`tag` is a tag name and `weight` is a positive integer representing relative
collection weight. A search can also provide `query` for a free-text search.
An empty `tag` is allowed when `query` is non-empty, so a tool without its own
Stack Overflow tag can be searched by name. Tag/query pairs and search IDs must
be unique. The original tag/weight-only format remains supported.

To select a different configuration, add `--tags-file /path/to/custom_tags.json`
to the scraper command. Relative paths are resolved from the current working
directory. Invalid or missing configurations fail before collection starts.

The current default configuration uses the final filtered
`outputs/training/20261010` training data: **53,543 trajectories / 13,638 unique
tasks**, compared with the latest **30-language / 181-tool** TB inventory.
It targets **7 languages and 37 tools**, merged into **41 searches**. The
`metadata/defaults/tags` structure remains unchanged. The original 98-tag
configuration is preserved in `config/scraper/archive/tags.backup.20261012.json`; the preceding
gap configurations are backed up in
`../../analysis_outputs/training-20261010-coverage-refresh-181tools-20261013/before_update/`.

- `config/scraper/coverage_gaps.json`: all 7 language and 62 published tool-label gaps,
  their benchmark tasks, candidate site routes, and search mappings. Of these
  tools, 25 are excluded from default collection after actual-use review.
- `config/scraper/tags.languages.json`: searches for the 7 language targets.
- `config/scraper/tags.tools.json`: searches for the 37 tool targets.
- `config/scraper/tags.json`: their combined default configuration.
- `config/scraper/tags.zero-hit.retry.json`: 17 SO searches for aliases of the 7 tool
  targets whose dedicated search groups were empty; question IDs should be
  deduplicated against the existing `data/coverage_gaps` collection.

Published task labels cover **23/30 languages** and **119/181 tools**. Their
tool vocabulary is frozen at 154 entries, so the 27 newly added tools cannot be
identified by those labels. Direct review of kept training trajectories confirms
actual use of **24 of the 27 new tools**, and re-confirms the historical PyMuPDF
exception. The union of published labels and these confirmed uses covers at
least **144/181 tools (79.56%)**; it is a lower bound, not a complete relabeling or
a measure of sample adequacy. Candidate keyword counts are not usage frequencies.
The remaining new collection targets are **mtools, ncompress, and vncsnapshot**.
The older gaps were not exhaustively audited for actual use, so default targets
remain collection candidates rather than proof of absolute absence.

The new training labels cover the Stan language (1 task / 1 trajectory) and
POV-Ray tool (1 task / 2 trajectories), which removes them from their respective
target dimensions. The Stan tool and POV-Ray SDL language still lack published
training labels. Details and replayable evidence are saved in
`../../analysis_outputs/training-20261010-coverage-refresh-181tools-20261013/report.md`.
The 47-site routes in `coverage_gaps.json` describe candidate sources. Use
`collect_coverage_gaps.py` for multisite collection; the legacy scraper remains
SO-only by default.

For niche topics, the profile starts on page 1, sorts by votes, accepts scores
of 0 or above, and requires an accepted answer. This avoids skipping the only
page of a small tag. Configured keyword searches use the Stack Exchange
[`/search/advanced` API](https://api.stackexchange.com/docs/advanced-search).
`required_terms`, when present, keeps only questions whose title or body contains
at least one of those case-insensitive terms. Search targets are saved alongside
the source question; they are sampling intent, not inferred task labels.

For example, both Rust module gaps use the existing `rust` tag with different
keyword searches:

```json
{
  "defaults": {"min_score": 0, "sort": "votes", "start_page": 1, "max_pages": 10},
  "tags": [
    {"id": "rust-collections", "tag": "rust", "query": "std::collections", "weight": 150},
    {"id": "rust-convert", "tag": "rust", "query": "std::convert", "weight": 150}
  ]
}
```

The same settings can override `defaults` on individual entries. The scraper
limits each search to its relative question budget, so a large tag cannot fill
the whole round by itself. Sparse topics may yield fewer questions than requested.
`logic-gate DSL` is a benchmark-specific language; its configuration searches
related logic-gate questions and does not claim direct coverage of that syntax.

Validate the collection plan without API requests or writing scraped data:

```bash
python scraper/so_scraper.py --count 5000 --dry-run
python scraper/so_scraper.py --tags-file config/scraper/tags.languages.json --dry-run
python scraper/so_scraper.py --tags-file config/scraper/tags.tools.json --dry-run
```

#### Stack Exchange site catalog

The API snapshot dated 2026-10-13 is saved in
`config/scraper/sites/stackexchange_main_sites.json`. Its `sites` array contains all
184 records marked `main_site` by the API. `metadata.catalog_api` stores the
GET endpoint, request parameters, documentation URL, and daily cache interval.

The latest target-based review is
`config/scraper/sites/terminal_lego_site_selection.tb2_181_tools.v3.json`, based on the
TB inventory updated on 2026-10-13: 30 languages and 181 tools. All 101 previously
retained sites were individually reassessed under the stricter source-value
policy: 47 remain `keep`, and 54 become `exclude`. The earlier 83 exclusions
were checked for relevance to the nine added tools and remain excluded.
The complete 184-site selection is 47 `keep` and 137 `exclude`, with no
third decision category. Every site includes a reason and collection condition;
kept sites also list candidate language/tool targets. These mappings are search
routes, not measured question coverage. The review weighs direct target-tool
workflows and affinity to actual TB tasks; merely mentioning Python/Rust/NumPy
or providing an isolated snippet does not justify enabling an entire community.

The same review is available as
`terminal_lego_site_selection.tb2_181_tools.v3.csv` and a readable `.md` report.
Separate subsets are `terminal_lego_kept_sites.tb2_181_tools.v3.json` and
`terminal_lego_excluded_sites.tb2_181_tools.v3.json`.
`tb2_target_site_routes.181_tools.v3.json` maps each of the 30 languages and 181 tools to
candidate sites. The earlier generic review (`terminal_lego_site_selection.json`,
54 `keep`, 49 `conditional`, 81 `exclude`) and the 172-tool v2 selection
(101 `keep`, 83 `exclude`) are preserved in `config/scraper/archive/sites/`
for comparison.

Read the default site identifiers directly from the review file:

```python
import json
from pathlib import Path

review = json.loads(Path("config/scraper/sites/terminal_lego_site_selection.tb2_181_tools.v3.json").read_text())
site_parameters = review["default_site_parameters"]  # 47 sites
```

The v3 review uses official site descriptions, actual TB task affinities, the
earlier evidence, and 36 bounded searches across 28 sites; individual questions still need
target relevance and executability checks. Collection considers
English-language questions and answers only, as recorded in
`metadata.language_policy`; this does not restrict programming languages.
Natural-language learning sites and the four localized Stack Overflow sites
(`pt.stackoverflow`, `ja.stackoverflow`, `ru.stackoverflow`, `es.stackoverflow`)
are excluded. `langdev` is retained for language implementation. Communities
whose overlap depends primarily on generic code, non-target platforms, subject
references, or isolated snippets are excluded from this collection round,
including the blockchain communities, CMS/enterprise platforms, `linguistics`,
and the nine marginal communities added by the earlier v2 review. Their known
examples are preserved in the audit; exclusion does not assert that every post
is unusable. Specialist sources with concrete target workflows, such as
MuJoCo on Robotics, Biopython on Biology, PUG REST on Chemistry, and pytest on SQA,
remain enabled within the documented collection scopes. Some review examples have target code only in
unaccepted answers; those enter the multisite review pool and do not become
accepted answers for the existing generator.
The API also marks two Meta-discussion sites as `main_site`; their full directory
records are preserved and the scope review excludes them.

The multisite collector passes each selected site to question and answer API
requests and deduplicates by `(site, question_id)`. Its initial 41 themes use
105 related-site routes, including the additional ncompress routes on Unix and
Super User. Unresolved themes get bounded searches on the remaining allowed
sites. Search matches are collection candidates, not semantic task labels.

#### Resumable multisite coverage-gap collection

Validate the plan without network requests or output writes:

```bash
python scraper/collect_coverage_gaps.py --dry-run
```

Run with `SO_API_KEY` exported, or use a private credential file. The key is
never included in logs, checkpoints, request snapshots or merged metadata:

```bash
python scraper/collect_coverage_gaps.py \
    --run-id gaps-20261014 \
    --api-key-file /path/to/private-key-file \
    --http-proxy http://10.200.50.67:7897 \
    --count 5000 --max-requests 3000 --quota-reserve 100 --max-hours 6
```

Stages are related-site first-page probing, answer-body discovery and aliases,
shallow expansion for zero-answer themes, then productive pagination. Answer
and question bodies are fetched in full; answers are grouped by source site.
Reference-only answers, unanswered questions and fetch failures are separate
from related answered candidates. API backoff and quota limits are honored.

Each search page and answer batch writes an atomic checkpoint under
`data/coverage_gaps_multisite/<run-id>/`. Resume with the same inputs and
`--run-id`, adding `--resume`. `--resume --merge-only` rebuilds local outputs
without API requests. Changing the collection inputs after a checkpoint requires
a new run. A timed-out or quota-limited run still exports its partial results.
Checkpoints created before the configuration move can still resume: missing
input paths under `scraper/` resolve to `config/scraper/`, and the original
SHA-256 checks still reject changed configuration contents.

Merged outputs are in `data/coverage_gaps_merged/<run-id>/`, with a pointer in
`data/coverage_gaps_merged/latest.json`:

- `qa_merged.json`: all original 1666 questions, plus distinct new related
  candidates with complete answer bodies; original question and accepted-answer
  bodies are checked byte-for-byte after merging.
- `qa_accepted_ready.json`: related candidates compatible with the existing
  generator's accepted-answer entry gate. Relevance still needs semantic review.
- `qa_answered_review.json`: remaining merged records, including unaccepted
  answers and historical records that do not pass this run's lexical screening.
- `questions_unanswered.json` and `pending_fetch.json`: separate archives.
- `summary.json`, `target_distribution.csv`, `report.md`: counts per target/site,
  answer categories, requests, stopping reason and unfinished searches.

Question counts obey `merged = 1666 + new distinct answered candidates`.
Unanswered or failed downloads do not count as added Q&A. `qa_merged.preview.jsonl`
is truncated for inspection; use the complete JSON files for further processing.
The collector does not run task generation, a paid LLM, Docker validation or training.

### 3.2 Generate Tasks

```bash
python generator/task_generator.py \
    --input ./data/so_data_r1.json \
    --output ./data/candidates_r1 \
    --workers 16 \
    --model claude-opus-4-6
```

### 3.3 Validate Tasks (requires Docker)

```bash
python validator/validate_tasks.py \
    --input ./data/candidates_r1 \
    --output ./data/validated_r1 \
    --workers 8 \
    --timeout 300
```

## 4. 📦 Output Format

Each generated task has this structure:

```
task_00001/
├── instruction.md          # Task description
├── task.toml               # Metadata (difficulty, category, tags)
├── environment/
│   ├── Dockerfile          # Container setup
│   └── task_file/          # Input files
├── solution/
│   └── solve.sh            # Reference solution
└── tests/
    ├── test.sh             # Test runner
    └── test_outputs.py     # pytest assertions
```

## 5. ⚙️ Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CRAWL_COUNT` | 5000 | Questions to crawl per round |
| `GEN_WORKERS` | 16 | Parallel threads for generation |
| `VAL_WORKERS` | 8 | Parallel threads for validation |
| `VAL_TIMEOUT` | 300 | Docker timeout per step (seconds) |
| `MODEL_NAME` | claude-opus-4-6 | LLM model name |

## 6. 📋 Requirements

- Python 3.10+
- Docker (for validation step)
- An OpenAI-compatible API endpoint
- StackOverflow API key (optional, increases rate limits)

## 7. 📄 License

MIT
