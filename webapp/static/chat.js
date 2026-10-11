let lastMsgId = 0;
let lastFileId = 0;

const seen = new Set(); // чтобы не дублировать элементы в ленте

let CHAT_TOKEN = window.CHAT_TOKEN;
let keyboardVisible = false;
let isScrolling = false;

// Для редактирования сообщений
let editingMessageId = null;
let originalEditText = null;
let paymentPanelLoaded = false;
let callInProgress = false;
let chatRole = "";

// single-flight refresh, чтобы параллельные запросы не делали несколько refresh одновременно
let refreshInFlight = null;

// интервалы, чтобы можно было остановить при "смерти" токена
let pollIntervalId = null;
let refreshIntervalId = null;

function setAppHeight() {
  // fallback для браузеров без dvh
  document.documentElement.style.setProperty("--app-height", `${window.innerHeight}px`);
}

function setFooterHeight() {
  const composer = document.querySelector(".composer");
  const hint = document.getElementById("inputHint");
  const fileHint = document.getElementById("fileHint");

  const hComposer = composer ? composer.getBoundingClientRect().height : 64;
  const hHint = (hint && hint.style.display !== "none") ? hint.getBoundingClientRect().height : 0;
  const hFile = (fileHint && fileHint.style.display !== "none") ? fileHint.getBoundingClientRect().height : 0;

  const footerH = Math.ceil(hComposer + hHint + hFile);

  document.documentElement.style.setProperty("--composer-h", `${Math.ceil(hComposer)}px`);
  document.documentElement.style.setProperty("--footer-h", `${footerH}px`);
}

function isNearBottom(el, px = 80) {
  return (el.scrollHeight - el.scrollTop - el.clientHeight) < px;
}

function scrollToBottom(el) {
  if (isScrolling) return;

  isScrolling = true;
  el.scrollTop = el.scrollHeight;

  requestAnimationFrame(() => {
    el.scrollTop = el.scrollHeight;
    setTimeout(() => {
      el.scrollTop = el.scrollHeight;
      isScrolling = false;
    }, 50);
  });
}

function smoothScrollToBottom(el) {
  if (isScrolling) return;

  isScrolling = true;
  const start = el.scrollTop;
  const end = el.scrollHeight - el.clientHeight;
  const distance = end - start;
  const duration = Math.min(300, Math.abs(distance) * 2);
  let startTime = null;

  function animation(currentTime) {
    if (startTime === null) startTime = currentTime;
    const timeElapsed = currentTime - startTime;
    const progress = Math.min(timeElapsed / duration, 1);

    const easeProgress = 1 - Math.pow(1 - progress, 3);
    el.scrollTop = start + (distance * easeProgress);

    if (timeElapsed < duration) {
      requestAnimationFrame(animation);
    } else {
      isScrolling = false;
    }
  }

  requestAnimationFrame(animation);
}

function handleAuthDead() {
  // токен не смогли обновить — останавливаем опрос, показываем подсказку
  if (pollIntervalId) clearInterval(pollIntervalId);
  if (refreshIntervalId) clearInterval(refreshIntervalId);
  pollIntervalId = null;
  refreshIntervalId = null;

  setComposerEnabled(false, "Ссылка истекла, пожалуйста, обновите кнопку в боте: выйдите в меню и войдите обратно");
}

