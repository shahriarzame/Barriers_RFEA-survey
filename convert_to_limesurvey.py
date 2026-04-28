#!/usr/bin/env python3
"""Convert Barriers_RFEA HTML survey (index.html) to a LimeSurvey 6.x .lss file.

Usage:
    python convert_to_limesurvey.py
    python convert_to_limesurvey.py --input index.html --output barriers_rfea.lss

Plan: ../../../Users/shahr/.claude/plans/barriers-rfea-survey-index-html-is-it-p-stateful-tide.md
Stress-test: same dir, *-stress-test.md

Requires Python >= 3.7 (relies on guaranteed dict insertion-order for idempotency).
Standard library only.
"""

import argparse
import html
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

# ============================================================================
# Constants & Mappings
# ============================================================================

DEFAULT_DB_VERSION = "649"   # LimeSurvey 6.17.x; override with --db-version
DEFAULT_SURVEY_ID = "123456"
LANGUAGE = "en"

SECTION_CODES = {
    'mainCategories':  'M',
    'economic':        'EC',
    'environmental':   'EN',
    'technological':   'TC',
    'operational':     'OP',
    'social':          'SO',
    'policy':          'PO',
}

# HTML <option value="..."> -> LimeSurvey answer code (alphanumeric only)
INDUSTRY_CODES = {
    'Freight/Logistics':         'FRLO',
    'Transportation':            'TRSP',
    'Automotive/OEM':            'AUTO',
    'Energy/Utilities':          'ENER',
    'Government/Public Sector':  'GOVT',
    'Academia/Research':         'ACAD',
    'Consulting':                'CONS',
    'Technology':                'TECH',
    'Other':                     'OTH',
}

EDUCATION_CODES = {
    "Bachelor's":   'BACH',
    "Master's":     'MAST',
    'PhD':          'PHD',
    'Professional': 'PROF',
    'Other':        'OTH',
}

EXPERIENCE_CODES = {
    # LimeSurvey answers.code is varchar(5) - codes must be <= 5 chars.
    # Positional codes Y1..Y6 keep ordering deterministic; the readable label
    # stored separately in <answer_l10ns>.
    '0-2':   'Y1',
    '3-5':   'Y2',
    '6-10':  'Y3',
    '11-15': 'Y4',
    '16-20': 'Y5',
    '20+':   'Y6',
}

SECTION_INTRO = (
    'Please answer Q1 (Most challenging) and Q2 (Least challenging) first. '
    'The comparison questions below will appear once both are selected.'
)

BWS_RESET_JS = """(function () {
    'use strict';
    function setup() {
        if (typeof jQuery === 'undefined') { setTimeout(setup, 100); return; }
        var $ = jQuery;
        $('[class*="bws-most-"], [class*="bws-least-"]').each(function () {
            var m = this.className.match(/(?:^|\\s)bws-(?:most|least)-([A-Za-z0-9]+)/);
            if (!m) return;
            var sel = '.bws-cmp-' + m[1] + ' input[type="radio"]';
            $(this).find('input[type="radio"]').on('change.bwsreset', function () {
                $(sel).prop('checked', false);
            });
        });
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', setup);
    } else { setup(); }
})();
"""


def _write_bws_reset_js(path):
    Path(path).write_text(BWS_RESET_JS, encoding='utf-8', newline='\n')


# ============================================================================
# JS literal extraction
# ============================================================================

def _find_balanced(text, start, open_ch, close_ch):
    """Return index of `close_ch` matching the `open_ch` at `start`, respecting
    JS string literals (single or double quoted, with backslash escapes)."""
    depth = 0
    in_string = False
    quote = None
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if in_string:
            if ch == quote:
                in_string = False
                quote = None
            continue
        if ch in ("'", '"'):
            in_string = True
            quote = ch
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
    raise ValueError(f"Unbalanced {open_ch}{close_ch} starting at {start}")


