"""Read-only quota adapters. Credentials never leave the provider adapter."""
from datetime import datetime
import base64
import fcntl
import json
import math
import os
import pty
import re
from pathlib import Path
import select
import selectors
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
import urllib.error
import urllib.request


class ProviderError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def timestamp(value):
    if number(value) is not None:
        return value / 1000 if value > 1e11 else value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass
    return None


def window(label, used, reset=None, minutes=None):
    used = number(used)
    if used is None or used < 0:
        return None
    return {'label': label, 'usedPercent': used, 'remaining': round(max(0, 100-used), 1),
            'resetsAt': timestamp(reset), 'minutes': minutes}


def duration_label(minutes):
    if minutes == 10080:
        return '每周额度'
    if number(minutes) is None or minutes <= 0:
        return '当前额度'
    if minutes >= 1440:
        return f'{minutes / 1440:g} 天额度'
    if minutes >= 60:
        return f'{minutes / 60:g} 小时窗口'
    return f'{minutes:g} 分钟窗口'


def plan_name(value):
    return str(value).replace('_', ' ').title() if isinstance(value, str) and value else ''


def configured_plan(home, key):
    """~/.config/quota-deck/plans.json, e.g. {"grok": "Premium", "codex": {"tier": "10x"}}.

    Returns (plan, tier); callers use them only for fields the service omits."""
    try:
        value = json.loads((Path(home)/'.config/quota-deck/plans.json').read_text()).get(key)
    except (OSError, ValueError, AttributeError):
        return '', None
    if isinstance(value, str):
        return value, None
    if isinstance(value, dict):
        plan, tier = value.get('plan'), value.get('tier')
        return (plan if isinstance(plan, str) else ''), (tier if isinstance(tier, str) and tier else None)
    return '', None


def apply_configured_plan(result, home, key):
    plan, tier = configured_plan(home, key)
    result['plan'] = result.get('plan') or plan
    result['tier'] = result.get('tier') or tier
    return result


def reset_credits(data):
    # Free "full reset" grants Codex offers; count only unexpired, available ones.
    block = data.get('rateLimitResetCredits')
    if not isinstance(block, dict) or not isinstance(block.get('credits'), list):
        return None
    now = time.time()
    expiries = [timestamp(item.get('expiresAt')) for item in block['credits']
                if isinstance(item, dict) and item.get('status') == 'available'
                and (timestamp(item.get('expiresAt')) or float('inf')) > now]
    known = [value for value in expiries if value]
    return {'available':len(expiries), 'nextExpiry':min(known) if known else None}


def json_field(path, *keys):
    # Best-effort read of a display-only field (account email); never raises.
    try:
        value = json.loads(Path(path).read_text())
        for key in keys:
            value = value[key]
        return value if isinstance(value, str) else ''
    except (OSError, ValueError, KeyError, TypeError):
        return ''


def jwt_email(token):
    # Display only: the CLI already validated this token; we just read its email claim.
    try:
        payload = token.split('.')[1]
        return json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload) % 4))).get('email') or ''
    except (AttributeError, IndexError, ValueError):
        return ''


