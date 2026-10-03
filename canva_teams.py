"""Discover and select teams from Canva's visible team switcher."""
import hashlib
import json
import re
import time

import canva_invite

CATALOG = canva_invite.DATA_DIR / 'canva_teams.json'


def _current_name(page):
    member = page.locator('[aria-label="Number of team members"]').first
    member.wait_for(state='visible', timeout=10000)
    return member.evaluate("""e => {
      for(let p=e.parentElement;p;p=p.parentElement){
        const lines=(p.innerText||'').split('\\n').map(s=>s.trim()).filter(Boolean);
        if(lines.length>=3 && !/^(Free|Pro|Education|Business|Teams|Enterprise|•|[0-9,]+)$/i.test(lines[0])
           && lines.some(s=>/^(Free|Pro|Education|Business|Teams|Enterprise)$/i.test(s))) return lines[0];
      } return '';
    }""")


def open_account(page):
    button = page.get_by_role('button', name=re.compile(r'More account and team options$'))
    button.wait_for(state='visible', timeout=15000)
    button.click()
    return _current_name(page)


def open_picker(page):
    current = open_account(page)
    if not current:
        raise RuntimeError('Current Canva team could not be identified')
    page.get_by_text(current, exact=True).first.click()
    picker = page.get_by_role('listbox', name='Change team', exact=True)
    picker.wait_for(state='visible', timeout=10000)
    return picker, current


def team_id(name):
    return hashlib.sha256(name.encode('utf-8')).hexdigest()[:24]


def discover(pw):
    if not canva_invite.SESSION_FILE.exists():
        return {'ok': False, 'error': 'Complete Canva login first'}
    browser = pw.chromium.launch(headless=False, args=['--no-sandbox', '--disable-dev-shm-usage'])
    context = browser.new_context(storage_state=str(canva_invite.SESSION_FILE))
    page = context.new_page()
    try:
        page.goto(canva_invite.CANVA_HOME, wait_until='domcontentloaded', timeout=45000)
        canva_invite._dismiss_cookies(page)
        picker, current = open_picker(page)
        raw = picker.get_by_role('option').evaluate_all("""es=>es.map(e=>({
          label:e.getAttribute('aria-label')||'',
          text:(e.innerText||'').trim(),
          members:(e.querySelector('[aria-label="Number of team members"]')?.innerText||'').trim()
        }))""")
        teams = []
        for row in raw:
            if not row['label'].startswith('Switch to '):
                continue
            name = row['label'][10:].strip()
            if not name:
                continue
            lines = [s.strip() for s in row['text'].splitlines() if s.strip()]
            plan = next((s for s in lines if s.lower() in ('free','pro','education','business','teams','enterprise')), '')
            teams.append({'id':team_id(name),'name':name,'plan':plan,'members':row['members'],'current':name==current})
        if not teams:
            return {'ok':False,'error':'No teams found. Check Canva login and refresh.'}
        names = [t['name'] for t in teams]
        for team in teams:
            team['ambiguous'] = names.count(team['name']) > 1
        result = {'ok':True,'teams':teams,'fetched_at':time.time()}
        CATALOG.write_text(json.dumps(result),encoding='utf-8')
        return result
    except Exception as exc:
        canva_invite._shot(page, 'teams_fetch_failed')
        return {'ok':False,'error':'Teams could not be fetched: '+str(exc)[:180]}
    finally:
        context.close()
        browser.close()


def resolve(selected_id):
    try:
        data = json.loads(CATALOG.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return next((t for t in data.get('teams',[]) if t['id']==selected_id and not t.get('ambiguous')),None)


def select(page, name):
    if not name:
        return 'Select a Canva team before sending the invite'
    page.goto(canva_invite.CANVA_HOME,wait_until='domcontentloaded',timeout=45000)
    canva_invite._dismiss_cookies(page)
    picker, current = open_picker(page)
    option = picker.get_by_role('option',name='Switch to '+name,exact=True)
    if option.count()!=1:
        return 'Selected team is missing or ambiguous. Refresh teams.'
    if current == name:
        page.keyboard.press('Escape')
        page.keyboard.press('Escape')
        return ''
    option.click()
    page.wait_for_timeout(2000)
    # Reopen the account menu to verify the actual active team, rather than
    # trusting aria-selected (Canva uses it for keyboard focus).
    page.keyboard.press('Escape')
    actual = open_account(page)
    page.keyboard.press('Escape')
    if actual != name:
        return 'Canva did not confirm the selected team; invite was stopped'
    return ''


def open_team_settings(page, name):
    error = select(page, name)
    if error:
        return error
    error = canva_invite._open_settings(page)
    if error:
        notice = page.inner_text('body')[:1200].lower()
        if 'education plan has ended' in notice:
            return 'Canva says this team Education plan has ended, and no Invite button is available. Choose another team or check its admin permissions.'
        if 'downgraded to canva free' in notice:
            return 'Canva has downgraded this team to Free, and no Invite button is available. Choose another team or check its admin permissions.'
        return 'Canva did not show an Invite button for this team. Check that this account has permission to invite members.'
    button = page.get_by_role('button', name=re.compile(r'More account and team options$'))
    if button.count() and button.first.is_visible():
        actual = open_account(page)
        page.keyboard.press('Escape')
        if actual != name:
            return 'People settings opened for another team; invite was stopped'
    else:
        matches = page.get_by_text(name, exact=True)
        if not any(matches.nth(i).is_visible() for i in range(matches.count())):
            return 'Selected team could not be verified on People settings; invite was stopped'
    return ''


def check(pw, name):
    browser = pw.chromium.launch(headless=False, args=['--no-sandbox','--disable-dev-shm-usage'])
    context = browser.new_context(storage_state=str(canva_invite.SESSION_FILE))
    page = context.new_page()
    try:
        error = open_team_settings(page, name)
        controls = page.get_by_role('button').evaluate_all("""es => es
          .filter(e=>e.getBoundingClientRect().width>0)
          .map(e=>(e.getAttribute('aria-label')||e.innerText||'').trim())
          .filter(s=>/invite|add (people|members|students|teachers)/i.test(s))
          .map(s=>s.slice(0,160))""")
        role = ''
        email_file = canva_invite.DATA_DIR / 'canva_email.txt'
        if email_file.exists():
            own_email = email_file.read_text(encoding='utf-8').strip()
            own_row = page.get_by_text(re.compile(r'^' + re.escape(own_email) + r'$', re.I))
            if own_row.count() == 1:
                role = own_row.evaluate("""e => {
                  for(let p=e.parentElement,depth=0;p&&depth<7;p=p.parentElement,depth++){
                    const text=p.innerText||'';
                    if(text.length>1500) break;
                    const role=text.match(/(?:^|\\n)(Owner|Administrator|Admin|Teacher|Student|Team member)(?:\\n|$)/i);
                    if(role) return role[1];
                  } return '';
                }""")
        return {'ok':not bool(error),'team_name':name,'can_invite':not bool(error),
                'note':error or 'Selected team verified on People settings',
                'invite_controls':controls,'team_role':role,'settings_url':page.url}
    except Exception as exc:
        return {'ok':False,'team_name':name,'can_invite':False,'note':str(exc)[:180]}
    finally:
        context.close()
        browser.close()
