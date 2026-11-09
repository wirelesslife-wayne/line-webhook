"""Public product-image index. No whole-site image fallback."""
import re
from urllib.parse import urlsplit, urlunsplit
import requests
from .db import now

STORE_URL = 'https://carandroid.com.tw/wp-json/wc/store/v1/products'
KINDS = [
    ('box', r'安卓盒|android\s*box|Z8|Z系列'),
    ('mirror', r'後視鏡|後照鏡|四錄|S14|B42'),
    ('android', r'安卓|車機|主機|android|\b(?:EX|DX|MX|T9|T10|T3|A3\d|H2\d)\b'),
    ('camera', r'環景|360'), ('blind', r'盲點|BSD'),
    ('audio', r'音響|喇叭|重低音|擴大機|DSP'),
    ('tailgate', r'尾門|撐桿|腳踢'), ('light', r'氣氛燈|氛圍燈'),
    ('rear', r'吸頂|後座|背掛'), ('carplay', r'carplay'),
]


def norm(s):
    return re.sub(r'[\s_\-]', '', str(s or '').lower())


def kinds(s):
    found = {key for key,pattern in KINDS if re.search(pattern,s,re.I)}
    if 'box' in found:
        found.discard('android')
        found.discard('carplay')
    if 'android' in found:
        found.discard('carplay')
    return found


def original_candidates(url):
    """Same upload path only; never transcode or search unrelated images."""
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
        return []
    if not parts.path.lower().endswith('.webp'):
        return []
    stem = parts.path[:-5]
    # WordPress/CDN variants include photo.jpg.webp and photo-300x300.webp.
    stem = re.sub(r'\.(?:jpe?g|png)$', '', stem, flags=re.I)
    plain = re.sub(r'-\d+x\d+$', '', stem)
    plain = re.sub(r'-scaled$', '', plain)
    paths = [stem, plain + '-scaled', plain]
    return list(dict.fromkeys(urlunsplit((parts.scheme, parts.netloc, p + ext,
                                         parts.query, ''))
                              for p in paths for ext in ('.jpg', '.png')))


def resolve_original(url, head):
    for candidate in original_candidates(url):
        try:
            response = head(candidate, timeout=8, allow_redirects=False)
            if (response.status_code == 200 and
                    response.headers.get('Content-Type', '').split(';')[0].strip().lower()
                    in ('image/jpeg', 'image/png')):
                return candidate
        except requests.RequestException:
            continue
    return None


def product_images(products, head=None, resolutions=None):
    from .engine import image_url
    result = []
    resolved = resolutions if resolutions is not None else {}
    for p in products:
        name = p.get('name','')
        categories = [c.get('name','') for c in p.get('categories',[])]
        for img in p.get('images',[]):
            u = img.get('src','')
            if name and head and original_candidates(u):
                if u not in resolved:
                    resolved[u] = resolve_original(u, head)
                u = resolved[u] or ''
            if name and image_url(u):
                result.append({'name':name,'url':u,'categories':categories})
    return result


def fetch_catalog(fetch=requests.get):
    products = []
    total = None
    for page in range(1,101):
        r = fetch(STORE_URL, params={'per_page':100,'page':page},timeout=30)
        r.raise_for_status()
        rows = r.json()
        if total is None:
            total = int(r.headers['X-WP-Total'])
        if not isinstance(rows,list) or int(r.headers['X-WP-Total']) != total:
            raise ValueError('商品索引抓取期間總數改變')
        products.extend(rows)
        if len(products) >= total:
            break
        if not rows:
            raise ValueError('商品索引未抓齊')
    if len(products)!=total or len({p['id'] for p in products})!=total:
        raise ValueError('商品索引筆數不符')
    return products


def refresh_catalog(store, fetch=requests.get, head=requests.head):
    from .worker import utc_time
    with store.lock('desk-product-images') as acquired:
        if not acquired:
            return
        attempted = store.meta('product_images_attempt')
        if attempted and (now()-utc_time(attempted)).total_seconds()<86400:
            return
        store.set_meta('product_images_attempt',now().isoformat())
        try:
            products = fetch_catalog(fetch)
            resolutions = {}
            store.set_meta('product_images',product_images(products, head, resolutions))
            store.set_meta('product_image_originals', resolutions)
            store.set_meta('product_images_success',now().isoformat())
            store.set_meta('product_images_error',False)
        except Exception:
            store.set_meta('product_images_error',True)


def select_images(index, wlf, text='', extract=None):
    from .engine import image_url
    ex = extract or {}
    products = ex.get('products') or []
    if isinstance(products,str):
        products = [products]
    series = ex.get('series') or ''
    if isinstance(series,list):
        series = ' '.join(map(str,series))
    query = ' '.join(map(str,products))+' '+str(series)+' '+text
    wanted = kinds(query)
    model = norm(ex.get('model'))
    explicit = [norm(p) for p in products if len(norm(p))>=2]
    if not wanted and not explicit:
        return []
    candidates = list(index)
    for p in wlf.get('z',[]):
        if image_url(p.get('image','')):
            candidates.append({'name':p['name'],'url':p['image'],'categories':[]})
    known_models = {norm(c['model']) for c in wlf.get('cars',[]) if len(norm(c['model']))>=3}
    if not model:
        model = next((m for m in sorted(known_models,key=len,reverse=True) if m in norm(text)), '')
    ranked = []
    for item in candidates:
        name = str(item.get('name',''))
        name_norm = norm(name)
        if not image_url(item.get('url','')):
            continue
        item_kinds = kinds(name)
        if not item_kinds:
            item_kinds = kinds(' '.join(item.get('categories',[])))
        exact = any(p in name_norm for p in explicit)
        if wanted:
            if not (wanted & item_kinds):
                continue
        elif not exact:
            continue
        # Avoid a specific different vehicle even if a broad category matches.
        vehicle_models = {m for m in known_models if m in name_norm}
        if vehicle_models and (not model or not any(m in model or model in m for m in vehicle_models)):
            continue
        if series and norm(series) not in name_norm:
            continue
        rank = (4 if model and model in name_norm else 0)+(2 if exact else 0)
        ranked.append((rank,item))
    result, seen = [], set()
    for _,item in sorted(ranked,key=lambda x:-x[0]):
        if item['url'] not in seen:
            result.append({'name':item['name'],'url':item['url']})
            seen.add(item['url'])
        if len(result)==4:
            break
    return result
