import json
import os
from datetime import datetime
from pathlib import Path
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, Error as PlaywrightError, TimeoutError as BrowserTimeout

from config import ROOT, NOTICE_URL, CAS_URL, DISPLAY_TZ
from security import safe_url, shape, write_json


class LoginError(Exception):
    pass


class Observer:
    def __init__(self):
        self.private = []
        self.report = []

    def response(self, response):
        request = response.request
        if request.resource_type not in ('xhr', 'fetch', 'document'):
            return
        host = urlsplit(request.url).hostname or ''
        if not host.endswith('.bupt.edu.cn'):
            return
        try:
            headers = request.all_headers()
            item = {'url': safe_url(request.url), 'method': request.method,
                    'header_names': sorted(headers), 'status': response.status}
            if request.resource_type == 'document':
                item['resource_type'] = 'document'
                self.report.append(item)
                return
            payload = None
            if request.post_data:
                try:
                    payload = request.post_data_json
                    item['request_shape'] = shape(payload)
                except Exception:
                    item['request_body'] = 'non-json'
            result = None
            if 'json' in response.headers.get('content-type', '').lower():
                try:
                    result = response.json()
                    item['response_shape'] = shape(result)
                except Exception:
                    item['response_shape'] = 'unavailable'
            self.report.append(item)
            self.private.append({'url': request.url, 'method': request.method,
                                 'headers': headers, 'payload': payload, 'response': result})
        except Exception:
            self.report.append({'url': safe_url(request.url), 'status': response.status,
                                'inspection': 'unavailable'})

    def finished(self, request):
        if request.resource_type not in ('xhr', 'fetch'):
            return
        try:
            response = request.response()
            if response:
                self.response(response)
        except PlaywrightError:
            pass


def _login_frame(page):
    for frame in page.frames:
        try:
            if frame.locator('input[name="username"]').first.is_visible() and frame.locator('input[name="password"]').first.is_visible():
                return frame
        except PlaywrightError:
            pass
    return None


def _screenshot(page, name):
    try:
        page.screenshot(path=str(ROOT / 'debug' / name), full_page=True,
                        mask=[frame.locator('input,textarea') for frame in page.frames])
    except PlaywrightError:
        pass


def cas_state(state):
    return {
        'cookies': [c for c in state['cookies'] if c['domain'].lstrip('.') == 'auth.bupt.edu.cn'],
        'origins': [o for o in state['origins'] if urlsplit(o['origin']).hostname == 'auth.bupt.edu.cn'],
    }


