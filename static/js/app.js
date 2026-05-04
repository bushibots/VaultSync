/**
 * VaultSync - Main Application JavaScript
 */

// ============================================
// Toast Notification System
// ============================================

const Toast = {
    container: null,

    init() {
        // Create toast container if it doesn't exist
        if (!this.container) {
            this.container = document.createElement('div');
            this.container.className = 'toast-container';
            document.body.appendChild(this.container);
        }
    },

    show(message, type = 'info', duration = 5000) {
        this.init();

        const icons = {
            success: 'fa-check-circle',
            error: 'fa-exclamation-circle',
            info: 'fa-info-circle',
            warning: 'fa-exclamation-triangle'
        };

        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.innerHTML = `
            <i class="fas ${icons[type]} toast-icon" aria-hidden="true"></i>
            <span class="toast-message">${this.escapeHtml(message)}</span>
            <button class="toast-close" aria-label="Close notification">
                <i class="fas fa-times"></i>
            </button>
        `;

        this.container.appendChild(toast);

        // Trigger animation
        requestAnimationFrame(() => {
            toast.classList.add('show');
        });

        // Close button handler
        const closeBtn = toast.querySelector('.toast-close');
        closeBtn.addEventListener('click', () => this.remove(toast));

        // Auto-remove after duration
        if (duration > 0) {
            setTimeout(() => this.remove(toast), duration);
        }

        return toast;
    },

    remove(toast) {
        toast.classList.add('hiding');
        toast.addEventListener('animationend', () => {
            toast.remove();
        });
    },

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    },

    success(message) { this.show(message, 'success'); },
    error(message) { this.show(message, 'error'); },
    info(message) { this.show(message, 'info'); },
    warning(message) { this.show(message, 'warning'); }
};

// ============================================
// Confirmation Dialog System
// ============================================

const ConfirmDialog = {
    modal: null,
    resolvePromise: null,

    init() {
        if (this.modal) return;

        this.modal = document.createElement('div');
        this.modal.className = 'modal-backdrop hidden';
        this.modal.setAttribute('role', 'dialog');
        this.modal.setAttribute('aria-modal', 'true');
        this.modal.setAttribute('aria-labelledby', 'confirm-title');
        this.modal.innerHTML = `
            <div class="bg-white rounded-xl shadow-xl max-w-md w-full p-6 m-4 animate-fade-in">
                <div class="flex items-center gap-4 mb-4">
                    <div class="flex-shrink-0 w-12 h-12 rounded-full bg-red-100 flex items-center justify-center">
                        <i class="fas fa-exclamation-triangle text-red-600 text-xl" aria-hidden="true"></i>
                    </div>
                    <h3 id="confirm-title" class="text-lg font-bold text-gray-800">Confirm Action</h3>
                </div>
                <p id="confirm-message" class="text-gray-600 mb-6"></p>
                <div class="flex gap-3">
                    <button id="confirm-cancel" class="flex-1 bg-gray-200 text-gray-800 font-semibold py-2.5 px-4 rounded-lg hover:bg-gray-300 transition">
                        Cancel
                    </button>
                    <button id="confirm-ok" class="flex-1 bg-red-600 text-white font-semibold py-2.5 px-4 rounded-lg hover:bg-red-700 transition">
                        Delete
                    </button>
                </div>
            </div>
        `;

        document.body.appendChild(this.modal);

        // Event handlers
        this.modal.querySelector('#confirm-cancel').addEventListener('click', () => this.resolve(false));
        this.modal.querySelector('#confirm-ok').addEventListener('click', () => this.resolve(true));

        // Close on backdrop click
        this.modal.addEventListener('click', (e) => {
            if (e.target === this.modal) this.resolve(false);
        });

        // Close on Escape key
        this.escHandler = (e) => {
            if (e.key === 'Escape') this.resolve(false);
        };
    },

    async ask(message, options = {}) {
        return new Promise((resolve) => {
            this.resolvePromise = resolve;

            if (!this.modal) this.init();

            const title = options.title || 'Confirm Action';
            const okText = options.okText || 'Confirm';
            const cancelText = options.cancelText || 'Cancel';

            this.modal.querySelector('#confirm-title').textContent = title;
            this.modal.querySelector('#confirm-message').textContent = message;
            this.modal.querySelector('#confirm-ok').textContent = okText;
            this.modal.querySelector('#confirm-cancel').textContent = cancelText;

            this.modal.classList.remove('hidden');

            // Focus trap
            this.modal.querySelector('#confirm-cancel').focus();

            // Add escape key handler
            document.addEventListener('keydown', this.escHandler);
        });
    },

    resolve(confirmed) {
        this.modal.classList.add('hidden');
        document.removeEventListener('keydown', this.escHandler);

        if (this.resolvePromise) {
            this.resolvePromise(confirmed);
            this.resolvePromise = null;
        }
    }
};

