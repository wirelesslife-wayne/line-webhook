"""Import Wayne's customer-facing common pictures into the desk library.

Usage: python -m desk.tools.upload_library <folder>
The folder holds the image files plus 圖卡清單.json: [{"filename","topic","summary","use_when","expires_on"?}].
Only files listed in the manifest are imported; a file whose manifest entry is removed is switched off, never deleted.
"""
import hashlib
import json
import secrets
import sys
from pathlib import Path
from .common import store
from ..media import image_info



def sniff(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    return None


def run(folder):
    folder = Path(folder)
    manifest = json.loads((folder / '圖卡清單.json').read_text(encoding='utf-8'))
    s = store()
    names = []
    for card in manifest:
        path = folder / card['filename']
        data = path.read_bytes()
        mime = sniff(data)  # some cards are JPEG saved with a .png name
        if not mime or len(data) > 10 * 1024 * 1024:
            print('SKIP (not a JPEG/PNG under 10MB):', card['filename'])
            continue
        image_info(data, mime)
        digest = hashlib.sha256(data).hexdigest()
        s.execute("""INSERT INTO desk_media_library(id,filename,topic,summary,use_when,content_type,data,size,sha256,active,expires_on)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s)
          ON CONFLICT(filename) DO UPDATE SET topic=EXCLUDED.topic,summary=EXCLUDED.summary,use_when=EXCLUDED.use_when,
            content_type=EXCLUDED.content_type,data=EXCLUDED.data,size=EXCLUDED.size,sha256=EXCLUDED.sha256,active=TRUE,expires_on=EXCLUDED.expires_on""",
                  (secrets.token_hex(16), card['filename'], card['topic'], card['summary'], card['use_when'], mime, data, len(data), digest, card.get('expires_on')))
        names.append(card['filename'])
    s.execute('UPDATE desk_media_library SET active=FALSE WHERE NOT (filename=ANY(%s))', (names,))
    print('imported', len(names), '| active', s.one('SELECT COUNT(*) n FROM desk_media_library WHERE active')['n'])


if __name__ == '__main__':
    run(sys.argv[1])
