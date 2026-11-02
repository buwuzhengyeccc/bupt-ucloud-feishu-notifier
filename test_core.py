import tempfile
from contextlib import closing
from pathlib import Path
import unittest
import time
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
import httpx

from security import safe_url, shape
from storage import Store
from fetcher import Fetcher, SchoolError
from fetcher import Notice
from feishu import Feishu, PushError
from service import poll_once
from login import cas_state


class CoreTests(unittest.TestCase):
    def test_a_new_failure_does_not_overwrite_an_unsent_recovery_of_the_same_kind(self):
        with tempfile.TemporaryDirectory() as directory, closing(Store(Path(directory) / 'state.db')) as store:
            with patch('storage.time.time', return_value=1000):
                store.mark_alert('notice_list_not_loaded')
            with patch('storage.time.time', return_value=1100):
                store.mark_recovered()
            first = store.pending_recoveries()[0]
            with patch('storage.time.time', return_value=1200):
                store.record_failure('notice_list_not_loaded')
                self.assertFalse(store.alert_due('notice_list_not_loaded'))
            with patch('storage.time.time', return_value=1300):
                store.mark_recovered()
            self.assertEqual(store.pending_recoveries(), [first])
            with patch('storage.time.time', return_value=5000):
                store.record_failure('notice_list_not_loaded')
                self.assertTrue(store.alert_due('notice_list_not_loaded'))
                store.mark_alert('notice_list_not_loaded')
            with patch('storage.time.time', return_value=5100):
                store.mark_recovered()
            self.assertEqual([r['recovered_at'] for r in store.pending_recoveries()], [1100, 5100])
            store.finish_recovery(first['id'])
            self.assertEqual([r['recovered_at'] for r in store.pending_recoveries()], [5100])

    def test_instance_name_is_configurable_without_account_credentials(self):
        from config import Config, ConfigError
        values = {'BUPT_USERNAME': 'demo', 'BUPT_PASSWORD': 'dummy',
                  'FEISHU_WEBHOOK': 'https://open.feishu.cn/open-apis/bot/v2/hook/' + 'example-hook-0000'}
        with patch('config.dotenv_values', return_value=values), patch.dict('config.os.environ', {}, clear=True):
            self.assertEqual(Config.load().instance_name, '默认实例')
            with patch.dict('config.os.environ', {'INSTANCE_NAME': '第二账号'}):
                self.assertEqual(Config.load().instance_name, '第二账号')
            with patch.dict('config.os.environ', {'INSTANCE_NAME': 'invalid\nname'}):
                with self.assertRaises(ConfigError):
                    Config.load()

    def test_successful_poll_without_new_notices_sends_one_named_recovery(self):
        from service import notify_recoveries
        with tempfile.TemporaryDirectory() as directory, closing(Store(Path(directory) / 'state.db')) as store:
            with patch('storage.time.time', return_value=1000):
                store.record_failure('notice_list_not_loaded')
                store.mark_alert('notice_list_not_loaded')
            sender = Mock()
            with patch('storage.time.time', return_value=1142):
                notify_recoveries(sender, store, '原账号', 0)
            message = sender.send_text.call_args.args[0]
            self.assertIn('恢复通知【原账号】', message)
            self.assertIn('notice_list_not_loaded', message)
            self.assertIn('0小时2分22秒', message)
            self.assertIn('当前共 0 条通知', message)
            notify_recoveries(sender, store, '原账号', 0)
            sender.send_text.assert_called_once()
            self.assertEqual(store.pending_recoveries(), [])

    def test_failed_recovery_push_is_retried_after_restart_with_original_recovery_time(self):
        from service import notify_recoveries
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            sender = Mock()
            with closing(Store(path)) as store:
                with patch('storage.time.time', return_value=1000):
                    store.mark_alert('active_role_not_observed')
                sender.send_text.side_effect = PushError('network')
                with patch('storage.time.time', return_value=1142):
                    notify_recoveries(sender, store, '第二账号', 30)
                self.assertEqual(len(store.pending_recoveries()), 1)
            with closing(Store(path)) as store:
                sender.send_text.side_effect = None
                with patch('storage.time.time', return_value=1800):
                    notify_recoveries(sender, store, '第二账号', 30)
                self.assertEqual(sender.send_text.call_count, 2)
                self.assertEqual(sender.send_text.call_args_list[0], sender.send_text.call_args_list[1])
                self.assertEqual(store.pending_recoveries(), [])

    def test_failed_or_suppressed_fault_alert_does_not_create_an_unmatched_recovery(self):
        from service import notify_recoveries, report_login_failure
        with tempfile.TemporaryDirectory() as directory, closing(Store(Path(directory) / 'state.db')) as store:
            sender = Mock()
            sender.send_text.side_effect = PushError('network')
            with self.assertRaises(PushError):
                report_login_failure(sender, store, 'notice_list_not_loaded', '原账号')
            sender.send_text.side_effect = None
            notify_recoveries(sender, store, '原账号', 44)
            sender.send_text.assert_called_once()
            for kind in ('login_cooldown', 'login_network_unavailable'):
                report_login_failure(sender, store, kind, '原账号')
            notify_recoveries(sender, store, '原账号', 44)
            sender.send_text.assert_called_once()
            self.assertEqual(store.pending_recoveries(), [])

    def test_service_only_reports_recovery_after_a_complete_successful_poll(self):
        from service import run
        from login import LoginError
        for recovers in (False, True):
            with self.subTest(recovers=recovers), tempfile.TemporaryDirectory() as directory:
                store = Store(Path(directory) / 'state.db')
                store.baseline([])
                source, sender, stop = Mock(), Mock(), Mock()
                sender.batches.return_value = []
                source.fetch.side_effect = [LoginError('notice_list_not_loaded'), []]
                source.session = {'expires_at': time.time() + 7200}
                source.last_page_count = 1
                stop.is_set.side_effect = [False, False, True] if recovers else [False, True]
                with patch('service.Store', return_value=store), patch('service.Fetcher', return_value=source), \
                     patch('service.Feishu', return_value=sender), patch('service.threading.Event', return_value=stop), \
                     patch('service.signal.signal'), patch('service.write_json'):
                    run(SimpleNamespace(interval=60, instance_name='第二账号'))
                messages = [c.args[0] for c in sender.send_text.call_args_list]
                self.assertIn('登录告警【第二账号】', messages[0])
                self.assertEqual(len(messages), 2 if recovers else 1)
                if recovers:
                    self.assertIn('恢复通知【第二账号】', messages[1])

    def test_alert_recovery_persists_until_acknowledged_and_keeps_first_recovery_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            with closing(Store(path)) as store:
                with patch('storage.time.time', return_value=1000):
                    store.record_failure('notice_list_not_loaded')
                with patch('storage.time.time', return_value=1010):
                    store.mark_alert('notice_list_not_loaded')
                with patch('storage.time.time', return_value=1050):
                    store.record_failure('notice_list_not_loaded')
            with closing(Store(path)) as store:
                with patch('storage.time.time', return_value=1142):
                    store.mark_recovered()
                pending = store.pending_recoveries()
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0], {'id': 1, 'kind': 'notice_list_not_loaded',
                    'started_at': 1000, 'alerted_at': 1010, 'recovered_at': 1142})
            with closing(Store(path)) as store:
                with patch('storage.time.time', return_value=1200):
                    store.mark_recovered()
                    self.assertFalse(store.alert_due('notice_list_not_loaded'))
                self.assertEqual(store.pending_recoveries(), pending)
                store.finish_recovery(pending[0]['id'])
                self.assertEqual(store.pending_recoveries(), [])
                with patch('storage.time.time', return_value=5000):
                    self.assertTrue(store.alert_due('notice_list_not_loaded'))

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
