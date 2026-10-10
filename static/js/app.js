/**
 * VaultSync Core Application Script v3.0
 * Unified Theme, Drawer Navigation, Modals, Receipt/Statement AI Scanners, & Chatbot Widget
 */

// ============================================================================
// 1. Theme Manager (Synchronizes body.dark-mode and html.dark)
// ============================================================================
class ThemeManager {
    constructor() {
        this.storageKey = 'vaultsync-dark-mode';
        this.init();
    }

    init() {
        const saved = localStorage.getItem(this.storageKey);
        const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
        const isDark = saved !== null ? saved === 'true' : prefersDark;
        this.applyTheme(isDark, false);

        document.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-theme-toggle], #theme-toggle-desktop, #theme-toggle-mobile');
            if (btn) {
                e.preventDefault();
                this.toggle();
            }
        });
    }

    applyTheme(isDark, persist = true) {
        document.body.classList.toggle('dark-mode', isDark);
        document.documentElement.classList.toggle('dark', isDark);
        if (persist) {
            localStorage.setItem(this.storageKey, isDark ? 'true' : 'false');
        }
        this.updateButtons(isDark);
    }

    toggle() {
        const isDark = !document.body.classList.contains('dark-mode');
        this.applyTheme(isDark, true);
    }

    updateButtons(isDark) {
        const buttons = document.querySelectorAll('[data-theme-toggle], #theme-toggle-desktop, #theme-toggle-mobile');
        buttons.forEach((btn) => {
            btn.setAttribute('aria-pressed', isDark ? 'true' : 'false');
            const icon = btn.querySelector('i');
            if (icon) {
                icon.className = isDark ? 'fas fa-sun text-amber-400' : 'fas fa-moon';
            }
            const label = btn.querySelector('[data-theme-label]');
            if (label) {
                label.textContent = isDark ? 'Light Mode' : 'Dark Mode';
            }
        });
    }
}

// ============================================================================
// 2. Responsive Drawer & Navigation Manager
// ============================================================================
class DrawerManager {
    constructor() {
        this.sidebar = document.getElementById('vs-sidebar') || document.getElementById('sidebar');
        this.backdrop = document.getElementById('vs-drawer-backdrop') || document.getElementById('mobile-nav-backdrop');
        this.init();
    }

    init() {
        document.addEventListener('click', (e) => {
            const toggleBtn = e.target.closest('[data-drawer-toggle], #menu-toggle');
            if (toggleBtn) {
                e.preventDefault();
                this.toggle();
                return;
            }

            const closeBtn = e.target.closest('[data-drawer-close]');
            if (closeBtn) {
                e.preventDefault();
                this.close();
                return;
            }

            if (this.backdrop && e.target === this.backdrop) {
                this.close();
            }
        });

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                this.close();
            }
        });

        window.addEventListener('resize', () => {
            if (window.innerWidth >= 1024) {
                this.close();
            }
        });
    }

    open() {
        if (!this.sidebar) return;
        this.sidebar.classList.add('open');
        this.sidebar.classList.remove('-translate-x-full');
        if (this.backdrop) {
            this.backdrop.classList.add('open');
            this.backdrop.classList.remove('hidden');
        }
        document.body.style.overflow = 'hidden';
    }

    close() {
        if (!this.sidebar) return;
        this.sidebar.classList.remove('open');
        if (this.backdrop) {
            this.backdrop.classList.remove('open');
            this.backdrop.classList.add('hidden');
        }
        document.body.style.overflow = '';
    }

    toggle() {
        if (!this.sidebar) return;
        if (this.sidebar.classList.contains('open')) {
            this.close();
        } else {
            this.open();
        }
    }
}

// ============================================================================
// 3. Accessible Modal Manager
// ============================================================================
window.openModal = function(modalId) {
    const modal = document.getElementById(modalId);
    if (!modal) return;
    modal.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
    const firstInput = modal.querySelector('input:not([type="hidden"]), select, textarea');
    if (firstInput) {
        setTimeout(() => firstInput.focus(), 50);
    }
};