// ============================================
// Form Loading State Handler
// ============================================

const FormLoader = {
    init() {
        // Handle all forms with data-loading attribute
        document.querySelectorAll('form[data-loading]').forEach(form => {
            form.addEventListener('submit', (e) => {
                const submitBtn = form.querySelector('button[type="submit"]');
                if (submitBtn) {
                    submitBtn.classList.add('btn-loading');
                    submitBtn.disabled = true;

                    // Re-enable after 3 seconds as fallback
                    setTimeout(() => {
                        submitBtn.classList.remove('btn-loading');
                        submitBtn.disabled = false;
                    }, 3000);
                }
            });
        });
    }
};

// ============================================
// Password Visibility Toggle
// ============================================

const PasswordToggle = {
    init() {
        document.querySelectorAll('.password-wrapper').forEach(wrapper => {
            const input = wrapper.querySelector('input[type="password"], input[type="text"]');
            const toggleBtn = wrapper.querySelector('.password-toggle');

            if (!input || !toggleBtn) return;

            toggleBtn.addEventListener('click', () => {
                const isPassword = input.type === 'password';
                input.type = isPassword ? 'text' : 'password';
                toggleBtn.innerHTML = isPassword
                    ? '<i class="fas fa-eye-slash"></i>'
                    : '<i class="fas fa-eye"></i>';
            });
        });
    }
};

// ============================================
// Mobile Menu Handler
// ============================================

const MobileMenu = {
    isOpen: false,

    init() {
        const menuToggle = document.getElementById('menu-toggle');
        const closeMenu = document.getElementById('close-menu');
        const sidebar = document.getElementById('sidebar');
        const overlay = document.getElementById('sidebar-overlay');

        if (!menuToggle || !sidebar) return;

        const self = this;

        const openMenu = (e) => {
            if (e) {
                e.preventDefault();
                e.stopPropagation();
            }
            
            if (self.isOpen) return;
            
            self.isOpen = true;
            sidebar.classList.add('active');
            document.body.classList.add('menu-open');
            menuToggle.classList.add('active');
            menuToggle.setAttribute('aria-expanded', 'true');
            if (overlay) {
                overlay.classList.remove('fading');
                overlay.classList.add('active');
            }
        };

        const closeMenuPanel = (e) => {
            if (e) {
                e.preventDefault();
                e.stopPropagation();
            }
            
            if (!self.isOpen) return;
            
            self.isOpen = false;
            sidebar.classList.remove('active');
            document.body.classList.remove('menu-open');
            menuToggle.classList.remove('active');
            menuToggle.setAttribute('aria-expanded', 'false');
            
            if (overlay) {
                overlay.classList.add('fading');
                setTimeout(() => {
                    if (!self.isOpen) {
                        overlay.classList.remove('active');
                        overlay.classList.remove('fading');
                    }
                }, 300);
            }
        };

        menuToggle.addEventListener('click', () => {
            if (self.isOpen) {
                closeMenuPanel();
            } else {
                openMenu();
            }
        });

        if (closeMenu) closeMenu.addEventListener('click', closeMenuPanel);

        if (overlay) {
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) {
                    closeMenuPanel(e);
                }
            }, true);
        }

        // Close menu when clicking on nav links
        const navLinks = sidebar.querySelectorAll('.sidebar-nav a');
        navLinks.forEach(link => {
            link.addEventListener('click', () => {
                setTimeout(() => closeMenuPanel(), 150);
            });
        });

        // Escape key to close
        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape' && self.isOpen) {
                closeMenuPanel();
            }
        });
    }
};

// ============================================
// Edit Modal Handler (for dashboard)
// ============================================

