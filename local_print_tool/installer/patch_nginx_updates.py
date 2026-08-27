# -*- coding: utf-8 -*-
# patch_nginx_updates.py — 在 printer-backend nginx 配置插入 /updates/ 静态 location
p = '/etc/nginx/sites-available/printer-backend'
with open(p, encoding='utf-8') as f:
    s = f.read()
block = '''
    # Self-update installer static files (local print tool updater.py)
    location /updates/ {
        alias /home/printer-backend/updates/;
        default_type application/octet-stream;
    }
'''
if '/updates/' in s:
    print('already patched')
else:
    anchor = '    location /socket.io/ {'
    assert anchor in s, 'anchor not found'
    s = s.replace(anchor, block + anchor, 1)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(s)
    print('patched OK')
