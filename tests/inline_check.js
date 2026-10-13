
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let S = null, tab = "overview", propStatus = "new";
const openSteps = new Set(), closedSteps = new Set();

const STEPS = {
  api: {t: "Ключи Telegram API", go: "sec-api", h: `<ol>
    <li>Открой <a href="https://my.telegram.org" target="_blank" rel="noopener">my.telegram.org</a> и войди по номеру <b>аккаунта-читателя</b> (+7…). Код придёт в приложение Telegram, в чат «Telegram», — введи его на сайте.</li>
    <li>Нажми <b>API development tools</b>.</li>
    <li>Если приложения ещё нет — заполни: <i>App title</i> — любое (например NewsReader), <i>Short name</i> — латиницей, 5–32 символа, <i>Platform</i> — Desktop. Остальное можно не трогать. Нажми <b>Create application</b>.</li>
    <li>Скопируй <b>App api_id</b> и <b>App api_hash</b> и вставь их в «Подключения → 1. Telegram API».</li></ol>
    <p class="tip">Если сайт отвечает «ERROR» при создании приложения — отключи VPN или блокировщик рекламы либо попробуй другой браузер.</p>`},
  reader: {t: "Войди аккаунтом-читателем", go: "sec-reader", h: `<ol>
    <li>В «Подключения → 2. Аккаунт-читатель» введи номер и нажми «Получить код».</li>
    <li>Код придёт в Telegram (чат «Telegram») или по SMS — введи его в панели.</li>
    <li>Если включена двухэтапная аутентификация — панель попросит облачный пароль.</li></ol>
    <p class="warn">Никому не пересылай код и не отправляй его в чаты, даже в «Избранное»: Telegram сразу аннулирует код, увидев его в сообщении.</p>`},
  bot: {t: "Создай бота", go: "sec-bot", h: `<ol>
    <li>Открой <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a> и отправь <code>/newbot</code>.</li>
    <li>Придумай имя (любое) и username — латиницей, должен заканчиваться на <code>bot</code>.</li>
    <li>BotFather пришлёт токен вида <code>1234567890:AA…</code> — вставь его в «Подключения → 3. Бот» и сохрани.</li></ol>`},
  admin: {t: "Привяжи свой Telegram", go: "sec-bot", h: `<ol>
    <li>В «Подключения → 3. Бот» нажми «Открыть бота и привязать».</li>
    <li>В Telegram нажми <b>Запустить</b>. Бот ответит «Готово».</li></ol>
    <p class="tip">Не открылось? Отправь боту команду, которая написана под кнопкой.</p>`},
  target: {t: "Канал для публикаций", go: "sec-target", h: `<ol>
    <li>Создай канал или возьми существующий.</li>
    <li>Канал → Управление каналом → Администраторы → Добавить администратора → найди бота по @username → оставь право «Публикация сообщений» → Сохранить.</li>
    <li>Впиши @username канала в «Подключения → 4» и нажми «Сохранить и проверить».</li></ol>
    <p class="tip">Канал приватный? Перешли боту любой пост из него — бот покажет кнопку «Публиковать сюда».</p>`},
  llm: {t: "Подключи нейросеть", go: "sec-llm", h: `<ol>
    <li>Выбери провайдера в «Подключения → 5. Нейросеть».</li>
    <li>Получи API-ключ на сайте провайдера (ссылка под полем ключа), при необходимости пополни баланс.</li>
    <li>Вставь ключ и сохрани, нажми «Загрузить модели», выбери модель, сохрани и нажми «Проверить».</li></ol>`},
  channels: {t: "Добавь каналы-источники", go: "channels", h: `<ol>
    <li>Во вкладке «Каналы» вставь @username или ссылки t.me/… — можно сразу списком.</li>
    <li>Приватный канал: подпиши на него аккаунт-читатель, перешли боту пост оттуда и нажми «Сделать источником».</li></ol>
    <p class="tip">Заодно загляни во вкладку «ИИ и промпт» и выбери характер модели.</p>`},
  run: {t: "Пробный запуск", go: "overview", h: `<p>Нажми «Собрать сейчас» вверху. Через минуту-две новости придут боту в личку и появятся во вкладке «Предложения». Дальше бот работает по расписанию.</p>`},
};
const PANEL_TABS = {"sec-api":"connect","sec-reader":"connect","sec-bot":"connect","sec-target":"publish","sec-llm":"connect","sec-model":"ai"};

