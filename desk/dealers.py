"""Which dealer stores to offer a customer (Wayne 11/15: a 雲林 customer got only the 嘉義 store, not 彰化 芳苑).

The program picks the stores by county, the AI only writes them up."""
import re

NEAR = {
    '基隆': ['新北', '台北'], '台北': ['新北'], '新北': ['台北'], '桃園': ['新北'], '新竹': ['桃園', '苗栗'],
    '苗栗': ['新竹', '台中'], '台中': ['彰化'], '彰化': ['台中'], '南投': ['台中'], '雲林': ['嘉義', '彰化'],
    '嘉義': ['台南'], '台南': ['嘉義', '高雄'], '高雄': ['屏東'], '屏東': ['高雄'], '宜蘭': [], '花蓮': [],
    '台東': [], '澎湖': [],
}
ALIAS = {'臺': '台', '桃竹苗': '', '雙北': '新北'}
COUNTY = re.compile('|'.join(NEAR))
PHONE = re.compile(r'TEL[:：]\s*([\d-]+)')


def stores(text):
    out = []
    for line in (text or '').splitlines():
        parts = [p.strip() for p in line.split('｜')]
        if len(parts) == 3:
            m = COUNTY.search(parts[1].replace('臺', '台'))
            out.append(dict(name=parts[0], address=parts[1], phone=parts[2], county=m.group(0) if m else ''))
    return out


def county_of(*texts):
    """The last county the customer named (their own words win over what the AI extracted)."""
    found = ''
    for t in texts:
        for m in COUNTY.finditer(str(t or '').replace('臺', '台')):
            found = m.group(0)
    return found


def tapped(text, convo):
    """The store whose phone number the customer tapped last in the LINE menu (「TEL：0981-104-442」)."""
    digits = [re.sub(r'\D', '', m.group(1)) for m in PHONE.finditer(convo or '')]
    if not digits:
        return None
    return next((s for s in stores(text) if re.sub(r'\D', '', s['phone']) == digits[-1]), None)


def for_customer(text, location, customer_words):
    """The dealer list cut down to the customer's county and its neighbours; the whole list when we don't know where they are."""
    county = county_of(location, customer_words)
    if not county:
        return text, ''
    want = [county] + NEAR.get(county, [])
    rows = [s for s in stores(text) if s['county'] in want]
    if not rows:
        return text, county
    rows.sort(key=lambda s: want.index(s['county']))
    rows = rows[:3]  # Wayne 11/15 20:44: at most 3, own county first
    head = (f'客人提到{county}。客人問門市、在哪裡裝，或這次需要介紹門市時，下面{county}和鄰近縣市的門市（已挑好，最多 3 間）全部列給客人'
            '（同縣市的放前面），不要只挑一家，也不要加清單以外的店；一律稱「經銷商門市」，不要說「合作」；客人沒問門市就不用列：\n')
    return head + '\n'.join(f"{s['name']}｜{s['address']}｜{s['phone']}" for s in rows) + '\n安裝項目、報價與時間請先電話確認。', county