async function refreshToken() {
  if (refreshInFlight) return refreshInFlight;

  refreshInFlight = (async () => {
    try {
      const fd = new FormData();
      fd.append("token", CHAT_TOKEN);

      const r = await fetch("/api/auth/refresh", { method: "POST", body: fd });
      if (!r.ok) return false;

      const data = await r.json().catch(() => ({}));
      if (!data.token) return false;

      CHAT_TOKEN = data.token;
      window.CHAT_TOKEN = CHAT_TOKEN;
      return true;
    } catch (e) {
      console.error("refreshToken error", e);
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();

  return refreshInFlight;
}

async function apiFetch(url, opts) {
  // добавляем token в URL
  const u = new URL(url, window.location.origin);
  u.searchParams.set("token", CHAT_TOKEN);

  let r = await fetch(u.toString(), opts);

  // если истёк/отозван — refresh и повтор
  if (r.status === 401) {
    const ok = await refreshToken();
    if (!ok) {
      handleAuthDead();
      return r;
    }
    const u2 = new URL(url, window.location.origin);
    u2.searchParams.set("token", CHAT_TOKEN);
    r = await fetch(u2.toString(), opts);
  }

  if (r.status === 403) {
    const ok = await refreshToken();
    if (ok) {
      const u3 = new URL(url, window.location.origin);
      u3.searchParams.set("token", CHAT_TOKEN);
      r = await fetch(u3.toString(), opts);
    }
  }
  return r;
}

function pendingHint(data, canWrite, stRaw) {
  // Use explicit null check to avoid Number(null)===0 false-positives
  const leftRaw = data?.pending_client_messages_left;
  const limitRaw = data?.pending_client_messages_limit;
  const hasCounters = leftRaw != null && limitRaw != null;
  const left = Number(leftRaw);
  const limit = Number(limitRaw);
  // Prefer role from server (reflects dual-role upgrade), fall back to page-load sender
  const roleFromApi = String(data?.role || "").toLowerCase();
  const isClient = roleFromApi ? (roleFromApi === "client") : (Number(window.MY_SENDER) === 0);

  if (stRaw === "pending" && isClient) {
    if (hasCounters && Number.isFinite(left) && Number.isFinite(limit)) {
      if (left > 0) return `Заявка ещё не принята. Можно отправить ещё ${left} из ${limit} сообщений.`;
      return "Лимит сообщений до принятия заявки исчерпан. Ожидайте принятия специалистом.";
    }
    if (!canWrite) return "Заявка ещё не принята. Ожидайте принятия специалистом.";
  }

  if (!canWrite) {
    if (stRaw === "closed") return "Чат закрыт — писать нельзя.";
    return "Можно написать после принятия заявки";
  }

  return "";
}

function statusRu(s) {
  const v = String(s || "").toUpperCase();
  const map = {
    "PENDING": "Ожидает принятия",
    "ACTIVE": "В работе",
    "TEST": "Тест",
    "CLOSED": "Завершено",
    "DONE": "Завершено",
    "CANCELLED": "Отменено",
    "CONSULTATION": "Консультация"
  };
  return map[v] || (s || "—");
}

function setComposerEnabled(enabled, hintText = "") {
  const textEl = document.getElementById("text");
  const sendBtn = document.getElementById("send");
  const attachBtn = document.getElementById("attach");
  const fileEl = document.getElementById("file");
  const hintEl = document.getElementById("inputHint");

  if (textEl) {
    textEl.disabled = !enabled;
    if (!enabled && hintText) {
      textEl.placeholder = hintText;
    } else {
      textEl.placeholder = "Сообщение...";
    }
  }
  if (sendBtn) sendBtn.disabled = !enabled;
  if (attachBtn) attachBtn.disabled = !enabled;
  if (fileEl) fileEl.disabled = !enabled;

  if (hintEl) {
    if (!enabled && hintText) {
      hintEl.textContent = hintText;
      hintEl.style.display = "block";
    } else {
      hintEl.textContent = "";
      hintEl.style.display = "none";
    }
  }

  setFooterHeight();
}

function setCallButtonState({ visible, waiting }) {
  const callBtn = document.getElementById("callBtn");
  if (!callBtn) return;

  callBtn.style.display = visible ? "inline-block" : "none";
  callBtn.disabled = !visible || waiting;
  callBtn.classList.toggle("waiting", Boolean(waiting));
  callBtn.textContent = waiting ? "Ожидайте" : "📞 Позвонить";
  setFooterHeight();
}

async function loadChatMeta() {
  try {
    const r = await apiFetch(`/api/chats/${window.CHAT_ID}`);
    if (!r.ok) {
      if (r.status === 403) {
        setComposerEnabled(false, "Доступ к чату временно ограничен. Обновите кнопку «Ответить» в боте.");
      }
      return;
    }

    const data = await r.json().catch(() => ({}));
    const chat = data.chat || {};
    chatRole = String(data.role || "").trim().toLowerCase();
    syncPayButtonVisibility();

    const canWrite = Boolean(data.can_write);
    const canCall = Boolean(data.can_call);
    callInProgress = Boolean(data.call_in_progress);
    window.CAN_WRITE = canWrite;

    const reqType = chat.request_type || "—";
    const cat = chat.category || "—";
    const st = statusRu(chat.status);

    const titleEl = document.getElementById("chatTitle");
    const metaEl = document.getElementById("chatMeta");

    const isAgent = Number(window.MY_SENDER) === 1;

    if (isAgent) {
      const name = chat.client_name || `Клиент ${chat.client_user_id || "—"}`;
      titleEl.textContent = name;
    } else {
      titleEl.textContent = "Обращение";
    }

    const lines = [`Тип: ${reqType}`, `Категория: ${cat}`, `Статус: ${st}`];
    if (isAgent) {
      const hasInClientBase = Boolean(chat.client_we_added);
      lines.push(hasInClientBase ? "Наш клиент" : "Неизвестный клиент");
      if (chatRole === "director") {
        lines.push(`Телефон: ${chat.client_phone || "—"}`);
        lines.push(`ИНН: ${chat.client_inn || "—"}`);
      } else {
        lines.push(chat.client_has_phone ? "Телефон: указан" : "Телефон: не указан");
        lines.push(chat.client_has_inn ? "ИНН: указан" : "ИНН: не указан");
      }
    }
    metaEl.textContent = lines.join("\n");

    const stRaw = String(chat.status || "").toLowerCase();
    const hint = pendingHint(data, canWrite, stRaw);

    if (canWrite) {
      setComposerEnabled(true);
      if (hint) {
        const hintEl = document.getElementById("inputHint");
        if (hintEl) {
          hintEl.textContent = hint;
          hintEl.style.display = "block";
        }
      }
    } else {
      setComposerEnabled(false, hint || "Можно написать после принятия заявки");
    }

    setCallButtonState({
      visible: canCall,
      waiting: callInProgress,
    });
  } catch (e) {
    console.error("loadChatMeta error", e);
  }
}

async function loadClientPaymentPanel() {
  if (Number(window.MY_SENDER) !== 0) return;
  try {
    const r = await apiFetch(`/api/chats/${window.CHAT_ID}/payment-current`);
    const data = await r.json().catch(() => ({}));
    const p = data?.payment;
    let panel = document.getElementById("paymentPanel");
    const paymentStatus = String(p?.status || "").toUpperCase();
    const isOpenPayment = paymentStatus === "NEW" || paymentStatus === "BONUS_RESERVED";

    if (!p || !isOpenPayment) {
      if (panel) panel.remove();
      return;
    }

    if (!panel) {
      panel = document.createElement("div");
      panel.id = "paymentPanel";
      panel.style.padding = "10px 12px";
      panel.style.borderBottom = "1px solid #e6e6e6";
      panel.style.background = "#fffdf3";
      const topbar = document.querySelector(".topbar");
      if (topbar && topbar.parentNode) {
        topbar.parentNode.insertBefore(panel, topbar.nextSibling);
      }
    }

    const preview = p.bonus_preview || {};
    panel.innerHTML = `
      <div style="font-size:13px;line-height:1.35;">
        <b>💳 Счёт к оплате:</b> ${p.amount_rub} ₽<br>
        Списать баллы (авто): до ${preview.max_discount_rub || 0} ₽<br>
        К оплате после списания: ${preview.cash_amount_rub || p.amount_rub} ₽
      </div>
      <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;">
        <button id="payNoBonusBtn" type="button">Оплатить без баллов</button>
        <button id="payWithBonusBtn" type="button">Списать баллы и оплатить</button>
      </div>
    `;

    document.getElementById("payNoBonusBtn")?.addEventListener("click", () => {
      if (p.pay_url) window.open(String(p.pay_url), "_blank", "noopener");
    });

    document.getElementById("payWithBonusBtn")?.addEventListener("click", async () => {
      const confirmed = confirm("Списать баллы автоматически до лимита и обновить ссылку оплаты?");
      if (!confirmed) return;
      const rr = await apiFetch(
        `/api/chats/${window.CHAT_ID}/payment/${encodeURIComponent(p.payment_id)}/apply-bonus`,
        { method: "POST" }
      );
      const payload = await rr.json().catch(() => ({}));
      if (!rr.ok) {
        alert(payload?.detail || `Ошибка применения баллов (HTTP ${rr.status})`);
        return;
      }
      if (payload?.pay_url) window.open(String(payload.pay_url), "_blank", "noopener");
      await loadClientPaymentPanel();
      await poll();
    });
  } catch (e) {
    console.error("loadClientPaymentPanel error", e);
  }
}

function getTelegramUserIdOrNull() {
  try {
    const tg = window.Telegram && window.Telegram.WebApp;
    const uid = tg?.initDataUnsafe?.user?.id;
    return uid ? Number(uid) : null;
  } catch (e) {
    return null;
  }
}

function getMaxUserIdOrNull() {
  try {
    const wa = window.WebApp;
    const uid = wa?.initDataUnsafe?.user?.id || wa?.initData?.user?.id || wa?.user?.id;
    return uid ? Number(uid) : null;
  } catch (e) {
    return null;
  }
}

function getActorIdForPaymentOrNull() {
  const tgId = getTelegramUserIdOrNull();
  if (tgId && Number.isFinite(tgId)) return tgId;

  const maxId = getMaxUserIdOrNull();
  if (maxId && Number.isFinite(maxId)) return maxId;

  return null;
}

async function createPaymentLink(amountRub, useBonus = false) {
  const actorId = getActorIdForPaymentOrNull();
  const hasActorId = !!actorId && Number.isFinite(actorId);
  const payloadRole = chatRole === "director" ? "director" : "agent";
  if (!hasActorId) {
    console.warn("createPaymentLink: actor_id not detected in WebApp bridge, fallback to token actor_id");
  }

  const r = await apiFetch(`/api/chats/${window.CHAT_ID}/payment-link`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      actor_id: hasActorId ? actorId : null,
      role: payloadRole,
      amount: Number(amountRub),
      use_bonus: Boolean(useBonus)
    })
  });

  const responseText = await r.text().catch(() => "");
  let data = {};
  if (responseText) {
    try {
      data = JSON.parse(responseText);
    } catch (e) {
      data = {};
    }
  }

  if (!r.ok) {
    const debugId = r.headers.get("x-request-id") || r.headers.get("x-correlation-id") || "-";
    const detail = String(data?.detail || "").trim();
    const raw = (responseText || "").trim();
    const fallback = raw ? raw.slice(0, 700) : "";

    console.error("createPaymentLink failed", {
      platform: window.PLATFORM || "tg",
      chat_id: window.CHAT_ID,
      actor_id: actorId,
      amount: Number(amountRub),
      status: r.status,
      status_text: r.statusText,
      request_id: debugId,
      detail,
      raw_response: raw,
    });

    const msg = detail || fallback || `Ошибка создания оплаты (HTTP ${r.status})`;
    alert(msg);
    return;
  }

  await poll();
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function parseTime(s) {
  const d = new Date(s);
  return isNaN(d.getTime()) ? 0 : d.getTime();
}

function appendTextWithBreaks(parent, chunk) {
  const safeText = String(chunk)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\n/g, "<br>");

  parent.innerHTML += safeText;
}