window.closeModal = function(modalId) {
    const modal = document.getElementById(modalId);
    if (!modal) return;
    modal.classList.add('hidden');
    document.body.style.overflow = '';
};

document.addEventListener('click', (e) => {
    const openTrigger = e.target.closest('[data-modal-open]');
    if (openTrigger) {
        e.preventDefault();
        window.openModal(openTrigger.getAttribute('data-modal-open'));
        return;
    }

    const closeTrigger = e.target.closest('[data-modal-close]');
    if (closeTrigger) {
        e.preventDefault();
        const modalId = closeTrigger.getAttribute('data-modal-close');
        if (modalId) {
            window.closeModal(modalId);
        } else {
            const parentModal = closeTrigger.closest('.vs-modal-backdrop');
            if (parentModal && parentModal.id) window.closeModal(parentModal.id);
        }
        return;
    }

    if (e.target.classList.contains('vs-modal-backdrop')) {
        window.closeModal(e.target.id);
    }
});

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        document.querySelectorAll('.vs-modal-backdrop:not(.hidden)').forEach((modal) => {
            if (modal.id) window.closeModal(modal.id);
        });
    }
});

// ============================================================================
// 4. Copy Helper & Toast Notifications
// ============================================================================
window.copyToClipboard = function(text, btnElement) {
    if (!text) return;
    navigator.clipboard.writeText(text).then(() => {
        if (btnElement) {
            const originalHTML = btnElement.innerHTML;
            btnElement.innerHTML = '<i class="fas fa-check text-emerald-400"></i> <span>Copied!</span>';
            setTimeout(() => {
                btnElement.innerHTML = originalHTML;
            }, 1800);
        }
    });
};

