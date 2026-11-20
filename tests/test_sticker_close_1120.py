"""Wayne 11/20 (劉俊毅, Jex): a colleague's sticker after a customer's plain closing line closes a LINE conversation."""
from desk.fb import sticker_close


def rows(*pairs):
    out = []
    for p in pairs:
        role, text, note = (p + ('',))[:3]
        out.append(dict(role=role, content=text, note=note))
    return out


def test_booking_confirmed_with_a_sticker_closes_and_says_register_it():
    r = rows(('user', '星期五不方便。那下個星期一或是下星期二可以嗎？'),
             ('page', '下週一 11/23（一）下午 13:00 後都可以協助安排喔！想請問您當天幾點比較方便呢？'),
             ('user', '那就下星期一下午一點，謝謝您的幫忙。'), ('page', '[貼圖]', '暫回'))
    assert sticker_close(r) == 'sticker_booking'
    r2 = rows(('page', '好的～那幫您安排下週二 11/24（二）早上 10:00，請問這個時間方便嗎？'), ('user', '可以的，到時見'), ('page', '[貼圖]', '暫回'))
    assert sticker_close(r2) == 'sticker_booking'
    assert sticker_close(rows(('page', '已幫您開立發票'), ('user', '謝謝'), ('page', '[貼圖]', '暫回'))) == 'sticker'


def test_questions_faults_photos_and_owed_answers_stay_open():
    base = ('page', '完工價 $18,000，施工約 3 小時。')
    for said in ('好的，那價格呢', '謝謝，但還是不能用', '好，我再考慮看看', '[圖片]', '螢幕壞了謝謝', '可以嗎？', '那就改天好了謝謝', '好的，幫我訂一台', '收到，請寄到公司', '好 我等等匯款'):
        assert sticker_close(rows(base, ('user', said), ('page', '[貼圖]', '暫回'))) == '', said
    owed = rows(('page', '這邊幫您確認庫存後回覆您'), ('user', '好的謝謝'), ('page', '[貼圖]', '暫回'))
    assert sticker_close(owed) == ''
    assert sticker_close(rows(base, ('user', '謝謝'))) == ''                         # no sticker yet
    assert sticker_close(rows(('user', '謝謝'), ('page', '[貼圖]', '暫回'))) == ''   # nothing of ours answered before