function renderTextMessage(m) {
  const side = (Number(m.sender) === Number(window.MY_SENDER)) ? "me" : "them";
  const wrap = el("div", `msg ${side}`);
  const bubble = el("div", "bubble");

  const text = String(m.text || "");
  const urlRe = /https?:\/\/[^\s]+/g;

  let last = 0;
  let match;

  while ((match = urlRe.exec(text)) !== null) {
    const start = match.index;
    const end = start + match[0].length;

    if (start > last) appendTextWithBreaks(bubble, text.slice(last, start));

    const url = match[0];
    const a = document.createElement("a");
    a.href = url;
    a.textContent = url;
    a.target = "_blank";
    a.rel = "noopener";
    a.className = "msg-link";
    bubble.appendChild(a);

    last = end;
  }

  if (last < text.length) appendTextWithBreaks(bubble, text.slice(last));

  // Добавляем индикатор "отредактировано"
  const isEdited = m.is_edited || m.edited_at;
  if (isEdited) {
    const editedMark = el("span", "edited-mark", " (ред.)");
    bubble.appendChild(editedMark);
  }

  // Добавляем кнопку редактирования для своих сообщений
  const isMyMessage = Number(m.sender) === Number(window.MY_SENDER);
  if (isMyMessage) {
    const editBtn = document.createElement("button");
    editBtn.className = "edit-msg-btn";
    editBtn.textContent = "✏️";
    editBtn.title = "Редактировать";
    editBtn.onclick = () => startEditMessage(m.id, m.text);
    bubble.appendChild(editBtn);
  }

  wrap.appendChild(bubble);
  return wrap;
}

