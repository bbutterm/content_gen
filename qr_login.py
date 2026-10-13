"""On-demand QR login. Tokens remain in memory, never in panel state."""
import asyncio
import io
from datetime import datetime, timezone
from settings import UserError

REQUEST_TIMEOUT = 20


def render(url):
    import qrcode
    from qrcode.image.svg import SvgPathImage
    image = qrcode.make(url, image_factory=SvgPathImage, border=4)
    out = io.BytesIO()
    image.save(out)
    return out.getvalue()


async def start(app, client_factory, session_factory):
    if app.user is not None:
        return {'phase': 'ok'}
    api_id, api_hash = app.api_creds()
    if not api_id:
        raise UserError('Сначала сохраните api_id и api_hash (шаг 1)')
    await app.login_cancel()
    client = client_factory(session_factory(), api_id, api_hash, receive_updates=True,
                            flood_sleep_threshold=0, timeout=10, request_retries=0,
                            connection_retries=1)
    attempt = {'client': client, 'mode': 'qr', 'phase': 'starting'}
    app.login = attempt
    try:
        await asyncio.wait_for(client.connect(), REQUEST_TIMEOUT)
        qr = await asyncio.wait_for(client.qr_login(), REQUEST_TIMEOUT)
        if app.login is not attempt:
            raise asyncio.CancelledError()
        attempt.update(qr=qr, image=render(qr.url), phase='waiting',
                       expires=qr.expires.timestamp())
        entered = asyncio.Event()
        async def registered_wait(remaining):
            entered.set()
            return await qr.wait(timeout=remaining)
        async def wait():
            from telethon.errors import SessionPasswordNeededError
            try:
                remaining = max(0, attempt['expires'] - datetime.now(timezone.utc).timestamp())
                await asyncio.wait_for(registered_wait(remaining), remaining + 1)
                if app.login is attempt:
                    await app._login_finish(attempt)
            except SessionPasswordNeededError:
                if app.login is attempt:
                    attempt['phase'] = 'password'
            except asyncio.TimeoutError:
                if app.login is attempt:
                    attempt['phase'] = 'expired'
            except asyncio.CancelledError:
                raise
            except Exception:
                if app.login is attempt:
                    attempt['phase'] = 'error'
            finally:
                attempt.pop('image', None)
                attempt.pop('qr', None)
                if attempt['phase'] != 'password':
                    try:
                        await asyncio.wait_for(client.disconnect(), REQUEST_TIMEOUT)
                    except Exception:
                        pass
        attempt['task'] = app.spawn(wait())
        # Let qr.wait register its update handler before exposing the image.
        await asyncio.wait_for(entered.wait(), REQUEST_TIMEOUT)
        return status(app)
    except BaseException:
        if app.login is attempt:
            app.login = None
        await client.disconnect()
        raise UserError('Не удалось создать QR. Попробуйте снова.') from None


def status(app):
    if app.user is not None:
        return {'phase': 'ok'}
    attempt = app.login
    if not attempt or attempt.get('mode') != 'qr':
        return {'phase': 'idle'}
    return {'phase': attempt['phase'], 'expires': attempt.get('expires')}


def image(app):
    attempt = app.login
    if (not attempt or attempt.get('mode') != 'qr' or attempt['phase'] != 'waiting'
            or attempt.get('expires', 0) <= datetime.now(timezone.utc).timestamp()):
        raise UserError('QR недоступен. Создайте новый QR.')
    return attempt['image']
