"""Download public, individual Pinterest pins without treating video covers as photos."""
import json
import os
import re
from urllib.parse import urljoin, urlparse

import requests

PINTEREST_DOMAINS = tuple('pinterest.' + suffix for suffix in (
    'com fr de ch jp cl ca it co.uk nz ru com.au at pt co.kr es com.mx '
    'dk ph th com.uy co nl info kr ie vn com.vn ec mx in pe co.at hu '
    'co.in co.nz id com.ec com.py tw be uk com.bo com.pe'
).split()) + ('pin.it',)
HEADERS = {'User-Agent': 'Mozilla/5.0', 'X-Pinterest-PWS-Handler': 'www/[username].js'}


def is_pinterest_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower().rstrip('.')
    return parsed.scheme in ('http', 'https') and any(
        host == domain or host.endswith('.' + domain) for domain in PINTEREST_DOMAINS
    )


def canonical_pin(url):
    for _ in range(6):
        if not is_pinterest_url(url):
            raise ValueError('Please send a public Pinterest pin link.')
        match = re.fullmatch(r'/pin/(?:[\w-]+--)?(\d+)/?', urlparse(url).path)
        if match:
            return f'https://www.pinterest.com/pin/{match[1]}/', match[1]
        with requests.get(url, headers=HEADERS, timeout=30, allow_redirects=False, stream=True) as response:
            response.raise_for_status()
            location = response.headers.get('Location')
            if not location:
                break
            url = urljoin(url, location)
    raise ValueError('Send an individual Pinterest pin, not a board or profile.')


def pin_has_video(data):
    if isinstance(data, dict):
        embed = data.get('embed')
        if data.get('video_list') or (isinstance(embed, dict) and embed.get('src')):
            return True
        return any(pin_has_video(value) for key, value in data.items() if key in ('videos', 'story_pin_data', 'pages', 'blocks', 'video'))
    if isinstance(data, list):
        return any(pin_has_video(value) for value in data)
    return False


def photo_url(data):
    images = data.get('images') or {}
    candidates = [value for value in images.values() if isinstance(value, dict) and value.get('url')]
    original = images.get('orig') or images.get('originals')
    if isinstance(original, dict) and original.get('url'):
        return original['url']
    if candidates:
        return max(candidates, key=lambda item: (item.get('width') or 0) * (item.get('height') or 0))['url']
    raise ValueError('This pin does not expose a downloadable photo.')


def download_photo(url, directory, max_size):
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or not (host == 'pinimg.com' or host.endswith('.pinimg.com')):
        raise ValueError('Unexpected Pinterest image address.')
    with requests.get(url, headers=HEADERS, stream=True, timeout=30, allow_redirects=False) as response:
        response.raise_for_status()
        mime = response.headers.get('Content-Type', '').split(';')[0].lower()
        extension = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp', 'image/gif': '.gif'}.get(mime)
        if not extension:
            raise ValueError('Pinterest did not return an image.')
        path = os.path.join(directory, 'pinterest' + extension)
        try:
            size = 0
            with open(path, 'wb') as output:
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > max_size:
                        raise ValueError('Pinterest photo exceeds the upload limit.')
                    output.write(chunk)
            if not size:
                raise ValueError('Pinterest returned an empty image.')
            return path
        except Exception:
            if os.path.exists(path):
                os.remove(path)
            raise


def download_pin(url, kind, directory, options, video_downloader, max_size):
    canonical, pin_id = canonical_pin(url)
    try:
        with requests.get('https://www.pinterest.com/resource/PinResource/get/', params={
            'data': json.dumps({'options': {'id': pin_id, 'field_set_key': 'unauth_react_main_pin'}}),
        }, headers=HEADERS, timeout=30) as response:
            response.raise_for_status()
            data = response.json()['resource_response']['data']
        if not isinstance(data, dict) or str(data.get('id')) != pin_id:
            raise ValueError('Pinterest pin is unavailable.')
    except (requests.RequestException, ValueError, KeyError, TypeError):
        # yt-dlp can still handle video pins when metadata lookup is unavailable.
        data = None
    if data is not None and not pin_has_video(data):
        if kind == 'mp3':
            raise ValueError('This Pinterest pin is a photo and has no audio.')
        return [download_photo(photo_url(data), directory, max_size)]
    options = dict(options)
    options['outtmpl'] = os.path.join(directory, '%(id)s.%(ext)s')
    options.pop('cookiefile', None)
    video_downloader(canonical, options)
    files = [os.path.join(directory, name) for name in os.listdir(directory)
             if os.path.splitext(name)[1].lower() in {'.mp4', '.mov', '.mkv', '.webm', '.m4v', '.mp3'}]
    if not files:
        raise ValueError('No downloadable Pinterest media was found.')
    return files
