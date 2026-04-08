#!/usr/bin/env python3
"""
openclaw-gateway 服务器监控脚本（Kuasar VMM 容器内运行）

与 monitor_worker.py 的区别：
- 读取 /tmp/openclaw/openclaw-YYYY-MM-DD.log 而非 docker logs
- 每日自动切换日志文件
- 重启用 kill -HUP supervisord 而非 docker compose restart
- 解析 openclaw JSON 格式日志
"""
import subprocess, time, urllib.request, json, os, re

APP_ID     = os.environ.get('FEISHU_MONITOR_APP_ID',     'cli_a95f02c3733adbd1')
APP_SECRET = os.environ.get('FEISHU_MONITOR_APP_SECRET', 'ZmZssGtQ0DffLx5JSWpF7bmmyEFwJP8W')
RECV_ID    = os.environ.get('FEISHU_MONITOR_RECEIVE_ID', 'oc_3c4863248b13e278568a95e20b82f2a5')
RECV_TYPE  = os.environ.get('FEISHU_MONITOR_RECEIVE_ID_TYPE', 'chat_id')
LOG_SELF        = '/tmp/gspd_monitor_server.log'
LOG_DIR         = '/tmp/openclaw'
SUPERVISORD_PID = '/home/sandbox/.openclaw/logs/supervisord.pid'
RESTART_COOLDOWN = 300  # 秒，防止重启循环

token_cache = {'t': '', 'exp': 0}

def log(msg):
    with open(LOG_SELF, 'a') as f:
        f.write(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {msg}\n')

def get_token():
    now = time.time()
    if token_cache['exp'] > now + 60:
        return token_cache['t']
    data = json.dumps({'app_id': APP_ID, 'app_secret': APP_SECRET}).encode()
    req = urllib.request.Request(
        'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
        data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        resp = json.loads(r.read())
    token_cache['t'] = resp['tenant_access_token']
    token_cache['exp'] = now + resp.get('expire', 7200)
    return token_cache['t']

def send(text):
    try:
        token = get_token()
        body = json.dumps({'receive_id': RECV_ID, 'msg_type': 'text',
                           'content': json.dumps({'text': text})}).encode()
        req = urllib.request.Request(
            f'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={RECV_TYPE}',
            data=body, headers={'Authorization': f'Bearer {token}',
                                'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as r:
            pass
        log(f'sent {len(text)}b')
    except Exception as e:
        log(f'send error: {e}')

SKIP = re.compile(
    r'plugins\.allow is empty|'
    r'\[DEBUG\].*FACTORY CALLED|'
    r'\[DEBUG\].*assemble called|'
    r'\[DEBUG\].*calling register|'
    r'context-gspd: registered|'
    r'gspd: registered|'
    r'feishu_(?:doc|chat|wiki|drive|bitable): Registered|'
    r'\[info\].*event-dispatch is ready|'
    r'\[info\].*client ready|'
    r'\[info\].*\[ws\]|'
    r'\[bonjour\]'
)

def extract_msg(line):
    # openclaw 日志是 JSON 格式：{"0":"subsystem","1":"实际消息",...}
    try:
        obj = json.loads(line)
        return obj.get('1') or line
    except Exception:
        return line

RESTART_TRIGGER = re.compile(r'unable to connect to the server after trying')

def do_restart():
    log('sending SIGHUP to supervisord...')
    send('[监控] ⚠️ 检测到飞书连接断开，正在重启 openclaw-gateway...')
    try:
        with open(SUPERVISORD_PID) as f:
            pid = f.read().strip()
        subprocess.run(['kill', '-HUP', pid], timeout=10, check=True)
        log('SIGHUP sent')
        send('[监控] ✅ 已发送 SIGHUP，openclaw-gateway 重启中（约 40 秒）')
    except Exception as e:
        log(f'restart failed: {e}')
        send(f'[监控] ❌ 重启失败: {e}')

def today_log():
    return os.path.join(LOG_DIR, f'openclaw-{time.strftime("%Y-%m-%d")}.log')

def open_tail(path):
    for _ in range(30):
        if os.path.exists(path):
            break
        log(f'waiting for {path}...')
        time.sleep(1)
    log(f'tailing {path}')
    return subprocess.Popen(
        ['tail', '-F', '-n', '0', path],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

log('monitor_server.py started')
send('[监控] 服务器日志监控已启动 ✓')

buf = []
last_flush = time.time()
last_restart = 0
BATCH_SEC = 3
MAX_CHARS = 3800

def flush(lines):
    if not lines:
        return
    text = '\n'.join(lines)
    if len(text) > MAX_CHARS:
        text = text[-MAX_CHARS:]
    send(text)

current_date = time.strftime('%Y-%m-%d')
proc = open_tail(today_log())

while True:
    raw = proc.stdout.readline()
    if not raw:
        time.sleep(0.1)
        new_date = time.strftime('%Y-%m-%d')
        if new_date != current_date:
            proc.kill()
            current_date = new_date
            proc = open_tail(today_log())
        continue

    line = raw.decode('utf-8', errors='replace').rstrip()
    if not line:
        continue

    line = extract_msg(line)
    if not line or SKIP.search(line):
        continue

    if RESTART_TRIGGER.search(line):
        now = time.time()
        if now - last_restart > RESTART_COOLDOWN:
            flush(buf)
            buf = []
            last_flush = now
            last_restart = now
            do_restart()
            continue

    buf.append(line)
    now = time.time()
    if now - last_flush >= BATCH_SEC or len('\n'.join(buf)) > MAX_CHARS:
        flush(buf)
        buf = []
        last_flush = now