def _extract_const_body(text, name, open_ch, close_ch):
    m = re.search(rf"const\s+{re.escape(name)}\s*=\s*", text)
    if not m:
        raise ValueError(f"Could not find `const {name} = `")
    start = text.find(open_ch, m.end() - 1)
    if start == -1:
        raise ValueError(f"No opening `{open_ch}` after const {name} =")
    end = _find_balanced(text, start, open_ch, close_ch)
    return text[start:end + 1]


def js_to_json(text):
    """Convert a JS object/array literal (with single-quoted strings, unquoted
    keys, JS comments, and \\' escapes) to JSON-parseable text."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # Strip JS comments
        if ch == '/' and i + 1 < n and text[i + 1] in ('/', '*'):
            if text[i + 1] == '/':
                end = text.find('\n', i)
                i = end if end >= 0 else n
            else:
                end = text.find('*/', i + 2)
                i = end + 2 if end >= 0 else n
            continue
        # Parse a string literal (single or double quoted)
        if ch in ("'", '"'):
            quote = ch
            j = i + 1
            chars = []
            while j < n:
                cj = text[j]
                if cj == '\\' and j + 1 < n:
                    nxt = text[j + 1]
                    if nxt == quote:
                        # \' inside single-quoted string -> literal '
                        chars.append(quote)
                        j += 2
                    elif nxt in ('\\', '/', '"', "'", 'b', 'f', 'n', 'r', 't'):
                        if nxt == "'":
                            chars.append("'")
                        else:
                            chars.append('\\' + nxt)
                        j += 2
                    elif nxt == 'u':
                        chars.append(text[j:j + 6])
                        j += 6
                    else:
                        chars.append(nxt)
                        j += 2
                elif cj == quote:
                    j += 1
                    break
                else:
                    chars.append(cj)
                    j += 1
            else:
                raise ValueError(f"Unterminated string at offset {i}")
            # Re-emit as JSON string (always double-quoted, properly escaped)
            content = ''.join(chars)
            # Convert any kept "\\X" escapes back to actual chars where JSON
            # accepts them as-is; json.dumps re-escapes anything required.
            content = bytes(content, 'utf-8').decode('unicode_escape') if '\\' in content else content
            out.append(json.dumps(content, ensure_ascii=False))
            i = j
            continue
        out.append(ch)
        i += 1
    s = ''.join(out)
    # Quote unquoted keys: { key: ... } or , key: ...
    s = re.sub(r'([\{,]\s*)([A-Za-z_]\w*)(\s*:)', r'\1"\2"\3', s)
    # Drop trailing commas before } or ]
    s = re.sub(r',(\s*[}\]])', r'\1', s)
    return s


def extract_descriptions(html_text):
    body = _extract_const_body(html_text, 'descriptions', '{', '}')
    return json.loads(js_to_json(body))


def extract_survey_data(html_text):
    body = _extract_const_body(html_text, 'surveyData', '{', '}')
    return json.loads(js_to_json(body))


def extract_scale_options(html_text):
    body = _extract_const_body(html_text, 'scaleOptions', '[', ']')
    return json.loads(js_to_json(body))


# ============================================================================
# Demographic <select> extraction
# ============================================================================

class _SelectExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_select = False
        self.select_id = None
        self.in_option = False
        self.option_value = None
        self.option_chars = []
        self.results = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'select':
            self.in_select = True
            self.select_id = a.get('id')
            if self.select_id:
                self.results.setdefault(self.select_id, [])
        elif tag == 'option' and self.in_select:
            self.in_option = True
            self.option_value = a.get('value', '')
            self.option_chars = []

    def handle_data(self, data):
        if self.in_option:
            self.option_chars.append(data)

    def handle_endtag(self, tag):
        if tag == 'option' and self.in_option:
            label = ''.join(self.option_chars).strip()
            if self.option_value:  # skip the placeholder "" option
                self.results[self.select_id].append((self.option_value, label))
            self.in_option = False
            self.option_value = None
        elif tag == 'select':
            self.in_select = False
            self.select_id = None


def extract_demographic_options(html_text):
    p = _SelectExtractor()
    p.feed(html_text)
    return p.results


# ============================================================================
# XML helpers
# ============================================================================

def cdata(s):
    if s is None:
        s = ''
    s = str(s).replace(']]>', ']]]]><![CDATA[>')
    return f'<![CDATA[{s}]]>'


def xml_text(s):
    if s is None:
        return ''
    return html.escape(str(s), quote=True)


# ============================================================================
# LSS Builder
# ============================================================================

class LssBuilder:
    """Accumulates rows for each LSS table; renders them as a complete XML file.

    All IDs are deterministic global counters keyed off insertion order so the
    output is byte-identical across re-runs (required for plan idempotency)."""

    THEMES = {
        'L': 'listradio',
        '!': 'listdropdown',
        'S': 'shortfreetext',
        'T': 'longfreetext',
    }

    def __init__(self, db_version, survey_id, language=LANGUAGE):
        self.db_version = db_version
        self.survey_id = int(survey_id)
        self.language = language
        self.next_qid = 1
        self.next_aid = 1
        self.next_gid = 1
        self.next_qa_id = 1
        self.next_l10n_id = 1
        self.surveys = []
        self.surveys_languagesettings = []
        self.groups = []
        self.group_l10ns = []
        self.questions = []
        self.question_l10ns = []
        self.answers = []
        self.answer_l10ns = []
        self.question_attributes = []

    # ---------- row builders ----------

    def add_survey(self, **fields):
        self.surveys.append(fields)

    def add_languagesettings(self, **fields):
        self.surveys_languagesettings.append(fields)

    def add_group(self, group_name, group_order, description=''):
        gid = self.next_gid
        self.next_gid += 1
        self.groups.append({
            'gid': gid,
            'sid': self.survey_id,
            'group_order': group_order,
            'randomization_group': '',
            'grelevance': '',
        })
        l10n_id = self.next_l10n_id
        self.next_l10n_id += 1
        self.group_l10ns.append({
            'id': l10n_id,
            'gid': gid,
            'group_name': group_name,
            'description': description,
            'language': self.language,
        })
        return gid

    def add_question(self, gid, qtype, code, question_text, help_text='',
                     mandatory='N', relevance='1', question_order=1,
                     validation_eqn='', script='', cssclass=''):
        qid = self.next_qid
        self.next_qid += 1
        self.questions.append({
            'qid': qid,
            'parent_qid': 0,
            'sid': self.survey_id,
            'gid': gid,
            'type': qtype,
            'title': code,
            'preg': '',
            'other': 'N',
            'mandatory': mandatory,
            'encrypted': 'N',
            'question_order': question_order,
            'scale_id': 0,
            'same_default': 0,
            'relevance': relevance,
            'question_theme_name': self.THEMES.get(qtype, 'core'),
            'modulename': '',
            'same_script': 0,
        })
        l10n_id = self.next_l10n_id
        self.next_l10n_id += 1
        self.question_l10ns.append({
            'id': l10n_id,
            'qid': qid,
            'question': question_text,
            'help': help_text,
            'language': self.language,
            'script': script,
        })
        if validation_eqn:
            self.add_question_attribute(qid, 'em_validation_q', validation_eqn)
        if cssclass:
            self.add_question_attribute(qid, 'cssclass', cssclass)
        return qid

    def add_answer(self, qid, code, label, sortorder, scale_id=0):
        aid = self.next_aid
        self.next_aid += 1
        self.answers.append({
            'aid': aid,
            'qid': qid,
            'code': code,
            'sortorder': sortorder,
            'assessment_value': 0,
            'scale_id': scale_id,
        })
        l10n_id = self.next_l10n_id
        self.next_l10n_id += 1
        self.answer_l10ns.append({
            'id': l10n_id,
            'aid': aid,
            'answer': label,
            'language': self.language,
        })
        return aid

    def add_question_attribute(self, qid, attribute, value, language=''):
        qaid = self.next_qa_id
        self.next_qa_id += 1
        self.question_attributes.append({
            'qaid': qaid,
            'qid': qid,
            'attribute': attribute,
            'value': value,
            'language': language,
        })

    # ---------- rendering ----------

    def build_xml(self):
        parts = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<document>',
            '<LimeSurveyDocType>Survey</LimeSurveyDocType>',
            f'<DBVersion>{xml_text(self.db_version)}</DBVersion>',
            '<languages>',
            f'  <language>{xml_text(self.language)}</language>',
            '</languages>',
            self._render_table('surveys', self.surveys),
            self._render_table('surveys_languagesettings', self.surveys_languagesettings),
            self._render_table('groups', self.groups),
            self._render_table('group_l10ns', self.group_l10ns),
            self._render_table('questions', self.questions),
            self._render_table('question_l10ns', self.question_l10ns),
            self._render_table('answers', self.answers),
            self._render_table('answer_l10ns', self.answer_l10ns),
            '<subquestions/>',
            self._render_table('question_attributes', self.question_attributes),
            '</document>',
            '',
        ]
        return '\n'.join(parts)

    def _render_table(self, name, rows):
        if not rows:
            return f'<{name}>\n  <fields/>\n  <rows/>\n</{name}>'
        field_names = list(rows[0].keys())
        out = [f'<{name}>', '  <fields>']
        for f in field_names:
            out.append(f'    <fieldname>{xml_text(f)}</fieldname>')
        out.append('  </fields>')
        out.append('  <rows>')
        for row in rows:
            out.append('    <row>')
            for f in field_names:
                v = row.get(f, '')
                out.append(f'      <{f}>{cdata(v)}</{f}>')
            out.append('    </row>')
        out.append('  </rows>')
        out.append(f'</{name}>')
        return '\n'.join(out)


# ============================================================================
# Welcome / End Text
# ============================================================================

def build_welcome_text():
    return (
        '<h1>🚛 Road Freight Electrification &amp; Automation Barriers Survey</h1>\n'
        '<p>Thank you for participating in this research study on identifying barriers to road freight electrification and automation.</p>\n'
        '<div style="border-left: 4px solid #3498db; padding: 12px 16px; background: #eaf4fc; margin: 15px 0;">\n'
        '<p><strong>Purpose of This Study</strong></p>\n'
        '<p>This study aims to evaluate and rank barriers to the implementation of electric and automated technologies in road freight transportation. The identified barriers are classified into six categories (Economic, Environmental, Technological, Operational, Social, and Policy), each containing specific sub-barriers as shown in the figure below.</p>\n'
        '</div>\n'
        '<p><strong>Survey Methodology:</strong></p>\n'
        '<p>This survey uses the <strong>Best-Worst Method (BWM)</strong> to rank barriers. For each category, you will:</p>\n'
        '<ol>\n'
        '<li>Select the <strong>most challenging barrier</strong> that plays the most critical role in hindering implementation</li>\n'
        '<li>Select the <strong>least challenging barrier</strong> that plays the least critical role</li>\n'
        '<li>Compare how much more challenging the most challenging barrier is compared to each of the other barriers</li>\n'
        '<li>Compare how much more challenging the other barriers are compared to the least challenging barrier</li>\n'
        '</ol>\n'
        '<p>For all comparisons, you will use the following scale:</p>\n'
        '<ul>\n'
        '<li><strong>Slightly more challenging</strong> (minor difference in impact)</li>\n'
        '<li><strong>Moderately more challenging</strong> (noticeable difference in impact)</li>\n'
        '<li><strong>Significantly more challenging</strong> (substantial difference in impact)</li>\n'
        '<li><strong>Extremely more challenging</strong> (very large difference in impact)</li>\n'
        '</ul>\n'
        '<p><strong>⏱️ Estimated time:</strong> 20-30 minutes</p>\n'
        '<div style="text-align: center; margin: 25px 0;">\n'
        '<img src="upload/surveys/{SID}/images/Barriers_RF.png" alt="Barriers Framework" style="max-width: 100%; height: auto;" />\n'
        '<p style="font-size: 0.9em; color: #666;"><em>Figure 1: Barrier categories and sub-barriers</em></p>\n'
        '</div>\n'
        '<p><strong>Confidentiality:</strong> This survey is anonymous and responses will be used solely for academic research purposes. No personally identifiable information will be published.</p>\n'
        '<p style="font-size: 0.95em; color: #555;">This research is conducted by the Chair of Transportation Systems Engineering at Technical University of Munich (TUM), Germany. For questions, please contact: Shahriar Iqbal Zame (PhD candidate at TUM) at <strong>shahriar.zame@tum.de</strong> or Dr.-Ing. Mohammad Sadrani (Assistant Professor, University of Twente) at <strong>m.sadrani@utwente.nl</strong>. You may also reach out to our team leader Prof. Dr. Constantinos Antoniou at <strong>c.antoniou(at)tum.de</strong>.</p>'
    )


def build_end_text():
    return (
        '<h2>✅ Survey Complete!</h2>\n'
        '<p>Your responses have been successfully submitted. Thank you for participating in this research!</p>\n'
        '<p style="color: #7f8c8d;">You may now close this window.</p>'
    )


# ============================================================================
# Survey assembly
# ============================================================================

def assemble(builder, survey_data, descriptions, scale_options, demo_opts):
    _add_survey_row(builder)
    _add_languagesettings_row(builder)

    g1 = builder.add_group(
        group_name='Demographics',
        group_order=1,
        description='Please provide some information about your background. Fields marked with * are required.',
    )
    _add_demographics(builder, g1, demo_opts)

    group_order = 2
    for skey in SECTION_CODES:
        section = survey_data[skey]
        gid = builder.add_group(
            group_name=_safe_html(section['title']),
            group_order=group_order,
            description=SECTION_INTRO + '<script src="upload/surveys/{SID}/files/bws_reset.js"></script>',
        )
        _add_section(builder, gid, skey, section, descriptions, scale_options)
        group_order += 1


def _add_survey_row(b):
    b.add_survey(
        sid=b.survey_id,
        admin='Survey Administrator',
        expires='',
        startdate='',
        adminemail='admin@example.com',
        anonymized='Y',
        format='G',
        savetimings='N',
        template='fruity',
        language=b.language,
        additional_languages='',
        datestamp='Y',
        usecookie='N',
        allowregister='N',
        allowsave='Y',
        autonumber_start=0,
        autoredirect='N',
        allowprev='Y',
        printanswers='N',
        ipaddr='N',
        ipanonymize='N',
        refurl='N',
        datecreated='2026-04-28 00:00:00',
        publicstatistics='N',
        publicgraphs='N',
        listpublic='N',
        htmlemail='Y',
        sendconfirmation='Y',
        tokenanswerspersistence='N',
        assessments='N',
        usecaptcha='D',
        usetokens='N',
        bounce_email='admin@example.com',
        attributedescriptions='',
        emailresponseto='',
        emailnotificationto='',
        tokenlength=15,
        showxquestions='Y',
        showgroupinfo='B',
        shownoanswer='Y',
        showqnumcode='X',
        questionindex=0,
        navigationdelay=0,
        nokeyboard='N',
        alloweditaftercompletion='N',
        googleanalyticsstyle='0',
        googleanalyticsapikey='',
        showwelcome='Y',
        showprogress='Y',
        questioncount=0,
        showsurveypolicynotice=0,
    )


def _add_languagesettings_row(b):
    b.add_languagesettings(
        surveyls_survey_id=b.survey_id,
        surveyls_language=b.language,
        surveyls_title='Road Freight Electrification &amp; Automation Barriers Survey',
        surveyls_description='Best-Worst Method survey to rank barriers to electric and automated road freight.',
        surveyls_welcometext=build_welcome_text(),
        surveyls_endtext=build_end_text(),
        surveyls_policy_notice='',
        surveyls_policy_error='',
        surveyls_policy_notice_label='',
        surveyls_url='',
        surveyls_urldescription='',
        surveyls_email_invite_subj='Invitation to take a survey',
        surveyls_email_invite='',
        surveyls_email_remind_subj='Reminder',
        surveyls_email_remind='',
        surveyls_email_register_subj='Survey registration confirmation',
        surveyls_email_register='',
        surveyls_email_confirm_subj='Confirmation of your participation',
        surveyls_email_confirm='',
        surveyls_dateformat=1,
        surveyls_numberformat=0,
        surveyls_attributecaptions='',
        attachments='',
    )


def _add_demographics(b, gid, demo_opts):
    qorder = 1
    # NOTE on question codes: LimeSurvey 6.x (esp. limequery.com hosted) strips
    # any non-alphanumeric character from question titles on import. The
    # importer rewrites references in the `relevance` column to match the
    # truncated title, but does NOT rewrite references in `em_validation_q`.
    # We therefore use camelCase (no underscore) codes throughout.
    b.add_question(
        gid=gid, qtype='S', code='D1occupation',
        question_text='Job Title / Occupation',
        help_text='e.g., Professor, Manager, Researcher',
        mandatory='Y', relevance='1', question_order=qorder)
    qorder += 1

    qid = b.add_question(
        gid=gid, qtype='!', code='D2industry',
        question_text='Industry Sector',
        mandatory='Y', relevance='1', question_order=qorder)
    qorder += 1
    for sortorder, (value, label) in enumerate(demo_opts.get('demo-industry', []), start=1):
        code = INDUSTRY_CODES.get(value)
        if code is None:
            raise ValueError(f"Unmapped industry option: {value!r}")
        b.add_answer(qid, code, label, sortorder)

    b.add_question(
        gid=gid, qtype='S', code='D3industryother',
        question_text='Please specify Industry Sector',
        mandatory='Y',
        relevance='D2industry.NAOK == "OTH"',
        question_order=qorder)
    qorder += 1

    qid = b.add_question(
        gid=gid, qtype='!', code='D4experience',
        question_text='Years of Experience',
        mandatory='Y', relevance='1', question_order=qorder)
    qorder += 1
    for sortorder, (value, label) in enumerate(demo_opts.get('demo-experience', []), start=1):
        code = EXPERIENCE_CODES.get(value)
        if code is None:
            raise ValueError(f"Unmapped experience option: {value!r}")
        b.add_answer(qid, code, label, sortorder)

    qid = b.add_question(
        gid=gid, qtype='!', code='D5education',
        question_text='Highest Education Level',
        mandatory='Y', relevance='1', question_order=qorder)
    qorder += 1
    for sortorder, (value, label) in enumerate(demo_opts.get('demo-education', []), start=1):
        code = EDUCATION_CODES.get(value)
        if code is None:
            raise ValueError(f"Unmapped education option: {value!r}")
        b.add_answer(qid, code, label, sortorder)

    b.add_question(
        gid=gid, qtype='S', code='D6educationother',
        question_text='Please specify Education Level',
        mandatory='Y',
        relevance='D5education.NAOK == "OTH"',
        question_order=qorder)
    qorder += 1

    b.add_question(
        gid=gid, qtype='S', code='D7country',
        question_text='Country',
        help_text='Country of residence',
        mandatory='Y', relevance='1', question_order=qorder)
    qorder += 1

    b.add_question(
        gid=gid, qtype='T', code='D8expertise',
        question_text='Area of Expertise (Optional)',
        help_text='e.g., Fleet management, EV technology, Supply chain optimization. Briefly describe your relevant expertise or specialization.',
        mandatory='N', relevance='1', question_order=qorder)


def _safe_html(s):
    """Escape < and > only. Leave & alone: LimeSurvey's {QCODE.shown} path
    applies htmlspecialchars() to answer labels at render time. Pre-escaping &
    causes &amp;amp; to appear literally in piped question text."""
    return s.replace('<', '&lt;').replace('>', '&gt;')


def _answer_label(item):
    """Answer-list label. Plain text only — descriptions go into the
    Q_most question text, and emphasis (<strong>) is added by the comparison
    question text where the piped value is consumed. LimeSurvey's
    {QCODE.shown} doesn't reliably preserve HTML inside answer text."""
    return _safe_html(item)


