#!/usr/bin/env python3
"""tv-feed: scarica e normalizza i dati che oggi scarica la TV (guide TV + StreamingCommunity + canali).

Uso:  feed.py [out_dir]          un giro completo (cron/timer ogni 30 min)
      feed.py serve [port]       server HTTP statico con CORS su out_dir (Pi in LAN)
Env:  TVFEED_TV=192.168.188.80   IP TV per il controllo canali via telnet (vuoto = salta, es. su GitHub)

Uscita (out_dir):
  epg/<src>-<id>.json   [{title,start,end,desc,img,...}]  ms epoch; src/id = channels.json (= NusGuide)
  sc/host.json          {host,cdn}
  sc/home.json          {trending,latest,top10: [titoli]}
  sc/archive-<type>-<sort>.json  {last_page,data:[titoli]}   pagina 1
  channels/map.json     channels.json con numeri riallineati alla lista TV
  channels/tv.json, channels/sky-dtt.json, channels/sky-dth.json, channels/changes.log
  status.json           esito di ogni fonte
Una fonte che fallisce lascia i file del giro precedente (mai sovrascritti con dati vuoti).
Parser = porting di nulauncher/app/parsers.js e nusguide/app/main.js (loadRaiChannel).
"""
import datetime as dt, html, json, os, re, socket, sys, time, urllib.request
from zoneinfo import ZoneInfo

ROME = ZoneInfo('Europe/Rome')
HERE = os.path.dirname(os.path.abspath(__file__))
UA = 'Mozilla/5.0 (X11; Linux armv6l) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36'
RAI = 'https://guidatv.rai.it/palinsesto/app/'
MEDIASET = 'https://mediasetinfinity.mediaset.it/guidatv'
MEDIASET_IMG = 'https://img-prod-api2.mediasetplay.mediaset.it/api/images/ml/v5/ita/'
SKY = 'https://apid.sky.it/gtv/v1/'
SKY_IMG = 'https://guidatv.sky.it'
AURORA = 'https://public.aurora.enhanced.live/site/page/'
SC_SEEDS = ['https://streamingcommunityz.day', 'https://streamingcommunityz.shiksha',
            'https://streamingcommunityz.rip', 'https://streamingcommunityz.taxi']
SC_ARCHIVES = [('movie', 'release_date'), ('tv', 'release_date'), ('tv', 'last_air_date')]
TGR_RE = re.compile(r'^TGR?\b.*\bRegione\b', re.I)