const EditModal = {
    modal: null,
    form: null,

    init() {
        this.modal = document.getElementById('editModal');
        if (!this.modal) return;

        this.form = document.getElementById('editForm');

        // Close button handlers
        const closeBtn = this.modal.querySelector('button[onclick="closeEditModal()"]');
        if (closeBtn) closeBtn.addEventListener('click', () => this.close());

        // Close on backdrop click
        this.modal.addEventListener('click', (e) => {
            if (e.target === this.modal) this.close();
        });

        // Close on Escape
        this.modal.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') this.close();
        });
    },

    open(id, name, color, monthlyLimit = 0, isFixed = false) {
        if (!this.modal || !this.form) return;

        document.getElementById('editName').value = name;
        document.getElementById('editColor').value = color;
        const monthlyLimitInput = document.getElementById('editMonthlyLimit');
        const isFixedInput = document.getElementById('editIsFixed');
        if (monthlyLimitInput) monthlyLimitInput.value = Number(monthlyLimit || 0).toFixed(2);
        if (isFixedInput) isFixedInput.checked = Boolean(isFixed);
        const actionTemplate = this.form.dataset.actionTemplate || '/edit_category/0';
        this.form.action = actionTemplate.replace(/0$/, String(id));
        this.modal.classList.remove('hidden');

        // Focus first input
        this.modal.querySelector('input').focus();
    },

    close() {
        if (!this.modal) return;
        this.modal.classList.add('hidden');
    }
};

// Make openEditModal and closeEditModal globally available
window.openEditModal = (id, name, color, monthlyLimit, isFixed) => EditModal.open(id, name, color, monthlyLimit, isFixed);
window.closeEditModal = () => EditModal.close();

// ============================================
// Current Time Update
// ============================================

const CurrentTime = {
    init() {
        const timeEl = document.getElementById('current-time');
        if (!timeEl) return;

        const update = () => {
            const now = new Date();
            timeEl.textContent = now.toLocaleTimeString('en-US', {
                hour: '2-digit',
                minute: '2-digit',
                hour12: true
            });
        };

        update();
        setInterval(update, 1000);
    }
};

// ============================================
// Preferences: Theme and Language
// ============================================

