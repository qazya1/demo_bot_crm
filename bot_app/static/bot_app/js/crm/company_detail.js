function getCsrfToken() {
    const match = document.cookie.match(/csrftoken=([^;]+)/);
    return match ? match[1] : '';
}
const companyConfig = window.CRM_COMPANY_DETAIL_CONFIG || {};
const companyId = companyConfig.companyId;
const companyApiBase = companyConfig.companyApiBase || '/crm/company/';
const contactsSearchUrl = companyConfig.contactsSearchUrl || '/api/contacts/search/';

if (!companyId) {
    console.error('CRM company detail config is missing companyId');
}

function showToast(message, type) {
    const toast = document.createElement('div');
    toast.className = 'toast ' + type;
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
}

// ======== Редактирование полей компании ========
document.querySelectorAll('.editable').forEach(el => {
    el.addEventListener('click', function() {
        if (this.querySelector('input') || this.querySelector('textarea')) return;
        const field = this.dataset.field;
        const currentValue = this.dataset.value || '';
        const inputType = this.dataset.inputType || 'text';
        const originalHTML = this.innerHTML;

        const editor = inputType === 'textarea'
            ? '<textarea class="edit-input" rows="4" style="max-width: 100%;">' + currentValue + '</textarea>'
            : '<input type="text" class="edit-input" value="' + currentValue.replace(/"/g, '&quot;') + '">';

        this.innerHTML = editor +
            '<button class="edit-btn" onclick="saveField(this, \'' + field + '\', \'' + inputType + '\')">&#x2713;</button>' +
            '<button class="edit-btn cancel" onclick="cancelEdit(this)">&#x2717;</button>';
        const editorEl = this.querySelector(inputType === 'textarea' ? 'textarea' : 'input');
        editorEl.focus();
        this.classList.add('editing');
        this._originalHTML = originalHTML;
    });
});

function saveField(btn, field, inputType) {
    const container = btn.closest('.editable');
    const input = container.querySelector(inputType === 'textarea' ? 'textarea' : 'input');
    const value = input.value;

    fetch(companyApiBase + companyId + '/edit/', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()},
        body: JSON.stringify({field: field, value: value})
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            container.dataset.value = value;
            const formattedValue = (value || '-').replace(/\n/g, '<br>');
            container.innerHTML = formattedValue + ' <span class="edit-icon">&#9999;&#65039;</span>';
            container.classList.remove('editing');
            showToast('Сохранено!', 'success');
        } else {
            showToast(data.error || 'Ошибка', 'error');
        }
    });
}

