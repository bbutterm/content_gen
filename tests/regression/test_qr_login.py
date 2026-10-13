import asyncio
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
import core
from core import App
from db import DB
from telethon.errors import SessionPasswordNeededError

class FakeQR:
    def __init__(self):
        self.url = 'tg://login?token=TEST_ONLY_NOT_A_SECRET'
        self.expires = datetime.now(timezone.utc) + timedelta(seconds=60)
        self.entered = asyncio.Event()
        self.result = asyncio.get_running_loop().create_future()
    async def wait(self, timeout=None):
        self.entered.set()
        return await self.result

class FakeClient:
    instances = []
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.qr = FakeQR()
        self.disconnected = False
        self.session = SimpleNamespace(save=lambda: 'TEST_SESSION')
        self.me = SimpleNamespace(phone='15550000000', first_name='Test', last_name=None)
        self.need_password = False
        self.instances.append(self)
    async def connect(self): pass
    async def disconnect(self): self.disconnected = True
    async def qr_login(self): return self.qr
    async def get_me(self): return self.me
    async def is_user_authorized(self): return True
    async def get_dialogs(self, **kwargs): return []
    async def send_code_request(self, phone):
        return SimpleNamespace(phone_code_hash='TEST_HASH', type=type('SentCodeTypeApp', (), {})())
    async def sign_in(self, *args, **kwargs):
        if self.need_password and 'password' not in kwargs:
            raise SessionPasswordNeededError(None)

class QRTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = DB(':memory:')
        self.db.set('tg_api_id', '12345')
        self.db.set('tg_api_hash', 'test')
        self.app = App(self.db)
        self.patcher = patch.object(core, 'TelegramClient', FakeClient)
        self.patcher.start()
        self.session_patcher = patch.object(core, 'StringSession', lambda *a: None)
        self.session_patcher.start()
        FakeClient.instances.clear()
    async def asyncTearDown(self):
        await self.app.login_cancel()
        await self.app.stop_user()
        self.patcher.stop()
        self.session_patcher.stop()
        self.db.conn.close()
    async def test_start_waits_before_return_and_keeps_token_private(self):
        self.assertTrue(callable(getattr(self.app, 'login_qr_start', None)), 'QR start missing')
        result = await self.app.login_qr_start()
        client = FakeClient.instances[0]
        self.assertTrue(client.qr.entered.is_set())
        self.assertTrue(client.kwargs['receive_updates'])
        self.assertEqual(client.kwargs['flood_sleep_threshold'], 0)
        self.assertEqual(result['phase'], 'waiting')
        self.assertNotIn('token', str(result))
        self.assertNotIn('TEST_ONLY', str(self.app.state()))
        image = self.app.login_qr_image()
        self.assertIn(b'<svg', image)
        self.assertNotIn(b'tg://', image)

    async def test_success_without_phone_finishes_reader(self):
        await self.app.login_qr_start()
        client = FakeClient.instances[0]
        task = self.app.login['task']
        client.qr.result.set_result(client.me)
        await task
        self.assertIsNotNone(self.app.user)
        self.assertEqual(self.db.get('user_phone'), '+15550000000')
        self.assertEqual(self.app.login_qr_status()['phase'], 'ok')

    async def test_cancel_stops_wait_and_preserves_authorized_reader(self):
        await self.app.login_qr_start()
        attempt = self.app.login
        await self.app.login_cancel()
        self.assertTrue(attempt['task'].done())
        self.assertTrue(attempt['client'].disconnected)
        self.assertEqual(self.db.get('user_session'), '')
        existing = object()
        self.app.user = existing
        self.assertEqual((await self.app.login_qr_start())['phase'], 'ok')
        self.assertIs(self.app.user, existing)
        self.app.user = None

    async def test_qr_password_uses_existing_form(self):
        await self.app.login_qr_start()
        attempt = self.app.login
        attempt['client'].qr.result.set_exception(SessionPasswordNeededError(None))
        await attempt['task']
        self.assertEqual(self.app.login_qr_status()['phase'], 'password')
        self.assertEqual(await self.app.login_password('test-only'), 'ok')
        self.assertIsNotNone(self.app.user)

    async def test_timeout_and_generic_error_clear_image(self):
        for error, phase in [(asyncio.TimeoutError(), 'expired'), (RuntimeError('sensitive-test'), 'error')]:
            await self.app.login_qr_start()
            attempt = self.app.login
            attempt['client'].qr.result.set_exception(error)
            await attempt['task']
            self.assertEqual(self.app.login_qr_status()['phase'], phase)
            self.assertNotIn('sensitive', str(self.app.login_qr_status()))
            self.assertNotIn('image', attempt)
            self.assertTrue(attempt['client'].disconnected)

    async def test_sms_and_sms_password_regression(self):
        self.assertEqual(await self.app.login_send_code('+15550000000'), 'app')
        self.assertFalse(FakeClient.instances[0].kwargs['receive_updates'])
        FakeClient.instances[0].need_password = True
        self.assertEqual(await self.app.login_code('12345'), 'password')
        self.assertEqual(await self.app.login_password('test-only'), 'ok')
        self.assertEqual(self.db.get('user_phone'), '+15550000000')
        self.assertEqual(self.db.get('auto_publish'), '0')

    async def test_panel_qr_routes_are_guarded_and_no_store(self):
        from aiohttp.test_utils import TestClient, TestServer
        from web import build_web
        import config
        with patch.object(config, 'PANEL_URL', ''):
            panel = TestClient(TestServer(build_web(self.app, 'test-panel')))
            await panel.start_server()
            try:
                for method, path in [('POST','/api/tg/qr/start'), ('GET','/api/tg/qr/status'), ('GET','/api/tg/qr/image')]:
                    response = await panel.request(method, path)
                    self.assertEqual(response.status, 401)
                    self.assertEqual(response.headers.get('Cache-Control'), 'no-store')
                await panel.post('/api/login', json={'password':'test-panel'})
                response = await panel.post('/api/tg/qr/start')
                self.assertEqual(response.status, 403)
                response = await panel.post('/api/tg/qr/start', headers={'X-Panel':'1'})
                self.assertEqual(response.status, 200)
                self.assertNotIn('TEST_ONLY', await response.text())
                response = await panel.get('/api/tg/qr/status')
                self.assertEqual((await response.json())['phase'], 'waiting')
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                response = await panel.get('/api/tg/qr/image')
                self.assertEqual(response.status, 200)
                self.assertEqual(response.content_type, 'image/svg+xml')
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertNotIn(b'tg://', await response.read())
            finally:
                await panel.close()

    def test_frontend_on_demand_qr_controls(self):
        from pathlib import Path
        text = Path('static/index.html').read_text()
        self.assertIn('id="startQR"', text)
        self.assertIn('id="renewQR"', text)
        self.assertIn('id="lpQR"', text)
        self.assertIn('/api/tg/qr/image', text)

    async def test_stale_password_cannot_finish_new_attempt(self):
        await self.app.login_qr_start()
        old = self.app.login
        old['phase'] = 'password'
        gate = asyncio.Event()
        async def slow_sign_in(**kwargs):
            await gate.wait()
        old['client'].sign_in = slow_sign_in
        pending = asyncio.create_task(self.app.login_password('test-only'))
        await asyncio.sleep(0)
        await self.app.login_cancel()
        await self.app.login_qr_start()
        new = self.app.login
        gate.set()
        try:
            await pending
        except Exception:
            pass
        self.assertIs(self.app.login, new)
        self.assertEqual(self.db.get('user_session'), '')

    async def test_qr_password_errors_are_generic(self):
        await self.app.login_qr_start()
        self.app.login['phase'] = 'password'
        async def fail(**kwargs): raise RuntimeError('sensitive-test')
        self.app.login['client'].sign_in = fail
        try:
            await self.app.login_password('test-only')
        except Exception as e:
            self.assertNotIn('sensitive-test', str(e))
        else:
            self.fail('Expected safe password error')

    async def test_stale_finish_during_get_me_does_not_write(self):
        await self.app.login_qr_start()
        old = self.app.login
        entered, gate = asyncio.Event(), asyncio.Event()
        async def delayed_me():
            entered.set()
            await gate.wait()
            return old['client'].me
        old['client'].get_me = delayed_me
        old['client'].qr.result.set_result(None)
        await entered.wait()
        await self.app.login_cancel()
        await self.app.login_qr_start()
        new = self.app.login
        gate.set()
        self.assertIs(self.app.login, new)
        self.assertEqual(self.db.get('user_session'), '')

    async def test_expired_image_is_not_served_before_worker_ticks(self):
        await self.app.login_qr_start()
        self.app.login['expires'] = 0
        from settings import UserError
        with self.assertRaises(UserError):
            self.app.login_qr_image()

    async def test_panel_cleanup_cancels_pending_login(self):
        from aiohttp.test_utils import TestClient, TestServer
        from web import build_web
        await self.app.login_qr_start()
        attempt = self.app.login
        panel = TestClient(TestServer(build_web(self.app, 'test-panel')))
        await panel.start_server()
        await panel.close()
        self.assertIsNone(self.app.login)
        self.assertTrue(attempt['task'].done())

if __name__ == '__main__': unittest.main()
