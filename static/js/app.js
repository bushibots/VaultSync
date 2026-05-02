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
    init() {
        const menuToggle = document.getElementById('menu-toggle');
        const closeMenu = document.getElementById('close-menu');
        const sidebar = document.getElementById('sidebar');
        const overlay = document.getElementById('sidebar-overlay');

        if (!menuToggle || !sidebar) return;

        menuToggle.addEventListener('click', () => {
            sidebar.classList.add('active');
            if (overlay) overlay.classList.add('active');
        });

        if (closeMenu) {
            closeMenu.addEventListener('click', () => {
                sidebar.classList.remove('active');
                if (overlay) overlay.classList.remove('active');
            });
        }

        if (overlay) {
            overlay.addEventListener('click', () => {
                sidebar.classList.remove('active');
                overlay.classList.remove('active');
            });
        }
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

    open(id, name, color) {
        if (!this.modal || !this.form) return;

        document.getElementById('editName').value = name;
        document.getElementById('editColor').value = color;
        this.form.action = `/edit_category/${id}`;
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
window.openEditModal = (id, name, color) => EditModal.open(id, name, color);
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
// Initialize on DOM Ready
// ============================================

document.addEventListener('DOMContentLoaded', () => {
    MobileMenu.init();
    FormLoader.init();
    PasswordToggle.init();
    CurrentTime.init();
    EditModal.init();

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