// ---------- утилиты ----------
async function api(method, path, body) {
  const epoch=workspaceEpoch, wid=workspaceID;
  const controller=new AbortController(); pendingRequests.add(controller);
  const opt = {method, signal:controller.signal, headers: {"X-Panel": "1", "X-Workspace-ID":String(wid)}};
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  let r;
  try { r=await fetch(path,opt); }
  catch(e) { if (epoch!==workspaceEpoch) throw new StaleWorkspace(); throw e; }
  finally { pendingRequests.delete(controller); }
  if (epoch!==workspaceEpoch) throw new StaleWorkspace();
  let data = null; try { data = await r.json(); } catch (e) {}
  if (epoch!==workspaceEpoch) throw new StaleWorkspace();
  if (r.status === 401 && path !== "/api/login") { showLogin(); throw new Error("Нужно войти"); }
  if (!r.ok) throw new Error((data && data.error) || `Ошибка ${r.status}`);
  return data;
}
function toast(msg, kind = "") {
  const d = document.createElement("div"); d.textContent = msg; if (kind) d.className = kind;
  $("#toast").appendChild(d); setTimeout(() => d.remove(), kind === "err" ? 6000 : 3000);
}
async function act(btn, fn, okMsg) {
  const epoch=workspaceEpoch;
  if (btn) { btn.disabled = true; btn.classList.add("busy"); }
  try { const r = await fn(); if (okMsg) toast(typeof okMsg === "function" ? okMsg(r) : okMsg); return r; }
  catch (e) { if (!(e instanceof StaleWorkspace) && e.message !== "Нужно войти") toast(e.message, "err"); }
  finally { if (btn && epoch===workspaceEpoch) { btn.disabled = false; btn.classList.remove("busy"); } }
}
const fmt = ts => ts ? new Date(ts * 1000).toLocaleString("ru-RU", {day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"}) : "—";
function rel(ts) {
  const m = Math.round((ts * 1000 - Date.now()) / 60000);
  if (m <= 0) return "в ближайшую минуту";
  return m < 60 ? `через ${m} мин` : `через ${Math.floor(m / 60)} ч ${m % 60} мин`;
}
function goTo(target) {
  const t = PANEL_TABS[target] || target;
  showTab(t);
  const el = document.getElementById(target);
  if (el && el.classList.contains("card")) setTimeout(() => el.scrollIntoView({behavior: "smooth", block: "start"}), 50);
}

// ---------- вход ----------
function showLogin() { $("#app").classList.add("hidden"); $("#login").classList.remove("hidden"); $("#pw").focus(); }
$("#loginForm").addEventListener("submit", async e => {
  e.preventDefault();
  await act(e.submitter, async () => {
    await api("POST", "/api/login", {password: $("#pw").value});
    $("#pw").value = ""; $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
    await refresh(true);
  });
});
$("#logoutBtn").onclick = async () => { await api("POST", "/api/logout").catch(() => {}); showLogin(); };

// ---------- вкладки ----------
function showTab(name) {
  tab = name;
  $$("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === name));
  $$("section.tab").forEach(s => s.classList.toggle("hidden", s.id !== "tab-" + name));
  if (name === "channels") loadChannels().catch(ignoreStale);
  if (name === "proposals") loadProposals().catch(ignoreStale);
  if (name === "log") loadEvents().catch(ignoreStale);
  if (name === "modules") renderModules();
  window.scrollTo(0, 0);
}
$$("#tabs button").forEach(b => b.onclick = () => showTab(b.dataset.tab));

// ---------- состояние ----------
class StaleWorkspace extends Error {}
let workspaceID=1, workspaceEpoch=0, W=[], pendingRequests=new Set(), personaDirty=false, personaRevision=0;
function ignoreStale(e) { if (!(e instanceof StaleWorkspace)) toast(e.message,"err"); }
async function switchWorkspace(id) {
  if(Number(id)===workspaceID)return;
  if(personaDirty && !confirm('Есть несохранённые правки характера. Переключить пространство и отказаться от них?')) {$('#workspaceSelect').value=String(workspaceID);return}
  pendingRequests.forEach(c=>c.abort()); pendingRequests.clear();
  workspaceEpoch++; workspaceID=Number(id); personaDirty=false; personaRevision++;
  $$('.busy').forEach(b=>{b.disabled=false;b.classList.remove('busy')});
  ['#channelList','#propList','#events','#addResult','#promptPreview','#operationCost'].forEach(s=>$(s).textContent='');
  $('#characterKeywords').value=''; $('#characterSuggestion').classList.add('hidden');
  await refresh(true); showTab(tab);
}
$('#workspaceSelect').onchange=e=>switchWorkspace(e.target.value).catch(ignoreStale);
$('#createWorkspace').onclick=e=>act(e.currentTarget,async()=>{
  const name=prompt('Название нового пространства (до 80 символов):'); if(name===null)return;
  const r=await api('POST','/api/workspaces',{name}); await switchWorkspace(r.id);
});
$('#renameWorkspace').onclick=e=>act(e.currentTarget,async()=>{
  const name=prompt('Новое название пространства:',S.workspace.name); if(name===null)return;
  await api('POST',`/api/workspaces/${workspaceID}`,{name}); await refresh();
});
async function moveWorkspace(direction) {
  const ids=W.map(w=>w.id), i=ids.indexOf(workspaceID), j=i+direction;
  if(j<0 || j>=ids.length)return;
  [ids[i],ids[j]]=[ids[j],ids[i]]; await api('POST','/api/workspaces/order',{ids}); await refresh();
}
$('#workspaceLeft').onclick=e=>act(e.currentTarget,()=>moveWorkspace(-1));
$('#workspaceRight').onclick=e=>act(e.currentTarget,()=>moveWorkspace(1));
$('#sec-persona').addEventListener('input',()=>{personaDirty=true;personaRevision++});
function applyCharacter(text) {$('#personaInput').value='custom';$('[data-key="persona_custom"]').value=text;personaDirty=true;personaRevision++;renderPersonas()}
$('#generateCharacter').onclick=e=>act(e.currentTarget,async()=>{
  if(personaDirty && !confirm('Заменить несохранённые правки характера новой генерацией?'))return;
  const revision=personaRevision, original=$('[data-key="persona_custom"]').value;
  const r=await api('POST','/api/persona/generate',{keywords:$('#characterKeywords').value});
  if(personaRevision!==revision || $('[data-key="persona_custom"]').value!==original) {
    $('#characterSuggestionText').value=r.instruction; $('#characterSuggestion').classList.remove('hidden');
    toast('Новая инструкция показана отдельно: ваши правки сохранены.');
  } else {applyCharacter(r.instruction);toast('Инструкция готова. Отредактируй и нажми «Сохранить».')}
  await refresh();
});
$('#useCharacterSuggestion').onclick=()=>{if(personaDirty && !confirm('Заменить текущие правки новой инструкцией?'))return;applyCharacter($('#characterSuggestionText').value);$('#characterSuggestion').classList.add('hidden')};

async function refresh(fillForms = false) {
  S = await api("GET", "/api/state");
  W = (await api("GET", "/api/workspaces")).workspaces;
  $('#workspaceName').textContent=S.workspace.name;
  $('#workspaceSelect').innerHTML=W.map(w=>`<option value="${w.id}">${esc(w.name)} · учтено ${esc(w.recorded_spending.known_cost_usd)} USD${w.recorded_spending.incomplete_records ? ` · ${w.recorded_spending.incomplete_records} неполн.` : ''}</option>`).join('');
  $('#workspaceSelect').value=String(workspaceID);
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
  renderOverview(); renderConnect();
  if (fillForms) fill();
}

const COST_PROVENANCE = {provider_reported: "Стоимость из ответа провайдера", tariff_estimate: "Оценка по тарифу", local_no_bill: "Локальная модель: без счёта API", no_calls: "Без вызовов LLM", mixed: "Смешанный расчёт", pending: "Пока не завершён", "unknown/incomplete": "Стоимость неизвестна / неполная", unknown: "Стоимость неизвестна / неполная"};
const CYCLE_STATUS = {running:"Сбор идёт",completed:"Завершён",empty:"Нет постов для обработки",failed:"Ошибка",cancelled:"Отменён",interrupted:"Прерван перезапуском"};
function costText(r) {
  return r.cost_usd === null || r.cost_usd === undefined ? `неизвестна / неполная; известная часть ${esc(r.known_cost_usd || "0")} USD` : `${esc(r.cost_usd)} USD`;
}
function renderCycleCosts() {
  const op=S.operation_cost || {history:[]};
  $('#operationCost').innerHTML=(op.history || []).map(r=>`<details><summary>${esc(r.operation_type)} #${r.id} · ${fmt(r.started_at)} · ${esc(CYCLE_STATUS[r.status] || r.status)} · ${costText(r)}</summary><p>${esc(COST_PROVENANCE[r.provenance] || r.provenance)} · вызовов ${r.calls}</p><ul>${(r.attempts||[]).map(a=>`<li>${esc(a.provider)} / ${esc(a.model)} · ${esc(a.status)} · ${a.cost_usd===null?'неизвестна':esc(a.cost_usd)+' USD'}</li>`).join('')}</ul></details>`).join('') || '<p>Ещё нет учтённых операций вне сборов.</p>';
  const c = S.cycle_cost || {history:[]};
  const summary = r => `${esc(CYCLE_STATUS[r.status] || r.status)} · ${costText(r)} · ${esc(COST_PROVENANCE[r.provenance] || r.provenance)} · вызовов ${r.calls} · токены вход/выход ${r.input_tokens}/${r.output_tokens}${r.tokens_complete ? "" : " (неполные, известная часть)"}`;
  $("#cycleCost").innerHTML = (c.running ? '<p class="warn-t">Сбор идёт — итоговая стоимость после завершения.</p>' : '') +
    (c.last_completed ? `<p><b>Последний завершённый цикл #${c.last_completed.id}</b><br>${summary(c.last_completed)}</p>` : '<p>Ещё нет истории завершённых сборов.</p>') +
    (c.history || []).map(r => `<details><summary>#${r.id} · ${fmt(r.started_at)} · ${summary(r)}</summary><p>${r.result ? `Постов ${r.result.posts}, предложений ${r.result.proposals}` : 'Сбор не дошёл до обработки постов'}</p><ul>${(r.attempts || []).map(a => `<li>${esc(a.stage)} · ${esc(a.provider)} / ${esc(a.model)} · ${esc(a.status)} · токены ${a.input_tokens ?? '—'}/${a.output_tokens ?? '—'} · ${a.cost_usd === null ? 'неизвестна' : esc(a.cost_usd)+' USD'} · ${esc(COST_PROVENANCE[a.provenance] || a.provenance)}</li>`).join('')}</ul></details>`).join('');
}
function renderOverview() {
  renderCycleCosts();
  const spending=S.recorded_spending;
  const spendingBox=$('#recordedSpending');
  spendingBox.innerHTML=`<h3>Учтённые расходы: ${esc(S.workspace.name)}</h3><p>Известная сумма: <b>${esc(spending.known_cost_usd)} USD</b> · операций с неизвестной / неполной стоимостью: <b>${spending.incomplete_records}</b> · записей: ${spending.records}</p><p class="sub">Не полный биллинг: только сохранённые отчёты сборов и учтённых отдельных операций. Оценки по тарифу включены; расходы до начала учёта и неучтённые вызовы неизвестны.</p>`;
  const st = S.settings, done = S.steps.filter(s => s.done).length, total = S.steps.length;
  const ready = S.steps.slice(0, 7).every(s => s.done);
  const pill = $("#pill");
  if (S.running) { pill.textContent = "Собираю…"; pill.className = "pill warn"; }
  else if (!ready) { pill.textContent = `Настройка ${done}/${total}`; pill.className = "pill warn"; }
  else if (st.paused === "1") { pill.textContent = "Пауза"; pill.className = "pill"; }
  else { pill.textContent = "Работает"; pill.className = "pill ok"; }

  const yes = (cond, okText, badText) => cond ? `<span class="ok-t">✓ ${esc(okText)}</span>` : `<span class="warn-t">${esc(badText)}</span>`;
  const lr = S.last_result;
  let next = S.next_run_reason;
  if (!next) next = S.next_run ? rel(S.next_run) : "—";
  else if (S.next_run && next === "тихие часы") next = "после тихих часов";
  $("#statusKv").innerHTML = `
    <dt>Читатель</dt><dd>${yes(S.user.status === "ok", S.user.name || "подключён", S.user.status)}</dd>
    <dt>Бот</dt><dd>${yes(S.bot.status === "ok", "@" + S.bot.username, S.bot.status)}</dd>
    <dt>Нейросеть</dt><dd>${S.llm.ok ? `<span class="ok-t">✓ ${esc(st.llm_model)}</span>` : (S.llm.configured ? `<span class="warn-t">не проверена</span>` : `<span class="warn-t">не настроена</span>`)}</dd>
    <dt>Канал</dt><dd>${st.target_channel ? esc(st.target_channel.match(/^-?\d+$/) ? st.target_channel : "@" + st.target_channel) : '<span class="warn-t">не указан</span>'}</dd>
    <dt>Источников</dt><dd>${S.channels_count}</dd>
    <dt>Последний сбор</dt><dd>${lr ? `${fmt(lr.ts)} — постов ${lr.posts}, новостей ${lr.proposals}` : "ещё не было"}</dd>
    <dt>Следующий</dt><dd>${esc(next)}</dd>`;
  $("#runBtn").disabled = S.running;
  $("#runBtn").textContent = S.running ? "Собираю…" : "Собрать сейчас";
  $("#pauseBtn").textContent = st.paused === "1" ? "Продолжить" : "Пауза";

  const s = S.stats;
  $("#stats").innerHTML = [["Ждут модерации", s.new], ["Опубликовано за сутки", s.published_24h],
    ["Опубликовано всего", s.published], ["Постов собрано за сутки", s.posts_24h]]
    .concat(S.modules.some(m => m.id === "queue" && m.enabled) ? [["В очереди", s.queued]] : [])
    .map(([l, v]) => `<div class="stat"><b>${v}</b><span>${l}</span></div>`).join("");
  const nc = $("#newCount"); nc.textContent = s.new; nc.classList.toggle("hidden", !s.new);

  const firstOpen = S.steps.find(x => !x.done);
  const allDone = !firstOpen, sec = $("#tab-overview"), card = $("#setupCard");
  if (allDone && sec.lastElementChild !== card) sec.appendChild(card);
  if (!allDone && sec.firstElementChild !== card) sec.prepend(card);
  $("#setupCard .sub").textContent = allDone ? "Всё настроено ✓ Нажми на шаг, чтобы посмотреть инструкцию." : "Пройди шаги по порядку. Готовые отмечаются сами.";
  $("#steps").innerHTML = S.steps.map((x, i) => {
    const info = STEPS[x.id], cur = firstOpen && firstOpen.id === x.id;
    const open = openSteps.has(x.id) || (cur && !closedSteps.has(x.id));
    return `<li class="step ${x.done ? "done" : ""} ${cur ? "cur" : ""}" data-id="${x.id}">
      <button type="button"><span class="num">${x.done ? "✓" : i + 1}</span><span class="t">${info.t}</span><span style="color:var(--muted)">›</span></button>
      <div class="body ${open ? "" : "hidden"}">${info.h}
        <div class="actions"><button class="b" data-go="${info.go}">Перейти</button></div></div></li>`;
  }).join("");
  $$("#steps .step>button").forEach(b => b.onclick = () => {
    const id = b.parentElement.dataset.id, hidden = b.nextElementSibling.classList.toggle("hidden");
    if (hidden) { openSteps.delete(id); closedSteps.add(id); } else { openSteps.add(id); closedSteps.delete(id); }
  });
  $$("#steps [data-go]").forEach(b => b.onclick = () => goTo(b.dataset.go));
}

function renderConnect() {
  const u = S.user;
  $("#readerOn").classList.toggle("hidden", u.status !== "ok");
  $("#readerOff").classList.toggle("hidden", u.status === "ok");
  $("#readerName").textContent = u.name || "";
  $("#readerPhone").textContent = u.phone || "";
  $("#readerStatus").textContent = u.status === "ok" ? "" : "Статус: " + u.status;

  const b = S.bot;
  $("#botStatus").innerHTML = b.status === "ok" ? `<span class="ok-t">✓ Бот работает: <b>@${esc(b.username)}</b></span>` : `<span class="warn-t">Статус: ${esc(b.status)}</span>`;
  $("#linkBox").classList.toggle("hidden", !b.link);
  if (b.link) { $("#linkBtn").href = b.link; $("#linkCmd").textContent = "/start " + b.link_code; }

  $("#llmStatus").innerHTML = S.llm.ok ? '<span class="ok-t">✓ Подключение проверено</span>' : "";
  $("#tzHint").textContent = `Время по часовому поясу ${S.tz}. Пусто — без тихих часов.`;
}

function fill() {
  const preserved=personaDirty ? collectFrom($('#sec-persona')) : null;
  const st = S.settings;
  $$("[data-key]").forEach(el => {
    const k = el.dataset.key;
    if (el.dataset.ph === undefined) el.dataset.ph = el.placeholder || "";
    if (el.type === "checkbox") el.checked = st[k] === "1";
    else if (st[k + "_hint"] !== undefined) { el.value = ""; el.placeholder = st[k + "_hint"] ? `сохранён ${st[k + "_hint"]} · пусто = не менять` : el.dataset.ph || el.placeholder; }
    else if (k === "target_channel" && st[k] && !/^-?\d+$/.test(st[k])) el.value = "@" + st[k];
    else el.value = st[k] ?? "";
  });
  if(preserved) $$('#sec-persona [data-key]').forEach(el=>{const k=el.dataset.key; if(el.type==='checkbox')el.checked=preserved[k]==='1';else el.value=preserved[k]??''});
  renderPersonas(); updateProvider();
  $('#publicationHashtagsHint').textContent=`Пусто — автоматически ${S.publication_hashtags_auto}. До 8 тегов через пробел или запятую, 1–64 буквы/цифры/_, минимум одна буква. Снять флажок — отключить.`;
}

function collectFrom(root, group) {
  const data = {};
  $$("[data-key]", root).forEach(el => {
    if (group !== undefined && el.dataset.group !== group) return;
    if (group === undefined && el.dataset.group) return;
    data[el.dataset.key] = el.type === "checkbox" ? (el.checked ? "1" : "0") : el.value;
  });
  return data;
}
async function save(root, group, btn) {
  await act(btn, async () => {
    await api("POST", "/api/settings", collectFrom(root, group));
    if (root.id==='sec-persona') personaDirty=false;
    await refresh(true);
  }, "Сохранено");
}
$$("[data-save]").forEach(b => b.onclick = () => save(document.getElementById(b.dataset.save), undefined, b));
$$("[data-save-group]").forEach(b => b.onclick = () => save(b.closest(".card"), b.dataset.saveGroup, b));

// ---------- обзор: кнопки ----------
$("#reanalyseBtn").onclick = e => act(e.currentTarget, async () => {
  const period = $("#reanalysePeriod").value;
  const preview = await api("GET", `/api/posts/reanalyse?period=${encodeURIComponent(period)}`);
  if (!confirm(`Вернуть на анализ ${preview.count} сохранённых оригиналов?\nСам сброс не расходует токены и не запускает сбор. Следующий сбор использует токены ИИ. Предложения, статусы и антиповтор по заголовкам сохраняются; повтор отклонённых новостей не гарантируется. Старые данные не восстанавливаются.\nПосле сброса нажми «Собрать сейчас» вручную.`)) return;
  const result = await api("POST", "/api/posts/reanalyse", {period});
  toast(`Возвращено на анализ: ${result.affected}. Нажми «Собрать сейчас» вручную.`);
  await refresh();
});
$("#resetTestDataBtn").onclick = e => act(e.currentTarget, async () => {
  const preview = await api("GET", "/api/test-data/reset");
  const c = preview.counts;
  const confirmation = prompt(`Удалить ВСЕ локальные тестовые данные без отмены?\nПосты: ${c.posts}; предложения: ${c.proposals}; журнал: ${c.events}; отчёты расходов: ${c.collection_cycles}.\nИсчезнут локальная история опубликованных/отклонённых предложений, антиповтор и отчёты расходов. Настройки, каналы, Telegram/ИИ-ключи и сессии сохранятся. Сообщения Telegram и файлы сервера не удаляются.\nСбор не запускается. Следующий ручной сбор использует токены ИИ и загрузит только настроенный начальный период, не всю историю Telegram. Перед сбросом обязательно поставь расписание на паузу и дождись завершения сбора.\nВведите СБРОС для подтверждения:`);
  if (confirmation !== "СБРОС") return;
  const result = await api("POST", "/api/test-data/reset", {confirmation});
  const d = result.deleted;
  toast(`Удалено: постов ${d.posts}, предложений ${d.proposals}, событий ${d.events}, отчётов расходов ${d.collection_cycles}.`);
  await refresh();
});
$("#runBtn").onclick = e => act(e.currentTarget, async () => { await api("POST", "/api/run"); await refresh(); }, "Сбор запущен — новости придут боту и сюда");
$("#pauseBtn").onclick = e => act(e.currentTarget, async () => {
  await api("POST", "/api/settings", {paused: S.settings.paused === "1" ? "0" : "1"}); await refresh();
});

// ---------- вход читателя ----------
let qrTimer = null, qrGeneration = 0;
function stopQR() { qrGeneration++; clearTimeout(qrTimer); qrTimer = null; $("#qrImage").removeAttribute("src"); }
function phase(p) { if (p !== "QR") stopQR(); ["Phone", "Code", "Pass", "QR"].forEach(x => $("#lp" + x).classList.toggle("hidden", x !== p)); }
async function pollQR(generation) {
  if (generation !== qrGeneration) return;
  try {
    const r = await api("GET", "/api/tg/qr/status");
    if (generation !== qrGeneration) return;
    if (r.phase === "password") { phase("Pass"); $("#tgpass").focus(); return; }
    if (r.phase === "ok") { phase("Phone"); await refresh(true); toast("Аккаунт подключён"); return; }
    if (r.phase !== "waiting") {
      $("#qrImage").removeAttribute("src");
      $("#qrStatus").textContent = r.phase === "expired" ? "QR истёк. Создай новый." : "QR недоступен. Попробуй снова.";
      $("#renewQR").classList.remove("hidden"); return;
    }
    $("#qrStatus").textContent = "Ожидаем сканирование. QR действителен до " + new Date(r.expires * 1000).toLocaleTimeString("ru-RU");
    qrTimer = setTimeout(() => pollQR(generation), 1500);
  } catch (e) { stopQR(); $("#renewQR").classList.remove("hidden"); }
}
async function startQR() {
  stopQR(); const generation = qrGeneration;
  const r = await api("POST", "/api/tg/qr/start");
  if (generation !== qrGeneration) return;
  if (r.phase === "ok") { await refresh(true); return; }
  phase("QR"); $("#renewQR").classList.add("hidden");
  $("#qrImage").src = "/api/tg/qr/image?v=" + Date.now();
  pollQR(generation);
}
$("#startQR").onclick = e => act(e.currentTarget, startQR);
$("#renewQR").onclick = e => act(e.currentTarget, startQR);
$("#sendCode").onclick = e => act(e.currentTarget, async () => {
  const r = await api("POST", "/api/tg/send_code", {phone: $("#phone").value});
  $("#codeWhere").textContent = r.kind === "app" ? "Код отправлен в приложение Telegram — в чат «Telegram» на этом аккаунте."
    : r.kind === "sms" ? "Код отправлен по SMS." : "Код отправлен. Проверь Telegram и SMS.";
  phase("Code"); $("#code").focus();
});
$("#sendCodeBtn").onclick = e => act(e.currentTarget, async () => {
  const r = await api("POST", "/api/tg/code", {code: $("#code").value});
  if (r.result === "password") { phase("Pass"); $("#tgpass").focus(); return; }
  phase("Phone"); $("#code").value = ""; await refresh(true); toast("Аккаунт подключён");
});
$("#sendPass").onclick = e => act(e.currentTarget, async () => {
  await api("POST", "/api/tg/password", {password: $("#tgpass").value});
  $("#tgpass").value = ""; phase("Phone"); await refresh(true); toast("Аккаунт подключён");
});
$$("[data-cancel]").forEach(b => b.onclick = async () => { await api("POST", "/api/tg/cancel").catch(() => {}); phase("Phone"); });
$("#readerLogout").onclick = e => { if (confirm("Отключить аккаунт-читатель? Сбор новостей остановится.")) act(e.currentTarget, async () => { await api("POST", "/api/tg/logout"); await refresh(true); }, "Аккаунт отключён"); };
$("#botRestart").onclick = e => act(e.currentTarget, async () => { const r = await api("POST", "/api/bot/restart"); await refresh(); return r; }, r => "Бот: " + r.status);
$("#saveTarget").onclick = e => act(e.currentTarget, async () => {
  await api("POST", "/api/settings", collectFrom($("#sec-target")));
  await refresh(true);
  const r = await api("POST", "/api/bot/check_target");
  return r;
}, r => `Канал «${r.title}» проверен — бот может публиковать`);

// ---------- нейросеть ----------
function updateProvider() {
  const sel = $("#provider"), cur = sel.value || S.settings.llm_provider;
  sel.innerHTML = Object.entries(S.providers).map(([k, p]) => `<option value="${k}">${esc(p.name)}</option>`).join("");
  sel.value = cur;
  const p = S.providers[cur] || {};
  $("#baseUrlHint").textContent = p.base_url ? `По умолчанию: ${p.base_url}. Обычно менять не нужно.` : "Адрес OpenAI-совместимого API, обычно заканчивается на /v1.";
  $("#keysLink").innerHTML = p.keys_url ? `Где взять: <a href="${p.keys_url}" target="_blank" rel="noopener">${esc(p.keys_url)}</a>` : "";
}
$("#provider").onchange = updateProvider;
$("#loadModels").onclick = e => act(e.currentTarget, async () => {
  const r = await api("GET", "/api/llm/models");
  $("#modelList").innerHTML = r.models.map(m => `<option value="${esc(m)}">`).join("");
  return r;
}, r => `Загружено моделей: ${r.models.length} — начни вводить название в поле «Модель»`);
$("#testLlm").onclick = e => act(e.currentTarget, async () => {
  const r = await api("POST", "/api/llm/test"); await refresh(); return r;
}, r => `Нейросеть ответила: «${r.reply.slice(0, 60)}»`);

// ---------- персоны и промпт ----------
function updatePersonaPreview() {
  const cur = $("#personaInput").value || S.settings.persona;
  const custom = $('[data-key="persona_custom"]').value.trim();
  const text = cur === "custom" ? (custom || S.personas.editor.text) : (S.personas[cur] || S.personas.editor).text;
  $("#personaPreview").textContent = text;
}
$('[data-key="persona_custom"]').addEventListener("input", updatePersonaPreview);
function renderPersonas() {
  const cur = $("#personaInput").value || S.settings.persona;
  $("#personas").innerHTML = Object.entries(S.personas).map(([k, p]) =>
    `<div class="persona ${k === cur ? "on" : ""}" data-p="${k}" role="button" tabindex="0"><b>${esc(p.name)}</b><span>${esc(p.desc)}</span></div>`).join("");
  $$("#personas .persona").forEach(el => el.onclick = () => { $("#personaInput").value = el.dataset.p; renderPersonas(); });
  $("#customBox").classList.toggle("hidden", cur !== "custom");
  updatePersonaPreview();
  $("#placeholders").innerHTML = Object.entries(S.placeholders).map(([k, v]) =>
    `<button class="chip" type="button" data-ins="${k}" title="${esc(v)}">${k} · ${esc(v)}</button>`).join("");
  $$("#placeholders [data-ins]").forEach(b => b.onclick = () => {
    const ta = $("#promptText"), s = ta.selectionStart, v = ta.value;
    ta.value = v.slice(0, s) + b.dataset.ins + v.slice(ta.selectionEnd); ta.focus();
    ta.selectionStart = ta.selectionEnd = s + b.dataset.ins.length;
  });
}
$("#resetPrompt").onclick = () => { if (confirm("Заменить текст промпта на стандартный? Не забудь сохранить.")) $("#promptText").value = S.default_prompt; };
$("#previewPrompt").onclick = e => act(e.currentTarget, async () => {
  const r = await api("GET", "/api/prompt/preview");
  const pre = $("#promptPreview"); pre.textContent = r.system; pre.classList.remove("hidden");
});

// ---------- расписание ----------
$$("select.hours").forEach(s => s.innerHTML = `<option value="">—</option>` +
  Array.from({length: 24}, (_, h) => `<option value="${h}">${String(h).padStart(2, "0")}:00</option>`).join(""));
$("#intervalChips").innerHTML = [[30, "30 мин"], [60, "1 ч"], [120, "2 ч"], [180, "3 ч"], [360, "6 ч"], [720, "12 ч"], [1440, "сутки"]]
  .map(([v, l]) => `<button class="chip" type="button" data-v="${v}">${l}</button>`).join("");
$$("#intervalChips .chip").forEach(c => c.onclick = () => { $("#interval").value = c.dataset.v; });

// ---------- каналы ----------
async function loadChannels() {
  const list = await api("GET", "/api/channels").catch(e => { if(e instanceof StaleWorkspace)throw e; toast(e.message, "err"); return []; });
  $("#channelList").innerHTML = list.length ? list.map(c => {
    const name = /^-?\d+$/.test(c.username) ? c.username : "@" + c.username;
    const st = c.last_error ? `<span class="err-t">ошибка: ${esc(c.last_error)}</span>`
      : c.last_fetch ? `собран ${fmt(c.last_fetch)} · постов ${c.posts_total}` : "ещё не собирался";
    return `<div class="item"><div class="head"><b>${esc(c.title || name)}</b><span>${esc(name)}</span><span class="sp"></span>
      <label class="switch"><input type="checkbox" data-tog="${esc(c.username)}" ${c.enabled ? "checked" : ""}>вкл</label>
      <button class="b danger" data-del="${esc(c.username)}" style="padding:4px 10px">Удалить</button></div>
      <div class="links">${st}</div></div>`;
  }).join("") : `<div class="empty">Каналов пока нет</div>`;
  $$("[data-tog]").forEach(el => el.onchange = () => act(null, () => api("POST", `/api/channels/${encodeURIComponent(el.dataset.tog)}/toggle`, {enabled: el.checked}), el.checked ? "Включён" : "Выключен"));
  $$("[data-del]").forEach(b => b.onclick = () => { if (confirm("Удалить канал из источников?")) act(b, async () => { await api("DELETE", `/api/channels/${encodeURIComponent(b.dataset.del)}`); await loadChannels(); await refresh(); }, "Удалён"); });
}
$("#addChannels").onclick = e => act(e.currentTarget, async () => {
  const r = await api("POST", "/api/channels", {items: $("#newChannels").value});
  $("#addResult").innerHTML = (r.ok.length ? `<p class="note tip">✅ Добавлены:<br>${r.ok.map(esc).join("<br>")}</p>` : "") +
    (r.bad.length ? `<p class="note warn">⚠️ Не добавлены:<br>${r.bad.map(esc).join("<br>")}</p>` : "");
  if (r.ok.length) $("#newChannels").value = "";
  await loadChannels(); await refresh();
});

// ---------- предложения ----------
const ST_NAMES = {new: ["ждёт", "warn"], queued: ["в очереди", "warn"], published: ["опубликовано", "ok"], skipped: ["пропущено", ""]};
async function loadProposals() {
  const list = await api("GET", "/api/proposals?status=" + propStatus).catch(e => { if(e instanceof StaleWorkspace)throw e; toast(e.message, "err"); return []; });
  $("#propList").innerHTML = list.length ? list.map(p => {
    const [stName, stCls] = ST_NAMES[p.status] || [p.status, ""];
    const open = p.status === "new" || p.status === "queued";
    const links = p.links.length ? `<div class="links">Источники: ${p.links.map((l, i) => `<a href="${esc(l)}" target="_blank" rel="noopener">[${i + 1}]</a>`).join(" ")}</div>` : "";
    const img = p.media ? `<img class="thumb" loading="lazy" src="/api/proposals/${p.id}/media?workspace_id=${workspaceID}" alt="Фото к новости" onerror="this.remove()">` : "";
    const notes = p.notes.length ? `<div class="links">${p.notes.map(esc).join("<br>")}</div>` : "";
    const btns = p.buttons.map(row => row.map(b => {
      const cls = b.data.startsWith("pub:") ? "b pri" : b.data.startsWith("skip:") ? "b danger" : "b";
      return `<button class="${cls}" data-btn="${esc(b.data)}" data-id="${p.id}">${esc(b.text)}</button>`;
    }).join("")).join("");
    const body = open
      ? `${img}<textarea data-text="${p.id}">${esc(p.text)}</textarea>${links}${notes}
         <div class="actions">${btns}<button class="b" data-save-text="${p.id}">Сохранить текст</button></div>`
      : `${img}<pre>${esc(p.text)}</pre>${links}`;
    const badge = p.kind === "breaking" ? `<span class="pill err">🔥 срочно</span>` : "";
    const edited = p.edited ? `<span title="Текст правили вручную">✏️</span>` : "";
    return `<div class="item"><div class="head"><b>#${p.id}</b><span>${fmt(p.created)} · target: ${esc(p.target_channel || 'не задан')}</span>${edited}<span class="sp"></span>${badge}<span class="pill ${stCls}">${stName}</span></div>${body}</div>`;
  }).join("") : `<div class="empty">${propStatus === "new" ? "Новых предложений нет. Нажми «Собрать сейчас» на вкладке «Обзор»." : "Пусто"}</div>`;

  $$("#propList [data-btn]").forEach(b => b.onclick = () => {
    const data = b.dataset.btn, ta = $(`[data-text="${b.dataset.id}"]`);
    act(b, async () => {
      const publishing = /^pub:|:now:/.test(data);
      if (publishing && ta && ta.value !== ta.defaultValue) await api("POST", `/api/proposals/${b.dataset.id}/text`, {text: ta.value});
      const r = await api("POST", "/api/button", {data});
      if (data.startsWith("rw:")) { ta.value = r.text; ta.defaultValue = r.text; }
      else { await loadProposals(); await refresh(); }
      return r;
    }, r => r.message);
  });
  $$("#propList [data-save-text]").forEach(b => b.onclick = () => {
    const ta = $(`[data-text="${b.dataset.saveText}"]`);
    act(b, async () => { await api("POST", `/api/proposals/${b.dataset.saveText}/text`, {text: ta.value}); ta.defaultValue = ta.value; }, "Текст сохранён");
  });
}
$$("#propFilter .chip").forEach(c => c.onclick = () => {
  propStatus = c.dataset.st; $$("#propFilter .chip").forEach(x => x.classList.toggle("on", x === c)); loadProposals();
});

// ---------- модули ----------
function fieldHtml(m, f) {
  const id = `mf-${m.id}-${f.key}`, help = f.help ? `<small>${esc(f.help)}</small>` : "";
  if (f.type === "bool")
    return `<label class="check"><input type="checkbox" id="${id}" data-mkey="${f.key}" ${f.value === "1" ? "checked" : ""}><span>${esc(f.label)}${help}</span></label>`;
  let input;
  if (f.type === "select")
    input = `<select id="${id}" data-mkey="${f.key}">${f.options.map(([v, l]) => `<option value="${esc(v)}" ${v === f.value ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
  else if (f.type === "textarea")
    input = `<textarea id="${id}" data-mkey="${f.key}">${esc(f.value)}</textarea>`;
  else if (f.type === "int")
    input = `<input type="number" id="${id}" data-mkey="${f.key}" value="${esc(f.value)}" min="${f.min}" max="${f.max}">`;
  else
    input = `<input type="text" id="${id}" data-mkey="${f.key}" value="${esc(f.value)}">`;
  return `<label class="f"><span>${esc(f.label)}</span>${input}${help}</label>`;
}

const MODULE_INFO = {
  queue: i => {
    const next = i.next_slot ? `Ближайший слот: ${fmt(i.next_slot)}` : "Слоты не заданы";
    const rows = i.items.length ? i.items.map((q, n) => `<div class="ev"><time>№${n + 1}</time><span><b>#${q.id}</b> ${esc(q.title)}<br><small style="color:var(--muted)">выйдет ${esc(q.eta)}</small></span></div>`).join("")
      : `<p class="hint">Очередь пуста.</p>`;
    return `<p>${next} · в очереди: ${i.items.length}</p>${rows}`;
  },
  learning: i => {
    const ex = i.examples.length ? i.examples.map(e => `<div class="ev"><time>#${e.id}</time><span>${esc(e.title)}${e.edited ? " ✏️" : ""}</span></div>`).join("")
      : `<p class="hint">Примеров пока нет — опубликуй несколько новостей, лучше с правками.</p>`;
    return `<div class="stats"><div class="stat"><b>${i.published}</b><span>опубликовано</span></div><div class="stat"><b>${i.edited}</b><span>с правками</span></div><div class="stat"><b>${i.skipped}</b><span>пропущено</span></div></div>
      <h3 style="margin-top:12px">Примеры для нейросети сейчас</h3>${ex}
      <div class="actions"><button class="b danger" data-maction="reset">Сбросить обучение</button></div>
      <p class="hint">${i.since ? "Учитывается всё начиная с " + fmt(i.since) + "." : "Учитывается вся история."} Сброс полезен, если ты сильно поменял стиль канала.</p>`;
  },
  breaking: i => {
    const rows = i.items.length ? i.items.map(x => `<div class="ev"><time>${fmt(x.created)}</time><span>#${x.id} ${esc(x.title)} · ${esc((ST_NAMES[x.status] || [x.status])[0])}</span></div>`).join("")
      : `<p class="hint">Срочных новостей пока не было.</p>`;
    return `<p>Последняя проверка: ${i.last_check ? fmt(i.last_check) : "ещё не было"}</p>${rows}
      <div class="actions"><button class="b" data-maction="check">Проверить сейчас</button></div>`;
  },
  media: i => `<p class="hint">Картинок в кэше: ${i.cached}. Кэш старше 14 дней чистится сам.</p>`,
};

function renderModules() {
  $("#modulesList").innerHTML = S.modules.map(m => `
    <div class="card module" data-mod="${m.id}">
      <div style="display:flex;align-items:center;gap:10px">
        <h2 style="flex:1">${esc(m.name)}</h2>
        <label class="toggle"><input type="checkbox" data-menabled ${m.enabled ? "checked" : ""}><span></span></label>
      </div>
      <p class="sub" style="margin-top:4px">${esc(m.description)}</p>
      <div class="${m.enabled ? "" : "hidden"}">
        ${m.fields.map(f => fieldHtml(m, f)).join("")}
        ${m.fields.length ? `<div class="actions"><button class="b pri" data-msave>Сохранить</button></div>` : ""}
        ${m.info && MODULE_INFO[m.id] ? `<div class="modinfo">${MODULE_INFO[m.id](m.info)}</div>` : ""}
      </div>
    </div>`).join("");

  $$("#modulesList .module").forEach(card => {
    const id = card.dataset.mod;
    $("[data-menabled]", card).onchange = e => act(null, async () => {
      await api("POST", `/api/modules/${id}`, {enabled: e.target.checked ? "1" : "0"});
      await refresh(); renderModules();
    }, e.target.checked ? "Модуль включён" : "Модуль выключен");
    const save = $("[data-msave]", card);
    if (save) save.onclick = () => act(save, async () => {
      const data = {};
      $$("[data-mkey]", card).forEach(el => data[el.dataset.mkey] = el.type === "checkbox" ? (el.checked ? "1" : "0") : el.value);
      await api("POST", `/api/modules/${id}`, data);
      await refresh(); renderModules();
    }, "Сохранено");
    $$("[data-maction]", card).forEach(b => b.onclick = () => {
      if (b.dataset.maction === "reset" && !confirm("Сбросить обучение? Старые публикации перестанут использоваться как примеры.")) return;
      act(b, async () => { const r = await api("POST", `/api/modules/${id}/action`, {action: b.dataset.maction}); await refresh(); renderModules(); return r; }, r => r.message);
    });
  });
}

// ---------- журнал ----------
async function loadEvents() {
  const list = await api("GET", "/api/events").catch(e=>{if(e instanceof StaleWorkspace)throw e;return []});
  $("#events").innerHTML = list.length ? list.map(e => `<div class="ev ${e.level}"><time>${fmt(e.ts)}</time><span>${e.level === "error" ? "⚠️ " : ""}${esc(e.text)}</span></div>`).join("")
    : `<div class="empty">Пока пусто</div>`;
}
$("#reloadLog").onclick = loadEvents;

// ---------- старт ----------
// Global connection form and scoped model/target are physically separate.
const modelCard=document.createElement('div'); modelCard.id='sec-model';modelCard.className='card';
modelCard.innerHTML='<h2>Модель и тарифы этого пространства</h2><p class="hint">Провайдер и ключ общие, в «Подключения». Модель и тарифы независимы.</p>';
const modelLabel=$('[data-key="llm_model"]').closest('label');
const sharedCard=$('#sec-llm');
let moving=false;
Array.from(sharedCard.children).forEach(el=>{if(el===modelLabel)moving=true;if(moving)modelCard.appendChild(el)});
sharedCard.insertAdjacentHTML('beforeend','<div class="actions"><button class="b pri" id="saveSharedLLM">Сохранить общие настройки ИИ</button></div>');
$('#saveSharedLLM').onclick=e=>save(sharedCard,undefined,e.currentTarget);
$('#tab-ai').prepend(modelCard);$('#tab-publish').prepend($('#sec-target'));
refresh(true).catch(() => {});
setInterval(() => { if (S && !document.hidden && !$("#app").classList.contains("hidden")) refresh().catch(() => {}); }, 8000);
