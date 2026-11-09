"""Central outbound gateway. Only authenticated desk actions invoke this gateway.

No automatic retry: ambiguous requests keep their retry key for staff reconciliation.
"""
import os
import sys
import requests


def delivery_result(code, accepted_request=None):
    accepted = code == 200 or (code == 409 and bool(accepted_request))
    return {'ok':accepted,'status':code,'unknown':code>=500,
            'reason':'' if accepted else {
                400:'LINE 拒絕內容或圖片格式，請檢查後再送',
                401:'LINE 授權失效，請通知管理者',
                403:'LINE 權限不足，請通知管理者',
                429:'LINE 額度或速率受限，請稍後處理',
            }.get(code,'LINE 回應尚未確認，請保留內容並使用原送出確認重試')}


class LineNotifier:
    def push(self, user_id, text, images, retry_key):
        if 'pytest' in sys.modules or os.environ.get('DESK_TEST') == '1':
            raise RuntimeError('測試禁止實際 LINE 傳送；請注入 FakeNotifier')
        if os.environ.get('DESK_SEND_ENABLED') != '1':
            return {'ok': False, 'status': 0, 'unknown': False, 'reason': '目前未啟用實際傳送，請由管理者審核後開啟'}
        token = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
        if not token:
            return {'ok': False, 'status': 0, 'unknown': False, 'reason': 'LINE 傳送設定尚未完成'}
        messages = [{'type': 'text', 'text': text}]
        messages += [{'type': 'image', 'originalContentUrl': u, 'previewImageUrl': u} for u in images]
        try:
            res = requests.post('https://api.line.me/v2/bot/message/push',
                headers={'Authorization': 'Bearer '+token, 'X-Line-Retry-Key': retry_key},
                json={'to': user_id, 'messages': messages}, timeout=15)
        except requests.RequestException:
            return {'ok': False, 'status': 0, 'unknown': True,
                    'reason': 'LINE 回應不明；請保留內容，10 秒後以同一筆確認重試，勿另開新訊息'}
        return delivery_result(res.status_code,res.headers.get('x-line-accepted-request-id'))