// ============================================================================
// 5. AI Receipt Scanner & Bulk Statement Importer
// ============================================================================
function initReceiptAndStatementScanners() {
    // Single Receipt Scanner
    document.querySelectorAll('[data-receipt-input], #receipt-input').forEach((input) => {
        if (input.dataset.bound === 'true') return;
        input.dataset.bound = 'true';

        input.addEventListener('change', async (e) => {
            const file = e.target.files[0];
            if (!file) return;

            const form = input.closest('form') || document;
            const statusEl = document.getElementById('receipt-upload-text') || form.querySelector('[data-receipt-status]');
            const originalText = statusEl ? statusEl.innerHTML : 'Scan Receipt';
            if (statusEl) {
                statusEl.innerHTML = '<i class="fas fa-circle-notch fa-spin"></i> Reading receipt...';
            }

            const formData = new FormData();
            formData.append('receipt', file);

            try {
                const response = await fetch('/api/scan-receipt', {
                    method: 'POST',
                    body: formData
                });
                const data = await response.json().catch(() => ({}));

                if (response.ok && !data.error) {
                    const itemInput = form.querySelector('input[name="item_name"]');
                    const amountInput = form.querySelector('input[name="amount"]');
                    const dateInput = form.querySelector('input[name="expense_date"]');
                    const catSelect = form.querySelector('select[name="category_id"]');

                    if (data.item_name && itemInput) itemInput.value = data.item_name;
                    if (data.amount && amountInput) amountInput.value = data.amount;
                    if (data.date && dateInput && /^\d{4}-\d{2}-\d{2}$/.test(data.date)) {
                        dateInput.value = data.date;
                    }
                    if (data.category && catSelect) {
                        const target = data.category.toLowerCase();
                        for (let i = 0; i < catSelect.options.length; i++) {
                            if (catSelect.options[i].text.toLowerCase().includes(target)) {
                                catSelect.selectedIndex = i;
                                break;
                            }
                        }
                    }
                    if (statusEl) {
                        statusEl.innerHTML = '<i class="fas fa-check text-emerald-500"></i> Autofilled!';
                    }
                } else {
                    alert('Receipt Scan Error: ' + (data.error || 'Could not extract receipt details.'));
                    if (statusEl) statusEl.innerHTML = '<i class="fas fa-exclamation-circle text-rose-500"></i> Scan failed';
                }
            } catch (err) {
                console.error(err);
                if (statusEl) statusEl.innerHTML = 'Scan error';
            } finally {
                input.value = '';
                setTimeout(() => {
                    if (statusEl) statusEl.innerHTML = originalText;
                }, 3200);
            }
        });
    });

    // Smart Expense Book & Statement Importer Forms (#statement-upload-form & #book-upload-section-form)
    const bookForms = [
        { formId: 'statement-upload-form', statusId: 'statement-scan-status' },
        { formId: 'book-upload-section-form', statusId: 'book-section-status' }
    ];

    bookForms.forEach(({ formId, statusId }) => {
        const formEl = document.getElementById(formId);
        if (!formEl || formEl.dataset.bound === 'true') return;
        formEl.dataset.bound = 'true';

        formEl.addEventListener('submit', async (e) => {
            e.preventDefault();
            const fileInput = formEl.querySelector('input[type="file"]');
            const textInput = formEl.querySelector('textarea[name="book_text"]');
            const submitBtn = formEl.querySelector('button[type="submit"]');
            const statusBox = document.getElementById(statusId);

            const hasFile = fileInput && fileInput.files && fileInput.files.length > 0;
            const hasText = textInput && textInput.value.trim().length > 0;

            if (!hasFile && !hasText) {
                alert('Please select an Expense Book file (.xlsx, .csv, .pdf, image) or paste spreadsheet data first.');
                return;
            }

            const originalBtnHTML = submitBtn.innerHTML;
            submitBtn.disabled = true;
            submitBtn.innerHTML = '<i class="fas fa-circle-notch fa-spin"></i> Capturing & Implementing Book...';
            if (statusBox) {
                statusBox.className = 'mt-3 p-3.5 rounded-xl bg-indigo-50 dark:bg-indigo-950/40 text-indigo-700 dark:text-indigo-300 text-xs font-medium border border-indigo-200 dark:border-indigo-500/30';
                statusBox.innerHTML = '<i class="fas fa-sparkles mr-1.5"></i> Parsing ledger transactions, category budgets, and family members...';
                statusBox.classList.remove('hidden');
            }

            try {
                const formData = new FormData(formEl);
                ['apply_budgets', 'sync_members', 'skip_transfers'].forEach((field) => {
                    const cb = formEl.querySelector(`input[name="${field}"]`);
                    if (cb) {
                        formData.set(field, cb.checked ? 'true' : 'false');
                    }
                });

                const response = await fetch('/api/import-book', {
                    method: 'POST',
                    body: formData
                });
                const data = await response.json().catch(() => ({}));

                if (response.ok && data.success) {
                    if (statusBox) {
                        statusBox.className = 'mt-3 p-4 rounded-xl bg-emerald-50 dark:bg-emerald-950/40 text-emerald-800 dark:text-emerald-200 text-xs border border-emerald-200 dark:border-emerald-500/30 space-y-1.5';
                        let detailsHtml = `<div class="font-bold text-sm flex items-center gap-1.5"><i class="fas fa-check-circle text-emerald-600 dark:text-emerald-400"></i> <span>Expense Book Auto-Captured &amp; Implemented!</span></div>`;
                        detailsHtml += `<div>${data.message || `Imported ${data.count} expenses.`}</div>`;
                        if (data.budgets_updated && data.budgets_updated.length) {
                            detailsHtml += `<div class="text-[11px] opacity-90"><strong>Budgets Set (${data.book_month || 'Current Month'}):</strong> ${data.budgets_updated.join(', ')}</div>`;
                        }
                        statusBox.innerHTML = detailsHtml;
                    }
                    setTimeout(() => window.location.reload(), 1600);
                } else {
                    const errMsg = data.error || data.message || 'No valid transactions or budgets found in document.';
                    if (statusBox) {
                        statusBox.className = 'mt-3 p-3.5 rounded-xl bg-rose-50 dark:bg-rose-950/40 text-rose-700 dark:text-rose-300 text-xs font-semibold border border-rose-200 dark:border-rose-500/30';
                        statusBox.textContent = errMsg;
                    }
                    submitBtn.disabled = false;
                    submitBtn.innerHTML = originalBtnHTML;
                }
            } catch (err) {
                if (statusBox) {
                    statusBox.className = 'mt-3 p-3.5 rounded-xl bg-rose-50 dark:bg-rose-950/40 text-rose-700 dark:text-rose-300 text-xs font-semibold border border-rose-200 dark:border-rose-500/30';
                    statusBox.textContent = 'Network error while importing book: ' + err.message;
                }
                submitBtn.disabled = false;
                submitBtn.innerHTML = originalBtnHTML;
            }
        });
    });
}

