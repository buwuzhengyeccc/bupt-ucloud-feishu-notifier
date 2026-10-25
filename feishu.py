import base64
import hashlib
import hmac
import json
import logging
import time

import httpx


log = logging.getLogger('ucloud_notify.push')


class PushError(Exception):
    pass


def signature(timestamp, secret):
    key = f'{timestamp}\n{secret}'.encode()
    return base64.b64encode(hmac.new(key, b'', hashlib.sha256).digest()).decode()


class Feishu:
    def __init__(self, config):
        self.config = config
        self.last_sent = 0.0

    def send_text(self, text):
        return self.send({'msg_type': 'text', 'content': {'text': text}})

    def notice_payload(self, notices):
        content = []
        for notice in notices:
            content.append([{'tag': 'text', 'text': notice.course[:200]}])
            content.append([{'tag': 'text', 'text': f'{notice.time}  {notice.kind[:100]}'}])
            content.append([{'tag': 'text', 'text': notice.text[:1500] or '请打开通知页查看详情。'}])
            content.append([{'tag': 'a', 'text': '打开北邮消息通知', 'href': notice.link}])
        return {'msg_type': 'post', 'content': {'post': {'zh_cn': {
            'title': f'北邮 ucloud：{len(notices)} 条新通知', 'content': content}}}}

    def batches(self, notices):
        batch = []
        for notice in notices:
            candidate = batch + [notice]
            size = len(json.dumps(self.notice_payload(candidate), ensure_ascii=False).encode('utf-8'))
            if batch and (len(candidate) > 10 or size > 18000):
                yield batch
                batch = [notice]
            else:
                batch = candidate
        if batch:
            yield batch

    def send(self, payload):
        last_error = 'network'
        for attempt in range(4):
            time.sleep(max(0, self.last_sent + 0.7 - time.monotonic()))
            body = dict(payload)
            if self.config.secret:
                timestamp = str(int(time.time()))
                body.update(timestamp=timestamp, sign=signature(timestamp, self.config.secret))
            try:
                started = time.monotonic()
                response = httpx.post(self.config.webhook, json=body, timeout=20,
                                      follow_redirects=False, trust_env=False)
                self.last_sent = time.monotonic()
                if response.status_code == 200:
                    result = response.json()
                    if not isinstance(result, dict):
                        raise ValueError('invalid_response_shape')
                    code = result.get('code', result.get('StatusCode'))
                    if code == 0:
                        log.info('feishu_request_ok msg_type=%s attempt=%d elapsed_ms=%d',
                                 payload.get('msg_type', 'unknown'), attempt + 1,
                                 round((time.monotonic() - started) * 1000))
                        return
                    last_error = f'feishu_code_{code}' if isinstance(code, int) else 'invalid_response'
                    if code not in (11232, 9499):
                        log.warning('feishu_request_rejected reason=%s retry=false', last_error)
                        raise PushError(last_error)
                else:
                    last_error = f'http_{response.status_code}'
                    if response.status_code < 500 and response.status_code != 429:
                        log.warning('feishu_request_rejected reason=%s retry=false', last_error)
                        raise PushError(last_error)
            except (httpx.HTTPError, ValueError):
                last_error = 'network_or_invalid_json'
            if attempt < 3:
                log.warning('feishu_request_retry reason=%s attempt=%d retry_in_s=%d', last_error, attempt + 1, 2 ** (attempt + 1))
                time.sleep(2 ** (attempt + 1))
        log.warning('feishu_request_failed reason=%s attempts=4', last_error)
        raise PushError(last_error)
