function getCsrfToken() {
    const match = document.cookie.match(/csrftoken=([^;]+)/);
    return match ? match[1] : '';
}
const clientId = window.CRM_CLIENT_DETAIL_CONFIG?.clientId;

if (!clientId) {
    console.error('CRM client detail config is missing clientId');
}

function refreshAlfabankIpStatus() {
    const btn = document.getElementById('btn-alfabank-ip-refresh');
    const msg = document.getElementById('alfabank-ip-refresh-msg');
    btn.disabled = true;
    btn.textContent = '⏳ Обновление...';
    msg.style.display = 'none';
    fetch(`/crm/client/${clientId}/alfabank-ip-refresh/`, {
        method: 'POST',
        headers: {'X-CSRFToken': getCsrfToken(), 'Content-Type': 'application/json'},
        body: JSON.stringify({}),
    })
    .then(r => r.json())
    .then(data => {
        btn.disabled = false;
        btn.textContent = '🔄 Обновить';
        if (data.success) {
            const statusBadge = document.getElementById('alfabank-ip-status-badge');
            const rawState = document.getElementById('alfabank-ip-raw-state');
            const updatedAt = document.getElementById('alfabank-ip-updated-at');
            const statusMap = {
                'approved': '<span class="badge badge-success" style="font-size:0.9rem;padding:0.35rem 0.75rem;">✅ Принята</span>',
                'declined': '<span class="badge badge-danger" style="font-size:0.9rem;padding:0.35rem 0.75rem;">❌ Отклонена</span>',
                'pending':  '<span class="badge badge-warning" style="font-size:0.9rem;padding:0.35rem 0.75rem;">🕒 Рассматривается</span>',
            };
            statusBadge.innerHTML = statusMap[data.status] || data.status;
            rawState.textContent = data.alfabank_state || '—';
            updatedAt.textContent = data.updated_at || '—';
            msg.style.display = 'block';
            msg.style.color = '#27ae60';
            msg.textContent = 'Статус обновлён.';
        } else {
            msg.style.display = 'block';
            msg.style.color = '#e74c3c';
            msg.textContent = 'Ошибка: ' + (data.error || 'неизвестная ошибка');
        }
    })
    .catch(err => {
        btn.disabled = false;
        btn.textContent = '🔄 Обновить';
        msg.style.display = 'block';
        msg.style.color = '#e74c3c';
        msg.textContent = 'Сетевая ошибка: ' + err;
    });
}

// Редактирование полей клиента
document.querySelectorAll('.editable').forEach(el => {
    el.addEventListener('click', function() {
        if (this.querySelector('.edit-input, .edit-checkbox')) return;

        const field = this.dataset.field;
        const currentValue = this.dataset.value || '';
        const originalHTML = this.innerHTML;

        const isCheckbox = this.dataset.type === 'checkbox';
        const isDate = this.dataset.type === 'date';
        if (isCheckbox) {
            this.innerHTML = `
                <label style="display:flex;align-items:center;gap:0.4rem;cursor:pointer;">
                    <input type="checkbox" class="edit-checkbox" ${currentValue === '1' ? 'checked' : ''}>
                    Добавить в базу клиентов
                </label>
                <button class="edit-btn" onclick="saveField(this, '${field}', event)">✓</button>
                <button class="edit-btn cancel" onclick="cancelEdit(this, event)">✗</button>
            `;
        } else {
            const inputType = isDate ? 'date' : 'text';
            this.innerHTML = `
                <input type="${inputType}" class="edit-input" value="${currentValue}">
                <button class="edit-btn" onclick="saveField(this, '${field}', event)">✓</button>
                <button class="edit-btn cancel" onclick="cancelEdit(this, event)">✗</button>
            `;
            this.querySelector('input').focus();
        }
        this.dataset.originalHtml = originalHTML;
        this.classList.add('editing');
    });
});