function cancelEdit(btn) {
    const container = btn.closest('.editable');
    container.innerHTML = container._originalHTML;
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

// ======== Контакты ========
let contactTab = 'existing';
let searchTimeout = null;

function openContactModal() {
    document.getElementById('contact-modal').classList.add('active');
    document.getElementById('contact-search').value = '';
    document.getElementById('selected-contact-id').value = '';
    document.getElementById('new-first-name').value = '';
    document.getElementById('new-last-name').value = '';
    document.getElementById('new-phone').value = '+7';
    document.getElementById('new-inn').value = '';
    document.getElementById('new-email').value = '';
    document.getElementById('selected-contact-preview').style.display = 'none';
    document.getElementById('contact-autocomplete').style.display = 'none';
}
function closeContactModal() {
    document.getElementById('contact-modal').classList.remove('active');
}

function switchContactTab(tab) {
    contactTab = tab;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    if (tab === 'existing') {
        document.querySelector('.tab-btn:first-child').classList.add('active');
        document.getElementById('tab-existing').classList.add('active');
    } else {
        document.querySelector('.tab-btn:last-child').classList.add('active');
        document.getElementById('tab-new').classList.add('active');
    }
}

function searchContacts(q) {
    clearTimeout(searchTimeout);
    const list = document.getElementById('contact-autocomplete');
    if (q.length < 2) { list.style.display = 'none'; return; }

    searchTimeout = setTimeout(() => {
        fetch(contactsSearchUrl + '?q=' + encodeURIComponent(q))
            .then(r => r.json())
            .then(data => {
                if (!data.contacts.length) {
                    list.innerHTML = '<div class="autocomplete-item"><span class="ac-name">Ничего не найдено</span></div>';
                    list.style.display = 'block';
                    return;
                }
                list.innerHTML = data.contacts.map(c => {
                    const name = (c.first_name || '') + ' ' + (c.last_name || '');
                    const details = [c.phone, c.inn ? 'ИНН: ' + c.inn : '', c.org_name, c.email, c.tg_username ? '@' + c.tg_username : ''].filter(Boolean).join(' | ');
                    return '<div class="autocomplete-item" onclick="selectContact(' + c.id + ', \'' + name.replace(/'/g, "\\'").trim() + '\', \'' + details.replace(/'/g, "\\'") + '\')">' +
                        '<div class="ac-name">' + name.trim() + ' <small style="color:#95a5a6;">(ID: ' + c.id + ')</small></div>' +
                        '<div class="ac-details">' + details + '</div></div>';
                }).join('');
                list.style.display = 'block';
            });
    }, 300);
}

function selectContact(id, name, details) {
    document.getElementById('selected-contact-id').value = id;
    document.getElementById('selected-contact-name').textContent = name;
    document.getElementById('selected-contact-details').textContent = details;
    document.getElementById('selected-contact-preview').style.display = 'block';
    document.getElementById('contact-autocomplete').style.display = 'none';
    document.getElementById('contact-search').value = name;
}

function saveContact() {
    let body = {};
    if (contactTab === 'existing') {
        const contactId = document.getElementById('selected-contact-id').value;
        if (!contactId) { showToast('Выберите контакт из списка', 'error'); return; }
        body = { contact_id: parseInt(contactId) };
        const existingRoleInput = document.getElementById('contact-role-existing');
        const existingRoleGroup = existingRoleInput ? existingRoleInput.closest('.form-group') : null;
        if (existingRoleInput && (!existingRoleGroup || existingRoleGroup.style.display !== 'none')) {
            body.role = existingRoleInput.value;
        }
    } else {
        const firstName = document.getElementById('new-first-name').value.trim();
        if (!firstName) { showToast('Введите имя контакта', 'error'); return; }
        body = {
            first_name: firstName,
            last_name: document.getElementById('new-last-name').value.trim(),
            phone: document.getElementById('new-phone').value.trim(),
            email: document.getElementById('new-email').value.trim(),
            inn: document.getElementById('new-inn').value.trim()
        };
        const newRoleInput = document.getElementById('contact-role-new');
        const newRoleGroup = newRoleInput ? newRoleInput.closest('.form-group') : null;
        if (newRoleInput && (!newRoleGroup || newRoleGroup.style.display !== 'none')) {
            body.role = newRoleInput.value.trim();
        }
    }

    fetch(companyApiBase + companyId + '/contact/add/', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()},
        body: JSON.stringify(body)
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            showToast('Контакт добавлен!', 'success');
            closeContactModal();
            location.reload();
        } else {
            showToast(data.error || 'Ошибка', 'error');
        }
    });
}

function removeContact(contactId) {
    if (!confirm('Отвязать этот контакт от компании?')) return;
    fetch(companyApiBase + companyId + '/contact/' + contactId + '/remove/', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()}
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            showToast('Контакт отвязан', 'success');
            const item = document.querySelector('.contact-item[data-contact-id="' + contactId + '"]');
            if (item) item.remove();
        } else {
            showToast(data.error || 'Ошибка', 'error');
        }
    });
}

// ======== ККТ ========
function toggleKktCard(kktId) {
    document.getElementById('kkt-card-' + kktId).classList.toggle('expanded');
}

function switchKktTab(kktId, tabName) {
    const card = document.getElementById('kkt-card-' + kktId);
    card.querySelectorAll('.kkt-tab').forEach(t => t.classList.remove('active'));
    card.querySelectorAll('.kkt-tab-content').forEach(c => c.classList.remove('active'));
    event.target.classList.add('active');
    document.getElementById('kkt-' + kktId + '-' + tabName).classList.add('active');
}

function editKktField(el, kktId, field, inputType) {
    if (el.querySelector('input')) return;
    event.stopPropagation();
    inputType = inputType || 'text';
    const currentValue = el.dataset.value || '';
    el._originalHTML = el.innerHTML;

    el.innerHTML = '<input type="' + inputType + '" class="kkt-edit-input" value="' + currentValue.replace(/"/g, '&quot;') + '">' +
        '<button class="edit-btn" onclick="saveKktField(this, ' + kktId + ', \'' + field + '\')">&#x2713;</button>' +
        '<button class="edit-btn cancel" onclick="cancelKktEdit(this)">&#x2717;</button>';
    el.querySelector('input').focus();
    el.classList.add('editing');
}

function saveKktField(btn, kktId, field) {
    const container = btn.closest('.kkt-editable');
    const input = container.querySelector('input');
    const value = input.value;

    fetch(companyApiBase + companyId + '/kkt/' + kktId + '/update/', {
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
            container.classList.remove('editing');
            showToast('Сохранено!', 'success');
        } else {
            showToast(data.error || 'Ошибка', 'error');
        }
    });
}

