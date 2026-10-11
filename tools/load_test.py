"""Local isolated HTTP/WebSocket load test; never connects to a user's server."""

import argparse
import asyncio
import json
import platform
import socket
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path

import httpx
import uvicorn
from websockets.asyncio.client import connect

from lanmsg.reliability import Limits
from lanmsg.server import create_app


async def exercise(base, users, count, interval):
    clients, sockets, latency, failures = [], [], [], []
    channels = []
    try:
        for i in range(users):
            client = httpx.AsyncClient(base_url=base, headers={'X-LANmsg':'1'}, timeout=15)
            clients.append(client)
            invite = ''
            if i:
                response = await clients[0].post('/api/admin/invites')
                response.raise_for_status()
                invite = response.json()['invite']
            response = await client.post('/api/register', json={'username':f'user{i}', 'password':'local load test password', 'invite':invite})
            response.raise_for_status()
        for i in range(min(3, users)):
            response = await clients[0].post('/api/admin/channels', json={'name':f'load-{i}'})
            response.raise_for_status()
            channels.append(response.json()['id'])
        for client in clients:
            cookie = 'session=' + client.cookies.get('session')
            ws = await connect(base.replace('http:', 'ws:') + '/ws', origin=base, additional_headers={'Cookie':cookie}, max_queue=512)
            sockets.append(ws)
        total = users * count
        async def receive(ws):
            ids = set()
            while len(ids) < total:
                event = json.loads(await asyncio.wait_for(ws.recv(), 30))
                if event['type'] == 'message':
                    ids.add(event['message']['id'])
            return len(ids)
        receivers = [asyncio.create_task(receive(ws)) for ws in sockets]
        async def send(i, client):
            for n in range(count):
                started = time.perf_counter()
                response = await client.post(f'/api/channels/{channels[i % len(channels)]}/messages', json={'content':f'load message {i}/{n}', 'nonce':f'{i}-{n}'})
                latency.append((time.perf_counter() - started) * 1000)
                if response.status_code != 200:
                    failures.append(response.status_code)
                await asyncio.sleep(interval)
        started = time.perf_counter()
        await asyncio.gather(*(send(i,c) for i,c in enumerate(clients)))
        if failures:
            for receiver in receivers:
                receiver.cancel()
            await asyncio.gather(*receivers, return_exceptions=True)
            received = []
        else:
            received = await asyncio.gather(*receivers)
        elapsed = time.perf_counter() - started
        # Read every channel's entire history through the public pagination API.
        history_count = 0
        for channel in channels:
            before = 9223372036854775807
            while True:
                response = await clients[0].get(f'/api/channels/{channel}/messages', params={'before':before})
                response.raise_for_status()
                rows = response.json()
                history_count += len(rows)
                if len(rows) < 50:
                    break
                before = rows[0]['id']
        latency.sort()
        return {'users':users, 'channels':len(channels), 'messages_attempted':total, 'messages_persisted':history_count,
                'delivery_counts_per_client':received, 'http_failures':failures, 'elapsed_seconds':round(elapsed,2),
                'messages_per_second':round(total/elapsed,2), 'send_latency_ms_p50':round(latency[len(latency)//2],2),
                'send_latency_ms_p95':round(latency[min(len(latency)-1,int(len(latency)*.95))],2)}
    finally:
        await asyncio.gather(*(ws.close() for ws in sockets), return_exceptions=True)
        await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--users', type=int, default=10)
    parser.add_argument('--messages', type=int, default=20)
    parser.add_argument('--interval', type=float, default=.6)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not 1 <= args.users <= 10 or args.messages < 1 or args.interval < .5:
        parser.error('Use 1–10 users, positive messages, and interval >= 0.5 seconds')
    with tempfile.TemporaryDirectory() as directory:
        # Provisioning many test accounts skips only the login/signup rate limit.
        limits = Limits(auth_burst=args.users+10, write_burst=args.users+10)
        app = create_app(Path(directory)/'load.db', limits=limits)
        listener = socket.socket()
        listener.bind(('127.0.0.1',0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', ws_max_size=2048, ws_max_queue=8))
        worker = threading.Thread(target=server.run, kwargs={'sockets':[listener]}, daemon=True)
        tracemalloc.start()
        worker.start()
        try:
            deadline = time.monotonic() + 15
            while not server.started:
                if not worker.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError('Load-test server did not start')
                time.sleep(.01)
            result = asyncio.run(exercise(f'http://127.0.0.1:{port}',args.users,args.messages,args.interval))
            result.update({'platform':platform.system(), 'python':platform.python_version(),
                           'peak_python_heap_mb':round(tracemalloc.get_traced_memory()[1]/1024**2,2),
                           'measurement_note':'Local server and test clients share one process; traced Python heap is not total RSS. Signup and admin-write bursts increased for provisioning; message and connection limits use defaults.'})
        finally:
            server.should_exit = True
            worker.join(timeout=10)
            listener.close()
            tracemalloc.stop()
        text = json.dumps(result,indent=2)
        print(text)
        if args.output:
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(text+'\n',encoding='utf-8')
        if result['http_failures'] or result['messages_persisted'] != result['messages_attempted']:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
