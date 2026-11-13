#!/usr/bin/python3
"""Display the local quota app on the connected HDMI output, as the user."""
import re
import json
import signal
import subprocess
import time
import threading
from urllib.error import URLError
from urllib.request import Request, build_opener, ProxyHandler

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('WebKit2', '4.1')
from gi.repository import Gdk, GLib, Gtk, WebKit2

from server import start_server


BACKEND_URL = 'http://127.0.0.1:18768'


def backend_request(path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    request = Request(BACKEND_URL + path, data=body,
                      headers={'Content-Type': 'application/json', 'X-Quota-Deck': '1'})
    with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
        return json.load(response)


def connect_backend():
    # The user service keeps LAN control available independently of the HDMI session.
    try:
        subprocess.run(['systemctl', '--user', 'start', 'quota-deck-web.service'],
                       capture_output=True, timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass
    owned = None
    for attempt in range(10):
        try:
            health = backend_request('/api/health')
            if health != {'application': 'quota-deck', 'version': 2}:
                raise RuntimeError('Port 18768 is not a compatible Quota Deck service.')
            return owned
        except (URLError, TimeoutError):
            if attempt == 0:
                try:
                    owned = start_server(18768, host='0.0.0.0')
                except OSError as error:
                    if error.errno != 98:
                        raise
            time.sleep(.5)
    raise RuntimeError('Could not connect to the Quota Deck backend on port 18768.')


def save_theme(theme):
    if not isinstance(theme, dict) or theme.get('theme') not in ('light', 'dark'):
        return
    def send():
        try:
            backend_request('/api/control', {'action': 'theme', 'mode': theme['theme']})
        except (OSError, ValueError):
            print('Could not sync the manual theme with the controller.', flush=True)
    threading.Thread(target=send, daemon=True).start()


def hdmi_geometry():
    def query():
        return subprocess.check_output(['xrandr', '--query'], text=True)
    # As a resident service the panel may be asleep or unplugged at start; wait for it
    # (xrandr --query re-probes connectors) instead of failing and restart-looping.
    waiting = False
    while True:
        report = query()
        connected = re.findall(r'^(\S*HDMI\S*) connected\b', report, re.M)
        if len(connected) == 1:
            break
        if len(connected) > 1:
            raise RuntimeError('Expected one connected HDMI display.')
        if not waiting:
            print('Waiting for the HDMI display to connect...', flush=True)
            waiting = True
        time.sleep(5)
    output = connected[0]
    # This small panel advertises 1024x600 as its preferred/native resolution.
    block = report.split(output+' connected',1)[1].split('\n',1)[1]
    block = re.split(r'\n\S',block)[0]
    mode = ['--mode','1024x600'] if re.search(r'^\s+1024x600\s',block,re.M) else ['--auto']
    # A display connected after Xorg started has no active mode until one is set.
    subprocess.run(['xrandr','--output',output,*mode],check=True)
    report=query()
    line=next(line for line in report.splitlines() if line.startswith(output+' connected'))
    geometry=re.search(r'(\d+)x(\d+)\+(-?\d+)\+(-?\d+)',line)
    if not geometry:
        raise RuntimeError('HDMI output has no active mode.')
    return output, tuple(map(int,geometry.groups()))


def main():
    output,(width,height,left,top)=hdmi_geometry()
    owned_backend=connect_backend()
    url=BACKEND_URL+'/'
    window=Gtk.Window(title='Quota Deck')
    window.set_decorated(False)
    window.set_default_size(width,height)
    window.move(left,top)
    window.connect('realize',lambda w:w.get_window().set_override_redirect(True))
    window.connect('destroy',Gtk.main_quit)
    context=WebKit2.WebContext.new_ephemeral()
    manager=WebKit2.UserContentManager()
    theme=backend_request('/api/control')['theme']['override']
    manager.add_script(WebKit2.UserScript.new(
        f'window.quotaNativeTheme = {json.dumps(theme)};',
        WebKit2.UserContentInjectedFrames.TOP_FRAME,
        WebKit2.UserScriptInjectionTime.START, None, None))
    manager.register_script_message_handler('quota')
    def message(_manager,value):
        action=value.get_js_value().to_string()
        if action=='exit':
            window.destroy()
        elif action.startswith('theme:'):
            try:
                save_theme(json.loads(action.split(':', 1)[1]))
            except ValueError:
                pass
    manager.connect('script-message-received::quota',message)
    view=WebKit2.WebView(web_context=context,user_content_manager=manager)
    view.get_settings().set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.NEVER)
    view.get_settings().set_enable_developer_extras(False)
    view.connect('context-menu',lambda *_:True)
    def navigation(_view,decision,kind):
        if kind==WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            target=decision.get_navigation_action().get_request().get_uri()
            if not target.startswith(url):
                decision.ignore()
                return True
        return False
    view.connect('decide-policy',navigation)
    color=Gdk.RGBA()
    hour=time.localtime().tm_hour
    scheduled='dark' if hour >= 18 or hour < 7 else 'light'
    initial=theme['theme'] if theme else scheduled
    color.parse('#101113' if initial=='dark' else '#F5F6F8')
    view.set_background_color(color)
    window.add(view)
    view.load_uri(url)
    window.show_all()
    window.move(left,top)
    for sig in (signal.SIGTERM,signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT,sig,lambda:(window.destroy(),False)[1])
    print(f'Quota Deck on {output}: {width}x{height}. Escape or power button exits.',flush=True)
    try:
        Gtk.main()
    finally:
        if owned_backend:
            server,store,stop=owned_backend
            stop.set()
            server.shutdown()
            server.server_close()


if __name__=='__main__':
    main()
