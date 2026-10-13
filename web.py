"""Веб-панель: JSON API + одностраничный интерфейс."""
import asyncio
import hmac
import logging
import re
import secrets
import time
from pathlib import Path

from aiohttp import web

import config
import prompts
from settings import UserError, ProposalNotFound, normalize_channel

log = logging.getLogger("web")
STATIC = Path(__file__).parent / "static"
COOKIE = "nb_session"
SESSION_TTL = 30 * 86400
REQUEST_CORE = web.RequestKey('workspace_core', object)


def _err(status, text):
    return web.json_response({"error": text}, status=status)


def build_web(core, password: str) -> web.Application:
    root = core.root
    sessions: dict[str, float] = {}
    fails: dict[str, list[float]] = {}

    @web.middleware
    async def qr_private(request, handler):
        if not request.path.startswith(('/api/tg/qr/', '/api/content-plan')):
            return await handler(request)
        try:
            response = await handler(request)
        except web.HTTPException as error:
            response = _err(error.status,'Материал недоступен')
        except Exception:
            response = _err(400, 'QR недоступен. Попробуйте снова.' if request.path.startswith('/api/tg/qr/') else 'Контент-план недоступен. Повторите позже.')
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Pragma'] = 'no-cache'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @web.middleware
    async def guard(request, handler):
        if request.path.startswith("/api/") and request.path != "/api/login":
            token = request.cookies.get(COOKIE, "")
            if sessions.get(token, 0) < time.time():
                return _err(401, "Нужно войти")
            if request.method != "GET" and request.headers.get("X-Panel") != "1":
                return _err(403, "Запрос отклонён")
        try:
            if request.path.startswith('/api/') and request.path not in ('/api/login','/api/logout'):
                header = request.headers.get('X-Workspace-ID')
                query = request.query.get('workspace_id')
                if header is not None and query is not None and header != query:
                    raise UserError('Несовпадающее пространство')
                value = header if header is not None else query if query is not None else '1'
                if not re.fullmatch(r'[1-9][0-9]{0,17}',value):
                    raise UserError('Неверное пространство')
                try:
                    request[REQUEST_CORE] = root.workspace(int(value))
                except KeyError:
                    return _err(404,'Пространство не найдено')
            return await handler(request)
        except web.HTTPException:
            raise
        except ProposalNotFound as e:
            return _err(404,str(e))
        except UserError as e:
            return _err(400, str(e))
        except Exception as e:
            if request.path.startswith('/api/content-plan'):
                log.error('Content-plan API failed; sensitive details omitted')
                return _err(500,'Контент-план недоступен. Проверьте настройки или повторите позже.')
            if request.path.startswith('/api/tg/qr/'):
                return _err(500, 'QR недоступен. Попробуйте снова.')
            log.exception("Ошибка API %s", request.path)
            return _err(500, f"{type(e).__name__}: {e}")

    routes = web.RouteTableDef()

    async def body(request) -> dict:
        try:
            data = await request.json()
        except Exception:
            raise UserError("Неверный запрос")
        if not isinstance(data, dict):
            raise UserError("Неверный запрос")
        return data

    def ok(**extra):
        return web.json_response({"ok": True, **extra})

    # ---------- страницы ----------
    @routes.get("/")
    async def index(request):
        core = request.get(REQUEST_CORE, root)
        resp = web.FileResponse(STATIC / "index.html")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @routes.get("/health")
    async def health(request):
        core = request.get(REQUEST_CORE, root)
        return web.Response(text="ok")

    # ---------- вход ----------
    @routes.post("/api/login")
    async def login(request):
        core = request.get(REQUEST_CORE, root)
        ip = request.headers.get("X-Forwarded-For", request.remote or "?").split(",")[0].strip()
        recent = [t for t in fails.get(ip, []) if t > time.time() - 600]
        if len(recent) >= 10:
            return _err(429, "Слишком много попыток. Подождите 10 минут.")
        data = await body(request)
        if not hmac.compare_digest(str(data.get("password", "")).encode(), password.encode()):
            recent.append(time.time())
            fails[ip] = recent
            await asyncio.sleep(1)
            return _err(403, "Неверный пароль")
        fails.pop(ip, None)
        token = secrets.token_urlsafe(32)
        sessions[token] = time.time() + SESSION_TTL
        resp = ok()
        resp.set_cookie(COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="Strict",
                        secure=config.PANEL_URL.startswith("https://"))
        return resp

    @routes.post("/api/logout")
    async def logout(request):
        core = request.get(REQUEST_CORE, root)
        sessions.pop(request.cookies.get(COOKIE, ""), None)
        resp = ok()
        resp.del_cookie(COOKIE)
        return resp

    # ---------- состояние и настройки ----------
    @routes.get('/api/workspaces')
    async def workspaces(request):
        core = request.get(REQUEST_CORE, root)
        return ok(workspaces=[{**w, 'recorded_spending': root.db.workspace(w['id']).recorded_spending()}
                              for w in root.db.workspaces()])

    @routes.post('/api/workspaces')
    async def create_workspace(request):
        core = request.get(REQUEST_CORE, root)
        wid=root.db.create_workspace((await body(request)).get('name'))
        return ok(id=wid)

    @routes.post('/api/workspaces/order')
    async def workspace_order(request):
        core = request.get(REQUEST_CORE, root)
        root.db.reorder_workspaces((await body(request)).get('ids'))
        return ok()

    @routes.post('/api/workspaces/{wid}')
    async def rename_workspace(request):
        core = request.get(REQUEST_CORE, root)
        value=request.match_info['wid']
        if not re.fullmatch(r'[1-9][0-9]{0,17}',value):
            raise UserError('Неверное пространство')
        try:
            root.db.rename_workspace(int(value),(await body(request)).get('name'))
        except KeyError:
            return _err(404,'Пространство не найдено')
        return ok()

    @routes.get("/api/state")
    async def state(request):
        core = request.get(REQUEST_CORE, root)
        st = core.state()
        st["personas"] = prompts.PERSONAS
        st["default_prompt"] = prompts.DEFAULT_PROMPT
        st["placeholders"] = prompts.PLACEHOLDERS
        return web.json_response(st)

    @routes.get('/api/posts/reanalyse')
    async def reanalysis_preview(request):
        core = request.get(REQUEST_CORE, root)
        period = request.query.get('period')
        since = core.reanalysis_since(period)
        return ok(period=period, count=core.db.reanalysis_count(since))

    @routes.post('/api/posts/reanalyse')
    async def reanalyse(request):
        core = request.get(REQUEST_CORE, root)
        period = (await body(request)).get('period')
        affected = await core.reanalyse_posts(period)
        if affected is None:
            return _err(409, 'Дождитесь завершения сбора и поставьте расписание на паузу.')
        return ok(period=period, affected=affected)

    @routes.get('/api/test-data/reset')
    async def test_data_preview(request):
        core = request.get(REQUEST_CORE, root)
        return ok(counts=core.db.test_data_counts())

    @routes.post('/api/test-data/reset')
    async def reset_test_data(request):
        core = request.get(REQUEST_CORE, root)
        deleted = await core.reset_test_data((await body(request)).get('confirmation'))
        if deleted is None:
            return _err(409, 'Дождитесь завершения сбора и поставьте расписание на паузу.')
        return ok(deleted=deleted)

    @routes.get("/api/cycles")
    async def cycle_history(request):
        core = request.get(REQUEST_CORE, root)
        before = request.query.get('before')
        if before is not None and (not before.isdigit() or len(before) > 18 or int(before) < 1):
            raise UserError('Неверная страница истории')
        return web.json_response(core.db.cycle_cost_state(before=int(before) if before else None))

    @routes.post("/api/settings")
    async def save_settings(request):
        core = request.get(REQUEST_CORE, root)
        changed = await core.save_settings(await body(request))
        return ok(changed=sorted(changed))

    @routes.get("/api/prompt/preview")
    async def prompt_preview(request):
        core = request.get(REQUEST_CORE, root)
        return ok(system=prompts.build_propose(core.db.get, core.db.geti("proposals"),
                                               core.prompt_extra("propose")))

    @routes.post('/api/persona/generate')
    async def generate_character(request):
        core=request[REQUEST_CORE]
        return ok(**await core.generate_character((await body(request)).get('keywords')))

    @routes.get('/api/operations')
    async def operations(request):
        core=request[REQUEST_CORE]
        before=request.query.get('before')
        if before is not None and not re.fullmatch(r'[1-9][0-9]{0,17}',before):
            raise UserError('Неверная страница истории')
        return web.json_response(core.db.operation_cost_state(before=int(before) if before else None))

    @routes.get('/api/content-plan')
    async def content_plan_list(request):
        return ok(**root.plan.listing(request[REQUEST_CORE]))

    @routes.post('/api/content-plan')
    async def content_plan_create(request):
        return ok(item=root.plan.create(request[REQUEST_CORE], await body(request)))

    @routes.post('/api/content-plan/bulk')
    async def content_plan_bulk(request):
        return ok(items=root.plan.bulk(request[REQUEST_CORE], await body(request)))

    @routes.post('/api/content-plan/pause')
    async def content_plan_pause(request):
        return ok(**root.plan.pause(request[REQUEST_CORE], await body(request)))

    def plan_id(request):
        value = request.match_info['iid']
        if not re.fullmatch(r'[1-9][0-9]{0,17}', value): raise UserError('Неверный ID задания')
        return int(value)

    @routes.post('/api/content-plan/{iid}')
    async def content_plan_edit(request):
        return ok(item=root.plan.edit(request[REQUEST_CORE], plan_id(request), await body(request)))

    @routes.post('/api/content-plan/{iid}/cancel')
    async def content_plan_cancel(request):
        return ok(item=await root.plan.cancel(request[REQUEST_CORE], plan_id(request)))

    @routes.post('/api/content-plan/{iid}/generate')
    async def content_plan_generate(request):
        item = root.plan.start_job(request[REQUEST_CORE],plan_id(request))
        return web.json_response({'ok':True,'item':item},status=202)

    @routes.post('/api/content-plan/{iid}/retry')
    async def content_plan_retry(request):
        item = root.plan.start_job(request[REQUEST_CORE],plan_id(request),retry=True)
        return web.json_response({'ok':True,'item':item},status=202)

    @routes.get('/api/content-plan/{iid}/video')
    async def content_plan_video(request):
        path = root.plan.video(request[REQUEST_CORE],plan_id(request))
        if path is None: raise web.HTTPNotFound()
        return web.FileResponse(path,headers={'Content-Type':'video/mp4','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

    @routes.post('/api/content-plan/{iid}/publish')
    async def content_plan_publish(request):
        data = await body(request)
        return ok(item=await root.plan.publish(request[REQUEST_CORE],plan_id(request),data.get('confirmation')))

    # ---------- Telegram: аккаунт-читатель ----------
    @routes.post('/api/tg/qr/start')
    async def tg_qr_start(request):
        core = request.get(REQUEST_CORE, root)
        return web.json_response(await root.login_qr_start())

    @routes.get('/api/tg/qr/status')
    async def tg_qr_status(request):
        core = request.get(REQUEST_CORE, root)
        return web.json_response(root.login_qr_status())

    @routes.get('/api/tg/qr/image')
    async def tg_qr_image(request):
        core = request.get(REQUEST_CORE, root)
        return web.Response(body=root.login_qr_image(), content_type='image/svg+xml')

    @routes.post("/api/tg/send_code")
    async def tg_send_code(request):
        core = request.get(REQUEST_CORE, root)
        kind = await root.login_send_code(str((await body(request)).get("phone", "")))
        return ok(kind=kind)

    @routes.post("/api/tg/code")
    async def tg_code(request):
        core = request.get(REQUEST_CORE, root)
        return ok(result=await root.login_code(str((await body(request)).get("code", ""))))

    @routes.post("/api/tg/password")
    async def tg_password(request):
        core = request.get(REQUEST_CORE, root)
        return ok(result=await root.login_password(str((await body(request)).get("password", ""))))

    @routes.post("/api/tg/cancel")
    async def tg_cancel(request):
        core = request.get(REQUEST_CORE, root)
        await root.login_cancel()
        return ok()

    @routes.post("/api/tg/logout")
    async def tg_logout(request):
        core = request.get(REQUEST_CORE, root)
        await root.logout_user()
        return ok()

    @routes.post("/api/bot/restart")
    async def bot_restart(request):
        core = request.get(REQUEST_CORE, root)
        await root.start_bot()
        return ok(status=core.bot_status)

    @routes.post("/api/bot/check_target")
    async def check_target(request):
        core = request.get(REQUEST_CORE, root)
        return ok(title=await core.check_target())

    # ---------- LLM ----------
    @routes.post("/api/llm/test")
    async def llm_test(request):
        core = request.get(REQUEST_CORE, root)
        return ok(reply=await core.test_llm())

    @routes.get("/api/llm/models")
    async def llm_models(request):
        core = request.get(REQUEST_CORE, root)
        return ok(models=await core.llm_models())

    # ---------- каналы ----------
    @routes.get("/api/channels")
    async def channels(request):
        core = request.get(REQUEST_CORE, root)
        return web.json_response([dict(r) for r in core.db.channels()])

    @routes.post("/api/channels")
    async def add_channels(request):
        core = request.get(REQUEST_CORE, root)
        return ok(**await core.add_channels(str((await body(request)).get("items", ""))))

    @routes.post("/api/channels/{key}/toggle")
    async def toggle_channel(request):
        core = request.get(REQUEST_CORE, root)
        key = normalize_channel(request.match_info["key"])
        core.db.toggle_channel(key, bool((await body(request)).get("enabled")))
        return ok()

    @routes.delete("/api/channels/{key}")
    async def delete_channel(request):
        core = request.get(REQUEST_CORE, root)
        key = normalize_channel(request.match_info["key"])
        if not core.db.remove_channel(key):
            raise UserError("Канал не найден")
        core.event("info", f"Удалён канал @{key}")
        return ok()

    # ---------- предложения ----------
    @routes.get("/api/proposals")
    async def proposals(request):
        core = request.get(REQUEST_CORE, root)
        status = request.query.get("status") or None
        if status not in (None, "new", "queued", "published", "skipped"):
            raise UserError("Неверный фильтр")
        items = core.db.proposals(status, limit=60)
        out = []
        for p in items:
            rows = core.button_rows(p)
            out.append({
                **root.plan.preview_fields(core,p),
                "id": p["id"], "created": p["created"], "title": p["title"], "text": p["text"],
                "rendered_html": core.post_body(p).html(),
                "status": p["status"], "kind": p["kind"], "published_at": p["published_at"],
                "workspace_id":p["workspace_id"], "target_channel":p["target_channel"],
                "media": bool(p["media"]), "links": core._source_links(p),
                "edited": (p.get("original_text") or p["text"]) != p["text"],
                "notes": [line for m in core.active() for line in m.preview_lines(p)],
                "buttons": [[{"text": t, "data": d} for t, d in row] for row in rows],
            })
        return web.json_response(out)

    @routes.get("/api/proposals/{pid}/media")
    async def proposal_media(request):
        core = request.get(REQUEST_CORE, root)
        media = core.mod("media")
        p = core.db.get_proposal(int(request.match_info["pid"]))
        if not media or not p or not p["media"]:
            raise web.HTTPNotFound()
        data = await media.fetch(p)
        if not data:
            raise web.HTTPNotFound()
        return web.Response(body=data, content_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})

    @routes.post("/api/button")
    async def press_button(request):
        core = request.get(REQUEST_CORE, root)
        """Та же кнопка, что в Telegram: pub:/skip:/rw:/m:<модуль>:<действие>:<id>."""
        data = str((await body(request)).get("data", ""))
        action, _, arg = data.partition(":")
        if action == "m":
            module_id, mod_action, pid = arg.split(":", 2)
            return ok(message=await core.module_action(module_id, mod_action, int(pid)))
        if not arg.isdigit():
            raise UserError("Неверная кнопка")
        pid = int(arg)
        if action == "pub":
            return ok(message=await core.approve(pid))
        if action == "skip":
            await core.skip(pid)
            return ok(message="Пропущено")
        if action == "rw":
            return ok(message="Переписано", text=await core.rewrite(pid))
        raise UserError("Неизвестное действие")

    # ---------- модули ----------
    @routes.post("/api/modules/{mid}")
    async def save_module(request):
        core = request.get(REQUEST_CORE, root)
        m = core.modules.get(request.match_info["mid"])
        if not m:
            raise UserError("Нет такого модуля")
        m.save(await body(request))
        return ok()

    @routes.post("/api/modules/{mid}/action")
    async def module_action(request):
        core = request.get(REQUEST_CORE, root)
        data = await body(request)
        pid = data.get("pid")
        msg = await core.module_action(request.match_info["mid"], str(data.get("action", "")),
                                       int(pid) if pid else None, data)
        return ok(message=msg)

    @routes.post("/api/proposals/{pid}/{action}")
    async def proposal_action(request):
        core = request.get(REQUEST_CORE, root)
        pid = int(request.match_info["pid"])
        action = request.match_info["action"]
        if action == "approve":
            return ok(message=await core.approve(pid))
        if action == "publish":
            await core.publish(pid)
        elif action == "skip":
            await core.skip(pid)
        elif action == "rewrite":
            return ok(text=await core.rewrite(pid))
        elif action == "text":
            await core.set_text(pid, str((await body(request)).get("text", "")))
        else:
            raise UserError("Неизвестное действие")
        return ok()

    # ---------- запуск ----------
    @routes.post("/api/run")
    async def run(request):
        core = request.get(REQUEST_CORE, root)
        core.trigger_run()
        return ok()

    @routes.get("/api/events")
    async def events(request):
        core = request.get(REQUEST_CORE, root)
        return web.json_response(core.db.events(150))

    app = web.Application(middlewares=[qr_private, guard], client_max_size=2 * 1024 * 1024)
    app.add_routes(routes)
    async def cleanup(application):
        await root.login_cancel()
    app.on_cleanup.append(cleanup)
    return app