def _comparison_spec(kind, s_code, idx, item, most_code, least_code):
    """Return (code, question_text, relevance) for one comparison question.
    `kind` is 'mvs' (Most-vs-Ai) or 'vsl' (Ai-vs-Least)."""
    ai = f'A{idx}'
    item_html = _safe_html(item)
    if kind == 'mvs':
        return (
            f'{s_code}mvsA{idx}',
            (f'How much <strong>more challenging</strong> is '
             f'<strong>{{{most_code}.shown}}</strong> compared to '
             f'<strong>{item_html}</strong>?'),
            (f'!is_empty({most_code}.NAOK) and '
             f'{most_code}.NAOK != "{ai}"'),
        )
    return (
        f'{s_code}vslA{idx}',
        (f'How much <strong>more challenging</strong> is '
         f'<strong>{item_html}</strong> compared to '
         f'<strong>{{{least_code}.shown}}</strong>?'),
        (f'!is_empty({least_code}.NAOK) and '
         f'{most_code}.NAOK != "{ai}" and '
         f'{least_code}.NAOK != "{ai}"'),
    )


def _build_descriptions_block(items, descriptions):
    """Reference list of barrier definitions, prepended to Q_most/Q_least text.
    Returns '' if the section's items have no entries in `descriptions`
    (e.g. mainCategories with hasDescriptions=False)."""
    rows = []
    for item in items:
        desc = descriptions.get(item)
        if not desc:
            return ''
        rows.append(
            f'<p style="margin: 6px 0;">'
            f'<strong>{_safe_html(item)}:</strong> '
            f'<span style="color: #555;">{_safe_html(desc)}</span>'
            f'</p>'
        )
    return (
        '<div style="margin-bottom: 15px; padding: 10px 14px; '
        'background: #f7f9fa; border-left: 3px solid #3498db; '
        'font-size: 0.92em;">'
        '<p style="margin: 0 0 6px 0;"><strong>Barrier definitions:</strong></p>'
        + ''.join(rows) +
        '</div>'
    )


