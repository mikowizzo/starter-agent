"""chart_svg.py - dependency-free SVG price charts with hover + event overlays.

Public API:
    generate_price_svg(...) -> svg string
    standalone_html(svg, title) -> full HTML page with hover JS inlined
    HOVER_JS -> the shared <script> block (inline once per page)

All interactivity lives in HOVER_JS: one delegated listener drives every
<svg class="chart"> on the page. Each chart carries its own data in an
embedded <script type="application/json" class="chart-data"> node.
"""

import json

HOVER_JS = """<script>
(function(){
if(window.__chartHover)return;window.__chartHover=1;
var tip=document.createElement('div');
tip.style.cssText='position:fixed;pointer-events:none;background:#1a2236;color:#e2e8f0;'
 +'padding:6px 10px;font:12px/1.4 -apple-system,sans-serif;border-radius:6px;display:none;'
 +'z-index:9999;white-space:nowrap;border:1px solid #283046';
document.body.appendChild(tip);
function clean(){
  var c=document.querySelectorAll('.chart-x');
  for(var i=0;i<c.length;i++)c[i].style.display='none';
  tip.style.display='none';
}
document.addEventListener('pointermove',function(e){
  var hit=e.target&&e.target.closest?e.target.closest('.chart-hit'):null;
  if(!hit){if(!e.target.closest||!e.target.closest('svg.chart'))clean();return;}
  var svg=hit.closest('svg.chart');
  var d=JSON.parse(svg.querySelector('.chart-data').textContent);
  var r=svg.getBoundingClientRect();
  var k=d.W/r.width;
  var mx=(e.clientX-r.left)*k;
  var xs=d.xs,best=0,bd=1e18;
  for(var i=0;i<xs.length;i++){var dd=Math.abs(xs[i]-mx);if(dd<bd){bd=dd;best=i;}}
  var x=xs[best];
  var line=svg.querySelector('.chart-xl'),dot=svg.querySelector('.chart-xd');
  line.setAttribute('x1',x);line.setAttribute('x2',x);
  dot.setAttribute('cx',x);dot.setAttribute('cy',d.ys[best]);
  svg.querySelector('.chart-x').style.display='';
  tip.textContent=d.dates[best]+'  '+d.labels[best];
  tip.style.display='block';
  var tx=e.clientX+12;
  if(tx+tip.offsetWidth>innerWidth-8)tx=e.clientX-tip.offsetWidth-12;
  tip.style.left=tx+'px';tip.style.top=(e.clientY-32)+'px';
},{passive:true});
document.addEventListener('pointercancel',clean,true);
document.addEventListener('pointerleave',function(e){
  if(!e.relatedTarget)clean();
},true);
})();
</script>"""

_OVERLAY_PALETTE = ['#e07020', '#8050c0', '#3a9090', '#c05070', '#7a8a3a']


def _num(v):
    s = f"{v:.2f}".rstrip('0').rstrip('.')
    return s or '0'


def _nice_ticks(lo, hi, n=4):
    """Round-number ticks spanning [lo, hi]."""
    import math
    if hi <= lo:
        return [lo]
    span = hi - lo
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if step >= raw:
            break
    start = math.floor(lo / step) * step
    ticks, t = [], start
    while t <= hi + 1e-9:
        if t >= lo - 1e-9:
            ticks.append(round(t, 6))
        t += step
    return ticks


