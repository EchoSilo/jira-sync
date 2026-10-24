#!/usr/bin/env python3
"""jira-sync: keep a Jira project in step with a git repo, GSD-aware.

Stdlib only. Per-repo config lives in <repo>/.jira-sync.json ({"site", "project"});
credentials come from the JIRA_EMAIL / JIRA_API_TOKEN environment variables.
Jira itself is the source of truth for which GSD item maps to which issue: every
synced issue carries the label "jira-sync" plus "gsd-<id>", and the local cache in
.git/jira-sync-map.json only exists so the commit-msg hook can work offline.
"""
import argparse
import base64
import calendar
import datetime
import json
import math
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG = '.jira-sync.json'
SYNC_LABEL = 'jira-sync'
HOOK_MARK = 'installed by jira-sync'
STABLE = Path.home() / '.jira-sync' / 'jira_sync.py'
SCOPE_RE = re.compile(r'^\w+!?\(([^)]+)\)')
CATS = ['new', 'indeterminate', 'done']  # Jira status categories, in workflow order
CAT_NAMES = {'new': 'To Do', 'indeterminate': 'In Progress', 'done': 'Done'}
LOCK_STALE_S = 600


class JiraError(Exception):
    pass


class Unrecognized(Exception):
    """GSD files exist but do not look like the format this parser knows."""


# --------------------------------------------------------------------------- git

def git(*args, cwd=None):
    r = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, encoding='utf-8')
    return r.stdout.strip() if r.returncode == 0 else ''


def repo_root(cwd=None):
    top = git('rev-parse', '--show-toplevel', cwd=cwd)
    if not top:
        raise SystemExit('jira-sync: not inside a git repository')
    return Path(top)


def git_path(root, name):
    # Honors core.hooksPath and worktrees; may come back relative to root.
    return root / git('rev-parse', '--git-path', name, cwd=root)


def load_config(root):
    p = root / CONFIG
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else None


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')


# ----------------------------------------------------------------- key resolution

def key_re(project):
    return re.compile(r'\b%s-\d+\b' % re.escape(project))


def scope_ids(subject):
    """GSD ids a commit subject points at, most specific first.

    feat(39.2-16): -> ['39.2-16', '39.2'];  docs(41): -> ['41'];
    docs(phase-41): -> ['41'];  docs(quick-261018-ri9): -> ['quick-261018-ri9'].
    """
    m = SCOPE_RE.match(subject)
    if not m:
        return []
    scope = m.group(1)
    if scope.startswith('quick-'):
        return [scope]
    scope = scope[len('phase-'):] if scope.startswith('phase-') else scope
    m = re.fullmatch(r'(\d+(?:\.\d+)*)(?:-(\d+))?', scope)
    if not m:
        return []
    return ['%s-%s' % (m.group(1), m.group(2)), m.group(1)] if m.group(2) else [m.group(1)]


def resolve_key(msg, branch, cache, active, project):
    """The issue key a commit belongs to, or None. First match wins:
    key already in message -> key in branch -> GSD scope -> active issue."""
    kre = key_re(project)
    if kre.search(msg):
        return None  # already linked; leave the message alone
    m = kre.search(branch or '')
    if m:
        return m.group(0)
    for gid in scope_ids(msg.splitlines()[0] if msg else ''):
        if gid in cache:
            return cache[gid]
    return active or None


def tag_message(msg, key):
    lines = msg.split('\n')
    lines[0] = lines[0].rstrip() + ' [%s]' % key
    return '\n'.join(lines)


# ---------------------------------------------------------------------- GSD parse

def frontmatter(text):
    m = re.match(r'---\r?\n(.*?)\r?\n---', text, re.S)
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        kv = re.match(r'([A-Za-z_][\w-]*):\s*(.*)$', line)
        if kv:
            out[kv.group(1)] = kv.group(2).strip().strip('"\'').replace('\\"', '"')
    return out


def first_sentence(text, limit=200):
    text = ' '.join(text.split())
    m = re.match(r'(.+?[.!?])(\s|$)', text)
    s = m.group(1) if m else text
    return s if len(s) <= limit else s[:limit - 3].rstrip() + '...'


# ------------------------------------------------------------ story points

FIB = [1, 2, 3, 5, 8, 13]
ANCHOR_POINTS = [1, 2, 3, 5, 8]
ANCHOR_QUANTILES = [0.1, 0.3, 0.5, 0.75, 0.95]
DEFAULT_TOKENS = 60000
AUTO_LABEL, REVIEWED_LABEL = 'points-auto', 'points-reviewed'


def plan_signals(text):
    """Plan-time sizing inputs from a GSD PLAN.md (never actuals: points are a forecast)."""
    fm = re.match(r'---\r?\n(.*?)\r?\n---', text, re.S)
    head = fm.group(1) if fm else ''
    tok = re.search(r'^estimate:[ \t]*$(?:\n[ \t]+.*)*?\n[ \t]+tokens:[ \t]*(\d+)', head, re.M)
    block = re.search(r'^files_modified:[ \t]*$((?:\n[ \t]+-.*)*)', head, re.M)
    inline = re.search(r'^files_modified:[ \t]*\[(.*)\]', head, re.M)
    files = (len(re.findall(r'\n[ \t]+-', block.group(1))) if block
             else len([x for x in inline.group(1).split(',') if x.strip()]) if inline else 0)
    conf = re.search(r'^[ \t]+confidence:[ \t]*(\w+)', head, re.M)
    return {'tokens': int(tok.group(1)) if tok else None, 'files': files,
            'tasks': len(re.findall(r'<task\b', text)),
            'checkpoints': len(re.findall(r'<task\b[^>]*type="checkpoint', text)),
            'hands_on': bool(re.search(r'^autonomous:[ \t]*false', head, re.M)),
            'confidence': conf.group(1).lower() if conf else None}


def plan_score(s):
    """CUE score. Effort = estimated tokens, complexity = files + tasks, unknowns/risk =
    human checkpoints, hands-on plans and stated confidence. Log scale, so doubling the
    effort adds one fixed step, the way Fibonacci steps grow."""
    return round(math.log(s['tokens'] or DEFAULT_TOKENS) + 0.5 * math.log1p(s['files'])
                 + 0.15 * s['tasks'] + 0.3 * s['checkpoints'] + 0.2 * s['hands_on']
                 + {'low': 0.2, 'medium': 0.1}.get(s['confidence'], 0), 3)


def describe_signals(s):
    parts = ['est. %dk effort' % round((s['tokens'] or DEFAULT_TOKENS) / 1000), '%d files' % s['files'], '%d tasks' % s['tasks']]
    if s['checkpoints']:
        parts.append('%d human checkpoint%s' % (s['checkpoints'], 's' if s['checkpoints'] > 1 else ''))
    if s['hands_on']:
        parts.append('not autonomous')
    return ', '.join(parts)


def propose_anchors(stories):
    """Pick finished stories spread across the score range as the 1/2/3/5/8 references."""
    done = sorted((s for s in stories if s['cat'] == 'done'), key=lambda s: (s['score'], s['id']))
    picks, last = [], None
    for q, pts in zip(ANCHOR_QUANTILES, ANCHOR_POINTS):
        i = int(q * (len(done) - 1) + 0.5)
        while i < len(done) and last is not None and done[i]['score'] <= last:
            i += 1  # anchors must be strictly larger than the one below
        if i >= len(done):
            return []
        last = done[i]['score']
        picks.append({'id': done[i]['id'], 'points': pts, 'score': done[i]['score'], 'summary': done[i]['summary']})
    return picks


def size_against(score, anchors):
    """(points, reason) by triangulating between the two anchors a story falls between."""
    a = sorted((x for x in anchors if x.get('score') is not None), key=lambda x: x['score'])
    if len(a) < 2:
        return None, 'fewer than two scored anchors'
    lo = [x for x in a if x['score'] <= score]
    hi = [x for x in a if x['score'] > score]
    if not lo:
        return a[0]['points'], 'no bigger than the smallest anchor %s (%d)' % (a[0]['id'], a[0]['points'])
    if not hi:
        top, gap = a[-1], a[-1]['score'] - a[-2]['score']
        if score >= top['score'] + gap:
            pts = FIB[min(FIB.index(top['points']) + 1, len(FIB) - 1)] if top['points'] in FIB else top['points']
            return pts, 'well beyond the largest anchor %s (%d)' % (top['id'], top['points'])
        return top['points'], 'about the size of the largest anchor %s (%d)' % (top['id'], top['points'])
    below, above = lo[-1], hi[0]
    near = below if score - below['score'] <= above['score'] - score else above
    return near['points'], 'bigger than %s (%d), smaller than %s (%d), closer to %s' % (
        below['id'], below['points'], above['id'], above['points'], near['id'])


