"""Offline follow-up boundaries for review5541330104; no external effects."""
import copy
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import glp_release_inputs as inputs
import glp_affiliate_sync_job as job
import glp_release_main as release


def test_writable_namespace_provenance_and_dirty_reuse_refusal(tmp_path, monkeypatch):
    import glp_deploy_glp34_authority_v1 as carrier
    compiler=tmp_path/'compiler'; compiler.mkdir(); (compiler/'x').write_text('compiler')
    monkeypatch.setattr(carrier, 'TOOLCHAIN', compiler)
    seed=tmp_path/'seed'; seed.mkdir(); (seed/'input').write_text('governed')
    source=tmp_path/'source'; source.mkdir()
    config={'namespace':'/site','base_argv':['/bin/true','--clearenv','--unshare-all',
        '--cap-drop','ALL','--bind',str(source),'/site','--bind',str(seed),'/inputs']}
    capture=lambda:inputs.capture(config,source_tree='T',source_revision='H',sheet_sha256='S')
    declared=copy.deepcopy(config)
    baseline=capture()
    report=tmp_path/'one';report.mkdir()
    argv=job.namespace(config,source,report)
    assert capture()==baseline  # paths change, effective contents do not
    assert config['base_argv']==declared['base_argv']
    bound=Path(argv[argv.index('/inputs')-1])
    (bound/'input').write_text('drift')
    assert capture()!=baseline
    with pytest.raises(ValueError,match='effective_writable_bind_preexisting_drift'):
        job.namespace(config,source,report)
    # Outputs in source/public are not confused with non-source input mounts.
    (bound/'input').write_text('governed')
    (source/'public').mkdir();(source/'public/index.html').write_text('generated')
    assert capture()==baseline
    (bound/'input').unlink()
    with pytest.raises(ValueError,match='effective_writable_bind_preexisting_drift'):
        job.namespace(config,source,report)


@pytest.mark.parametrize('defect', ['healthy','missing-workflow','partial','wrong-head','wrong-repo',
    'wrong-event','wrong-pr','failed','missing-job','wrong-job-head','truncated','wrong-run','newer-pending'])
def test_recovery_authenticates_complete_applicable_actions(defect):
    checks=[{'name':n,'status':'COMPLETED','conclusion':'SUCCESS'} for n in release.ACCEPTANCE_CHECKS.values()]
    pr={'number':99,'headRefOid':'H','statusCheckRollup':checks}
    runs=[{'id':i,'path':w,'head_sha':'H','event':'pull_request','run_number':1,'run_attempt':1,
        'head_repository':{'full_name':'izaiahat/glp-site-candidate'},
        'pull_requests':[{'number':99,'head':{'sha':'H'}}],
        'status':'completed','conclusion':'success'} for i,w in enumerate(release.ACCEPTANCE_CHECKS)]
    jobs={i:[{'run_id':i,'head_sha':'H','name':n,'status':'completed','conclusion':'success'}]
          for i,n in enumerate(release.ACCEPTANCE_CHECKS.values())}
    if defect=='missing-workflow':runs.pop()
    if defect=='partial':runs[-1]['status']='in_progress'
    if defect=='wrong-head':runs[-1]['head_sha']='OLD'
    if defect=='wrong-repo':runs[-1]['head_repository']['full_name']='foreign/repo'
    if defect=='wrong-event':runs[-1]['event']='workflow_dispatch'
    if defect=='wrong-pr':runs[-1]['pull_requests'][0]['number']=98
    if defect=='failed':jobs[2][0]['conclusion']='failure'
    if defect=='missing-job':jobs[2][0]['name']='unrelated'
    if defect=='wrong-job-head':jobs[2][0]['head_sha']='OLD'
    if defect=='wrong-run':jobs[2][0]['run_id']=123
    if defect=='newer-pending':runs.append({**runs[-1],'id':3,'run_number':2,'status':'queued'})
    def gh(*args):
        assert args[0]=='api'
        endpoint=args[1]
        if '/actions/runs?' in endpoint:
            return {'total_count':len(runs),'workflow_runs':runs}
        i=int(endpoint.split('/runs/')[1].split('/')[0])
        return {'total_count':2 if defect=='truncated' and i==2 else len(jobs[i]),'jobs':jobs[i]}
    assert release.acceptance_ci_complete(pr,gh) is (defect=='healthy')