function saveField(btn, field, e) {
    e?.stopPropagation();
    const container = btn.closest('.editable');
    const input = container.querySelector('input');
    const isCheckbox = container.dataset.type === 'checkbox';
    const value = isCheckbox ? Boolean(input.checked) : input.value;

    fetch(`/crm/client/${clientId}/edit/`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()},
        body: JSON.stringify({field: field, value: value})
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            if (isCheckbox) {
                container.dataset.value = value ? '1' : '0';
                container.innerHTML = `<label style="display:flex;align-items:center;gap:0.4rem;cursor:pointer;"><input type="checkbox" ${value ? 'checked' : ''} disabled style="pointer-events:none;">Добавить в базу клиентов</label>`;
            } else {
                container.dataset.value = value;
                if (container.dataset.type === 'date') {
                    container.innerHTML = (formatDateRu(value) || '-') + ' <span class="edit-icon">✏️</span>';
                } else {
                    container.innerHTML = (value || '-') + ' <span class="edit-icon">✏️</span>';
                }
            }
            delete container.dataset.originalHtml;
            container.classList.remove('editing');
            showToast('Сохранено!', 'success');
        } else {
            showToast(data.error || 'Ошибка', 'error');
        }
    });
}

function cancelEdit(btn, e) {
    e?.stopPropagation();
    const container = btn.closest('.editable');
    container.innerHTML = container.dataset.originalHtml || '';
    delete container.dataset.originalHtml;
    container.classList.remove('editing');
}

function formatDateRu(value) {
    if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return value || '';
    const [year, month, day] = value.split('-');
    return `${day}.${month}.${year}`;
}

function setEditableText(container, text) {
    container.textContent = text || '-';
}

function updateKktDateExpiryStyle(container, value) {
    const isDate = value && /^\d{4}-\d{2}-\d{2}$/.test(value);
    if (!isDate) {
        container.style.color = '';
        container.style.fontWeight = '';
        return;
    }

    const today = new Date();
    today.setHours(0, 0, 0, 0);

    const date = new Date(value + 'T00:00:00');
    if (date < today) {
        container.style.color = '#e74c3c';
        container.style.fontWeight = 'bold';
    } else {
        container.style.color = '';
        container.style.fontWeight = '';
    }
}

function isKktDateField(container, field) {
    const input = container.querySelector('input');
    return (input && input.type === 'date') || [
        'kkt_registered_at',
        'ecp_expires_at',
        'to_kkt_yurt',
        'sim_payment_at',
        'tspiot_installed_at',
        'tspiot_tariff_expires_at',
        'fn_expires_at',
        'ofd_expires_at',
        'mark_expires_at'
    ].includes(field);
}

// Кастомные поля
function openCustomFieldModal(id = '', name = '', value = '') {
    document.getElementById('field-id').value = id;
    document.getElementById('field-name').value = name;
    document.getElementById('field-value').value = value;
    document.getElementById('modal-title').textContent = id ? 'Редактировать поле' : 'Добавить поле';
    document.getElementById('custom-field-modal').classList.add('active');
}

function closeModal() {
    document.getElementById('custom-field-modal').classList.remove('active');
}

function editCustomField(id, name, value) {
    openCustomFieldModal(id, name, value);
}

function saveCustomField() {
    const fieldId = document.getElementById('field-id').value;
    const fieldName = document.getElementById('field-name').value;
    const fieldValue = document.getElementById('field-value').value;

    if (!fieldName.trim()) {
        showToast('Введите название поля', 'error');
        return;
    }

    fetch(`/crm/client/${clientId}/custom-field/`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()},
        body: JSON.stringify({field_id: fieldId, field_name: fieldName, field_value: fieldValue})
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            showToast(data.message, 'success');
            closeModal();
            location.reload();
        } else {
            showToast(data.error || 'Ошибка', 'error');
        }
    });
}

function deleteCustomField(id) {
    if (!confirm('Удалить это поле?')) return;

    fetch(`/crm/client/${clientId}/custom-field/${id}/delete/`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()}
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            showToast('Поле удалено', 'success');
            document.querySelector(`.custom-field-item[data-id="${id}"]`).remove();
        } else {
            showToast(data.error || 'Ошибка', 'error');
        }
    });
}

function showToast(message, type) {
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
}

// ====== ККТ Карточки ======
function toggleKktCard(kktId) {
    const card = document.getElementById('kkt-card-' + kktId);
    card.classList.toggle('expanded');
}

function switchKktTab(kktId, tabName) {
    // Убираем active со всех вкладок и контента
    const card = document.getElementById('kkt-card-' + kktId);
    card.querySelectorAll('.kkt-tab').forEach(t => t.classList.remove('active'));
    card.querySelectorAll('.kkt-tab-content').forEach(c => c.classList.remove('active'));
    // Активируем нужную вкладку
    event.target.classList.add('active');
    document.getElementById('kkt-' + kktId + '-' + tabName).classList.add('active');
}