// Начало редактирования сообщения
function startEditMessage(messageId, currentText) {
  if (editingMessageId !== null) {
    // Уже редактируем другое сообщение - отменяем
    cancelEdit();
  }

  editingMessageId = messageId;
  originalEditText = currentText;

  const textEl = document.getElementById("text");
  const sendBtn = document.getElementById("send");

  // Сохраняем текущий текст в поле ввода
  textEl.value = currentText;
  textEl.focus();
  autoResizeTextarea();

  // Меняем кнопку "Отправить" на "Сохранить"
  sendBtn.textContent = "💾 Сохранить";
  sendBtn.dataset.editMode = "true";

  // Добавляем кнопку отмены
  let cancelBtn = document.getElementById("cancelEditBtn");
  if (!cancelBtn) {
    cancelBtn = el("button", "cancel-edit-btn", "✖");
    cancelBtn.id = "cancelEditBtn";
    cancelBtn.title = "Отменить редактирование";
    cancelBtn.onclick = cancelEdit;
    textEl.parentNode.insertBefore(cancelBtn, textEl.nextSibling);
  }

  // Показываем подсказку
  showEditHint();
}

// Отмена редактирования
function cancelEdit() {
  editingMessageId = null;
  originalEditText = null;

  const textEl = document.getElementById("text");
  const sendBtn = document.getElementById("send");
  const cancelBtn = document.getElementById("cancelEditBtn");

  textEl.value = "";
  sendBtn.textContent = "Отправить";
  delete sendBtn.dataset.editMode;

  if (cancelBtn) {
    cancelBtn.remove();
  }

  hideEditHint();
}

