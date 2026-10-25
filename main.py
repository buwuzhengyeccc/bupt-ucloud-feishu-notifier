import argparse
import json
import logging
import sys

from config import Config, ConfigError
from feishu import Feishu, PushError
from login import login, LoginError
from fetcher import Fetcher, save_session, SchoolError
from service import run


def main():
    parser = argparse.ArgumentParser(description='北邮 ucloud 通知 → 飞书')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--test-push', action='store_true')
    mode.add_argument('--debug-login', action='store_true')
    mode.add_argument('--once', action='store_true')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--chrome', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    try:
        config = Config.load()
        print('CONFIG_OK required_fields=present webhook_format=valid', flush=True)
        if args.test_push:
            Feishu(config).send_text('北邮 ucloud → 飞书通知：连接测试成功（部署验证）')
            print('FEISHU_TEST_OK', flush=True)
        elif args.debug_login:
            captures = login(config, debug=True, headed=args.headed, channel='chrome' if args.chrome else None)
            save_session(captures)
            print('NOTICE_API_CONFIRMED path=/ykt-basics/api/inform/news/list', flush=True)
        elif args.once:
            fetcher = Fetcher(config)
            try:
                notices = fetcher.fetch()
                print(json.dumps([{'id': n.id, 'course': n.course, 'time': n.time,
                                   'type': n.kind, 'summary': n.text} for n in notices], ensure_ascii=False, indent=2))
                print(f'NOTICE_COUNT={len(notices)}', flush=True)
            finally:
                fetcher.close()
        else:
            run(config)
        return 0
    except (ConfigError, LoginError, PushError, SchoolError) as exc:
        print(f'FAILED {exc}', file=sys.stderr, flush=True)
        return 1
    except Exception as exc:
        print(f'FAILED {type(exc).__name__}', file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