function editKktField(el, kktId, field, inputType = 'text') {
    if (el.querySelector('input')) return;
    event.stopPropagation();

    const currentValue = el.dataset.value || '';
    const originalHTML = el.innerHTML;

    el.innerHTML = `
        <input type="${inputType}" class="kkt-edit-input" value="${currentValue}">
        <button class="edit-btn" onclick="saveKktField(this, ${kktId}, '${field}')">✓</button>
        <button class="edit-btn cancel" onclick="cancelKktEdit(this, '${originalHTML.replace(/'/g, "\\'")}')">✗</button>
    `;
    el.querySelector('input').focus();
    el.classList.add('editing');
}

function saveKktField(btn, kktId, field) {
    const container = btn.closest('.kkt-editable');
    const input = container.querySelector('input');
    const isCheckbox = container.dataset.type === 'checkbox';
    const value = isCheckbox ? Boolean(input.checked) : input.value;

    fetch(`/crm/client/${clientId}/kkt/${kktId}/update/`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()},
        body: JSON.stringify({field: field, value: value})
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            const isDateField = isKktDateField(container, field);
            const displayValue = isDateField ? formatDateRu(value) : value;
            container.dataset.value = value;
            setEditableText(container, displayValue);
            if (isDateField) {
                updateKktDateExpiryStyle(container, value);
            }
            delete container.dataset.originalHtml;
            container.classList.remove('editing');
            showToast('Сохранено!', 'success');
        } else {
            showToast(data.error || 'Ошибка', 'error');
        }
    });
}

function cancelKktEdit(btn, originalHTML) {
    const container = btn.closest('.kkt-editable');
    container.innerHTML = originalHTML;
    container.classList.remove('editing');
}

function toggleMarking(kktId, currentValue) {
    event.stopPropagation();
    const btn = event.currentTarget;
    const newValue = currentValue == 1 ? 0 : 1;
    fetch(`/crm/client/${clientId}/kkt/${kktId}/update/`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()},
        body: JSON.stringify({field: 'marking_enabled', value: newValue})
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            showToast('Маркировка ' + (newValue ? 'включена' : 'выключена'), 'success');
            updateBoolUI(btn, newValue, kktId, 'marking_enabled');
        } else {
            showToast(data.error || 'Ошибка', 'error');
        }
    });
}

// Универсальный toggle для boolean полей
function toggleKktBool(kktId, field, currentValue) {
    event.stopPropagation();
    const btn = event.currentTarget;
    const newValue = currentValue == 1 ? 0 : 1;
    fetch(`/crm/client/${clientId}/kkt/${kktId}/update/`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()},
        body: JSON.stringify({field: field, value: newValue})
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            showToast('Сохранено!', 'success');
            updateBoolUI(btn, newValue, kktId, field);
        } else {
            showToast(data.error || 'Ошибка', 'error');
        }
    });
}

// Обновляет бейдж Да/Нет и onclick без перезагрузки
function updateBoolUI(btn, newValue, kktId, field) {
    const container = btn.parentElement;
    const badge = container.querySelector('.badge');
    if (badge) {
        badge.textContent = newValue ? 'Да' : 'Нет';
        badge.className = 'badge ' + (newValue ? 'badge-success' : 'badge-secondary');
    }
    // Обновляем onclick кнопки с новым currentValue
    if (field === 'marking_enabled') {
        btn.setAttribute('onclick', `event.stopPropagation(); toggleMarking(${kktId}, ${newValue})`);
    } else {
        btn.setAttribute('onclick', `event.stopPropagation(); toggleKktBool(${kktId}, '${field}', ${newValue})`);
    }
}

// Выпадающий список для выбора значения
function editKktSelect(el, kktId, field, options) {
    if (el.querySelector('select')) return;
    event.stopPropagation();

    const currentValue = el.dataset.value || '';
    const originalHTML = el.innerHTML;

    let optionsHtml = '<option value="">-- Выбрать --</option>';
    options.forEach(opt => {
        const selected = opt === currentValue ? 'selected' : '';
        optionsHtml += `<option value="${opt}" ${selected}>${opt}</option>`;
    });

    el.innerHTML = `
        <select class="kkt-edit-input" style="width:180px;">${optionsHtml}</select>
        <button class="edit-btn" onclick="saveKktSelect(this, ${kktId}, '${field}')">✓</button>
        <button class="edit-btn cancel" onclick="cancelKktEdit(this, '${originalHTML.replace(/'/g, "\\'")}')">✗</button>
    `;
    el.querySelector('select').focus();
    el.classList.add('editing');
}

