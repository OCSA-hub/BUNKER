"""Бункер — сервер комнат. Запуск: python server.py  (порт берётся из переменной PORT, по умолчанию 8080)"""
import asyncio, json, os, random, secrets, string, time
from pathlib import Path
from aiohttp import web, WSMsgType

ROOT = Path(__file__).parent
ROOMS = {}
MAX_PLAYERS = 16
MAX_ROOMS = 300


def clip(s, n):
    return str(s if s is not None else '').strip()[:n]


def clean_pack(p):
    cats = []
    for c in (p.get('categories') or [])[:12]:
        items = [clip(i, 120) for i in (c.get('items') or [])[:200] if clip(i, 120)]
        cats.append({'name': clip(c.get('name'), 40) or '???', 'items': items})
    if not cats:
        raise ValueError('В паке нет категорий')
    return {
        'name': clip(p.get('name'), 60) or 'Пак',
        'catastrophe': clip(p.get('catastrophe'), 1500),
        'bunker': clip(p.get('bunker'), 1500),
        'categories': cats,
        'info': clean_info(p.get('info')),
        'events': [clip(e, 300) for e in (p.get('events') or [])[:100] if clip(e, 300)],
    }


def clean_info(i):
    i = i if isinstance(i, dict) else {}
    return {'size': clip(i.get('size'), 60), 'time': clip(i.get('time'), 60), 'food': clip(i.get('food'), 60),
            'water': clip(i.get('water'), 60),
            'items': [clip(x, 100) for x in (i.get('items') or [])[:30] if clip(x, 100)]}


def new_code():
    while True:
        c = ''.join(random.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(4))
        if c not in ROOMS:
            return c


def alive(room):
    return [p for p in room['players'].values() if not p['out']]


def deal(room):
    used = {}
    for p in room['players'].values():
        p['out'] = False
        p['vote'] = None
        p['attrs'] = []
        for i, c in enumerate(room['pack']['categories']):
            if not c['items']:
                p['attrs'].append({'cat': c['name'], 'val': '—', 'open': False})
                continue
            u = used.setdefault(i, [])
            free = [x for x in c['items'] if x not in u] or c['items']
            v = random.choice(free)
            u.append(v)
            p['attrs'].append({'cat': c['name'], 'val': v, 'open': False})


def view(room, me):
    game = room['phase'] != 'lobby'
    players = []
    for p in room['players'].values():
        attrs = []
        if game:
            for a in p['attrs']:
                show = a['open'] or p is me or room['phase'] == 'over'
                attrs.append({'cat': a['cat'], 'open': a['open'], 'val': a['val'] if show else None})
        players.append({'id': p['id'], 'name': p['name'], 'out': p['out'], 'online': p['ws'] is not None or p.get('bot', False), 'bot': p.get('bot', False),
                        'voted': p['vote'] is not None, 'attrs': attrs})
    pk = room['pack']
    return {'t': 'state', 'code': room['code'], 'phase': room['phase'], 'you': me['id'], 'host': room['host'],
            'slots': room['slots'], 'round': room['round'], 'event': room['event'], 'lastvote': room['lastvote'],
            'myvote': me['vote'], 'hasEvents': bool(pk['events']),
            'pack': {'name': pk['name'], 'catastrophe': pk['catastrophe'], 'bunker': pk['bunker']},
            'info': room['info'],
            'players': players}


async def send(p, data):
    ws = p['ws']
    if ws is not None and not ws.closed:
        try:
            await ws.send_json(data)
        except Exception:
            pass


async def broadcast(room):
    room['touched'] = time.time()
    await asyncio.gather(*(send(p, view(room, p)) for p in room['players'].values()))


def check_over(room):
    if room['phase'] in ('game', 'vote') and len(alive(room)) <= room['slots']:
        room['phase'] = 'over'
        for p in room['players'].values():
            p['vote'] = None