function cancelKktEdit(btn) {
    const container = btn.closest('.kkt-editable');
    container.innerHTML = container._originalHTML;
    container.classList.remove('editing');
}

function toggleKktBool(kktId, field, currentValue) {
    event.stopPropagation();
    const btn = event.currentTarget;
    const newValue = currentValue == 1 ? 0 : 1;
    fetch(companyApiBase + companyId + '/kkt/' + kktId + '/update/', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()},
        body: JSON.stringify({field: field, value: newValue})
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            showToast('Сохранено!', 'success');
            const container = btn.parentElement;
            const badge = container.querySelector('.badge');
            if (badge) {
                badge.textContent = newValue ? 'Да' : 'Нет';
                badge.className = 'badge ' + (newValue ? 'badge-success' : 'badge-secondary');
            }
            btn.setAttribute('onclick', 'event.stopPropagation(); toggleKktBool(' + kktId + ', \'' + field + '\', ' + newValue + ')');
        } else {
            showToast(data.error || 'Ошибка', 'error');
        }
    });
}

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
    ['kkt-name','kkt-address','kkt-phone','kkt-landmark','kkt-model','kkt-registered-at','kkt-serial-number','kkt-reg-number'].forEach(id => {
        document.getElementById(id).value = '';
    });
    document.getElementById('kkt-owner-contact').value = '';
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
        kkt_reg_number: document.getElementById('kkt-reg-number').value,
        owner_contact_id: document.getElementById('kkt-owner-contact').value || null,
    };

    fetch(companyApiBase + companyId + '/kkt/add/', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()},
        body: JSON.stringify(data)
    }).then(r => r.json()).then(result => {
        if (result.success) { showToast('ККТ добавлен', 'success'); location.reload(); }
        else { showToast(result.error || 'Не удалось добавить ККТ. Попробуйте обновить страницу.', 'error'); }
    });
}

function deleteKkt(kktId) {
    if (!confirm('Удалить эту ККТ?')) return;
    fetch(companyApiBase + companyId + '/kkt/' + kktId + '/delete/', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()}
    }).then(r => r.json()).then(data => {
        if (data.success) { showToast('Удалено', 'success'); location.reload(); }
        else { showToast(data.error || 'Ошибка', 'error'); }
    });
}

// Закрытие модалок
document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { closeContactModal(); closeKktModal(); closeDealModal(); closeOrderEditModal(); }
});
document.querySelectorAll('.modal').forEach(modal => {
    modal.addEventListener('click', e => { if (e.target.classList.contains('modal')) e.target.classList.remove('active'); });
});
document.addEventListener('click', e => {
    if (!e.target.closest('.search-wrapper')) {
        document.getElementById('contact-autocomplete').style.display = 'none';
    }
});

// ======== Сделки (заказы) компании ========
function openDealModal() {
    document.getElementById('deal-modal').classList.add('active');
    document.getElementById('deal-product').value = '';
    document.getElementById('deal-amount').value = '';
    document.getElementById('deal-description').value = '';
    document.getElementById('deal-status').value = 'CONFIRMED';
    document.getElementById('deal-contact-id').value = '';
}
function closeDealModal() {
    document.getElementById('deal-modal').classList.remove('active');
}

function saveDeal() {
    var product = document.getElementById('deal-product').value.trim();
    var description = document.getElementById('deal-description').value.trim();
    if (!product && !description) {
        showToast('Заполните продукт или описание', 'error');
        return;
    }

    var contactId = document.getElementById('deal-contact-id').value;
    var body = {
        company_id: companyId,
        client_id: contactId ? parseInt(contactId) : null,
        product: product,
        amount: document.getElementById('deal-amount').value || null,
        description: description,
        status: document.getElementById('deal-status').value
    };

    fetch('/crm/orders/create/', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()},
        body: JSON.stringify(body)
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            showToast('Сделка #' + data.order_id + ' создана!', 'success');
            closeDealModal();
            setTimeout(function() { location.reload(); }, 600);
        } else {
            showToast(data.error || 'Ошибка', 'error');
        }
    })
    .catch(function() { showToast('Ошибка сети', 'error'); });
}

