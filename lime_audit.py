#!/usr/bin/env python3
"""Comprehensive bug-check on a live LimeSurvey survey via RC2.

Catches LimeSurvey-import gotchas: bad code formats, dangling references in
relevance/validation equations, missing CSS classes that the reset-on-change
JS depends on, mandatory-flag mismatches, answer-count mismatches.

Usage:
    python lime_audit.py --sid 634353
"""
import argparse
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

from lime_rpc import add_auth_args, authenticate, load_dotenv, rpc

load_dotenv(Path(__file__).parent)

EXPECTED_GROUP_ORDER = [
    'Demographics', 'Main Category Barriers', 'Economic Barriers',
    'Environmental Barriers', 'Technological Barriers',
    'Operational Barriers', 'Social Barriers', 'Policy Barriers',
]
SECTION_N = {'M': 6, 'EC': 4, 'EN': 3, 'TC': 4, 'OP': 4, 'SO': 4, 'PO': 4}
EXPECTED_QUESTION_COUNT = 80
CODE_PAT = re.compile(r'^[a-zA-Z][a-zA-Z0-9]*$')
EM_KEYWORDS = {'NAOK', 'and', 'or', 'AND', 'OR', 'is_empty', 'true', 'false'}


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--sid', type=int, required=True)
    p.add_argument('--check-files', action='store_true',
                   help='After the API audit, do an HTTP HEAD check on bws_reset.js')
    add_auth_args(p)
    args = p.parse_args()

    endpoint, session = authenticate(args)
    issues = []
    def issue(sev, cat, msg):
        issues.append((sev, cat, msg))

    try:
        _check_survey_level(endpoint, session, args.sid, issue)
        all_qs = _fetch_all_questions(endpoint, session, args.sid, issue)
        if all_qs is None:
            return _report(args.sid, len(issues), 0, issues)
        all_codes = {q.get('title') for q in all_qs}
        for q in all_qs:
            _check_question(q, all_codes, issue)
    finally:
        rpc(endpoint, 'release_session_key', [session])

    if args.check_files:
        _check_file_reachability(args.url, args.sid)

    return _report(args.sid, EXPECTED_QUESTION_COUNT, len(all_qs), issues)


def _check_file_reachability(base_url, sid):
    url = f'{base_url.rstrip("/")}/upload/surveys/{sid}/files/bws_reset.js'
    print(f'\nChecking JS file reachability: {url}')
    try:
        req = urllib.request.Request(url, method='HEAD')
        resp = urllib.request.urlopen(req, timeout=10)
        print(f'  bws_reset.js: HTTP {resp.status} OK')
    except urllib.error.HTTPError as e:
        print(f'  bws_reset.js: HTTP {e.code} ERROR — file not uploaded?')
    except urllib.error.URLError as e:
        print(f'  bws_reset.js: connection error — {e.reason}')


def _check_survey_level(endpoint, session, sid, issue):
    ls = rpc(endpoint, 'get_language_properties',
             [session, sid, ['surveyls_welcometext'], 'en'])
    welcome = ls.get('surveyls_welcometext', '') if isinstance(ls, dict) else ''
    if '{SID}' in welcome:
        issue('CRITICAL', 'welcome', '{SID} placeholder still in welcome text')

    sp = rpc(endpoint, 'get_survey_properties', [session, sid, ['format', 'allowsave']])
    if isinstance(sp, dict):
        if sp.get('format') != 'G':
            issue('IMPORTANT', 'survey', f'format != G (got {sp.get("format")!r})')
        if sp.get('allowsave') != 'Y':
            issue('NICE', 'survey', f'allowsave != Y (got {sp.get("allowsave")!r})')


def _fetch_all_questions(endpoint, session, sid, issue):
    groups = rpc(endpoint, 'list_groups', [session, sid])
    if not isinstance(groups, list):
        issue('CRITICAL', 'groups', f'list_groups failed: {groups}')
        return None
    groups = sorted(groups, key=lambda g: int(g.get('group_order', 0)))
    got = [g.get('group_name') for g in groups]
    if got != EXPECTED_GROUP_ORDER:
        issue('IMPORTANT', 'groups', f'group order: got {got}')

    all_qs = []
    for g in groups:
        qs = rpc(endpoint, 'list_questions', [session, sid, g['gid']])
        if not isinstance(qs, list):
            continue
        for q in qs:
            props = rpc(endpoint, 'get_question_properties',
                        [session, q['qid'], ['attributes', 'attributes_lang', 'answeroptions', 'questionl10ns']])
            q['_props'] = props if isinstance(props, dict) else {}
            all_qs.append(q)
    if len(all_qs) != EXPECTED_QUESTION_COUNT:
        issue('CRITICAL', 'count',
              f'question count {len(all_qs)} != {EXPECTED_QUESTION_COUNT}')
    return all_qs


