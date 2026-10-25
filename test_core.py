import tempfile
from pathlib import Path
import unittest
import time
import json
from unittest.mock import patch
import httpx

from security import safe_url, shape
from storage import Store
from fetcher import Fetcher, SchoolError
from fetcher import Notice
from feishu import Feishu, PushError
from service import poll_once
from login import cas_state


class CoreTests(unittest.TestCase):
    def test_baseline_survives_restart_and_only_acknowledged_ids_are_seen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            store = Store(path)
            store.baseline(['old'])
            store.close()
            store = Store(path)
            self.assertTrue(store.initialized())
            self.assertEqual(store.unseen(['old', 'new']), {'new'})
            # A failed push performs no acknowledgement, so it remains pending.
            self.assertEqual(store.unseen(['new']), {'new'})
            store.mark_sent(['new'])
            self.assertEqual(store.unseen(['old', 'new']), set())
            store.close()

    def test_diagnostics_keep_field_names_without_values(self):
        self.assertNotIn('private-session-value', safe_url('https://example.org/list?token=private-session-value'))
        self.assertEqual(shape({'token': 'private-session-value'}), {'token': 'str'})

    def test_refresh_preserves_the_sites_raw_jwt_header(self):
        def respond(request):
            if request.url.path.endswith('/token'):
                return httpx.Response(200, json={'access_token': 'new.jwt.value', 'refresh_token': 'private-refresh-value', 'expires_in': 7200})
            self.assertEqual(request.headers['blade-auth'], 'new.jwt.value')
            return httpx.Response(200, json={'code': 200, 'success': True, 'data': {
                'records': [{'id': 'one', 'newsTitle': '课程', 'newsInfo': '通知'}], 'total': 1, 'pages': 1}})
        with tempfile.TemporaryDirectory() as directory, patch('fetcher.SESSION_PATH', Path(directory) / 'session.json'):
            fetcher = Fetcher(None)
            fetcher.client.close()
            fetcher.client = httpx.Client(transport=httpx.MockTransport(respond))
            fetcher.session = {'url': 'https://apiucloud.bupt.edu.cn/ykt-basics/api/inform/news/list',
                'method': 'POST', 'params': {}, 'headers': {'blade-auth': 'old.jwt.value'},
                'refresh_url': 'https://apiucloud.bupt.edu.cn/ykt-basics/oauth/token',
                'refresh_headers': {'content-type': 'application/x-www-form-urlencoded'},
                'refresh_payload': {'refresh_token': 'old'}, 'expires_at': 0}
            with self.assertLogs('ucloud_notify.auth', level='INFO') as captured:
                self.assertEqual([n.id for n in fetcher.fetch()], ['one'])
            messages = '\n'.join(captured.output)
            self.assertIn('auth_refresh_start reason=expiry_window', messages)
            self.assertIn('auth_refresh_ok', messages)
            self.assertIn('password_submitted=false', messages)
            for private_value in ('old.jwt.value', 'new.jwt.value', 'private-refresh-value'):
                self.assertNotIn(private_value, messages)
            fetcher.close()

    def test_rejected_refresh_triggers_only_one_browser_recovery(self):
        calls = []
        def respond(request):
            calls.append(request.url.path)
            if request.url.path.endswith('/token'):
                return httpx.Response(400, json={'error': 'invalid_grant'})
            return httpx.Response(200, json={'code': 200, 'success': True, 'data': {'records': [], 'total': 0, 'pages': 0}})
        fetcher = Fetcher(None)
        fetcher.client.close()
        fetcher.client = httpx.Client(transport=httpx.MockTransport(respond))
        fetcher.session = {'url': 'https://apiucloud.bupt.edu.cn/list', 'method': 'POST', 'params': {},
            'headers': {'blade-auth': 'old.jwt.value'}, 'refresh_url': 'https://apiucloud.bupt.edu.cn/token',
            'refresh_headers': {}, 'refresh_payload': {'refresh_token': 'private-refresh'}, 'expires_at': 0}
        def recover(**kwargs):
            fetcher.session['expires_at'] = time.time() + 7200
        with patch.object(fetcher, '_authenticate', side_effect=recover) as authenticate:
            self.assertEqual(fetcher.fetch(), [])
            authenticate.assert_called_once_with(reason='refresh_rejected')
        self.assertEqual(calls.count('/token'), 1)
        fetcher.close()

    def test_regular_polls_reuse_credentials_without_authentication(self):
        fetcher = Fetcher(None)
        fetcher.client.close()
        calls = []
        def respond(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={'code': 200, 'success': True, 'data': {'records': [], 'total': 0, 'pages': 0}})
        fetcher.client = httpx.Client(transport=httpx.MockTransport(respond))
        fetcher.session = {'url': 'https://apiucloud.bupt.edu.cn/list', 'refresh_url': 'https://apiucloud.bupt.edu.cn/token',
            'method': 'POST', 'params': {}, 'headers': {'blade-auth': 'private-access'}, 'expires_at': time.time() + 7200}
        with patch.object(fetcher, 'refresh') as refresh, patch.object(fetcher, '_authenticate') as authenticate:
            self.assertEqual(fetcher.fetch(), [])
            self.assertEqual(fetcher.fetch(), [])
            refresh.assert_not_called()
            authenticate.assert_not_called()
        self.assertEqual(calls, ['/list', '/list'])
        fetcher.close()

    def test_school_server_errors_do_not_trigger_password_login(self):
        fetcher = Fetcher(None)
        fetcher.client.close()
        fetcher.client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503)))
        fetcher.session = {'url': 'https://apiucloud.bupt.edu.cn/list', 'refresh_url': 'https://apiucloud.bupt.edu.cn/token',
            'method': 'POST', 'params': {}, 'headers': {}, 'expires_at': time.time() + 7200}
        with patch.object(fetcher, '_authenticate') as authenticate:
            with self.assertRaises(SchoolError):
                fetcher.fetch()
            authenticate.assert_not_called()
        fetcher.close()

    def test_failed_delivery_remains_pending_until_a_successful_push(self):
        class Source:
            def fetch(self):
                return [Notice('new', '课程', '2026-10-25 12:00', '新通知', '作业')]
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state.db')
            store.baseline(['old'])
            sender = Feishu(None)
            with patch.object(sender, 'send', side_effect=PushError('network')):
                with self.assertRaises(PushError):
                    poll_once(Source(), sender, store)
            self.assertEqual(store.unseen(['new']), {'new'})
            with patch.object(sender, 'send', return_value=None):
                self.assertEqual(poll_once(Source(), sender, store), (1, 1))
            self.assertEqual(store.unseen(['new']), set())
            store.close()

    def test_cached_browser_state_only_retains_cas_session(self):
        state = {'cookies': [{'domain': 'auth.bupt.edu.cn', 'name': 'CASTGC'},
                             {'domain': 'ucloud.bupt.edu.cn', 'name': 'iClass-token'}],
                 'origins': [{'origin': 'https://auth.bupt.edu.cn', 'localStorage': []},
                             {'origin': 'https://ucloud.bupt.edu.cn', 'localStorage': []}]}
        cached = cas_state(state)
        self.assertEqual([c['name'] for c in cached['cookies']], ['CASTGC'])
        self.assertEqual(len(cached['origins']), 1)

    def test_rich_text_matches_bot_schema_and_batches_fit_the_byte_limit(self):
        sender = Feishu(None)
        notices = [Notice(str(i), '课程', '2026-10-25 12:00', '新通知' * 1500, '作业') for i in range(20)]
        batches = list(sender.batches(notices))
        self.assertEqual([n.id for b in batches for n in b], [n.id for n in notices])
        for batch in batches:
            payload = sender.notice_payload(batch)
            self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode('utf-8')), 18000)
            for paragraph in payload['content']['post']['zh_cn']['content']:
                for node in paragraph:
                    permitted = {'tag', 'text', 'un_escape'} if node['tag'] == 'text' else {'tag', 'text', 'href'}
                    self.assertTrue(set(node).issubset(permitted))


if __name__ == '__main__':
    unittest.main()