def _add_section(b, gid, skey, section, descriptions, scale_options):
    s_code = SECTION_CODES[skey]
    items = section['items']
    has_desc = section.get('hasDescriptions', False)
    title = _safe_html(section['title'])
    qorder = 1

    # Question codes use camelCase (no underscore) to survive LimeSurvey's
    # alphanumeric-only title constraint without rewriting (see comment in
    # _add_demographics).
    most_code = f'{s_code}most'
    least_code = f'{s_code}least'

    # CSS classes (added via cssclass question_attribute). The reset script
    # finds host & target questions by these classes — survives import.
    most_class = f'bws-most-{s_code}'
    least_class = f'bws-least-{s_code}'
    cmp_class = f'bws-cmp-{s_code}'

    # Reference block: barrier descriptions prepended to Q_most.
    # Empty for sections with hasDescriptions=False (mainCategories).
    descs_block = _build_descriptions_block(items, descriptions) if has_desc else ''

    qid_most = b.add_question(
        gid=gid, qtype='L', code=most_code,
        question_text=(
            descs_block +
            f'<p>Which is the <strong>Most challenging</strong> barrier among '
            f'<em>{title}</em>?</p>'
        ),
        mandatory='Y', relevance='1', question_order=qorder,
        cssclass=most_class)
    qorder += 1
    for i, item in enumerate(items, start=1):
        b.add_answer(qid_most, f'A{i}', _answer_label(item), i)

    # No descs_block on Q_least — already shown above on Q_most (same page,
    # format=G), don't duplicate.
    qid_least = b.add_question(
        gid=gid, qtype='L', code=least_code,
        question_text=(
            f'<p>Which is the <strong>Least challenging</strong> barrier among '
            f'<em>{title}</em>?</p>'
        ),
        mandatory='Y', relevance='1', question_order=qorder,
        # is_empty guard: don't fire validation before Q_most is answered.
        validation_eqn=(
            f'is_empty({most_code}.NAOK) or '
            f'{least_code}.NAOK != {most_code}.NAOK'
        ),
        cssclass=least_class)
    qorder += 1
    for i, item in enumerate(items, start=1):
        b.add_answer(qid_least, f'A{i}', _answer_label(item), i)
    b.add_question_attribute(qid_least, 'em_validation_q_tip',
                             'Most and Least challenging cannot be the same barrier.',
                             language=b.language)

    # mvs (relevance excludes only the chosen Most — preserves Most-vs-Least).
    # vsl (relevance excludes both Most and Least).
    # NAOK + is_empty guards keep EM happy when Q_most/Q_least are unanswered.
    # Piped values wrapped in <strong> here because answer text is plain.
    for kind in ('mvs', 'vsl'):
        for i, item in enumerate(items, start=1):
            code, text, relevance = _comparison_spec(
                kind, s_code, i, item, most_code, least_code)
            qid = b.add_question(
                gid=gid, qtype='L', code=code,
                question_text=text,
                mandatory='Y', relevance=relevance,
                question_order=qorder,
                cssclass=cmp_class)
            qorder += 1
            for j, opt in enumerate(scale_options, start=1):
                b.add_answer(qid, f'S{j}', _safe_html(opt), j)


