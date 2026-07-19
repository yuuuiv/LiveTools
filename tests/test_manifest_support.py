import os
import sys
import unittest
import base64
import json
import tempfile
import urllib.parse
from types import SimpleNamespace
from unittest import mock


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import HLS_Stream_Interactive as live_tools
import page_harvester


PROTECTED_MPD = '''
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"
     xmlns:cenc="urn:mpeg:cenc:2013">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011"
                         value="cenc"
                         cenc:default_KID="E7D6E1CA-DD9F-495A-8EAD-E2116710660B" />
      <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed" />
      <ContentProtection schemeIdUri="urn:uuid:9a04f079-9840-4286-ab92-e65be0885f95" />
      <Representation id="low" bandwidth="700000" width="640" height="360"
                      codecs="avc1.4D401E" />
      <Representation id="high" bandwidth="6000000" width="1920" height="1080"
                      codecs="avc1.640029" />
    </AdaptationSet>
  </Period>
</MPD>
'''


CLEAR_MPD = '''
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
  <Period>
    <AdaptationSet contentType="video" codecs="avc1.640029">
      <Representation id="v1" bandwidth="2000000" width="1280" height="720" />
    </AdaptationSet>
    <AdaptationSet mimeType="audio/mp4">
      <Representation id="a1" bandwidth="128000" codecs="mp4a.40.2" />
    </AdaptationSet>
  </Period>
</MPD>
'''