// Сохранение отредактированного сообщения
async function saveEditedMessage() {
  if (!editingMessageId) return;

  const textEl = document.getElementById("text");
  const newText = textEl.value.trim();

  if (!newText) {
    alert("Сообщение не может быть пустым");
    return;
  }

  if (newText === originalEditText) {
    // Текст не изменился
    cancelEdit();
    return;
  }

  try {
    const r = await apiFetch(`/api/chats/${window.CHAT_ID}/messages/${editingMessageId}`, {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ text: newText })
    });

    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      throw new Error(data?.detail || `edit failed: ${r.status}`);
    }

    // Успешно обновлено
    cancelEdit();
    await poll(); // Обновляем сообщения
  } catch (e) {
    console.error("Edit error", e);
    alert(String(e?.message || "Не удалось отредактировать. Посмотри консоль/логи сервера."));
  }
}

function showEditHint() {
  const hintEl = document.getElementById("inputHint");
  if (hintEl) {
    hintEl.textContent = "Режим редактирования: нажмите Enter для сохранения, Esc для отмены";
    hintEl.style.display = "block";
    hintEl.classList.add("edit-hint");
    setFooterHeight();
  }
}

function hideEditHint() {
  const hintEl = document.getElementById("inputHint");
  if (hintEl) {
    hintEl.textContent = "";
    hintEl.style.display = "none";
    hintEl.classList.remove("edit-hint");
    setFooterHeight();
  }
}