def login(config, *, debug=False, headed=False, channel=None):
    data = ROOT / 'data'
    debug_dir = ROOT / 'debug'
    data.mkdir(exist_ok=True, mode=0o700)
    debug_dir.mkdir(exist_ok=True, mode=0o700)
    guard_path = data / 'login-guard.json'
    guard = json.loads(guard_path.read_text()) if guard_path.exists() else {}
    if time.time() < guard.get('next_attempt', 0):
        print(f'LOGIN_COOLDOWN failures={guard.get("failures", 0)} retry_in_s={max(0, int(guard["next_attempt"] - time.time()))}', flush=True)
        raise LoginError('login_cooldown')
    observer = Observer()
    print(f'LOGIN_STAGE=browser_start cas_cache_available={str((data / "browser-state.json").exists()).lower()}', flush=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed, channel=channel,
            ignore_default_args=['--enable-automation'],
            args=['--disable-dev-shm-usage', '--disable-blink-features=AutomationControlled'])
        settings = {'viewport': {'width': 1440, 'height': 1000}, 'locale': 'zh-CN'}
        if not headed:
            platform = 'Windows NT 10.0; Win64; x64' if os.name == 'nt' else 'X11; Linux x86_64'
            settings['user_agent'] = f'Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{browser.version} Safari/537.36'
        state_path = data / 'browser-state.json'
        if state_path.exists():
            saved = json.loads(state_path.read_text(encoding='utf-8'))
            # Reuse CAS SSO and its browser protection state, not stale ucloud JWT cookies.
            settings['storage_state'] = cas_state(saved)
        context = browser.new_context(**settings)
        context.on('response', lambda response: observer.response(response) if response.request.resource_type == 'document' else None)
        context.on('requestfinished', observer.finished)
        page = context.new_page()
        page.set_default_timeout(15000)
        submitted = False
        try:
            print('LOGIN_STAGE=open_cas_native_form', flush=True)
            page.goto(CAS_URL, wait_until='domcontentloaded', timeout=60000)
            frame = None
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                frame = _login_frame(page)
                if frame or urlsplit(page.url).hostname == 'ucloud.bupt.edu.cn':
                    break
                page.wait_for_timeout(1000)
            if not frame and urlsplit(page.url).hostname != 'ucloud.bupt.edu.cn':
                _screenshot(page, 'login-page.png')
                write_json(debug_dir / 'page-structure.json', {
                    'pages': [{'url': safe_url(p.url), 'frames': [safe_url(f.url) for f in p.frames]} for p in context.pages],
                    'input_names': page.locator('input').evaluate_all('(els) => els.map(e => ({name:e.name, type:e.type, id:e.id}))'),
                    'title': page.title()})
                raise LoginError('login_form_not_found')
            if frame:
                if frame.locator('#captchaParent').is_visible():
                    raise LoginError('captcha_required')
                print('LOGIN_STAGE=submit_once password_submitted=true attempt=1', flush=True)
                frame.locator('input[name="username"]').fill(config.username)
                frame.locator('input[name="password"]').fill(config.password)
                submitted = True
                write_json(guard_path, {'failures': guard.get('failures', 0), 'next_attempt': time.time() + 120})
                buttons = frame.locator('input[name="submit"],button[type="submit"],input[type="submit"]')
                clicked = False
                for index in range(buttons.count()):
                    button = buttons.nth(index)
                    if button.is_visible():
                        button.click()
                        clicked = True
                        break
                if not clicked:
                    frame.locator('input[name="password"]').press('Enter')
            else:
                print('LOGIN_STAGE=reuse_cas_session password_submitted=false', flush=True)
            deadline = time.monotonic() + 50
            while time.monotonic() < deadline:
                page.wait_for_timeout(1000)
                host = urlsplit(page.url).hostname or ''
                if host == 'ucloud.bupt.edu.cn' and _login_frame(page) is None:
                    break
                current = _login_frame(page)
                if current and current.locator('#captchaParent').is_visible():
                    raise LoginError('captcha_required')
            else:
                raise LoginError('login_not_completed')
            print('LOGIN_STAGE=cas_authenticated_waiting_for_ucloud', flush=True)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                cookies = {c['name']: c['value'] for c in context.cookies('https://ucloud.bupt.edu.cn')}
                if cookies.get('iClass-token') and cookies.get('iClass-refresh_token'):
                    break
                page.wait_for_timeout(1000)
            else:
                raise LoginError('ucloud_login_initialization_timeout')
            print('LOGIN_STAGE=portal_token_ready_opening_notice_app', flush=True)
            page.goto(NOTICE_URL, wait_until='domcontentloaded', timeout=60000)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                lists = [x for x in observer.private if urlsplit(x['url']).path == '/ykt-basics/api/inform/news/list']
                if lists and isinstance(lists[-1].get('response'), dict) and lists[-1]['response'].get('code') == 200:
                    break
                page.wait_for_timeout(1000)
            else:
                raise LoginError('notice_list_not_loaded')
            if _login_frame(page):
                raise LoginError('session_not_accepted')
            if debug:
                _screenshot(page, 'notice-page.png')
            storage = context.storage_state()
            write_json(data / 'browser-state.json', cas_state(storage))
            write_json(data / 'probe-private.json', observer.private)
            write_json(debug_dir / 'network-structure.json', observer.report)
            write_json(debug_dir / 'session-summary.json', {
                'cookies': [{'name': c['name'], 'domain': c['domain'], 'expires': c.get('expires')} for c in storage['cookies']],
                'origins': [{'origin': o['origin'], 'storage_keys': [i['name'] for i in o['localStorage']]} for o in storage['origins']]})
            write_json(guard_path, {'failures': 0, 'next_attempt': 0})
            print(f'LOGIN_OK requests_observed={len(observer.report)}', flush=True)
            return observer.private
        except Exception as exc:
            reason = str(exc) if isinstance(exc, LoginError) else type(exc).__name__
            navigation = [r for r in observer.report if r.get('resource_type') == 'document']
            temporary = isinstance(exc, BrowserTimeout) or (navigation and navigation[-1]['status'] >= 500)
            if temporary:
                reason = 'login_network_unavailable'
            failures = guard.get('failures', 0) + (0 if temporary else 1)
            delay = 120 if temporary else (1800 if failures >= 3 or reason == 'captcha_required' else min(1800, 120 * 2 ** (failures - 1)))
            write_json(guard_path, {'failures': failures, 'next_attempt': time.time() + delay})
            retry_at = datetime.fromtimestamp(time.time() + delay, DISPLAY_TZ).isoformat(timespec='seconds')
            print(f'LOGIN_RETRY_SCHEDULED reason={reason} failures={failures} retry_in_s={delay} retry_at={retry_at}', flush=True)
            if debug:
                _screenshot(page, 'login-failure.png')
            write_json(data / 'browser-state.json', cas_state(context.storage_state()))
            write_json(debug_dir / 'network-structure.json', observer.report)
            raise LoginError(reason) from None
        finally:
            try:
                context.close()
                browser.close()
            except PlaywrightError:
                pass
            print('LOGIN_STAGE=browser_closed', flush=True)
