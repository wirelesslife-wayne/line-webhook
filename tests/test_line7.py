"""Wayne 11/15 LINE fixes, second round (20:44): no false red warnings, at most 3 nearby stores, never 合作. Synthetic data."""
from datetime import datetime, timedelta, timezone

from desk import dealers
from desk.business_days import date_flags
from desk.engine import BANNED

NOW = datetime(2026, 11, 15, 21, 0, tzinfo=timezone(timedelta(hours=8)))
LIST = '\n'.join([
    '甲音響｜台南市東區一路1號｜0900-000-001', '乙音響｜嘉義縣朴子市二路2號｜0900-000-002',
    '丙音響｜高雄市鳳山區三路3號｜0900-000-003', '丁音響｜高雄市三民區四路4號｜0900-000-004',
    '戊汽車｜彰化縣芳苑鄉五路5號｜0900-000-005',
])


def test_a_closed_day_listed_as_closed_is_not_a_warning():
    assert date_flags('公休日：11/22（日）、11/29（日）。\n若想安排回店檢查，告訴小編方便的日期', NOW) == []
    assert date_flags('可以預約 11/29，當天幫您安排', NOW)


def test_at_most_three_stores_own_county_first():
    text, county = dealers.for_customer(LIST, '', '請問台南有分店嗎')
    rows = [l for l in text.splitlines() if '｜' in l]
    assert county == '台南' and len(rows) == 3 and rows[0].startswith('甲') and '合作' in text  # the rule names the word it bans
    assert '不要說「合作」' in text


def test_yunlin_reaches_changhua():
    text, _ = dealers.for_customer(LIST, '', '雲林有門市嗎')
    assert '戊汽車' in text and '乙音響' in text


def test_cooperation_word_is_banned():
    assert '合作' in BANNED
