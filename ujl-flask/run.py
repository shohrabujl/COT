"""Start the app locally: python run.py. Vercel imports the WSGI `app` from api/index.py."""
import os
import socket
import threading
import webbrowser

from ujl import create_app


app = create_app()


def free_port(start=5000, tries=30):
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('127.0.0.1', port)) != 0:
                return port
    raise SystemExit('No free port found between 5000 and 5029. Close other programs and try again.')


if __name__ == '__main__':
    port = int(os.environ.get('UJL_PORT') or free_port())
    url = f'http://127.0.0.1:{port}'
    print(f'\n  UJL Plan Control Center is running at {url}')
    print('  Keep this window open. Close it (or press Ctrl+C) to stop the app.\n')
    if not os.environ.get('UJL_NO_BROWSER'):
        threading.Timer(1.2, webbrowser.open, args=(url,)).start()
    app.run(host='127.0.0.1', port=port, debug=False, threaded=True, use_reloader=False)