def read_credentials(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise ProviderError('missing', '尚未在本机登录')
    except (ValueError, OSError):
        raise ProviderError('error', '无法读取本机登录信息')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward authorization headers to an unexpected redirect target.
        return None


def get_json(url, headers, attempts=2):
    try:
        request = urllib.request.Request(url, headers={**headers, 'Accept':'application/json'})
        with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise ProviderError('expired', '登录失效或缺少额度读取权限')
        if error.code == 429:
            raise ProviderError('limited', '查询过于频繁，稍后自动重试')
        raise ProviderError('error', f'额度服务暂不可用（HTTP {error.code}）')
    except (OSError, ValueError):
        if attempts > 1:
            # Proxies/VPNs drop connections briefly; one quick retry avoids a 1-minute gap.
            time.sleep(3)
            return get_json(url, headers, attempts-1)
        raise ProviderError('error', '网络连接失败，稍后自动重试')


def codex_result(data):
    buckets = data.get('rateLimitsByLimitId') or {}
    bucket = buckets.get('codex') or data.get('rateLimits') or {}
    windows = []
    for key in ('primary', 'secondary'):
        item = bucket.get(key)
        if not isinstance(item, dict):
            continue
        minutes = item.get('windowDurationMins')
        parsed = window(duration_label(minutes), item.get('usedPercent'), item.get('resetsAt'), minutes)
        if parsed:
            windows.append(parsed)
    return {'windows':windows, 'plan':plan_name(bucket.get('planType')), 'tier':None,
            'resetCredits':reset_credits(data), 'note':'额度来自 Codex 本机登录账户', 'source':'Codex App Server',
            'status':'ready' if windows else 'unavailable'}


def fetch_codex(home):
    executable = shutil.which('codex') or '/usr/local/bin/codex'
    if not Path(executable).exists():
        raise ProviderError('missing', '本机未安装 Codex CLI')
    # CLI owns authentication and token refresh; only a read RPC is issued.
    process = subprocess.Popen([executable, 'app-server', '--stdio'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env={**os.environ, 'HOME':str(home)},
                               bufsize=0)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    def send(payload):
        process.stdin.write((json.dumps(payload)+'\n').encode())
        process.stdin.flush()
    try:
        send({'id':1, 'method':'initialize', 'params':{
            'clientInfo':{'name':'quota_deck','version':'1.0.0'}, 'capabilities':{}}})
        end = time.monotonic()+25
        buffer = b''
        while time.monotonic()<end:
            if not selector.select(0.5):
                if process.poll() is not None:
                    break
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            buffer += chunk
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n',1)
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if data.get('id') in (1,2) and 'error' in data:
                    raise ProviderError('error', 'Codex 额度读取失败，请检查 CLI 登录状态')
                if data.get('id') == 1:
                    send({'method':'initialized'})
                    send({'id':2,'method':'account/rateLimits/read'})
                if data.get('id') == 2:
                    result = apply_configured_plan(codex_result(data.get('result') or {}), home, 'codex')
                    result['account'] = jwt_email(json_field(Path(home)/'.codex/auth.json', 'tokens', 'id_token'))
                    return result
        raise ProviderError('error', 'Codex 额度请求超时')
    finally:
        selector.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdin.close()
        process.stdout.close()


def claude_result(data, auth):
    windows = []
    for key,label,minutes in [('five_hour','5 小时窗口',300),('seven_day','每周额度',10080)]:
        item = data.get(key)
        if isinstance(item,dict):
            parsed = window(label,item.get('utilization'),item.get('resets_at'),minutes)
            if parsed:
                windows.append(parsed)
    # rateLimitTier looks like "default_claude_max_5x"; the multiplier is the plan tier.
    tier = re.search(r'(\d+)x$', str(auth.get('rateLimitTier') or ''))
    return {'windows':windows,'plan':plan_name(auth.get('subscriptionType')),
            'tier':tier.group(1)+'x' if tier else None,
            'source':'Claude OAuth Usage','note':'额度来自 Claude Code 本机登录账户',
            'status':'ready' if windows else 'unavailable'}


CLAUDE_REFRESH_COOLDOWN = 600
claude_refresh_state = {'at':0, 'error':None}
claude_refresh_lock = threading.Lock()


def claude_auth(home):
    auth = read_credentials(Path(home)/'.claude/.credentials.json').get('claudeAiOauth') or {}
    if not auth.get('accessToken'):
        raise ProviderError('missing','请先登录 Claude Code')
    return auth


def claude_cli_touch(home, timeout=10):
    """Let Claude Code renew its own OAuth token (as CodexBar does): start the CLI in a PTY,
    trust the empty probe folder, open the local /status panel and stop once the credential
    file changes. The CLI rotates refresh tokens, so Quota Deck never calls the token
    endpoint itself. No model request."""
    executable = shutil.which('claude') or str(Path(home)/'.local/bin/claude')
    if not Path(executable).exists():
        return 'missing'
    path = Path(home)/'.claude/.credentials.json'
    def stamp():
        try:
            stat = path.stat()
            return stat.st_mtime_ns, stat.st_size, path.read_bytes()
        except OSError:
            return None
    before = stamp()
    # A dedicated, empty working directory keeps the probe away from real projects.
    workdir = Path(home)/'.cache/quota-deck/claude-probe'
    workdir.mkdir(parents=True, exist_ok=True)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
    try:
        process = subprocess.Popen([executable], stdin=slave, stdout=slave, stderr=slave, cwd=workdir,
                                   env={**{k:v for k,v in os.environ.items() if k != 'CLAUDECODE'},
                                        'HOME':str(home), 'TERM':'xterm-256color'},
                                   start_new_session=True)
    except OSError:
        os.close(master)
        os.close(slave)
        return 'missing'
    os.close(slave)
    outcome = 'timeout'
    try:
        start = time.monotonic()
        screen, trusted, typed = b'', False, False
        while time.monotonic()-start < timeout:
            # Drain output so the TUI never blocks on a full PTY buffer.
            ready, _, _ = select.select([master], [], [], 0.3)
            if ready:
                try:
                    screen = (screen+os.read(master, 65536))[-16384:]
                except OSError:
                    pass
            if stamp() != before:
                outcome = 'changed'
                break
            if process.poll() is not None:
                outcome = 'exited'
                break
            text = re.sub(rb'\x1b\[[0-9;?<>=]*[A-Za-z~]', b'', screen)
            if not trusted and b'trust this folder' in text:
                # First run in the probe folder: the default answer is "No, exit".
                time.sleep(0.5)
                os.write(master, b'\x1b[B')
                time.sleep(0.5)
                os.write(master, b'\r')
                trusted, screen = True, b''
            elif not typed and b'Context' in text and time.monotonic()-start > 2:
                os.write(master, b'/status\r')
                typed = True
        if outcome == 'changed':
            time.sleep(0.3)  # let an atomic rename settle before rereading
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                process.wait()
        os.close(master)
    return outcome


def claude_renewed_auth(home, auth):
    """Return usable credentials, delegating an (almost) expired token to the Claude CLI."""
    expiry = timestamp(auth.get('expiresAt'))
    if expiry is not None and expiry-time.time() > 300:
        return auth
    with claude_refresh_lock:
        auth = claude_auth(home)
        expiry = timestamp(auth.get('expiresAt'))
        if expiry is not None and expiry-time.time() > 300:
            return auth
        if time.time()-claude_refresh_state['at'] >= CLAUDE_REFRESH_COOLDOWN:
            claude_refresh_state['at'] = time.time()
            outcome = claude_cli_touch(home)
            claude_refresh_state['error'] = None if outcome == 'changed' else outcome
            auth = claude_auth(home)
            expiry = timestamp(auth.get('expiresAt'))
        error = claude_refresh_state['error']
    if expiry is not None and expiry > time.time():
        return auth
    if error == 'timeout':
        # The CLI did not finish in time; that is not proof the login is gone.
        raise ProviderError('error','Claude 登录续期未完成，稍后自动重试')
    raise ProviderError('expired','登录已过期，自动续期未成功，请在 Claude Code 中重新登录')


def fetch_claude(home):
    auth = claude_renewed_auth(home, claude_auth(home))
    data = get_json('https://api.anthropic.com/api/oauth/usage',{
        'Authorization':'Bearer '+auth['accessToken'], 'anthropic-beta':'oauth-2025-04-20',
        'User-Agent':'QuotaDeck/1.0'})
    result = claude_result(data, auth)
    result['account'] = json_field(Path(home)/'.claude.json', 'oauthAccount', 'emailAddress')
    return result


def grok_result(data):
    data = data if isinstance(data, dict) else {}
    config = data.get('config') if isinstance(data.get('config'), dict) else {}
    def amount(value):
        return number(value.get('val')) if isinstance(value, dict) else None
    used = number(config.get('creditUsagePercent'))
    if used is None:
        spent = amount(config.get('onDemandUsed'))
        cap = amount(config.get('onDemandCap'))
        if spent is not None and cap is not None and cap > 0:
            used = spent / cap * 100
    period = config.get('currentPeriod') if isinstance(config.get('currentPeriod'), dict) else {}
    start, end = timestamp(period.get('start')), timestamp(period.get('end'))
    if end is None:
        start, end = timestamp(config.get('billingPeriodStart')), timestamp(config.get('billingPeriodEnd'))
    # Older CLI versions return a monetary included-limit schema instead of config.
    if not config:
        usage = data.get('usage') if isinstance(data.get('usage'), dict) else {}
        spent, cap = amount(usage.get('totalUsed')), amount(data.get('monthlyLimit'))
        if spent is not None and cap is not None and cap > 0:
            used = spent / cap * 100
        cycle = data.get('billingCycle') if isinstance(data.get('billingCycle'), dict) else {}
        start, end = timestamp(cycle.get('billingPeriodStart')), timestamp(cycle.get('billingPeriodEnd'))
    minutes = round((end-start)/60) if start and end and start <= time.time() < end else None
    label = '订阅额度'
    if minutes and abs(minutes-10080)<60:
        label='每周额度'
    elif minutes and 27*1440 <= minutes <= 32*1440:
        label='每月额度'
    item = window(label,used,end,minutes)
    plan = data.get('subscription_tier')
    return {'windows':[item] if item else [],'plan':plan if isinstance(plan, str) else '', 'tier':None, 'source':'Grok CLI Billing',
            'note':'订阅额度查询成功' if item else '服务未返回可验证的额度百分比',
            'status':'ready' if item else 'unavailable'}


def grok_credentials(home):
    auth = read_credentials(Path(os.environ.get('GROK_HOME', str(home/'.grok')))/'auth.json')
    if not isinstance(auth, dict):
        raise ProviderError('error', '无法读取本机登录信息')
    candidates = [(key,value) for key,value in auth.items()
                  if isinstance(value,dict) and isinstance(value.get('key'),str) and value['key']]
    candidates.sort(key=lambda pair: not pair[0].startswith('https://auth.x.ai::'))
    if not candidates:
        raise ProviderError('missing','请先运行 grok login')
    return candidates


def grok_rpc(home, initialize_timeout=4, billing_timeout=10):
    """Use the CLI-owned login/refresh flow. No session, prompt or model call is made.

    ACP contract reference: steipete/CodexBar's GrokRPCClient and docs/grok.md.
    Read-only capabilities, bounded output/deadlines, and guaranteed child cleanup.
    """
    executable = shutil.which('grok')
    if not executable:
        raise ProviderError('missing', '本机未安装 Grok CLI')
    with tempfile.TemporaryDirectory(prefix='quota-grok-') as cwd:
        try:
            process = subprocess.Popen([executable, 'agent', 'stdio'], cwd=cwd,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={**os.environ, 'HOME':str(home)}, bufsize=0)
        except OSError:
            raise ProviderError('error', 'Grok CLI 启动失败，稍后自动重试')
        selector = selectors.DefaultSelector()
        buffer = b''
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            calls = [(1, 'initialize', {'protocolVersion':'1', 'clientCapabilities':{
                'fs':{'readTextFile':False, 'writeTextFile':False}, 'terminal':False}}, initialize_timeout),
                (2, '_x.ai/billing', {}, billing_timeout)]
            for request_id, method, params, timeout in calls:
                process.stdin.write((json.dumps({'jsonrpc':'2.0','id':request_id,
                    'method':method,'params':params})+'\n').encode())
                process.stdin.flush()
                deadline = time.monotonic()+timeout
                result = None
                while result is None:
                    if time.monotonic() >= deadline:
                        raise ProviderError('error', 'Grok 额度请求超时')
                    if b'\n' not in buffer:
                        if not selector.select(min(.25, max(0, deadline-time.monotonic()))):
                            continue
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            raise ProviderError('error', 'Grok CLI 额度连接已断开')
                        buffer += chunk
                        if len(buffer) > 2*1024*1024:
                            raise ProviderError('error', 'Grok 额度响应无效')
                        continue
                    line, buffer = buffer.split(b'\n', 1)
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(message, dict) or message.get('id') != request_id:
                        continue
                    if 'error' in message:
                        error = message['error'] if isinstance(message['error'], dict) else {}
                        if error.get('code') == -32601:
                            raise ProviderError('unavailable', '当前 Grok CLI 不支持额度接口')
                        hint = str(error.get('message', '')).lower()
                        if 'authentication required' in hint or 'grok login' in hint:
                            raise ProviderError('expired', '登录已过期，请运行 grok login')
                        # Never expose upstream messages, which may contain credential details.
                        raise ProviderError('error', 'Grok CLI 额度读取失败，稍后自动重试')
                    result = message.get('result')
                    if not isinstance(result, dict):
                        raise ProviderError('error', 'Grok 额度响应无效')
            return result
        except OSError:
            raise ProviderError('error', 'Grok CLI 额度连接已断开')
        finally:
            selector.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdin.close()
            process.stdout.close()


def fetch_grok(home):
    home = Path(home)
    try:
        before = grok_credentials(home)[0][1]
    except ProviderError:
        before = {}
    rpc_error = None
    try:
        result = grok_result(grok_rpc(home))
        try:
            after = grok_credentials(home)[0][1]
        except ProviderError:
            after = {}
        # Token rotation is expected, but a switched account needs a new billing request.
        identity = lambda value: (value.get('user_id'), value.get('team_id'), value.get('email'))
        if result['status'] == 'ready' and identity(before) == identity(after):
            result['source'] = 'Grok CLI ACP'
            result['account'] = after.get('email') if isinstance(after.get('email'), str) else ''
            return apply_configured_plan(result, home, 'grok')
    except ProviderError as error:
        rpc_error = error
    # Reload after the CLI attempt: it may have renewed the expired token on disk.
    candidates = grok_credentials(home)
    credential = next((value for _,value in candidates
                       if (timestamp(value.get('expires_at')) or 0)>time.time()),None)
    if credential is None:
        if rpc_error and rpc_error.status == 'error':
            # A transport/timeout failure is not proof that the refresh token is invalid.
            raise rpc_error
        raise ProviderError('expired','登录已过期，请运行 grok login')
    headers = {'Authorization':'Bearer '+credential['key'], 'x-xai-token-auth':'xai-grok-cli',
               'User-Agent':'QuotaDeck/1.0'}
    data = get_json('https://cli-chat-proxy.grok.com/v1/billing?format=credits',headers)
    result = apply_configured_plan(grok_result(data), home, 'grok')
    result['account'] = credential.get('email') if isinstance(credential.get('email'), str) else ''
    return result


FETCHERS = {'codex':fetch_codex,'grok':fetch_grok,'claude':fetch_claude}
