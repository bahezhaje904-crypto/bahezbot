import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault('TOKEN', 'test-only')
import bahez
import pinterest_media as pm


def response(data=None, headers=None, chunks=None):
    r = MagicMock()
    r.__enter__.return_value = r
    r.headers = headers or {}
    r.json.return_value = {'resource_response': {'data': data}}
    r.iter_content.return_value = chunks or [b'photo']
    return r


class PinterestTests(unittest.TestCase):
    def test_hosts_and_existing_platforms(self):
        for url in ('https://pin.it/abc', 'https://www.pinterest.com/pin/123/', 'https://pinterest.co.uk/pin/123/'):
            self.assertTrue(bahez.is_supported_url(url))
            self.assertEqual(bahez.platform_name(url), 'Pinterest')
        for url in ('https://pinterest.com.evil.test/pin/123', 'https://evilpinterest.com/pin/123', 'file://pinterest.com/pin/123'):
            self.assertFalse(pm.is_pinterest_url(url))
        for host in ('youtube.com', 'instagram.com', 'facebook.com', 'tiktok.com'):
            self.assertTrue(bahez.is_supported_url('https://' + host + '/x'))

    def test_short_link_and_slug(self):
        with patch.object(pm.requests, 'get', return_value=response(headers={'Location':'https://www.pinterest.com/pin/title--123/'})):
            self.assertEqual(pm.canonical_pin('https://pin.it/abc')[1], '123')
        with patch.object(pm.requests, 'get', return_value=response(headers={'Location':'http://localhost/'})):
            with self.assertRaises(ValueError):
                pm.canonical_pin('https://pin.it/abc')

    def test_shared_pin_sent_suffix(self):
        for url in (
            'https://www.pinterest.com/pin/939845016006398419/sent/?sender=123',
            'https://www.pinterest.com/pin/a-title--939845016006398419/sent/',
        ):
            self.assertEqual(pm.canonical_pin(url), ('https://www.pinterest.com/pin/939845016006398419/', '939845016006398419'))
        with patch.object(pm.requests, 'get', side_effect=[
            response(headers={'Location': 'https://api.pinterest.com/url_shortener/abc/redirect/'}),
            response(headers={'Location': 'https://www.pinterest.com/pin/939845016006398419/sent/?sender=123'}),
        ]):
            self.assertEqual(pm.canonical_pin('https://pin.it/abc')[1], '939845016006398419')

    def test_photo_original_and_limit(self):
        data = {'id':'123', 'images':{'orig':{'url':'https://i.pinimg.com/originals/a.jpg'}, 'small':{'url':'bad'}}}
        with tempfile.TemporaryDirectory() as directory, patch.object(pm.requests, 'get', side_effect=[response(data), response(headers={'Content-Type':'image/jpeg'})]):
            video = MagicMock()
            paths = pm.download_pin('https://pinterest.com/pin/123/', 'video', directory, {}, video, 100)
            self.assertTrue(os.path.isfile(paths[0]))
            video.assert_not_called()
        with tempfile.TemporaryDirectory() as directory, patch.object(pm.requests, 'get', return_value=response(headers={'Content-Type':'image/jpeg'}, chunks=[b'x'*101])):
            with self.assertRaises(ValueError):
                pm.download_photo('https://i.pinimg.com/a.jpg', directory, 100)
            self.assertEqual(os.listdir(directory), [])

    def test_video_and_story_never_become_cover_photo(self):
        for data in ({'id':'123','videos':{'video_list':{'V_720P':{'url':'video'}}}}, {'id':'123','story_pin_data':{'pages':[{'blocks':[{'video':{'video_list':{'x':{}}}}]}]}}):
            with tempfile.TemporaryDirectory() as directory, patch.object(pm.requests, 'get', return_value=response(data)):
                def download(url, options):
                    self.assertNotIn('cookiefile', options)
                    with open(os.path.join(directory, '123.mp4'), 'wb') as file:
                        file.write(b'video')
                paths = pm.download_pin('https://pinterest.com/pin/123/', 'video', directory, {'cookiefile':'youtube'}, download, 100)
                self.assertTrue(paths[0].endswith('.mp4'))

    def test_gif_download_preserves_animation(self):
        data = {'id': '123', 'embed': {'type': 'gif', 'src': 'https://i.pinimg.com/a.gif'}, 'images': {'orig': {'url': 'https://i.pinimg.com/a.gif'}}}
        self.assertFalse(pm.pin_has_video(data))
        with tempfile.TemporaryDirectory() as directory, patch.object(pm.requests, 'get', side_effect=[response(data), response(headers={'Content-Type': 'image/gif'}, chunks=[b'GIF89a'])]):
            video = MagicMock()
            files = pm.download_pin('https://pinterest.com/pin/123/', 'video', directory, {}, video, 100)
            self.assertTrue(files[0].endswith('.gif'))
            video.assert_not_called()

    def test_photo_has_no_mp3(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(pm.requests, 'get', return_value=response({'id':'123','images':{}})):
            with self.assertRaisesRegex(ValueError, 'no audio'):
                pm.download_pin('https://pinterest.com/pin/123/', 'mp3', directory, {}, MagicMock(), 100)

    def test_telegram_original_send_and_cleanup(self):
        message = MagicMock()
        message.reply_text = AsyncMock()
        message.reply_photo = AsyncMock()
        message.reply_document = AsyncMock()
        directories = []
        def download(url, kind, directory, *args):
            directories.append(directory)
            path = os.path.join(directory, 'pin.jpg')
            with open(path, 'wb') as file:
                file.write(b'image')
            return [path]
        with patch.object(bahez, 'download_pin', side_effect=download):
            self.assertTrue(asyncio.run(bahez.send_download(message, 'https://pin.it/abc')))
        message.reply_document.assert_awaited_once()
        message.reply_photo.assert_not_awaited()
        self.assertTrue(message.reply_document.call_args.kwargs["disable_content_type_detection"])
        self.assertFalse(os.path.exists(directories[0]))

    def test_original_bytes_for_photo_gif_and_video(self):
        for extension in ('.jpg', '.gif', '.mp4'):
            message = MagicMock()
            message.reply_text = AsyncMock()
            payload = b'original-media-bytes'
            async def verify_upload(**kwargs):
                self.assertEqual(kwargs['document'].read(), payload)
                self.assertTrue(kwargs['filename'].endswith(extension))
            message.reply_document = AsyncMock(side_effect=verify_upload)
            def download(url, kind, directory, *args):
                path = os.path.join(directory, 'pin' + extension)
                with open(path, 'wb') as file:
                    file.write(payload)
                return [path]
            with patch.object(bahez, 'download_pin', side_effect=download), patch.object(bahez, 'prepare_iphone_video') as convert:
                self.assertTrue(asyncio.run(bahez.send_download(message, 'https://pin.it/abc')))
                convert.assert_not_called()
            message.reply_document.assert_awaited_once()

if __name__ == '__main__':
    unittest.main()
