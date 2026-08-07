from __future__ import annotations

import argparse
import json

from .db import Base, engine, SessionLocal
from .models import Schedule
from .services.automation import monitor_and_act


def main() -> int:
    parser = argparse.ArgumentParser(description='Home Assistant Fleet Manager automation worker')
    parser.add_argument('--execute', action='store_true', help='install eligible low-risk updates after deterministic review')
    parser.add_argument('--dry-run', action='store_true', help='scan/review only; never install')
    args = parser.parse_args()
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if db.query(Schedule).count() == 0:
            db.add(Schedule(name='Default monitor cadence', kind='monitor_and_act', cron='0 */6 * * *', enabled=True))
            db.commit()
        summary = monitor_and_act(db, auto_execute=args.execute and not args.dry_run, actor='cron')
        db.commit()
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