function renderFileItem(f) {
  const side = (Number(f.sender) === Number(window.MY_SENDER)) ? "me" : "them";
  const wrap = el("div", `msg ${side}`);
  const bubble = el("div", "bubble");

  const card = el("div", "file-card");
  const name = f.original_name || "file";
  const url = f.download_url || `/api/files/${f.id}/download`;
  const mime = (f.mime || "").toLowerCase();

  if (mime.startsWith("image/")) {
    const img = document.createElement("img");
    img.className = "file-img";
    img.src = url;
    img.alt = name;
    card.appendChild(img);
  }

  const a = document.createElement("a");
  a.className = "file-link";
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener";
  a.textContent = `📎 ${name}`;
  card.appendChild(a);

  bubble.appendChild(card);
  wrap.appendChild(bubble);
  return wrap;
}
function appendItem(key, node) {
  if (seen.has(key)) return;
  seen.add(key);

  const box = document.getElementById("messages");
  const stick = isNearBottom(box);

  box.appendChild(node);

  // 🧨 Форсируем пересоздание пузыря
  requestAnimationFrame(() => {
    const bubble = node.querySelector('.bubble');
    if (bubble) {
      const clone = bubble.cloneNode(true);     // 🔁 Клонируем bubble
      bubble.replaceWith(clone);                // 🔁 Заменяем оригинал
    }
  });

  if (stick) scrollToBottom(box);
}





async function loadNewMessages() {
  const r = await apiFetch(`/api/chats/${window.CHAT_ID}/messages?after_id=${lastMsgId}&limit=50`);
  const data = await r.json().catch(() => ({}));
  const items = [];

  for (const m of (data.messages || [])) {
    lastMsgId = Math.max(lastMsgId, Number(m.id));
    items.push({
      key: `m:${m.id}`,
      ts: parseTime(m.created_at),
      id: Number(m.id),
      node: renderTextMessage(m)
    });
  }
  return items;
}

async function loadNewFiles() {
  const r = await apiFetch(`/api/chats/${window.CHAT_ID}/files?after_id=${lastFileId}&limit=50`);
  const data = await r.json().catch(() => ({}));
  const items = [];

  for (const f of (data.files || [])) {
    lastFileId = Math.max(lastFileId, Number(f.id));
    items.push({
      key: `f:${f.id}`,
      ts: parseTime(f.created_at),
      id: Number(f.id),
      node: renderFileItem(f)
    });
  }
  return items;
}

async function poll() {
  try {
    await loadChatMeta();
    if (!paymentPanelLoaded || Number(window.MY_SENDER) === 0) {
      await loadClientPaymentPanel();
      paymentPanelLoaded = true;
    }
    const [msgItems, fileItems] = await Promise.all([loadNewMessages(), loadNewFiles()]);
    const all = [...msgItems, ...fileItems];

    all.sort((a, b) => (a.ts - b.ts) || (a.id - b.id));
    for (const it of all) appendItem(it.key, it.node);
  } catch (e) {
    console.error("poll error", e);
  }
}

async function sendTextOnly(text) {
  const r = await apiFetch(`/api/chats/${window.CHAT_ID}/messages`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ sender: window.MY_SENDER, text })
  });

  if (!r.ok) {
    const data = await r.json().catch(() => ({}));
    throw new Error(data?.detail || `send failed: ${r.status}`);
  }

  return await r.json().catch(() => ({}));
}

async function sendWithFile(text, file) {
  const fd = new FormData();
  fd.append("sender", String(window.MY_SENDER));
  fd.append("text", text || "");
  fd.append("file", file);

  const r = await apiFetch(`/api/chats/${window.CHAT_ID}/files`, { method: "POST", body: fd });
  if (!r.ok) {
    const data = await r.json().catch(() => ({}));
    throw new Error(data?.detail || `upload failed: ${r.status}`);
  }

  return await r.json().catch(() => ({}));
}

function setFileHint(file) {
  const hint = document.getElementById("fileHint");
  if (!hint) return;

  if (!file) {
    hint.style.display = "none";
    hint.textContent = "";
    setFooterHeight();
    return;
  }

  hint.style.display = "block";
  hint.textContent = `Выбран файл: ${file.name}`;
  setFooterHeight();
}