// ============================================================================
// 6. VaultSync AI Assistant Floating Widget
// ============================================================================
class ChatbotWidget {
    constructor() {
        if (document.body.dataset.disableChatbot === 'true') return;
        if (window.location.pathname === '/login' || window.location.pathname === '/register') return;
        this.isOpen = false;
        this.historyLoaded = false;
        this.mount();
    }

    mount() {
        const root = document.createElement('section');
        root.className = 'vaultsync-chatbot-root';
        root.setAttribute('aria-label', 'VaultSync AI Assistant');
        root.innerHTML = `
            <div class="vaultsync-chatbot-panel" id="vaultsyncChatbotPanel" hidden>
                <header class="vaultsync-chatbot-header">
                    <div>
                        <h3><i class="fas fa-sparkles mr-1.5 text-indigo-400"></i>VaultSync Assistant</h3>
                        <p>Log expenses, manage categories &amp; ask budget questions</p>
                    </div>
                    <button type="button" class="vaultsync-chatbot-icon-btn" id="vaultsyncChatbotClose" aria-label="Close assistant">
                        <i class="fas fa-times"></i>
                    </button>
                </header>
                <div class="vaultsync-chatbot-messages" id="vaultsyncChatbotMessages">
                    <div class="vaultsync-chat-msg assistant">
                        Hi! Tell me an expense to log (e.g., <em>"Spent 450 on groceries today"</em>) or ask about your monthly budget.
                    </div>
                </div>
                <div class="vaultsync-chatbot-prompts">
                    <button type="button" data-chat-prompt="Summarize this month's budget and remaining balance">Month summary</button>
                    <button type="button" data-chat-prompt="Add expense 350 for groceries today">Log Rs. 350 groceries</button>
                    <button type="button" data-chat-prompt="Which categories are highest this month?">Top categories</button>
                </div>
                <form class="vaultsync-chatbot-form" id="vaultsyncChatbotForm">
                    <input type="text" id="vaultsyncChatbotInput" placeholder="Ask or log an expense..." autocomplete="off" maxlength="600" />
                    <button type="submit" id="vaultsyncChatbotSend" aria-label="Send">
                        <i class="fas fa-paper-plane"></i>
                    </button>
                </form>
            </div>
            <button type="button" class="vaultsync-chatbot-fab" id="vaultsyncChatbotFab" aria-expanded="false">
                <i class="fas fa-wand-magic-sparkles"></i>
                <span>AI Assistant</span>
            </button>
        `;
        document.body.appendChild(root);

        this.panel = root.querySelector('#vaultsyncChatbotPanel');
        this.fab = root.querySelector('#vaultsyncChatbotFab');
        this.closeBtn = root.querySelector('#vaultsyncChatbotClose');
        this.messages = root.querySelector('#vaultsyncChatbotMessages');
        this.form = root.querySelector('#vaultsyncChatbotForm');
        this.input = root.querySelector('#vaultsyncChatbotInput');
        this.sendBtn = root.querySelector('#vaultsyncChatbotSend');

        this.fab.addEventListener('click', () => this.toggle());
        this.closeBtn.addEventListener('click', () => this.close());

        document.addEventListener('click', (e) => {
            const trigger = e.target.closest('[data-open-chatbot]');
            if (trigger) {
                e.preventDefault();
                this.open();
            }
        });

        root.querySelectorAll('[data-chat-prompt]').forEach((button) => {
            button.addEventListener('click', () => {
                this.open();
                this.input.value = button.getAttribute('data-chat-prompt') || '';
                this.input.focus();
            });
        });

        this.form.addEventListener('submit', (event) => {
            event.preventDefault();
            this.sendMessage();
        });
    }

