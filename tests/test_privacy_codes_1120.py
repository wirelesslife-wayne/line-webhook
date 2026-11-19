"""11/20: our own product codes reached the AI as 「[車牌]」 (WRM-418 is plate-shaped)."""
from desk.privacy import redact


def test_our_product_codes_stay_and_plates_are_still_masked():
    assert redact('WRM-418 四錄電子後視鏡') == 'WRM-418 四錄電子後視鏡'
    assert redact('背掛 AP-101、DT-102，吸頂 CMC-1506MP5') == '背掛 AP-101、DT-102，吸頂 CMC-1506MP5'
    assert redact('我的車 ABC-1234') == '我的車 [車牌]'
    assert redact('DT-1023') == '[車牌]'
