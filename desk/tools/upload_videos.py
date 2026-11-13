"""Load the titles of every public video on our YouTube channel into the desk (desk_videos).

Usage: python -m desk.tools.upload_videos
Needs yt-dlp on this machine. Reads public pages only, no sign-in. Run again any time; the server itself picks up
new uploads from the channel feed daily, so this is for the first load and for catching up on renamed videos.
"""
import subprocess
from .common import store
from ..videos import save

CHANNEL = 'https://www.youtube.com/channel/UCcOUUK9vW7Z_fOrgLM0QtRw/'


def listing(tab):
    out = subprocess.run(['yt-dlp', '--flat-playlist', '--no-update', '--encoding', 'utf-8',
                          '--extractor-args', 'youtube:lang=zh-TW',  # the titles as written, not YouTube's English translation
                          '--print', '%(id)s\t%(title)s', CHANNEL + tab],
                         capture_output=True, encoding='utf-8', timeout=600)
    return [tuple(line.split('\t', 1)) for line in out.stdout.splitlines() if '\t' in line]


def run():
    rows = listing('videos') + listing('shorts')
    if not rows:
        raise SystemExit('沒有讀到任何影片（yt-dlp 沒裝，或 YouTube 暫時讀不到），資料沒有變動')
    print('讀到', len(rows), '支，寫入', save(store(), rows), '支')


if __name__ == '__main__':
    run()