function saveKktSelect(btn, kktId, field) {
    const container = btn.closest('.kkt-editable');
    const select = container.querySelector('select');
    const value = select.value;

    fetch(`/crm/client/${clientId}/kkt/${kktId}/update/`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()},
        body: JSON.stringify({field: field, value: value})
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            container.dataset.value = value;
            container.innerHTML = value || 'Выбрать';
            delete container.dataset.originalHtml;
            container.classList.remove('editing');
            showToast('Сохранено!', 'success');
        } else {
            showToast(data.error || 'Ошибка', 'error');
        }
    });
}

// ====== ККТ Модалка ======
function getNextKktSlot() {
    const usedSlots = Array.from(document.querySelectorAll('.kkt-card .badge-info'))
        .map(badge => {
            const match = badge.textContent.match(/ККТ\s*#\s*(\d+)/);
            return match ? parseInt(match[1], 10) : 0;
        })
        .filter(slot => slot > 0);
    return usedSlots.length ? Math.max(...usedSlots) + 1 : 1;
}

function fillNextKktSlot() {
    document.getElementById('kkt-slot').value = getNextKktSlot();
}

function openKktModal() {
    fillNextKktSlot();
    document.getElementById('kkt-modal').classList.add('active');
}
function closeKktModal() {
    document.getElementById('kkt-modal').classList.remove('active');
    document.getElementById('kkt-name').value = '';
    document.getElementById('kkt-address').value = '';
    document.getElementById('kkt-phone').value = '';
    document.getElementById('kkt-landmark').value = '';
    document.getElementById('kkt-model').value = '';
    document.getElementById('kkt-registered-at').value = '';
    document.getElementById('kkt-serial-number').value = '';
    document.getElementById('kkt-reg-number').value = '';
    fillNextKktSlot();
}
function saveKkt() {
    const slot = document.getElementById('kkt-slot').value;

    const data = {
        kkt_slot: slot,
        name: document.getElementById('kkt-name').value,
        address: document.getElementById('kkt-address').value,
        phone: document.getElementById('kkt-phone').value,
        landmark: document.getElementById('kkt-landmark').value,
        kkt_model: document.getElementById('kkt-model').value,
        kkt_registered_at: document.getElementById('kkt-registered-at').value,
        kkt_serial_number: document.getElementById('kkt-serial-number').value,
        kkt_reg_number: document.getElementById('kkt-reg-number').value
    };

    fetch(`/crm/client/${clientId}/kkt/add/`, {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()},
        body: JSON.stringify(data)
    }).then(r => r.json()).then(result => {
        if (result.success) { showToast('ККТ добавлен', 'success'); location.reload(); }
        else { showToast(result.error || 'Не удалось добавить ККТ. Попробуйте обновить страницу.', 'error'); }
    });
}

// ====== Удаление записей ======
function deleteRecord(type, id) {
    if (!confirm('Удалить эту запись?')) return;
    fetch(`/crm/client/${clientId}/${type}/${id}/delete/`, {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()}
    }).then(r => r.json()).then(data => {
        if (data.success) { showToast('Удалено', 'success'); location.reload(); }
        else { showToast(data.error || 'Ошибка', 'error'); }
    });
}