function autoResizeTextarea() {
  const ta = document.getElementById("text");
  if (!ta) return;

  const box = document.getElementById("messages");
  const stick = box ? isNearBottom(box, 140) : false;

  ta.style.height = "auto";

  const max = Math.min(180, Math.floor(window.innerHeight * 0.30));
  const next = Math.min(ta.scrollHeight, max);

  ta.style.height = `${next}px`;
  ta.style.overflowY = (ta.scrollHeight > max) ? "auto" : "hidden";

  setFooterHeight();
  if (stick && box) scrollToBottom(box);
}

async function send() {
  // Если в режиме редактирования - сохраняем изменения
  if (editingMessageId !== null) {
    await saveEditedMessage();
    return;
  }

  const inp = document.getElementById("text");
  const fileInput = document.getElementById("file");
  const text = (inp.value || "").trim();
  const file = fileInput.files && fileInput.files[0] ? fileInput.files[0] : null;

  if (!text && !file) return;

  inp.value = "";
  autoResizeTextarea();

  try {
    let postData = null;
    if (file) {
      postData = await sendWithFile(text, file);
      fileInput.value = "";
      setFileHint(null);
    } else {
      postData = await sendTextOnly(text);
    }

    if (postData && Object.prototype.hasOwnProperty.call(postData, "pending_client_messages_left")) {
      await loadChatMeta();
    }
  } catch (e) {
    console.error(e);
    alert(String(e?.message || "Не удалось отправить. Посмотри консоль/логи сервера."));
  }

  await poll();
}

async function startCall() {
  if (callInProgress) return;
  const isAgent = Number(window.MY_SENDER) === 1;
  const target = isAgent ? "клиенту" : "специалисту";
  const confirmed = confirm(`Начать звонок ${target}?`);
  if (!confirmed) return;

  setCallButtonState({ visible: true, waiting: true });

  try {
    const r = await apiFetch(`/api/chats/${window.CHAT_ID}/call/start`, { method: "POST" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      throw new Error(data?.detail || `call failed: ${r.status}`);
    }
    callInProgress = true;
  } catch (e) {
    callInProgress = false;
    await loadChatMeta();
    alert(String(e?.message || "Не удалось запустить звонок."));
  }
}

document.getElementById("send").onclick = send;

document.getElementById("attach").onclick = () => {
  document.getElementById("file").click();
};
document.getElementById("callBtn")?.addEventListener("click", startCall);

document.getElementById("file").addEventListener("change", (e) => {
  const file = e.target.files && e.target.files[0] ? e.target.files[0] : null;
  setFileHint(file);
});

function canShowPayButton() {
  // Кнопка оплаты доступна специалисту и директору.
  if (chatRole) return chatRole === "agent" || chatRole === "director";

  // fallback для старых ответов API без role
  return Number(window.MY_SENDER) === 1;
}

function syncPayButtonVisibility() {
  const payBtn = document.getElementById("payBtn");
  if (!payBtn) return;
  payBtn.style.display = canShowPayButton() ? "inline-block" : "none";
  setFooterHeight();
}

(function initPayButton() {
  const payBtn = document.getElementById("payBtn");
  if (!payBtn) return;

  syncPayButtonVisibility();

  payBtn.addEventListener("click", async () => {
    const raw = prompt("Введите сумму к оплате (в рублях), например 1500:");
    if (!raw) return;

    const amount = Number(String(raw).replace(/[^\d]/g, ""));
    if (!Number.isFinite(amount) || amount <= 0) {
      alert("Сумма должна быть числом больше нуля.");
      return;
    }

    try {
      await createPaymentLink(amount, false);
    } catch (e) {
      console.error(e);
      alert("Не удалось создать ссылку на оплату. Посмотри консоль/логи сервера.");
    }
  });
})();

// Многострочный ввод:
// - мобилка: Enter = новая строка (отправка кнопкой)
// - десктоп: Enter = отправка, Shift+Enter = новая строка
document.getElementById("text").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    const isMobile = Boolean(window.IS_MOBILE);

    if (isMobile) {
      // перенос строки
      return;
    }

    if (!e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  // Esc для отмены редактирования
  if (e.key === "Escape" && editingMessageId !== null) {
    e.preventDefault();
    cancelEdit();
  }
});

