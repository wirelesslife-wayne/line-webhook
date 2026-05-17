#!/usr/bin/env python3
"""
排程結果寫入工具，供 cron job / shell script 呼叫。

用法:
  python report_result.py --name "每日報表" --status success --description "成功產出 150 筆"
  python report_result.py --name "備份任務" --status failed  --description "連線逾時"
  python report_result.py --name "已停用任務" --status disabled

狀態值: success | failed | stuck | disabled
"""
import argparse
import json
import pathlib
import sys
from datetime import datetime

DASHBOARD_JSON = pathlib.Path.home() / '.claude' / 'dashboard.json'

VALID_STATUSES = {'success', 'failed', 'stuck', 'disabled'}


def main():
    parser = argparse.ArgumentParser(description='更新 ~/.claude/dashboard.json 排程結果')
    parser.add_argument('--name',        required=True,  help='排程名稱（唯一識別用）')
    parser.add_argument('--status',      required=True,  choices=VALID_STATUSES, help='執行狀態')
    parser.add_argument('--description', default='',     help='簡短說明')
    args = parser.parse_args()

    if DASHBOARD_JSON.exists():
        with open(DASHBOARD_JSON, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        data = {'schedules': []}

    schedules = data.get('schedules', [])
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    for s in schedules:
        if s['name'] == args.name:
            s['last_run']    = now
            s['status']      = args.status
            s['description'] = args.description
            break
    else:
        schedules.append({
            'name':        args.name,
            'last_run':    now,
            'status':      args.status,
            'description': args.description,
        })

    data['schedules']    = schedules
    data['last_updated'] = now

    DASHBOARD_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(DASHBOARD_JSON, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f'[dashboard] {now}  {args.name} → {args.status}', file=sys.stderr)


if __name__ == '__main__':
    main()
