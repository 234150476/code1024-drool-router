"""Run an immutable, isolated console job with existing, tested probe adapters."""
import argparse
import json
import os
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    root = Path(args.root)
    os.environ['DROOL_CONFIG'] = str(root / 'config.json')
    os.environ['DROOL_WORKER_SNAPSHOT'] = '1'
    # Imports must follow selection of the snapshot: legacy adapters load constants once.
    from . import config
    from .monitor import Store, execute_run, read_admin_key
    from .upstream import Sub2API
    from .schedule import current_slot
    from concurrent.futures import ThreadPoolExecutor

    os.umask(0o077)
    store = Store(root)
    api = Sub2API(config.get('base_url'), read_admin_key(os.environ.get('SUB2API_ADMIN_KEY_FILE', '')))
    accounts = api.accounts()
    expected = set(config.get('scope.account_ids'))
    targets = {a['id']: a for a in config.get('scope.targets', [])}
    accounts = [a for a in accounts if a['id'] in expected and a['status'] in ('active', 'inactive', 'error')
                and a['platform'] == targets[a['id']]['platform']]
    found = {a['id'] for a in accounts}
    (root / 'skipped.json').write_text(json.dumps([{'id': ident, 'reason': '账号已移除、平台/分组成员或状态已变化'}
                                                for ident in expected - found], ensure_ascii=False))
    if not accounts:
        return
    slot = current_slot()
    store.sync(accounts, slot, 'console')
    original_controls = store.controls

    class StopControls:
        def blocked(self, *values):
            return (root / 'stop').exists() or original_controls.blocked(*values)
        def read(self):
            return original_controls.read()
    store.controls = StopControls()
    # Keep overall deadline independent of the legacy timer's next cadence anchor.
    deadline = time.monotonic() + 10800
    rows = store.pending(slot)
    with ThreadPoolExecutor(max_workers=config.get('console.concurrency', 2)) as pool:
        list(pool.map(lambda row: execute_run(store, api, row, deadline), rows))
    store.publish_state()


if __name__ == '__main__':
    main()