const Preferences = {
    translations: {
        en: {
            nav_dashboard: 'Dashboard',
            nav_diary: 'Daily Diary',
            nav_ai_budget: 'AI Budget Planner',
            nav_archive: 'Archive',
            nav_features: 'Feature Guide',
            dark_mode: 'Dark Mode',
            light_mode: 'Light Mode',
            language: 'Language',
            guide_title: 'VaultSync Feature Guide',
            guide_subtitle: 'Every feature, what it does, and how to use it correctly.',
            macro_title: 'Macro Dashboard',
            macro_body: 'Use the dashboard to view monthly spending, budget, projected savings, and the category chart.',
            diary_title: 'Daily Expense Diary',
            diary_body: 'Quickly add small everyday expenses. These are the same expenses that roll into the dashboard.',
            plan_title: 'Expected Monthly Expenditure',
            plan_body: 'Write monthly bills in advance. They appear in charts and Recent Activity only after you mark them paid.',
            categories_title: 'Recommended Categories',
            categories_body: 'Preset categories keep spending clean and organized. Add custom categories only when needed.',
            archive_title: 'Monthly Archive',
            archive_body: 'Open Archive to browse older months and click any month to review its dashboard.',
            admin_title: 'Admin Panel',
            admin_body: 'Family managers can update budget, members, and family settings.',
            dark_title: 'Dark Mode and Hindi',
            dark_body: 'Dark Mode and Language controls live at the bottom of the sidebar. Your choice is saved in this browser.',
            ai_title: 'AI Budget Planner',
            ai_subtitle: 'Export family spending as clean JSON, ask an AI for a budget, import the JSON answer, compare it, then apply the plan.',
            ai_feature_title: 'AI Budget Planner',
            ai_feature_body: 'Export a date or month window as AI-ready JSON, ask an AI to create the next budget, import the JSON answer, compare it with your current plan, then apply it.'
        },
        hi: {
            nav_dashboard: 'डैशबोर्ड',
            nav_diary: 'डेली डायरी',
            nav_ai_budget: 'AI बजट प्लानर',
            nav_archive: 'आर्काइव',
            nav_features: 'फीचर गाइड',
            dark_mode: 'डार्क मोड',
            light_mode: 'लाइट मोड',
            language: 'भाषा',
            guide_title: 'VaultSync फीचर गाइड',
            guide_subtitle: 'हर फीचर क्या करता है और उसे सही तरीके से कैसे इस्तेमाल करना है।',
            macro_title: 'मैक्रो डैशबोर्ड',
            macro_body: 'महीने का कुल खर्च, बजट, बचत और कैटेगरी चार्ट देखने के लिए डैशबोर्ड इस्तेमाल करें।',
            diary_title: 'डेली एक्सपेंस डायरी',
            diary_body: 'छोटे रोज़ाना खर्च जल्दी जोड़ें। ये वही खर्च हैं जो डैशबोर्ड में भी जुड़ते हैं।',
            plan_title: 'Expected Monthly Expenditure',
            plan_body: 'महीने के बिल पहले से लिखें। Mark Paid करने पर ही वे असली खर्च बनकर चार्ट और Recent Activity में दिखते हैं।',
            categories_title: 'Recommended Categories',
            categories_body: 'Preset categories खर्चों को साफ और व्यवस्थित रखते हैं। Custom category सिर्फ तब बनाएं जब सच में ज़रूरत हो।',
            archive_title: 'Monthly Archive',
            archive_body: 'पुराने महीनों का खर्च देखने के लिए Archive खोलें और महीने पर क्लिक करें।',
            admin_title: 'Admin Panel',
            admin_body: 'Family manager बजट, members और family settings संभाल सकता है।',
            dark_title: 'Dark Mode and Hindi',
            dark_body: 'Sidebar के नीचे Dark Mode और Language controls हैं। आपकी पसंद browser में save रहती है।',
            ai_title: 'AI बजट प्लानर',
            ai_subtitle: 'खर्च को JSON में export करें, AI से budget बनवाएं, JSON answer import करें, compare करें और plan apply करें।',
            ai_feature_title: 'AI बजट प्लानर',
            ai_feature_body: 'किसी date या month window को AI-ready JSON में export करें, AI से अगला budget बनवाएं, answer import करें, current plan से compare करें और apply करें।'
        }
    },

    init() {
        this.themeButton = document.getElementById('dark-mode-toggle');
        this.languageSelect = document.getElementById('language-select');

        document.querySelectorAll('[data-i18n]').forEach((el) => {
            if (!el.dataset.en) el.dataset.en = el.textContent;
        });

        this.applyTheme(localStorage.getItem('vaultsync-theme') || 'light');
        this.applyLanguage(localStorage.getItem('vaultsync-language') || 'en');

        if (this.themeButton) {
            this.themeButton.addEventListener('click', () => {
                const next = document.body.classList.contains('dark-mode') ? 'light' : 'dark';
                localStorage.setItem('vaultsync-theme', next);
                this.applyTheme(next);
            });
        }

        if (this.languageSelect) {
            this.languageSelect.addEventListener('change', () => {
                localStorage.setItem('vaultsync-language', this.languageSelect.value);
                this.applyLanguage(this.languageSelect.value);
            });
        }
    },

    applyTheme(theme) {
        const isDark = theme === 'dark';
        document.body.classList.toggle('dark-mode', isDark);

        if (this.themeButton) {
            const label = this.themeButton.querySelector('[data-i18n]');
            const icon = this.themeButton.querySelector('i');
            if (label) label.dataset.i18n = isDark ? 'light_mode' : 'dark_mode';
            if (icon) icon.className = isDark ? 'fas fa-sun' : 'fas fa-moon';
        }

        this.applyLanguage(localStorage.getItem('vaultsync-language') || 'en');
    },

    applyLanguage(language) {
        if (this.languageSelect) this.languageSelect.value = language;

        document.documentElement.lang = language === 'hi' ? 'hi' : 'en';
        const dictionary = this.translations[language] || {};

        document.querySelectorAll('[data-i18n]').forEach((el) => {
            const key = el.dataset.i18n;
            if (dictionary[key]) el.textContent = dictionary[key];
            else if (el.dataset.en) el.textContent = el.dataset.en;
        });
    }
};

// ============================================
// Initialize on DOM Ready
// ============================================

document.addEventListener('DOMContentLoaded', () => {
    MobileMenu.init();
    FormLoader.init();
    PasswordToggle.init();
    CurrentTime.init();
    EditModal.init();
    Preferences.init();

    // Process any flash messages from server
    if (window.flashMessages && window.flashMessages.length > 0) {
        window.flashMessages.forEach(msg => {
            Toast.show(msg.message, msg.category);
        });
    }
});

// ============================================
// Helper: Confirm Dialog Replacement
// ============================================

// Override default confirm for progressive enhancement
async function confirmDialog(message) {
    return await ConfirmDialog.ask(message);
}
