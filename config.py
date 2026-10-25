from dataclasses import dataclass, field
from datetime import timezone, timedelta
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parent
NOTICE_URL = 'https://ucloud.bupt.edu.cn/uclass/index.html#/set/notice_fullpage'
CAS_URL = 'https://auth.bupt.edu.cn/authserver/login?service=https%3A%2F%2Fucloud.bupt.edu.cn&dev=1'
DISPLAY_TZ = timezone(timedelta(hours=8), 'Asia/Shanghai')


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    username: str = field(repr=False)
    password: str = field(repr=False)
    webhook: str = field(repr=False)
    secret: str = field(default='', repr=False)
    interval: int = 60

    @classmethod
    def load(cls, path=None):
        values = dotenv_values(path or ROOT / '.env', encoding='utf-8-sig', interpolate=False)
        def get(name, default=''):
            return os.environ.get(name, values.get(name) or default)
        try:
            interval = int(get('POLL_INTERVAL', '60'))
        except ValueError:
            raise ConfigError('POLL_INTERVAL must be an integer') from None
        if interval < 30:
            raise ConfigError('POLL_INTERVAL must be at least 30 seconds')
        result = cls(get('BUPT_USERNAME').strip(), get('BUPT_PASSWORD'),
                     get('FEISHU_WEBHOOK').strip(), get('FEISHU_SECRET').strip(), interval)
        if not result.username or not result.password:
            raise ConfigError('BUPT_USERNAME and BUPT_PASSWORD are required')
        try:
            parsed = urlsplit(result.webhook)
            valid = parsed.scheme == 'https' and parsed.hostname == 'open.feishu.cn'
            valid = valid and parsed.path.startswith('/open-apis/bot/v2/hook/')
            valid = valid and len(parsed.path.rsplit('/', 1)[-1]) >= 16 and not parsed.username
        except ValueError:
            valid = False
        if not valid:
            raise ConfigError('FEISHU_WEBHOOK must be a complete Feishu custom bot HTTPS webhook')
        return result