// ====== Заметки / Сделки ======
function loadNotes() {
    fetch(`/crm/client/${clientId}/notes/`)
        .then(r => r.json())
        .then(data => {
            const list = document.getElementById('notes-list');
            if (!data.success || !data.notes.length) {
                list.innerHTML = '<div class="empty"><div class="icon">📝</div><p>Нет заметок и сделок</p></div>';
                return;
            }
            let html = '';
            data.notes.forEach(n => {
                const isDeal = n.note_type === 'deal';
                const sourceTable = n.source_table || 'client_notes';
                const statusMap = {
                    CONFIRMED: {label: 'Оплачено', style: 'background:#d4edda;color:#27ae60;'},
                    ADVANCE: {label: 'Аванс', style: 'background:#e8f4fd;color:#3498db;'},
                    INIT: {label: 'Не оплачено', style: 'background:#fff3cd;color:#f39c12;'}
                };
                const badgeMeta = statusMap[n.status] || statusMap.INIT;
                const statusBadge = isDeal && sourceTable === 'orders'
                    ? `<span style="${badgeMeta.style}padding:2px 8px;border-radius:10px;font-size:0.75rem;font-weight:600;margin-left:0.5rem;">${badgeMeta.label}</span>`
                    : '';
                html += `<div class="note-item">
                    <div class="note-icon ${n.note_type}">${isDeal ? '💼' : '📝'}</div>
                    <div class="note-body">
                        <div class="note-header">
                            <span class="note-product">${isDeal ? (n.product || 'Сделка') : 'Заметка'}${statusBadge}</span>
                            ${n.amount ? '<span class="note-amount">' + Number(n.amount).toLocaleString('ru-RU') + ' ₽</span>' : ''}
                        </div>
                        ${n.description ? '<div class="note-desc">' + escapeHtml(n.description) + '</div>' : ''}
                        <div class="note-meta">
                            <span>${n.author_name}</span>
                            <span>${n.created_at}</span>
                            ${isDeal && sourceTable === 'orders' ? `<a class="note-delete" style="text-decoration:none;" href="/crm/orders/${n.id}/?next=${encodeURIComponent(window.location.pathname)}">Открыть</a>` : ''}
                            ${isDeal && sourceTable === 'orders' ? `<button class="note-delete js-order-edit"
                                data-order-id="${n.id}"
                                data-amount="${escapeHtml(String(n.amount || ''))}"
                                data-status="${escapeHtml(String(n.status || 'INIT'))}"
                                data-description="${escapeHtml(String(n.description || ''))}"
                                data-internal-comment="${escapeHtml(String(n.internal_comment || ''))}"
                                data-paid-at="${escapeHtml(String(n.paid_at ? n.paid_at.replace(' ', 'T') : ''))}"
                                data-specialist-id="${escapeHtml(String(n.specialist_id || ''))}"
                                data-buyer-name="${escapeHtml(String((n.author_name || '').trim() || 'Покупатель'))}">Редактировать</button>` : ''}
                            <button class="note-delete" onclick="deleteNote(${n.id}, '${sourceTable}')">Удалить</button>
                        </div>
                    </div>
                </div>`;
            });
            list.innerHTML = html;
        });
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function openNoteModal(type) {
    document.getElementById('note-type').value = type;
    document.getElementById('note-modal-title').textContent = type === 'deal' ? 'Новая сделка' : 'Новая заметка';
    document.getElementById('note-product-group').style.display = type === 'deal' ? '' : 'none';
    document.getElementById('note-amount-group').style.display = type === 'deal' ? '' : 'none';
    document.getElementById('note-status-group').style.display = type === 'deal' ? '' : 'none';
    document.getElementById('note-product').value = '';
    document.getElementById('note-amount').value = '';
    document.getElementById('note-status').value = 'CONFIRMED';
    document.getElementById('note-description').value = '';
    document.getElementById('note-modal').classList.add('active');
}

function closeNoteModal() {
    document.getElementById('note-modal').classList.remove('active');
}

function saveNote() {
    const noteType = document.getElementById('note-type').value;
    const product = document.getElementById('note-product').value;
    const amount = document.getElementById('note-amount').value;
    const status = document.getElementById('note-status').value;
    const description = document.getElementById('note-description').value;

    if (!description.trim() && !product.trim()) {
        showToast('Заполните описание или продукт', 'error');
        return;
    }

    fetch(`/crm/client/${clientId}/note/save/`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()},
        body: JSON.stringify({note_type: noteType, product: product, amount: amount || null, description: description, status: status})
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            showToast('Сохранено!', 'success');
            closeNoteModal();
            loadNotes();
        } else {
            showToast(data.error || 'Ошибка', 'error');
        }
    });
}

function deleteNote(noteId, sourceTable) {
    if (!confirm('Удалить эту запись?')) return;
    fetch(`/crm/client/${clientId}/note/${noteId}/delete/`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()},
        body: JSON.stringify({source_table: sourceTable || 'client_notes'})
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            showToast('Удалено', 'success');
            loadNotes();
        } else {
            showToast(data.error || 'Ошибка', 'error');
        }
    });
}