def _check_question(q, all_codes, issue):
    title = q.get('title', '')
    qtype = q.get('type')
    attrs = q['_props'].get('attributes') or {}
    attrs_lang = q['_props'].get('attributes_lang') or {}
    opts = q['_props'].get('answeroptions') or {}

    if not CODE_PAT.match(title):
        issue('CRITICAL', 'code-format', f'qid={q["qid"]} title={title!r} not alphanumeric')
    if len(title) > 20:
        issue('CRITICAL', 'code-length', f'qid={q["qid"]} title={title!r} > 20 chars')

    # Answer code length + reference uniqueness
    if qtype in ('L', '!') and isinstance(opts, dict):
        for code in opts:
            if len(str(code)) > 5:
                issue('CRITICAL', 'answer-code-length',
                      f'{title}: answer code {code!r} > 5 chars')

    # Reference checks: every CamelCase identifier in relevance/validation
    # must be either an EM keyword or an actual question code in the survey.
    refs = set()
    for eq in (q.get('relevance', ''), attrs.get('em_validation_q', '')):
        if not eq or eq == '1':
            continue
        for tok in re.findall(r'\b([A-Za-z_][A-Za-z0-9_]*)\b', eq):
            if tok in EM_KEYWORDS:
                continue
            # Heuristic: tokens that are likely question-code refs
            # (they appear before . or in comparison context)
            if re.search(rf'\b{re.escape(tok)}\s*(\.|!=|==)', eq):
                refs.add(tok)
    for ref in refs:
        if ref not in all_codes:
            issue('CRITICAL', 'broken-ref',
                  f'{title}: equation references {ref!r} which is not a question code')

    # CSS class expectations
    cssclass = attrs.get('cssclass', '') if isinstance(attrs, dict) else ''
    if 'mvs' in title or 'vsl' in title:
        expected = 'bws-cmp-'
    elif title.endswith('most') and not title.startswith('D'):
        expected = 'bws-most-'
    elif title.endswith('least') and not title.startswith('D'):
        expected = 'bws-least-'
    else:
        expected = None
    if expected and not cssclass.startswith(expected):
        issue('CRITICAL', 'cssclass',
              f'{title}: cssclass={cssclass!r} should start with {expected!r}')

    # Q_least must have validation
    if title.endswith('least') and not title.startswith('D'):
        if not attrs.get('em_validation_q'):
            issue('CRITICAL', 'validation', f'{title}: missing em_validation_q')
        if not (isinstance(attrs_lang, dict) and attrs_lang.get('em_validation_q_tip')):
            issue('IMPORTANT', 'validation', f'{title}: missing em_validation_q_tip')

    # Mandatory flags
    expected_mand = 'N' if title == 'D8expertise' else 'Y'
    if q.get('mandatory') != expected_mand:
        issue('IMPORTANT', 'mandatory',
              f'{title}: mandatory={q.get("mandatory")} expected {expected_mand}')

    # Answer counts
    if qtype in ('L', '!') and isinstance(opts, dict):
        n_opts = len(opts)
        for prefix, n in SECTION_N.items():
            if (title == f'{prefix}most' or title == f'{prefix}least') and n_opts != n:
                issue('CRITICAL', 'answer-count',
                      f'{title}: {n_opts} answers, expected {n}')
                break
        else:
            if 'mvs' in title or 'vsl' in title:
                if n_opts != 4:
                    issue('CRITICAL', 'answer-count',
                          f'{title}: {n_opts} scale answers, expected 4')

    # Inline-script check
    l10ns = q['_props'].get('questionl10ns') or {}
    if isinstance(l10ns, dict):
        en = l10ns.get('en') or {}
        if isinstance(en, dict) and en.get('script', '').strip():
            issue('CRITICAL', 'inline-script',
                  f'{title}: question_l10ns.script is non-empty (EM-mangling risk)')


def _report(sid, expected_count, actual_count, issues):
    print(f'\n=== Audit of SID {sid} ({actual_count}/{expected_count} questions) ===\n')
    if not issues:
        print('No issues found.')
        return 0
    by_sev = defaultdict(list)
    for sev, cat, msg in issues:
        by_sev[sev].append((cat, msg))
    for sev in ('CRITICAL', 'IMPORTANT', 'NICE'):
        items = by_sev.get(sev, [])
        if items:
            print(f'{sev} ({len(items)}):')
            for cat, msg in items:
                print(f'  [{cat}] {msg}')
            print()
    return 1 if by_sev.get('CRITICAL') else 0


if __name__ == '__main__':
    sys.exit(main())
