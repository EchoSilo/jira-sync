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
import json
import math
import os
import re
import shutil
import subprocess
import sys
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


def _plan_summary(text, pid):
    m = re.search(r'<objective>(.*?)</objective>', text, re.S)
    return 'Plan %s: %s' % (pid, first_sentence(m.group(1))) if m else 'Plan %s' % pid


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


def gsd_items(root, started_ids=()):
    """Desired Jira items derived from .planning/, parents before children.

    Each item: {id, type, summary, parent, cat, version, path}.
    Returns ([], None) when the repo has no .planning/ directory.
    """
    pl = root / '.planning'
    if not pl.is_dir():
        return [], None
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
    started = set(started_ids)
    items = []
    for d in sorted((pl / 'phases').iterdir(), key=lambda p: [int(x) if x.isdigit() else 0 for x in re.split(r'[.-]', p.name.split('-')[0])]):
        m = re.match(r'(\d+(?:\.\d+)*)-(.+)', d.name)
        if not d.is_dir() or not m or m.group(1).startswith('999'):
            continue  # 999.x = backlog, not scheduled work
        num, slug = m.group(1), m.group(2)
        done, name = checked.get(num, (False, slug.replace('-', ' ').title()))
        phase = {'id': num, 'type': 'epic', 'summary': 'Phase %s: %s' % (num, name),
                 'parent': None, 'version': version, 'path': d.relative_to(root).as_posix()}
        # A phase-scoped commit is planning (docs(40): create phase plan), not work.
        children, any_started = [], num == current
        for p in sorted(d.glob('%s-*-PLAN.md' % num)):
            pid = p.name[:-len('-PLAN.md')]
            text = p.read_text(encoding='utf-8')
            summ = p.with_name(pid + '-SUMMARY.md')
            pdone = summ.exists() and frontmatter(summ.read_text(encoding='utf-8')).get('status', 'complete') == 'complete'
            pstart = pdone or pid in started
            any_started = any_started or pstart
            cat = 'done' if pdone else 'indeterminate' if pstart else 'new'
            sig = plan_signals(text)
            children.append({'id': pid, 'type': 'story', 'summary': _plan_summary(text, pid),
                             'parent': num, 'cat': cat, 'version': version,
                             'path': p.relative_to(root).as_posix(),
                             'signals': sig, 'score': plan_score(sig)})
            for i, tname in enumerate(re.findall(r'<task\b[^>]*>\s*<name>(.*?)</name>', text, re.S), 1):
                tname = re.sub(r'^Task \d+:\s*', '', ' '.join(tname.split()))
                children.append({'id': '%s-t%d' % (pid, i), 'type': 'subtask',
                                 'summary': 'T%d: %s' % (i, first_sentence(tname, 200)),
                                 'parent': pid, 'cat': 'done' if pdone else 'new',
                                 'version': None, 'path': p.relative_to(root).as_posix()})
        phase['cat'] = 'done' if done else 'indeterminate' if any_started else 'new'
        items.append(phase)
        items.extend(children)

    # Work items that live outside phases.
    for sub, cat in (('debug', 'indeterminate'), ('debug/resolved', 'done')):
        for f in sorted((pl / sub).glob('*.md')) if (pl / sub).is_dir() else []:
            trig = frontmatter(f.read_text(encoding='utf-8')).get('trigger') or f.stem.replace('-', ' ')
            items.append({'id': 'debug-' + f.stem, 'type': 'bug', 'summary': first_sentence(trig, 200),
                          'parent': None, 'cat': cat, 'version': version, 'path': f.relative_to(root).as_posix()})
    qdir = pl / 'quick'
    for q in sorted(qdir.iterdir()) if qdir.is_dir() else []:
        if not q.is_dir():
            continue
        m = re.match(r'(\d{6}-[a-z0-9]{3})-(.+)', q.name)
        qid, slug = (m.group(1), m.group(2)) if m else (q.name, q.name)
        done = any(q.glob('*SUMMARY.md'))
        items.append({'id': 'quick-' + qid, 'type': 'task', 'summary': 'Quick: ' + slug.replace('-', ' '),
                      'parent': None, 'cat': 'done' if done else 'indeterminate', 'version': version,
                      'path': q.relative_to(root).as_posix()})
    for sub, cat in (('todos/pending', 'new'), ('todos/completed', 'done')):
        for f in sorted((pl / sub).glob('*.md')) if (pl / sub).is_dir() else []:
            title = frontmatter(f.read_text(encoding='utf-8')).get('title') or f.stem.replace('-', ' ')
            items.append({'id': 'todo-' + f.stem, 'type': 'task', 'summary': 'Todo: ' + first_sentence(title, 200),
                          'parent': None, 'cat': cat, 'version': version, 'path': f.relative_to(root).as_posix()})
    archived = {f.name.split('-ROADMAP.md')[0] for f in (pl / 'milestones').glob('*-ROADMAP.md')} if (pl / 'milestones').is_dir() else set()
    return items, {'current': version, 'archived': archived}