document.addEventListener('click', function(e) {
    const btn = e.target.closest('.js-order-edit');
    if (!btn) return;
    openOrderEdit(
        btn.dataset.orderId,
        btn.dataset.amount,
        btn.dataset.status,
        btn.dataset.description,
        btn.dataset.internalComment,
        btn.dataset.paidAt,
        btn.dataset.specialistId,
        btn.dataset.buyerName
    );
});

function openOrderEdit(orderId, amount, status, description, internalComment, paidAt, specialistId, buyerName) {
    document.getElementById('edit-order-id').value = orderId;
    document.getElementById('edit-order-id-label').textContent = '#' + orderId;
    document.getElementById('edit-amount').value = amount;
    const statusMap = {'CONFIRMED': 'paid', 'ADVANCE': 'advance', 'INIT': 'unpaid', 'CANCELLED': 'cancelled'};
    document.getElementById('edit-paid-status').value = statusMap[status] || 'unpaid';
    document.getElementById('edit-description').value = description || '';
    document.getElementById('edit-comment').value = internalComment || '';
    document.getElementById('edit-paid-at').value = paidAt ? paidAt.slice(0, 16) : '';
    const specialistField = document.getElementById('edit-specialist-id');
    if (specialistField) specialistField.value = specialistId || '';
    document.getElementById('edit-buyer-box').textContent = buyerName || 'Покупатель';
    document.getElementById('order-edit-modal').classList.add('active');
}
function closeOrderEditModal() { document.getElementById('order-edit-modal').classList.remove('active'); }
function saveOrderEdit() {
    const orderId = document.getElementById('edit-order-id').value;
    const payload = {
        amount: document.getElementById('edit-amount').value || 0,
        new_status: document.getElementById('edit-paid-status').value,
        description: document.getElementById('edit-description').value.trim(),
        internal_comment: document.getElementById('edit-comment').value.trim(),
        paid_at: document.getElementById('edit-paid-at').value || null,
    };
    const specialistField = document.getElementById('edit-specialist-id');
    if (specialistField) payload.specialist_id = specialistField.value;
    fetch('/crm/orders/' + orderId + '/update/', {method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken':getCsrfToken()}, body: JSON.stringify(payload)})
        .then(r => r.json()).then(data => {
            if (data.success) { closeOrderEditModal(); showToast('Сделка обновлена', 'success'); loadNotes(); }
            else { showToast(data.error || 'Ошибка', 'error'); }
        }).catch(() => showToast('Ошибка сети', 'error'));
}

window.getCsrfToken = getCsrfToken;
window.refreshAlfabankIpStatus = refreshAlfabankIpStatus;
window.saveField = saveField;
window.cancelEdit = cancelEdit;
window.openCustomFieldModal = openCustomFieldModal;
window.closeModal = closeModal;
window.editCustomField = editCustomField;
window.saveCustomField = saveCustomField;
window.deleteCustomField = deleteCustomField;
window.toggleKktCard = toggleKktCard;
window.switchKktTab = switchKktTab;
window.editKktField = editKktField;
window.saveKktField = saveKktField;
window.cancelKktEdit = cancelKktEdit;
window.toggleMarking = toggleMarking;
window.toggleKktBool = toggleKktBool;
window.editKktSelect = editKktSelect;
window.saveKktSelect = saveKktSelect;
window.openKktModal = openKktModal;
window.closeKktModal = closeKktModal;
window.saveKkt = saveKkt;
window.deleteRecord = deleteRecord;
window.openNoteModal = openNoteModal;
window.closeNoteModal = closeNoteModal;
window.saveNote = saveNote;
window.deleteNote = deleteNote;
window.openOrderEdit = openOrderEdit;
window.closeOrderEditModal = closeOrderEditModal;
window.saveOrderEdit = saveOrderEdit;
window.showToast = showToast;

// Загружаем заметки при открытии страницы
loadNotes();

// Закрытие модалок по Escape и клику вне
document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
        closeModal(); closeKktModal(); closeNoteModal();
    }
});
document.querySelectorAll('.modal').forEach(modal => {
    modal.addEventListener('click', e => { if (e.target.classList.contains('modal')) e.target.classList.remove('active'); });
});
