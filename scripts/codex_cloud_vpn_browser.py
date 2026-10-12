"""Account-bound, secret-safe Codex VPN UI actions for the renewal producer."""
import json
import pathlib
import time


def nodes():
    return cdp('Accessibility.getFullAXTree')['nodes']


def has(role, name):
    return any(n.get('role', {}).get('value') == role and n.get('name', {}).get('value') == name for n in nodes())


def click(role, name):
    n = next(n for n in nodes() if n.get('role', {}).get('value') == role and n.get('name', {}).get('value') == name)
    cdp('DOM.scrollIntoViewIfNeeded', backendNodeId=n['backendDOMNodeId'])
    box = cdp('DOM.getBoxModel', backendNodeId=n['backendDOMNodeId'])['model']['content']
    x = sum(box[::2]) / 4
    y = sum(box[1::2]) / 4
    for kind in ('mousePressed', 'mouseReleased'):
        cdp('Input.dispatchMouseEvent', type=kind, x=x, y=y, button='left', clickCount=1)


def text():
    return js('document.body.innerText')


def wait_for(predicate, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(1)
    raise ValueError('ui_state_timeout')


def owned_target(env, row):
    live = {t['targetId'] for t in cdp('Target.getTargets')['targetInfos']}
    target = row.get('editor_target')
    if target not in live:
        url = row.get('draft_task_url') or 'https://chatgpt.com/settings/codex-cloud'
        target = cdp('Target.createTarget', url=url, background=True)['targetId']
    switch_tab(target)
    cdp('Target.activateTarget', targetId=target)
    wait_for(lambda: js('location.origin') == 'https://chatgpt.com')
    identity = js("(async()=>{let d=await(await fetch('/api/auth/session')).json();return d.user?.email;})()")
    if identity != env['account']:
        raise ValueError('account_signin_required_use_secure_vault')
    row['editor_target'] = target
    return target


def open_editor(env, row):
    owned_target(env, row)
    if not row.get('draft_task_url'):
        wait_for(lambda: has('button', 'Actions for '+env['name']))
        click('button', 'Actions for '+env['name'])
        wait_for(lambda: has('menuitem', 'Edit') or has('menuitem', 'Continue setup'))
        click('menuitem', 'Edit' if has('menuitem', 'Edit') else 'Continue setup')
        wait_for(lambda: js('location.pathname').startswith('/local/'))
        row['draft_task_url'] = js('location.href')
    wait_for(lambda: has('button', 'Open environment panel') or has('button', 'Edit Tailscale auth key'))
    if not has('button', 'Edit Tailscale auth key'):
        click('button', 'Open environment panel')
        if has('button', 'Advanced'):
            click('button', 'Advanced')
    wait_for(lambda: has('button', 'Edit Tailscale auth key'))
    paths = js("Array.from(new Set(performance.getEntriesByType('resource').map(e=>e.name).filter(e=>e.includes('/api/codex-cloud/v1/environment-configs/')).map(e=>e.split('?')[0]))).slice(-20)")
    if not any(env['environment_id'] in path for path in paths):
        raise ValueError('exact_environment_resource_not_observed')
    if '2 domains' not in text():
        raise ValueError('approved_host_settings_missing_do_not_infer_repair')


def save_vpn(env, row, renewal, state):
    open_editor(env, row)
    renewal.atomic(renewal.STATE, state)
    secret = renewal.private_read(pathlib.Path(row['secret_path']))
    if secret['key_id'] != row['key_id'] or secret['environment_id'] != env['environment_id']:
        raise ValueError('private_key_binding_mismatch')
    if not js("!!document.querySelector('input[type=password]')"):
        click('button', 'Edit Tailscale auth key')
    wait_for(lambda: js("!!document.querySelector('input[type=password]')"))
    root = cdp('DOM.getDocument')['root']['nodeId']
    node = cdp('DOM.querySelector', nodeId=root, selector='input[type=password]')['nodeId']
    cdp('DOM.scrollIntoViewIfNeeded', nodeId=node)
    box = cdp('DOM.getBoxModel', nodeId=node)['model']['content']
    x, y = sum(box[::2])/4, sum(box[1::2])/4
    for kind in ('mousePressed','mouseReleased'):
        cdp('Input.dispatchMouseEvent',type=kind,x=x,y=y,button='left',clickCount=1)
    for kind in ('keyDown','keyUp'):
        cdp('Input.dispatchKeyEvent',type=kind,key='a',code='KeyA',windowsVirtualKeyCode=65,modifiers=2)
    cdp('Input.insertText', text=secret['key'])
    secret = None
    click('button', 'Save')
    wait_for(lambda: not js("!!document.querySelector('input[type=password]')"))
    wait_for(lambda: 'Key entered' in text() or 'Configured' in text())
    click('button', 'Save draft')
    wait_for(lambda: 'All changes saved' in text())
    row.update(phase='VPN_SAVED')
    renewal.atomic(renewal.STATE, state)


def publish(env, row, renewal, state):
    open_editor(env, row)
    if 'Environment published' in text():
        row['phase'] = 'PUBLISHED'
        renewal.atomic(renewal.STATE, state)
        return
    refusal = 'Couldn’t publish environment' in text()
    if row['phase'] == 'PUBLISH_INTENT' and not refusal:
        raise ValueError('publication_uncertain_no_replay')
    if refusal and row.get('publish_retry_count',0) >= 1:
        raise ValueError('provider_publish_refused_after_one_retry')
    button = 'Retry' if refusal else 'Publish'
    if refusal:
        row['publish_retry_count'] = row.get('publish_retry_count',0)+1
    row['phase'] = 'PUBLISH_INTENT'
    renewal.atomic(renewal.STATE, state)
    click('button', button)
    wait_for(lambda: 'Environment published' in text() or 'Couldn’t publish environment' in text(),120)
    if 'Couldn’t publish environment' in text():
        row.update(phase='PUBLISH_FAILED',provider_message='Couldn’t publish environment. Your settings are saved. Try publishing again.')
        renewal.atomic(renewal.STATE,state)
        raise ValueError('provider_publish_refused')
    row['phase'] = 'PUBLISHED'
    renewal.atomic(renewal.STATE,state)


def check_usage(env):
    target = cdp('Target.createTarget',url='https://chatgpt.com/settings/usage',background=True)['targetId']
    switch_tab(target)
    cdp('Target.activateTarget',targetId=target)
    wait_for(lambda: 'Weekly limit' in text())
    identity=js("(async()=>{let d=await(await fetch('/api/auth/session')).json();return d.user?.email;})()")
    if identity != env['account']:
        raise ValueError('account_identity_mismatch')
    import re
    body=text()
    left=re.search(r'(\d+)% left',body)
    reset=re.search(r'Resets in ([^\n]+)',body)
    if not left:
        raise ValueError('included_allowance_unknown_no_task')
    result={'remaining_pct':int(left.group(1)),'reset_label':reset.group(1) if reset else None}
    if result['remaining_pct'] <= 0:
        raise ValueError('included_quota_zero_no_credits_or_resets')
    return result