def short_title(text, limit=60):
    """A Jira-sized title: parentheticals dropped, cut at the strongest clause boundary
    that fits (topic break, then semicolon, then comma), never mid-word, no ellipsis."""
    t = ' '.join(re.sub(r'\s*\([^)]*\)', '', text or '').split()).strip(' .')
    t = re.sub(r'^(?:Task \d+:\s*|Plan [\d.]+-\d+:\s*|Tracer\s*--\s*)+', '', t)
    if t.split(' ')[0].isalpha():
        t = t[:1].upper() + t[1:]
    if len(t) <= limit:
        return t
    head, out = t[:limit + 1], None
    for seps, floor in (((': ', ' -- ', ' \u2014 '), 15), (('; ',), 25), ((', ', ' - '), limit // 2)):
        pos = max(head.rfind(s) for s in seps)
        if pos >= floor:
            out = t[:pos]
            break
    if out is None:  # no clause break: cut at a word, and not on a dangling little word
        out = head[:head.rfind(' ')] if ' ' in head else t[:limit]
        out = re.sub(r'(?:\s+(?:the|a|an|of|in|to|and|or|for|with|when|on|at|by|is|are))+$', '', out, flags=re.I)
    if out.count('"') % 2:
        out = out[:out.rfind('"')]  # never end inside a quotation
    return out.strip(' .,;:-')


def tag(text, name):
    m = re.search(r'<%s\b[^>]*>(.*?)</%s>' % (name, name), text or '', re.S)
    return textwrap.dedent(m.group(1)).strip() if m else ''


def yaml_list(text, key):
    """Items of `key:` as a block list (any indent) or an inline [a, b] list."""
    m = re.search(r'^([ \t]*)%s:[ \t]*\[(.*?)\]' % re.escape(key), text, re.M)
    if m:
        return [x.strip().strip('"\'') for x in m.group(2).split(',') if x.strip()]
    m = re.search(r'^([ \t]*)%s:[ \t]*\n((?:\1[ \t]+-.*(?:\n|$))+)' % re.escape(key), text, re.M)
    return [x.strip().strip('"\'').replace('\\"', '"') for x in re.findall(r'^[ \t]+-[ \t]+(.*)$', m.group(2), re.M)] if m else []


def bullets(text):
    """Markdown '- ' lines as a list, or None when the text is prose."""
    items = re.findall(r'^\s*[-*]\s+(.*)$', text or '', re.M)
    return items if items and len(items) >= len([l for l in (text or '').splitlines() if l.strip()]) / 2 else None


def md_sections(text):
    """{heading: body} for every '## heading' block in a markdown file."""
    parts = re.split(r'^##\s+(.+?)\s*$', text, flags=re.M)
    return {parts[i].strip(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def body_after_frontmatter(text):
    return re.sub(r'\A---\r?\n.*?\r?\n---\r?\n?', '', text, flags=re.S)


def _sec(heading, value, kind=None):
    """One description section; kind inferred: list -> bullets, text with '- ' lines -> bullets, else prose."""
    if not value:
        return None
    if kind is None:
        kind = 'ul' if isinstance(value, list) else 'p'
        if kind == 'p' and bullets(value):
            value, kind = bullets(value), 'ul'
    return (heading, kind, value)


def epic_sections(roadmap, num):
    m = re.search(r'^###\s+Phase %s:.*?$(.*?)(?=^###?\s|\Z)' % re.escape(num), roadmap, re.M | re.S)
    block = m.group(1) if m else ''
    field = lambda name: (re.search(r'\*\*%s\*\*:?\s*(.+)' % name, block) or [None, ''])[1].strip()
    criteria = re.findall(r'^\s+\d+\.\s+(.+)$', block, re.M)
    return [s for s in (_sec('Goal', field('Goal')), _sec('Success criteria', criteria, 'ol'),
                        _sec('Requirements', field('Requirements')), _sec('Depends on', field('Depends on'))) if s]


def story_sections(text, summary_text=''):
    head = (re.match(r'---\r?\n(.*?)\r?\n---', text, re.S) or [None, ''])[1]
    tasks = [short_title(n, 120) for n in re.findall(r'<task\b[^>]*>\s*<name>(.*?)</name>', text, re.S)]
    outcome = (re.search(r'^\*\*(.+?)\*\*\s*$', body_after_frontmatter(summary_text), re.M) or [None, ''])[1]
    details = '; '.join(x for x in (
        'Requirements: ' + ', '.join(yaml_list(head, 'requirements')) if yaml_list(head, 'requirements') else '',
        'Depends on: ' + ', '.join(yaml_list(head, 'depends_on')) if yaml_list(head, 'depends_on') else '',
        'Wave ' + (re.search(r'^wave:\s*(\S+)', head, re.M) or [None, ''])[1] if re.search(r'^wave:', head, re.M) else '') if x)
    return [s for s in (_sec('Objective', tag(text, 'objective'), 'p'), _sec('Outcome', outcome),
                        _sec('Scope', tasks), _sec('Acceptance criteria', yaml_list(head, 'truths')),
                        _sec('Files', yaml_list(head, 'files_modified'), 'code'), _sec('Details', details)) if s]


def subtask_sections(block):
    files = [f.strip() for f in tag(block, 'files').split(',') if f.strip()]
    return [s for s in (_sec('Behaviour', tag(block, 'behavior')), _sec('What to do', tag(block, 'action')),
                        _sec('Acceptance criteria', tag(block, 'acceptance_criteria')),
                        _sec('Verify', tag(block, 'automated') or tag(block, 'verify'), 'pre'),
                        _sec('Done when', tag(block, 'done')), _sec('Files', files, 'code')) if s]


def bug_sections(text):
    secs = md_sections(text)
    res = secs.get('Resolution', '')
    val = lambda k: (re.search(r'^%s:\s*"?(.*?)"?\s*$' % k, res, re.M) or [None, ''])[1].replace('\\"', '"')
    return [s for s in (_sec('Symptoms', secs.get('Symptoms', '')), _sec('Root cause', val('root_cause')),
                        _sec('Fix', val('fix')), _sec('Verification', val('verification'))) if s]


# ------------------------------------------------------------------ git dates

def git_add_dates(root):
    """{repo-relative path: ISO date it was first committed} for files under .planning/."""
    out, when = {}, None
    for line in git('log', '--diff-filter=A', '--name-only', '--format=@@%aI', '--', '.planning', cwd=root).splitlines():
        if line.startswith('@@'):
            when = line[2:]
        elif line.strip():
            out[line.strip()] = when  # log is newest first, so the last write is the earliest add
    return out


def phase_starts(root):
    """{phase number: ISO date of its first plan commit} -- when work (not planning) on it began."""
    out = {}
    for line in git('log', '--format=%aI%x09%s', cwd=root).splitlines():
        when, _, subj = line.partition('\t')
        ids = scope_ids(subj)
        if len(ids) == 2:
            out[ids[-1]] = when  # newest first -> ends on the earliest
    return out


def parse_ts(s):
    """ISO timestamp (git %aI or Jira's ...000Z) -> aware datetime."""
    return datetime.datetime.fromisoformat(re.sub(r'\.\d+', '', s).replace('Z', '+00:00')) if s else None


def gsd_items(root, started_ids=(), add_dates=None, starts=None):
    """Desired Jira items derived from .planning/, parents before children.

    Each item: {id, type, summary, parent, cat, version, path, sections, created_at, done_at}
    (+ signals/score on stories). Returns ([], None) when there is no .planning/ directory.
    """
    pl = root / '.planning'
    if not pl.is_dir():
        return [], None
    add_dates, starts = add_dates or {}, starts or {}
    state_md = pl / 'STATE.md'
    roadmap_md = pl / 'ROADMAP.md'
    if not state_md.exists() or not roadmap_md.exists() or not (pl / 'phases').is_dir():
        raise Unrecognized('.planning/ exists but STATE.md, ROADMAP.md or phases/ is missing')
    state = frontmatter(state_md.read_text(encoding='utf-8'))
    if not state.get('milestone'):
        raise Unrecognized('STATE.md frontmatter has no "milestone" field')
    version = (state['milestone'] + ' ' + state.get('milestone_name', '')).strip()
    current = state.get('current_phase', '')
    roadmap = roadmap_md.read_text(encoding='utf-8')
    checked = {}
    for m in re.finditer(r'^- \[( |x)\] \**Phase ([\d.]+): (.+?)(?:\*\*|\s+-\s|\s+\(|$)', roadmap, re.M):
        checked[m.group(2)] = (m.group(1) == 'x', m.group(3).strip())
    plan_lines = {m.group(1): m.group(2) for m in re.finditer(
        r'^\s*- \[[ x]\] ([\d.]+-\d+)-PLAN\.md\s+[\u2014-]+\s+(.+)$', roadmap, re.M)}
    rel = lambda p: p.relative_to(root).as_posix()
    started = set(started_ids)
    items, phases = [], []
    for d in sorted((pl / 'phases').iterdir(), key=lambda p: [int(x) if x.isdigit() else 0 for x in re.split(r'[.-]', p.name.split('-')[0])]):
        m = re.match(r'(\d+(?:\.\d+)*)-(.+)', d.name)
        if not d.is_dir() or not m or m.group(1).startswith('999'):
            continue  # 999.x = backlog, not scheduled work
        num, slug = m.group(1), m.group(2)
        done, name = checked.get(num, (False, slug.replace('-', ' ').title()))
        phases.append(num)
        phase = {'id': num, 'type': 'epic', 'summary': 'Phase %s: %s' % (num, short_title(name, 50)),
                 'parent': None, 'version': version, 'path': rel(d),
                 'sections': epic_sections(roadmap, num), 'created_at': starts.get(num), 'done_at': None}
        # A phase-scoped commit is planning (docs(40): create phase plan), not work.
        children, any_started = [], num == current
        for p in sorted(d.glob('%s-*-PLAN.md' % num)):
            pid = p.name[:-len('-PLAN.md')]
            text = p.read_text(encoding='utf-8')
            summ = p.with_name(pid + '-SUMMARY.md')
            stext = summ.read_text(encoding='utf-8') if summ.exists() else ''
            pdone = bool(stext) and frontmatter(stext).get('status', 'complete') == 'complete'
            pstart = pdone or pid in started
            any_started = any_started or pstart
            cat = 'done' if pdone else 'indeterminate' if pstart else 'new'
            stitle = (re.search(r'^#\s+Phase [\d.]+ Plan \d+:\s*(.+?)(?:\s+Summary)?\s*$', stext, re.M) or [None, ''])[1]
            title = stitle or plan_lines.get(pid) or tag(text, 'objective') or 'Plan %s' % pid
            sig = plan_signals(text)
            children.append({'id': pid, 'type': 'story', 'summary': short_title(title),
                             'parent': num, 'cat': cat, 'version': version, 'path': rel(p),
                             'sections': story_sections(text, stext),
                             'created_at': add_dates.get(rel(p)), 'done_at': add_dates.get(rel(summ)) if pdone else None,
                             'signals': sig, 'score': plan_score(sig)})
            for i, block in enumerate(re.findall(r'<task\b[^>]*>.*?</task>', text, re.S), 1):
                children.append({'id': '%s-t%d' % (pid, i), 'type': 'subtask',
                                 'summary': short_title(tag(block, 'name')) or 'Task %d' % i,
                                 'parent': pid, 'cat': 'done' if pdone else 'new',
                                 'version': None, 'path': rel(p), 'sections': subtask_sections(block),
                                 'created_at': None, 'done_at': None})
        phase['cat'] = 'done' if done else 'indeterminate' if any_started else 'new'
        items.append(phase)
        items.extend(children)

    def epic_for(created_at, is_open):
        """The phase being worked on when the item appeared: the current phase for open
        work, else the latest phase that had started by then (the first phase if none had)."""
        if not phases:
            return None
        if is_open and current in phases:
            return current
        t = parse_ts(created_at)
        began = [(parse_ts(starts[p]), p) for p in phases if starts.get(p)]
        prior = [x for x in began if t and x[0] <= t]
        return max(prior)[1] if prior else (current if current in phases and not t else phases[0])

    # Work items that live outside phases.
    for sub, cat in (('debug', 'indeterminate'), ('debug/resolved', 'done')):
        for f in sorted((pl / sub).glob('*.md')) if (pl / sub).is_dir() else []:
            text = f.read_text(encoding='utf-8')
            trig = frontmatter(text).get('trigger') or f.stem.replace('-', ' ')
            created = add_dates.get('.planning/debug/%s.md' % f.stem) or add_dates.get(rel(f))
            items.append({'id': 'debug-' + f.stem, 'type': 'bug', 'summary': short_title(trig),
                          'parent': epic_for(created, cat != 'done'), 'cat': cat, 'version': version,
                          'path': rel(f), 'sections': bug_sections(text), 'created_at': created,
                          'done_at': add_dates.get(rel(f)) if cat == 'done' else None})
    qdir = pl / 'quick'
    for q in sorted(qdir.iterdir()) if qdir.is_dir() else []:
        if not q.is_dir():
            continue
        m = re.match(r'(\d{6}-[a-z0-9]{3})-(.+)', q.name)
        qid, slug = (m.group(1), m.group(2)) if m else (q.name, re.sub(r'^\d{6,8}-', '', q.name))
        plan = next(iter(sorted(q.glob('*PLAN.md'))), None)
        summ = next(iter(sorted(q.glob('*SUMMARY.md'))), None)
        ptext = plan.read_text(encoding='utf-8') if plan else ''
        stext = summ.read_text(encoding='utf-8') if summ else ''
        heading = (re.search(r'^#\s+(.+)$', stext, re.M) or [None, ''])[1].strip()
        heading = '' if heading.lower() in ('summary', 'quick task summary') else heading
        title = tag(ptext, 'objective') or heading or slug.replace('-', ' ')
        created = min((add_dates[rel(x)] for x in q.iterdir() if rel(x) in add_dates), default=None)
        secs = story_sections(ptext, stext) if ptext else [s for s in (_sec('Outcome', body_after_frontmatter(stext)[:1500]),) if s]
        items.append({'id': 'quick-' + qid, 'type': 'task', 'summary': short_title(first_sentence(title, 400)),
                      'parent': epic_for(created, not summ), 'cat': 'done' if summ else 'indeterminate',
                      'version': version, 'path': rel(q), 'sections': secs, 'created_at': created,
                      'done_at': add_dates.get(rel(summ)) if summ else None})
    for sub, cat in (('todos/pending', 'new'), ('todos/completed', 'done')):
        for f in sorted((pl / sub).glob('*.md')) if (pl / sub).is_dir() else []:
            text = f.read_text(encoding='utf-8')
            title = frontmatter(text).get('title') or f.stem.replace('-', ' ')
            created = add_dates.get('.planning/todos/pending/%s.md' % f.stem) or add_dates.get(rel(f))
            items.append({'id': 'todo-' + f.stem, 'type': 'task', 'summary': short_title(title),
                          'parent': epic_for(created, cat != 'done'), 'cat': cat, 'version': version,
                          'path': rel(f), 'sections': [s for s in (_sec('Details', body_after_frontmatter(text).strip()[:3000]),) if s],
                          'created_at': created, 'done_at': add_dates.get(rel(f)) if cat == 'done' else None})
    archived = {f.name.split('-ROADMAP.md')[0] for f in (pl / 'milestones').glob('*-ROADMAP.md')} if (pl / 'milestones').is_dir() else set()
    return items, {'current': version, 'archived': archived}


def started_ids(root, n=3000):
    """GSD ids that have at least one commit, from recent commit subjects."""
    out = set()
    for subj in git('log', '--format=%s', '-n', str(n), cwd=root).splitlines():
        out.update(scope_ids(subj))
    return out


def load_items(root):
    return gsd_items(root, started_ids(root), git_add_dates(root), phase_starts(root))


# --------------------------------------------------------------------------- Jira

ENV_ALIASES = {'JIRA_EMAIL': ('JIRA_EMAIL', 'JIRA_USER_EMAIL', 'JIRA_USERNAME', 'JIRA_USER', 'ATLASSIAN_EMAIL'),
               'JIRA_API_TOKEN': ('JIRA_API_TOKEN', 'JIRA_TOKEN', 'ATLASSIAN_API_TOKEN')}


def dotenv():
    """KEY=value pairs from the repo-root .env, read here so values never pass through a shell."""
    top = git('rev-parse', '--show-toplevel')
    p = Path(top) / '.env' if top else None
    vals = {}
    if p and p.exists():
        for line in p.read_text(encoding='utf-8', errors='replace').splitlines():
            m = re.match(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$', line)
            if m:
                vals[m.group(1)] = m.group(2).strip().strip('"\'')
    return vals


def _env_one(name):
    v = os.environ.get(name)
    if v or os.name != 'nt':
        return v
    try:  # `setx` values are not visible to processes started before it ran
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def env_source(name):
    """(value, where it came from) -- environment first, then the repo's .env."""
    names = ENV_ALIASES.get(name, (name,))
    for n in names:
        v = _env_one(n)
        if v:
            return v, 'environment %s' % n
    dv = dotenv()
    for n in names:
        if dv.get(n):
            return dv[n], '.env %s' % n
    return None, None


def env(name):
    return env_source(name)[0]


def adf(text):
    return {'type': 'doc', 'version': 1, 'content': [
        {'type': 'paragraph', 'content': [{'type': 'text', 'text': line}]}
        for line in text.split('\n') if line.strip()]}


class Jira:
    def __init__(self, site):
        email, token = env('JIRA_EMAIL'), env('JIRA_API_TOKEN')
        if not email or not token:
            raise JiraError('JIRA_EMAIL and/or JIRA_API_TOKEN are not set')
        site = re.sub(r'^https?://', '', site).split('/')[0]
        self.base = 'https://' + site
        self.auth = 'Basic ' + base64.b64encode(('%s:%s' % (email, token)).encode()).decode()

    def req(self, method, path, body=None):
        # ponytail: serial requests, one Retry-After backoff; batch only if backfills get slow
        for attempt in (0, 1):
            r = urllib.request.Request(
                self.base + path, method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={'Authorization': self.auth, 'Accept': 'application/json',
                         'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(r, timeout=30) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt == 0:
                    time.sleep(min(int(e.headers.get('Retry-After') or 5), 60))
                    continue
                raise JiraError('%s %s -> HTTP %s: %s' % (method, path, e.code, e.read().decode('utf-8', 'replace')[:300]))
            except urllib.error.URLError as e:
                raise JiraError('%s %s -> %s' % (method, path, e.reason))

    def project(self, key):
        p = self.req('GET', '/rest/api/3/project/%s' % key)
        types = [t for t in p.get('issueTypes', [])]
        by_name = {t['name'].lower(): t['id'] for t in types}
        meta = {'id': p['id'], 'key': p['key'],
                'subtask': next((t['id'] for t in types if t.get('subtask') or t.get('hierarchyLevel') == -1), None),
                'epic': next((t['id'] for t in types if t.get('hierarchyLevel') == 1), by_name.get('epic')),
                'story': by_name.get('story'), 'task': by_name.get('task'), 'bug': by_name.get('bug')}
        meta['story'] = meta['story'] or meta['task']
        meta['bug'] = meta['bug'] or meta['task']
        meta['points'] = self.points_field(p['key'], meta['story']) if meta['story'] else None
        return meta

    def points_field(self, project_key, type_id):
        """Field id of 'Story Points' (company-managed) or 'Story point estimate' (team-managed)."""
        try:
            r = self.req('GET', '/rest/api/3/issue/createmeta/%s/issuetypes/%s?maxResults=200' % (project_key, type_id))
        except JiraError:
            return None
        for f in r.get('fields') or r.get('values') or []:
            if f.get('name', '').lower() in ('story point estimate', 'story points'):
                return f['fieldId']
        return None

    def comment(self, key, text):
        self.req('POST', '/rest/api/3/issue/%s/comment' % key, {'body': adf(text)})

    def search(self, jql, fields, limit=None):
        out, token = [], None
        while True:
            body = {'jql': jql, 'fields': fields, 'maxResults': min(limit or 100, 100)}
            if token:
                body['nextPageToken'] = token
            r = self.req('POST', '/rest/api/3/search/jql', body)
            out.extend(r.get('issues', []))
            token = r.get('nextPageToken')
            if not token or r.get('isLast') or (limit and len(out) >= limit):
                return out[:limit] if limit else out

    def synced(self, project, points_field=None):
        """{gsd_id: {key, cat, points}} for every issue jira-sync created in this project."""
        have = {}
        fields = ['labels', 'status', 'parent'] + ([points_field] if points_field else [])
        for i in self.search('project = "%s" AND labels = "%s"' % (project, SYNC_LABEL), fields):
            for label in i['fields']['labels']:
                if label.startswith('gsd-'):
                    have[label[4:]] = {'key': i['key'], 'cat': i['fields']['status']['statusCategory']['key'],
                                       'points': i['fields'].get(points_field) if points_field else None,
                                       'parent': (i['fields'].get('parent') or {}).get('key')}
        return have

    def category(self, key):
        return self.req('GET', '/rest/api/3/issue/%s?fields=status' % key)['fields']['status']['statusCategory']['key']

    def move(self, key, target):
        """Walk the issue forward to `target` category. Never moves backwards."""
        for _ in range(3):
            cur = self.category(key)
            if CATS.index(cur) >= CATS.index(target):
                return True
            options = self.req('GET', '/rest/api/3/issue/%s/transitions' % key)['transitions']
            t = pick_transition(options, target) or pick_transition(options, CATS[CATS.index(cur) + 1])
            if not t:
                return False
            self.req('POST', '/rest/api/3/issue/%s/transitions' % key, {'transition': {'id': t['id']}})
        return self.category(key) == target

    def versions(self, project_key):
        return {v['name']: v for v in self.req('GET', '/rest/api/3/project/%s/versions' % project_key)}


def pick_transition(options, target):
    return next((t for t in options if t.get('to', {}).get('statusCategory', {}).get('key') == target), None)


def _t(text, code=False):
    node = {'type': 'text', 'text': text[:3000]}
    if code:
        node['marks'] = [{'type': 'code'}]
    return node


def adf_doc(sections, footer):
    """Atlassian document from [(heading, kind, value)]; kind: p | ul | ol | code | pre."""
    content = []
    for heading, kind, value in sections:
        content.append({'type': 'heading', 'attrs': {'level': 3}, 'content': [_t(heading)]})
        if kind == 'p':
            for para in re.split(r'\n\s*\n', value.strip()):
                if para.strip():
                    content.append({'type': 'paragraph', 'content': [_t(' '.join(para.split()))]})
        elif kind == 'pre':
            content.append({'type': 'codeBlock', 'content': [_t(value.strip())]})
        else:
            items = [x for x in value if str(x).strip()][:30]
            content.append({'type': 'orderedList' if kind == 'ol' else 'bulletList', 'content': [
                {'type': 'listItem', 'content': [{'type': 'paragraph', 'content': [_t(str(x).strip(), kind == 'code')]}]}
                for x in items]})
    content.append({'type': 'paragraph', 'content': [{'type': 'text', 'text': footer, 'marks': [{'type': 'em'}]}]})
    return {'type': 'doc', 'version': 1, 'content': content}


def item_description(it):
    label = {'epic': 'GSD phase', 'story': 'GSD plan', 'subtask': 'GSD plan task', 'bug': 'GSD debug session',
             'task': 'GSD quick task / todo'}.get(it['type'], 'GSD item')
    return adf_doc(it.get('sections') or [], '%s %s -- source %s -- synced by jira-sync' % (label, it['id'], it['path']))


# --------------------------------------------------------------------- sprints

HISTORY_SPRINT = 'Sprint 0 (before Jira)'


def add_month(dt):
    y, m = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    return dt.replace(year=y, month=m, day=min(dt.day, calendar.monthrange(y, m)[1]))


def next_sprint_name(name):
    m = re.search(r'(\d+)\s*$', name)
    return name[:m.start()] + str(int(m.group(1)) + 1) if m else name + ' 2'


def jira_ts(dt):
    return dt.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


class Sprints:
    """The project's sprint board: find it, keep a sprint running, add issues to sprints."""

    def __init__(self, j, project):
        self.j, self.board, self.sprints = j, None, []
        for b in j.req('GET', '/rest/agile/1.0/board?projectKeyOrId=%s' % project).get('values', []):
            try:
                self.sprints = self._list(b['id'])
                self.board = b['id']
                break
            except JiraError:
                continue  # board without sprints

    def _list(self, board):
        out, start = [], 0
        while True:
            r = self.j.req('GET', '/rest/agile/1.0/board/%s/sprint?startAt=%d' % (board, start))
            out.extend(r.get('values', []))
            if r.get('isLast', True):
                return out
            start += len(r.get('values', []))

    def by_state(self, state):
        return sorted((s for s in self.sprints if s['state'] == state and s['name'] != HISTORY_SPRINT),
                      key=lambda s: s.get('startDate') or s['name'])

    def history(self):
        return next((s for s in self.sprints if s['name'] == HISTORY_SPRINT), None)

    def _update(self, sid, **fields):
        r = self.j.req('POST', '/rest/agile/1.0/sprint/%s' % sid, fields)
        for s in self.sprints:
            if s['id'] == sid:
                s.update(r or fields)
        return r

    def _create(self, name, start, end):
        s = self.j.req('POST', '/rest/agile/1.0/sprint', {'name': name, 'originBoardId': self.board,
                                                          'startDate': jira_ts(start), 'endDate': jira_ts(end)})
        self.sprints.append(s)
        return s

    def add(self, sid, keys):
        for i in range(0, len(keys), 50):
            self.j.req('POST', '/rest/agile/1.0/sprint/%s/issue' % sid, {'issues': keys[i:i + 50]})

    def ensure_active(self, out, dry=False):
        """Keep exactly one calendar-month sprint running; roll over any that have ended,
        carrying unfinished work into the next one. Returns the active sprint or None."""
        if not self.board:
            return None
        now = datetime.datetime.now(datetime.timezone.utc)
        active = (self.by_state('active') or [None])[0]
        while active and parse_ts(active.get('endDate')) and parse_ts(active['endDate']) <= now:
            old_end = parse_ts(active['endDate'])
            nxt = (self.by_state('future') or [None])[0]
            if dry:
                out('would close %s and start %s' % (active['name'], nxt['name'] if nxt else next_sprint_name(active['name'])))
                return active
            carry = [i['key'] for i in self.j.search('sprint = %s AND statusCategory != Done AND issuetype not in subTaskIssueTypes()'
                                                     % active['id'], ['summary'])]
            if not nxt:
                nxt = self._create(next_sprint_name(active['name']), old_end, add_month(old_end))
            self._update(active['id'], state='closed')
            out('closed sprint %s' % active['name'])
            self.close_history(out)
            start = parse_ts(nxt.get('startDate')) or old_end
            self._update(nxt['id'], state='active', startDate=jira_ts(start),
                         endDate=nxt.get('endDate') or jira_ts(add_month(start)))
            out('started sprint %s' % nxt['name'])
            if carry:
                self.add(nxt['id'], carry)
                out('carried %d unfinished issues into %s' % (len(carry), nxt['name']))
            active = nxt
        if not active:
            nxt = (self.by_state('future') or [None])[0]
            if dry:
                out('would start %s' % (nxt['name'] if nxt else 'a new monthly sprint'))
                return None
            self.close_history(out)
            if not nxt:
                start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                nxt = self._create('Sprint 1', start, add_month(start))
            start = parse_ts(nxt.get('startDate')) or now
            self._update(nxt['id'], state='active', startDate=jira_ts(start),
                         endDate=nxt.get('endDate') or jira_ts(add_month(start)))
            out('started sprint %s' % nxt['name'])
            active = nxt
        return active

    def close_history(self, out):
        """Team-managed boards allow one active sprint, so the history sprint is filled
        while still 'future' and started+closed at the next moment nothing else is active."""
        h = self.history()
        if h and h['state'] == 'future':
            self._update(h['id'], state='active', startDate=h['startDate'], endDate=h['endDate'])
            self._update(h['id'], state='closed')
            out('closed %s' % HISTORY_SPRINT)

    def ensure_history(self, first_start, earliest):
        h = self.history()
        if not h:
            h = self._create(HISTORY_SPRINT, earliest, first_start)
        return h


def assign_sprints(j, sp, project, meta, have, items, out, dry=False):
    """Every Story/Task/Bug sits in a sprint: open work in the active sprint, finished work
    in the sprint whose dates cover when it finished, pre-Jira work in the history sprint."""
    warnings = []
    if not sp.board:
        return ['no sprint board found for %s -- stories, tasks and bugs left out of sprints' % project]
    active = sp.ensure_active(out, dry)
    if not active:
        return warnings + ['no active sprint -- items stay in the backlog until one exists']
    types = ','.join(str(meta[t]) for t in ('story', 'task', 'bug') if meta.get(t))
    loose = j.search('project = "%s" AND labels = "%s" AND issuetype in (%s) AND sprint is EMPTY'
                     % (project, SYNC_LABEL, types), ['labels'])
    by_key = {v['key']: gid for gid, v in have.items()}
    item_by_id = {it['id']: it for it in items}
    real = sp.by_state('active') + sp.by_state('future') + sp.by_state('closed')
    first_start = min((parse_ts(s['startDate']) for s in real if s.get('startDate')), default=None)
    plan, skipped = {}, 0
    for i in loose:
        it = item_by_id.get(by_key.get(i['key']))
        done_at = parse_ts(it['done_at']) if it and it.get('done_at') else None
        target = active
        if done_at and first_start and done_at < first_start:
            target = 'history'
        elif done_at:
            cover = next((s for s in real if s.get('startDate') and parse_ts(s['startDate']) <= done_at
                          < parse_ts(s.get('endDate') or s['startDate'])), None)
            if cover and cover['state'] == 'closed':
                skipped += 1  # Jira does not accept issues into a closed sprint
                continue
            target = cover or active
        plan.setdefault('history' if target == 'history' else target['id'], []).append(i['key'])
    if 'history' in plan:
        earliest = min(parse_ts(item_by_id[by_key[k]]['done_at']) for k in plan['history'])
        if dry:
            out('would put %d issues finished before %s into %s' % (len(plan['history']), first_start.date(), HISTORY_SPRINT))
        else:
            h = sp.ensure_history(first_start, earliest)
            plan[h['id']] = plan.pop('history')
            if h['state'] == 'future':
                warnings.append('%s holds %d pre-Jira issues; it closes itself at the next sprint rollover '
                                '(team-managed boards allow one active sprint)' % (HISTORY_SPRINT, len(plan[h['id']])))
    names = {s['id']: s['name'] for s in sp.sprints}
    for sid, keys in plan.items():
        if sid == 'history':
            continue
        if not dry:
            sp.add(sid, keys)
        out('%s %d issues to sprint %s' % ('would add' if dry else 'added', len(keys), names.get(sid, sid)))
    if skipped:
        warnings.append('%d finished issues belong to already-closed sprints and were left out' % skipped)
    return warnings


# ---------------------------------------------------------------------- reconcile

def reconcile(root, cfg, dry=False, out=print, refresh_text=False):
    project = cfg['project']
    items, ms = load_items(root)
    if ms is None:
        out('no .planning/ directory -- nothing to reconcile (use start/done for non-GSD work)')
        return 0
    j = Jira(cfg['site'])
    meta = j.project(project)
    pf = meta['points']
    have = j.synced(project, pf)
    version_id, created, moved, sized, fixed, warnings = None, 0, 0, 0, 0, []
    anchors = (cfg.get('estimation') or {}).get('anchors') or []
    if any(it['type'] == 'story' for it in items):
        if not pf:
            warnings.append('story points skipped: no Story Points / Story point estimate field on %s stories' % project)
        elif len(anchors) < 2:
            warnings.append('story points skipped: no anchors yet -- run `anchors` and accept a set')

    def size(it):
        if it['type'] != 'story' or not pf or len(anchors) < 2:
            return None, None
        pts, why = size_against(it['score'], anchors)
        return pts, ('Story points %d (automatic first guess): %s. Signals: %s. '
                     'Review with /jira-sync estimate.' % (pts, why, describe_signals(it['signals']))) if pts else None

    try:
        vers = j.versions(project)
        if ms['current'] not in vers:
            if not dry:
                vers[ms['current']] = j.req('POST', '/rest/api/3/version', {'name': ms['current'], 'projectId': int(meta['id'])})
            out('%s fix version %s' % ('would create' if dry else 'created', ms['current']))
        version_id = vers.get(ms['current'], {}).get('id')
        for name, v in vers.items():
            if name.split(' ')[0] in ms['archived'] and not v.get('released'):
                if not dry:
                    j.req('PUT', '/rest/api/3/version/%s' % v['id'], {'released': True})
                out('%sreleased fix version %s' % ('would mark ' if dry else '', name))
    except JiraError as e:
        warnings.append('fix versions skipped (are Releases enabled on %s?): %s' % (project, e))

    for it in items:
        type_id = meta.get(it['type'])
        if not type_id:
            warnings.append('%s %s skipped: project %s has no %s issue type' % (it['type'], it['id'], project, it['type']))
            continue
        if it['type'] in ('story', 'task', 'bug') and not it['parent']:
            warnings.append('%s %s has no epic to go under (no phases in this milestone)' % (it['type'], it['id']))
        parent = have.get(it['parent']) if it['parent'] else None
        cur = have.get(it['id'])
        if not cur:
            if it['parent'] and not parent:
                warnings.append('%s skipped: parent %s was not created' % (it['id'], it['parent']))
                continue
            pts, note = size(it)
            if dry:
                out('would create %-7s %-18s %s' % (it['type'], it['id'], it['summary']))
                have[it['id']] = {'key': '(new %s)' % it['id'], 'cat': 'new', 'points': pts, 'parent': parent and parent['key']}
                created += 1
                if pts:
                    out('would size   %-18s %d pts' % (it['id'], pts))
                    sized += 1
                if it['cat'] != 'new':
                    out('would move   %-18s -> %s' % (it['id'], CAT_NAMES[it['cat']]))
                    moved += 1
                continue
            fields = {'project': {'key': project}, 'issuetype': {'id': type_id},
                      'summary': it['summary'][:254], 'labels': [SYNC_LABEL, 'gsd-' + it['id']],
                      'description': item_description(it)}
            if parent:
                fields['parent'] = {'key': parent['key']}
            if it['version'] and version_id:
                fields['fixVersions'] = [{'id': version_id}]
            if pts:
                fields[pf] = pts
                fields['labels'].append(AUTO_LABEL)
            try:
                key = j.req('POST', '/rest/api/3/issue', {'fields': fields})['key']
            except JiraError as e:
                if 'fixVersions' not in fields:
                    raise
                del fields['fixVersions']  # field not on this issue type's screen
                key = j.req('POST', '/rest/api/3/issue', {'fields': fields})['key']
                warnings.append('%s created without fix version: %s' % (key, e))
            cur = have[it['id']] = {'key': key, 'cat': 'new', 'points': pts, 'parent': parent and parent['key']}
            created += 1
            out('created %s %s (%s)%s' % (key, it['id'], it['type'], ' %d pts' % pts if pts else ''))
            if pts:
                j.comment(key, note)
                sized += 1
        else:
            update, labels = {}, []
            if refresh_text:
                update.update(summary=it['summary'][:254], description=item_description(it))
            if parent and not cur.get('parent') and it['type'] != 'subtask':
                update['parent'] = {'key': parent['key']}  # only fills a missing epic; never re-parents
            pts, note = size(it) if cur.get('points') is None else (None, None)
            if pts:
                update[pf] = pts  # points a person set are never overwritten
                labels = [{'add': AUTO_LABEL}]
            if update:
                if dry:
                    out('would update %-18s %s %s' % (it['id'], cur['key'], ', '.join(
                        'points=%d' % pts if k == pf else 'epic=%s' % parent['key'] if k == 'parent' else k for k in update)))
                else:
                    j.req('PUT', '/rest/api/3/issue/%s' % cur['key'], {'fields': update, 'update': {'labels': labels}} if labels
                          else {'fields': update})
                    if pts:
                        j.comment(cur['key'], note)
                    out('updated %s %s (%s)' % (cur['key'], it['id'], ', '.join(
                        'points' if k == pf else 'epic' if k == 'parent' else k for k in update)))
                if 'parent' in update:
                    cur['parent'] = parent['key']
                    fixed += 1
                if pts:
                    cur['points'] = pts
                    sized += 1
        if CATS.index(it['cat']) > CATS.index(cur['cat']):
            if dry:
                out('would move   %-18s %s -> %s' % (it['id'], cur['key'], CAT_NAMES[it['cat']]))
            elif j.move(cur['key'], it['cat']):
                out('moved %s %s -> %s' % (cur['key'], it['id'], CAT_NAMES[it['cat']]))
            else:
                warnings.append('%s could not reach %s: no transition in the workflow' % (cur['key'], CAT_NAMES[it['cat']]))
                continue
            moved += 1
            cur['cat'] = it['cat']
    try:
        warnings += assign_sprints(j, Sprints(j, project), project, meta, have, items, out, dry)
    except JiraError as e:
        warnings.append('sprint assignment failed: %s' % e)
    if not dry:
        write_json(git_path(root, 'jira-sync-map.json'), {k: v['key'] for k, v in have.items()})
    for w in warnings:
        out('WARNING ' + w)
    out(('dry run: %d to create, %d to move, %d to size, %d to put under an epic' if dry
         else 'done: %d created, %d moved, %d sized, %d put under an epic') % (created, moved, sized, fixed)
        + ', %d warnings' % len(warnings))
    return 1 if warnings else 0


def locked_reconcile(root, cfg):
    """Run reconcile under a lock; a commit that lands mid-run sets a dirty flag
    so the holder runs once more instead of the change being dropped."""
    lock, dirty = git_path(root, 'jira-sync.lock'), git_path(root, 'jira-sync.dirty')
    log = git_path(root, 'jira-sync.log')
    if lock.exists() and time.time() - lock.stat().st_mtime > LOCK_STALE_S:
        lock.unlink()
    try:
        os.close(os.open(str(lock), os.O_CREAT | os.O_EXCL))
    except FileExistsError:
        dirty.touch()
        return
    try:
        with open(log, 'a', encoding='utf-8') as fh:
            def out(line):
                fh.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), line))
                fh.flush()
                os.utime(lock)  # a long run keeps its lock fresh so it is never judged stale
            while True:
                if dirty.exists():
                    dirty.unlink()
                try:
                    reconcile(root, cfg, out=out)
                except (JiraError, Unrecognized) as e:
                    out('ERROR %s' % e)
                if not dirty.exists():
                    break
    finally:
        lock.unlink()


# ------------------------------------------------------------------ git hooks

def hook_body(name, args):
    pre = '"$(dirname "$0")/%s.pre-jira-sync"' % name
    return ('#!/bin/sh\n# %s -- rerun `jira_sync.py init` to refresh\n'
            '[ -x %s ] && { %s "$@" || exit $?; }\n'
            'PY=python3; command -v python3 >/dev/null 2>&1 || PY=python\n'
            '"$PY" "%s" %s || true\n') % (HOOK_MARK, pre, pre, STABLE.as_posix(), args)


def install_hooks(root):
    hooks = git_path(root, 'hooks')
    hooks.mkdir(parents=True, exist_ok=True)
    for name, args in (('commit-msg', 'commit-msg "$1"'), ('post-commit', 'post-commit')):
        h = hooks / name
        if h.exists() and HOOK_MARK not in h.read_text(encoding='utf-8', errors='replace'):
            h.replace(hooks / (name + '.pre-jira-sync'))
        with open(h, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(hook_body(name, args))
        h.chmod(0o755)
    return hooks


def sync_stable_copy():
    """Git hooks call a stable copy so plugin updates (new versioned path) do not break them."""
    me = Path(__file__).resolve()
    if me == STABLE.resolve():
        return
    STABLE.parent.mkdir(parents=True, exist_ok=True)
    if not STABLE.exists() or STABLE.read_bytes() != me.read_bytes():
        shutil.copyfile(me, STABLE)


# ------------------------------------------------------------------- commands

def cmd_init(a):
    root = repo_root()
    cfg = load_config(root) or {}
    cfg.update({k: v for k, v in (('site', a.site), ('project', a.project)) if v})
    if a.auto_push is not None:
        cfg['auto_push'] = a.auto_push == 'on'
    if not cfg.get('site') or not cfg.get('project'):
        raise SystemExit('init needs --site and --project the first time')
    write_json(root / CONFIG, cfg)
    sync_stable_copy()
    hooks = install_hooks(root)
    print('wrote %s; installed commit-msg + post-commit in %s' % (CONFIG, hooks))
    print('commit %s so other clones pick up the Jira target' % CONFIG)


def need_config(root):
    cfg = load_config(root)
    if not cfg:
        raise SystemExit('jira-sync: no %s in %s -- run `jira_sync.py init` first' % (CONFIG, root))
    return cfg


def cmd_doctor(a):
    root, fails = repo_root(), 0

    def check(ok, label, detail=''):
        nonlocal fails
        fails += 0 if ok else 1
        print('%s %s%s' % ('OK  ' if ok else 'FAIL', label, (' -- ' + detail) if detail else ''))

    cfg = load_config(root)
    check(bool(cfg), 'config %s' % CONFIG, json.dumps(cfg) if cfg else 'missing, run init')
    for n in ('JIRA_EMAIL', 'JIRA_API_TOKEN'):
        v, src = env_source(n)
        check(bool(v), 'credential %s' % n, 'from ' + src if v else 'not set (environment or repo .env)')
    jira_keys = sorted(k for k in dotenv() if 'JIRA' in k.upper() or 'ATLASSIAN' in k.upper())
    if jira_keys:
        print('--   .env Jira-related key names: %s' % ', '.join(jira_keys))
    url = dotenv().get('JIRA_URL') or dotenv().get('JIRA_BASE_URL') or ''
    if cfg and url and cfg['site'] not in url:
        print('--   NOTE .env JIRA_URL host differs from %s site %s' % (CONFIG, cfg['site']))
    hooks = git_path(root, 'hooks')
    for n in ('commit-msg', 'post-commit'):
        h = hooks / n
        check(h.exists() and HOOK_MARK in h.read_text(encoding='utf-8', errors='replace'), 'git hook %s' % n, str(h))
    check(STABLE.exists(), 'hook script copy', str(STABLE))
    if cfg and env('JIRA_EMAIL') and env('JIRA_API_TOKEN'):
        try:
            j = Jira(cfg['site'])
            me = j.req('GET', '/rest/api/3/myself')
            check(True, 'auth', me.get('displayName', '?'))
            meta = j.project(cfg['project'])
            missing = [t for t in ('epic', 'story', 'task', 'bug', 'subtask') if not meta.get(t)]
            check(not missing, 'project %s issue types' % cfg['project'], 'missing: ' + ', '.join(missing) if missing else 'epic/story/task/bug/sub-task')
            try:
                check(True, 'fix versions', '%d existing' % len(j.versions(cfg['project'])))
            except JiraError as e:
                check(False, 'fix versions', str(e))
            check(True, 'synced issues', '%d tagged %s' % (len(j.synced(cfg['project'])), SYNC_LABEL))
        except JiraError as e:
            check(False, 'jira', str(e))
    try:
        items, ms = load_items(root)
        if ms is None:
            print('--   no .planning/ (non-GSD repo): start/done only')
        else:
            counts = {}
            for it in items:
                counts[it['type']] = counts.get(it['type'], 0) + 1
            check(True, 'GSD parse', '%s; %s' % (ms['current'], ', '.join('%d %s' % (v, k) for k, v in sorted(counts.items()))))
    except Unrecognized as e:
        check(False, 'GSD parse', 'unrecognized format: %s' % e)
    return 1 if fails else 0


def cmd_reconcile(a):
    root = repo_root()
    cfg = need_config(root)
    if a.dry_run:
        return reconcile(root, cfg, dry=True, refresh_text=a.refresh_text)
    # Share the background sync's lock so a commit-triggered run never races this one.
    lock = git_path(root, 'jira-sync.lock')
    for _ in range(LOCK_STALE_S):
        if lock.exists() and time.time() - lock.stat().st_mtime > LOCK_STALE_S:
            lock.unlink()
        try:
            os.close(os.open(str(lock), os.O_CREAT | os.O_EXCL))
            break
        except FileExistsError:
            time.sleep(1)
    else:
        raise SystemExit('jira-sync: another sync is still running (%s)' % lock)
    try:
        def out(line):
            print(line, flush=True)
            os.utime(lock)
        return reconcile(root, cfg, out=out, refresh_text=a.refresh_text)
    finally:
        lock.unlink()


def cmd_epics(a):
    """Open epics to file new work under."""
    root = repo_root()
    cfg = need_config(root)
    j = Jira(cfg['site'])
    meta = j.project(cfg['project'])
    for i in j.search('project = "%s" AND issuetype = %s AND statusCategory != Done ORDER BY key DESC'
                      % (cfg['project'], meta['epic']), ['summary', 'status'], limit=50):
        print('%s | %s | %s' % (i['key'], i['fields']['status']['name'], i['fields']['summary']))
    return 0


def cmd_commit_msg(a):
    try:
        root = repo_root()
        cfg = load_config(root)
        path = Path(a.file)
        msg = path.read_text(encoding='utf-8')
        if not cfg or not msg.strip() or msg.startswith(('Merge ', 'fixup!', 'squash!')):
            return 0
        cache = read_json(git_path(root, 'jira-sync-map.json'), {})
        active_f = git_path(root, 'jira-sync-active')
        active = active_f.read_text(encoding='utf-8').strip() if active_f.exists() else None
        key = resolve_key(msg, git('branch', '--show-current', cwd=root), cache, active, cfg['project'])
        if key and not key.startswith('('):
            path.write_text(tag_message(msg, key), encoding='utf-8')
    except Exception as e:  # never block a commit
        print('jira-sync: commit-msg skipped (%s)' % e, file=sys.stderr)
    return 0


def cmd_post_commit(a):
    root = repo_root()
    cfg = load_config(root)
    if not cfg or not (cfg.get('auto_push') or (root / '.planning').is_dir()):
        return 0
    args = [sys.executable, str(Path(__file__).resolve()), '_reconcile-locked', str(root)]
    kw = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL, 'cwd': str(root)}
    if os.name == 'nt':
        kw['creationflags'] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw['start_new_session'] = True
    subprocess.Popen(args, **kw)  # keep commits fast; result goes to .git/jira-sync.log
    return 0


def auto_push(root):
    """Push the current branch so the GitHub for Jira app sees new commits. Never forces;
    skips mid-rebase/merge and detached HEAD; a failure is logged and retried next commit."""
    gd = Path(git('rev-parse', '--absolute-git-dir', cwd=root))
    if any((gd / n).exists() for n in ('rebase-merge', 'rebase-apply', 'MERGE_HEAD', 'CHERRY_PICK_HEAD')):
        return 'push skipped: rebase/merge in progress'
    branch = git('branch', '--show-current', cwd=root)
    if not branch:
        return 'push skipped: detached HEAD'
    if not git('remote', cwd=root):
        return 'push skipped: no remote'
    has_upstream = bool(git('rev-parse', '--abbrev-ref', '%s@{upstream}' % branch, cwd=root))
    cmd = ['git', 'push', '--quiet'] + ([] if has_upstream else ['-u', 'origin', branch])
    r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=120)
    return 'pushed %s' % branch if r.returncode == 0 else 'ERROR push failed: %s' % (r.stderr.strip().splitlines() or ['?'])[-1]


def cmd_reconcile_locked(a):
    root = Path(a.root)
    cfg = load_config(root)
    if not cfg:
        return 0
    if cfg.get('auto_push'):
        try:
            msg = auto_push(root)
        except (subprocess.SubprocessError, OSError) as e:
            msg = 'ERROR push failed: %s' % e
        with open(git_path(root, 'jira-sync.log'), 'a', encoding='utf-8') as fh:
            fh.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg))
    if (root / '.planning').is_dir():
        locked_reconcile(root, cfg)
    return 0


def jql_text(s):
    return s.replace('\\', '\\\\').replace('"', '\\"')


def cmd_search(a):
    root = repo_root()
    cfg = need_config(root)
    j = Jira(cfg['site'])
    jql = 'project = "%s" AND statusCategory != Done AND text ~ "%s" ORDER BY updated DESC' % (cfg['project'], jql_text(a.text))
    issues = j.search(jql, ['summary', 'status', 'issuetype'], limit=10)
    for i in issues:
        f = i['fields']
        print('%s | %s | %s | %s' % (i['key'], f['issuetype']['name'], f['status']['name'], f['summary']))
    if not issues:
        print('no open matches')
    return 0


def set_active(root, key):
    git_path(root, 'jira-sync-active').write_text(key + '\n', encoding='utf-8')


def start_issue(root, cfg, j, key, branch, summary=None):
    ok = j.move(key, 'indeterminate')
    set_active(root, key)
    print('%s is now the current issue%s' % (key, '' if ok else ' (WARNING: could not move it to In Progress)'))
    if branch:
        slug = re.sub(r'[^a-z0-9]+', '-', (summary or '').lower()).strip('-')[:40]
        name = '%s-%s' % (key, slug) if slug else key
        r = subprocess.run(['git', 'switch', '-c', name], cwd=root, capture_output=True, text=True)
        print('branch %s' % name if r.returncode == 0 else 'WARNING branch not created: ' + r.stderr.strip())


def cmd_create(a):
    root = repo_root()
    cfg = need_config(root)
    j = Jira(cfg['site'])
    meta = j.project(cfg['project'])
    type_id = meta.get(a.type)
    if not type_id:
        raise SystemExit('project %s has no %s issue type' % (cfg['project'], a.type))
    if a.type in ('story', 'task', 'bug') and not a.parent:
        raise SystemExit('every story, task and bug needs an epic: pass --parent <EPIC KEY> (see `epics`)')
    if len(a.summary) > 70:
        raise SystemExit('summary is %d characters; keep it under 70 and put the detail in --description' % len(a.summary))
    fields = {'project': {'key': cfg['project']}, 'issuetype': {'id': type_id}, 'summary': a.summary}
    if a.description:
        fields['description'] = adf_doc([(h.strip(), 'p', b.strip()) for h, b in
                                         re.findall(r'^##\s*(.+?)\n(.*?)(?=^##|\Z)', a.description, re.M | re.S)]
                                        or [('Details', 'p', a.description)], 'created by jira-sync')
    if a.parent:
        fields['parent'] = {'key': a.parent}
    if a.points is not None:
        if a.points not in FIB or not meta['points']:
            raise SystemExit('points must be one of %s and the project needs a story points field' % FIB)
        if not a.reason:
            raise SystemExit('--points needs --reason naming the anchors it was compared against')
        fields[meta['points']] = a.points
        fields['labels'] = [REVIEWED_LABEL]
    key = j.req('POST', '/rest/api/3/issue', {'fields': fields})['key']
    print('created %s (%s) %s/browse/%s' % (key, a.type, j.base, key))
    if a.points is not None:
        j.comment(key, 'Story points %d (sized against anchors): %s' % (a.points, a.reason))
    if a.type in ('story', 'task', 'bug'):
        sp = Sprints(j, cfg['project'])
        active = sp.ensure_active(print)
        if active:
            sp.add(active['id'], [key])
            print('added to sprint %s' % active['name'])
        else:
            print('WARNING no active sprint -- %s left in the backlog' % key)
    if a.start:
        start_issue(root, cfg, j, key, a.branch, a.summary)
    return 0


def cmd_use(a):
    root = repo_root()
    cfg = need_config(root)
    j = Jira(cfg['site'])
    summary = j.req('GET', '/rest/api/3/issue/%s?fields=summary' % a.key)['fields']['summary']
    start_issue(root, cfg, j, a.key, a.branch, summary)
    return 0


def cmd_done(a):
    root = repo_root()
    cfg = need_config(root)
    active_f = git_path(root, 'jira-sync-active')
    active = active_f.read_text(encoding='utf-8').strip() if active_f.exists() else None
    key = a.key or active
    if not key:
        raise SystemExit('no key given and no current issue')
    ok = Jira(cfg['site']).move(key, 'done')
    if key == active:
        active_f.unlink()
    print('%s -> Done' % key if ok else 'WARNING %s could not reach Done: no transition in the workflow' % key)
    return 0 if ok else 1


def cmd_status(a):
    root = repo_root()
    cfg = need_config(root)
    active_f = git_path(root, 'jira-sync-active')
    print('jira: https://%s project %s' % (cfg['site'], cfg['project']))
    print('current issue: %s' % (active_f.read_text(encoding='utf-8').strip() if active_f.exists() else 'none'))
    print('cached GSD mappings: %d' % len(read_json(git_path(root, 'jira-sync-map.json'), {})))
    log = git_path(root, 'jira-sync.log')
    if log.exists():
        print('last log lines:')
        print('\n'.join(log.read_text(encoding='utf-8').splitlines()[-8:]))
    return 0


def _print_anchor(x, by_id):
    it = by_id.get(x['id'])
    sig = ' | ' + describe_signals(it['signals']) if it else ''
    print('  %2d pts  %-9s score %-6s %s%s' % (x['points'], x['id'], x.get('score'), (x.get('summary') or '')[:70], sig))


def cmd_anchors(a):
    """Show, propose, accept or hand-pick the reference stories points are sized against."""
    root = repo_root()
    cfg = need_config(root)
    items, _ = gsd_items(root, started_ids(root))
    stories = [it for it in items if it['type'] == 'story']
    by_id = {it['id']: it for it in stories}
    est = cfg.setdefault('estimation', {})
    if a.set:
        anchors = []
        for pair in a.set.split(','):
            gid, _, pts = pair.strip().partition('=')
            if int(pts) not in FIB:
                raise SystemExit('%s: %s is not on the scale %s' % (gid, pts, FIB))
            it = by_id.get(gid)
            anchors.append({'id': gid, 'points': int(pts), 'score': it['score'] if it else None,
                            'summary': it['summary'] if it else ''})
        scored = sorted((x for x in anchors if x['score'] is not None), key=lambda x: x['points'])
        if any(b['score'] <= s['score'] for s, b in zip(scored, scored[1:])):
            print('WARNING a bigger-point anchor does not score bigger than a smaller one; automatic sizing will be inconsistent')
        est['anchors'] = anchors
    elif a.accept:
        est['anchors'] = propose_anchors(stories)
        if not est['anchors']:
            raise SystemExit('need at least %d finished stories to propose anchors' % len(ANCHOR_POINTS))
    if a.set or a.accept:
        write_json(root / CONFIG, cfg)
        print('anchors saved to %s (commit it):' % CONFIG)
        for x in est['anchors']:
            _print_anchor(x, by_id)
        return 0
    current = est.get('anchors')
    if current and not a.propose:
        print('current anchors:')
        for x in current:
            _print_anchor(x, by_id)
        return 0
    picks = propose_anchors(stories)
    if not picks:
        print('not enough finished stories to propose anchors; name them with --set ID=PTS,...')
        return 1
    print('proposed anchors (finished stories spread small -> large):')
    for x in picks:
        _print_anchor(x, by_id)
    print('accept with `anchors --accept`, or swap any with `anchors --set 41-01=1,...`')
    return 0


def cmd_estimate(a):
    """Claude's review pass: list automatic guesses, or set reviewed points with a reason."""
    root = repo_root()
    cfg = need_config(root)
    j = Jira(cfg['site'])
    meta = j.project(cfg['project'])
    pf = meta['points']
    if not pf:
        raise SystemExit('project %s has no story points field on stories' % cfg['project'])
    if a.set:
        key, _, pts = a.set.partition('=')
        if not pts.isdigit() or int(pts) not in FIB:
            raise SystemExit('points must be one of %s' % FIB)
        if not a.reason:
            raise SystemExit('--reason is required: name the anchors it was compared against')
        j.req('PUT', '/rest/api/3/issue/%s' % key, {'fields': {pf: int(pts)}, 'update': {
            'labels': [{'remove': AUTO_LABEL}, {'add': REVIEWED_LABEL}]}})
        j.comment(key, 'Story points %s (reviewed against anchors): %s' % (pts, a.reason))
        print('%s -> %s pts (reviewed)' % (key, pts))
        return 0
    items, _ = gsd_items(root, started_ids(root))
    by_id = {it['id']: it for it in items if it['type'] == 'story'}
    anchors = (cfg.get('estimation') or {}).get('anchors') or []
    print('anchors:')
    for x in anchors:
        _print_anchor(x, by_id)
    jql = 'project = "%s" AND labels = "%s" AND statusCategory != Done ORDER BY key' % (cfg['project'], AUTO_LABEL)
    issues = j.search(jql, ['summary', 'labels', pf], limit=a.limit)
    print('%d automatic guesses awaiting review (not yet Done):' % len(issues))
    for i in issues:
        gid = next((l[4:] for l in i['fields']['labels'] if l.startswith('gsd-')), None)
        it = by_id.get(gid)
        print('  %s %s pts  %-9s %s%s' % (i['key'], i['fields'].get(pf), gid or '-', i['fields']['summary'][:70],
                                         '\n      plan: %s | %s' % (it['path'], describe_signals(it['signals'])) if it else ''))
    return 0


def cmd_link_pr(a):
    """PostToolUse hook: after `gh pr create`, prefix the PR title with its issue key."""
    try:
        data = json.load(sys.stdin) if a.if_gh_pr_create else {}
        if a.if_gh_pr_create and 'gh pr create' not in (data.get('tool_input') or {}).get('command', ''):
            return 0
        root = repo_root(data.get('cwd'))
        cfg = load_config(root)
        if not cfg:
            return 0
        r = subprocess.run(['gh', 'pr', 'view', '--json', 'number,title,headRefName'], cwd=root, capture_output=True, text=True)
        if r.returncode != 0:
            return 0
        pr = json.loads(r.stdout)
        kre = key_re(cfg['project'])
        if kre.search(pr['title']):
            return 0
        cache = read_json(git_path(root, 'jira-sync-map.json'), {})
        active_f = git_path(root, 'jira-sync-active')
        m = kre.search(pr['headRefName']) or None
        phase = re.match(r'gsd/phase-(\d+(?:\.\d+)*)', pr['headRefName'])
        key = (m.group(0) if m else None) or (cache.get(phase.group(1)) if phase else None) \
            or (active_f.read_text(encoding='utf-8').strip() if active_f.exists() else None)
        if key:
            subprocess.run(['gh', 'pr', 'edit', str(pr['number']), '--title', '%s %s' % (key, pr['title'])], cwd=root, capture_output=True)
            print('jira-sync: PR #%s linked to %s' % (pr['number'], key))
    except Exception as e:
        print('jira-sync: link-pr skipped (%s)' % e, file=sys.stderr)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog='jira_sync.py', description=__doc__.split('\n')[0])
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('init'); s.add_argument('--site'); s.add_argument('--project')
    s.add_argument('--auto-push', choices=['on', 'off'], help='push the branch after every commit so Jira sees it')
    s.set_defaults(fn=cmd_init)
    sub.add_parser('doctor').set_defaults(fn=cmd_doctor)
    s = sub.add_parser('reconcile'); s.add_argument('--dry-run', action='store_true')
    s.add_argument('--refresh-text', action='store_true', help='rewrite summary + description of every synced issue')
    s.set_defaults(fn=cmd_reconcile)
    sub.add_parser('epics').set_defaults(fn=cmd_epics)
    s = sub.add_parser('search'); s.add_argument('text'); s.set_defaults(fn=cmd_search)
    s = sub.add_parser('create')
    s.add_argument('--type', required=True, choices=['epic', 'story', 'task', 'bug', 'subtask'])
    s.add_argument('--summary', required=True); s.add_argument('--description'); s.add_argument('--parent')
    s.add_argument('--start', action='store_true'); s.add_argument('--branch', action='store_true')
    s.add_argument('--points', type=int); s.add_argument('--reason')
    s.set_defaults(fn=cmd_create)
    s = sub.add_parser('anchors'); s.add_argument('--propose', action='store_true')
    s.add_argument('--accept', action='store_true'); s.add_argument('--set', help='ID=PTS,ID=PTS,...')
    s.set_defaults(fn=cmd_anchors)
    s = sub.add_parser('estimate'); s.add_argument('--set', help='KEY=PTS'); s.add_argument('--reason')
    s.add_argument('--limit', type=int, default=50); s.set_defaults(fn=cmd_estimate)
    s = sub.add_parser('use'); s.add_argument('key'); s.add_argument('--branch', action='store_true'); s.set_defaults(fn=cmd_use)
    s = sub.add_parser('done'); s.add_argument('key', nargs='?'); s.set_defaults(fn=cmd_done)
    sub.add_parser('status').set_defaults(fn=cmd_status)
    s = sub.add_parser('commit-msg'); s.add_argument('file'); s.set_defaults(fn=cmd_commit_msg)
    sub.add_parser('post-commit').set_defaults(fn=cmd_post_commit)
    s = sub.add_parser('_reconcile-locked'); s.add_argument('root'); s.set_defaults(fn=cmd_reconcile_locked)
    s = sub.add_parser('link-pr'); s.add_argument('--if-gh-pr-create', action='store_true'); s.set_defaults(fn=cmd_link_pr)
    a = p.parse_args(argv)
    if a.cmd not in ('commit-msg', 'post-commit', '_reconcile-locked', 'link-pr'):
        sync_stable_copy()
    try:
        return a.fn(a) or 0
    except (JiraError, Unrecognized) as e:
        print('jira-sync: %s' % e, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
