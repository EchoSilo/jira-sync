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
        assert by['41-02']['summary'] == 'Plan 41-02: Finish the tab.'
        assert by['41-02-t2'] == {'id': '41-02-t2', 'type': 'subtask', 'summary': 'T2: Test it', 'parent': '41-02',
                                  'cat': 'new', 'version': None, 'path': '.planning/phases/41-clv/41-02-PLAN.md'}
        assert by['debug-broken-thing']['summary'] == 'API returns 502' and by['debug-old-bug']['cat'] == 'done'
        assert by['quick-261018-ri9']['cat'] == 'indeterminate' and by['quick-20260911-parlay']['cat'] == 'done'
        ids = [it['id'] for it in items]
        for it in items:  # parents always precede children
            if it['parent']:
                assert ids.index(it['parent']) < ids.index(it['id']), it['id']


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