# ============================================================================
# Main
# ============================================================================

def main(argv=None):
    here = Path(__file__).parent
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--input',  default=str(here / 'index.html'))
    p.add_argument('--output', default=str(here / 'barriers_rfea.lss'))
    p.add_argument('--survey-id', default=DEFAULT_SURVEY_ID)
    p.add_argument('--db-version', default=DEFAULT_DB_VERSION)
    args = p.parse_args(argv)

    in_path = Path(args.input)
    out_path = Path(args.output)
    if not in_path.exists():
        print(f"ERROR: input file not found: {in_path}", file=sys.stderr)
        return 1

    print(f"Reading {in_path}")
    src = in_path.read_text(encoding='utf-8')

    print("Extracting JS data")
    descriptions = extract_descriptions(src)
    survey_data = extract_survey_data(src)
    scale_options = extract_scale_options(src)
    demo_opts = extract_demographic_options(src)

    print(f"  descriptions: {len(descriptions)}")
    print(f"  sections:     {list(survey_data.keys())}")
    print(f"  scale opts:   {scale_options}")
    print(f"  demographic selects: {sorted(demo_opts.keys())}")

    print("Building LSS XML")
    b = LssBuilder(args.db_version, args.survey_id)
    assemble(b, survey_data, descriptions, scale_options, demo_opts)
    xml = b.build_xml()

    print(f"Writing {out_path}")
    out_path.write_text(xml, encoding='utf-8', newline='\n')
    js_path = out_path.parent / 'bws_reset.js'
    _write_bws_reset_js(js_path)
    print(
        f"Done. groups={len(b.groups)}, questions={len(b.questions)}, "
        f"answers={len(b.answers)}, question_attributes={len(b.question_attributes)}"
    )
    print(f"Wrote {out_path} and {js_path}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