class ManifestSupportTests(unittest.TestCase):
    def test_detects_cenc_widevine_and_playready(self):
        drm = live_tools.detect_mpd_drm(PROTECTED_MPD)

        self.assertTrue(drm['protected'])
        self.assertEqual(drm['schemes'], ['cenc'])
        self.assertEqual(
            drm['systems'],
            ['Google Widevine', 'Microsoft PlayReady'],
        )
        self.assertEqual(
            drm['key_ids'],
            ['E7D6E1CA-DD9F-495A-8EAD-E2116710660B'],
        )

    def test_parses_dash_video_representations(self):
        streams = live_tools.parse_mpd_string(PROTECTED_MPD, 'https://example.com/index.mpd')

        self.assertEqual([stream.resolution for stream in streams], ['640x360', '1920x1080'])
        self.assertEqual([stream.video_index for stream in streams], [0, 1])
        self.assertTrue(all(stream.manifest_type == 'dash' for stream in streams))
        self.assertTrue(all(stream.drm_info['protected'] for stream in streams))

    def test_clear_dash_is_not_marked_as_drm(self):
        streams = live_tools.parse_mpd_string(CLEAR_MPD, 'https://example.com/index.mpd')

        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0].resolution, '1280x720')
        self.assertEqual(streams[0].codecs, 'avc1.640029')
        self.assertFalse(streams[0].drm_info['protected'])

    def test_parses_private_input_file_fields(self):
        description = live_tools.parse_input_description('''
节目名称: demo
视频链接: https://example.com/index.mpd?token=secret
Cookie: session=secret
Authorization: Bearer token
操作: push
清晰度: best
流名称: 3696505
''')

        self.assertEqual(description['source'], 'https://example.com/index.mpd?token=secret')
        self.assertEqual(description['headers']['Cookie'], 'session=secret')
        self.assertEqual(description['headers']['Authorization'], 'Bearer token')
        self.assertEqual(description['operation'], 'push')
        self.assertEqual(description['quality'], 'best')
        self.assertEqual(description['stream_name'], '3696505')

    def test_hls_parser_remains_supported(self):
        playlist = '''#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
video/720p.m3u8
'''
        streams = live_tools.parse_m3u8_string(playlist, 'https://example.com/master.m3u8')

        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0].resolution, '1280x720')
        self.assertEqual(streams[0].url, 'https://example.com/video/720p.m3u8')

    def test_clear_dash_push_maps_selected_representation_and_headers(self):
        stream = live_tools.parse_mpd_string(CLEAR_MPD, 'https://example.com/index.mpd')[0]

        with mock.patch.object(live_tools, 'check_ffmpeg', return_value=True), \
             mock.patch.object(live_tools.time, 'time', return_value=1700000000), \
             mock.patch.object(live_tools.subprocess, 'run') as run:
            live_tools.perform_livestream(
                stream,
                headers={'Authorization': 'Bearer secret'},
                stream_name='3696505',
            )

        command = run.call_args.args[0]
        self.assertIn('-headers', command)
        self.assertIn('Authorization: Bearer secret\r\n', command)
        self.assertIn('-map', command)
        self.assertIn('0:v:0', command)
        self.assertIn('0:a:0?', command)
        signed_url = urllib.parse.urlsplit(command[-1])
        self.assertEqual(
            signed_url.path,
            '/live/3696505',
        )
        self.assertRegex(
            signed_url.query,
            r'^auth_key=1700001800-0-0-[0-9a-f]{32}$',
        )

    def test_default_push_url_uses_a_auth_key(self):
        with mock.patch.object(live_tools.time, 'time', return_value=1700000000):
            url = live_tools.build_default_push_url('20260718')

        self.assertEqual(
            url,
            'rtmp://push.neofantasy.online/live/20260718?'
            'auth_key=1700001800-0-0-'
            'c2f8550099a5035aedcf74f5a4216d74',
        )

    def test_push_auth_key_can_be_overridden_without_cli_arguments(self):
        with mock.patch.dict(
            live_tools.os.environ,
            {'LIVETOOLS_PUSH_AUTH_KEY': 'temporary-key'},
            clear=False,
        ), mock.patch.object(live_tools.time, 'time', return_value=1700000000):
            url = live_tools.build_default_push_url('20260718')

        self.assertIn(
            'auth_key=1700001800-0-0-'
            'a406d8e850f4ebd1673739591bf89a3a',
            url,
        )

    def test_normalizes_single_bare_key_against_manifest_kid(self):
        lines = live_tools.normalize_drm_key_material(
            '00112233445566778899aabbccddeeff',
            ['E7D6E1CA-DD9F-495A-8EAD-E2116710660B'],
        )

        self.assertEqual(
            lines,
            ['e7d6e1cadd9f495a8eade2116710660b:'
             '00112233445566778899aabbccddeeff'],
        )

    def test_rejects_key_for_different_kid(self):
        with self.assertRaisesRegex(ValueError, '未覆盖'):
            live_tools.normalize_drm_key_material(
                '11111111111111111111111111111111:'
                '00112233445566778899aabbccddeeff',
                ['E7D6E1CA-DD9F-495A-8EAD-E2116710660B'],
            )

    def test_expands_key_lines_with_guid_byte_order_variant(self):
        lines = live_tools.expand_key_lines_for_downloader([
            'f7839c436c2445218e75eac004328786:'
            '00112233445566778899aabbccddeeff',
        ])

        self.assertEqual(lines, [
            'f7839c436c2445218e75eac004328786:'
            '00112233445566778899aabbccddeeff',
            '439c83f7246c21458e75eac004328786:'
            '00112233445566778899aabbccddeeff',
        ])

    def test_drm_relay_command_uses_private_key_file_not_key_argv(self):
        stream = live_tools.parse_mpd_string(
            PROTECTED_MPD, 'https://example.com/index.mpd')[1]

        command = live_tools.build_drm_relay_command(
            stream,
            {'Cookie': 'session=secret'},
            r'C:\temp\keys.private.txt',
            r'C:\tools\N_m3u8DL-RE.exe',
            r'C:\tools\ffmpeg.exe',
            r'C:\temp\relay',
            live_take_count=2,
        )

        self.assertIn('--key-text-file', command)
        self.assertNotIn('--key', command)
        self.assertIn('--mp4-real-time-decryption', command)
        self.assertIn('--live-pipe-mux', command)
        self.assertIn('res=1920x1080:for=best', command)
        self.assertIn('Cookie: session=secret', command)
        self.assertEqual(command[command.index('--log-level') + 1], 'OFF')

    def test_drm_relay_command_prefers_mp4decrypt_when_available(self):
        stream = live_tools.parse_mpd_string(
            PROTECTED_MPD, 'https://example.com/index.mpd')[1]

        command = live_tools.build_drm_relay_command(
            stream,
            {},
            r'C:\temp\keys.private.txt',
            r'C:\tools\N_m3u8DL-RE.exe',
            r'C:\tools\ffmpeg.exe',
            r'C:\temp\relay',
            decryption_engine='MP4DECRYPT',
            decryption_binary_path=r'C:\tools\mp4decrypt.exe',
        )

        self.assertEqual(
            command[command.index('--decryption-engine') + 1],
            'MP4DECRYPT',
        )
        self.assertEqual(
            command[command.index('--decryption-binary-path') + 1],
            r'C:\tools\mp4decrypt.exe',
        )

    def test_drm_relay_command_uses_cached_mpd_with_remote_base_url(self):
        stream = live_tools.parse_mpd_string(
            PROTECTED_MPD,
            'https://media.example/path/live/index.mpd?token=secret',
        )[1]
        stream.relay_manifest_source = r'C:\temp\cached.mpd'

        command = live_tools.build_drm_relay_command(
            stream,
            {'Cookie': 'session=secret'},
            r'C:\temp\keys.private.txt',
            r'C:\tools\N_m3u8DL-RE.exe',
            r'C:\tools\ffmpeg.exe',
            r'C:\temp\relay',
        )

        self.assertEqual(command[1], r'C:\temp\cached.mpd')
        self.assertIn('--base-url', command)
        self.assertEqual(
            command[command.index('--base-url') + 1],
            'https://media.example/path/live/',
        )
        self.assertIn('Cookie: session=secret', command)

    def test_private_input_parses_drm_relay_settings(self):
        description = live_tools.parse_input_description('''
视频链接: https://example.com/index.mpd
DRM密钥文件: C:\\private\\keys.private.txt
DRM密钥环境变量: SHOW_DRM_KEY
N_m3u8DL-RE路径: C:\\tools\\N_m3u8DL-RE.exe
FFmpeg路径: D:\\ffmpeg\\bin\\ffmpeg.exe
''')

        self.assertEqual(description['drm_key_env'], 'SHOW_DRM_KEY')
        self.assertTrue(description['drm_key_file'].endswith('keys.private.txt'))
        self.assertTrue(description['downloader_path'].endswith('N_m3u8DL-RE.exe'))
        self.assertTrue(description['ffmpeg_path'].endswith('ffmpeg.exe'))

    def test_drm_relay_removes_key_env_and_cleans_private_file(self):
        stream = live_tools.parse_mpd_string(
            PROTECTED_MPD, 'https://example.com/index.mpd')[0]
        key_pair = (
            'e7d6e1cadd9f495a8eade2116710660b:'
            '00112233445566778899aabbccddeeff'
        )

        with mock.patch.dict(os.environ, {'SHOW_DRM_KEY': key_pair}), \
             mock.patch.object(
                 live_tools, 'resolve_executable',
                 side_effect=[
                     r'C:\tools\N_m3u8DL-RE.exe',
                     r'C:\tools\ffmpeg.exe',
                     FileNotFoundError('mp4decrypt'),
                 ]), \
             mock.patch.object(
                 live_tools, 'run_relay_process',
                 return_value=0) as run:
            result = live_tools.perform_drm_livestream(
                stream,
                'rtmp://push.example.com/live/demo',
                drm_key_env='SHOW_DRM_KEY',
            )

        command = run.call_args.args[0]
        child_environment = run.call_args.args[1]
        private_key_path = command[command.index('--key-text-file') + 1]
        self.assertEqual(result, 0)
        self.assertNotIn(key_pair, command)
        self.assertNotIn('SHOW_DRM_KEY', child_environment)
        self.assertEqual(
            child_environment['RE_LIVE_PIPE_OPTIONS'],
            '-f flv "rtmp://push.example.com/live/demo"',
        )
        self.assertFalse(os.path.exists(private_key_path))

    def test_drmtoday_license_headers_match_reference_tool(self):
        auth = page_harvester.DrmAuthToken(
            auth_token='token123',
            session_id='session456',
            user_id='user789',
            merchant_id='merchant000',
        )

        headers = page_harvester.build_license_headers(
            page_harvester.DRMTODAY_WIDEVINE_LICENSE_URL, auth)

        self.assertEqual(
            set(headers),
            {'Accept', 'dt-custom-data', 'x-dt-auth-token'},
        )
        self.assertEqual(headers['Accept'], '*/*')
        self.assertEqual(headers['x-dt-auth-token'], 'token123')
        decoded = json.loads(base64.b64decode(headers['dt-custom-data']))
        self.assertEqual(decoded, {
            'userId': 'user789',
            'sessionId': 'session456',
            'merchant': 'merchant000',
        })

    def test_license_post_does_not_mix_page_session_headers(self):
        calls = []

        class FakeRequestsSession:
            def post(self, url, data=None, headers=None, timeout=None,
                     allow_redirects=None):
                calls.append({
                    'url': url,
                    'data': data,
                    'headers': headers,
                    'timeout': timeout,
                    'allow_redirects': allow_redirects,
                })
                return SimpleNamespace(status_code=200, content=b'\x08\x01')

        session = page_harvester.HttpSession(
            cookie='session=secret',
            headers={'User-Agent': 'Page UA', 'Referer': 'https://page/'},
        )
        session._requests_session = FakeRequestsSession()

        response = page_harvester._post_license_request(
            session,
            page_harvester.DRMTODAY_WIDEVINE_LICENSE_URL,
            b'\x00\xffchallenge',
            {'Accept': '*/*', 'dt-custom-data': 'custom',
             'x-dt-auth-token': 'auth'},
        )

        self.assertEqual(response, {'status': 200, 'body': b'\x08\x01'})
        self.assertEqual(calls[0]['headers'], {
            'Accept': '*/*',
            'dt-custom-data': 'custom',
            'x-dt-auth-token': 'auth',
        })
        self.assertEqual(calls[0]['data'], b'\x00\xffchallenge')

    def test_curl_post_keeps_binary_license_response(self):
        binary_body = b'\x08\x01\x12\x03\xff\x00\n'
        completed = SimpleNamespace(stdout=binary_body + b'\n200', stderr=b'')

        with mock.patch.object(
                page_harvester.subprocess, 'run',
                return_value=completed) as run:
            result = page_harvester._curl_post(
                'https://license.example/',
                b'\x00challenge',
                {'Accept': '*/*'},
                curl_path='curl',
            )

        self.assertEqual(result['status'], 200)
        self.assertEqual(result['body'], binary_body)
        self.assertIn('--data-binary', run.call_args.args[0])

    def test_pssh_box_has_valid_version_zero_layout(self):
        box = page_harvester.build_pssh_box(
            'E7D6E1CA-DD9F-495A-8EAD-E2116710660B',
            system_id='edef8ba9-79d6-4ace-a3c8-27dcd51d21ed',
        )

        self.assertEqual(int.from_bytes(box[0:4], 'big'), len(box))
        self.assertEqual(box[4:8], b'pssh')
        self.assertEqual(box[8:12], b'\x00\x00\x00\x00')
        self.assertEqual(
            box[12:28].hex(),
            'edef8ba979d64acea3c827dcd51d21ed',
        )
        data_size = int.from_bytes(box[28:32], 'big')
        self.assertEqual(data_size, len(box) - 32)
        self.assertEqual(
            box[32:],
            b'\x12\x10' + bytes.fromhex('e7d6e1cadd9f495a8eade2116710660b'),
        )

    def test_extract_content_keys_supports_key_objects_and_tuples(self):
        content_key = SimpleNamespace(
            kid=bytes.fromhex('e7d6e1cadd9f495a8eade2116710660b'),
            key=bytes.fromhex('00112233445566778899aabbccddeeff'),
            type='CONTENT',
        )
        signing_key = SimpleNamespace(
            kid=bytes.fromhex('11111111111111111111111111111111'),
            key=bytes.fromhex('22222222222222222222222222222222'),
            type='SIGNING',
        )

        class FakeCdm:
            def get_keys(self, session_id):
                return [
                    content_key,
                    signing_key,
                    (
                        bytes.fromhex('33333333333333333333333333333333'),
                        bytes.fromhex('44444444444444444444444444444444'),
                    ),
                ]

        keys = page_harvester._extract_content_keys(FakeCdm(), object())

        self.assertEqual(
            [(key.kid, key.key, key.key_type) for key in keys],
            [
                (
                    'e7d6e1cadd9f495a8eade2116710660b',
                    '00112233445566778899aabbccddeeff',
                    'CONTENT',
                ),
                (
                    '33333333333333333333333333333333',
                    '44444444444444444444444444444444',
                    'CONTENT',
                ),
            ],
        )

    def test_resolves_drmtoday_jwt_to_widevine_endpoint(self):
        payload = {
            'license': 'https://lic.drmtoday.com/rightsmanager.asmx',
        }
        payload_b64 = base64.urlsafe_b64encode(
            json.dumps(payload).encode()).decode().rstrip('=')
        token = 'eyJhbGciOiJub25lIn0.' + payload_b64 + '.signature'

        self.assertEqual(
            page_harvester.resolve_license_url(token),
            page_harvester.DRMTODAY_WIDEVINE_LICENSE_URL,
        )

    def test_page_harvest_without_push_only_prints_keys(self):
        result = page_harvester.HarvestResult(
            mpd_url='https://media.example/index.mpd',
            keys=[
                page_harvester.ContentKey(
                    kid='e7d6e1cadd9f495a8eade2116710660b',
                    key='00112233445566778899aabbccddeeff',
                    key_type='CONTENT',
                )
            ],
        )
        args = SimpleNamespace(
            page_url='https://page.example/live',
            auth_cookie='session=secret',
            wvd_path='device.wvd',
            channel_id='',
            use_browser=False,
            browser_headless=False,
            header=[],
            operation=None,
            downloader_path='',
            ffmpeg_path='',
        )

        with mock.patch.object(page_harvester, 'harvest', return_value=result), \
             mock.patch.object(page_harvester.subprocess, 'run') as run:
            exit_code = page_harvester.run_harvest_and_push(args)

        self.assertEqual(exit_code, 0)
        run.assert_not_called()

    def test_stream_name_variable_implies_push(self):
        result = page_harvester.HarvestResult(
            mpd_url='https://media.example/index.mpd',
            mpd_content=PROTECTED_MPD,
            source_headers={'Cookie': 'CloudFront-Policy=abc',
                            'User-Agent': 'Edge UA'},
            keys=[
                page_harvester.ContentKey(
                    kid='e7d6e1cadd9f495a8eade2116710660b',
                    key='00112233445566778899aabbccddeeff',
                    key_type='CONTENT',
                )
            ],
        )
        args = SimpleNamespace(
            page_url='https://page.example/live',
            auth_cookie='',
            wvd_path='device.wvd',
            channel_id='',
            use_browser=False,
            browser_headless=False,
            header=[],
            operation=None,
            stream_name='custom_live_001',
            push_url='',
            downloader_path='',
            ffmpeg_path='',
            quality='best',
            relay_restarts=None,
            relay_restart_delay=None,
            live_take_count=None,
        )

        with mock.patch.object(page_harvester, 'harvest', return_value=result), \
             mock.patch.object(
                 page_harvester.subprocess, 'run',
                 return_value=SimpleNamespace(returncode=0)) as run:
            exit_code = page_harvester.run_harvest_and_push(args)

        self.assertEqual(exit_code, 0)
        command = run.call_args.args[0]
        self.assertIn('--operation', command)
        self.assertIn('push', command)
        self.assertIn('--stream-name', command)
        self.assertEqual(
            command[command.index('--stream-name') + 1],
            'custom_live_001',
        )
        self.assertIn('--manifest-content-file', command)
        mpd_cache = command[command.index('--manifest-content-file') + 1]
        self.assertFalse(os.path.exists(mpd_cache))
        header_values = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == '--header'
        ]
        self.assertIn('Cookie: CloudFront-Policy=abc', header_values)
        self.assertIn('User-Agent: Edge UA', header_values)

    def test_source_manifest_headers_include_browser_cookie(self):
        headers = page_harvester.source_manifest_headers(
            'CloudFront-Policy=abc; CloudFront-Signature=def',
            {'Referer': 'https://page.example/live'},
        )

        self.assertEqual(
            headers['Cookie'],
            'CloudFront-Policy=abc; CloudFront-Signature=def',
        )
        self.assertEqual(headers['Referer'], 'https://page.example/live')
        self.assertIn('User-Agent', headers)

    def test_main_uses_manifest_content_file_for_initial_parse(self):
        with tempfile.NamedTemporaryFile(
                mode='w', suffix='.mpd', delete=False,
                encoding='utf-8') as handle:
            handle.write(CLEAR_MPD)
            cache_path = handle.name

        try:
            with mock.patch.object(live_tools, 'load_manifest',
                                   wraps=live_tools.load_manifest) as load:
                result = live_tools.main([
                    '--source', 'https://media.example/index.mpd',
                    '--manifest-content-file', cache_path,
                    '--operation', 'inspect',
                ])
        finally:
            if os.path.exists(cache_path):
                os.unlink(cache_path)

        self.assertEqual(result, 0)
        self.assertEqual(load.call_args.args[0], cache_path)

    def test_main_stream_name_implies_push_and_preserves_cached_mpd_for_relay(self):
        with tempfile.NamedTemporaryFile(
                mode='w', suffix='.mpd', delete=False,
                encoding='utf-8') as handle:
            handle.write(PROTECTED_MPD)
            cache_path = handle.name

        try:
            with mock.patch.object(
                    live_tools, 'perform_livestream',
                    return_value=0) as relay:
                result = live_tools.main([
                    '--source', 'https://media.example/live/index.mpd',
                    '--manifest-content-file', cache_path,
                    '--quality', 'best',
                    '--stream-name', 'custom_stream',
                ])
        finally:
            if os.path.exists(cache_path):
                os.unlink(cache_path)

        self.assertEqual(result, 0)
        selected_stream = relay.call_args.args[0]
        self.assertEqual(selected_stream.url, 'https://media.example/live/index.mpd')
        self.assertEqual(selected_stream.relay_manifest_source, cache_path)

    def test_browser_license_failure_falls_back_to_native_post(self):
        expected_keys = [
            page_harvester.ContentKey(
                kid='e7d6e1cadd9f495a8eade2116710660b',
                key='00112233445566778899aabbccddeeff',
                key_type='CONTENT',
            )
        ]
        session = page_harvester.HttpSession()
        auth = page_harvester.DrmAuthToken(auth_token='token')

        with mock.patch.object(
                page_harvester,
                '_exchange_license_via_browser',
                side_effect=RuntimeError('License 服务器返回 403')), \
             mock.patch.object(
                 page_harvester,
                 'exchange_widevine_license',
                 return_value=expected_keys) as native:
            keys = page_harvester.exchange_license_with_browser_fallback(
                object(),
                session,
                page_harvester.DRMTODAY_WIDEVINE_LICENSE_URL,
                'AAAA',
                auth,
                'device.wvd',
                mpd_url='https://media.example/index.mpd',
            )

        self.assertEqual(keys, expected_keys)
        native.assert_called_once_with(
            session,
            page_harvester.DRMTODAY_WIDEVINE_LICENSE_URL,
            'AAAA',
            auth,
            'device.wvd',
            mpd_url='https://media.example/index.mpd',
        )


if __name__ == '__main__':
    unittest.main()