def started_ids(root, n=3000):
    """GSD ids that have at least one commit, from recent commit subjects."""
    out = set()
    for subj in git('log', '--format=%s', '-n', str(n), cwd=root).splitlines():
        out.update(scope_ids(subj))
    return out


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
        fields = ['labels', 'status'] + ([points_field] if points_field else [])
        for i in self.search('project = "%s" AND labels = "%s"' % (project, SYNC_LABEL), fields):
            for label in i['fields']['labels']:
                if label.startswith('gsd-'):
                    have[label[4:]] = {'key': i['key'], 'cat': i['fields']['status']['statusCategory']['key'],
                                       'points': i['fields'].get(points_field) if points_field else None}
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


# ---------------------------------------------------------------------- reconcile

def reconcile(root, cfg, dry=False, out=print):
    project = cfg['project']
    items, ms = gsd_items(root, started_ids(root))
    if ms is None:
        out('no .planning/ directory -- nothing to reconcile (use start/done for non-GSD work)')
        return 0
    j = Jira(cfg['site'])
    meta = j.project(project)
    pf = meta['points']
    have = j.synced(project, pf)
    version_id, created, moved, sized, warnings = None, 0, 0, 0, []
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
        cur = have.get(it['id'])
        if not cur:
            parent = have.get(it['parent']) if it['parent'] else None
            if it['parent'] and not parent:
                warnings.append('%s skipped: parent %s was not created' % (it['id'], it['parent']))
                continue
            pts, note = size(it)
            if dry:
                out('would create %-7s %-18s %s' % (it['type'], it['id'], it['summary'][:80]))
                have[it['id']] = {'key': '(new %s)' % it['id'], 'cat': 'new', 'points': pts}
                created += 1
                if pts:
                    out('would size   %-18s %d pts -- %s' % (it['id'], pts, note.split(': ', 1)[1].split('. Signals')[0]))
                    sized += 1
                if it['cat'] != 'new':
                    out('would move   %-18s -> %s' % (it['id'], CAT_NAMES[it['cat']]))
                    moved += 1
                continue
            fields = {'project': {'key': project}, 'issuetype': {'id': type_id},
                      'summary': it['summary'][:254], 'labels': [SYNC_LABEL, 'gsd-' + it['id']],
                      'description': adf('GSD item %s\nSource: %s' % (it['id'], it['path']))}
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
            cur = have[it['id']] = {'key': key, 'cat': 'new', 'points': pts}
            created += 1
            out('created %s %s (%s)%s' % (key, it['id'], it['type'], ' %d pts' % pts if pts else ''))
            if pts:
                j.comment(key, note)
                sized += 1
        elif cur.get('points') is None:
            # Existing story with no points yet: size it. Points a person set are never overwritten.
            pts, note = size(it)
            if pts and dry:
                out('would size   %-18s %s %d pts' % (it['id'], cur['key'], pts))
                sized += 1
            elif pts:
                j.req('PUT', '/rest/api/3/issue/%s' % cur['key'],
                      {'fields': {pf: pts}, 'update': {'labels': [{'add': AUTO_LABEL}]}})
                j.comment(cur['key'], note)
                cur['points'] = pts
                sized += 1
                out('sized %s %s -> %d pts' % (cur['key'], it['id'], pts))
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
    if not dry:
        write_json(git_path(root, 'jira-sync-map.json'), {k: v['key'] for k, v in have.items()})
    for w in warnings:
        out('WARNING ' + w)
    out(('dry run: %d to create, %d to move, %d to size' if dry else 'done: %d created, %d moved, %d sized')
        % (created, moved, sized) + ', %d warnings' % len(warnings))
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
        items, ms = gsd_items(root, started_ids(root))
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
    return reconcile(root, need_config(root), dry=a.dry_run)


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
    if not load_config(root) or not (root / '.planning').is_dir():
        return 0
    args = [sys.executable, str(Path(__file__).resolve()), '_reconcile-locked', str(root)]
    kw = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL, 'cwd': str(root)}
    if os.name == 'nt':
        kw['creationflags'] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw['start_new_session'] = True
    subprocess.Popen(args, **kw)  # keep commits fast; result goes to .git/jira-sync.log
    return 0


def cmd_reconcile_locked(a):
    root = Path(a.root)
    cfg = load_config(root)
    if cfg:
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
    fields = {'project': {'key': cfg['project']}, 'issuetype': {'id': type_id}, 'summary': a.summary[:254]}
    if a.description:
        fields['description'] = adf(a.description)
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
    s = sub.add_parser('init'); s.add_argument('--site'); s.add_argument('--project'); s.set_defaults(fn=cmd_init)
    sub.add_parser('doctor').set_defaults(fn=cmd_doctor)
    s = sub.add_parser('reconcile'); s.add_argument('--dry-run', action='store_true'); s.set_defaults(fn=cmd_reconcile)
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
