"""Offline checks for jira_sync.py. Run: python tests/test_jira_sync.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills' / 'jira-sync'))
import jira_sync as js  # noqa: E402


def test_scope_ids():
    assert js.scope_ids('feat(39.2-16): x') == ['39.2-16', '39.2']
    assert js.scope_ids('docs(41): create phase plan') == ['41']
    assert js.scope_ids('docs(phase-41): x') == ['41']
    assert js.scope_ids('docs(quick-261018-ri9): x') == ['quick-261018-ri9']
    assert js.scope_ids('fix(odds): x') == []
    assert js.scope_ids('plain message') == []


def test_resolve_key_order():
    cache = {'41-04': 'PROJ-20', '41': 'PROJ-12'}
    r = js.resolve_key
    assert r('feat(41-04): x [PROJ-9]', 'main', cache, 'PROJ-1', 'PROJ') is None  # already linked
    assert r('feat(41-04): x', 'PROJ-7-fix', cache, 'PROJ-1', 'PROJ') == 'PROJ-7'  # branch beats scope
    assert r('feat(41-04): x', 'main', cache, 'PROJ-1', 'PROJ') == 'PROJ-20'        # plan story
    assert r('feat(41-09): x', 'main', cache, None, 'PROJ') == 'PROJ-12'            # falls back to epic
    assert r('chore: tidy', 'main', cache, 'PROJ-1', 'PROJ') == 'PROJ-1'            # active issue
    assert r('chore: UTF-8 fix', 'main', {}, None, 'PROJ') is None                  # not a key
    sub = dict(cache, **{'41-04-t2': 'PROJ-22', '41-ops': 'PROJ-13'})
    assert r('feat(41-04): x', 'main', sub, None, 'PROJ', '41-04-t2') == 'PROJ-22'     # task sub-task first
    assert r('docs(41-04): complete', 'main', sub, None, 'PROJ', None) == 'PROJ-20'    # bookkeeping -> story
    assert r('docs(41): verification report', 'main', sub, None, 'PROJ') == 'PROJ-13'  # phase work -> ops task
    assert r('feat(41-09): x', 'main', sub, None, 'PROJ') == 'PROJ-13'                 # unknown plan -> ops task
    assert js.tag_message('feat(41-04): x\n\nbody\n', 'PROJ-20') == 'feat(41-04): x [PROJ-20]\n\nbody\n'


def test_pick_transition():
    opts = [{'id': '11', 'to': {'statusCategory': {'key': 'new'}}},
            {'id': '21', 'to': {'statusCategory': {'key': 'indeterminate'}}},
            {'id': '31', 'to': {'statusCategory': {'key': 'done'}}}]
    assert js.pick_transition(opts, 'done')['id'] == '31'
    assert js.pick_transition(opts[:2], 'done') is None


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8')


def test_gsd_items():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        assert js.gsd_items(root) == ([], None)
        write(root, '.planning/STATE.md', '---\nmilestone: v14.0\nmilestone_name: Beat the Market\ncurrent_phase: 41\n---\n')
        write(root, '.planning/ROADMAP.md',
              '- [x] **Phase 39: Start the Clocks** - stamp\n- [ ] **Phase 41: CLV Engine** - store\n- [ ] **Phase 42: Bet Timing** - x\n')
        plan = '---\nphase: 41-clv\nplan: 0{n}\n---\n<objective>\nFinish the tab. More text.\n</objective>\n<tasks>\n' \
               '<task type="auto">\n  <name>Task 1: Build it</name>\n</task>\n<task type="auto"><name>Task 2: Test it</name></task>\n'
        write(root, '.planning/phases/39-start-the-clocks/39-01-PLAN.md', plan.format(n=1))
        write(root, '.planning/phases/39-start-the-clocks/39-01-SUMMARY.md', '---\nstatus: complete\n---\n')
        write(root, '.planning/phases/41-clv/41-01-PLAN.md', plan.format(n=1))
        write(root, '.planning/phases/41-clv/41-01-SUMMARY.md', '---\nstatus: complete\n---\n')
        write(root, '.planning/phases/41-clv/41-02-PLAN.md', plan.format(n=2))
        write(root, '.planning/phases/41-clv/41-03-PLAN.md', plan.format(n=3))
        write(root, '.planning/phases/42-bet-timing/42-CONTEXT.md', '')
        write(root, '.planning/phases/999.1-backlog/999.1-01-PLAN.md', plan.format(n=1))
        write(root, '.planning/debug/broken-thing.md', '---\ntrigger: "API returns 502"\n---\n')
        write(root, '.planning/debug/resolved/old-bug.md', '---\ntrigger: old\n---\n')
        write(root, '.planning/quick/261018-ri9-sheet-view/261018-ri9-PLAN.md', '')
        write(root, '.planning/quick/20260911-parlay/SUMMARY.md', '')
        write(root, '.planning/milestones/v13.0-ROADMAP.md', '')

        items, ms = js.gsd_items(root, started_ids={'41-02'})
        by = {it['id']: it for it in items}
        assert ms == {'current': 'v14.0 Beat the Market', 'archived': {'v13.0'}}
        assert '999.1' not in by and '999.1-01' not in by
        assert by['39']['cat'] == 'done' and by['39']['summary'] == 'Phase 39: Start the Clocks'
        assert by['41']['cat'] == 'indeterminate' and by['42']['cat'] == 'new'
        assert by['41-01']['cat'] == 'done' and by['41-01-t1']['cat'] == 'done'
        assert by['41-02']['cat'] == 'indeterminate'  # has a commit, no summary
        assert by['41-03']['cat'] == 'new'
        assert by['41-02']['summary'] == 'Finish the tab. More text'
        assert by['41-02']['sections'][0] == ('Objective', 'p', 'Finish the tab. More text.')
        t2 = by['41-02-t2']
        assert (t2['summary'], t2['parent'], t2['cat'], t2['type']) == ('Test it', '41-02', 'new', 'subtask')
        assert by['debug-broken-thing']['summary'] == 'API returns 502' and by['debug-old-bug']['cat'] == 'done'
        # open unplanned work goes under the current phase's epic
        assert by['debug-broken-thing']['parent'] == '41' and by['quick-261018-ri9']['parent'] == '41'
        assert by['quick-261018-ri9']['cat'] == 'indeterminate' and by['quick-20260911-parlay']['cat'] == 'done'
        ids = [it['id'] for it in items]
        for it in items:  # parents always precede children
            if it['parent']:
                assert ids.index(it['parent']) < ids.index(it['id']), it['id']


def test_plan_signals_and_score():
    text = ('---\nautonomous: false\nfiles_modified:\n  - a.py\n  - b.py\n  - c.py\nestimate:\n  tokens: 50000\n'
            '  tasks: 2\n  confidence: low\n---\n<task type="auto"><name>x</name></task>\n'
            '<task type="checkpoint:human-verify"><name>y</name></task>\n')
    s = js.plan_signals(text)
    assert s == {'tokens': 50000, 'files': 3, 'tasks': 2, 'checkpoints': 1, 'hands_on': True, 'confidence': 'low'}
    assert js.plan_signals('---\nfiles_modified: [a, b]\n---\n')['files'] == 2
    bigger = dict(s, tokens=100000)
    assert js.plan_score(bigger) > js.plan_score(s)            # more effort -> bigger
    assert js.plan_score(dict(s, files=30)) > js.plan_score(s)  # more complexity -> bigger
    assert js.plan_score(dict(s, checkpoints=0)) < js.plan_score(s)  # less risk -> smaller


def test_anchor_triangulation():
    stories = [{'id': 'p%d' % i, 'score': float(i), 'cat': 'done', 'summary': ''} for i in range(1, 21)]
    stories.append({'id': 'open', 'score': 99.0, 'cat': 'new', 'summary': ''})
    a = js.propose_anchors(stories)
    assert [x['points'] for x in a] == [1, 2, 3, 5, 8]
    assert 'open' not in [x['id'] for x in a]                   # only finished work anchors
    assert all(b['score'] > s['score'] for s, b in zip(a, a[1:]))
    sc = {x['points']: x['score'] for x in a}
    assert js.size_against(sc[3], a)[0] == 3                   # equal to an anchor -> that anchor
    assert js.size_against(sc[3] + 0.1, a)[0] == 3             # just above 3 -> closer to 3
    assert js.size_against(sc[5] - 0.1, a)[0] == 5              # just below 5 -> closer to 5
    assert js.size_against(-5, a)[0] == 1                       # tiny -> smallest anchor
    gap = sc[8] - sc[5]
    assert js.size_against(sc[8] + gap * 0.5, a)[0] == 8         # a bit past the top -> 8
    assert js.size_against(sc[8] + gap, a)[0] == 13             # a full step past the top -> 13
    assert js.propose_anchors(stories[:3]) == []                # too few finished stories


def test_subtask_for():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        write(root, '.planning/phases/41-x/41-04-PLAN.md',
              '<task type="auto"><name>A</name><files>src/a.ts, src/a.check.ts</files></task>\n'
              '<task type="auto"><name>B</name><files>src/b.ts, tests/b.spec.ts</files></task>\n')
        assert js.subtask_for(root, '41-04', ['src/b.ts', 'tests/b.spec.ts', 'src/a.ts']) == '41-04-t2'
        assert js.subtask_for(root, '41-04', ['src/a.ts']) == '41-04-t1'
        assert js.subtask_for(root, '41-04', ['README.md']) is None
        assert js.subtask_for(root, '41-04', ['src/a.ts', '.planning/phases/41-x/41-04-SUMMARY.md']) is None
        assert js.subtask_for(root, '41-09', ['src/a.ts']) is None


def test_site_allowed():
    import os
    os.environ.pop('JIRA_ALLOWED_HOSTS', None)
    assert js.site_allowed('echosilopoc01.atlassian.net')
    for bad in ('evil.com', 'atlassian.net.evil.com', 'evil.com/x.atlassian.net', 'x.atlassian.net.evil.com'):
        assert not js.site_allowed(bad), bad
    os.environ['JIRA_ALLOWED_HOSTS'] = 'jira.mycorp.com'
    assert js.site_allowed('jira.mycorp.com') and not js.site_allowed('evil.com')
    os.environ.pop('JIRA_ALLOWED_HOSTS')


def test_short_title():
    st = js.short_title
    assert st('Task 1: Tracer -- Your bets renders from the report: counts, every leg status, direction markers') \
        == 'Your bets renders from the report'
    assert st('Your bets, beat-vs-win, tier and sharp sections, grade-toast failure line, Playwright dev spec (wave 2)') \
        == 'Your bets, beat-vs-win, tier and sharp sections'
    assert st('Win-chance CLV (pick-time pull, de-vig, residual curve), tier and sharp flags') \
        == 'Win-chance CLV, tier and sharp flags'
    assert st('short one.') == 'Short one'
    for s in ('a ' * 80, 'word' * 40):
        assert len(st(s)) <= 60


def test_epic_for_history_by_date():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        write(root, '.planning/STATE.md', '---\nmilestone: v1\ncurrent_phase: 3\n---\n')
        write(root, '.planning/ROADMAP.md', '- [x] **Phase 1: A**\n- [x] **Phase 2: B**\n- [ ] **Phase 3: C**\n')
        for n in (1, 2, 3):
            write(root, '.planning/phases/%d-x/%d-CONTEXT.md' % (n, n), '')
        write(root, '.planning/debug/resolved/mid.md', '---\ntrigger: t\n---\n')
        write(root, '.planning/debug/resolved/early.md', '---\ntrigger: t\n---\n')
        starts = {'1': '2026-01-01T00:00:00+00:00', '2': '2026-02-01T00:00:00+00:00', '3': '2026-03-01T00:00:00+00:00'}
        dates = {'.planning/debug/mid.md': '2026-02-10T00:00:00+00:00', '.planning/debug/early.md': '2025-12-01T00:00:00+00:00'}
        items, _ = js.gsd_items(root, (), dates, starts)
        by = {it['id']: it for it in items}
        assert by['debug-mid']['parent'] == '2'     # phase 2 was being worked on then
        assert by['debug-early']['parent'] == '1'   # before any phase: the first one


def test_sprint_dates():
    import datetime as dt
    utc = dt.timezone.utc
    assert js.add_month(dt.datetime(2026, 11, 1, 4, tzinfo=utc)) == dt.datetime(2026, 12, 1, 4, tzinfo=utc)
    assert js.add_month(dt.datetime(2026, 12, 1, tzinfo=utc)) == dt.datetime(2027, 1, 1, tzinfo=utc)
    assert js.add_month(dt.datetime(2026, 1, 31, tzinfo=utc)).day == 28
    assert js.next_sprint_name('PROJ Sprint 1') == 'PROJ Sprint 2'
    assert js.parse_ts('2026-11-01T04:00:00.000Z') == dt.datetime(2026, 11, 1, 4, tzinfo=utc)
    assert js.jira_ts(dt.datetime(2026, 11, 1, 4, tzinfo=utc)) == '2026-11-01T04:00:00.000Z'


def test_adf_doc_shape():
    doc = js.adf_doc([('Goal', 'p', 'one\n\ntwo'), ('Files', 'code', ['a.py']), ('Verify', 'pre', 'make test')], 'foot')
    kinds = [n['type'] for n in doc['content']]
    assert kinds == ['heading', 'paragraph', 'paragraph', 'heading', 'bulletList', 'heading', 'codeBlock', 'paragraph']
    assert doc['content'][4]['content'][0]['content'][0]['content'][0]['marks'] == [{'type': 'code'}]


def test_unrecognized():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        write(root, '.planning/config.json', '{}')
        try:
            js.gsd_items(root)
        except js.Unrecognized:
            return
        raise AssertionError('expected Unrecognized')


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn()
            print('ok', name)
