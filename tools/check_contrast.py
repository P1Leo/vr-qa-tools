#!/usr/bin/env python3
"""
Check that every theme of VR Log Inspector keeps log text readable.

For each theme it measures the WCAG contrast of all log text colours (every level and
every tag colour around the hue wheel) on plain, shaded, highlighted, selected and
focused rows. Anything below 4.5:1 fails, and the script exits with code 1.

    python tools/check_contrast.py vr-log-inspector/vr_log_inspector.py
"""
import re, colorsys, sys
SRC = sys.argv[1] if len(sys.argv) > 1 else 'vr-log-inspector/vr_log_inspector.py'
s = open(SRC).read()
def hx(c): c=c.lstrip('#'); return tuple(int(c[i:i+2],16) for i in (0,2,4))
def lum(rgb):
    f=lambda c: c/12.92 if c<=0.03928 else ((c+0.055)/1.055)**2.4
    r,g,b=[x/255 for x in rgb]; return 0.2126*f(r)+0.7152*f(g)+0.0722*f(b)
def cr(a,b):
    la,lb=sorted([lum(a),lum(b)],reverse=True); return (la+0.05)/(lb+0.05)
def mix(fg,a,bg): return tuple(round(a*x+(1-a)*y) for x,y in zip(fg,bg))
def parse(val, bg=None):
    val=val.strip()
    if val.startswith('#'): return hx(val)
    m=re.match(r'rgba\((\d+),(\d+),(\d+),([\d.]+)\)', val.replace(' ',''))
    if m: return mix(tuple(map(int,m.groups()[:3])), float(m.group(4)), bg)
    raise ValueError(val)
themes = re.findall(r'html\[data-theme="([\w-]+)"\]\{(.*?)\n\}', s, re.S)
worst_all = []
for name, body in themes:
    tok = dict(re.findall(r'--([\w-]+):\s*([^;]+);', body))
    bg = hx(tok['bg']); panel = hx(tok['panel']); p2 = parse(tok['panel-2'], panel)
    rows = {'plain':bg, 'zebra':parse(tok['zebra'],bg), 'hover':p2,
            'selected':parse(tok['sel'],bg), 'focused':parse(tok['focus'],bg)}
    checks = []
    for k in ('text','muted'):
        checks.append((f'{k} on panel', cr(parse(tok[k],panel),panel)))
        checks.append((f'{k} on log', min(cr(parse(tok[k],r),r) for r in rows.values())))
    checks.append(('placeholder (faint) on input', cr(parse(tok['faint'],p2),p2)))
    for lv,key,tint in (('error','l-e',.09),('warn','l-w',.07),('fatal','l-f',.14),('debug','l-d',0),('verbose','l-v',0)):
        c = hx(tok[key]) if tok[key].startswith('#') else None
        cand = list(rows.values())
        if tint: cand.append(mix(c if c else parse(tok[key],bg), tint, bg))
        fg = lambda r: parse(tok[key], r)
        checks.append((f'{lv} text', min(cr(fg(r), r) for r in cand)))
    # coloured tags across the hue wheel
    sat=float(tok['tag-s'].rstrip('%'))/100; lig=float(tok['tag-l'].rstrip('%'))/100
    tagmin = min(cr(tuple(round(x*255) for x in colorsys.hls_to_rgb(h/360,lig,sat)), r) for h in range(0,360,5) for r in rows.values())
    checks.append(('tag colours (worst hue)', tagmin))
    acc = hx(tok['accent']); ink = hx(tok['accent-ink'])
    checks.append(('button text on accent', cr(ink, acc)))
    print(f'\n{name}')
    for label, v in checks:
        flag = '' if v >= 4.5 else '   <-- below 4.5'
        print(f'  {label:30} {v:5.1f}{flag}')
        worst_all.append((v, name, label))
fails = [w for w in worst_all if w[0] < 4.5]
print('\nFAILS:', len(fails)); [print('  ', round(v,1), n, l) for v,n,l in fails]
sys.exit(1 if fails else 0)