def generate_price_svg(dates, prices, title='', unit='', anchors=None,
                       overlays=None, stats=None,
                       width=720, height=300,
                       line_color='var(--accent, #34d399)',
                       danger_color='var(--danger, #f87171)',
                       text_color='var(--text, #e2e8f0)',
                       bg_color='var(--bg, #0b0f18)',
                       embed_js=False):
    """Render an interactive SVG price chart (dark-themed, CSS-var aware).

    dates/prices : aligned lists, len >= 2 (dates as datetime.date objects)
    anchors      : list of {'label': str, 'date': date}
    overlays     : list of {'label', 'dates', 'prices', 'color'?}
    stats        : dict with optional 'max_high', 'min_low', 'pct_change'
    embed_js     : if True, append HOVER_JS after the svg (standalone use)
    """
    anchors = anchors or []
    overlays = list(overlays or [])
    stats = stats or {}
    for i, o in enumerate(overlays):
        o.setdefault('color', _OVERLAY_PALETTE[i % len(_OVERLAY_PALETTE)])

    if len(dates) < 2 or len(dates) != len(prices):
        raise ValueError('need >=2 aligned date/price points')

    ML, MR, MT, MB = 58, 52, 36, 30
    X0, X1 = ML, width - MR
    YBOT, YTOP = height - MB, MT

    d0, d1 = dates[0].toordinal(), dates[-1].toordinal()
    span = max(1, d1 - d0)

    def X(dt):
        return X0 + (dt.toordinal() - d0) / span * (X1 - X0)

    # Find announcement anchor position and price (needed for overlay normalisation)
    anchor_x = X0
    announce_price = prices[0]
    for a in anchors:
        if a.get('name') == 'announcement':
            anchor_x = max(min(X(a['date']), X1), X0)
            ad = a['date'].toordinal()
            best_i, best_d = 0, 1e18
            for i, dt in enumerate(dates):
                dd = abs(dt.toordinal() - ad)
                if dd < best_d:
                    best_d, best_i = dd, i
            announce_price = prices[best_i]
            break

    # Determine y-range from main series + normalised overlays
    all_y = list(prices)
    for o in overlays:
        if o['dates'] and o['prices'][0] > 0:
            ratio = announce_price / o['prices'][0]
            all_y.extend(p * ratio for p in o['prices'])
    pmin, pmax = min(all_y), max(all_y)
    pad = (pmax - pmin) * 0.06 or 1.0
    pmin, pmax = pmin - pad, pmax + pad

    def Y(p):
        return YBOT - (p - pmin) / (pmax - pmin) * (YBOT - YTOP)

    xs = [X(dt) for dt in dates]
    ys = [Y(p) for p in prices]
    up = prices[-1] >= prices[0]
    lc = line_color if up else danger_color

    parts = []
    w = (f'<svg class="chart" viewBox="0 0 {width} {height}" '
         f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{title}" '
         f'style="max-width:100%;height:auto;display:block;'
         f'font-family:-apple-system,BlinkMacSystemFont,sans-serif">')

    # --- area gradient ---
    gid = 'g' + str(abs(hash(title)))[:8]
    w += (f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
          f'<stop offset="0%" style="stop-color:{lc};stop-opacity:.18"/>'
          f'<stop offset="100%" style="stop-color:{lc};stop-opacity:0"/>'
          f'</linearGradient></defs>')

    # --- y ticks + gridlines ---
    for t in _nice_ticks(pmin, pmax, 4):
        y = Y(t)
        w += (f'<line x1="{X0}" y1="{y:.1f}" x2="{X1}" y2="{y:.1f}" '
              f'stroke-width="1" style="stroke:{text_color}" stroke-opacity=".06"/>')
        w += (f'<text x="{X0 - 6}" y="{y + 3:.1f}" text-anchor="end" '
              f'font-size="11" style="fill:{text_color}" fill-opacity=".5">{_num(t)}</text>')

    # --- x labels ---
    for i in sorted({0, len(dates) // 3, 2 * len(dates) // 3, len(dates) - 1}):
        dt = dates[i]
        anchor = 'start' if i == 0 else ('end' if i == len(dates) - 1 else 'middle')
        w += (f'<text x="{X(dt):.1f}" y="{height - 8}" text-anchor="{anchor}" '
              f'font-size="11" style="fill:{text_color}" fill-opacity=".45">'
              f'{dt.strftime("%b %d")}</text>')

    # --- title ---
    if title:
        w += (f'<text x="{X0}" y="16" style="fill:{text_color}" font-size="13" '
              f'font-weight="600">{title}</text>')

    # --- pct-change chip ---
    pct = stats.get('pct_change')
    if pct is not None:
        sign = '+' if pct >= 0 else ''
        w += (f'<text x="{X0}" y="30" font-size="11" font-weight="700" '
              f'style="fill:{lc}">{sign}{pct:.1f}%</text>')

    # --- area fill ---
    pts_str = ' '.join(f'{x:.1f},{y:.1f}' for x, y in zip(xs, ys))
    w += (f'<polygon points="{X0:.1f},{ys[0]:.1f} {pts_str} {X1:.1f},{ys[-1]:.1f} '
          f'{X1:.1f},{YBOT} {X0:.1f},{YBOT}" fill="url(#{gid})"/>')

    # --- event-aligned overlays: overlay day 0 = announcement anchor ---
    # Overlays are normalised: each starts at the main series' price on the
    # announcement date, so all lines share a common origin for comparison.
    # (anchor_x and announce_price computed above)
    days_from_anchor = round((anchor_x - X0) / (X1 - X0) * span) if X1 > X0 else 0
    overlay_span = max(1, span - days_from_anchor)
    cropped = []
    for o in overlays:
        if not o['dates']:
            continue
        od0 = o['dates'][0].toordinal()
        o_base = o['prices'][0]
        pts = []
        for dt, p in zip(o['dates'], o['prices']):
            day_offset = dt.toordinal() - od0
            if day_offset <= overlay_span and o_base > 0:
                px = anchor_x + day_offset / overlay_span * (X1 - anchor_x)
                # Normalise: scale so overlay starts at announce_price
                norm_p = announce_price * (p / o_base)
                pts.append((px, Y(norm_p)))
        if pts:
            cropped.append((o, pts))
    for o, pts in cropped:
        s = ' '.join(f'{px:.1f},{py:.1f}' for px, py in pts)
        w += (f'<polyline points="{s}" fill="none" stroke="{o["color"]}" '
              f'stroke-width="1.5" stroke-dasharray="5 4" opacity="0.35"/>')

    # --- anchors ---
    anchor_colors = {'pre_announcement': (text_color, '.5'),
                     'announcement': (lc, '1'),
                     'current': (text_color, '.9')}
    placed = []
    for a in anchors:
        name = a.get('name', a.get('label', ''))
        ax = max(min(X(a['date']), X1), X0)
        col, op = anchor_colors.get(name, (text_color, '.7'))
        # Collision stagger
        tier = 0
        while any(abs(ax - px) < 70 and t == tier for px, t in placed):
            tier += 1
        placed.append((ax, tier))
        ly = YTOP - 2 + tier * 13
        label_text = a.get('label', name)
        w += (f'<line x1="{ax:.1f}" y1="{YTOP + 4}" x2="{ax:.1f}" y2="{YBOT}" '
              f'stroke-dasharray="3 3" style="stroke:{col}" '
              f'stroke-opacity="{float(op) * .6:.2f}"/>')
        w += (f'<text x="{ax:.1f}" y="{ly}" text-anchor="middle" font-size="9" '
              f'font-weight="600" style="fill:{col}" fill-opacity="{op}">'
              f'{label_text}</text>')

    # --- main price line ---
    w += (f'<polyline points="{pts_str}" fill="none" stroke-width="2" '
          f'stroke-linejoin="round" stroke-linecap="round" style="stroke:{lc}"/>')

    # --- current price dot + label ---
    cx, cy = xs[-1], ys[-1]
    w += (f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" style="fill:{lc}"/>')
    w += (f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="8" style="fill:{lc}" '
          f'fill-opacity=".2"/>')
    # Label to the right of the dot, like HIGH/LOW labels
    w += (f'<text x="{X1 + 4}" y="{cy + 3:.1f}" font-size="10" font-weight="700" '
          f'style="fill:{lc}">{_num(prices[-1])}</text>')

    # --- overlay legend (top-right inside plot) ---
    if cropped:
        lx, ly = X1 - 148, YTOP + 8
        lh = 14 * len(cropped) + 8
        w += (f'<rect x="{lx - 8}" y="{ly - 10}" width="156" height="{lh}" '
              f'fill="#0b0f18" opacity="0.7" rx="4" '
              f'style="stroke:{text_color}" stroke-opacity=".1"/>')
        for i, (o, _) in enumerate(cropped):
            ty = ly + i * 14
            w += (f'<line x1="{lx}" y1="{ty - 3}" x2="{lx + 18}" y2="{ty - 3}" '
                  f'stroke="{o["color"]}" stroke-width="1.5" '
                  f'stroke-dasharray="4 3" opacity="0.6"/>')
            w += (f'<text x="{lx + 23}" y="{ty}" font-size="10" '
                  f'style="fill:{text_color}" fill-opacity=".6">{o["label"]}</text>')

    # --- hover furniture (hidden until pointermove) ---
    w += (f'<g class="chart-x" style="display:none" pointer-events="none">'
          f'<line class="chart-xl" x1="0" y1="{YTOP}" x2="0" y2="{YBOT}" '
          f'style="stroke:{text_color}" stroke-opacity=".4" stroke-dasharray="3 3"/>'
          f'<circle class="chart-xd" r="4" style="fill:{lc}" '
          f'stroke="#0b0f18" stroke-width="1.5"/></g>')

    # --- hit zone (LAST child = topmost) ---
    w += (f'<rect class="chart-hit" x="{X0}" y="{YTOP}" '
          f'width="{X1 - X0}" height="{YBOT - YTOP}" fill="transparent" '
          f'style="touch-action:pan-y"/>')

    # --- embedded data for shared JS ---
    data = {
        'W': width,
        'dates': [dt.isoformat() for dt in dates],
        'labels': [f'{p:,.2f} {unit}'.strip() for p in prices],
        'xs': [round(v, 1) for v in xs],
        'ys': [round(v, 1) for v in ys],
    }
    w += ('<script type="application/json" class="chart-data">'
          + json.dumps(data, separators=(',', ':')) + '</script>')
    w += '</svg>'
    parts.append(w)
    if embed_js:
        parts.append(HOVER_JS)
    return '\n'.join(parts)


def standalone_html(svg, title='chart'):
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{title}</title>'
            '<style>:root{--bg:#0b0f18;--accent:#34d399;--danger:#f87171;--text:#e2e8f0}'
            'body{margin:16px;background:var(--bg);color:var(--text);'
            "font-family:-apple-system,sans-serif}</style></head><body>"
            f'{svg}{HOVER_JS}</body></html>')