def get(url, accept='', timeout=30):
    """-> (testo, url finale). Un retry: le fonti a volte chiudono la prima connessione."""
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': accept or '*/*'})
    for n in (0, 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode('utf-8', 'replace'), r.geturl()
        except Exception:
            if n:
                raise
            time.sleep(3)


def ms(d):
    return int(d.timestamp() * 1000)


def local_ms(y, mo, d, h, mi):
    return ms(dt.datetime(y, mo, d, h, mi, tzinfo=ROME))


def iso_ms(s):
    return ms(dt.datetime.fromisoformat(s.replace('Z', '+00:00')))


def write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(path + '.tmp', path)


# ---------------- Parser EPG ----------------

def parse_rai(j, tgr):
    out = []
    for e in j.get('events') or []:
        m = re.match(r'(\d{2})/(\d{2})/(\d{4})', e.get('date') or '')
        t = re.match(r'(\d{1,2}):(\d{2})', e.get('hour') or '')
        if not m or not t:
            continue
        st = local_ms(int(m[3]), int(m[2]), int(m[1]), int(t[1]), int(t[2]))
        dm = re.search(r'\d+', e.get('duration_in_minutes') or '')
        dd = re.match(r'(\d{1,2}):(\d{2}):(\d{2})', e.get('duration') or '')
        dur = int(dm[0]) * 60000 if dm else (int(dd[1]) * 3600 + int(dd[2]) * 60 + int(dd[3])) * 1000 if dd else 1800000
        name = str(e.get('name') or '')
        prog = e.get('program')
        p = {'title': name, 'start': st, 'end': st + dur, 'desc': str(e.get('description') or ''),
             'ep': str(e.get('episode_title') or ''), 'img': str(e.get('image') or ''),
             'prog': str(prog.get('name') or '') if isinstance(prog, dict) else ''}
        if tgr and TGR_RE.match(name):
            p['tgr'] = name   # l'app sostituisce "Regione" con la regione scelta
        out.append(p)
    return sorted(out, key=lambda p: p['start'])


def parse_sky(j):
    out = {}
    for e in j.get('events') or []:
        ch = (e.get('channel') or {}).get('id')
        if ch is None:
            continue
        c = e.get('content') or {}
        img = None
        for im in c.get('imagesMap') or []:
            if im.get('key') == 'scene' or (not img and im.get('key') == 'background'):
                img = SKY_IMG + im['img']['url']
        gen = ' · '.join(x for x in ((c.get('genre') or {}).get('name'), (c.get('subgenre') or {}).get('name')) if x)
        out.setdefault(str(ch), []).append({
            'start': iso_ms(e['starttime']), 'end': iso_ms(e['endtime']),
            'title': html.unescape(c.get('contentTitle') or e.get('eventTitle') or ''),
            'desc': html.unescape(e.get('eventSynopsis') or ''), 'gen': gen,
            'img': img or '', 'sized': bool(img)})   # sized: l'app aggiunge &w=&h=
    for v in out.values():
        v.sort(key=lambda p: p['start'])
    return out


def aurora_local(s):
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})', s)   # ora locale etichettata +00:00
    return local_ms(*(int(x) for x in m.groups()))


def parse_aurora(j):
    out, seen = [], set()
    for b in j.get('blocks') or []:
        if b.get('type') != 'epgBlock':
            continue
        for c in b['channels']:
            for d in c['days']:
                for p in d.get('programs') or []:
                    if p['start_dt'][11:16] == p['start_time']:
                        st = aurora_local(p['start_dt'])
                    else:
                        st = aurora_local(p['end_dt']) - p['duration'] * 60000   # testa troncata a 00:00
                    title = p.get('show_name') or p.get('title') or ''
                    if p.get('show_name') and p.get('title'):
                        title += ' - ' + p['title']
                    if (st, title) in seen:
                        continue
                    seen.add((st, title))
                    out.append({'start': st, 'end': st + p['duration'] * 60000, 'title': title,
                                'desc': p.get('description') or p.get('show_description') or '',
                                'img': p.get('show_image') or ''})
    return sorted(out, key=lambda p: p['start'])


def parse_mediaset_rsc(page):
    s = ''
    for m in re.finditer(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', page):
        try:
            s += json.loads(m[1])
        except ValueError:
            pass

    def jstr(x, k):
        r = re.search('"' + k + r'":("(?:[^"\\]|\\.)*")', x)
        try:
            return json.loads(r[1]) if r else ''
        except ValueError:
            return ''
    out = {}
    for seg in s.split('"__typename":"StationItem"')[1:]:
        sl = re.search(r'/diretta/(\w+)"', seg)
        if not sl or sl[1] in out:
            continue
        progs = []
        for x in seg.split('"__typename":"Listing"')[1:]:
            d = jstr(x, 'description')
            g = re.match(r'^\[([^\]]{2,30})\]\s*-?\s*', d)   # "[Azione] - trama"
            im = re.search(r'"id":"([^"]+)","r":"(\d+)","sourceType":"image_keyframe_poster"', x)
            try:
                st, en = iso_ms(jstr(x, 'startTime')), iso_ms(jstr(x, 'endTime'))
            except ValueError:
                continue
            p = {'title': jstr(x, 'title'), 'start': st, 'end': en,
                 'desc': d[len(g[0]):] if g else d, 'gen': g[1] if g else '', 'ep': jstr(x, 'shortDescription'),
                 'img': MEDIASET_IMG + urllib.request.quote(im[1], safe='') + '/image_keyframe_poster/1280/720?r=' + im[2] if im else ''}
            if p['title'] and p['end'] > p['start']:
                progs.append(p)
        out[sl[1]] = progs
    return out


def parse_mediaset(page, now=None):
    now = now or dt.datetime.now(ROME)
    out = {}
    for row in page.split('href="/diretta/')[1:]:
        m = re.match(r'[a-zA-Z0-9_]+', row)
        if not m or m[0] in out:
            continue
        cards, progs, last = row.split('--epg-card-width'), [], 0
        for k in range(1, len(cards) - 1, 2):
            sl = cards[k + 1]
            mt = re.search(r'>(\d{1,2}):(\d{2})<', sl)
            mtit = re.search(r'text-headline-8b-m[^>]*>([^<]{2,80})<', sl)
            if not mt or not mtit:
                continue
            st = local_ms(now.year, now.month, now.day, int(mt[1]), int(mt[2]))
            if st < last - 12 * 3600000:
                st += 86400000   # dopo mezzanotte
            last = st
            progs.append({'title': html.unescape(mtit[1]).strip(), 'start': st, 'end': st + 1800000, 'desc': ''})
        progs.sort(key=lambda p: p['start'])
        for a, b in zip(progs, progs[1:]):
            a['end'] = b['start']
        # dopo mezzanotte la pagina mostra ancora ieri sera: se così copre "adesso", indietro di 1 giorno
        t = ms(now)
        if progs and progs[0]['start'] > t and any(x['start'] - 86400000 <= t < x['end'] - 86400000 for x in progs):
            for x in progs:
                x['start'] -= 86400000
                x['end'] -= 86400000
        out[m[0]] = progs
    for k, r in parse_mediaset_rsc(page).items():   # trama + orari esatti dove ci sono
        if not r:
            continue
        a, b = r[0]['start'], r[-1]['end']
        out[k] = sorted([x for x in out.get(k, []) if x['end'] <= a or x['start'] >= b] + r, key=lambda p: p['start'])
    return out


# ---------------- Giri per fonte ----------------

def all_channels(cfg):
    return cfg['main'] + cfg['extra']


def job_epg(cfg, out, status):
    now = dt.datetime.now(ROME)
    chans = all_channels(cfg)
    cut = ms(now) - 6 * 3600000

    def save(c, progs):
        progs = [p for p in progs if p['end'] > cut]
        if progs:
            write(f"{out}/epg/{c['src']}-{c['id']}.json", progs)
        status['epg'][c['src'] + '-' + c['id']] = len(progs)

    def guard(c, fn):
        try:
            save(c, fn())
        except Exception as e:
            status['epg'][c['src'] + '-' + c['id']] = 'ERR ' + str(e)[:80]

    days = [now, now + dt.timedelta(days=1)]
    for c in chans:
        if c['src'] == 'rai':
            def rai(c=c):
                progs = []
                for d in days:
                    try:
                        progs += parse_rai(json.loads(get(f"{RAI}{c['id']}/{d:%d-%m-%Y}.json")[0]), c.get('tgr'))
                    except Exception:
                        if d is now:
                            raise   # domani può non esserci ancora
                return progs
            guard(c, rai)
        elif c['src'] == 'aurora':
            guard(c, lambda c=c: parse_aurora(json.loads(get(
                f"{AURORA}{c.get('page', 'guida-tv')}/?include=default&filter%5Benvironment%5D={c['id']}&v=2")[0])))

    # fonti multi-canale: un download per tutti; se fallisce, ogni canale riceve lo stesso errore
    t0 = dt.datetime(now.year, now.month, now.day, tzinfo=ROME).astimezone(dt.timezone.utc)

    def sky(env):
        ids = ''.join('&channels=' + c['id'] for c in chans if c['src'] == env)
        return parse_sky(json.loads(get(   # niente header Origin: con Origin Sky dà 403
            f"{SKY}events?from={t0:%Y-%m-%dT%H:%M:%SZ}&to={t0 + dt.timedelta(hours=49):%Y-%m-%dT%H:%M:%SZ}"
            f"&pageSize=999&pageNum=0&env={'DTH' if env == 'skydth' else 'DTT'}{ids}", 'application/json')[0]))
    multi = {'mediaset': lambda: parse_mediaset(get(MEDIASET, timeout=40)[0], now),
             'sky': lambda: sky('sky'), 'skydth': lambda: sky('skydth')}
    for src, fn in multi.items():
        try:
            m, err = fn(), ''
        except Exception as e:
            m, err = {}, 'ERR ' + str(e)[:80]
        for c in chans:
            if c['src'] == src:
                if err:
                    status['epg'][c['src'] + '-' + c['id']] = err
                else:
                    save(c, m.get(c['id'], []))


def sc_titles(arr):
    """Titolo SC ridotto ai campi usati da parseTitles di NusStream."""
    keep = ('id', 'slug', 'name', 'type', 'score', 'plot', 'seasons_count', 'last_air_date', 'last_air_date_it')
    return [dict({k: t[k] for k in keep if t.get(k) is not None},
                 images=[{'type': i.get('type'), 'filename': i.get('filename')} for i in t.get('images') or [] if i.get('filename')])
            for t in arr or [] if t.get('id') is not None and t.get('slug')]


def data_page(page):
    m = re.search(r'data-page="(.*?)"\s*>', page, re.S)
    return json.loads(html.unescape(m[1])) if m else None


def job_sc(out, status):
    page = host = None
    for seed in SC_SEEDS:   # i domini cambiano spesso: si segue il redirect
        try:
            txt, final = get(seed + '/')
            page = data_page(txt)
            if page:
                host = re.match(r'https://[^/]+', final)[0]
                break
        except Exception:
            continue
    if not page:
        status['sc'] = 'ERR home non raggiungibile'
        return
    p = page.get('props') or {}
    host = (p.get('app_url') or host).rstrip('/')
    cdn = (p.get('cdn_url') or '').rstrip('/') + '/images/' if p.get('cdn_url') else ''
    write(f'{out}/sc/host.json', {'host': host, 'cdn': cdn})
    home = {s['name']: sc_titles(s.get('titles')) for s in p.get('sliders') or [] if s and s.get('name') in ('trending', 'latest', 'top10')}
    write(f'{out}/sc/home.json', home)
    st = {'host': host, 'home': {k: len(v) for k, v in home.items()}}
    for typ, sort in SC_ARCHIVES:
        try:
            time.sleep(2)   # niente raffiche verso Cloudflare
            j = json.loads(get(f'{host}/it/archive?type={typ}&sort={sort}&page=1', 'application/json')[0])
            data = sc_titles(j.get('data'))
            if data:
                write(f'{out}/sc/archive-{typ}-{sort}.json', {'last_page': j.get('last_page') or 0, 'data': data})
            st[f'{typ}-{sort}'] = len(data)
        except Exception as e:
            st[f'{typ}-{sort}'] = 'ERR ' + str(e)[:80]
    status['sc'] = st


def tv_channels(ip):
    """Lista canali dalla TV via telnet root :23 (socket nudo: telnetlib non c'è più in Python 3.13)."""
    cmd = ("luna-send -n 1 -f luna://com.webos.service.apiadapter/tv/getChannelList '{}'"
           " | grep -E '\"(channelNumber|channelName|channelId|Radio)\"'; echo __E\"\"ND__\n")
    s = socket.create_connection((ip, 23), timeout=20)
    buf = b''
    try:
        time.sleep(1)
        s.recv(65536)   # banner + negoziazione IAC
        s.sendall(cmd.encode())
        while b'__END__' not in buf:
            d = s.recv(65536)
            if not d:
                break
            buf += d
        s.sendall(b'exit\n')
    finally:
        s.close()
    out, cur = [], {}
    for k, v in re.findall(r'"(channelNumber|channelName|channelId|Radio)": "?([^",\r\n]*)', buf.decode('utf-8', 'replace')):
        if k in cur:   # nuovo record
            out.append(cur)
            cur = {}
        cur[k] = v
    if cur:
        out.append(cur)
    return [{'num': c.get('channelNumber', ''), 'name': c.get('channelName', ''), 'id': c.get('channelId', ''),
             'radio': c.get('Radio') == 'true'} for c in out if c.get('channelNumber')]


def norm(name):
    return re.sub(r'(hd|uhd|4k)$', '', re.sub(r'[^a-z0-9]', '', name.lower())) or name.lower()


def remap(cfg, tv):
    """Numeri in channels.json riallineati alla TV: tiene il numero se il nome combacia, altrimenti
    prende l'unico canale TV con lo stesso nome. Ritorna (mappa, modifiche)."""
    tv = [c for c in tv if c['id'].startswith('1_')]   # solo terrestre: il satellite (7_) riusa gli stessi numeri
    by_num = {c['num']: norm(c['name']) for c in tv}
    by_name = {}
    for c in tv:
        if not c['radio']:   # "Rai Radio 2" audio (702) non deve rubare il numero al visual (202)
            by_name.setdefault(norm(c['name']), []).append(c['num'])
    changes, res = [], {}
    for part in ('main', 'extra'):
        res[part] = []
        for c in cfg[part]:
            c = dict(c)
            n = norm(c['name'])
            # "Rai 3 TGR Campania" al 3 vale come Rai 3: il numero resta
            if not by_num.get(c['num'], '').startswith(n) and len(by_name.get(n, [])) == 1 and by_name[n][0] != c['num']:
                changes.append(f"{c['name']}: {c['num']} -> {by_name[n][0]}")
                c['num'] = by_name[n][0]
            res[part].append(c)
    return res, changes


def job_channels(cfg, out, status):
    log, st = [], {}
    ts = dt.datetime.now(ROME).strftime('%Y-%m-%d %H:%M')

    def diff(name, new, key):
        path = f'{out}/channels/{name}.json'
        try:
            old = {key(c): c for c in json.load(open(path, encoding='utf-8'))}
        except (OSError, ValueError):
            old = None
        cur = {key(c): c for c in new}
        if old is not None:
            log.extend(f'{ts} {name} +{k} {cur[k]["name"]}' for k in cur.keys() - old.keys())
            log.extend(f'{ts} {name} -{k} {old[k]["name"]}' for k in old.keys() - cur.keys())
            log.extend(f'{ts} {name} ~{k} {old[k]} -> {cur[k]}' for k in cur.keys() & old.keys() if cur[k] != old[k])
        write(path, new)
        st[name] = len(new)

    for env in ('DTT', 'DTH'):
        try:
            j = json.loads(get(f'{SKY}channels?env={env}', 'application/json')[0])
            diff('sky-' + env.lower(), sorted(({'id': str(c['id']), 'name': c.get('name', ''), 'number': c.get('number')}
                                                for c in j.get('channels') or []), key=lambda c: c['id']), lambda c: c['id'])
        except Exception as e:
            st['sky-' + env.lower()] = 'ERR ' + str(e)[:80]
    for src, env in (('sky', 'dtt'), ('skydth', 'dth')):   # id guida Sky usati che Sky non elenca più
        try:
            have = {c['id'] for c in json.load(open(f'{out}/channels/sky-{env}.json', encoding='utf-8'))}
            st[f'sky-{env}-missing'] = [c['id'] for c in all_channels(cfg) if c['src'] == src and c['id'] not in have]
        except (OSError, ValueError):
            pass
    mp = cfg
    ip = os.environ.get('TVFEED_TV', '')
    if ip:
        try:
            tv = tv_channels(ip)
            if len(tv) < 20:
                raise ValueError(f'lista TV troppo corta ({len(tv)})')   # TV spenta/parziale: non toccare nulla
            diff('tv', tv, lambda c: c['num'] + '|' + c['name'])
            mp, ch = remap(cfg, tv)
            log.extend(f'{ts} map {x}' for x in ch)
        except Exception as e:
            st['tv'] = 'ERR ' + str(e)[:80]
            try:
                mp = json.load(open(f'{out}/channels/map.json', encoding='utf-8'))   # tiene l'ultima buona
            except (OSError, ValueError):
                pass
    write(f'{out}/channels/map.json', mp)
    if log:
        with open(f'{out}/channels/changes.log', 'a', encoding='utf-8') as f:
            f.write('\n'.join(log) + '\n')
    st['changes'] = len(log)
    status['channels'] = st


def run(out):
    cfg = json.load(open(os.path.join(HERE, 'channels.json'), encoding='utf-8'))
    status = {'start': int(time.time() * 1000), 'epg': {}}
    for job in (lambda: job_epg(cfg, out, status), lambda: job_sc(out, status), lambda: job_channels(cfg, out, status)):
        try:
            job()
        except Exception as e:   # una fonte rotta non ferma le altre
            status.setdefault('errors', []).append(str(e)[:200])
    status['end'] = int(time.time() * 1000)
    write(f'{out}/status.json', status)
    bad = [k for k, v in status['epg'].items() if not isinstance(v, int) or v == 0]
    print(f"epg {len(status['epg']) - len(bad)}/{len(status['epg'])} ok {bad} | sc {status.get('sc')} | channels {status.get('channels')}")


def serve(out, port):
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    class H(SimpleHTTPRequestHandler):
        def end_headers(self):
            self.send_header('Access-Control-Allow-Origin', '*')   # app webOS da file://
            self.send_header('Cache-Control', 'no-cache')
            super().end_headers()

        def log_message(self, *a):
            pass
    os.chdir(out)
    ThreadingHTTPServer(('0.0.0.0', port), H).serve_forever()


if __name__ == '__main__':
    a = sys.argv[1:]
    if a and a[0] == 'serve':
        serve(os.path.join(HERE, 'out'), int(a[1]) if len(a) > 1 else 8080)
    else:
        run(a[0] if a else os.path.join(HERE, 'out'))
