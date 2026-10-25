import logging
import random
import signal
import threading
import time

from config import ROOT
from feishu import Feishu, PushError
from fetcher import Fetcher, SchoolError
from login import LoginError
from security import write_json
from storage import Store


log = logging.getLogger('ucloud_notify')


def poll_once(fetcher, feishu, store):
    notices = fetcher.fetch()
    if not store.initialized():
        feishu.send_text(f'北邮 ucloud：监控已启动，当前共 {len(notices)} 条通知。已有通知已建立基线，之后只推送新增通知。')
        store.baseline(n.id for n in notices)
        log.info('baseline_initialized count=%d', len(notices))
        return len(notices), 0
    unseen = store.unseen(n.id for n in notices)
    pending = sorted((n for n in notices if n.id in unseen), key=lambda n: (n.time, n.id))
    if pending:
        log.info('notifications_detected count=%d', len(pending))
    sent = 0
    for batch in feishu.batches(pending):
        feishu.send(feishu.notice_payload(batch))
        store.mark_sent(n.id for n in batch)
        sent += len(batch)
        log.info('notifications_delivered count=%d ids=%s', len(batch), ','.join(n.id for n in batch))
    return len(notices), sent


def run(config):
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    store = Store()
    fetcher, feishu = Fetcher(config), Feishu(config)
    log.info('service_started poll_interval=%ds', config.interval)
    try:
        while not stop.is_set():
            started = time.monotonic()
            wait_seconds = config.interval + random.uniform(0, 10)
            try:
                total, delivered = poll_once(fetcher, feishu, store)
                elapsed_ms = round((time.monotonic() - started) * 1000)
                expires_at = fetcher.session['expires_at']
                write_json(ROOT / 'data' / 'heartbeat.json', {
                    'last_success': time.time(), 'total': total, 'last_delivered': delivered,
                    'elapsed_ms': elapsed_ms, 'auth_expires_at': expires_at,
                    'next_poll_due': time.time() + wait_seconds})
                log.info('poll_ok mode=api pages=%d total=%d delivered=%d elapsed_ms=%d auth_ttl_s=%d next_check_s=%.1f',
                         fetcher.last_page_count, total, delivered, elapsed_ms,
                         max(0, int(expires_at - time.time())), wait_seconds)
            except LoginError as exc:
                kind = str(exc)
                log.warning('login_unavailable reason=%s next_check_s=%.1f', kind, wait_seconds)
                try:
                    if kind not in ('login_cooldown', 'login_network_unavailable') and store.alert_due(kind):
                        feishu.send_text(f'北邮 ucloud 登录告警：{kind}。服务已启用登录退避，请检查账号、验证码或页面变化。')
                        store.mark_alert(kind)
                except Exception as alert_error:
                    log.warning('login_alert_unavailable type=%s', type(alert_error).__name__)
            except (SchoolError, PushError) as exc:
                log.warning('poll_delayed reason=%s next_check_s=%.1f', str(exc), wait_seconds)
            except Exception as exc:
                log.warning('poll_delayed unexpected_type=%s next_check_s=%.1f', type(exc).__name__, wait_seconds)
            stop.wait(wait_seconds)
    finally:
        fetcher.close()
        store.close()
        log.info('service_stopped')