def finish_vote(room):
    voters = [p for p in alive(room)]
    tally = {}
    for p in voters:
        if p['vote'] and p['vote'] in room['players'] and not room['players'][p['vote']]['out']:
            tally[p['vote']] = tally.get(p['vote'], 0) + 1
    out = None
    if tally:
        top = max(tally.values())
        leaders = [k for k, v in tally.items() if v == top]
        if len(leaders) == 1:
            out = leaders[0]
            room['players'][out]['out'] = True
    room['lastvote'] = {'tally': tally, 'out': out, 'n': room['lastvote_n'] + 1}
    room['lastvote_n'] += 1
    for p in room['players'].values():
        p['vote'] = None
    room['phase'] = 'game'
    check_over(room)


BOT_NAMES = ['Бот Гена', 'Бот Зина', 'Бот Витя', 'Бот Люся', 'Бот Борис', 'Бот Клава', 'Бот Семён', 'Бот Тамара',
             'Бот Игнатьич', 'Бот Валера', 'Бот Нинель', 'Бот Колян']


def bots_reveal(room):
    for p in room['players'].values():
        if p.get('bot') and not p['out']:
            closed = [a for a in p['attrs'] if not a['open']]
            if closed:
                random.choice(closed)['open'] = True


def bots_vote(room):
    al = alive(room)
    for p in al:
        if p.get('bot'):
            others = [q for q in al if q is not p]
            if others:
                p['vote'] = random.choice(others)['id']


def host_failover(room, pid):
    async def later():
        await asyncio.sleep(45)
        p = room['players'].get(pid)
        if room['host'] == pid and p and p['ws'] is None:
            for q in room['players'].values():
                if q['ws'] is not None:
                    room['host'] = q['id']
                    await broadcast(room)
                    return
    asyncio.ensure_future(later())


async def handle(room, me, d):
    t = d.get('t')
    is_host = room['host'] == me['id']
    ph = room['phase']

    if t == 'reveal' and ph in ('game', 'vote') and not me['out']:
        i = d.get('i')
        if isinstance(i, int) and 0 <= i < len(me['attrs']):
            me['attrs'][i]['open'] = True
    elif t == 'vote' and ph == 'vote' and not me['out']:
        tgt = room['players'].get(d.get('p'))
        if tgt and not tgt['out'] and tgt is not me:
            me['vote'] = tgt['id']
            if all(p['vote'] for p in alive(room) if p['ws'] is not None):
                finish_vote(room)
    elif is_host:
        if t == 'addbot' and ph == 'lobby':
            if len(room['players']) < MAX_PLAYERS:
                used = {p['name'] for p in room['players'].values()}
                free = [n for n in BOT_NAMES if n not in used] or [f'Бот {len(room["players"])}']
                bid = secrets.token_hex(4)
                room['players'][bid] = {'id': bid, 'token': secrets.token_hex(16), 'name': random.choice(free), 'ws': None,
                                        'out': False, 'attrs': [], 'vote': None, 'bot': True}
        elif t == 'start' and ph in ('lobby', 'over'):
            n = len(room['players'])
            if n < 2:
                return await send(me, {'t': 'error', 'm': 'Нужно минимум 2 игрока'})
            room['slots'] = max(1, min(int(d.get('slots') or room['slots']), n - 1))
            deal(room)
            room.update(phase='game', round=1, event='', lastvote=None)
            bots_reveal(room)
        elif t == 'setinfo':
            room['info'] = clean_info(d.get('info'))
        elif t == 'slots' and ph == 'lobby':
            room['slots'] = max(1, min(int(d.get('v') or 1), max(1, len(room['players']) - 1)))
        elif t == 'event' and ph in ('game', 'vote') and room['pack']['events']:
            room['event'] = random.choice(room['pack']['events'])
        elif t == 'round' and ph == 'game':
            room.update(round=room['round'] + 1, event='', lastvote=None)
            bots_reveal(room)
        elif t == 'startvote' and ph == 'game':
            room['phase'] = 'vote'
            room['lastvote'] = None
            for p in room['players'].values():
                p['vote'] = None
            bots_vote(room)
        elif t == 'endvote' and ph == 'vote':
            finish_vote(room)
        elif t == 'toggleout' and ph in ('game', 'vote', 'over'):
            p = room['players'].get(d.get('p'))
            if p:
                p['out'] = not p['out']
                if ph == 'over' and len(alive(room)) > room['slots']:
                    room['phase'] = 'game'
                check_over(room)
        elif t == 'revealall' and ph != 'lobby':
            for p in room['players'].values():
                for a in p['attrs']:
                    a['open'] = True
        elif t == 'lobby' and ph != 'lobby':
            room.update(phase='lobby', event='', lastvote=None, round=1)
            for p in room['players'].values():
                p.update(out=False, vote=None, attrs=[])
        elif t == 'remove' and ph == 'lobby':
            p = room['players'].get(d.get('p'))
            if p and p is not me:
                await send(p, {'t': 'kicked'})
                if p['ws'] is not None:
                    await p['ws'].close()
                room['players'].pop(p['id'], None)
    await broadcast(room)


