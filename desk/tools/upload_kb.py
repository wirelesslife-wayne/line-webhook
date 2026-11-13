import json
from .common import WORKSPACE,store
from ..worker import validate_prices

FILES = ['口吻規則.md','canned_v2.json','faq_v2.json','install_hours.json','經銷商清單.txt','wlf-data.json','price_table_v2.json','line_saved_replies.json']


def run():
    source=WORKSPACE/'AI客服訓練'/'kb_v2'
    values={}
    for name in FILES:
        text=(source/name).read_text(encoding='utf-8')
        values[name]=json.loads(text) if name.endswith('.json') else text
    validate_prices(values['wlf-data.json'])
    db=store()
    for name,value in values.items():
        db.put_kb(name,value)
    count=db.one('SELECT COUNT(*) AS n FROM desk_kb WHERE name=ANY(%s)',(FILES,))['n']
    if count!=len(FILES):
        raise RuntimeError('知識庫筆數未對齊')
    print(json.dumps({'ok':True,'source_files':len(FILES),'verified_files':count}))


if __name__=='__main__':
    run()
