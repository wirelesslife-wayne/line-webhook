"""The store's real opening days (Wayne 11/15: the November calendar card says 11/28 is open; it is not).

One list the drafting AI reads instead of guessing from the calendar picture. Sundays are closed; weekdays are
10:00-20:00; Saturdays alternate, so a Saturday is open or closed only when it is listed here, otherwise
「請跟門市確認」. Add next month's Saturdays and any closed day when the monthly calendar is posted."""
import re
from datetime import date, datetime, timedelta

WEEKDAY = '一二三四五六日'
SATURDAY_OPEN = {'2026-11-07', '2026-11-21'}   # 10:00-18:00
CLOSED = {'2026-11-14': '週六公休', '2026-11-28': '投票日公休'}
KNOWN_UNTIL = '2026-11-30'   # the last day the monthly calendar covers


def label(d):
    return f'{d.month}/{d.day}（{WEEKDAY[d.weekday()]}）'


def status(d):
    """('open', hours) / ('closed', why) / ('ask', why) for one day."""
    k = d.isoformat()
    if k in CLOSED:
        return 'closed', CLOSED[k]
    if d.weekday() == 6:
        return 'closed', '週日公休'
    if d.weekday() == 5:
        if k in SATURDAY_OPEN:
            return 'open', '10:00–18:00'
        return ('closed', '週六公休') if k <= KNOWN_UNTIL else ('ask', '週六輪休，月曆還沒出，請跟門市確認')
    if k > KNOWN_UNTIL:
        return 'ask', '下個月營業月曆還沒出，平日通常 10:00–20:00，請跟門市確認'
    return 'open', '10:00–20:00'


def table(today, days=21):
    lines = []
    for i in range(days):
        d = today + timedelta(days=i)
        state, why = status(d)
        lines.append(f"{label(d)} {'營業 ' + why if state == 'open' else ('公休（' + why + '）' if state == 'closed' else why)}")
    return '\n'.join(lines)


def prompt(now_local):
    today = now_local.date()
    return (f'\n\n今天是 {today.year}/{label(today)}，台北時間 {now_local:%H:%M}（Wayne 11/15：草稿會晚一點才送，日期要從今天算）。\n'
            '門市營業日（以這張表為準，優先於營業月曆圖卡、歷史問答、範例和其他規則；表上寫「請跟門市確認」的日子才寫跟門市確認後回覆）：\n'
            + table(today) +
            '\n日期一律寫成「11/16（一）」這種月／日＋星期，不要只寫「明天」「後天」「這週六」。'
            '客人說的「明天」「下週一」是從客人傳訊息那天算；那天如果已經過了（例如客人週六問明天，今天已經是週日），'
            '要說明那天已過或公休，改問或建議下一個營業日。\n')


DATE = re.compile(r'(?<![\d/])(1[0-2]|[1-9])/([1-3]\d|[1-9])(?![\d/])')
BOOKING = re.compile(r'預約|安排|方便|過來|回門市|來門市|到店|營業|有開|時段')
SAYS_CLOSED = re.compile(r'公休|休息|沒有營業|不營業|沒開|店休')


def date_flags(draft, now_local):
    """Dates in a draft that are already past, or a closed day written as if the store were open."""
    flags, today = [], now_local.date()
    for m in DATE.finditer(draft):
        try:
            d = date(today.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            continue
        if (today - d).days > 300:   # December talking about January
            d = date(today.year + 1, d.month, d.day)
        near = draft[max(0, m.start() - 8):m.end() + 20]
        if not BOOKING.search(near):
            continue   # 「11/9 安裝的主機」: talking about the past, fine
        if d < today:
            flags.append(f'草稿提到 {label(d)}，這天已經過了，請改成之後的日期')
        elif status(d)[0] == 'closed' and not SAYS_CLOSED.search(draft[m.end():m.end() + 15]) \
                and not SAYS_CLOSED.search(draft[draft.rfind('\n', 0, m.start()) + 1:m.start()]):  # 「公休日：11/15（日）、11/29（日）」
            flags.append(f'草稿提到 {label(d)}，那天門市{status(d)[1]}')
    return list(dict.fromkeys(flags))


def taipei(at, tz):
    """'2026-11-14T08:54:23.178Z' -> '11/14（六）16:54'; anything else (FB's 「上午8:31」) is left alone."""
    try:
        t = datetime.fromisoformat(str(at).replace('Z', '+00:00'))
    except ValueError:
        return at
    if t.tzinfo is None:
        return at
    t = t.astimezone(tz)
    return f'{label(t.date())}{t:%H:%M}'
