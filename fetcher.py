from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
import json
import logging
from pathlib import Path
import time
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx

from config import ROOT, NOTICE_URL, DISPLAY_TZ
from login import login, LoginError
from security import write_json


LIST_PATH = '/ykt-basics/api/inform/news/list'
SESSION_PATH = ROOT / 'data' / 'api-session.json'
log = logging.getLogger('ucloud_notify.auth')


class SchoolError(Exception):
    pass


class SessionExpired(Exception):
    pass


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1
        if tag in ('br', 'p', 'div', 'li'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def plain_text(value):
    parser = _Text()
    parser.feed(str(value or ''))
    return ' '.join(''.join(parser.parts).split())


@dataclass
class Notice:
    id: str
    course: str
    time: str
    text: str
    kind: str
    link: str = NOTICE_URL


def _headers(headers):
    allowed = {'accept', 'authorization', 'blade-auth', 'tenant-id', 'identity',
               'origin', 'referer', 'user-agent', 'content-type'}
    return {k.lower(): v for k, v in headers.items() if k.lower() in allowed}


def save_session(captures):
    notices = [x for x in captures if urlsplit(x['url']).path == LIST_PATH]
    tokens = [x for x in captures if urlsplit(x['url']).path == '/ykt-basics/oauth/token'
              and isinstance(x.get('payload'), dict)
              and isinstance(x.get('response'), dict) and x['response'].get('access_token')]
    if not notices or not tokens:
        raise LoginError('notice_api_or_refresh_not_observed')
    notice, token = notices[-1], tokens[-1]
    if token['payload'].get('grant_type') == 'refresh_token':
        refresh_payload = dict(token['payload'])
    else:
        bindings = [x for x in captures if urlsplit(x['url']).path == '/ykt-basics/userroledomaindept/listByUserId'
                    and isinstance(x.get('response'), dict)]
        roles = bindings[-1]['response'].get('data', []) if bindings else []
        if not isinstance(roles, list) or len(roles) != 1 or not roles[0].get('id'):
            raise LoginError('active_role_not_observed')
        # Verified against the browser's refresh request: identity is this binding's id.
        refresh_payload = {'grant_type': 'refresh_token', 'identity': str(roles[0]['id'])}
    parsed = urlsplit(notice['url'])
    if parsed.hostname != 'apiucloud.bupt.edu.cn':
        raise LoginError('unexpected_api_host')
    session = {
        'url': urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', '')),
        'method': notice['method'], 'params': dict(parse_qsl(parsed.query)),
        'headers': _headers(notice['headers']), 'refresh_url': token['url'],
        'refresh_headers': _headers(token['headers']), 'refresh_payload': refresh_payload,
        'expires_at': time.time() + int(token['response']['expires_in']),
    }
    session['refresh_payload']['refresh_token'] = token['response']['refresh_token']
    session['refresh_headers'].pop('blade-auth', None)
    session['refresh_headers']['content-type'] = 'application/x-www-form-urlencoded'
    write_json(SESSION_PATH, session)
    log.info('auth_session_created expires_at=%s ttl_s=%d',
             datetime.fromtimestamp(session['expires_at'], DISPLAY_TZ).isoformat(timespec='seconds'),
             max(0, int(session['expires_at'] - time.time())))
    # Keep the minimal credential cache; discard the discovery dump with extra user data.
    (ROOT / 'data' / 'probe-private.json').unlink(missing_ok=True)
    (ROOT / 'data' / 'browser-state-pending.json').unlink(missing_ok=True)
    return session


class Fetcher:
    def __init__(self, config):
        self.config = config
        self.client = httpx.Client(timeout=30, follow_redirects=False, trust_env=False)
        self.session = None
        self.last_page_count = 0

    def _authenticate(self, reason='no_cached_session'):
        log.info('auth_recovery_requested reason=%s', reason)
        captures = login(self.config)
        self.session = save_session(captures)

    def _load(self):
        if self.session is None:
            if SESSION_PATH.exists():
                try:
                    self.session = json.loads(SESSION_PATH.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    raise SchoolError('invalid_saved_session') from None
                log.info('auth_session_loaded source=cache expires_at=%s ttl_s=%d',
                         datetime.fromtimestamp(self.session['expires_at'], DISPLAY_TZ).isoformat(timespec='seconds'),
                         max(0, int(self.session['expires_at'] - time.time())))
            else:
                self._authenticate()
        for field in ('url', 'refresh_url'):
            parsed = urlsplit(self.session.get(field, ''))
            if parsed.scheme != 'https' or parsed.hostname != 'apiucloud.bupt.edu.cn':
                raise SchoolError('invalid_saved_api_host')

    def refresh(self, reason='explicit_request'):
        self._load()
        session = self.session
        started = time.monotonic()
        log.info('auth_refresh_start reason=%s ttl_s=%d password_submitted=false',
                 reason, max(0, int(session['expires_at'] - time.time())))
        headers = dict(session['refresh_headers'])
        headers.pop('blade-auth', None)
        encoding = 'json' if 'json' in headers.get('content-type', '').lower() else 'data'
        try:
            response = self.client.post(session['refresh_url'], headers=headers,
                                       **{encoding: session['refresh_payload']})
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning('auth_refresh_failed reason=network_or_invalid_json error_type=%s', type(exc).__name__)
            raise SchoolError('refresh_network_or_invalid_json') from None
        if not isinstance(result, dict):
            log.warning('auth_refresh_failed reason=invalid_schema')
            raise SchoolError('refresh_invalid_schema')
        if response.status_code == 401 or result.get('error') in ('invalid_grant', 'invalid_token'):
            log.warning('auth_refresh_rejected http=%d', response.status_code)
            raise SessionExpired('refresh_rejected')
        if response.status_code != 200 or not isinstance(result.get('access_token'), str):
            log.warning('auth_refresh_failed reason=invalid_response http=%d', response.status_code)
            raise SchoolError(f'refresh_http_{response.status_code}')
        old = session['headers'].get('blade-auth', '')
        # This site's browser sends the raw JWT, without a Bearer prefix.
        prefix = old.split(' ', 1)[0] + ' ' if old.lower().startswith('bearer ') else ''
        session['headers']['blade-auth'] = prefix + result['access_token']
        session['refresh_payload']['refresh_token'] = result['refresh_token']
        session['expires_at'] = time.time() + int(result['expires_in'])
        write_json(SESSION_PATH, session)
        log.info('auth_refresh_ok expires_in_s=%d expires_at=%s elapsed_ms=%d password_submitted=false',
                 int(result['expires_in']),
                 datetime.fromtimestamp(session['expires_at'], DISPLAY_TZ).isoformat(timespec='seconds'),
                 round((time.monotonic() - started) * 1000))

    def _renew_or_authenticate(self, reason):
        try:
            self.refresh(reason=reason)
        except SessionExpired:
            self._authenticate(reason='refresh_rejected')

    def _page(self, current, size):
        params = dict(self.session['params'])
        params.update(current=current, size=size)
        try:
            response = self.client.request(self.session['method'], self.session['url'],
                                           headers=self.session['headers'], params=params)
        except httpx.HTTPError as exc:
            log.warning('notice_request_failed error_type=%s', type(exc).__name__)
            raise SchoolError('notice_network_error') from None
        if response.status_code == 401:
            raise SessionExpired('api_session_expired')
        if response.status_code != 200:
            raise SchoolError(f'notice_http_{response.status_code}')
        try:
            result = response.json()
        except ValueError:
            raise SchoolError('notice_invalid_json') from None
        if result.get('code') == 401:
            raise SessionExpired('api_session_expired')
        if result.get('code') != 200 or result.get('success') is not True:
            raise SchoolError('notice_business_error')
        data = result.get('data')
        if not isinstance(data, dict) or not isinstance(data.get('records'), list):
            raise SchoolError('notice_schema_changed')
        return data

    def fetch(self):
        self._load()
        if time.time() >= self.session['expires_at'] - 120:
            self._renew_or_authenticate('expiry_window')
        for attempt in range(2):
            try:
                records = {}
                current = 1
                while current <= 1000:
                    data = self._page(current, 100)
                    for record in data['records']:
                        if not record.get('id'):
                            raise SchoolError('notice_id_missing')
                        records[str(record['id'])] = record
                    if current >= int(data.get('pages', 1)):
                        break
                    current += 1
                else:
                    raise SchoolError('pagination_limit_reached')
                if len(records) < int(data.get('total', len(records))):
                    raise SchoolError('pagination_changed_retry_next_poll')
                self.last_page_count = current
                return [Notice(id=key, course=plain_text(item.get('newsTitle')),
                               time=str(item.get('newsCopyTime') or item.get('createTime') or ''),
                               text=plain_text(item.get('newsInfo')), kind=plain_text(item.get('type')))
                        for key, item in records.items()]
            except SessionExpired:
                if attempt:
                    raise LoginError('api_authentication_rejected') from None
                self._renew_or_authenticate('api_unauthorized')
        raise SchoolError('fetch_failed')

    def close(self):
        self.client.close()