    toggle() {
        if (this.isOpen) this.close();
        else this.open();
    }

    open() {
        this.isOpen = true;
        this.panel.hidden = false;
        this.fab.setAttribute('aria-expanded', 'true');
        if (!this.historyLoaded) {
            this.loadHistory();
        }
        setTimeout(() => this.input.focus(), 50);
    }

    close() {
        this.isOpen = false;
        this.panel.hidden = true;
        this.fab.setAttribute('aria-expanded', 'false');
    }

    async loadHistory() {
        this.historyLoaded = true;
        try {
            const response = await fetch('/chatbot/history');
            if (!response.ok) return;
            const payload = await response.json();
            if (!Array.isArray(payload.messages) || !payload.messages.length) return;
            this.messages.innerHTML = '';
            payload.messages.forEach((item) => {
                this.appendMessage(
                    item.role === 'user' ? 'user' : 'assistant',
                    item.message || '',
                    item.actions || [],
                    false
                );
            });
        } catch (err) {
            console.warn('Could not load chat history', err);
        }
    }

    appendMessage(role, text, actions = [], allowExecute = true) {
        const bubble = document.createElement('div');
        bubble.className = `vaultsync-chat-msg ${role}`;
        bubble.textContent = text;

        if (Array.isArray(actions) && actions.length) {
            const actionsWrap = document.createElement('div');
            actionsWrap.className = 'vaultsync-chat-actions';
            actions.forEach((action) => {
                const item = document.createElement('div');
                item.className = 'vaultsync-chat-action-item';
                item.textContent = action.summary || `${action.type}: ${action.name || action.item_name || ''}`;
                actionsWrap.appendChild(item);
            });

            if (allowExecute) {
                const approveBtn = document.createElement('button');
                approveBtn.type = 'button';
                approveBtn.className = 'vaultsync-chat-approve-btn';
                approveBtn.innerHTML = '<i class="fas fa-check mr-1"></i> Approve & Apply';
                approveBtn.addEventListener('click', () => this.executeActions(actions, approveBtn));
                actionsWrap.appendChild(approveBtn);
            }
            bubble.appendChild(actionsWrap);
        }

        this.messages.appendChild(bubble);
        this.messages.scrollTop = this.messages.scrollHeight;
    }

    async sendMessage() {
        const message = this.input.value.trim();
        if (!message) return;

        this.input.value = '';
        this.appendMessage('user', message);
        this.sendBtn.disabled = true;

        try {
            const response = await fetch('/chatbot/message', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message })
            });
            const data = await response.json();
            this.appendMessage('assistant', data.reply || 'Done.', data.actions || [], true);
        } catch (err) {
            this.appendMessage('assistant', 'Network error while reaching VaultSync Assistant.');
        } finally {
            this.sendBtn.disabled = false;
        }
    }

    async executeActions(actions, button) {
        button.disabled = true;
        button.textContent = 'Applying...';
        try {
            const response = await fetch('/chatbot/message', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ execute: true, actions })
            });
            const data = await response.json();
            this.appendMessage('assistant', data.reply || 'Applied changes.');
            button.textContent = 'Applied ✓';
            setTimeout(() => window.location.reload(), 1000);
        } catch (err) {
            button.disabled = false;
            button.textContent = 'Retry Apply';
        }
    }
}

// ============================================================================
// Initialize on DOMContentLoaded
// ============================================================================
document.addEventListener('DOMContentLoaded', () => {
    window.vaultSyncTheme = new ThemeManager();
    window.vaultSyncDrawer = new DrawerManager();
    initReceiptAndStatementScanners();
    window.vaultSyncChatbot = new ChatbotWidget();
});