async def ws_handler(req):
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=300_000)
    await ws.prepare(req)
    room = me = None
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                d = json.loads(msg.data)
                t = d.get('t')
                if me is None:
                    name = clip(d.get('name'), 20)
                    if t == 'create':
                        if len(ROOMS) >= MAX_ROOMS:
                            raise ValueError('Сервер переполнен, попробуйте позже')
                        if not name:
                            raise ValueError('Введите имя')
                        code = new_code()
                        room = ROOMS[code] = {'code': code, 'players': {}, 'phase': 'lobby', 'pack': clean_pack(d.get('pack') or {}), 'info': None,
                                              'slots': 1, 'round': 1, 'event': '', 'lastvote': None, 'lastvote_n': 0,
                                              'host': None, 'touched': time.time()}
                    elif t == 'join':
                        room = ROOMS.get(clip(d.get('code'), 8).upper())
                        if not room:
                            await ws.send_json({'t': 'nojoin', 'm': 'Комната не найдена'})
                            continue
                        old = next((p for p in room['players'].values() if d.get('token') and p['token'] == d['token']), None)
                        if old:
                            me = old
                            if me['ws'] is not None and me['ws'] is not ws and not me['ws'].closed:
                                await me['ws'].close()
                            me['ws'] = ws
                        else:
                            if not name:
                                raise ValueError('Введите имя')
                            if room['phase'] != 'lobby':
                                await ws.send_json({'t': 'nojoin', 'm': 'Игра уже идёт — зайти нельзя'})
                                room = None
                                continue
                            if len(room['players']) >= MAX_PLAYERS:
                                await ws.send_json({'t': 'nojoin', 'm': 'Комната заполнена'})
                                room = None
                                continue
                    else:
                        continue
                    if room['info'] is None:
                        room['info'] = dict(room['pack']['info'])
                    if me is None:
                        base, n = name, 2
                        while any(p['name'].lower() == name.lower() for p in room['players'].values()):
                            name = f'{base[:17]}{n}'
                            n += 1
                        me = {'id': secrets.token_hex(4), 'token': secrets.token_hex(16), 'name': name, 'ws': ws,
                              'out': False, 'attrs': [], 'vote': None}
                        room['players'][me['id']] = me
                        if room['host'] is None:
                            room['host'] = me['id']
                    await ws.send_json({'t': 'joined', 'code': room['code'], 'token': me['token']})
                    await broadcast(room)
                elif t == 'leave':
                    break
                else:
                    await handle(room, me, d)
            except ValueError as e:
                await ws.send_json({'t': 'error', 'm': str(e)})
            except Exception as e:  # не роняем соединение из-за кривого сообщения
                print('ERR', repr(e))
    finally:
        if me is not None and me['ws'] is ws:
            me['ws'] = None
            if room['host'] == me['id']:
                host_failover(room, me['id'])
            await broadcast(room)
    return ws


async def cleanup(app):
    async def loop():
        while True:
            await asyncio.sleep(300)
            now = time.time()
            for c in [c for c, r in ROOMS.items()
                      if not any(p['ws'] for p in r['players'].values()) and now - r['touched'] > 7200]:
                ROOMS.pop(c, None)
    task = asyncio.ensure_future(loop())
    yield
    task.cancel()


async def index(_):
    return web.FileResponse(ROOT / 'index.html', headers={'Cache-Control': 'no-cache'})


app = web.Application()
app.cleanup_ctx.append(cleanup)
app.add_routes([web.get('/', index), web.get('/ws', ws_handler), web.get('/health', lambda r: web.Response(text='ok'))])

if __name__ == '__main__':
    web.run_app(app, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