document.getElementById("text").addEventListener("input", autoResizeTextarea);

(function mobileViewportFixes() {
  const box = document.getElementById("messages");
  const text = document.getElementById("text");

  setAppHeight();
  setFooterHeight();
  setTimeout(autoResizeTextarea, 0);

  const onResize = () => {
    setAppHeight();
    autoResizeTextarea();
    setFooterHeight();
  };
  window.addEventListener("resize", onResize);

  const vv = window.visualViewport;
  if (vv) {
    const onVVChange = () => {
      autoResizeTextarea();
      setFooterHeight();

      const newKeyboardVisible = vv.height < window.innerHeight * 0.8;

      if (newKeyboardVisible !== keyboardVisible) {
        keyboardVisible = newKeyboardVisible;

        if (keyboardVisible && box) {
          setTimeout(() => smoothScrollToBottom(box), 100);
          setTimeout(() => scrollToBottom(box), 300);
        }
      }

      if (keyboardVisible && box && isNearBottom(box, 150)) {
        setTimeout(() => scrollToBottom(box), 10);
      }
    };

    vv.addEventListener("resize", onVVChange);
    vv.addEventListener("scroll", onVVChange);
  }

  if (text) {
    let focusTimeout;

    const handleFocus = () => {
      if (!box) return;

      clearTimeout(focusTimeout);
      keyboardVisible = true;

      autoResizeTextarea();
      setFooterHeight();

      setTimeout(() => scrollToBottom(box), 10);

      focusTimeout = setTimeout(() => {
        if (keyboardVisible) scrollToBottom(box);
      }, 300);

      focusTimeout = setTimeout(() => {
        if (keyboardVisible) scrollToBottom(box);
      }, 500);
    };

    const handleBlur = () => {
      keyboardVisible = false;
      clearTimeout(focusTimeout);
    };

    text.addEventListener("focus", handleFocus);
    text.addEventListener("blur", handleBlur);
    text.addEventListener("touchstart", handleFocus);
  }

  // Не даём iOS "тянуть страницу" при скролле на краях списка сообщений
  if (box) {
    let touchStartY = 0;

    box.addEventListener("touchstart", (e) => {
      touchStartY = e.touches[0].clientY;
    }, { passive: true });

    box.addEventListener("touchmove", (e) => {
      const touchY = e.touches[0].clientY;
      const diff = touchStartY - touchY;

      const atTop = box.scrollTop <= 0;
      const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 1;

      // prevent bounce только на краях (иначе ломает обычный скролл)
      if ((atTop && diff < 0) || (atBottom && diff > 0)) {
        e.preventDefault();
      }
    }, { passive: false });
  }
})();

// старт
poll();
pollIntervalId = setInterval(poll, 2000);
loadChatMeta();
loadClientPaymentPanel();

// периодический refresh (на всякий случай)
refreshIntervalId = setInterval(() => {
  refreshToken().catch(() => {});
}, 10 * 60 * 1000);

document.addEventListener("DOMContentLoaded", () => {
  const box = document.getElementById("messages");
  if (box) setTimeout(() => scrollToBottom(box), 100);
  setFooterHeight();
  autoResizeTextarea();

  // Инициализируем bridge (Telegram или MAX) — к этому моменту defer-скрипты уже выполнены
  try {
    const bridge = window.WebApp || (window.Telegram && window.Telegram.WebApp);
    if (bridge) {
      if (typeof bridge.ready === "function") bridge.ready();
      if (typeof bridge.expand === "function") bridge.expand();
    }
    const tg = window.Telegram && window.Telegram.WebApp;
    if (tg && typeof tg.disableVerticalSwipes === "function") tg.disableVerticalSwipes();
  } catch (e) {}
});

if (document.readyState === "complete") {
  const box = document.getElementById("messages");
  if (box && isNearBottom(box)) {
    scrollToBottom(box);
  }
}