function openCompanyOrderEdit(btn) {
    var d = btn.dataset;
    document.getElementById('edit-order-id').value = d.orderId;
    document.getElementById('edit-order-id-label').textContent = '#' + d.orderId;
    document.getElementById('edit-order-amount').value = d.amount;
    var statusMap = {'CONFIRMED': 'paid', 'ADVANCE': 'advance', 'INIT': 'unpaid', 'CANCELLED': 'cancelled'};
    document.getElementById('edit-order-paid').value = statusMap[d.status] || 'unpaid';
    document.getElementById('edit-order-description').value = d.description || '';
    document.getElementById('edit-order-comment').value = d.comment || '';
    document.getElementById('edit-order-paid-at').value = d.paidAt ? d.paidAt.slice(0, 16) : '';
    var specField = document.getElementById('edit-order-specialist-id');
    if (specField) specField.value = d.specialistId || '';
    var buyerBox = document.getElementById('edit-order-buyer-box');
    buyerBox.innerHTML = d.buyerUrl
        ? '<a href="' + d.buyerUrl + '" style="color:var(--color-primary);text-decoration:none;">' + (d.buyerName || 'Не привязан') + '</a>'
        : (d.buyerName || 'Не привязан');
    document.getElementById('order-edit-modal').classList.add('active');
}
function closeOrderEditModal() {
    document.getElementById('order-edit-modal').classList.remove('active');
}

function saveOrderEdit() {
    var orderId = document.getElementById('edit-order-id').value;
    var amount = document.getElementById('edit-order-amount').value;
    var paidStatus = document.getElementById('edit-order-paid').value;
    var description = document.getElementById('edit-order-description').value.trim();
    var internalComment = document.getElementById('edit-order-comment').value.trim();
    var paidAt = document.getElementById('edit-order-paid-at').value;
    var specField = document.getElementById('edit-order-specialist-id');
    var specialistId = specField ? specField.value : null;

    fetch('/crm/orders/' + orderId + '/update/', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()},
        body: JSON.stringify({
            amount: amount ? parseInt(amount) : 0,
            new_status: paidStatus,
            description: description,
            internal_comment: internalComment,
            paid_at: paidAt || null,
            specialist_id: specialistId
        })
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.success) {
            showToast('Сделка обновлена', 'success');
            closeOrderEditModal();
            setTimeout(function() { location.reload(); }, 600);
        } else {
            showToast(data.error || 'Ошибка', 'error');
        }
    })
    .catch(function() { showToast('Ошибка сети', 'error'); });
}

function deleteCompanyOrder(orderId) {
    if (!confirm('Удалить эту сделку?')) return;
    fetch('/crm/orders/' + orderId + '/delete/', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()}
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            showToast('Сделка удалена', 'success');
            setTimeout(function() { location.reload(); }, 600);
        } else {
            showToast(data.error || 'Ошибка', 'error');
        }
    });
}

window.saveField = saveField;
window.cancelEdit = cancelEdit;
window.openContactModal = openContactModal;
window.closeContactModal = closeContactModal;
window.switchContactTab = switchContactTab;
window.searchContacts = searchContacts;
window.selectContact = selectContact;
window.saveContact = saveContact;
window.removeContact = removeContact;
window.toggleKktCard = toggleKktCard;
window.switchKktTab = switchKktTab;
window.editKktField = editKktField;
window.saveKktField = saveKktField;
window.cancelKktEdit = cancelKktEdit;
window.toggleKktBool = toggleKktBool;
window.getNextKktSlot = getNextKktSlot;
window.fillNextKktSlot = fillNextKktSlot;
window.openKktModal = openKktModal;
window.closeKktModal = closeKktModal;
window.saveKkt = saveKkt;
window.deleteKkt = deleteKkt;
window.openDealModal = openDealModal;
window.closeDealModal = closeDealModal;
window.saveDeal = saveDeal;
window.openCompanyOrderEdit = openCompanyOrderEdit;
window.closeOrderEditModal = closeOrderEditModal;
window.saveOrderEdit = saveOrderEdit;
window.deleteCompanyOrder = deleteCompanyOrder;
window.showToast = showToast;
window.getCsrfToken = getCsrfToken;
