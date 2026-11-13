#!/usr/bin/python3
"""Run a temporary X server on a free VT, then restore the original VT."""
import array
import argparse
import fcntl
import os
import pwd
from pathlib import Path
import secrets
import signal
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--quota', action='store_true', help='Show the persistent quota panel')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run this through run.sh (sudo is required).')
    original_vt = subprocess.check_output(['fgconsole'], text=True).strip()
    with open('/dev/tty0', 'rb', buffering=0) as console:
        vt = array.array('i', [0])
        fcntl.ioctl(console, 0x5600, vt, True)  # VT_OPENQRY
    if vt[0] < 1:
        raise SystemExit('No unused virtual terminal is available.')
    number = next((n for n in range(1, 100)
                   if not Path(f'/tmp/.X11-unix/X{n}').exists()
                   and not Path(f'/tmp/.X{n}-lock').exists()), None)
    if number is None:
        raise SystemExit('No free X display number found.')
    display = f':{number}'
    server = client = None
    user = None
    if args.quota:
        user = pwd.getpwuid(int(os.environ.get('SUDO_UID', os.getuid())))
        if user.pw_uid == 0:
            raise SystemExit('Launch run-quota.sh as your normal user so the panel can read your accounts.')

    def interrupted(signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    with tempfile.TemporaryDirectory(prefix='hdmi-test-') as directory:
        auth = str(Path(directory) / 'Xauthority')
        Path(auth).touch(mode=0o600)
        subprocess.run(['xauth', '-f', auth, 'add', display, '.',
                        secrets.token_hex(16)], check=True)
        if user:
            # User can read the X cookie, but cannot alter server-owned files.
            os.chown(directory, 0, user.pw_gid)
            os.chmod(directory, 0o750)
            os.chown(auth, 0, user.pw_gid)
            os.chmod(auth, 0o640)
        env = dict(os.environ, DISPLAY=display, XAUTHORITY=auth,
                   GDK_BACKEND='x11')
        log_path = Path('/var/tmp/hdmi-display-test.log')
        # Reject symlinks when opening a predictable privileged log path.
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC |
                     os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'w') as log:
                print(f'Starting HDMI test on {display}, VT {vt[0]}. '
                      f'Exit returns to VT {original_vt}.', flush=True)
                server = subprocess.Popen(
                    ['/usr/lib/xorg/Xorg', display, f'vt{vt[0]}',
                     '-auth', auth, '-nolisten', 'tcp', '-noreset',
                     '-logfile', str(Path(directory) / 'Xorg.log')],
                    stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True)
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    if server.poll() is not None:
                        raise RuntimeError('Temporary X server exited during startup.')
                    probe = subprocess.run(['xrandr', '--query'], env=env,
                                           capture_output=True, timeout=3)
                    if probe.returncode == 0:
                        print(probe.stdout.decode(), flush=True)
                        break
                    time.sleep(0.25)
                else:
                    raise RuntimeError('Temporary X server did not become ready.')
                subprocess.run(['chvt', str(vt[0])], check=True)
                subprocess.run(['xset', 's', 'off'], env=env, capture_output=True)
                subprocess.run(['xset', '-dpms'], env=env, capture_output=True)
                script = Path(__file__).with_name('kiosk.py') if args.quota else Path(__file__).with_name('test_display.py')
                options = {}
                if user:
                    env.update(HOME=user.pw_dir, USER=user.pw_name, LOGNAME=user.pw_name,
                               XDG_RUNTIME_DIR=f'/run/user/{user.pw_uid}',
                               PATH=f'{user.pw_dir}/.local/bin:/usr/local/bin:/usr/bin:/bin')
                    options = dict(user=user.pw_uid, group=user.pw_gid,
                                   extra_groups=os.getgrouplist(user.pw_name, user.pw_gid))
                client = subprocess.Popen(
                    ['/usr/bin/python3', str(script)],
                    env=env, start_new_session=True, **options)
                result = client.wait(timeout=None if args.quota else 315)
                if result:
                    raise RuntimeError(f'Test program exited with code {result}.')
        except KeyboardInterrupt:
            print('\nClosing test.', flush=True)
        except Exception as error:
            print(f'Test failed: {error}\nX server log: {log_path}', flush=True)
            print(log_path.read_text(errors='replace')[-6000:], flush=True)
            raise SystemExit(1)
        finally:
            # Complete cleanup even if Ctrl+C is pressed repeatedly.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            for process in (client, server):
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            subprocess.run(['chvt', original_vt], check=False)
            print('Original display session restored.', flush=True)


if __name__ == '__main__':
    main()
