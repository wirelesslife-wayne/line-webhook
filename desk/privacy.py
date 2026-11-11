"""Last-mile text redaction for every external AI request; never upload media."""
import re


PLATE = re.compile(r'(?i)(?<![A-Z0-9])(?:([A-Z]{2,3})[- ]?(\d{3,4})|(\d{3,4})[- ]?([A-Z]{2,3}))(?![A-Z0-9])')


def plate(models):
    """Mask plate-shaped text, except a car model code next to a model year ("CC 2026", "2020 CRV")."""
    def replace(m):
        letters, digits = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        if letters.upper() in models and 1990 <= int(digits) <= 2035:
            return m.group(0)
        return '[車牌]'
    return replace


def redact(text, keep=(), models=frozenset()):
    """keep: our own published strings (shop addresses and phones) that must reach the model unmasked.
    models: upper-case 2-3 letter car model codes from our price table (CC, CRV, HRV, NX ...)."""
    text = re.sub('[\ue000-\uf8ff]', '', str(text or ''))
    held = {}
    for item in sorted(set(keep), key=len, reverse=True):
        if item and item in text and len(held) < 6000:
            token = chr(0xE000+len(held))
            held[token] = item
            text = text.replace(item, token)
    patterns = [
        (r'(?i)(?<![A-Z0-9])U[0-9a-f]{32}(?![A-Z0-9])', '[LINE識別碼]'),
        (r'(?i)(?<![A-Z0-9])[A-Z][12]\d{8}(?![A-Z0-9])', '[身分證號]'),
        (r'(?i)(?<![A-Z0-9])[A-Z][89A-D]\d{8}(?![A-Z0-9])', '[居留證號]'),
        (r'(?i)(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![A-Z0-9.-])', '[Email]'),
        (r'(?<!\d)(?:\+?886[ ()-]*(?:0[ ()-]*)?(?:9(?:[ ()-]*\d){8}|[2-8](?:[ ()-]*\d){7,8})|09(?:[ ()-]*\d){8}|0[2-8](?:[ ()-]*\d){7,8})(?!\d)', '[電話]'),
        (PLATE, plate(models)),
        (r'(?:車牌|牌照)\s*[:：]?\s*[A-Za-z0-9 -]{4,12}', '車牌：[車牌]'),
        (r'(姓名|名字|聯絡人|稱呼|地址|住址|身分證|身份證|LINE\s*ID|userId)\s*[:：]\s*[^\s，。；,;\n]{1,100}', r'\1：[個資]'),
        (r'(?:我叫|姓名是|名字是)\s*[\u4e00-\u9fff]{2,4}(?=[，。；,;\s]|$)', '[姓名]'),
        (r'[\u4e00-\u9fff]{2,3}[縣市][\u4e00-\u9fff]{1,4}[區鄉鎮市][^\s，。；,;\n]{1,60}(?:號|樓)', '[地址]'),
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    for token, item in held.items():
        text = text.replace(token, item)
    return text
