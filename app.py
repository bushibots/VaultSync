from flask import Flask, render_template, request, redirect, jsonify, Response, flash, url_for, send_file
from flask_login import LoginManager, login_required, current_user, login_user, logout_user
from flask_migrate import Migrate
import calendar
import csv
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from io import StringIO
from datetime import date, datetime, timedelta
from sqlalchemy import func, inspect, text
from sqlalchemy.orm import joinedload
from models import (
    db,
    User,
    Category,
    Expense,
    ExpectedExpense,
    Family,
    BudgetSuggestion,
    MonthlyFinanceSetting,
    BudgetPlanApplication,
    AISavingsForecast,
    BudgetReport,
    ChatbotMessage,
    AIExpenseException,
    AuditLog,
)


def load_local_env(path='.env'):
    """Load simple KEY=value pairs from a local env file without extra dependencies."""
    env_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), path)
    if not os.path.exists(env_path):
        return

    with open(env_path, encoding='utf-8') as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


load_local_env()

from flask_caching import Cache

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
cache = Cache(config={'CACHE_TYPE': 'SimpleCache'})
cache.init_app(app)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or secrets.token_urlsafe(32)
basedir = os.path.abspath(os.path.dirname(__file__))


def is_windows_drive_path(path):
    return (
        len(path) >= 3
        and path[0].isalpha()
        and path[1] == ':'
        and path[2] in ('/', '\\')
    )


def sqlite_has_table(db_path, table_name):
    if not os.path.exists(db_path):
        return False

    try:
        connection = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        try:
            row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return False

    return row is not None


def choose_sqlite_path(preferred_path, fallback_paths=None):
    fallback_paths = fallback_paths or []
    candidates = [preferred_path] + [
        path for path in fallback_paths
        if path and os.path.abspath(path) != os.path.abspath(preferred_path)
    ]

    for candidate in candidates:
        if sqlite_has_table(candidate, 'user'):
            return candidate

    return preferred_path


def build_database_uri():
    default_db_path = os.path.join(basedir, 'vaultsync.db')
    instance_db_path = os.path.join(app.instance_path, 'vaultsync.db')
    configured_uri = os.environ.get('DATABASE_URL')

    if not configured_uri:
        db_path = choose_sqlite_path(default_db_path, [instance_db_path])
    elif not configured_uri.startswith('sqlite:///'):
        return configured_uri
    else:
        db_path = configured_uri.removeprefix('sqlite:///')
        if db_path == ':memory:':
            return configured_uri
        if os.name != 'nt' and is_windows_drive_path(db_path):
            db_path = choose_sqlite_path(default_db_path, [instance_db_path])
        elif not os.path.isabs(db_path):
            app_db_path = os.path.join(basedir, db_path)
            flask_instance_db_path = os.path.join(app.instance_path, db_path)
            db_path = choose_sqlite_path(app_db_path, [flask_instance_db_path])

    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    return 'sqlite:///' + db_path.replace('\\', '/')


app.config['SQLALCHEMY_DATABASE_URI'] = build_database_uri()
db.init_app(app)
migrate = Migrate(app, db)

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'info'

app.jinja_env.globals.update(min=min, max=max)


@app.template_filter('fromjson')
def fromjson_filter(value):
    """Decode JSON stored in text columns for compact template loops."""
    if not value:
        return []
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []

PRESET_CATEGORIES = [
    ('Groceries', '#16a34a', 0),
    ('Rent / EMI', '#2563eb', 0),
    ('Utilities', '#f59e0b', 0),
    ('Transport', '#0ea5e9', 0),
    ('Healthcare', '#dc2626', 0),
    ('Education', '#7c3aed', 0),
    ('Insurance', '#0891b2', 0),
    ('Dining Out', '#ea580c', 0),
    ('Household', '#64748b', 0),
    ('Savings', '#059669', 0),
    ('Emergency', '#be123c', 0),
    ('Personal Care', '#db2777', 0),
]

# Professional color palette for auto-assignment - distinct, accessible, and visually appealing
AUTO_COLOR_PALETTE = [
    '#3b82f6',  # Blue
    '#ef4444',  # Red
    '#10b981',  # Emerald
    '#f59e0b',  # Amber
    '#8b5cf6',  # Violet
    '#ec4899',  # Pink
    '#06b6d4',  # Cyan
    '#f97316',  # Orange
    '#6366f1',  # Indigo
    '#14b8a6',  # Teal
    '#d946ef',  # Fuchsia
    '#e11d48',  # Rose
    '#2563eb',  # Blue-600
    '#059669',  # Emerald-600
    '#7c2d12',  # Orange-900
    '#4f46e5',  # Indigo-600
    '#0891b2',  # Cyan-600
    '#be185d',  # Pink-900
    '#1e40af',  # Blue-800
    '#15803d',  # Green-700
    '#92400e',  # Amber-900
    '#6d28d9',  # Violet-700
    '#06b6d4',  # Cyan-500
    '#be123c',  # Rose-700
]

GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash-lite')
AI_ORGANIZER_BATCH_SIZE = 25
_ai_organizer_scheduler_started = False
_ai_plan_scheduler_started = False
_runtime_schema_checked = False
_gemini_key_index = 0
_gemini_key_lock = threading.Lock()


def get_next_auto_color(family_id):
    """Generate the next distinct color for a new category in auto-color mode."""
    existing_categories = Category.query.filter_by(family_id=family_id, is_color_auto=True).all()
    color_index = len(existing_categories) % len(AUTO_COLOR_PALETTE)
    return AUTO_COLOR_PALETTE[color_index]


def reassign_auto_colors(family_id):
    """Reassign auto colors to all categories in auto-color mode, ensuring distinctness."""
    auto_categories = Category.query.filter_by(family_id=family_id, is_color_auto=True).order_by(
        Category.id.asc()
    ).all()
    
    for idx, category in enumerate(auto_categories):
        color_index = idx % len(AUTO_COLOR_PALETTE)
        category.color = AUTO_COLOR_PALETTE[color_index]
    
    db.session.commit()


@app.cli.command('repair-schema')
def repair_schema():
    """Repair older SQLite databases that were deployed before migrations ran."""
    db.create_all()

    inspector = inspect(db.engine)
    if not inspector.has_table('expected_expense'):
        print('expected_expense table is missing; run flask db upgrade first.')
        return

    columns = {
        column['name']
        for column in inspector.get_columns('expected_expense')
    }
    expected_expense_columns = {
        'due_day': 'due_day INTEGER NOT NULL DEFAULT 1',
        'paid_at': 'paid_at DATETIME',
        'linked_expense_id': 'linked_expense_id INTEGER',
        'is_bucket': 'is_bucket BOOLEAN NOT NULL DEFAULT 0',
        'allocated_amount': 'allocated_amount FLOAT NOT NULL DEFAULT 0',
    }
    expense_columns = {
        column['name']
        for column in inspector.get_columns('expense')
    }
    expense_column_ddls = {
        'description': 'description TEXT',
        'bucket_id': 'bucket_id INTEGER',
    }
    category_columns = {
        column['name']
        for column in inspector.get_columns('category')
    } if inspector.has_table('category') else set()

    added = []
    with db.engine.begin() as connection:
        family_columns = {
            column['name']
            for column in inspector.get_columns('family')
        } if inspector.has_table('family') else set()
        if 'ai_notes' not in family_columns:
            connection.execute(text('ALTER TABLE family ADD COLUMN ai_notes TEXT'))
            added.append('family.ai_notes')
        if 'monthly_income' not in family_columns:
            connection.execute(text('ALTER TABLE family ADD COLUMN monthly_income FLOAT'))
            connection.execute(text('UPDATE family SET monthly_income = monthly_budget WHERE monthly_income IS NULL'))
            added.append('family.monthly_income')
        if 'google_sheet_url' not in family_columns:
            connection.execute(text('ALTER TABLE family ADD COLUMN google_sheet_url VARCHAR(500)'))
            added.append('family.google_sheet_url')
        if 'google_sheet_webhook_url' not in family_columns:
            connection.execute(text('ALTER TABLE family ADD COLUMN google_sheet_webhook_url VARCHAR(500)'))
            added.append('family.google_sheet_webhook_url')
        if 'google_sheet_sync_token' not in family_columns:
            connection.execute(text('ALTER TABLE family ADD COLUMN google_sheet_sync_token VARCHAR(64)'))
            added.append('family.google_sheet_sync_token')
        if 'last_sheet_sync_at' not in family_columns:
            connection.execute(text('ALTER TABLE family ADD COLUMN last_sheet_sync_at DATETIME'))
            added.append('family.last_sheet_sync_at')
        if not inspector.has_table('monthly_finance_setting'):
            connection.execute(text("""
                CREATE TABLE monthly_finance_setting (
                    id INTEGER NOT NULL PRIMARY KEY,
                    family_id INTEGER NOT NULL,
                    month INTEGER NOT NULL,
                    year INTEGER NOT NULL,
                    monthly_income FLOAT,
                    monthly_budget FLOAT,
                    notes TEXT,
                    updated_by_user_id INTEGER,
                    updated_at DATETIME,
                    FOREIGN KEY(family_id) REFERENCES family (id),
                    FOREIGN KEY(updated_by_user_id) REFERENCES user (id)
                )
            """))
            connection.execute(text(
                'CREATE INDEX IF NOT EXISTS ix_monthly_finance_setting_family_id '
                'ON monthly_finance_setting (family_id)'
            ))
            added.append('monthly_finance_setting')
        if not inspector.has_table('budget_plan_application'):
            connection.execute(text("""
                CREATE TABLE budget_plan_application (
                    id INTEGER NOT NULL PRIMARY KEY,
                    family_id INTEGER NOT NULL,
                    suggestion_id INTEGER NOT NULL,
                    applied_by_user_id INTEGER NOT NULL,
                    target_month INTEGER NOT NULL,
                    target_year INTEGER NOT NULL,
                    created_expected_expense_ids TEXT,
                    created_category_ids TEXT,
                    snapshot_json TEXT NOT NULL,
                    reverted_at DATETIME,
                    created_at DATETIME,
                    FOREIGN KEY(family_id) REFERENCES family (id),
                    FOREIGN KEY(suggestion_id) REFERENCES budget_suggestion (id),
                    FOREIGN KEY(applied_by_user_id) REFERENCES user (id)
                )
            """))
            connection.execute(text(
                'CREATE INDEX IF NOT EXISTS ix_budget_plan_application_family_id '
                'ON budget_plan_application (family_id)'
            ))
            connection.execute(text(
                'CREATE INDEX IF NOT EXISTS ix_budget_plan_application_suggestion_id '
                'ON budget_plan_application (suggestion_id)'
            ))
            added.append('budget_plan_application')
        if not inspector.has_table('ai_savings_forecast'):
            connection.execute(text("""
                CREATE TABLE ai_savings_forecast (
                    id INTEGER NOT NULL PRIMARY KEY,
                    family_id INTEGER NOT NULL,
                    created_by_user_id INTEGER NOT NULL,
                    source_start_date DATE NOT NULL,
                    forecast_end_date DATE NOT NULL,
                    current_spent FLOAT NOT NULL DEFAULT 0,
                    predicted_additional_spend FLOAT NOT NULL DEFAULT 0,
                    predicted_total_spend FLOAT NOT NULL DEFAULT 0,
                    expected_savings FLOAT NOT NULL DEFAULT 0,
                    confidence_level VARCHAR(20) NOT NULL DEFAULT 'medium',
                    confidence_score FLOAT NOT NULL DEFAULT 0.5,
                    admin_notes TEXT,
                    raw_json TEXT NOT NULL,
                    created_at DATETIME
                )
            """))
            added.append('ai_savings_forecast')
        if not inspector.has_table('budget_report'):
            connection.execute(text("""
                CREATE TABLE budget_report (
                    id INTEGER NOT NULL PRIMARY KEY,
                    family_id INTEGER NOT NULL,
                    report_date DATE NOT NULL,
                    total_spent_today FLOAT NOT NULL DEFAULT 0,
                    total_spent_this_week FLOAT NOT NULL DEFAULT 0,
                    total_spent_this_month FLOAT NOT NULL DEFAULT 0,
                    monthly_budget FLOAT NOT NULL DEFAULT 0,
                    budget_remaining FLOAT NOT NULL DEFAULT 0,
                    budget_used_percentage FLOAT NOT NULL DEFAULT 0,
                    top_spending_category VARCHAR(100),
                    top_spending_amount FLOAT NOT NULL DEFAULT 0,
                    spending_trend VARCHAR(20),
                    summary TEXT,
                    suggestions TEXT,
                    warnings TEXT,
                    insights TEXT,
                    category_breakdown TEXT,
                    is_read BOOLEAN,
                    created_at DATETIME,
                    FOREIGN KEY(family_id) REFERENCES family (id)
                )
            """))
            connection.execute(text(
                'CREATE INDEX IF NOT EXISTS ix_budget_report_report_date ON budget_report (report_date)'
            ))
            added.append('budget_report')
        for column_name, ddl in expected_expense_columns.items():
            if column_name in columns:
                continue
            connection.execute(text(f'ALTER TABLE expected_expense ADD COLUMN {ddl}'))
            added.append(column_name)
        for column_name, ddl in expense_column_ddls.items():
            if column_name in expense_columns:
                continue
            connection.execute(text(f'ALTER TABLE expense ADD COLUMN {ddl}'))
            added.append(f'expense.{column_name}')
        if 'is_color_auto' not in category_columns:
            connection.execute(text(
                'ALTER TABLE category ADD COLUMN is_color_auto BOOLEAN NOT NULL DEFAULT 0'
            ))
            added.append('category.is_color_auto')

    if added:
        print(f"Added missing schema items: {', '.join(added)}")
    else:
        print('Schema already has the expected columns and tables.')


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

def back_or(endpoint):
    """Redirect back to the submitting page, falling back to a known route."""
    return redirect(request.referrer or url_for(endpoint))


def is_family_manager(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'user_type', None) == 'family_manager':
        return True
    family = getattr(user, 'family', None)
    return bool(family and family.created_by_user_id == user.id)


@app.context_processor
def inject_role_helpers():
    return {'is_family_manager': is_family_manager(current_user)}


def ensure_preset_categories(family_id):
    """Create the recommended starter category set for a family if missing."""
    if not family_id:
        return 0

    existing_names = {
        name.lower()
        for (name,) in Category.query.with_entities(Category.name).filter_by(
            family_id=family_id
        ).all()
    }

    created = 0
    for name, color, monthly_limit in PRESET_CATEGORIES:
        if name.lower() in existing_names:
            continue
        db.session.add(Category(
            name=name,
            color=color,
            monthly_limit=monthly_limit,
            is_fixed=True,
            is_color_auto=False,  # Preset categories have fixed colors
            family_id=family_id
        ))
        created += 1

    return created


def expected_expense_date(expected_expense):
    last_day = calendar.monthrange(expected_expense.year, expected_expense.month)[1]
    due_day = min(max(expected_expense.due_day or 1, 1), last_day)
    return date(expected_expense.year, expected_expense.month, due_day)


def expected_budget_amount(expected_expense):
    """Return the reserved value for buckets and the payable value for bills."""
    if expected_expense.is_bucket:
        return float(expected_expense.allocated_amount or expected_expense.amount or 0)
    return float(expected_expense.amount or 0)


def find_budget_bucket(family_id, category_id, expense_date):
    if not family_id or not category_id or not expense_date:
        return None
    return ExpectedExpense.query.filter_by(
        family_id=family_id,
        category_id=category_id,
        month=expense_date.month,
        year=expense_date.year,
        is_bucket=True
    ).order_by(ExpectedExpense.due_day.asc(), ExpectedExpense.id.asc()).first()


def build_budget_bucket_summaries(buckets, family_id, month_str=None, year_str=None):
    bucket_ids = [bucket.id for bucket in buckets]
    spent_map = {}
    if bucket_ids:
        query = db.session.query(
            Expense.bucket_id,
            func.coalesce(func.sum(Expense.amount), 0).label('total')
        ).filter(
            Expense.bucket_id.in_(bucket_ids),
            Expense.family_id == family_id
        )
        if month_str and year_str:
            query = query.filter(
                func.strftime('%Y', Expense.date) == year_str,
                func.strftime('%m', Expense.date) == month_str
            )
        spent_rows = query.group_by(Expense.bucket_id).all()
        spent_map = {int(bucket_id): float(total) for bucket_id, total in spent_rows}

    summaries = []
    for bucket in buckets:
        allocated = expected_budget_amount(bucket)
        spent = spent_map.get(bucket.id, 0.0)
        remaining = max(allocated - spent, 0.0)
        overflow = max(spent - allocated, 0.0)
        fill_percentage = min((spent / allocated * 100), 100) if allocated else 0
        summaries.append({
            'bucket': bucket,
            'allocated': allocated,
            'spent': spent,
            'remaining': remaining,
            'overflow': overflow,
            'fill_percentage': fill_percentage,
            'is_overflow': overflow > 0
        })

    return summaries


def parse_date_arg(value, fallback=None):
    if not value:
        return fallback
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return fallback


def month_bounds(year, month):
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return first, last


def default_monthly_income(family):
    return float(getattr(family, 'monthly_income', None) or family.monthly_budget or 0)


def monthly_finance_for(family, month, year):
    """Return the income and spending budget for one month, honoring month overrides."""
    setting = MonthlyFinanceSetting.query.filter_by(
        family_id=family.id,
        month=month,
        year=year
    ).first()
    default_income = default_monthly_income(family)
    default_budget = float(family.monthly_budget or 0)
    return {
        'setting': setting,
        'monthly_income': float(setting.monthly_income) if setting and setting.monthly_income is not None else default_income,
        'monthly_budget': float(setting.monthly_budget) if setting and setting.monthly_budget is not None else default_budget,
        'has_override': bool(setting),
        'notes': setting.notes if setting else '',
    }


def finance_setting_snapshot(setting):
    if not setting:
        return {'existed': False}
    return {
        'existed': True,
        'id': setting.id,
        'month': setting.month,
        'year': setting.year,
        'monthly_income': setting.monthly_income,
        'monthly_budget': setting.monthly_budget,
        'notes': setting.notes or '',
    }


def parse_ai_budget_window(form):
    period_type = form.get('period_type', 'date_range')
    today = datetime.now().date()

    if period_type == 'single_date':
        start = parse_date_arg(form.get('single_date'), today)
        end = start
    elif period_type == 'single_month':
        year = int(form.get('single_month_year') or today.year)
        month = int(form.get('single_month') or today.month)
        start, end = month_bounds(year, month)
    elif period_type == 'month_range':
        start_year = int(form.get('start_month_year') or today.year)
        start_month = int(form.get('start_month') or today.month)
        end_year = int(form.get('end_month_year') or start_year)
        end_month = int(form.get('end_month') or start_month)
        start, _ = month_bounds(start_year, start_month)
        _, end = month_bounds(end_year, end_month)
    else:
        start = parse_date_arg(form.get('start_date'), today.replace(day=1))
        end = parse_date_arg(form.get('end_date'), today)

    if end < start:
        start, end = end, start

    target_start = end + timedelta(days=1)
    target_end = target_start + (end - start)
    return period_type, start, end, target_start, target_end


def serialize_expense(expense):
    return {
        'date': expense.date.isoformat(),
        'item_name': expense.item_name,
        'description': expense.description or '',
        'amount': round(float(expense.amount), 2),
        'category': expense.category.name if expense.category else 'Uncategorized',
        'spender': expense.user.username if expense.user else 'Unknown',
    }


def serialize_expected(expense):
    planned_date = expected_expense_date(expense)
    return {
        'name': expense.name,
        'amount': round(float(expense.amount), 2),
        'category': expense.category.name if expense.category else 'Uncategorized',
        'planned_date': planned_date.isoformat(),
        'month': expense.month,
        'year': expense.year,
        'due_day': expense.due_day,
        'is_paid': bool(expense.is_paid),
    }


def summarize_expenses(expenses):
    by_category = {}
    by_day = {}
    by_month = {}

    for expense in expenses:
        category_name = expense.category.name if expense.category else 'Uncategorized'
        day_key = expense.date.isoformat()
        month_key = expense.date.strftime('%Y-%m')
        amount = float(expense.amount)

        by_category[category_name] = by_category.get(category_name, 0.0) + amount
        by_day[day_key] = by_day.get(day_key, 0.0) + amount
        by_month[month_key] = by_month.get(month_key, 0.0) + amount

    total = sum(float(exp.amount) for exp in expenses)
    days = len(by_day) or 1
    return {
        'total_spent': round(total, 2),
        'transaction_count': len(expenses),
        'average_daily_spend': round(total / days, 2),
        'by_category': {key: round(value, 2) for key, value in sorted(by_category.items())},
        'by_day': {key: round(value, 2) for key, value in sorted(by_day.items())},
        'by_month': {key: round(value, 2) for key, value in sorted(by_month.items())},
    }


def build_ai_budget_export(period_type, start, end, target_start, target_end):
    expenses = Expense.query.filter(
        Expense.date >= start,
        Expense.date <= end,
        Expense.family_id == current_user.family_id
    ).order_by(Expense.date.asc(), Expense.id.asc()).all()

    expected_items = [
        item for item in ExpectedExpense.query.filter_by(
            family_id=current_user.family_id
        ).all()
        if start <= expected_expense_date(item) <= end
    ]

    categories = Category.query.filter_by(
        family_id=current_user.family_id
    ).order_by(Category.name.asc()).all()
    target_finance = monthly_finance_for(current_user.family, target_start.month, target_start.year)
    source_finance = monthly_finance_for(current_user.family, start.month, start.year)

    current_target_plan = [
        item for item in ExpectedExpense.query.filter_by(
            family_id=current_user.family_id
        ).all()
        if target_start <= expected_expense_date(item) <= target_end
    ]

    return {
        'vaultsync_ai_budget_export_version': '1.0',
        'generated_at': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
        'currency': 'INR',
        'scope': {
            'period_type': period_type,
            'source_start_date': start.isoformat(),
            'source_end_date': end.isoformat(),
            'target_start_date': target_start.isoformat(),
            'target_end_date': target_end.isoformat(),
        },
        'family_context': {
            'default_monthly_income': default_monthly_income(current_user.family),
            'default_monthly_budget': float(current_user.family.monthly_budget or 0),
            'source_month_income': source_finance['monthly_income'],
            'source_month_budget': source_finance['monthly_budget'],
            'target_month_income': target_finance['monthly_income'],
            'target_month_budget': target_finance['monthly_budget'],
            'category_names': [cat.name for cat in categories],
            'recommended_category_names': [name for name, _, _ in PRESET_CATEGORIES],
        },
        'actual_expenses': [serialize_expense(expense) for expense in expenses],
        'source_planned_expenses': [serialize_expected(item) for item in expected_items],
        'current_target_plan': [serialize_expected(item) for item in current_target_plan],
        'summaries': summarize_expenses(expenses),
        'instructions_for_ai': [
            'Analyze spending patterns, fixed bills, daily habits, spikes, and avoidable categories.',
            'Create a practical budget plan for the target_start_date through target_end_date.',
            'Use existing category_names whenever possible. If a new category is necessary, explain why.',
            'Return only valid JSON matching required_response_schema. No markdown, no prose outside JSON.',
            'Each planned_item date must be inside the target window and amount must be numeric.',
            'recommended_monthly_budget should be the ideal spending budget/limit, not salary income.',
            'If target_month_income is unusually high or low, explain how much should be saved instead of spent.',
        ],
        'required_response_schema': {
            'vaultsync_budget_suggestion_version': '1.0',
            'title': 'string',
            'target_start_date': 'YYYY-MM-DD',
            'target_end_date': 'YYYY-MM-DD',
            'recommended_monthly_budget': 0,
            'risk_level': 'low|medium|high',
            'strategy_notes': ['string'],
            'planned_items': [
                {
                    'date': 'YYYY-MM-DD',
                    'name': 'string',
                    'category': 'string',
                    'amount': 0,
                    'reason': 'string',
                    'priority': 'essential|recommended|optional'
                }
            ],
            'savings_goals': [
                {
                    'name': 'string',
                    'target_amount': 0,
                    'reason': 'string'
                }
            ],
            'guardrails': ['string']
        },
        'example_response': {
            'vaultsync_budget_suggestion_version': '1.0',
            'title': 'Balanced next-period budget',
            'target_start_date': target_start.isoformat(),
            'target_end_date': target_end.isoformat(),
            'recommended_monthly_budget': target_finance['monthly_budget'],
            'risk_level': 'medium',
            'strategy_notes': ['Keep fixed bills early and reduce avoidable snacks/transport spikes.'],
            'planned_items': [
                {
                    'date': target_start.isoformat(),
                    'name': 'Groceries cap',
                    'category': 'Groceries',
                    'amount': 5000,
                    'reason': 'Core household food plan',
                    'priority': 'essential'
                }
            ],
            'savings_goals': [
                {
                    'name': 'Emergency buffer',
                    'target_amount': 3000,
                    'reason': 'Protect against unplanned spend'
                }
            ],
            'guardrails': ['Review optional purchases before payment.']
        }
    }


def budget_suggestion_response_schema(category_names):
    return {
        'type': 'object',
        'properties': {
            'vaultsync_budget_suggestion_version': {'type': 'string'},
            'title': {'type': 'string'},
            'target_start_date': {'type': 'string'},
            'target_end_date': {'type': 'string'},
            'recommended_monthly_budget': {'type': 'number'},
            'risk_level': {'type': 'string', 'enum': ['low', 'medium', 'high']},
            'strategy_notes': {'type': 'array', 'items': {'type': 'string'}},
            'planned_items': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'date': {'type': 'string'},
                        'name': {'type': 'string'},
                        'category': {'type': 'string', 'enum': category_names},
                        'amount': {'type': 'number'},
                        'reason': {'type': 'string'},
                        'priority': {'type': 'string', 'enum': ['essential', 'recommended', 'optional']}
                    },
                    'required': ['date', 'name', 'category', 'amount', 'reason', 'priority']
                }
            },
            'savings_goals': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'name': {'type': 'string'},
                        'target_amount': {'type': 'number'},
                        'reason': {'type': 'string'}
                    },
                    'required': ['name', 'target_amount', 'reason']
                }
            },
            'guardrails': {'type': 'array', 'items': {'type': 'string'}}
        },
        'required': [
            'vaultsync_budget_suggestion_version',
            'title',
            'target_start_date',
            'target_end_date',
            'recommended_monthly_budget',
            'risk_level',
            'strategy_notes',
            'planned_items',
            'savings_goals',
            'guardrails'
        ]
    }


def build_family_ai_plan_context(family_id, target_start, target_end):
    family = db.session.get(Family, family_id)
    if not family:
        raise ValueError('Family not found.')

    today = datetime.now().date()
    source_end = min(max(today, target_start), target_end)
    source_start = target_start
    expenses = Expense.query.filter(
        Expense.family_id == family_id,
        Expense.date >= source_start,
        Expense.date <= source_end
    ).order_by(Expense.date.asc(), Expense.id.asc()).all()
    categories = Category.query.filter_by(
        family_id=family_id
    ).order_by(Category.name.asc()).all()
    current_plan = ExpectedExpense.query.filter_by(
        family_id=family_id,
        month=target_start.month,
        year=target_start.year
    ).order_by(ExpectedExpense.due_day.asc(), ExpectedExpense.amount.desc()).all()
    target_finance = monthly_finance_for(family, target_start.month, target_start.year)

    category_actuals = {}
    for expense in expenses:
        category_name = expense.category.name if expense.category else 'Uncategorized'
        category_actuals[category_name] = category_actuals.get(category_name, 0.0) + float(expense.amount or 0)

    plan_rows = []
    for item in current_plan:
        plan_rows.append({
            'name': item.name,
            'category': item.category.name if item.category else 'Uncategorized',
            'amount': round(expected_budget_amount(item), 2),
            'due_day': item.due_day,
            'is_paid': bool(item.is_paid),
            'is_bucket': bool(item.is_bucket),
        })

    return {
        'family': family,
        'categories': categories,
        'payload': {
            'currency': 'INR',
            'generated_at': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
            'target_start_date': target_start.isoformat(),
            'target_end_date': target_end.isoformat(),
            'monthly_income': round(target_finance['monthly_income'], 2),
            'monthly_budget': round(target_finance['monthly_budget'], 2),
            'category_names': [category.name for category in categories],
            'category_limits': {
                category.name: round(float(category.monthly_limit or 0), 2)
                for category in categories
            },
            'actual_expenses_to_date': [serialize_expense(expense) for expense in expenses],
            'actual_by_category_to_date': {
                key: round(value, 2)
                for key, value in sorted(category_actuals.items())
            },
            'current_plan': plan_rows,
            'instructions_for_ai': [
                'Create an AI suggested category plan for the target month.',
                'Return planned_items as category-level budget numbers, not individual grocery receipts.',
                'Use only category_names provided. Do not invent new categories.',
                'If a current planned bill or reserved bucket is clearly fixed, include enough suggested amount to cover it.',
                'Keep the total practical for the monthly budget/spending limit, not the monthly income.',
                'If income is higher than budget this month, preserve the difference as savings unless a fixed bill requires it.',
                'Return only valid JSON matching the schema.'
            ]
        }
    }


def build_ai_plan_prompt(context_payload):
    return (
        'You are VaultSync AI Suggested Plan.\n'
        'Analyze the report data and assign suggested budget numbers by category.\n'
        'Your output powers an Actual vs Plan vs AI Suggested Plan report.\n\n'
        f'{json.dumps(context_payload, ensure_ascii=False)}\n\n'
        'Important rules:\n'
        '- planned_items should usually contain one row per relevant category.\n'
        '- planned_items.date must be inside the target month.\n'
        '- category must exactly match one provided category name.\n'
        '- amount must be numeric and non-negative.\n'
        '- Use concise reasons that explain why each number was assigned.\n'
    )


def create_ai_suggested_plan(family_id, user_id, target_month=None, target_year=None, source='manual', replace_today=False):
    today = datetime.now().date()
    target_month = target_month or today.month
    target_year = target_year or today.year
    target_start, target_end = month_bounds(target_year, target_month)

    if replace_today:
        start_of_day = datetime.combine(today, datetime.min.time())
        end_of_day = start_of_day + timedelta(days=1)
        existing_today = BudgetSuggestion.query.filter(
            BudgetSuggestion.family_id == family_id,
            BudgetSuggestion.target_start_date == target_start,
            BudgetSuggestion.target_end_date == target_end,
            BudgetSuggestion.title.like('AI Suggested Plan:%'),
            BudgetSuggestion.created_at >= start_of_day,
            BudgetSuggestion.created_at < end_of_day
        ).all()
        for suggestion in existing_today:
            db.session.delete(suggestion)
        if existing_today:
            db.session.flush()

    context = build_family_ai_plan_context(family_id, target_start, target_end)
    categories = context['categories']
    if not categories:
        raise ValueError('Create categories before generating an AI suggested plan.')

    category_names = [category.name for category in categories]
    payload = call_gemini_json(
        build_ai_plan_prompt(context['payload']),
        budget_suggestion_response_schema(category_names)
    )
    payload, parsed_start, parsed_end, total = validate_budget_suggestion_payload(payload)
    if parsed_start != target_start or parsed_end != target_end:
        payload['target_start_date'] = target_start.isoformat()
        payload['target_end_date'] = target_end.isoformat()

    strategy_notes = payload.get('strategy_notes') if isinstance(payload.get('strategy_notes'), list) else []
    guardrails = payload.get('guardrails') if isinstance(payload.get('guardrails'), list) else []
    notes = '\n'.join(str(note) for note in strategy_notes + guardrails)
    source_label = 'Auto' if source == 'auto' else 'Manual'

    suggestion = BudgetSuggestion(
        family_id=family_id,
        created_by_user_id=user_id,
        source_start_date=target_start,
        source_end_date=min(today, target_end),
        target_start_date=target_start,
        target_end_date=target_end,
        title=f'AI Suggested Plan: {calendar.month_name[target_month]} {target_year} ({source_label})',
        suggested_monthly_budget=float(payload.get('recommended_monthly_budget') or 0),
        total_planned=total,
        risk_level=str(payload.get('risk_level') or 'medium')[:20],
        notes=notes,
        raw_json=json.dumps(payload, indent=2),
        created_at=datetime.utcnow()
    )
    db.session.add(suggestion)
    db.session.commit()
    return suggestion


def latest_ai_suggested_plan(family_id, target_start, target_end):
    return BudgetSuggestion.query.filter(
        BudgetSuggestion.family_id == family_id,
        BudgetSuggestion.target_start_date <= target_end,
        BudgetSuggestion.target_end_date >= target_start,
        BudgetSuggestion.title.like('AI Suggested Plan:%')
    ).order_by(BudgetSuggestion.created_at.desc(), BudgetSuggestion.id.desc()).first()


def suggestion_category_totals(suggestion, target_start, target_end):
    if not suggestion:
        return {}
    try:
        payload = suggestion_payload(suggestion)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}

    totals = {}
    for item in payload.get('planned_items') or []:
        item_date = parse_date_arg(item.get('date'))
        if item_date and not (target_start <= item_date <= target_end):
            continue
        category_name = str(item.get('category') or '').strip()
        if not category_name:
            continue
        try:
            amount = float(item.get('amount') or 0)
        except (TypeError, ValueError):
            amount = 0
        totals[category_name] = totals.get(category_name, 0.0) + max(amount, 0.0)
    return totals


def validate_budget_suggestion_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError('AI response must be a JSON object.')

    planned_items = payload.get('planned_items')
    if not isinstance(planned_items, list) or not planned_items:
        raise ValueError('AI response must include at least one planned_items entry.')

    target_start = parse_date_arg(payload.get('target_start_date'))
    target_end = parse_date_arg(payload.get('target_end_date'))
    if not target_start or not target_end or target_end < target_start:
        raise ValueError('AI response must include a valid target date range.')

    total = 0.0
    normalized_items = []
    for item in planned_items:
        item_date = parse_date_arg(item.get('date'))
        if not item_date or not (target_start <= item_date <= target_end):
            raise ValueError('Every planned item date must be inside the target range.')

        try:
            amount = float(item.get('amount'))
        except (TypeError, ValueError):
            raise ValueError('Every planned item must include a numeric amount.')

        if amount <= 0:
            raise ValueError('Planned item amounts must be positive.')

        name = str(item.get('name', '')).strip()
        category = str(item.get('category', '')).strip()
        if not name or not category:
            raise ValueError('Every planned item needs a name and category.')

        normalized_items.append({
            **item,
            'date': item_date.isoformat(),
            'name': name,
            'category': category,
            'amount': round(amount, 2),
            'priority': str(item.get('priority', 'recommended')).strip() or 'recommended',
            'reason': str(item.get('reason', '')).strip(),
        })
        total += amount

    payload['planned_items'] = normalized_items
    normalized_goals = []
    for goal in payload.get('savings_goals') or []:
        if not isinstance(goal, dict):
            continue
        try:
            target_amount = float(goal.get('target_amount') or 0)
        except (TypeError, ValueError):
            target_amount = 0.0
        normalized_goals.append({
            'name': str(goal.get('name', 'Savings Goal')).strip() or 'Savings Goal',
            'target_amount': round(max(target_amount, 0.0), 2),
            'reason': str(goal.get('reason', '')).strip(),
        })
    payload['savings_goals'] = normalized_goals
    payload['target_start_date'] = target_start.isoformat()
    payload['target_end_date'] = target_end.isoformat()
    payload['total_planned'] = round(total, 2)
    return payload, target_start, target_end, total


def suggestion_payload(suggestion):
    return json.loads(suggestion.raw_json)


def current_plan_between(start, end):
    return [
        item for item in ExpectedExpense.query.filter_by(
            family_id=current_user.family_id
        ).all()
        if start <= expected_expense_date(item) <= end
    ]


def compare_suggestion_to_current(suggestion):
    payload = suggestion_payload(suggestion)
    current_plan = current_plan_between(suggestion.target_start_date, suggestion.target_end_date)

    current_total = sum(float(item.amount) for item in current_plan)
    suggested_total = float(payload.get('total_planned') or suggestion.total_planned or 0)
    current_by_category = {}
    suggested_by_category = {}

    for item in current_plan:
        category_name = item.category.name if item.category else 'Uncategorized'
        current_by_category[category_name] = current_by_category.get(category_name, 0.0) + float(item.amount)

    for item in payload.get('planned_items', []):
        category_name = item['category']
        suggested_by_category[category_name] = suggested_by_category.get(category_name, 0.0) + float(item['amount'])

    category_rows = []
    for category_name in sorted(set(current_by_category) | set(suggested_by_category)):
        current_amount = current_by_category.get(category_name, 0.0)
        suggested_amount = suggested_by_category.get(category_name, 0.0)
        category_rows.append({
            'category': category_name,
            'current': round(current_amount, 2),
            'suggested': round(suggested_amount, 2),
            'delta': round(suggested_amount - current_amount, 2),
        })

    finance = monthly_finance_for(
        current_user.family,
        suggestion.target_start_date.month,
        suggestion.target_start_date.year
    )
    monthly_budget = finance['monthly_budget']
    suggested_monthly_budget = float(payload.get('recommended_monthly_budget') or 0)

    return {
        'payload': payload,
        'current_plan': current_plan,
        'current_total': round(current_total, 2),
        'suggested_total': round(suggested_total, 2),
        'plan_delta': round(suggested_total - current_total, 2),
        'current_monthly_budget': round(monthly_budget, 2),
        'current_monthly_income': round(finance['monthly_income'], 2),
        'suggested_monthly_budget': round(suggested_monthly_budget, 2),
        'budget_delta': round(suggested_monthly_budget - monthly_budget, 2),
        'category_rows': category_rows,
    }


def chunks(items, size):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def parse_gemini_json_text(response_payload):
    candidates = response_payload.get('candidates') or []
    if not candidates:
        raise ValueError('Gemini returned no candidates.')

    parts = (candidates[0].get('content') or {}).get('parts') or []
    text_parts = [part.get('text', '') for part in parts if part.get('text')]
    if not text_parts:
        raise ValueError('Gemini returned no JSON text.')

    raw_text = ''.join(text_parts).strip()
    if raw_text.startswith('```'):
        raw_text = raw_text.strip('`')
        if raw_text.lower().startswith('json'):
            raw_text = raw_text[4:].strip()

    return json.loads(raw_text)


def configured_gemini_keys():
    placeholder_values = {
        'your-gemini-api-key-here',
        'key-one',
        'key-two',
        'key-three',
    }

    def valid_key(value):
        if not value:
            return False
        normalized = value.strip()
        if not normalized or normalized in placeholder_values:
            return False
        if normalized.startswith('your-'):
            return False
        return normalized.startswith('AIza')

    keys = []
    for env_name in ('GEMINI_API_KEY', 'GEMINI_API_KEY_1', 'GEMINI_API_KEY_2', 'GEMINI_API_KEY_3'):
        key = os.environ.get(env_name, '').strip()
        if valid_key(key) and key not in keys:
            keys.append(key)

    combined = os.environ.get('GEMINI_API_KEYS', '').strip()
    if combined:
        for key in combined.replace('\n', ',').split(','):
            key = key.strip()
            if valid_key(key) and key not in keys:
                keys.append(key)

    return keys


def gemini_configured():
    return bool(configured_gemini_keys())


def next_gemini_key_order():
    keys = configured_gemini_keys()
    if not keys:
        return []
    global _gemini_key_index
    with _gemini_key_lock:
        start = _gemini_key_index % len(keys)
        _gemini_key_index = (_gemini_key_index + 1) % len(keys)
    return keys[start:] + keys[:start]


def should_try_next_gemini_key(exc):
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {400, 401, 403, 408, 409, 429, 500, 502, 503, 504}
    return False


def call_gemini_json(prompt, response_schema):
    api_keys = next_gemini_key_order()
    if not api_keys:
        raise ValueError('No Gemini API key is set. Configure GEMINI_API_KEY, GEMINI_API_KEY_1, GEMINI_API_KEY_2, GEMINI_API_KEY_3, or GEMINI_API_KEYS.')

    url = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent'
    payload = {
        'contents': [
            {
                'parts': [
                    {'text': prompt}
                ]
            }
        ],
        'generationConfig': {
            'temperature': 0.1,
            'responseMimeType': 'application/json',
            'responseJsonSchema': response_schema,
        }
    }
    body = json.dumps(payload).encode('utf-8')
    errors = []

    for index, api_key in enumerate(api_keys, start=1):
        request_obj = urllib.request.Request(
            url,
            data=body,
            headers={
                'Content-Type': 'application/json',
                'x-goog-api-key': api_key,
            },
            method='POST'
        )

        try:
            with urllib.request.urlopen(request_obj, timeout=75) as response:
                response_payload = json.loads(response.read().decode('utf-8'))
            return parse_gemini_json_text(response_payload)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode('utf-8', errors='replace')
            errors.append(f'key {index}: HTTP {exc.code} {error_body[:220]}')
            if not should_try_next_gemini_key(exc):
                break
        except urllib.error.URLError as exc:
            errors.append(f'key {index}: network {exc.reason}')
            if not should_try_next_gemini_key(exc):
                break
        except json.JSONDecodeError as exc:
            errors.append(f'key {index}: invalid JSON response {exc}')
            break

    raise ValueError('Gemini API failed for all configured keys: ' + ' | '.join(errors))


CHATBOT_FORBIDDEN_TERMS = {
    'password', 'email', 'login', 'logout', 'role', 'promote', 'demote',
    'member', 'user', 'invite', 'archive family', 'delete family',
    'admin', 'security', 'permission', 'account', 'active',
}


def chatbot_response_schema():
    return {
        'type': 'object',
        'properties': {
            'reply': {'type': 'string'},
            'actions': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'action': {
                            'type': 'string',
                            'enum': [
                                'create_expense',
                                'update_expense',
                                'delete_expense',
                                'create_category',
                                'update_category',
                                'delete_category',
                                'create_planned_expense',
                                'update_planned_expense',
                                'delete_planned_expense',
                                'mark_planned_paid',
                                'generate_daily_report',
                            ],
                        },
                        'id': {'type': 'integer'},
                        'item_name': {'type': 'string'},
                        'description': {'type': 'string'},
                        'amount': {'type': 'number'},
                        'date': {'type': 'string'},
                        'category': {'type': 'string'},
                        'name': {'type': 'string'},
                        'color': {'type': 'string'},
                        'monthly_limit': {'type': 'number'},
                        'month': {'type': 'integer'},
                        'year': {'type': 'integer'},
                        'due_day': {'type': 'integer'},
                        'is_bucket': {'type': 'boolean'},
                    },
                    'required': ['action'],
                },
            },
        },
        'required': ['reply', 'actions'],
    }


def parse_chatbot_date(value, default_date=None):
    if not value:
        return default_date or datetime.utcnow().date()
    return datetime.strptime(str(value), '%Y-%m-%d').date()


def recent_chatbot_messages(user, limit=12):
    messages = ChatbotMessage.query.filter_by(
        family_id=user.family_id,
        user_id=user.id
    ).order_by(ChatbotMessage.created_at.desc(), ChatbotMessage.id.desc()).limit(limit).all()
    return list(reversed(messages))


def serialize_chatbot_message(message):
    return {
        'id': message.id,
        'role': message.role,
        'content': message.content,
        'actions': fromjson_filter(message.actions_json),
        'results': fromjson_filter(message.results_json),
        'created_at': message.created_at.isoformat() if message.created_at else None,
    }


def save_chatbot_message(role, content, actions=None, results=None):
    if not current_user.is_authenticated or not current_user.family_id:
        return None
    message = ChatbotMessage(
        family_id=current_user.family_id,
        user_id=current_user.id,
        role=role,
        content=str(content or '')[:6000],
        actions_json=json.dumps(actions or []),
        results_json=json.dumps(results or []),
    )
    db.session.add(message)
    return message


def chatbot_context_payload(user):
    today = datetime.utcnow().date()
    month_start = today.replace(day=1)
    categories = Category.query.filter_by(family_id=user.family_id).order_by(Category.name.asc()).all()
    recent_expenses = Expense.query.filter_by(
        family_id=user.family_id
    ).order_by(Expense.date.desc(), Expense.id.desc()).limit(20).all()
    planned_items = ExpectedExpense.query.filter_by(
        family_id=user.family_id,
        month=today.month,
        year=today.year
    ).order_by(ExpectedExpense.due_day.asc()).limit(30).all()

    return {
        'today': today.isoformat(),
        'current_month_start': month_start.isoformat(),
        'user': {
            'id': user.id,
            'username': user.username,
            'role': user.user_type,
            'is_family_manager': is_family_manager(user),
        },
        'family': {
            'id': user.family_id,
            'name': user.family.name if user.family else '',
            'monthly_budget': user.family.monthly_budget if user.family else 0,
        },
        'categories': [
            {
                'id': category.id,
                'name': category.name,
                'monthly_limit': round(float(category.monthly_limit or 0), 2),
            }
            for category in categories
        ],
        'recent_expenses': [
            {
                'id': expense.id,
                'date': expense.date.isoformat(),
                'item_name': expense.item_name,
                'amount': round(float(expense.amount or 0), 2),
                'category': expense.category.name if expense.category else '',
                'spender': expense.user.username if expense.user else '',
                'is_mine': expense.user_id == user.id,
            }
            for expense in recent_expenses
        ],
        'planned_this_month': [
            {
                'id': item.id,
                'name': item.name,
                'amount': round(float(item.amount or 0), 2),
                'category': item.category.name if item.category else '',
                'month': item.month,
                'year': item.year,
                'due_day': item.due_day,
                'is_bucket': bool(item.is_bucket),
                'is_paid': bool(item.is_paid),
            }
            for item in planned_items
        ],
        'recent_chat_history': [
            serialize_chatbot_message(message)
            for message in recent_chatbot_messages(user, limit=12)
        ],
    }


def build_chatbot_prompt(message, context_payload):
    return (
        'You are VaultSync Assistant inside a family expense manager.\n'
        'Answer questions and, when the user asks to change site data, return structured actions.\n'
        'Never perform or suggest security/account/admin changes: passwords, login, email, roles, users, members, invites, permissions, family archive/delete.\n'
        'Do not invent IDs. Use IDs only from context. If an edit/delete target is ambiguous, ask a short clarifying question and return no actions.\n'
        'Use recent_chat_history to understand follow-up requests, but current site data in context is the source of truth for IDs and amounts.\n'
        'For new expenses, you MUST handle messy, unstructured, natural human language lists seamlessly. Intelligently organize multiple expenses, fix typos, and categorize them accurately.\n'
        'Parse any relative dates (like "yesterday", "last week", "3 days ago") based on the current date provided in Context JSON. Output dates precisely in YYYY-MM-DD format.\n'
        'If a requested category is missing, choose the closest existing category and note it in your reply.\n'
        'Family managers can manage planned expenses and reports. Normal members can manage only their own expenses plus categories.\n\n'
        f'Context JSON:\n{json.dumps(context_payload, ensure_ascii=False)}\n\n'
        f'User message:\n{message}'
    )


def find_chatbot_category(family_id, category_name):
    if not category_name:
        return None
    normalized = str(category_name).strip().lower()
    return Category.query.filter(
        Category.family_id == family_id,
        func.lower(Category.name) == normalized
    ).first()


def assert_chatbot_manager_action():
    if not is_family_manager(current_user):
        raise ValueError('Only a family manager can do that.')


def execute_chatbot_action(action_payload):
    action = action_payload.get('action')
    family_id = current_user.family_id
    if any(term in json.dumps(action_payload).lower() for term in CHATBOT_FORBIDDEN_TERMS):
        raise ValueError('Blocked a security or account-level change.')

    if action == 'create_expense':
        item_name = str(action_payload.get('item_name') or action_payload.get('name') or '').strip()
        amount = float(action_payload.get('amount') or 0)
        category = find_chatbot_category(family_id, action_payload.get('category'))
        expense_date = parse_chatbot_date(action_payload.get('date'))
        if not item_name or amount <= 0 or not category:
            raise ValueError('Expense needs an item, positive amount, and valid category.')
        bucket = find_budget_bucket(family_id, category.id, expense_date)
        db.session.add(Expense(
            date=expense_date,
            item_name=item_name[:100],
            description=str(action_payload.get('description') or '')[:1000],
            amount=amount,
            category_id=category.id,
            user_id=current_user.id,
            family_id=family_id,
            bucket_id=bucket.id if bucket else None,
        ))
        return f'Added expense "{item_name}" for Rs. {amount:,.0f}.'

    if action in {'update_expense', 'delete_expense'}:
        expense = Expense.query.filter_by(id=action_payload.get('id'), family_id=family_id).first()
        if not expense:
            raise ValueError('Expense not found.')
        if expense.user_id != current_user.id and not is_family_manager(current_user):
            raise ValueError('You can only change your own expenses.')
        if action == 'delete_expense':
            linked_plan = ExpectedExpense.query.filter_by(linked_expense_id=expense.id, family_id=family_id).first()
            if linked_plan:
                linked_plan.is_paid = False
                linked_plan.paid_at = None
                linked_plan.linked_expense_id = None
            db.session.delete(expense)
            return f'Deleted expense "{expense.item_name}".'
        if action_payload.get('item_name') or action_payload.get('name'):
            expense.item_name = str(action_payload.get('item_name') or action_payload.get('name')).strip()[:100]
        if action_payload.get('description') is not None:
            expense.description = str(action_payload.get('description') or '')[:1000]
        if action_payload.get('amount') is not None:
            amount = float(action_payload.get('amount') or 0)
            if amount <= 0:
                raise ValueError('Expense amount must be positive.')
            expense.amount = amount
        if action_payload.get('date'):
            expense.date = parse_chatbot_date(action_payload.get('date'))
        if action_payload.get('category'):
            category = find_chatbot_category(family_id, action_payload.get('category'))
            if not category:
                raise ValueError('Category not found.')
            expense.category_id = category.id
            expense.bucket_id = (find_budget_bucket(family_id, category.id, expense.date) or type('Empty', (), {'id': None})()).id
        return f'Updated expense "{expense.item_name}".'

    if action == 'create_category':
        name = str(action_payload.get('name') or action_payload.get('category') or '').strip()
        if not name:
            raise ValueError('Category needs a name.')
        if Category.query.filter_by(name=name, family_id=family_id).first():
            raise ValueError('That category already exists.')
        monthly_limit = float(action_payload.get('monthly_limit') or 0)
        color = str(action_payload.get('color') or '').strip() or get_next_auto_color(family_id)
        db.session.add(Category(
            name=name[:50],
            color=color,
            monthly_limit=max(monthly_limit, 0),
            is_color_auto=not bool(action_payload.get('color')),
            family_id=family_id,
        ))
        return f'Created category "{name}".'

    if action in {'update_category', 'delete_category'}:
        category_id = action_payload.get('id')
        category = Category.query.filter_by(id=category_id, family_id=family_id).first()
        if not category and action_payload.get('category'):
            category = find_chatbot_category(family_id, action_payload.get('category'))
        if not category:
            raise ValueError('Category not found.')
        if action == 'delete_category':
            if Expense.query.filter_by(category_id=category.id, family_id=family_id).count():
                raise ValueError('I will not delete a category that still has expenses. Move those expenses first.')
            if ExpectedExpense.query.filter_by(category_id=category.id, family_id=family_id).count():
                raise ValueError('I will not delete a category that still has planned items. Move those planned items first.')
            db.session.delete(category)
            return f'Deleted category "{category.name}".'
        if action_payload.get('name'):
            category.name = str(action_payload.get('name')).strip()[:50]
        if action_payload.get('color'):
            category.color = str(action_payload.get('color')).strip()
            category.is_color_auto = False
        if action_payload.get('monthly_limit') is not None:
            category.monthly_limit = max(float(action_payload.get('monthly_limit') or 0), 0)
        return f'Updated category "{category.name}".'

    if action in {'create_planned_expense', 'update_planned_expense', 'delete_planned_expense', 'mark_planned_paid', 'generate_daily_report'}:
        assert_chatbot_manager_action()

    if action == 'create_planned_expense':
        name = str(action_payload.get('name') or action_payload.get('item_name') or '').strip()
        amount = float(action_payload.get('amount') or 0)
        category = find_chatbot_category(family_id, action_payload.get('category'))
        month = int(action_payload.get('month') or datetime.utcnow().month)
        year = int(action_payload.get('year') or datetime.utcnow().year)
        due_day = int(action_payload.get('due_day') or 1)
        if not name or amount <= 0 or not category or not (1 <= month <= 12) or not (1 <= due_day <= 31):
            raise ValueError('Planned expense needs name, amount, category, month, year, and due day.')
        db.session.add(ExpectedExpense(
            name=name[:150],
            amount=amount,
            is_bucket=bool(action_payload.get('is_bucket')),
            allocated_amount=amount if action_payload.get('is_bucket') else 0.0,
            due_day=due_day,
            category_id=category.id,
            family_id=family_id,
            month=month,
            year=year,
        ))
        return f'Created planned item "{name}".'

    if action in {'update_planned_expense', 'delete_planned_expense', 'mark_planned_paid'}:
        planned = ExpectedExpense.query.filter_by(id=action_payload.get('id'), family_id=family_id).first()
        if not planned:
            raise ValueError('Planned item not found.')
        if action == 'delete_planned_expense':
            if planned.linked_expense_id:
                linked = Expense.query.filter_by(id=planned.linked_expense_id, family_id=family_id).first()
                if linked:
                    db.session.delete(linked)
            db.session.delete(planned)
            return f'Deleted planned item "{planned.name}".'
        if action == 'mark_planned_paid':
            if planned.is_bucket:
                raise ValueError('Buckets are drawn down by linked expenses, not marked paid.')
            if not planned.is_paid:
                linked = Expense(
                    date=expected_expense_date(planned),
                    item_name=f'Budget Plan: {planned.name}',
                    description='Posted by VaultSync Assistant.',
                    amount=planned.amount,
                    category_id=planned.category_id,
                    user_id=current_user.id,
                    family_id=family_id,
                )
                db.session.add(linked)
                db.session.flush()
                planned.is_paid = True
                planned.paid_at = datetime.utcnow()
                planned.linked_expense_id = linked.id
            return f'Marked "{planned.name}" paid.'
        if action_payload.get('name'):
            planned.name = str(action_payload.get('name')).strip()[:150]
        if action_payload.get('amount') is not None:
            amount = float(action_payload.get('amount') or 0)
            if amount <= 0:
                raise ValueError('Planned amount must be positive.')
            planned.amount = amount
            planned.allocated_amount = amount if planned.is_bucket else 0.0
        if action_payload.get('category'):
            category = find_chatbot_category(family_id, action_payload.get('category'))
            if not category:
                raise ValueError('Category not found.')
            planned.category_id = category.id
        if action_payload.get('month') is not None:
            planned.month = int(action_payload.get('month'))
        if action_payload.get('year') is not None:
            planned.year = int(action_payload.get('year'))
        if action_payload.get('due_day') is not None:
            planned.due_day = int(action_payload.get('due_day'))
        return f'Updated planned item "{planned.name}".'

    if action == 'generate_daily_report':
        report_date = parse_chatbot_date(action_payload.get('date'))
        report = create_daily_budget_report(family_id, report_date)
        return f'Generated report for {report.report_date.isoformat()}.'

    raise ValueError(f'Unsupported action: {action}')


def build_expense_organization_prompt(expenses, categories, exceptions=None):
    """
    Build the prompt for AI expense organization.
    
    Args:
        expenses: List of Expense objects to organize
        categories: List of Category objects
        exceptions: List of AIExpenseException objects describing rules to exclude expenses
    """
    exception_context = ''
    if exceptions:
        exception_rules = []
        for exc in exceptions:
            if not exc.is_active:
                continue
            rule_desc = f"- {exc.description}: "
            if exc.match_keywords:
                rule_desc += f"Exclude if item_name contains any of: {exc.match_keywords}. "
            if exc.match_description_contains:
                rule_desc += f"Exclude if description contains any of: {exc.match_description_contains}. "
            exception_rules.append(rule_desc)
        
        if exception_rules:
            exception_context = (
                '\nIMPORTANT EXCLUSION RULES:\n'
                'Some expenses should be excluded from organization (e.g., separate household members with different budgets).\n'
                + '\n'.join(exception_rules) +
                '\nIf an expense matches any exclusion rule, set its confidence to 0 and reason to "Excluded by exception rule".\n'
            )
    
    expense_rows = [
        {
            'expense_id': expense.id,
            'date': expense.date.isoformat(),
            'item_name': expense.item_name,
            'description': expense.description or '',
            'amount': round(float(expense.amount), 2),
            'current_category': expense.category.name if expense.category else '',
            'spender': expense.user.username if expense.user else 'Unknown',
        }
        for expense in expenses
    ]
    category_rows = [
        {
            'category_id': category.id,
            'name': category.name,
            'monthly_limit': round(float(category.monthly_limit or 0), 2),
            'is_preset': bool(category.is_fixed),
        }
        for category in categories
    ]

    return (
        'You organize household expenses for VaultSync.\n'
        'Choose exactly one category for each expense from the provided categories. '
        'Use item_name and description as the main evidence. Treat current_category as a weak hint only. '
        'Never invent a category. If uncertain, choose the closest existing category and use lower confidence.\n'
        f'{exception_context}\n'
        f'Categories:\n{json.dumps(category_rows, ensure_ascii=False)}\n\n'
        f'Expenses:\n{json.dumps(expense_rows, ensure_ascii=False)}\n\n'
        'Return only JSON matching the schema.'
    )


def expense_organization_schema(category_names):
    return {
        'type': 'object',
        'properties': {
            'classifications': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'expense_id': {'type': 'integer'},
                        'category_name': {'type': 'string', 'enum': category_names},
                        'confidence': {'type': 'number'},
                        'reason': {'type': 'string'},
                    },
                    'required': ['expense_id', 'category_name', 'confidence', 'reason']
                }
            }
        },
        'required': ['classifications']
    }


def organize_expenses_for_family(family_id, start_date, end_date):
    from models import AIExpenseException
    
    categories = Category.query.filter_by(
        family_id=family_id
    ).order_by(Category.name.asc()).all()
    if not categories:
        return {
            'scanned': 0,
            'changed': 0,
            'unchanged': 0,
            'skipped': 0,
            'message': 'No categories available.'
        }

    expenses = Expense.query.filter(
        Expense.family_id == family_id,
        Expense.date >= start_date,
        Expense.date <= end_date
    ).order_by(Expense.date.asc(), Expense.id.asc()).all()
    if not expenses:
        return {
            'scanned': 0,
            'changed': 0,
            'unchanged': 0,
            'skipped': 0,
            'message': 'No expenses found in that date range.'
        }

    category_by_name = {category.name: category for category in categories}
    
    # Load active exceptions for this family
    exceptions = AIExpenseException.query.filter_by(
        family_id=family_id,
        is_active=True
    ).all()
    
    linked_plans = {
        plan.linked_expense_id: plan
        for plan in ExpectedExpense.query.filter(
            ExpectedExpense.family_id == family_id,
            ExpectedExpense.linked_expense_id.in_([expense.id for expense in expenses])
        ).all()
        if plan.linked_expense_id
    }

    changed = 0
    unchanged = 0
    skipped = 0
    excluded = 0
    reasons = []

    for batch in chunks(expenses, AI_ORGANIZER_BATCH_SIZE):
        prompt = build_expense_organization_prompt(batch, categories, exceptions)
        payload = call_gemini_json(prompt, expense_organization_schema(list(category_by_name.keys())))
        classifications = payload.get('classifications') or []
        classification_by_id = {
            int(item.get('expense_id')): item
            for item in classifications
            if item.get('expense_id') is not None
        }

        for expense in batch:
            classification = classification_by_id.get(expense.id)
            if not classification:
                skipped += 1
                continue

            # Check if AI marked this as excluded (confidence 0 means exception rule matched)
            confidence = float(classification.get('confidence') or 1.0)
            reason = str(classification.get('reason') or '')
            if confidence == 0 or 'exception rule' in reason.lower():
                excluded += 1
                continue

            category = category_by_name.get(str(classification.get('category_name') or '').strip())
            if not category:
                skipped += 1
                continue

            if expense.category_id == category.id:
                unchanged += 1
                continue

            old_category = expense.category.name if expense.category else 'Uncategorized'
            expense.category_id = category.id
            linked_plan = linked_plans.get(expense.id)
            if linked_plan:
                linked_plan.category_id = category.id
                expense.bucket_id = None
            else:
                bucket = find_budget_bucket(family_id, category.id, expense.date)
                expense.bucket_id = bucket.id if bucket else None

            changed += 1
            if len(reasons) < 5:
                reasons.append(
                    f"{expense.item_name}: {old_category} -> {category.name} "
                    f"({reason})"
                )

    db.session.commit()
    return {
        'scanned': len(expenses),
        'changed': changed,
        'unchanged': unchanged,
        'skipped': skipped,
        'excluded': excluded,
        'message': '; '.join(reasons)
    }


def average(values):
    values = [float(value or 0) for value in values]
    return sum(values) / len(values) if values else 0.0


def percentile(values, pct):
    values = sorted(float(value or 0) for value in values)
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * pct
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[int(position)]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def add_months(source_date, months):
    month_index = source_date.year * 12 + source_date.month - 1 + months
    year = month_index // 12
    month = month_index % 12 + 1
    day = min(source_date.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def inclusive_dates(start, end):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def historical_month_windows(month_start, count=6):
    windows = []
    for offset in range(count, 0, -1):
        historical_start = add_months(month_start, -offset)
        historical_end = date(
            historical_start.year,
            historical_start.month,
            calendar.monthrange(historical_start.year, historical_start.month)[1]
        )
        windows.append((historical_start, historical_end))
    return windows


def normalize_forecast_note_text(value):
    return re.sub(r'[^a-z0-9]+', ' ', str(value or '').lower()).strip()


def forecast_category_aliases(category_name):
    normalized = normalize_forecast_note_text(category_name)
    words = set(normalized.split())
    aliases = set(words)

    if {'rent', 'emi'} & words or 'rent emi' in normalized:
        aliases.update({'rent', 'rents', 'emi', 'emis', 'loan', 'loans', 'installment', 'installments'})
    if 'insurance' in words:
        aliases.update({'insurance', 'insurances', 'policy', 'policies', 'premium', 'premiums'})

    return {alias for alias in aliases if len(alias) >= 3}


def is_automatic_current_only_forecast_category(category):
    normalized = normalize_forecast_note_text(category.name)
    words = set(normalized.split())
    fixed_bill_words = {
        'emi', 'emis', 'insurance', 'insurances', 'loan', 'loans',
        'mortgage', 'rent', 'rents'
    }
    fixed_bill_phrases = (
        'rent emi',
        'loan repayment',
        'loan payoff',
        'insurance premium',
        'car emi',
        'home loan',
    )
    return bool(words & fixed_bill_words) or any(phrase in normalized for phrase in fixed_bill_phrases)


def forecast_expense_text(expense):
    category_name = expense.category.name if expense.category else ''
    return normalize_forecast_note_text(f'{expense.item_name} {expense.description or ""} {category_name}')


def forecast_text_has_any(text, terms):
    words = set(text.split())
    return bool(words & set(terms)) or any(' ' in term and term in text for term in terms)


def classify_forecast_expense(expense, locked_categories, category_month_medians=None):
    category_name = expense.category.name if expense.category else 'Uncategorized'
    amount = float(expense.amount or 0)
    text = forecast_expense_text(expense)
    words = set(text.split())

    if category_name in locked_categories:
        return 'fixed_bill'

    one_time_terms = {
        'admission', 'advance', 'appliance', 'birthday', 'ceremony', 'deposit',
        'eid', 'emergency', 'event', 'festival', 'festivals', 'fine', 'flight',
        'function', 'functions', 'furniture', 'gift', 'hospital', 'laptop',
        'maintenance', 'party', 'penalty', 'purchase', 'registration', 'repair',
        'surgery', 'tax', 'ticket', 'trip', 'vacation', 'wedding'
    }
    regular_terms = {
        'bill', 'bills', 'daily', 'diesel', 'electricity', 'fuel', 'gas',
        'groceries', 'grocery', 'medicine', 'milk', 'monthly', 'petrol',
        'pharmacy', 'regular', 'subscription', 'utility', 'vegetable',
        'vegetables', 'weekly'
    }

    if words & one_time_terms or forecast_text_has_any(text, one_time_terms):
        return 'one_time_keyword'

    if words & regular_terms or forecast_text_has_any(text, regular_terms):
        return 'regular'

    category_month_medians = category_month_medians or {}
    category_median = float(category_month_medians.get(category_name) or 0)
    if category_median > 0 and amount >= max(5000, category_median * 0.65):
        return 'one_time_outlier'
    if amount >= 15000:
        return 'one_time_large'

    return 'regular'


def serialize_forecast_expense_classification(expense, classification):
    return {
        'id': expense.id,
        'date': expense.date.isoformat(),
        'name': expense.item_name,
        'category': expense.category.name if expense.category else 'Uncategorized',
        'amount': round(float(expense.amount or 0), 2),
        'classification': classification,
    }


def extract_forecast_exception_tokens(normalized_notes):
    tokens = set()
    generic_words = {
        'and', 'are', 'bill', 'bills', 'calculate', 'current', 'done', 'emi', 'emis',
        'except', 'for', 'insurance', 'month', 'one', 'other', 'paid', 'payment',
        'payments', 'policy', 'premium', 'rent', 'rents', 'than', 'this'
    }

    for match in re.finditer(r'\bexcept(?:\s+for)?\s+(.+?)(?:\bso\b|\bbut\b|\band\b|[.;,\n]|$)', normalized_notes):
        phrase = match.group(1)
        for token in phrase.split()[:6]:
            if len(token) >= 3 and token not in generic_words:
                tokens.add(token)

    return sorted(tokens)


def build_forecast_note_rules(admin_notes, categories):
    normalized_notes = normalize_forecast_note_text(admin_notes)
    note_words = set(normalized_notes.split())
    done_words = {'cleared', 'completed', 'done', 'finished', 'paid', 'settled'}
    current_only_phrases = (
        'current one',
        'current only',
        'dont calculate',
        'do not calculate',
        'don t calculate',
        'other than current',
        'already paid',
        'already done',
    )
    has_done_rule = bool(done_words & note_words) or any(phrase in normalized_notes for phrase in current_only_phrases)
    exception_tokens = extract_forecast_exception_tokens(normalized_notes)
    admin_current_only_categories = []

    if has_done_rule:
        for category in categories:
            aliases = forecast_category_aliases(category.name)
            if aliases & note_words:
                admin_current_only_categories.append(category.name)

    automatic_current_only_categories = [
        category.name for category in categories
        if is_automatic_current_only_forecast_category(category)
    ]
    current_only_categories = sorted(set(admin_current_only_categories + automatic_current_only_categories))

    return {
        'current_only_categories': current_only_categories,
        'admin_current_only_categories': sorted(set(admin_current_only_categories)),
        'automatic_current_only_categories': sorted(set(automatic_current_only_categories)),
        'exception_tokens': exception_tokens,
        'admin_notes_understood': bool(admin_current_only_categories or exception_tokens),
        'automatic_rules_applied': bool(automatic_current_only_categories),
    }


def forecast_item_matches_exception(item, exception_tokens):
    if not exception_tokens:
        return False
    category_name = item.category.name if item.category else ''
    item_text = normalize_forecast_note_text(f'{item.name} {category_name}')
    item_words = set(item_text.split())
    return any(token in item_words for token in exception_tokens)


def should_include_forecast_commitment(item, note_rules):
    category_name = item.category.name if item.category else 'Uncategorized'
    admin_current_only_categories = set(note_rules.get('admin_current_only_categories') or [])
    if category_name not in admin_current_only_categories:
        return True
    return forecast_item_matches_exception(item, note_rules.get('exception_tokens') or [])


def serialize_forecast_expected_item(item, reason=None):
    payload = {
        'name': item.name,
        'category': item.category.name if item.category else 'Uncategorized',
        'amount': round(expected_budget_amount(item), 2),
        'due_date': expected_expense_date(item).isoformat(),
        'type': 'reserved_bucket' if item.is_bucket else 'planned_bill',
    }
    if reason:
        payload['reason'] = reason
    return payload


def build_forecast_engine(family_id, month_start, today, month_end, monthly_budget, categories, expected_items, bucket_summaries, admin_notes=''):
    days_in_month = (month_end - month_start).days + 1
    elapsed_days = max((today - month_start).days + 1, 1)
    remaining_days = max((month_end - today).days, 0)
    lookback_start = add_months(month_start, -6)
    note_rules = build_forecast_note_rules(admin_notes, categories)

    expenses = Expense.query.filter(
        Expense.family_id == family_id,
        Expense.date >= lookback_start,
        Expense.date <= today
    ).order_by(Expense.date.asc(), Expense.id.asc()).all()
    current_expenses = [
        expense for expense in expenses
        if month_start <= expense.date <= today
    ]
    historical_expenses = [
        expense for expense in expenses
        if expense.date < month_start
    ]

    daily_totals = {}
    weekday_totals = {index: [] for index in range(7)}
    category_daily = {}
    current_by_category = {category.name: 0.0 for category in categories}
    historical_by_category_months = {category.name: [] for category in categories}
    locked_categories = set(note_rules.get('current_only_categories') or [])

    for expense in current_expenses:
        amount = float(expense.amount or 0)
        daily_totals[expense.date] = daily_totals.get(expense.date, 0.0) + amount
        category_name = expense.category.name if expense.category else 'Uncategorized'
        current_by_category[category_name] = current_by_category.get(category_name, 0.0) + amount

    historical_by_day = {}
    historical_month_totals = []
    historical_projectable_month_totals = []
    for window_start, window_end in historical_month_windows(month_start, 6):
        month_expenses = [
            expense for expense in historical_expenses
            if window_start <= expense.date <= window_end
        ]
        month_total = sum(float(expense.amount or 0) for expense in month_expenses)
        month_projectable_total = sum(
            float(expense.amount or 0)
            for expense in month_expenses
            if classify_forecast_expense(expense, locked_categories) == 'regular'
        )
        historical_month_totals.append(month_total)
        historical_projectable_month_totals.append(month_projectable_total)
        by_category = {}
        for expense in month_expenses:
            category_name = expense.category.name if expense.category else 'Uncategorized'
            by_category[category_name] = by_category.get(category_name, 0.0) + float(expense.amount or 0)
        for category in categories:
            historical_by_category_months[category.name].append(by_category.get(category.name, 0.0))

    for expense in historical_expenses:
        if classify_forecast_expense(expense, locked_categories) != 'regular':
            continue
        amount = float(expense.amount or 0)
        historical_by_day[expense.date] = historical_by_day.get(expense.date, 0.0) + amount

    for day, total in historical_by_day.items():
        weekday_totals[day.weekday()].append(total)

    current_spent = sum(daily_totals.values())
    category_month_medians = {
        category.name: percentile(historical_by_category_months.get(category.name, []), 0.5)
        for category in categories
    }
    projectable_daily_totals = {}
    regular_current_by_category = {category.name: 0.0 for category in categories}
    one_time_current_by_category = {category.name: 0.0 for category in categories}
    current_expense_classifications = []
    for expense in current_expenses:
        category_name = expense.category.name if expense.category else 'Uncategorized'
        amount = float(expense.amount or 0)
        classification = classify_forecast_expense(expense, locked_categories, category_month_medians)
        current_expense_classifications.append(
            serialize_forecast_expense_classification(expense, classification)
        )
        if classification == 'regular':
            projectable_daily_totals[expense.date] = projectable_daily_totals.get(expense.date, 0.0) + amount
            regular_current_by_category[category_name] = regular_current_by_category.get(category_name, 0.0) + amount
        elif classification != 'fixed_bill':
            one_time_current_by_category[category_name] = one_time_current_by_category.get(category_name, 0.0) + amount

    projectable_current_spent = sum(projectable_daily_totals.values())
    locked_current_spent = max(current_spent - projectable_current_spent, 0.0)
    one_time_current_spend = sum(one_time_current_by_category.values())
    nonzero_daily_values = [total for total in projectable_daily_totals.values() if total > 0]
    calendar_daily_avg = projectable_current_spent / elapsed_days if elapsed_days else 0.0
    active_daily_avg = average(nonzero_daily_values)
    last_7_start = max(month_start, today - timedelta(days=6))
    last_7_values = [projectable_daily_totals.get(day, 0.0) for day in inclusive_dates(last_7_start, today)]
    last_7_avg = average(last_7_values)
    historical_month_avg = average(historical_projectable_month_totals)
    historical_month_median = percentile(historical_projectable_month_totals, 0.5)
    historical_daily_avg = average(list(historical_by_day.values()))

    remaining_weekday_projection = 0.0
    for day in inclusive_dates(today + timedelta(days=1), month_end):
        weekday_average = average(weekday_totals.get(day.weekday(), []))
        remaining_weekday_projection += weekday_average if weekday_average else calendar_daily_avg

    unpaid_expected = [item for item in expected_items if not item.is_paid]
    forecast_unpaid_expected = [
        item for item in unpaid_expected
        if should_include_forecast_commitment(item, note_rules)
    ]
    ignored_unpaid_expected = [
        item for item in unpaid_expected
        if item not in forecast_unpaid_expected
    ]
    unpaid_expected_total = sum(expected_budget_amount(item) for item in unpaid_expected)
    forecast_unpaid_expected_total = sum(expected_budget_amount(item) for item in forecast_unpaid_expected)
    unpaid_future_total = sum(
        expected_budget_amount(item)
        for item in forecast_unpaid_expected
        if expected_expense_date(item) > today
    )
    bucket_remaining_total = sum(float(item.get('remaining') or 0) for item in bucket_summaries)

    simple_pace_total = current_spent + (calendar_daily_avg * remaining_days)
    last_7_pace_total = current_spent + (last_7_avg * remaining_days)
    weekday_total = current_spent + remaining_weekday_projection
    historical_remaining_projection = ((historical_month_avg * 0.65) + (historical_month_median * 0.35)) * (remaining_days / days_in_month)
    historical_blend_total = max(current_spent, current_spent + (historical_remaining_projection * 0.75) + ((simple_pace_total - current_spent) * 0.25))
    commitments_total = current_spent + unpaid_future_total

    candidate_totals = [
        simple_pace_total,
        last_7_pace_total,
        weekday_total,
        historical_blend_total,
        commitments_total,
    ]
    weighted_total = (
        simple_pace_total * 0.28
        + last_7_pace_total * 0.18
        + weekday_total * 0.22
        + historical_blend_total * 0.20
        + commitments_total * 0.12
    )
    # Known commitments are facts, not estimates. Never let the ensemble forecast
    # fall below money already spent plus unpaid future planned items.
    lower_guard = max(current_spent, commitments_total)
    upper_guard = percentile(candidate_totals, 0.9) * 1.18 if candidate_totals else weighted_total
    ensemble_total = min(max(weighted_total, lower_guard), max(upper_guard, lower_guard))
    predicted_additional = max(ensemble_total - current_spent, 0.0)
    expected_savings = monthly_budget - ensemble_total

    category_forecasts = []
    for category in categories:
        name = category.name
        current_amount = current_by_category.get(name, 0.0)
        category_current_share = current_amount / current_spent if current_spent else 0.0
        category_regular_current = regular_current_by_category.get(name, 0.0)
        category_one_time_current = one_time_current_by_category.get(name, 0.0)
        category_simple = current_amount + (
            category_regular_current / elapsed_days * remaining_days
            if elapsed_days else 0
        )
        category_history = average(historical_by_category_months.get(name, []))
        category_limit = float(category.monthly_limit or 0)
        unpaid_category_future = sum(
            expected_budget_amount(item)
            for item in forecast_unpaid_expected
            if item.category and item.category.name == name and expected_expense_date(item) > today
        )
        category_commitment = current_amount + unpaid_category_future
        if name in note_rules['current_only_categories']:
            category_model = category_commitment
            forecast_rule = (
                'admin_current_only'
                if name in note_rules.get('admin_current_only_categories', [])
                else 'auto_fixed_bill_current_plus_planned'
            )
        else:
            category_model = max(
                current_amount,
                category_commitment,
                (category_simple * 0.45) + (category_history * 0.30) + (ensemble_total * category_current_share * 0.25)
            )
            forecast_rule = 'pace_history_commitment_blend'
        if category_limit:
            category_model = max(category_model, min(category_limit, category_commitment))
        if category_model <= 0 and current_amount <= 0:
            continue
        category_forecasts.append({
            'category': name,
            'current_spent': round(current_amount, 2),
            'predicted_month_end_spend': round(category_model, 2),
            'historical_average': round(category_history, 2),
            'category_limit': round(category_limit, 2),
            'future_commitments': round(unpaid_category_future, 2),
            'share_of_current_spend': round(category_current_share * 100, 1),
            'regular_current_spend': round(category_regular_current, 2),
            'one_time_current_spend': round(category_one_time_current, 2),
            'forecast_rule': forecast_rule,
        })

    category_forecasts.sort(key=lambda row: row['predicted_month_end_spend'], reverse=True)
    category_forecast_total = sum(row['predicted_month_end_spend'] for row in category_forecasts)
    if category_forecast_total > ensemble_total:
        ensemble_total = category_forecast_total
        predicted_additional = max(ensemble_total - current_spent, 0.0)
        expected_savings = monthly_budget - ensemble_total

    confidence_score = 0.78
    if elapsed_days < 7:
        confidence_score -= 0.18
    if len([total for total in historical_month_totals if total > 0]) < 3:
        confidence_score -= 0.12
    if current_spent > monthly_budget * 0.85:
        confidence_score -= 0.05
    confidence_score = min(max(confidence_score, 0.35), 0.92)
    confidence_level = 'high' if confidence_score >= 0.75 else 'medium' if confidence_score >= 0.55 else 'low'

    return {
        'current_spent': round(current_spent, 2),
        'predicted_total_spend': round(max(ensemble_total, current_spent), 2),
        'predicted_additional_spend': round(predicted_additional, 2),
        'expected_savings': round(expected_savings, 2),
        'confidence_score': round(confidence_score, 2),
        'confidence_level': confidence_level,
        'daily_averages': {
            'calendar_daily_average': round(calendar_daily_avg, 2),
            'active_spend_day_average': round(active_daily_avg, 2),
            'last_7_day_average': round(last_7_avg, 2),
            'historical_daily_average': round(historical_daily_avg, 2),
        },
        'projectable_spend_to_date': round(projectable_current_spent, 2),
        'locked_current_spend': round(locked_current_spent, 2),
        'one_time_current_spend': round(one_time_current_spend, 2),
        'regular_current_spend': round(projectable_current_spent, 2),
        'candidate_totals': {
            'simple_pace_total': round(simple_pace_total, 2),
            'last_7_pace_total': round(last_7_pace_total, 2),
            'weekday_weighted_total': round(weekday_total, 2),
            'historical_blend_total': round(historical_blend_total, 2),
            'commitments_floor_total': round(commitments_total, 2),
        },
        'historical_month_totals': [round(total, 2) for total in historical_month_totals],
        'unpaid_expected_total': round(forecast_unpaid_expected_total, 2),
        'raw_unpaid_expected_total': round(unpaid_expected_total, 2),
        'unpaid_future_total': round(unpaid_future_total, 2),
        'bucket_remaining_total': round(bucket_remaining_total, 2),
        'category_forecast_total': round(category_forecast_total, 2),
        'category_forecasts': category_forecasts,
        'admin_note_rules': note_rules,
        'expense_classifications': current_expense_classifications,
        'ignored_unpaid_expected_items': [
            serialize_forecast_expected_item(
                item,
                'Admin note says this category is already done for the month.'
            )
            for item in ignored_unpaid_expected
        ],
        'method': [
            'weighted ensemble of current pace, last 7 days, weekday pattern, historical months, and known commitments',
            'admin notes can lock completed fixed categories to current actuals plus named exceptions',
            'rent, EMI, loans, and insurance are automatically treated as current actuals plus listed unpaid planned items',
            'one-time expenses are included in current spend but excluded from future daily pace',
            'unpaid future planned bills and reserved buckets are treated as spending commitments when not excluded by admin notes',
            'category forecasts blend current spend, historical average, category limits, and future commitments'
        ]
    }


def build_savings_forecast_schema(category_names):
    return {
        'type': 'object',
        'properties': {
            'forecast_title': {'type': 'string'},
            'forecast_end_date': {'type': 'string'},
            'predicted_additional_spend': {'type': 'number'},
            'predicted_total_spend': {'type': 'number'},
            'expected_savings': {'type': 'number', 'description': 'MUST copy the backend-calculated savings value exactly'},
            'confidence_level': {'type': 'string', 'enum': ['low', 'medium', 'high']},
            'confidence_score': {'type': 'number'},
            'assumptions': {'type': 'array', 'items': {'type': 'string'}},
            'important_notes_response': {'type': 'array', 'items': {'type': 'string'}},
            'risk_flags': {'type': 'array', 'items': {'type': 'string'}},
            'recommended_actions': {'type': 'array', 'items': {'type': 'string'}},
            'category_forecasts': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'category': {'type': 'string', 'enum': category_names},
                        'current_spent': {'type': 'number'},
                        'predicted_month_end_spend': {'type': 'number'},
                        'note': {'type': 'string'},
                    },
                    'required': ['category', 'current_spent', 'predicted_month_end_spend', 'note']
                }
            }
        },
        'required': [
            'forecast_title',
            'forecast_end_date',
            'predicted_additional_spend',
            'predicted_total_spend',
            'expected_savings',
            'confidence_level',
            'confidence_score',
            'assumptions',
            'important_notes_response',
            'risk_flags',
            'recommended_actions',
            'category_forecasts'
        ]
    }


def build_savings_forecast_context(family_id, admin_notes):
    family = db.session.get(Family, family_id)
    today = datetime.now().date()
    month_start, month_end = month_bounds(today.year, today.month)
    days_in_month = (month_end - month_start).days + 1
    elapsed_days = max((today - month_start).days + 1, 1)
    remaining_days = max((month_end - today).days, 0)

    # Backend-First Math: Get actual expenses for current month (up to today)
    expenses = Expense.query.filter(
        Expense.family_id == family_id,
        Expense.date >= month_start,
        Expense.date <= today
    ).order_by(Expense.date.asc(), Expense.id.asc()).all()

    categories = Category.query.filter_by(
        family_id=family_id
    ).order_by(Category.name.asc()).all()

    expected_items = ExpectedExpense.query.filter_by(
        family_id=family_id,
        month=today.month,
        year=today.year
    ).order_by(ExpectedExpense.due_day.asc(), ExpectedExpense.id.asc()).all()

    bucket_items = [item for item in expected_items if item.is_bucket]
    bucket_summaries = build_budget_bucket_summaries(
        bucket_items,
        family_id,
        f"{today.month:02d}",
        str(today.year)
    )

    category_spending = summarize_expenses(expenses).get('by_category', {})

    forecast_engine = build_forecast_engine(
        family_id,
        month_start,
        today,
        month_end,
        float(family.monthly_budget or 0),
        categories,
        expected_items,
        bucket_summaries,
        admin_notes
    )

    current_spent = forecast_engine['current_spent']
    simple_pace_total = forecast_engine['candidate_totals']['simple_pace_total']
    
    # Backend-First Math: Filter ONLY unpaid ExpectedExpense items (is_paid == False)
    # Paid items are already recorded in Expense table, so we exclude them to avoid double-counting
    unpaid_expected = [
        item for item in expected_items
        if not item.is_paid and should_include_forecast_commitment(item, forecast_engine['admin_note_rules'])
    ]
    
    total_unpaid_expected = forecast_engine['unpaid_expected_total']
    monthly_budget = float(family.monthly_budget or 0)
    calculated_savings = forecast_engine['expected_savings']
    
    unpaid_planned = [
        {
            'name': item.name,
            'category': item.category.name if item.category else 'Uncategorized',
            'amount': round(expected_budget_amount(item), 2),
            'due_date': expected_expense_date(item).isoformat(),
            'type': 'reserved_bucket' if item.is_bucket else 'planned_bill',
        }
        for item in unpaid_expected
    ]

    return {
        'family': family,
        'today': today,
        'month_start': month_start,
        'month_end': month_end,
        'current_spent': current_spent,
        'simple_pace_total': simple_pace_total,
        'total_unpaid_expected': total_unpaid_expected,
        'calculated_savings': calculated_savings,
        'categories': categories,
        'payload': {
            'currency': 'INR',
            'generated_at': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
            'month_start_date': month_start.isoformat(),
            'today': today.isoformat(),
            'forecast_end_date': month_end.isoformat(),
            'days_in_month': days_in_month,
            'elapsed_days': elapsed_days,
            'remaining_days': remaining_days,
            'monthly_budget': round(float(family.monthly_budget or 0), 2),
            'current_spent': current_spent,
            'simple_pace_projection': simple_pace_total,
            'simple_pace_savings': round(float(family.monthly_budget or 0) - simple_pace_total, 2),
            'forecast_engine': forecast_engine,
            'category_spending_to_date': category_spending,
            'categories': [
                {
                    'name': category.name,
                    'monthly_limit': round(float(category.monthly_limit or 0), 2),
                    'is_preset': bool(category.is_fixed),
                }
                for category in categories
            ],
            'current_month_expenses': [serialize_expense(expense) for expense in expenses],
            'unpaid_planned_items_and_reserves': unpaid_planned,
            'ignored_unpaid_items_from_admin_notes': forecast_engine.get('ignored_unpaid_expected_items', []),
            'reserved_bucket_status': [
                {
                    'name': item['bucket'].name,
                    'category': item['bucket'].category.name if item['bucket'].category else 'Uncategorized',
                    'allocated': round(item['allocated'], 2),
                    'spent': round(item['spent'], 2),
                    'remaining': round(item['remaining'], 2),
                    'overflow': round(item['overflow'], 2),
                }
                for item in bucket_summaries
            ],
            'important_admin_notes': admin_notes,
            'backend_calculated_values': {
                'total_actual_spent': current_spent,
                'total_unpaid_expected': total_unpaid_expected,
                'calculated_savings': calculated_savings,
                'predicted_total_spend': forecast_engine['predicted_total_spend'],
                'predicted_additional_spend': forecast_engine['predicted_additional_spend'],
                'confidence_score': forecast_engine['confidence_score'],
                'confidence_level': forecast_engine['confidence_level'],
            },
            'instructions_for_ai': [
                'Use forecast_engine as the source of truth for totals and confidence.',
                'Analyze the forecast engine candidates and explain why the ensemble result is plausible.',
                'Do not invent totals. Copy predicted_total_spend, predicted_additional_spend, expected_savings, confidence_level, and confidence_score from backend_calculated_values.',
                'Use actual expenses, unpaid planned items, bucket reserves, category limits, historical baselines, weekday patterns, and admin notes to inform your analysis.',
                'Admin notes are hard rules after the backend parses them. Do not re-add ignored_unpaid_items_from_admin_notes.',
                'If forecast_engine.admin_note_rules has current_only_categories, keep those categories at current actuals plus named exception commitments only.',
                'For automatic fixed-bill categories such as rent, EMI, loans, and insurance, only listed unpaid planned items are future commitments. Do not infer extra payoffs from historical/current spend.',
                'Use forecast_engine.expense_classifications to explain one-time versus regular spending. One-time expenses are already excluded from future pace math.',
                'Do not invent category names. Use only the provided categories in category_forecasts.',
            ]
        }
    }


def build_savings_forecast_prompt(context_payload):
    backend_values = context_payload.get('backend_calculated_values', {})
    calculated_savings = backend_values.get('calculated_savings', 0)
    total_actual_spent = backend_values.get('total_actual_spent', 0)
    total_unpaid_expected = backend_values.get('total_unpaid_expected', 0)
    predicted_total = backend_values.get('predicted_total_spend', 0)
    predicted_additional = backend_values.get('predicted_additional_spend', 0)
    
    return (
        'You are VaultSync senior forecasting analyst.\n'
        'You are given a deterministic forecast engine with multiple baselines. Your job is to audit, explain, and enrich it with practical actions.\n'
        'IMPORTANT: Use backend-calculated values as the numeric source of truth.\n\n'
        f'{json.dumps(context_payload, ensure_ascii=False)}\n\n'
        f'HARDCODED FACTS FOR THIS FORECAST:\n'
        f'- Exact predicted month-end spend: Rs {predicted_total}\n'
        f'- Exact predicted additional spend: Rs {predicted_additional}\n'
        f'- Exact calculated remaining savings: Rs {calculated_savings}\n'
        f'- Total actual spent to date: Rs {total_actual_spent}\n'
        f'- Total unpaid expected expenses: Rs {total_unpaid_expected}\n'
        f'\n'
        f'Admin note and automatic fixed-bill rules in forecast_engine are already applied to the math. Never override them or re-add ignored unpaid items.\n'
        f'Only count future loan, EMI, rent, or insurance payments when they are present in unpaid_planned_items_and_reserves.\n'
        f'Use expense_classifications to distinguish one-time expenses from regular expenses; do not project one-time expenses again.\n'
        f'Return those exact numeric values. Analyze candidate totals, outliers, category drivers, risks, and recommended actions.\n'
        f'Return only JSON matching the schema. No markdown, no prose outside JSON.'
    )


def normalize_savings_forecast_payload(payload, context):
    month_end = context['month_end']
    current_spent = context['current_spent']
    monthly_budget = float(context['family'].monthly_budget or 0)
    
    # Backend-First Math: Use the pre-calculated savings value
    backend_calculated_savings = context['calculated_savings']

    forecast_engine = context.get('payload', {}).get('forecast_engine', {})
    predicted_total = float(forecast_engine.get('predicted_total_spend') or context['simple_pace_total'])
    predicted_total = max(predicted_total, current_spent)
    predicted_additional = max(float(forecast_engine.get('predicted_additional_spend') or (predicted_total - current_spent)), 0.0)
    expected_savings = backend_calculated_savings

    try:
        confidence_score = float(forecast_engine.get('confidence_score', payload.get('confidence_score')))
    except (TypeError, ValueError):
        confidence_score = 0.5
    confidence_score = min(max(confidence_score, 0.0), 1.0)

    confidence_level = str(forecast_engine.get('confidence_level') or payload.get('confidence_level') or 'medium').strip().lower()
    if confidence_level not in {'low', 'medium', 'high'}:
        confidence_level = 'medium'

    payload['forecast_title'] = str(payload.get('forecast_title') or 'End-of-month savings forecast')[:160]
    payload['forecast_end_date'] = month_end.isoformat()
    payload['predicted_additional_spend'] = round(predicted_additional, 2)
    payload['predicted_total_spend'] = round(predicted_total, 2)
    payload['expected_savings'] = round(expected_savings, 2)
    payload['confidence_level'] = confidence_level
    payload['confidence_score'] = round(confidence_score, 2)

    for key in ('assumptions', 'important_notes_response', 'risk_flags', 'recommended_actions'):
        values = payload.get(key)
        if not isinstance(values, list):
            values = []
        payload[key] = [str(value).strip() for value in values if str(value).strip()][:8]

    category_names = {category.name for category in context['categories']}
    engine_category_rows = {
        row['category']: row
        for row in forecast_engine.get('category_forecasts') or []
    }
    normalized_category_forecasts = []
    ai_rows = payload.get('category_forecasts') or []
    for item in ai_rows:
        if not isinstance(item, dict):
            continue
        category_name = str(item.get('category') or '').strip()
        if category_name not in category_names:
            continue
        engine_row = engine_category_rows.get(category_name, {})
        current_category_spent = float(engine_row.get('current_spent', item.get('current_spent') or 0))
        predicted_category_spend = float(engine_row.get('predicted_month_end_spend', item.get('predicted_month_end_spend') or current_category_spent))
        normalized_category_forecasts.append({
            'category': category_name,
            'current_spent': round(max(current_category_spent, 0.0), 2),
            'predicted_month_end_spend': round(max(predicted_category_spend, 0.0), 2),
            'note': str(item.get('note') or '').strip()[:240],
        })
    existing_forecast_categories = {row['category'] for row in normalized_category_forecasts}
    for category_name, engine_row in engine_category_rows.items():
        if category_name in existing_forecast_categories:
            continue
        normalized_category_forecasts.append({
            'category': category_name,
            'current_spent': round(max(float(engine_row.get('current_spent') or 0), 0.0), 2),
            'predicted_month_end_spend': round(max(float(engine_row.get('predicted_month_end_spend') or 0), 0.0), 2),
            'note': 'Backend forecast engine baseline.',
        })
    normalized_category_forecasts.sort(key=lambda row: row['predicted_month_end_spend'], reverse=True)
    category_total = sum(row['predicted_month_end_spend'] for row in normalized_category_forecasts)
    if category_total > predicted_total:
        predicted_total = category_total
        predicted_additional = max(predicted_total - current_spent, 0.0)
        expected_savings = monthly_budget - predicted_total
        payload['predicted_additional_spend'] = round(predicted_additional, 2)
        payload['predicted_total_spend'] = round(predicted_total, 2)
        payload['expected_savings'] = round(expected_savings, 2)
        forecast_engine['predicted_total_spend'] = round(predicted_total, 2)
        forecast_engine['predicted_additional_spend'] = round(predicted_additional, 2)
        forecast_engine['expected_savings'] = round(expected_savings, 2)
    payload['category_forecasts'] = normalized_category_forecasts
    payload['forecast_engine'] = forecast_engine
    return payload


def create_ai_savings_forecast(family_id, user_id, admin_notes):
    context = build_savings_forecast_context(family_id, admin_notes)
    category_names = [category.name for category in context['categories']]
    if not category_names:
        raise ValueError('Create categories before running a savings forecast.')

    prompt = build_savings_forecast_prompt(context['payload'])
    payload = call_gemini_json(prompt, build_savings_forecast_schema(category_names))
    payload = normalize_savings_forecast_payload(payload, context)

    forecast = AISavingsForecast(
        family_id=family_id,
        created_by_user_id=user_id,
        source_start_date=context['month_start'],
        forecast_end_date=context['month_end'],
        current_spent=context['current_spent'],
        predicted_additional_spend=payload['predicted_additional_spend'],
        predicted_total_spend=payload['predicted_total_spend'],
        expected_savings=payload['expected_savings'],
        confidence_level=payload['confidence_level'],
        confidence_score=payload['confidence_score'],
        admin_notes=admin_notes,
        raw_json=json.dumps(payload),
        created_at=datetime.utcnow()
    )
    db.session.add(forecast)
    db.session.commit()
    return forecast


def savings_forecast_payload(forecast):
    if not forecast:
        return {}
    try:
        payload = json.loads(forecast.raw_json)
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}

    forecast_engine = payload.get('forecast_engine')
    if isinstance(forecast_engine, dict):
        forecast_engine.setdefault('candidate_totals', {})
        forecast_engine.setdefault('admin_note_rules', {})
        forecast_engine.setdefault('regular_current_spend', forecast_engine.get('projectable_spend_to_date', 0))
        forecast_engine.setdefault('one_time_current_spend', 0)
        forecast_engine.setdefault('locked_current_spend', 0)
        forecast_engine.setdefault('expense_classifications', [])

    return payload


def savings_forecast_chart_data(family, forecast):
    monthly_budget = float(family.monthly_budget or 0)
    if not forecast:
        return {
            'monthly_budget': round(monthly_budget, 2),
            'current_spent': 0,
            'predicted_additional_spend': 0,
            'expected_savings': max(round(monthly_budget, 2), 0),
            'expected_overspend': 0,
        }

    expected_savings = float(forecast.expected_savings or 0)
    return {
        'monthly_budget': round(monthly_budget, 2),
        'current_spent': round(float(forecast.current_spent or 0), 2),
        'predicted_additional_spend': round(float(forecast.predicted_additional_spend or 0), 2),
        'expected_savings': round(max(expected_savings, 0.0), 2),
        'expected_overspend': round(max(-expected_savings, 0.0), 2),
    }


def build_daily_report_context(family_id, report_date=None):
    """Build context for daily budget report generation."""
    if not report_date:
        report_date = datetime.now().date()
    
    family = db.session.get(Family, family_id)
    if not family:
        raise ValueError(f"Family {family_id} not found")
    
    month_start, month_end = month_bounds(report_date.year, report_date.month)
    week_start = report_date - timedelta(days=report_date.weekday())
    week_end = week_start + timedelta(days=6)
    
    # Get today's expenses
    today_expenses = Expense.query.filter(
        Expense.family_id == family_id,
        Expense.date == report_date
    ).all()
    today_total = sum(float(exp.amount) for exp in today_expenses)
    
    # Get this week's expenses
    week_expenses = Expense.query.filter(
        Expense.family_id == family_id,
        Expense.date >= week_start,
        Expense.date <= min(week_end, report_date)
    ).all()
    week_total = sum(float(exp.amount) for exp in week_expenses)
    
    # Get this month's expenses
    month_expenses = Expense.query.filter(
        Expense.family_id == family_id,
        Expense.date >= month_start,
        Expense.date <= report_date
    ).all()
    month_total = sum(float(exp.amount) for exp in month_expenses)
    
    # Category breakdown
    category_spending = {}
    for exp in month_expenses:
        cat_name = exp.category.name if exp.category else 'Uncategorized'
        category_spending[cat_name] = category_spending.get(cat_name, 0.0) + float(exp.amount)
    
    # Find top spending category
    top_category = max(category_spending.items(), key=lambda x: x[1]) if category_spending else ('None', 0)
    
    # Budget remaining
    finance = monthly_finance_for(family, report_date.month, report_date.year)
    monthly_income = finance['monthly_income']
    monthly_budget = finance['monthly_budget']
    budget_remaining = max(monthly_budget - month_total, 0)
    budget_used_pct = (month_total / monthly_budget * 100) if monthly_budget else 0
    
    # Daily average
    days_elapsed = (report_date - month_start).days + 1
    daily_average = month_total / days_elapsed if days_elapsed > 0 else 0
    
    # Spend trend (compare last 7 days vs previous 7 days)
    seven_days_ago = report_date - timedelta(days=7)
    last_week_start = seven_days_ago - timedelta(days=7)
    last_week_total = sum(float(exp.amount) for exp in Expense.query.filter(
        Expense.family_id == family_id,
        Expense.date >= last_week_start,
        Expense.date <= seven_days_ago
    ).all())
    
    if last_week_total > 0:
        week_trend = 'increasing' if week_total > last_week_total else ('decreasing' if week_total < last_week_total else 'stable')
    else:
        week_trend = 'stable'
    
    return {
        'family': family,
        'report_date': report_date,
        'today_total': today_total,
        'week_total': week_total,
        'month_total': month_total,
        'monthly_income': monthly_income,
        'monthly_budget': monthly_budget,
        'budget_remaining': budget_remaining,
        'budget_used_percentage': budget_used_pct,
        'top_category': top_category[0],
        'top_category_amount': top_category[1],
        'daily_average': daily_average,
        'week_trend': week_trend,
        'category_breakdown': category_spending,
        'today_expenses': today_expenses,
        'month_start': month_start,
        'month_end': month_end,
    }


def build_daily_report_prompt(context):
    """Build the prompt for AI daily budget report generation."""
    return (
        'You are VaultSync Daily Budget Report AI.\n'
        'Generate a helpful daily budget report with insights and actionable suggestions.\n'
        'Focus on spending patterns, warnings, and positive recommendations.\n\n'
        f'Family Monthly Budget: Rs {context["monthly_budget"]:,.0f}\n'
        f'Family Monthly Income: Rs {context["monthly_income"]:,.0f}\n'
        f'Budget Used: {context["budget_used_percentage"]:.1f}%\n'
        f'Budget Remaining: Rs {context["budget_remaining"]:,.0f}\n\n'
        f'Today\'s Spending: Rs {context["today_total"]:,.0f}\n'
        f'This Week\'s Spending: Rs {context["week_total"]:,.0f}\n'
        f'This Month\'s Spending: Rs {context["month_total"]:,.0f}\n'
        f'Daily Average: Rs {context["daily_average"]:,.0f}\n'
        f'Spending Trend: {context["week_trend"]}\n\n'
        f'Top Category: {context["top_category"]} (Rs {context["top_category_amount"]:,.0f})\n'
        f'Category Breakdown: {json.dumps(context["category_breakdown"], ensure_ascii=False)}\n\n'
        'Provide a detailed summary (3-5 sentences), 4-5 actionable suggestions, clear next-step instructions, and any warnings.\n'
        'Suggestions should say what to do next, how much to cap or move, and which category to watch.\n'
        'Return JSON matching the required schema. No markdown, no prose outside JSON.'
    )


def daily_report_schema():
    return {
        'type': 'object',
        'properties': {
            'summary': {'type': 'string', 'description': 'Brief 2-3 sentence summary of budget health'},
            'suggestions': {
                'type': 'array',
                'items': {'type': 'string'},
                'maxItems': 5,
                'description': 'Actionable suggestions to improve budget'
            },
            'warnings': {
                'type': 'array',
                'items': {'type': 'string'},
                'maxItems': 3,
                'description': 'Warning messages if spending is high'
            },
            'insights': {
                'type': 'array',
                'items': {'type': 'string'},
                'maxItems': 4,
                'description': 'Interesting insights about spending patterns'
            },
        },
        'required': ['summary', 'suggestions', 'warnings', 'insights']
    }


def create_daily_budget_report(family_id, report_date=None):
    """Generate a daily budget report with AI suggestions."""
    if not report_date:
        report_date = datetime.now().date()
    
    context = build_daily_report_context(family_id, report_date)
    existing_report = BudgetReport.query.filter_by(
        family_id=family_id,
        report_date=report_date
    ).first()
    if existing_report:
        db.session.delete(existing_report)
        db.session.flush()
    
    try:
        prompt = build_daily_report_prompt(context)
        payload = call_gemini_json(prompt, daily_report_schema())
    except Exception as exc:
        app.logger.warning(f'AI daily report failed for family {family_id}: {exc}')
        # Create a basic report without AI
        payload = {
            'summary': f"Spending this month: Rs {context['month_total']:,.0f} of Rs {context['monthly_budget']:,.0f}",
            'suggestions': [
                f"Keep daily spending near Rs {context['monthly_budget'] / max((context['month_end'] - context['month_start']).days + 1, 1):,.0f} unless a planned bill is due.",
                f"Review {context['top_category']} first because it is the largest category this month.",
                "Log every cash spend the same day so tomorrow's report can calculate a reliable safe daily limit.",
            ],
            'warnings': [],
            'insights': [
                f"Your top spending category is {context['top_category']}.",
                f"Your current daily average is Rs {context['daily_average']:,.0f}.",
            ]
        }
    
    # Normalize the AI response
    suggestions = []
    if isinstance(payload.get('suggestions'), list):
        suggestions = [str(s).strip() for s in payload['suggestions'] if str(s).strip()][:5]
    
    warnings = []
    if isinstance(payload.get('warnings'), list):
        warnings = [str(w).strip() for w in payload['warnings'] if str(w).strip()][:3]
    
    insights = []
    if isinstance(payload.get('insights'), list):
        insights = [str(i).strip() for i in payload['insights'] if str(i).strip()][:4]
    
    # Create the report record
    report = BudgetReport(
        family_id=family_id,
        report_date=report_date,
        total_spent_today=context['today_total'],
        total_spent_this_week=context['week_total'],
        total_spent_this_month=context['month_total'],
        monthly_budget=context['monthly_budget'],
        budget_remaining=context['budget_remaining'],
        budget_used_percentage=context['budget_used_percentage'],
        top_spending_category=context['top_category'],
        top_spending_amount=context['top_category_amount'],
        spending_trend=context['week_trend'],
        summary=str(payload.get('summary', ''))[:500],
        suggestions=json.dumps(suggestions),
        warnings=json.dumps(warnings),
        insights=json.dumps(insights),
        category_breakdown=json.dumps(context['category_breakdown']),
        is_read=False,
        created_at=datetime.utcnow()
    )
    
    db.session.add(report)
    db.session.commit()
    return report


def build_report_action_plan(report, suggestions=None, warnings=None, category_breakdown=None):
    suggestions = suggestions or []
    warnings = warnings or []
    category_breakdown = category_breakdown or {}
    days_in_month = calendar.monthrange(report.report_date.year, report.report_date.month)[1]
    days_elapsed = max(report.report_date.day, 1)
    days_remaining = max(days_in_month - report.report_date.day, 0)
    daily_target = (report.monthly_budget / days_in_month) if report.monthly_budget else 0
    safe_daily_spend = (report.budget_remaining / days_remaining) if days_remaining else max(report.budget_remaining, 0)
    expected_spend_by_today = daily_target * days_elapsed
    pace_delta = report.total_spent_this_month - expected_spend_by_today
    top_category = max(category_breakdown.items(), key=lambda item: float(item[1] or 0)) if category_breakdown else None

    actions = []
    if warnings:
        actions.append(f"Handle this first: {warnings[0]}")
    if report.budget_used_percentage >= 100:
        actions.append("Pause optional spending until a manager increases this month's budget or moves money from savings intentionally.")
    elif report.budget_used_percentage >= 80:
        actions.append(f"Keep tomorrow's unplanned spend under Rs {safe_daily_spend:,.0f} and delay optional purchases.")
    else:
        actions.append(f"Use Rs {safe_daily_spend:,.0f} as the safe daily spend target for the remaining days.")
    if top_category:
        actions.append(f"Check {top_category[0]} before the next purchase; it is currently Rs {float(top_category[1] or 0):,.0f}.")
    if suggestions:
        actions.append(suggestions[0])
    actions.append("After logging tomorrow's expenses, regenerate the report to confirm whether the pace improved.")

    return {
        'days_remaining': days_remaining,
        'daily_target': daily_target,
        'safe_daily_spend': safe_daily_spend,
        'expected_spend_by_today': expected_spend_by_today,
        'pace_delta': pace_delta,
        'top_category': top_category[0] if top_category else report.top_spending_category,
        'actions': actions[:5],
    }


def run_daily_budget_reports():
    """Generate daily budget reports for all families at 11:58 PM."""
    with app.app_context():
        target_date = datetime.now().date()
        families = Family.query.filter_by(is_archived=False).all()
        for family in families:
            try:
                # Check if report already exists for today
                existing = BudgetReport.query.filter_by(
                    family_id=family.id,
                    report_date=target_date
                ).first()
                if not existing:
                    create_daily_budget_report(family.id, target_date)
            except Exception as exc:
                db.session.rollback()
                app.logger.warning(f'Daily budget report generation failed for family {family.id}: {exc}')


def seconds_until_daily_report():
    """Calculate seconds until 11:58 PM."""
    now = datetime.now()
    target = now.replace(hour=23, minute=58, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(60, int((target - now).total_seconds()))


def daily_report_scheduler_loop():
    """Background scheduler for daily reports at 11:58 PM."""
    while True:
        time.sleep(seconds_until_daily_report())
        run_daily_budget_reports()
        time.sleep(60)


_daily_report_scheduler_started = False


def start_daily_report_scheduler():
    """Start the daily report scheduler if not already running."""
    global _daily_report_scheduler_started
    if _daily_report_scheduler_started:
        return
    if os.environ.get('DISABLE_DAILY_REPORT_SCHEDULER') == '1':
        return
    if not gemini_configured():
        return

    _daily_report_scheduler_started = True
    thread = threading.Thread(target=daily_report_scheduler_loop, daemon=True)
    thread.start()


def default_family_ai_user_id(family):
    if family.created_by_user_id:
        return family.created_by_user_id
    manager = User.query.filter_by(family_id=family.id, user_type='family_manager').first()
    if manager:
        return manager.id
    member = User.query.filter_by(family_id=family.id).first()
    return member.id if member else None


def run_daily_ai_suggested_plans():
    """Generate AI suggested plans for all active families at 11:25 PM."""
    with app.app_context():
        today = datetime.now().date()
        families = Family.query.filter_by(is_archived=False).all()
        for family in families:
            user_id = default_family_ai_user_id(family)
            if not user_id:
                continue
            try:
                existing_today = BudgetSuggestion.query.filter(
                    BudgetSuggestion.family_id == family.id,
                    BudgetSuggestion.target_start_date == date(today.year, today.month, 1),
                    BudgetSuggestion.title.like('AI Suggested Plan:%'),
                    func.strftime('%Y-%m-%d', BudgetSuggestion.created_at) == today.isoformat()
                ).first()
                if not existing_today:
                    create_ai_suggested_plan(
                        family.id,
                        user_id,
                        target_month=today.month,
                        target_year=today.year,
                        source='auto',
                        replace_today=False
                    )
            except Exception as exc:
                db.session.rollback()
                app.logger.warning('Scheduled AI suggested plan failed for family %s: %s', family.id, exc)


def seconds_until_daily_ai_plan():
    """Calculate seconds until 11:25 PM."""
    now = datetime.now()
    target = now.replace(hour=23, minute=25, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(60, int((target - now).total_seconds()))


def ai_plan_scheduler_loop():
    while True:
        time.sleep(seconds_until_daily_ai_plan())
        run_daily_ai_suggested_plans()
        time.sleep(60)


def start_ai_plan_scheduler():
    global _ai_plan_scheduler_started
    if _ai_plan_scheduler_started:
        return
    if os.environ.get('DISABLE_AI_PLAN_SCHEDULER') == '1':
        return
    if not gemini_configured():
        return

    _ai_plan_scheduler_started = True
    thread = threading.Thread(target=ai_plan_scheduler_loop, daemon=True)
    thread.start()


def run_daily_ai_organization():
    with app.app_context():
        target_date = datetime.now().date()
        families = Family.query.filter_by(is_archived=False).all()
        for family in families:
            try:
                organize_expenses_for_family(family.id, target_date, target_date)
            except Exception as exc:
                db.session.rollback()
                app.logger.warning('Scheduled AI expense organization failed for family %s: %s', family.id, exc)


def seconds_until_daily_organization():
    now = datetime.now()
    target = now.replace(hour=23, minute=30, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(60, int((target - now).total_seconds()))


def ai_organizer_scheduler_loop():
    while True:
        time.sleep(seconds_until_daily_organization())
        run_daily_ai_organization()
        time.sleep(60)


def start_ai_organizer_scheduler():
    global _ai_organizer_scheduler_started
    if _ai_organizer_scheduler_started:
        return
    if os.environ.get('DISABLE_AI_ORGANIZER_SCHEDULER') == '1':
        return
    if not gemini_configured():
        return

    _ai_organizer_scheduler_started = True
    thread = threading.Thread(target=ai_organizer_scheduler_loop, daemon=True)
    thread.start()


if os.environ.get('RUN_AI_ORGANIZER_SCHEDULER') == '1':
    start_ai_organizer_scheduler()


def ensure_runtime_schema():
    """Lightweight safety net for local SQLite runs that skip Alembic upgrades."""
    global _runtime_schema_checked
    if _runtime_schema_checked:
        return

    db.create_all()
    inspector = inspect(db.engine)
    if inspector.has_table('family'):
        family_columns = {column['name'] for column in inspector.get_columns('family')}
        with db.engine.begin() as connection:
            if 'monthly_income' not in family_columns:
                connection.execute(text('ALTER TABLE family ADD COLUMN monthly_income FLOAT'))
                connection.execute(text('UPDATE family SET monthly_income = monthly_budget WHERE monthly_income IS NULL'))
            if 'google_sheet_url' not in family_columns:
                connection.execute(text('ALTER TABLE family ADD COLUMN google_sheet_url VARCHAR(500)'))
            if 'google_sheet_webhook_url' not in family_columns:
                connection.execute(text('ALTER TABLE family ADD COLUMN google_sheet_webhook_url VARCHAR(500)'))
            if 'google_sheet_sync_token' not in family_columns:
                connection.execute(text('ALTER TABLE family ADD COLUMN google_sheet_sync_token VARCHAR(64)'))
            if 'last_sheet_sync_at' not in family_columns:
                connection.execute(text('ALTER TABLE family ADD COLUMN last_sheet_sync_at DATETIME'))

        # Ensure families have sync tokens generated
        families_without_token = Family.query.filter(
            (Family.google_sheet_sync_token == None) | (Family.google_sheet_sync_token == '')
        ).all()
        if families_without_token:
            for f in families_without_token:
                f.google_sheet_sync_token = secrets.token_hex(16)
            db.session.commit()

    _runtime_schema_checked = True


@app.before_request
def ensure_ai_organizer_scheduler_running():
    ensure_runtime_schema()
    if (
        current_user.is_authenticated
        and current_user.family
        and current_user.family.created_by_user_id == current_user.id
        and current_user.user_type != 'family_manager'
    ):
        current_user.user_type = 'family_manager'
        db.session.commit()
    start_ai_organizer_scheduler()
    start_daily_report_scheduler()
    start_ai_plan_scheduler()


@app.context_processor
def inject_global_layout_context():
    unread_reports_count = 0
    is_manager = False
    if current_user.is_authenticated:
        is_manager = is_family_manager(current_user)
        if current_user.family_id:
            try:
                unread_reports_count = BudgetReport.query.filter_by(
                    family_id=current_user.family_id,
                    is_read=False
                ).count()
            except Exception:
                unread_reports_count = 0
    return {
        'unread_reports_count': unread_reports_count,
        'gemini_ready': gemini_configured(),
        'is_manager': is_manager,
        'current_endpoint': request.endpoint or '',
    }

# --- AUTH ROUTES ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    """User registration"""
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        user_type = request.form.get('user_type', 'family_member')
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        password_confirm = request.form.get('password_confirm', '')

        # Basic validation
        if not username or len(username) < 3:
            flash('Username must be at least 3 characters long.', 'error')
            return redirect(url_for('register'))

        if not email or '@' not in email:
            flash('Please provide a valid email address.', 'error')
            return redirect(url_for('register'))

        if not password or len(password) < 6:
            flash('Password must be at least 6 characters long.', 'error')
            return redirect(url_for('register'))

        if password != password_confirm:
            flash('Passwords do not match.', 'error')
            return redirect(url_for('register'))

        # Check if user already exists
        if User.query.filter_by(username=username).first():
            flash('Username already exists. Please choose another.', 'error')
            return redirect(url_for('register'))

        if User.query.filter_by(email=email).first():
            flash('Email already registered. Please log in instead.', 'error')
            return redirect(url_for('register'))

        # Handle Family Manager registration
        if user_type == 'family_manager':
            family_name = request.form.get('family_name', '').strip()
            initial_budget = request.form.get('initial_budget', 170000)

            if not family_name or len(family_name) < 2:
                flash('Family name must be at least 2 characters long.', 'error')
                return redirect(url_for('register'))

            try:
                initial_budget = float(initial_budget)
                if initial_budget < 1000:
                    flash('Initial budget must be at least 1000.', 'error')
                    return redirect(url_for('register'))
            except (ValueError, TypeError):
                flash('Please provide a valid budget amount.', 'error')
                return redirect(url_for('register'))

            # Create family
            family = Family(name=family_name, monthly_income=initial_budget, monthly_budget=initial_budget)
            db.session.add(family)
            db.session.flush()
            ensure_preset_categories(family.id)

            # Create user as family manager
            user = User(username=username, email=email, user_type='family_manager', family_id=family.id)
            user.set_password(password)
            db.session.add(user)
            db.session.flush()
            family.created_by_user_id = user.id
            db.session.commit()

            flash('Family created successfully! Please log in.', 'success')

        # Handle Family Member registration
        else:
            invite_code = request.form.get('invite_code', '').strip()

            if not invite_code:
                flash('Please provide a family invite code to join.', 'error')
                return redirect(url_for('register'))

            family = Family.query.filter_by(invite_code=invite_code).first()
            if not family:
                flash('Invalid invite code. Please check and try again.', 'error')
                return redirect(url_for('register'))

            # Create user as family member
            user = User(username=username, email=email, user_type='family_member', family_id=family.id)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()

            flash('Successfully joined the family! Please log in.', 'success')

        return redirect(url_for('login'))

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    """User login"""
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username_or_email = request.form.get('username_or_email', '').strip()
        password = request.form.get('password', '')
        
        # Find user by username or email
        user = User.query.filter(
            (User.username == username_or_email) | 
            (User.email == username_or_email.lower())
        ).first()
        
        if user and user.check_password(password):
            login_user(user, remember=request.form.get('remember', False))
            next_page = request.args.get('next')
            return redirect(next_page) if next_page else redirect(url_for('dashboard'))
        else:
            flash('Invalid username/email or password.', 'error')
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    """User logout"""
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/join_family', methods=['POST'])
@login_required
def join_family():
    """Join a family using invite code"""
    invite_code = request.form.get('invite_code', '').strip()

    if not invite_code:
        flash('Please provide an invite code.', 'error')
        return redirect(url_for('dashboard'))

    family = Family.query.filter_by(invite_code=invite_code).first()
    if not family:
        flash('Invalid invite code. Please check and try again.', 'error')
        return redirect(url_for('dashboard'))

    # Update user's family
    current_user.family_id = family.id
    db.session.commit()
    flash('You have successfully joined the family!', 'success')
    return redirect(url_for('dashboard'))

# --- ROUTES ---
@app.route('/')
@app.route('/dashboard')
def dashboard():
    if not current_user.is_authenticated:
        return redirect(url_for('login'))

    # --- NEW FILTERING LOGIC ---
    if current_user.family_id:
        created_presets = ensure_preset_categories(current_user.family_id)
        if created_presets:
            db.session.commit()

    # Get month/year from URL args, default strictly to current date if none provided
    current_date = datetime.utcnow()
    raw_month = (request.args.get('month') or '').strip()
    is_all_time = raw_month.lower() in ('all', 'all_time', 'alltime', '0')

    all_categories = Category.query.filter_by(family_id=current_user.family_id).all()

    if is_all_time:
        selected_month = 'all'
        selected_year = request.args.get('year', current_date.year, type=int)
        if selected_year < 2020 or selected_year > 2050:
            selected_year = current_date.year
        month_name = 'All Time'

        # Filter expenses across all time for this family
        all_expenses = Expense.query.filter_by(
            family_id=current_user.family_id
        ).order_by(Expense.date.desc()).all()

        total_spent = sum(exp.amount for exp in all_expenses)

        # Totals grouped by category across all time
        totals = db.session.query(
            Expense.category_id,
            func.coalesce(func.sum(Expense.amount), 0).label('total')
        ).filter(
            Expense.family_id == current_user.family_id
        ).group_by(Expense.category_id).all()

        totals_map = {int(cat_id): float(total) for cat_id, total in totals}

        chart_labels = []
        chart_data = []
        chart_colors = []
        for cat in all_categories:
            cat_total = totals_map.get(cat.id, 0.0)
            if cat_total > 0:
                chart_labels.append(cat.name)
                chart_data.append(cat_total)
                chart_colors.append(cat.color)

        finance = monthly_finance_for(current_user.family, current_date.month, current_date.year)
        monthly_income = finance['monthly_income']
        monthly_budget = finance['monthly_budget']

        expected_expenses = ExpectedExpense.query.filter_by(
            family_id=current_user.family_id
        ).order_by(ExpectedExpense.amount.desc()).all()

        budget_bucket_items = [exp for exp in expected_expenses if exp.is_bucket]
        budget_buckets = build_budget_bucket_summaries(
            budget_bucket_items,
            current_user.family_id,
            month_str=None,
            year_str=None
        )
        bucket_unspent_total = sum(bucket['remaining'] for bucket in budget_buckets)
        bucket_allocated_total = sum(bucket['allocated'] for bucket in budget_buckets)
        bucket_spent_total = sum(bucket['spent'] for bucket in budget_buckets)
        bucket_overflow_total = sum(bucket['overflow'] for bucket in budget_buckets)

        projected_savings = monthly_income - total_spent - bucket_unspent_total
        expected_total = sum(expected_budget_amount(exp) for exp in expected_expenses)
        expected_paid_total = sum(exp.amount for exp in expected_expenses if exp.is_paid and not exp.is_bucket)
        expected_unpaid_total = sum(exp.amount for exp in expected_expenses if not exp.is_paid and not exp.is_bucket)
        planned_after_budget = monthly_budget - expected_total
        standard_expected_total = expected_paid_total + expected_unpaid_total
        plan_paid_percentage = (expected_paid_total / standard_expected_total * 100) if standard_expected_total else 0
        top_category_id = max(totals_map, key=totals_map.get) if totals_map else None
        top_category = next((cat for cat in all_categories if cat.id == top_category_id), None)
        days_elapsed = current_date.day
        days_remaining = 0
        daily_rate = total_spent / days_elapsed if days_elapsed else 0
        projected_month_total = total_spent
        budget_percentage = (total_spent / monthly_budget * 100) if monthly_budget else 0
        budget_remaining = monthly_budget - total_spent
        safe_daily_spend = 0
        daily_budget_target = 0
        burn_percentage = 0
    else:
        # Strict current active month default
        try:
            selected_month = int(raw_month) if raw_month else current_date.month
            if not (1 <= selected_month <= 12):
                selected_month = current_date.month
        except (ValueError, TypeError):
            selected_month = current_date.month

        selected_year = request.args.get('year', current_date.year, type=int)
        if selected_year < 2020 or selected_year > 2050:
            selected_year = current_date.year

        month_str = f"{selected_month:02d}"
        year_str = str(selected_year)
        month_name = calendar.month_name[selected_month]

        # Filter expenses strictly by the selected month, year, AND family
        all_expenses = Expense.query.filter(
            func.strftime('%Y', Expense.date) == year_str,
            func.strftime('%m', Expense.date) == month_str,
            Expense.family_id == current_user.family_id
        ).order_by(Expense.date.desc()).all()

        total_spent = sum(exp.amount for exp in all_expenses)
        finance = monthly_finance_for(current_user.family, selected_month, selected_year)
        monthly_income = finance['monthly_income']
        monthly_budget = finance['monthly_budget']

        totals = db.session.query(
            Expense.category_id,
            func.coalesce(func.sum(Expense.amount), 0).label('total')
        ).filter(
            func.strftime('%Y', Expense.date) == year_str,
            func.strftime('%m', Expense.date) == month_str,
            Expense.family_id == current_user.family_id
        ).group_by(Expense.category_id).all()

        totals_map = {int(cat_id): float(total) for cat_id, total in totals}

        chart_labels = []
        chart_data = []
        chart_colors = []
        for cat in all_categories:
            cat_total = totals_map.get(cat.id, 0.0)
            if cat_total > 0:
                chart_labels.append(cat.name)
                chart_data.append(cat_total)
                chart_colors.append(cat.color)

        expected_expenses = ExpectedExpense.query.filter_by(
            month=selected_month, 
            year=selected_year, 
            family_id=current_user.family_id
        ).order_by(ExpectedExpense.amount.desc()).all()

        budget_bucket_items = [exp for exp in expected_expenses if exp.is_bucket]
        budget_buckets = build_budget_bucket_summaries(
            budget_bucket_items,
            current_user.family_id,
            month_str,
            year_str
        )
        bucket_unspent_total = sum(bucket['remaining'] for bucket in budget_buckets)
        bucket_allocated_total = sum(bucket['allocated'] for bucket in budget_buckets)
        bucket_spent_total = sum(bucket['spent'] for bucket in budget_buckets)
        bucket_overflow_total = sum(bucket['overflow'] for bucket in budget_buckets)

        projected_savings = monthly_income - total_spent - bucket_unspent_total
        expected_total = sum(expected_budget_amount(exp) for exp in expected_expenses)
        expected_paid_total = sum(exp.amount for exp in expected_expenses if exp.is_paid and not exp.is_bucket)
        expected_unpaid_total = sum(exp.amount for exp in expected_expenses if not exp.is_paid and not exp.is_bucket)
        planned_after_budget = monthly_budget - expected_total
        standard_expected_total = expected_paid_total + expected_unpaid_total
        plan_paid_percentage = (expected_paid_total / standard_expected_total * 100) if standard_expected_total else 0
        top_category_id = max(totals_map, key=totals_map.get) if totals_map else None
        top_category = next((cat for cat in all_categories if cat.id == top_category_id), None)
        selected_days_in_month = calendar.monthrange(selected_year, selected_month)[1]
        if selected_year == current_date.year and selected_month == current_date.month:
            days_elapsed = current_date.day
            days_remaining = max(selected_days_in_month - current_date.day, 0)
        elif date(selected_year, selected_month, 1) < date(current_date.year, current_date.month, 1):
            days_elapsed = selected_days_in_month
            days_remaining = 0
        else:
            days_elapsed = 1
            days_remaining = selected_days_in_month
        daily_rate = total_spent / days_elapsed if days_elapsed else 0
        projected_month_total = daily_rate * selected_days_in_month
        budget_percentage = (total_spent / monthly_budget * 100) if monthly_budget else 0
        budget_remaining = monthly_budget - total_spent
        safe_daily_spend = budget_remaining / days_remaining if days_remaining else max(budget_remaining, 0)
        daily_budget_target = monthly_budget / selected_days_in_month if monthly_budget else 0
        burn_percentage = (daily_rate / daily_budget_target * 100) if daily_budget_target else 0

    return render_template('dashboard.html',
                            user=current_user,
                            family=current_user.family,
                            expenses=all_expenses,
                            categories=all_categories,
                            expected_expenses=expected_expenses,
                            total_spent=total_spent,
                            income=monthly_income,
                            monthly_budget=monthly_budget,
                            finance=finance,
                            projected=projected_savings,
                            chart_labels=chart_labels,
                            chart_data=chart_data,
                            chart_colors=chart_colors,
                            category_spent=totals_map,
                            category_spending=totals_map,
                            top_category=top_category,
                            days_elapsed=days_elapsed,
                            days_remaining=days_remaining,
                            daily_rate=daily_rate,
                            projected_month_total=projected_month_total,
                            daily_budget_target=daily_budget_target,
                            safe_daily_spend=safe_daily_spend,
                            budget_percentage=budget_percentage,
                            budget_remaining=budget_remaining,
                            burn_percentage=burn_percentage,
                            expected_total=expected_total,
                            expected_paid_total=expected_paid_total,
                            expected_unpaid_total=expected_unpaid_total,
                            planned_after_budget=planned_after_budget,
                            plan_paid_percentage=plan_paid_percentage,
                            budget_buckets=budget_buckets,
                            bucket_allocated_total=bucket_allocated_total,
                            bucket_spent_total=bucket_spent_total,
                            bucket_unspent_total=bucket_unspent_total,
                            bucket_overflow_total=bucket_overflow_total,
                            preset_categories=PRESET_CATEGORIES,
                            selected_month=selected_month,
                            selected_year=selected_year,
                            month_name=month_name,
                            is_all_time=is_all_time,
                            year_range=range(2024, datetime.utcnow().year + 3))


@app.route('/archive')
@login_required
def archive():
    """Show a monthly archive of months/years that contain expense data."""
    import calendar

    rows = db.session.query(
        func.strftime('%Y', Expense.date).label('year'),
        func.strftime('%m', Expense.date).label('month'),
        func.count(Expense.id).label('count')
    ).filter(
        Expense.family_id == current_user.family_id
    ).group_by('year', 'month').order_by(func.strftime('%Y', Expense.date).desc(), func.strftime('%m', Expense.date).desc()).all()

    months = []
    for year, month, count in rows:
        months.append({
            'year': int(year),
            'month': int(month),
            'month_name': calendar.month_name[int(month)],
            'count': int(count)
        })

    return render_template('archive.html', user=current_user, months=months)


@app.route('/financial_reports')
@login_required
def financial_reports():
    """Consolidated planning, forecasting, variance, and financial reports."""
    if not current_user.family_id:
        flash('Please join a family before opening financial reports.', 'error')
        return redirect(url_for('dashboard'))

    current_day = datetime.utcnow().date()
    selected_month = request.args.get('month', current_day.month, type=int)
    selected_year = request.args.get('year', current_day.year, type=int)
    if not (1 <= selected_month <= 12):
        selected_month = current_day.month
    if selected_year < 2020 or selected_year > 2050:
        selected_year = current_day.year

    month_start, month_end = month_bounds(selected_year, selected_month)
    finance = monthly_finance_for(current_user.family, selected_month, selected_year)
    monthly_income = finance['monthly_income']
    monthly_budget = finance['monthly_budget']

    categories = Category.query.filter_by(family_id=current_user.family_id).order_by(Category.name.asc()).all()
    expenses = Expense.query.filter(
        Expense.family_id == current_user.family_id,
        Expense.date >= month_start,
        Expense.date <= month_end
    ).order_by(Expense.date.desc()).all()
    expected_expenses = ExpectedExpense.query.filter_by(
        family_id=current_user.family_id,
        month=selected_month,
        year=selected_year
    ).order_by(ExpectedExpense.due_day.asc(), ExpectedExpense.amount.desc()).all()

    actual_by_category = {category.id: 0.0 for category in categories}
    count_by_category = {category.id: 0 for category in categories}
    spent_by_user = {}
    for expense in expenses:
        actual_by_category[expense.category_id] = actual_by_category.get(expense.category_id, 0.0) + float(expense.amount or 0)
        count_by_category[expense.category_id] = count_by_category.get(expense.category_id, 0) + 1
        spent_by_user[expense.user.username] = spent_by_user.get(expense.user.username, 0.0) + float(expense.amount or 0)

    planned_by_category = {category.id: 0.0 for category in categories}
    for item in expected_expenses:
        planned_by_category[item.category_id] = planned_by_category.get(item.category_id, 0.0) + expected_budget_amount(item)

    ai_suggested_plan = latest_ai_suggested_plan(current_user.family_id, month_start, month_end)
    ai_suggested_by_category = suggestion_category_totals(ai_suggested_plan, month_start, month_end)

    total_spent = sum(actual_by_category.values())
    planned_total = sum(expected_budget_amount(item) for item in expected_expenses)
    posted_total = sum(float(item.amount or 0) for item in expected_expenses if item.is_paid and not item.is_bucket)
    unposted_total = sum(float(item.amount or 0) for item in expected_expenses if not item.is_paid and not item.is_bucket)
    bucket_total = sum(expected_budget_amount(item) for item in expected_expenses if item.is_bucket)
    budget_remaining = monthly_budget - total_spent
    budget_used_percentage = (total_spent / monthly_budget * 100) if monthly_budget else 0

    variance_rows = []
    for category in categories:
        planned = planned_by_category.get(category.id, 0.0) or float(category.monthly_limit or 0)
        actual = actual_by_category.get(category.id, 0.0)
        ai_suggested = ai_suggested_by_category.get(category.name, 0.0)
        if not planned and not actual and not ai_suggested:
            continue
        variance = actual - planned
        ai_variance = actual - ai_suggested
        variance_rows.append({
            'name': category.name,
            'color': category.color,
            'planned': planned,
            'ai_suggested': ai_suggested,
            'actual': actual,
            'variance': variance,
            'ai_variance': ai_variance,
            'variance_percentage': (variance / planned * 100) if planned else 100,
            'ai_variance_percentage': (ai_variance / ai_suggested * 100) if ai_suggested else 100,
            'status': 'Over' if variance > 0 else 'Under' if variance < 0 else 'On plan'
        })
    variance_rows.sort(key=lambda row: max(abs(row['variance']), abs(row['ai_variance'])), reverse=True)

    expense_rows = []
    for category in categories:
        amount = actual_by_category.get(category.id, 0.0)
        count = count_by_category.get(category.id, 0)
        if amount <= 0 and count == 0:
            continue
        expense_rows.append({
            'name': category.name,
            'color': category.color,
            'amount': amount,
            'count': count,
            'average': amount / count if count else 0,
            'share': (amount / total_spent * 100) if total_spent else 0
        })
    expense_rows.sort(key=lambda row: row['amount'], reverse=True)

    selected_days = calendar.monthrange(selected_year, selected_month)[1]
    selected_month_start = date(selected_year, selected_month, 1)
    current_month_start = date(current_day.year, current_day.month, 1)
    if selected_month_start == current_month_start:
        days_elapsed = max(current_day.day, 1)
        days_remaining = max(selected_days - current_day.day, 0)
    elif selected_month_start < current_month_start:
        days_elapsed = selected_days
        days_remaining = 0
    else:
        days_elapsed = 1
        days_remaining = selected_days

    daily_rate = total_spent / days_elapsed if days_elapsed else 0
    forecast_total = daily_rate * selected_days
    forecast_remaining = monthly_budget - forecast_total
    safe_daily_spend = budget_remaining / days_remaining if days_remaining else max(budget_remaining, 0)
    forecast_status = 'Healthy' if forecast_total <= monthly_budget else 'At risk'

    cash_flow_rows = []
    for offset in range(5, -1, -1):
        month_index = (selected_year * 12 + selected_month - 1) - offset
        row_year = month_index // 12
        row_month = month_index % 12 + 1
        row_start, row_end = month_bounds(row_year, row_month)
        row_spent = db.session.query(func.coalesce(func.sum(Expense.amount), 0)).filter(
            Expense.family_id == current_user.family_id,
            Expense.date >= row_start,
            Expense.date <= row_end
        ).scalar() or 0
        row_planned = sum(
            expected_budget_amount(item)
            for item in ExpectedExpense.query.filter_by(
                family_id=current_user.family_id,
                month=row_month,
                year=row_year
            ).all()
        )
        cash_flow_rows.append({
            'label': f'{calendar.month_abbr[row_month]} {row_year}',
            'income': monthly_finance_for(current_user.family, row_month, row_year)['monthly_income'],
            'planned': float(row_planned),
            'outflow': float(row_spent),
            'net': monthly_finance_for(current_user.family, row_month, row_year)['monthly_income'] - float(row_spent)
        })

    top_spender = max(spent_by_user.items(), key=lambda item: item[1]) if spent_by_user else None
    pnl_rows = [
        {'label': 'Monthly income', 'amount': monthly_income, 'type': 'income'},
        {'label': 'Spending budget', 'amount': monthly_budget, 'type': 'budget'},
        {'label': 'Actual expenses', 'amount': -total_spent, 'type': 'expense'},
        {'label': 'Unposted planned bills', 'amount': -unposted_total, 'type': 'commitment'},
    ]
    pnl_net = monthly_income - total_spent
    pnl_projected_net = monthly_income - forecast_total

    missing_feature_cards = [
        {
            'title': 'Budget Planning',
            'tier': 'Core',
            'body': 'A month plan that combines expected bills, reserved buckets, category limits, and remaining budget.',
            'metric': f'Rs. {planned_total:,.0f} planned'
        },
        {
            'title': 'Forecasting',
            'tier': 'Advanced',
            'body': 'Projects month-end spend from the current burn rate and shows safe daily spend for the rest of the month.',
            'metric': f'Rs. {forecast_total:,.0f} forecast'
        },
        {
            'title': 'Variance Analysis',
            'tier': 'Advanced',
            'body': 'Compares real category spend against the active plan or category limit to reveal over/under areas.',
            'metric': f'{len(variance_rows)} tracked lines'
        },
        {
            'title': 'Budget Report',
            'tier': 'Core',
            'body': 'Turns budget, spending, remaining balance, planned bills, and protected buckets into one month summary.',
            'metric': f'{budget_used_percentage:.1f}% used'
        },
        {
            'title': 'Cash Flow Report',
            'tier': 'Advanced',
            'body': 'Shows budgeted inflow, planned commitments, actual outflow, and net cash flow across recent months.',
            'metric': f'Rs. {sum(row["net"] for row in cash_flow_rows):,.0f} 6-mo net'
        },
        {
            'title': 'Expense Report',
            'tier': 'Core',
            'body': 'Breaks expenses down by category, transaction count, average transaction size, and spend share.',
            'metric': f'{len(expenses)} transactions'
        },
        {
            'title': 'Profit & Loss Report',
            'tier': 'Advanced',
            'body': 'Household-style P&L that treats the family budget as income and spending as expenses.',
            'metric': f'Rs. {pnl_net:,.0f} net'
        }
    ]

    return render_template(
        'financial_reports.html',
        user=current_user,
        family=current_user.family,
        selected_month=selected_month,
        selected_year=selected_year,
        month_name=calendar.month_name[selected_month],
        year_range=range(2024, datetime.utcnow().year + 3),
        monthly_budget=monthly_budget,
        monthly_income=monthly_income,
        finance=finance,
        total_spent=total_spent,
        planned_total=planned_total,
        posted_total=posted_total,
        unposted_total=unposted_total,
        bucket_total=bucket_total,
        budget_remaining=budget_remaining,
        budget_used_percentage=budget_used_percentage,
        variance_rows=variance_rows,
        ai_suggested_plan=ai_suggested_plan,
        ai_suggested_total=sum(ai_suggested_by_category.values()),
        expense_rows=expense_rows,
        cash_flow_rows=cash_flow_rows,
        forecast_total=forecast_total,
        forecast_remaining=forecast_remaining,
        forecast_status=forecast_status,
        daily_rate=daily_rate,
        safe_daily_spend=safe_daily_spend,
        days_remaining=days_remaining,
        pnl_rows=pnl_rows,
        pnl_net=pnl_net,
        pnl_projected_net=pnl_projected_net,
        top_spender=top_spender,
        missing_feature_cards=missing_feature_cards,
        chart_labels=[row['name'] for row in expense_rows],
        chart_values=[row['amount'] for row in expense_rows],
        chart_colors=[row['color'] for row in expense_rows],
        cash_flow_labels=[row['label'] for row in cash_flow_rows],
        cash_flow_income=[row['income'] for row in cash_flow_rows],
        cash_flow_outflow=[row['outflow'] for row in cash_flow_rows],
        cash_flow_net=[row['net'] for row in cash_flow_rows]
    )


@app.route('/financial_reports/ai_suggested_plan', methods=['POST'])
@login_required
def generate_ai_suggested_plan_now():
    return_to = request.form.get('return_to', 'financial_reports')
    if not is_family_manager(current_user):
        flash('Unauthorized: family manager only', 'error')
        return redirect(url_for('ai_budget' if return_to == 'ai_budget' else 'financial_reports'))
    if not current_user.family_id:
        flash('Please join a family before generating an AI plan.', 'error')
        return redirect(url_for('dashboard'))

    current_day = datetime.utcnow().date()
    selected_month = request.form.get('month', current_day.month, type=int)
    selected_year = request.form.get('year', current_day.year, type=int)
    if not (1 <= selected_month <= 12):
        selected_month = current_day.month
    if selected_year < 2020 or selected_year > 2050:
        selected_year = current_day.year

    try:
        suggestion = create_ai_suggested_plan(
            current_user.family_id,
            current_user.id,
            target_month=selected_month,
            target_year=selected_year,
            source='manual',
            replace_today=True
        )
    except ValueError as exc:
        db.session.rollback()
        flash(f'AI suggested plan could not run: {exc}', 'error')
        if return_to == 'ai_budget':
            return redirect(url_for('ai_budget'))
        return redirect(url_for('financial_reports', month=selected_month, year=selected_year))
    except Exception as exc:
        db.session.rollback()
        app.logger.exception('AI suggested plan failed')
        flash(f'AI suggested plan failed unexpectedly: {exc}', 'error')
        if return_to == 'ai_budget':
            return redirect(url_for('ai_budget'))
        return redirect(url_for('financial_reports', month=selected_month, year=selected_year))

    flash(f'AI suggested plan saved: Rs. {suggestion.total_planned:,.0f} across {calendar.month_name[selected_month]} {selected_year}.', 'success')
    if return_to == 'ai_budget':
        return redirect(url_for('ai_budget', suggestion_id=suggestion.id))
    return redirect(url_for('financial_reports', month=selected_month, year=selected_year))


@app.route('/daily_diary')
@login_required
def daily_diary():
    """Focused day-by-day view of the same family expenses used by the dashboard."""
    if not current_user.family_id:
        flash('Please join a family before opening the Daily Diary.', 'error')
        return redirect(url_for('dashboard'))

    today = datetime.now().date()
    selected_date_str = request.args.get('date', '').strip()
    mine_only = request.args.get('mine') == '1'

    if selected_date_str:
        try:
            selected_date = datetime.strptime(selected_date_str, '%Y-%m-%d').date()
        except ValueError:
            selected_date = today
            flash('Invalid date selected. Showing today instead.', 'info')
    else:
        selected_date = today

    all_day_expenses = Expense.query.filter_by(
        date=selected_date,
        family_id=current_user.family_id
    ).order_by(Expense.id.desc()).all()

    family_daily_total = sum(exp.amount for exp in all_day_expenses)
    my_daily_total = sum(exp.amount for exp in all_day_expenses if exp.user_id == current_user.id)

    if mine_only:
        expenses = [exp for exp in all_day_expenses if exp.user_id == current_user.id]
    else:
        expenses = all_day_expenses

    categories = Category.query.filter_by(
        family_id=current_user.family_id
    ).order_by(Category.name.asc()).all()

    daily_total = sum(exp.amount for exp in expenses)
    quick_dates = [
        {'label': 'Yesterday', 'date': today - timedelta(days=1)},
        {'label': 'Today', 'date': today},
        {'label': 'Tomorrow', 'date': today + timedelta(days=1)},
    ]

    return render_template(
        'daily_diary.html',
        user=current_user,
        expenses=expenses,
        categories=categories,
        daily_total=daily_total,
        family_daily_total=family_daily_total,
        my_daily_total=my_daily_total,
        mine_only=mine_only,
        selected_date=selected_date,
        today=today,
        previous_date=selected_date - timedelta(days=1),
        next_date=selected_date + timedelta(days=1),
        quick_dates=quick_dates
    )

# --- NEW ROUTE: PERSONAL SPENDING TAB ---
@app.route('/my_spendings')
@login_required
def my_spendings():
    if not current_user.family_id:
        flash('Please join a family before viewing personal spendings.', 'error')
        return redirect(url_for('dashboard'))

    all_categories = Category.query.filter_by(family_id=current_user.family_id).order_by(Category.name.asc()).all()
    my_expenses = Expense.query.filter_by(
        user_id=current_user.id, 
        family_id=current_user.family_id
    ).order_by(Expense.date.desc(), Expense.id.desc()).all()
    
    my_total = sum(exp.amount for exp in my_expenses)
    today = datetime.now().date()
    this_month_total = sum(
        exp.amount for exp in my_expenses
        if exp.date and exp.date.year == today.year and exp.date.month == today.month
    )
    avg_expense = (my_total / len(my_expenses)) if my_expenses else 0.0
    cat_totals = {}
    for exp in my_expenses:
        cname = exp.category.name if exp.category else 'Uncategorized'
        cat_totals[cname] = cat_totals.get(cname, 0.0) + float(exp.amount or 0)
    top_category_name = max(cat_totals.items(), key=lambda x: x[1])[0] if cat_totals else 'None'

    return render_template('my_spendings.html', 
                           user=current_user,
                           expenses=my_expenses,
                           categories=all_categories,
                           total_spent=my_total,
                           this_month_total=this_month_total,
                           avg_expense=avg_expense,
                           top_category_name=top_category_name,
                           today=today)

def push_expense_to_google_sheet(expense, family):
    """
    Push a new expense logged in VaultSync to the family's connected Google Sheet
    via its Google Apps Script Webhook. Non-blocking asynchronous daemon thread.
    """
    if not family or not getattr(family, 'google_sheet_webhook_url', None):
        return
    webhook_url = (family.google_sheet_webhook_url or '').strip()
    if not webhook_url or not webhook_url.startswith(('http://', 'https://')):
        return

    payload = {
        'action': 'add_expense',
        'token': family.google_sheet_sync_token,
        'expense': {
            'id': expense.id,
            'date': expense.date.isoformat() if hasattr(expense.date, 'isoformat') else str(expense.date),
            'item_name': expense.item_name,
            'category': expense.category.name if expense.category else 'Other',
            'amount': round(float(expense.amount), 2),
            'member': expense.user.username if expense.user else 'Family',
            'description': expense.description or '',
        }
    }

    def _do_push():
        try:
            import urllib.request
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                webhook_url,
                data=data,
                headers={
                    'Content-Type': 'application/json',
                    'User-Agent': 'VaultSync-Bridge/1.0'
                }
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                resp.read()
        except Exception as e:
            app.logger.debug(f"Google Sheet push sync notice: {e}")

    threading.Thread(target=_do_push, daemon=True).start()


# --- UPDATED: ADD EXPENSE WITH DATE ---
@app.route('/add_expense', methods=['POST'])
@login_required
def add_expense():
    item_name = request.form.get('item_name', '').strip()
    description = request.form.get('description', '').strip()
    amount = request.form.get('amount')
    category_id = request.form.get('category_id')
    expense_date_str = request.form.get('expense_date')

    if not current_user.family_id:
        flash('Please join a family before adding expenses.', 'error')
        return back_or('dashboard')

    category = Category.query.filter_by(
        id=category_id,
        family_id=current_user.family_id
    ).first()

    if not (item_name and amount and category):
        flash('Please provide a valid item, amount, and family category.', 'error')
        return back_or('dashboard')

    try:
        expense_amount = float(amount)
        expense_date = datetime.strptime(expense_date_str, '%Y-%m-%d').date() if expense_date_str else datetime.utcnow().date()
        if expense_amount <= 0:
            raise ValueError
    except (ValueError, TypeError):
        flash('Please provide a valid expense amount and date.', 'error')
        return back_or('dashboard')

    budget_bucket = find_budget_bucket(current_user.family_id, category.id, expense_date)
    new_expense = Expense(
        date=expense_date,
        item_name=item_name,
        description=description,
        amount=expense_amount,
        category_id=category.id,
        user_id=current_user.id,
        family_id=current_user.family_id,
        bucket_id=budget_bucket.id if budget_bucket else None
    )
    db.session.add(new_expense)
    db.session.commit()
    push_expense_to_google_sheet(new_expense, current_user.family)

    # Check the master pool after accounting for unspent bucket reserves.
    month_str = f"{expense_date.month:02d}"
    year_str = str(expense_date.year)

    monthly_total = db.session.query(
        func.sum(Expense.amount).label('total')
    ).filter(
        func.strftime('%Y', Expense.date) == year_str,
        func.strftime('%m', Expense.date) == month_str,
        Expense.family_id == current_user.family_id
    ).scalar() or 0.0

    family = current_user.family
    budget_bucket_items = ExpectedExpense.query.filter_by(
        family_id=current_user.family_id,
        month=expense_date.month,
        year=expense_date.year,
        is_bucket=True
    ).all()
    bucket_summaries = build_budget_bucket_summaries(
        budget_bucket_items,
        current_user.family_id,
        month_str,
        year_str
    )
    bucket_unspent_total = sum(bucket['remaining'] for bucket in bucket_summaries)
    protected_total = monthly_total + bucket_unspent_total

    if budget_bucket:
        linked_summary = next(
            (bucket for bucket in bucket_summaries if bucket['bucket'].id == budget_bucket.id),
            None
        )
        if linked_summary and linked_summary['is_overflow']:
            flash(
                f"Bucket Alert: {budget_bucket.name} is over by Rs. {linked_summary['overflow']:,.0f}. "
                "The overflow now comes from the general budget pool.",
                'warning'
            )

    expense_month_budget = monthly_finance_for(family, expense_date.month, expense_date.year)['monthly_budget']
    if expense_month_budget and protected_total > expense_month_budget:
        percentage = (protected_total / expense_month_budget) * 100
        flash(f'Budget Alert: Your family has committed {percentage:.1f}% of the monthly budget.', 'warning')
    elif expense_month_budget and protected_total > (expense_month_budget * 0.8):
        percentage = (protected_total / expense_month_budget) * 100
        flash(f"You are at {percentage:.1f}% of your monthly budget after reserved buckets.", 'info')

    # Send them back to wherever they submitted the form from
    return back_or('dashboard')


@app.route('/add_expected', methods=['POST'])
@login_required
def add_expected():
    # Admin-only access
    if not is_family_manager(current_user):
        flash('Unauthorized: family manager only', 'error')
        return back_or('dashboard')

    name = request.form.get('name')
    amount = request.form.get('amount')
    category_id = request.form.get('category_id')
    month = request.form.get('month')
    year = request.form.get('year')
    due_day = request.form.get('due_day', 1)
    is_bucket = request.form.get('is_bucket') == 'on'

    if not (name and amount and category_id and month and year and due_day):
        flash('Please provide name, amount, category, due day, month and year.', 'error')
        return back_or('dashboard')

    category = Category.query.filter_by(
        id=category_id,
        family_id=current_user.family_id
    ).first()
    if not category:
        flash('Please choose a valid family category.', 'error')
        return back_or('dashboard')

    try:
        expected_amount = float(amount)
        expected_month = int(month)
        expected_year = int(year)
        expected_due_day = int(due_day)
        if expected_amount <= 0 or not (1 <= expected_month <= 12) or not (1 <= expected_due_day <= 31):
            raise ValueError
    except (ValueError, TypeError):
        flash('Please provide a valid amount, due day, month, and year.', 'error')
        return back_or('dashboard')

    new_expected = ExpectedExpense(
        name=name.strip(),
        amount=expected_amount,
        is_bucket=is_bucket,
        allocated_amount=expected_amount if is_bucket else 0.0,
        category_id=category.id,
        due_day=expected_due_day,
        month=expected_month,
        year=expected_year,
        family_id=current_user.family_id
    )
    db.session.add(new_expected)
    db.session.commit()
    return back_or('dashboard')


@app.route('/toggle_expected/<int:id>', methods=['GET', 'POST'])
@login_required
def toggle_expected(id):
    if not is_family_manager(current_user):
        flash('Unauthorized: family manager only', 'error')
        return back_or('dashboard')
    ee = ExpectedExpense.query.filter_by(
        id=id,
        family_id=current_user.family_id
    ).first_or_404()

    if ee.is_bucket:
        flash('Reserved buckets are drawn down by linked daily expenses instead of being marked paid.', 'info')
        return back_or('dashboard')

    if ee.is_paid:
        if ee.linked_expense_id:
            linked_expense = Expense.query.filter_by(
                id=ee.linked_expense_id,
                family_id=current_user.family_id
            ).first()
            if linked_expense:
                db.session.delete(linked_expense)
        ee.is_paid = False
        ee.paid_at = None
        ee.linked_expense_id = None
        flash('Planned expense moved back to upcoming.', 'info')
    else:
        linked_expense = Expense(
            date=expected_expense_date(ee),
            item_name=f"Budget Plan: {ee.name}",
            description='Posted from Expected Monthly Expenditure.',
            amount=ee.amount,
            category_id=ee.category_id,
            user_id=current_user.id,
            family_id=current_user.family_id
        )
        db.session.add(linked_expense)
        db.session.flush()
        ee.is_paid = True
        ee.paid_at = datetime.utcnow()
        ee.linked_expense_id = linked_expense.id
        flash('Planned expense marked paid and posted to actual spending.', 'success')

    db.session.commit()
    return back_or('dashboard')


@app.route('/edit_expense/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_expense(id):
    expense = Expense.query.filter_by(
        id=id,
        family_id=current_user.family_id
    ).first_or_404()

    categories = Category.query.filter_by(
        family_id=current_user.family_id
    ).order_by(Category.name.asc()).all()
    family_users = User.query.filter_by(
        family_id=current_user.family_id,
        is_active=True
    ).order_by(User.username.asc()).all()

    linked_plan = ExpectedExpense.query.filter_by(
        linked_expense_id=expense.id,
        family_id=current_user.family_id
    ).first()

    if request.method == 'POST':
        item_name = request.form.get('item_name', '').strip()
        description = request.form.get('description', '').strip()
        amount = request.form.get('amount')
        category_id = request.form.get('category_id')
        expense_date_str = request.form.get('expense_date')
        user_id = request.form.get('user_id')

        category = Category.query.filter_by(
            id=category_id,
            family_id=current_user.family_id
        ).first()

        if not (item_name and amount and category and expense_date_str):
            flash('Please provide item name, amount, category, and date.', 'error')
            return redirect(url_for('edit_expense', id=id))

        try:
            expense_amount = float(amount)
            expense_date = datetime.strptime(expense_date_str, '%Y-%m-%d').date()
            if expense_amount <= 0:
                raise ValueError
        except (ValueError, TypeError):
            flash('Please provide a valid amount and date.', 'error')
            return redirect(url_for('edit_expense', id=id))

        expense.item_name = item_name
        expense.description = description
        expense.amount = expense_amount
        expense.category_id = category.id
        expense.date = expense_date
        if is_family_manager(current_user) and user_id:
            spender = User.query.filter_by(
                id=user_id,
                family_id=current_user.family_id,
                is_active=True
            ).first()
            if not spender:
                flash('Please choose a valid family member for this expense.', 'error')
                return redirect(url_for('edit_expense', id=id))
            expense.user_id = spender.id

        if linked_plan:
            linked_plan.name = item_name.replace('Budget Plan: ', '', 1).strip() or item_name
            linked_plan.amount = expense_amount
            linked_plan.category_id = category.id
            linked_plan.month = expense_date.month
            linked_plan.year = expense_date.year
            linked_plan.due_day = expense_date.day
            expense.bucket_id = None
        else:
            budget_bucket = find_budget_bucket(current_user.family_id, category.id, expense_date)
            expense.bucket_id = budget_bucket.id if budget_bucket else None

        db.session.commit()
        flash('Expense updated successfully.', 'success')
        return redirect(request.form.get('next') or url_for('dashboard'))

    return render_template(
        'edit_expense.html',
        user=current_user,
        expense=expense,
        categories=categories,
        family_users=family_users,
        linked_plan=linked_plan,
        next_url=request.referrer or url_for('dashboard')
    )


@app.route('/delete_expected/<int:id>', methods=['POST'])
@login_required
def delete_expected(id):
    if not is_family_manager(current_user):
        flash('Unauthorized: family manager only', 'error')
        return back_or('dashboard')

    ee = ExpectedExpense.query.filter_by(
        id=id,
        family_id=current_user.family_id
    ).first_or_404()

    if ee.is_bucket:
        Expense.query.filter_by(
            bucket_id=ee.id,
            family_id=current_user.family_id
        ).update({'bucket_id': None})

    if ee.linked_expense_id:
        linked_expense = Expense.query.filter_by(
            id=ee.linked_expense_id,
            family_id=current_user.family_id
        ).first()
        if linked_expense:
            db.session.delete(linked_expense)

    db.session.delete(ee)
    db.session.commit()
    flash('Planned expense deleted.', 'success')
    return back_or('dashboard')


@app.route('/edit_expected/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_expected(id):
    if not is_family_manager(current_user):
        flash('Unauthorized: family manager only', 'error')
        return back_or('dashboard')

    planned = ExpectedExpense.query.filter_by(
        id=id,
        family_id=current_user.family_id
    ).first_or_404()

    categories = Category.query.filter_by(
        family_id=current_user.family_id
    ).order_by(Category.name.asc()).all()

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        amount = request.form.get('amount')
        category_id = request.form.get('category_id')
        month = request.form.get('month')
        year = request.form.get('year')
        due_day = request.form.get('due_day')
        requested_is_bucket = request.form.get('is_bucket') == 'on'

        category = Category.query.filter_by(
            id=category_id,
            family_id=current_user.family_id
        ).first()

        if not (name and amount and category and month and year and due_day):
            flash('Please provide every planned expense field.', 'error')
            return redirect(url_for('edit_expected', id=id))

        if planned.is_paid and requested_is_bucket != planned.is_bucket:
            flash('Unpost this planned payment before changing it into or out of a bucket.', 'error')
            return redirect(url_for('edit_expected', id=id))

        try:
            planned_amount = float(amount)
            planned_month = int(month)
            planned_year = int(year)
            planned_due_day = int(due_day)
            if planned_amount <= 0 or not (1 <= planned_month <= 12) or not (1 <= planned_due_day <= 31):
                raise ValueError
        except (ValueError, TypeError):
            flash('Please provide valid amount, month, year, and due day.', 'error')
            return redirect(url_for('edit_expected', id=id))

        planned.name = name
        planned.amount = planned_amount
        planned.is_bucket = requested_is_bucket
        planned.allocated_amount = planned_amount if requested_is_bucket else 0.0
        if requested_is_bucket:
            planned.is_paid = False
            planned.paid_at = None
        planned.category_id = category.id
        planned.month = planned_month
        planned.year = planned_year
        planned.due_day = planned_due_day

        if planned.linked_expense_id:
            linked_expense = Expense.query.filter_by(
                id=planned.linked_expense_id,
                family_id=current_user.family_id
            ).first()
            if linked_expense:
                linked_expense.item_name = f"Budget Plan: {name}"
                linked_expense.amount = planned_amount
                linked_expense.category_id = category.id
                linked_expense.date = expected_expense_date(planned)
                linked_expense.bucket_id = None

        db.session.commit()
        flash('Planned expense updated successfully.', 'success')
        return redirect(request.form.get('next') or url_for('dashboard'))

    return render_template(
        'edit_expected.html',
        user=current_user,
        planned=planned,
        categories=categories,
        year_range=range(2024, datetime.utcnow().year + 4),
        next_url=request.referrer or url_for('dashboard')
    )


@app.route('/copy_expected_plan', methods=['POST'])
@login_required
def copy_expected_plan():
    if not is_family_manager(current_user):
        flash('Unauthorized: family manager only', 'error')
        return back_or('dashboard')

    try:
        source_month = int(request.form.get('month'))
        source_year = int(request.form.get('year'))
    except (ValueError, TypeError):
        flash('Please choose a valid month to copy.', 'error')
        return back_or('dashboard')

    next_month = source_month + 1
    next_year = source_year
    if next_month > 12:
        next_month = 1
        next_year += 1

    source_items = ExpectedExpense.query.filter_by(
        family_id=current_user.family_id,
        month=source_month,
        year=source_year
    ).all()

    copied = 0
    for item in source_items:
        exists = ExpectedExpense.query.filter_by(
            family_id=current_user.family_id,
            month=next_month,
            year=next_year,
            name=item.name,
            category_id=item.category_id
        ).first()
        if exists:
            continue
        db.session.add(ExpectedExpense(
            name=item.name,
            amount=item.amount,
            is_bucket=item.is_bucket,
            allocated_amount=expected_budget_amount(item) if item.is_bucket else 0.0,
            due_day=item.due_day,
            category_id=item.category_id,
            month=next_month,
            year=next_year,
            family_id=current_user.family_id
        ))
        copied += 1

    db.session.commit()
    flash(f'Copied {copied} planned expenses to next month.', 'success')
    return redirect(url_for('dashboard', month=next_month, year=next_year))

@app.route('/add_category', methods=['POST'])
@login_required
def add_category():
    name = request.form.get('name', '').strip()
    color = request.form.get('color', '').strip()
    monthly_limit = request.form.get('monthly_limit', 0)
    if not current_user.family_id:
        flash('Please join a family before adding categories.', 'error')
        return back_or('dashboard')

    if not name:
        flash('Please provide a category name.', 'error')
        return back_or('dashboard')

    try:
        monthly_limit = float(monthly_limit or 0)
        if monthly_limit < 0:
            raise ValueError
    except (ValueError, TypeError):
        flash('Please provide a valid monthly category limit.', 'error')
        return back_or('dashboard')

    if Category.query.filter_by(name=name, family_id=current_user.family_id).first():
        flash('A category with that name already exists.', 'error')
        return back_or('dashboard')

    # Auto-color assignment: if no color provided, use auto-generate mode
    is_color_auto = not color
    if not color:
        color = get_next_auto_color(current_user.family_id)

    new_cat = Category(
        name=name,
        color=color,
        monthly_limit=monthly_limit,
        is_color_auto=is_color_auto,
        family_id=current_user.family_id
    )
    db.session.add(new_cat)
    db.session.commit()
    
    # Re-assign colors to all auto-color categories to maintain distinctness
    reassign_auto_colors(current_user.family_id)
    
    flash('Category added successfully.', 'success')
    return back_or('dashboard')


@app.route('/apply_category_presets', methods=['POST'])
@login_required
def apply_category_presets():
    if not current_user.family_id:
        flash('Please join a family before adding categories.', 'error')
        return back_or('dashboard')

    created = ensure_preset_categories(current_user.family_id)
    db.session.commit()

    if created:
        flash(f'Added {created} recommended categories.', 'success')
    else:
        flash('Your family already has all recommended categories.', 'info')
    return back_or('dashboard')

@app.route('/edit_category/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_category(id):
    category = Category.query.filter_by(id=id, family_id=current_user.family_id).first_or_404()
    other_categories = Category.query.filter(
        Category.family_id == current_user.family_id,
        Category.id != id
    ).order_by(Category.name.asc()).all()
    expenses_count = Expense.query.filter_by(category_id=id, family_id=current_user.family_id).count()
    expected_count = ExpectedExpense.query.filter_by(category_id=id, family_id=current_user.family_id).count()
    current_month = datetime.utcnow()
    month_spent = db.session.query(func.coalesce(func.sum(Expense.amount), 0)).filter(
        Expense.category_id == id,
        Expense.family_id == current_user.family_id,
        func.strftime('%Y', Expense.date) == str(current_month.year),
        func.strftime('%m', Expense.date) == f"{current_month.month:02d}"
    ).scalar() or 0

    if request.method == 'GET':
        return render_template(
            'edit_category.html',
            user=current_user,
            category=category,
            other_categories=other_categories,
            expenses_count=expenses_count,
            expected_count=expected_count,
            month_spent=float(month_spent),
            next_url=request.referrer or url_for('dashboard')
        )

    name = request.form.get('name', '').strip()
    color = request.form.get('color', '').strip()
    monthly_limit = request.form.get('monthly_limit', 0)
    is_fixed = request.form.get('is_fixed') == 'on'
    if not (name and color):
        flash('Please provide a category name and color.', 'error')
        return redirect(url_for('edit_category', id=id))

    try:
        monthly_limit = float(monthly_limit or 0)
        if monthly_limit < 0:
            raise ValueError
    except (ValueError, TypeError):
        flash('Please provide a valid monthly category limit.', 'error')
        return redirect(url_for('edit_category', id=id))
    
    # Check if new name already exists in this family (but allow keeping same name)
    existing = Category.query.filter_by(name=name, family_id=current_user.family_id).first()
    if existing and existing.id != id:
        flash('A category with that name already exists.', 'error')
        return redirect(url_for('edit_category', id=id))
    
    category.name = name
    category.color = color
    category.monthly_limit = monthly_limit
    category.is_fixed = is_fixed
    category.is_color_auto = False  # User manually set the color, so disable auto-color
    db.session.commit()
    flash('Category updated successfully.', 'success')
    return redirect(request.form.get('next') or url_for('dashboard'))

@app.route('/delete_category/<int:id>', methods=['GET', 'POST'])
@login_required
def delete_category(id):
    category = Category.query.filter_by(id=id, family_id=current_user.family_id).first_or_404()
    # Check if category has expenses in this family
    expenses_count = Expense.query.filter_by(category_id=id, family_id=current_user.family_id).count()
    expected_count = ExpectedExpense.query.filter_by(category_id=id, family_id=current_user.family_id).count()
    if expenses_count == 0 and expected_count == 0:
        db.session.delete(category)
        db.session.commit()
        flash('Category deleted successfully.', 'success')
    else:
        try:
            replacement_id = int(request.form.get('replacement_category_id') or 0)
        except (ValueError, TypeError):
            replacement_id = 0
        replacement = Category.query.filter(
            Category.id == replacement_id,
            Category.id != id,
            Category.family_id == current_user.family_id
        ).first()
        if request.method == 'POST' and replacement:
            Expense.query.filter_by(
                category_id=id,
                family_id=current_user.family_id
            ).update({'category_id': replacement.id})
            ExpectedExpense.query.filter_by(
                category_id=id,
                family_id=current_user.family_id
            ).update({'category_id': replacement.id})
            db.session.delete(category)
            db.session.commit()
            flash(f'Moved existing items to {replacement.name} and deleted the category.', 'success')
        else:
            flash('This category has expenses or planned items. Choose a replacement category before deleting it.', 'error')
            return redirect(url_for('edit_category', id=id))
    return redirect(request.form.get('next') or url_for('dashboard'))

@app.route('/delete_expense/<int:id>')
@login_required
def delete_expense(id):
    expense_to_delete = Expense.query.filter_by(
        id=id, 
        family_id=current_user.family_id
    ).first_or_404()
    linked_plan = ExpectedExpense.query.filter_by(
        linked_expense_id=id,
        family_id=current_user.family_id
    ).first()
    if linked_plan:
        linked_plan.is_paid = False
        linked_plan.paid_at = None
        linked_plan.linked_expense_id = None
    db.session.delete(expense_to_delete)
    db.session.commit()
    return back_or('dashboard')


@app.route('/chatbot/message', methods=['POST'])
@login_required
def chatbot_message():
    if not current_user.family_id:
        return jsonify({
            'reply': 'Join a family first, then I can work with your VaultSync data.',
            'actions': [],
            'results': [],
        }), 400

    payload = request.get_json(silent=True) or {}
    message = str(payload.get('message') or '').strip()
    execute = bool(payload.get('execute'))
    actions = payload.get('actions') or []

    if execute:
        if not isinstance(actions, list) or not actions:
            return jsonify({'reply': 'No actions were provided to run.', 'actions': [], 'results': []}), 400

        results = []
        try:
            for action_payload in actions[:8]:
                results.append(execute_chatbot_action(action_payload))
            save_chatbot_message(
                'assistant',
                'Ran approved actions. ' + ' '.join(results),
                actions=actions[:8],
                results=results,
            )
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            return jsonify({
                'reply': f'I stopped before saving because: {exc}',
                'actions': [],
                'results': results,
            }), 400

        return jsonify({
            'reply': 'Done. ' + ' '.join(results),
            'actions': [],
            'results': results,
        })

    if not message:
        return jsonify({'reply': 'Tell me what you want to do.', 'actions': [], 'results': []}), 400

    lowered_message = message.lower()
    if any(term in lowered_message for term in CHATBOT_FORBIDDEN_TERMS):
        reply = 'I cannot change security, account, member, invite, role, or family deletion settings. A family manager must handle those directly from the Admin Panel.'
        save_chatbot_message('user', message)
        save_chatbot_message('assistant', reply)
        db.session.commit()
        return jsonify({
            'reply': reply,
            'actions': [],
            'results': [],
        })

    context_payload = chatbot_context_payload(current_user)
    save_chatbot_message('user', message)
    try:
        ai_payload = call_gemini_json(
            build_chatbot_prompt(message, context_payload),
            chatbot_response_schema()
        )
    except Exception as exc:
        app.logger.exception('VaultSync Assistant failed')
        return jsonify({
            'reply': f'I could not reach the AI service right now: {exc}',
            'actions': [],
            'results': [],
        }), 502

    proposed_actions = ai_payload.get('actions') or []
    if not isinstance(proposed_actions, list):
        proposed_actions = []
    proposed_actions = proposed_actions[:8]

    reply = str(ai_payload.get('reply') or 'I can help with that.')
    save_chatbot_message('assistant', reply, actions=proposed_actions)
    db.session.commit()

    return jsonify({
        'reply': reply,
        'actions': proposed_actions,
        'results': [],
    })


@app.route('/chatbot/history', methods=['GET'])
@login_required
def chatbot_history():
    if not current_user.family_id:
        return jsonify({'messages': []})
    return jsonify({
        'messages': [
            serialize_chatbot_message(message)
            for message in recent_chatbot_messages(current_user, limit=20)
        ]
    })


@app.route('/features')
@login_required
def features():
    return render_template('features.html', user=current_user)


@app.route('/ai_budget')
@login_required
def ai_budget():
    if not current_user.family_id:
        flash('Please join a family before using the AI Budget Planner.', 'error')
        return redirect(url_for('dashboard'))

    suggestions = BudgetSuggestion.query.filter_by(
        family_id=current_user.family_id
    ).order_by(BudgetSuggestion.created_at.desc()).limit(10).all()
    applications = BudgetPlanApplication.query.filter_by(
        family_id=current_user.family_id
    ).order_by(BudgetPlanApplication.created_at.desc()).limit(8).all()

    selected_suggestion = None
    comparison = None
    suggestion_id = request.args.get('suggestion_id', type=int)
    if suggestion_id:
        selected_suggestion = BudgetSuggestion.query.filter_by(
            id=suggestion_id,
            family_id=current_user.family_id
        ).first_or_404()
        comparison = compare_suggestion_to_current(selected_suggestion)
    elif suggestions:
        selected_suggestion = suggestions[0]
        comparison = compare_suggestion_to_current(selected_suggestion)

    today = datetime.now().date()
    return render_template(
        'ai_budget.html',
        user=current_user,
        suggestions=suggestions,
        selected_suggestion=selected_suggestion,
        comparison=comparison,
        applications=applications,
        today=today,
        current_month=today.month,
        current_year=today.year,
        year_range=range(2024, today.year + 4)
    )


@app.route('/ai_budget/export', methods=['POST'])
@login_required
def export_ai_budget():
    if not current_user.family_id:
        flash('Please join a family before exporting budget data.', 'error')
        return redirect(url_for('dashboard'))

    try:
        period_type, start, end, target_start, target_end = parse_ai_budget_window(request.form)
    except (TypeError, ValueError):
        flash('Please choose a valid export date or month range.', 'error')
        return redirect(url_for('ai_budget'))

    payload = build_ai_budget_export(period_type, start, end, target_start, target_end)
    filename = f"vaultsync_ai_budget_{start.isoformat()}_to_{end.isoformat()}.json"
    return Response(
        json.dumps(payload, indent=2),
        mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


@app.route('/ai_budget/import', methods=['POST'])
@login_required
def import_ai_budget():
    if not current_user.family_id:
        flash('Please join a family before importing a budget suggestion.', 'error')
        return redirect(url_for('dashboard'))

    raw_text = request.form.get('ai_response', '').strip()
    uploaded = request.files.get('ai_response_file')
    if uploaded and uploaded.filename:
        raw_text = uploaded.read().decode('utf-8').strip()

    if not raw_text:
        flash('Paste the AI JSON response or upload a JSON file.', 'error')
        return redirect(url_for('ai_budget'))

    try:
        payload = json.loads(raw_text)
        payload, target_start, target_end, total = validate_budget_suggestion_payload(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        flash(f'Could not import AI budget: {exc}', 'error')
        return redirect(url_for('ai_budget'))

    source_start = parse_date_arg(request.form.get('source_start_date'), target_start)
    source_end = parse_date_arg(request.form.get('source_end_date'), target_end)

    strategy_notes = payload.get('strategy_notes') if isinstance(payload.get('strategy_notes'), list) else []
    guardrails = payload.get('guardrails') if isinstance(payload.get('guardrails'), list) else []
    notes = '\n'.join(str(note) for note in strategy_notes + guardrails)

    suggestion = BudgetSuggestion(
        family_id=current_user.family_id,
        created_by_user_id=current_user.id,
        source_start_date=source_start,
        source_end_date=source_end,
        target_start_date=target_start,
        target_end_date=target_end,
        title=str(payload.get('title') or 'AI Budget Suggestion')[:160],
        suggested_monthly_budget=float(payload.get('recommended_monthly_budget') or 0),
        total_planned=total,
        risk_level=str(payload.get('risk_level') or 'medium')[:20],
        notes=notes,
        raw_json=json.dumps(payload, indent=2),
    )
    db.session.add(suggestion)
    db.session.commit()
    flash('AI budget suggestion imported. Review the comparison before applying.', 'success')
    return redirect(url_for('ai_budget', suggestion_id=suggestion.id))


@app.route('/ai_budget/apply/<int:id>', methods=['POST'])
@login_required
def apply_ai_budget(id):
    if not is_family_manager(current_user):
        flash('Unauthorized: family manager only', 'error')
        return redirect(url_for('ai_budget'))

    suggestion = BudgetSuggestion.query.filter_by(
        id=id,
        family_id=current_user.family_id
    ).first_or_404()
    payload = suggestion_payload(suggestion)

    category_map = {
        category.name.lower(): category
        for category in Category.query.filter_by(family_id=current_user.family_id).all()
    }

    created_categories = 0
    created_items = 0
    skipped_items = 0
    created_category_ids = []
    created_expected_expense_ids = []
    target_finance_setting = MonthlyFinanceSetting.query.filter_by(
        family_id=current_user.family_id,
        month=suggestion.target_start_date.month,
        year=suggestion.target_start_date.year
    ).first()
    snapshot = {
        'suggestion_id': suggestion.id,
        'suggestion_title': suggestion.title,
        'target_month': suggestion.target_start_date.month,
        'target_year': suggestion.target_start_date.year,
        'family_monthly_income': current_user.family.monthly_income,
        'family_monthly_budget': current_user.family.monthly_budget,
        'target_finance_setting': finance_setting_snapshot(target_finance_setting),
        'changed_target_budget': request.form.get('update_monthly_budget') == 'yes' and bool(suggestion.suggested_monthly_budget),
    }
    for item in payload.get('planned_items', []):
        item_date = parse_date_arg(item.get('date'))
        if not item_date:
            skipped_items += 1
            continue

        category_name = str(item.get('category', 'Household')).strip() or 'Household'
        category = category_map.get(category_name.lower())
        if not category:
            category = Category(
                name=category_name,
                color='#64748b',
                monthly_limit=0,
                is_fixed=False,
                family_id=current_user.family_id
            )
            db.session.add(category)
            db.session.flush()
            category_map[category_name.lower()] = category
            created_categories += 1
            created_category_ids.append(category.id)

        name = str(item.get('name')).strip()
        amount = float(item.get('amount'))
        existing = ExpectedExpense.query.filter_by(
            family_id=current_user.family_id,
            month=item_date.month,
            year=item_date.year,
            name=name,
            category_id=category.id
        ).first()
        if existing:
            skipped_items += 1
            continue

        expected_item = ExpectedExpense(
            name=name,
            amount=amount,
            due_day=item_date.day,
            category_id=category.id,
            month=item_date.month,
            year=item_date.year,
            family_id=current_user.family_id
        )
        db.session.add(expected_item)
        db.session.flush()
        created_expected_expense_ids.append(expected_item.id)
        created_items += 1

    if request.form.get('update_monthly_budget') == 'yes' and suggestion.suggested_monthly_budget:
        if not target_finance_setting:
            target_finance_setting = MonthlyFinanceSetting(
                family_id=current_user.family_id,
                month=suggestion.target_start_date.month,
                year=suggestion.target_start_date.year
            )
            db.session.add(target_finance_setting)
        target_finance = monthly_finance_for(
            current_user.family,
            suggestion.target_start_date.month,
            suggestion.target_start_date.year
        )
        target_finance_setting.monthly_income = target_finance['monthly_income']
        target_finance_setting.monthly_budget = suggestion.suggested_monthly_budget
        target_finance_setting.updated_by_user_id = current_user.id
        target_finance_setting.updated_at = datetime.utcnow()

    suggestion.is_applied = True
    application = BudgetPlanApplication(
        family_id=current_user.family_id,
        suggestion_id=suggestion.id,
        applied_by_user_id=current_user.id,
        target_month=suggestion.target_start_date.month,
        target_year=suggestion.target_start_date.year,
        created_expected_expense_ids=json.dumps(created_expected_expense_ids),
        created_category_ids=json.dumps(created_category_ids),
        snapshot_json=json.dumps(snapshot, default=str),
        created_at=datetime.utcnow()
    )
    db.session.add(application)
    db.session.commit()
    flash(
        f'Applied {created_items} suggested planned items. '
        f'{skipped_items} duplicates skipped. {created_categories} new categories added. '
        f'Rollback #{application.id} is available in AI Budget Planner.',
        'success'
    )
    return redirect(url_for(
        'dashboard',
        month=suggestion.target_start_date.month,
        year=suggestion.target_start_date.year
    ))


@app.route('/ai_budget/revert/<int:application_id>', methods=['POST'])
@login_required
def revert_ai_budget_application(application_id):
    if not is_family_manager(current_user):
        flash('Unauthorized: family manager only', 'error')
        return redirect(url_for('ai_budget'))

    application = BudgetPlanApplication.query.filter_by(
        id=application_id,
        family_id=current_user.family_id
    ).first_or_404()
    if application.reverted_at:
        flash('That AI plan application has already been reverted.', 'info')
        return redirect(url_for('ai_budget', suggestion_id=application.suggestion_id))

    try:
        snapshot = json.loads(application.snapshot_json or '{}')
        expected_ids = json.loads(application.created_expected_expense_ids or '[]')
        category_ids = json.loads(application.created_category_ids or '[]')
    except (TypeError, json.JSONDecodeError):
        snapshot = {}
        expected_ids = []
        category_ids = []

    deleted_items = 0
    skipped_items = 0
    for expected_id in expected_ids:
        expected_item = ExpectedExpense.query.filter_by(
            id=expected_id,
            family_id=current_user.family_id
        ).first()
        if not expected_item:
            continue
        if expected_item.is_paid or expected_item.linked_expense_id:
            skipped_items += 1
            continue
        db.session.delete(expected_item)
        deleted_items += 1

    restored_budget = False
    if snapshot.get('changed_target_budget'):
        setting_snapshot = snapshot.get('target_finance_setting') or {}
        setting = MonthlyFinanceSetting.query.filter_by(
            family_id=current_user.family_id,
            month=application.target_month,
            year=application.target_year
        ).first()
        if setting_snapshot.get('existed'):
            if not setting:
                setting = MonthlyFinanceSetting(
                    family_id=current_user.family_id,
                    month=application.target_month,
                    year=application.target_year
                )
                db.session.add(setting)
            setting.monthly_income = setting_snapshot.get('monthly_income')
            setting.monthly_budget = setting_snapshot.get('monthly_budget')
            setting.notes = setting_snapshot.get('notes') or ''
            setting.updated_by_user_id = current_user.id
            setting.updated_at = datetime.utcnow()
        elif setting:
            db.session.delete(setting)
        restored_budget = True

    deleted_categories = 0
    for category_id in category_ids:
        category = Category.query.filter_by(
            id=category_id,
            family_id=current_user.family_id
        ).first()
        if not category:
            continue
        has_expenses = Expense.query.filter_by(category_id=category.id, family_id=current_user.family_id).first()
        has_expected = ExpectedExpense.query.filter_by(category_id=category.id, family_id=current_user.family_id).first()
        if has_expenses or has_expected:
            continue
        db.session.delete(category)
        deleted_categories += 1

    application.reverted_at = datetime.utcnow()
    active_applications = BudgetPlanApplication.query.filter(
        BudgetPlanApplication.suggestion_id == application.suggestion_id,
        BudgetPlanApplication.family_id == current_user.family_id,
        BudgetPlanApplication.reverted_at.is_(None),
        BudgetPlanApplication.id != application.id
    ).first()
    if not active_applications and application.suggestion:
        application.suggestion.is_applied = False

    db.session.commit()
    message = f'Reverted AI plan application #{application.id}: removed {deleted_items} planned item(s)'
    if deleted_categories:
        message += f', removed {deleted_categories} new categor{"y" if deleted_categories == 1 else "ies"}'
    if restored_budget:
        message += ', restored the month budget setting'
    if skipped_items:
        message += f'. {skipped_items} paid/linked item(s) were kept for safety'
    flash(message + '.', 'success' if not skipped_items else 'warning')
    return redirect(url_for('ai_budget', suggestion_id=application.suggestion_id))

@app.route('/export_ai_data')
@login_required
def export_ai_data():
    expenses = Expense.query.filter_by(family_id=current_user.family_id).all()
    def generate():
        data = StringIO()
        writer = csv.writer(data)
        writer.writerow(('Date', 'Spender', 'Item Name', 'Description', 'Category', 'Amount'))
        for exp in expenses:
            writer.writerow((exp.date.strftime("%Y-%m-%d"), exp.user.username, exp.item_name, exp.description or '', exp.category.name, exp.amount))
            yield data.getvalue()
            data.seek(0)
            data.truncate(0)

# ============================================================================
# AI Financial Advisor & Antigravity Decision Studio (Monthly, Quarterly, Annually)
# ============================================================================

def aggregate_family_financial_period(family_id, period_type='monthly', year=None, month=None, quarter=None):
    now = datetime.utcnow()
    year = int(year) if year and str(year).isdigit() else now.year
    period_type = str(period_type or 'monthly').lower()

    if period_type == 'quarterly':
        quarter = str(quarter or 'Q4').upper()
        q_map = {'Q1': [1, 2, 3], 'Q2': [4, 5, 6], 'Q3': [7, 8, 9], 'Q4': [10, 11, 12]}
        months = q_map.get(quarter, [10, 11, 12])
        period_label = f"{quarter} {year} ({calendar.month_abbr[months[0]]} - {calendar.month_abbr[months[-1]]})"
    elif period_type == 'annually':
        quarter = None
        months = list(range(1, 13))
        period_label = f"Full Year {year}"
    else:
        period_type = 'monthly'
        quarter = None
        month = int(month) if month and str(month).isdigit() else now.month
        if not (1 <= month <= 12):
            month = now.month
        months = [month]
        period_label = f"{calendar.month_name[month]} {year}"

    month_strs = [f"{m:02d}" for m in months]
    expenses = Expense.query.filter(
        Expense.family_id == family_id,
        func.strftime('%Y', Expense.date) == str(year),
        func.strftime('%m', Expense.date).in_(month_strs)
    ).order_by(Expense.date.desc()).all()

    total_spent = sum(e.amount for e in expenses)

    family = db.session.get(Family, family_id)
    monthly_incomes = []
    for m in months:
        setting = MonthlyFinanceSetting.query.filter_by(family_id=family_id, month=m, year=year).first()
        if setting and setting.monthly_income is not None:
            monthly_incomes.append(setting.monthly_income)
        elif family and family.monthly_income is not None:
            monthly_incomes.append(family.monthly_income)
        else:
            monthly_incomes.append(0.0)

    total_income = sum(monthly_incomes)
    net_savings = max(total_income - total_spent, 0.0) if total_income > 0 else 0.0
    savings_rate = (net_savings / total_income * 100) if total_income > 0 else 0.0

    total_days = sum(calendar.monthrange(year, m)[1] for m in months)
    daily_avg = total_spent / total_days if total_days else 0.0

    essential_keywords = {'rent', 'housing', 'grocery', 'groceries', 'utilit', 'bill', 'electric', 'water', 'gas', 'health', 'medic', 'doctor', 'pharma', 'insur', 'emi', 'loan', 'tax', 'school', 'tuition', 'educat'}

    categories_all = Category.query.filter_by(family_id=family_id).all()
    cat_spent_map = {}
    cat_count_map = {}
    for e in expenses:
        cat_spent_map[e.category_id] = cat_spent_map.get(e.category_id, 0.0) + e.amount
        cat_count_map[e.category_id] = cat_count_map.get(e.category_id, 0) + 1

    category_items = []
    discretionary_spent = 0.0
    essential_spent = 0.0

    for c in categories_all:
        amt = cat_spent_map.get(c.id, 0.0)
        cnt = cat_count_map.get(c.id, 0)
        c_name_lower = c.name.lower()
        is_essential = any(k in c_name_lower for k in essential_keywords)
        is_discretionary = not is_essential

        if is_discretionary:
            discretionary_spent += amt
        else:
            essential_spent += amt

        period_limit = (c.monthly_limit or 0.0) * len(months)
        pct = (amt / total_spent * 100) if total_spent > 0 else 0.0
        variance = amt - period_limit if period_limit > 0 else 0.0

        category_items.append({
            'id': c.id,
            'name': c.name,
            'color': c.color,
            'spent': round(amt, 2),
            'count': cnt,
            'monthly_limit': round(c.monthly_limit or 0.0, 2),
            'period_limit': round(period_limit, 2),
            'pct_of_total': round(pct, 1),
            'variance': round(variance, 2),
            'is_over_limit': bool(period_limit > 0 and amt > period_limit),
            'is_discretionary': is_discretionary
        })

    category_items.sort(key=lambda x: x['spent'], reverse=True)

    weekday_spent = 0.0
    weekend_spent = 0.0
    for e in expenses:
        if e.date.weekday() >= 5:
            weekend_spent += e.amount
        else:
            weekday_spent += e.amount

    weekend_pct = (weekend_spent / total_spent * 100) if total_spent > 0 else 0.0
    weekday_pct = (weekday_spent / total_spent * 100) if total_spent > 0 else 0.0

    spender_map = {}
    for e in expenses:
        uname = e.user.username if e.user else 'Unknown'
        if uname not in spender_map:
            spender_map[uname] = {'spent': 0.0, 'count': 0}
        spender_map[uname]['spent'] += e.amount
        spender_map[uname]['count'] += 1

    spenders = []
    for uname, data in spender_map.items():
        spenders.append({
            'username': uname,
            'spent': round(data['spent'], 2),
            'count': data['count'],
            'pct_of_total': round((data['spent'] / total_spent * 100) if total_spent > 0 else 0.0, 1)
        })
    spenders.sort(key=lambda x: x['spent'], reverse=True)

    top_txs = sorted(expenses, key=lambda e: e.amount, reverse=True)[:5]
    top_expenses = [{
        'item_name': t.item_name,
        'amount': round(t.amount, 2),
        'date': t.date.strftime('%d %b'),
        'category': t.category.name if t.category else '',
        'spender': t.user.username if t.user else ''
    } for t in top_txs]

    discretionary_pct = (discretionary_spent / total_spent * 100) if total_spent > 0 else 0.0

    return {
        'period_label': period_label,
        'period_type': period_type,
        'year': year,
        'month': month if period_type == 'monthly' else None,
        'quarter': quarter if period_type == 'quarterly' else None,
        'months': months,
        'days_count': total_days,
        'total_spent': round(total_spent, 2),
        'total_income': round(total_income, 2),
        'net_savings': round(net_savings, 2),
        'savings_rate': round(savings_rate, 1),
        'expense_count': len(expenses),
        'daily_avg': round(daily_avg, 2),
        'categories': category_items,
        'weekday_spent': round(weekday_spent, 2),
        'weekend_spent': round(weekend_spent, 2),
        'weekend_pct': round(weekend_pct, 1),
        'weekday_pct': round(weekday_pct, 1),
        'spenders': spenders,
        'top_expenses': top_expenses,
        'discretionary_spent': round(discretionary_spent, 2),
        'essential_spent': round(essential_spent, 2),
        'discretionary_pct': round(discretionary_pct, 1)
    }


def financial_advisor_schema():
    return {
        'type': 'OBJECT',
        'properties': {
            'summary_diagnosis': {'type': 'STRING'},
            'health_grade': {'type': 'STRING'},
            'leakage_total_estimate': {'type': 'NUMBER'},
            'potential_annual_savings': {'type': 'NUMBER'},
            'savings_tips': {
                'type': 'ARRAY',
                'items': {
                    'type': 'OBJECT',
                    'properties': {
                        'title': {'type': 'STRING'},
                        'category': {'type': 'STRING'},
                        'type': {'type': 'STRING'},
                        'recommendation': {'type': 'STRING'},
                        'estimated_monthly_savings': {'type': 'NUMBER'},
                        'action_steps': {'type': 'STRING'},
                    },
                    'required': ['title', 'category', 'type', 'recommendation', 'estimated_monthly_savings'],
                },
            },
            'decision_questions': {
                'type': 'ARRAY',
                'items': {
                    'type': 'OBJECT',
                    'properties': {
                        'id': {'type': 'STRING'},
                        'title': {'type': 'STRING'},
                        'context': {'type': 'STRING'},
                        'question': {'type': 'STRING'},
                        'is_multi_select': {'type': 'BOOLEAN'},
                        'options': {
                            'type': 'ARRAY',
                            'items': {
                                'type': 'OBJECT',
                                'properties': {
                                    'id': {'type': 'STRING'},
                                    'label': {'type': 'STRING'},
                                    'impact_description': {'type': 'STRING'},
                                    'projected_savings_rs': {'type': 'NUMBER'},
                                    'is_recommended': {'type': 'BOOLEAN'},
                                },
                                'required': ['id', 'label', 'impact_description', 'projected_savings_rs', 'is_recommended'],
                            },
                        },
                    },
                    'required': ['id', 'title', 'context', 'question', 'is_multi_select', 'options'],
                },
            },
        },
        'required': [
            'summary_diagnosis',
            'health_grade',
            'leakage_total_estimate',
            'potential_annual_savings',
            'savings_tips',
            'decision_questions',
        ],
    }


def generate_fallback_advisor_analysis(agg, custom_goal=''):
    total_spent = agg['total_spent']
    months_count = max(len(agg['months']), 1)
    monthly_equiv_spent = total_spent / months_count
    discretionary = agg['discretionary_spent']
    monthly_discretionary = discretionary / months_count
    savings_rate = agg['savings_rate']

    if savings_rate >= 30:
        health_grade = 'A'
    elif savings_rate >= 20:
        health_grade = 'B+'
    elif savings_rate >= 10:
        health_grade = 'B'
    elif agg['net_savings'] > 0:
        health_grade = 'C+'
    else:
        health_grade = 'Needs Attention'

    disc_cats = [c for c in agg['categories'] if c['is_discretionary'] and c['spent'] > 0]
    top_disc = disc_cats[0] if disc_cats else (agg['categories'][0] if agg['categories'] else None)
    top_disc_name = top_disc['name'] if top_disc else 'Discretionary'
    top_disc_monthly = (top_disc['spent'] / months_count) if top_disc else 0.0

    over_limit_total = sum(c['variance'] for c in agg['categories'] if c['is_over_limit'])
    leakage_estimate = (monthly_discretionary * 0.18) + (over_limit_total / months_count) if discretionary > 0 else 1500.0
    potential_annual = leakage_estimate * 12

    goal_sentence = f" Grounded in your focus: '{custom_goal}'." if custom_goal else ""
    summary_diagnosis = (
        f"During {agg['period_label']}, your household logged {agg['expense_count']} transactions totaling Rs. {total_spent:,.0f} "
        f"(averaging Rs. {agg['daily_avg']:,.0f}/day). Discretionary spending accounted for {agg['discretionary_pct']:.1f}% "
        f"of total outlays. Weekend purchasing made up {agg['weekend_pct']:.1f}% of overall expenditure. "
        f"By optimizing discretionary allocations in categories like {top_disc_name}, you can unlock approximately "
        f"Rs. {leakage_estimate:,.0f} every month in clean family savings.{goal_sentence}"
    )

    tips = [
        {
            'title': f"Trim {top_disc_name} Outlays by 20%",
            'category': top_disc_name,
            'type': 'high_impact',
            'recommendation': f"Reduce non-essential dining and food delivery orders by 20% by establishing dedicated home-cooking days.",
            'estimated_monthly_savings': round(top_disc_monthly * 0.20, 2),
            'action_steps': "Plan weekday meals in advance and reserve restaurant orders strictly for designated weekend family milestones."
        },
        {
            'title': "Weekend Surge Guardrail",
            'category': "Weekend Spending",
            'type': 'lifestyle',
            'recommendation': f"Weekend purchasing represents {agg['weekend_pct']:.1f}% of your budget (Rs. {agg['weekend_spent']:,.0f}). Implement a 48-hour delay rule on non-urgent Saturday/Sunday online shopping.",
            'estimated_monthly_savings': round((agg['weekend_spent'] / months_count) * 0.15, 2),
            'action_steps': "Leave items in online carts for 48 hours before purchasing to curb impulsive convenience buying."
        },
        {
            'title': "Micro-Spend & Delivery Fee Consolidation",
            'category': "Everyday Convenience",
            'type': 'quick_win',
            'recommendation': "Consolidate small frequent delivery and snack orders into larger planned bi-weekly restocks to avoid repeated platform delivery surcharges.",
            'estimated_monthly_savings': round(min(monthly_discretionary * 0.08, 2500.0) if monthly_discretionary > 0 else 800.0, 2),
            'action_steps': "Bundle household needs into one weekly grocery order instead of 3-4 daily express deliveries."
        }
    ]

    disc_savings_opt1 = round(top_disc_monthly * 0.20, 2) if top_disc_monthly > 0 else 1500.0
    disc_savings_opt2 = round(monthly_discretionary * 0.12, 2) if monthly_discretionary > 0 else 1000.0
    weekend_savings_opt = round((agg['weekend_spent'] / months_count) * 0.15, 2) if agg['weekend_spent'] > 0 else 800.0
    top_spender = agg['spenders'][0] if agg['spenders'] else {'username': 'Family', 'pct_of_total': 100}

    questions = [
        {
            'id': 'q_discretionary_strategy',
            'title': 'Discretionary Spending Calibration',
            'context': f"Your flexible discretionary spend totaled Rs. {discretionary:,.0f} ({agg['discretionary_pct']:.1f}% of outlays), led by {top_disc_name} at Rs. {top_disc['spent']:,.0f}." if top_disc else "Evaluate non-essential household spend.",
            'question': "Which target strategy would your household prefer to adopt for discretionary expenses?",
            'is_multi_select': False,
            'options': [
                {
                    'id': 'opt_trim_top_category',
                    'label': f"(Recommended) Trim {top_disc_name} by 20%",
                    'impact_description': f"Saves ~Rs. {disc_savings_opt1:,.0f}/mo (Rs. {disc_savings_opt1 * 12:,.0f}/year) without touching essential groceries or bills.",
                    'projected_savings_rs': disc_savings_opt1,
                    'is_recommended': True
                },
                {
                    'id': 'opt_balanced_cut',
                    'label': "Apply a balanced 10% reduction across all flexible categories",
                    'impact_description': f"Saves ~Rs. {disc_savings_opt2:,.0f}/mo evenly distributed across all non-essential items.",
                    'projected_savings_rs': disc_savings_opt2,
                    'is_recommended': False
                },
                {
                    'id': 'opt_weekend_ceiling',
                    'label': f"Enforce a weekend spending ceiling ({agg['weekend_pct']:.0f}% of total spend)",
                    'impact_description': f"Saves ~Rs. {weekend_savings_opt:,.0f}/mo by curbing impulsive Friday-Sunday outings.",
                    'projected_savings_rs': weekend_savings_opt,
                    'is_recommended': False
                },
                {
                    'id': 'opt_preserve_lifestyle',
                    'label': "Preserve current lifestyle spend and focus exclusively on fixed utility reductions",
                    'impact_description': "Saves ~Rs. 1,000/mo through utility efficiency and plan renegotiation.",
                    'projected_savings_rs': 1000.0,
                    'is_recommended': False
                }
            ]
        },
        {
            'id': 'q_savings_destination',
            'title': 'Savings Goal & Capital Allocation',
            'context': f"By implementing these steps, you will free up an estimated Rs. {leakage_estimate:,.0f} monthly (Rs. {potential_annual:,.0f} annually).",
            'question': "Where would you like to direct your household's realized monthly savings?",
            'is_multi_select': False,
            'options': [
                {
                    'id': 'opt_dest_emergency',
                    'label': "(Recommended) Build / Expand 3-Month Emergency Safety Fund",
                    'impact_description': "Protects family against unforeseen medical or household surprises with zero debt reliance.",
                    'projected_savings_rs': leakage_estimate,
                    'is_recommended': True
                },
                {
                    'id': 'opt_dest_investments',
                    'label': "Systematic Investment / High-Yield Savings (Wealth Accumulation)",
                    'impact_description': "Channels surplus directly into monthly automated investments (SIPs / FDs).",
                    'projected_savings_rs': leakage_estimate,
                    'is_recommended': False
                },
                {
                    'id': 'opt_dest_debt_paydown',
                    'label': "Accelerate Debt, EMI, or Credit Card Paydown",
                    'impact_description': "Reduces compounding interest liabilities and frees up future cash flow faster.",
                    'projected_savings_rs': leakage_estimate,
                    'is_recommended': False
                },
                {
                    'id': 'opt_dest_family_goal',
                    'label': "Earmark for an upcoming family milestone (Vacation, Festival, Gadget)",
                    'impact_description': "Funds milestone purchases in cash without touching credit or monthly income.",
                    'projected_savings_rs': leakage_estimate,
                    'is_recommended': False
                }
            ]
        },
        {
            'id': 'q_enforcement_mode',
            'title': 'Household Accountability & Limit Enforcement',
            'context': f"Your household has {len(agg['spenders'])} active spenders ({top_spender['username']} represented {top_spender['pct_of_total']:.0f}% of spend).",
            'question': "How would you like VaultSync to enforce and monitor these savings limits?",
            'is_multi_select': False,
            'options': [
                {
                    'id': 'opt_auto_category_limits',
                    'label': "(Recommended) Automatically update VaultSync Category Monthly Limits",
                    'impact_description': "Locks in hard caps on categories with live warning badges when approaching limits.",
                    'projected_savings_rs': 0.0,
                    'is_recommended': True
                },
                {
                    'id': 'opt_daily_briefing_alerts',
                    'label': "Use Daily AI Briefings to enforce dynamic daily burn rate ceilings",
                    'impact_description': "Delivers a fresh safe spend ceiling each morning based on remaining monthly budget.",
                    'projected_savings_rs': 0.0,
                    'is_recommended': False
                },
                {
                    'id': 'opt_weekly_family_review',
                    'label': "Establish a Sunday Family Finance Check-in review rhythm",
                    'impact_description': "Fosters transparent household communication without automated friction.",
                    'projected_savings_rs': 0.0,
                    'is_recommended': False
                }
            ]
        }
    ]

    return {
        'summary_diagnosis': summary_diagnosis,
        'health_grade': health_grade,
        'leakage_total_estimate': round(leakage_estimate, 2),
        'potential_annual_savings': round(potential_annual, 2),
        'savings_tips': tips,
        'decision_questions': questions
    }


def refine_advisor_decisions(family_id, agg, user_answers):
    months_count = max(len(agg.get('months') or [1]), 1)
    disc_cats = [c for c in agg.get('categories', []) if c.get('is_discretionary') and c.get('spent', 0) > 0]
    top_disc = disc_cats[0] if disc_cats else (agg.get('categories', [None])[0] if agg.get('categories') else None)
    top_disc_name = top_disc['name'] if top_disc else 'Discretionary'
    top_disc_monthly = (top_disc['spent'] / months_count) if top_disc else 0.0
    monthly_discretionary = agg.get('discretionary_spent', 0.0) / months_count

    strat = user_answers.get('q_discretionary_strategy', 'opt_trim_top_category')
    dest = user_answers.get('q_savings_destination', 'opt_dest_emergency')
    enf = user_answers.get('q_enforcement_mode', 'opt_auto_category_limits')
    note = user_answers.get('custom_note', '').strip()

    if strat == 'opt_trim_top_category':
        selected_savings_monthly = round(top_disc_monthly * 0.20, 2)
        strat_text = f"Trimming {top_disc_name} by 20%"
    elif strat == 'opt_balanced_cut':
        selected_savings_monthly = round(monthly_discretionary * 0.12, 2)
        strat_text = "Applying 10% balanced reduction across all discretionary categories"
    elif strat == 'opt_weekend_ceiling':
        selected_savings_monthly = round((agg.get('weekend_spent', 0.0) / months_count) * 0.15, 2)
        strat_text = "Enforcing weekend spending ceiling (curbing Fri-Sun surge)"
    else:
        selected_savings_monthly = 1000.0
        strat_text = "Optimizing utility and fixed plan contracts"

    selected_savings_annual = selected_savings_monthly * 12

    dest_labels = {
        'opt_dest_emergency': 'Emergency Safety Fund (3 Months Reserve)',
        'opt_dest_investments': 'Automated SIPs & High-Yield Investments',
        'opt_dest_debt_paydown': 'Accelerated Debt & Loan Paydown',
        'opt_dest_family_goal': 'Family Milestone Savings Goal'
    }
    dest_text = dest_labels.get(dest, 'Family Savings Reserve')

    category_caps = {}
    for c in agg.get('categories', []):
        curr_limit = c.get('monthly_limit', 0.0)
        monthly_spent = c.get('spent', 0.0) / months_count

        if c.get('is_discretionary') and strat == 'opt_trim_top_category' and top_disc and c['id'] == top_disc['id']:
            new_limit = max(monthly_spent * 0.80, 500.0)
            category_caps[c['id']] = {
                'name': c['name'],
                'current_limit': curr_limit,
                'proposed_limit': round(new_limit, 0),
                'monthly_savings': round(max(monthly_spent - new_limit, 0), 0)
            }
        elif c.get('is_discretionary') and strat == 'opt_balanced_cut' and monthly_spent > 0:
            new_limit = max(monthly_spent * 0.90, 500.0)
            category_caps[c['id']] = {
                'name': c['name'],
                'current_limit': curr_limit,
                'proposed_limit': round(new_limit, 0),
                'monthly_savings': round(max(monthly_spent - new_limit, 0), 0)
            }
        elif c.get('is_discretionary') and c.get('is_over_limit'):
            new_limit = max(monthly_spent * 0.85, 500.0)
            category_caps[c['id']] = {
                'name': c['name'],
                'current_limit': curr_limit,
                'proposed_limit': round(new_limit, 0),
                'monthly_savings': round(max(monthly_spent - new_limit, 0), 0)
            }

    return {
        'strategy_chosen': strat_text,
        'destination_chosen': dest_text,
        'enforcement_chosen': enf,
        'custom_note': note,
        'selected_savings_monthly': selected_savings_monthly,
        'selected_savings_annual': selected_savings_annual,
        'category_recommendations': category_caps,
        'action_summary': f"Decisions confirmed! Your strategy will reclaim Rs. {selected_savings_monthly:,.0f} every month (Rs. {selected_savings_annual:,.0f}/year) dedicated to {dest_text}."
    }


@app.route('/financial_advisor')
@login_required
def financial_advisor():
    now = datetime.utcnow()
    selected_month = request.args.get('month', now.month, type=int)
    selected_year = request.args.get('year', now.year, type=int)
    selected_period = request.args.get('period', 'monthly').lower()
    selected_quarter = request.args.get('quarter', 'Q4').upper()

    if selected_period not in {'monthly', 'quarterly', 'annually'}:
        selected_period = 'monthly'

    agg = aggregate_family_financial_period(
        current_user.family_id,
        period_type=selected_period,
        year=selected_year,
        month=selected_month,
        quarter=selected_quarter
    )

    categories = Category.query.filter_by(family_id=current_user.family_id).all()

    return render_template('financial_advisor.html',
                           aggregated=agg,
                           categories=categories,
                           selected_period=selected_period,
                           selected_month=selected_month,
                           selected_year=selected_year,
                           selected_quarter=selected_quarter,
                           year_range=range(2024, now.year + 3))


@app.route('/api/financial_advisor/analyze', methods=['POST'])
@login_required
def api_financial_advisor_analyze():
    payload = request.get_json(silent=True) or {}
    period_type = payload.get('period_type', 'monthly')
    year = payload.get('year')
    month = payload.get('month')
    quarter = payload.get('quarter')
    custom_goal = payload.get('custom_goal', '').strip()

    agg = aggregate_family_financial_period(
        current_user.family_id,
        period_type=period_type,
        year=year,
        month=month,
        quarter=quarter
    )

    analysis = generate_fallback_advisor_analysis(agg, custom_goal)

    if gemini_configured():
        try:
            prompt = (
                f"You are the VaultSync AI Financial Advisor. Analyze this family's financial patterns for {agg['period_label']}.\n"
                f"Financial Data Summary:\n"
                f"- Total Spent: Rs. {agg['total_spent']:,.2f}\n"
                f"- Total Income: Rs. {agg['total_income']:,.2f}\n"
                f"- Net Savings: Rs. {agg['net_savings']:,.2f} (Savings Rate: {agg['savings_rate']}%)\n"
                f"- Discretionary Spend: Rs. {agg['discretionary_spent']:,.2f} ({agg['discretionary_pct']}% of total)\n"
                f"- Essential Spend: Rs. {agg['essential_spent']:,.2f}\n"
                f"- Weekday vs Weekend: Weekday Rs. {agg['weekday_spent']:,.2f} ({agg['weekday_pct']}%) vs Weekend Rs. {agg['weekend_spent']:,.2f} ({agg['weekend_pct']}%)\n"
                f"- Category Breakdown: {[{c['name']: c['spent']} for c in agg['categories'] if c['spent'] > 0]}\n"
                f"- Spenders: {agg['spenders']}\n"
                f"- User Custom Goal: {custom_goal or 'Maximize household savings and eliminate leakage'}\n\n"
                f"Provide: (1) executive summary diagnosis of spending patterns, (2) financial health grade, "
                f"(3) estimated leakage amount in Rs., (4) potential annual savings in Rs., "
                f"(5) 3-4 specific actionable savings tips with estimated Rs. monthly savings, "
                f"(6) exactly 3 Antigravity-style decision-making questions with realistic tradeoffs, multiple choice options (including one marked is_recommended=True with calculated projected_savings_rs)."
            )
            gemini_res = call_gemini_json(prompt, financial_advisor_schema())
            if gemini_res and isinstance(gemini_res, dict) and gemini_res.get('decision_questions'):
                analysis = gemini_res
        except Exception as exc:
            app.logger.warning(f"Gemini Financial Advisor call failed, using deterministic engine: {exc}")

    return jsonify({
        'success': True,
        'aggregated': agg,
        'analysis': analysis
    })


@app.route('/api/financial_advisor/refine', methods=['POST'])
@login_required
def api_financial_advisor_refine():
    payload = request.get_json(silent=True) or {}
    agg = payload.get('aggregated')
    answers = payload.get('answers') or {}

    if not agg:
        period_type = payload.get('period_type', 'monthly')
        year = payload.get('year')
        month = payload.get('month')
        quarter = payload.get('quarter')
        agg = aggregate_family_financial_period(
            current_user.family_id,
            period_type=period_type,
            year=year,
            month=month,
            quarter=quarter
        )

    refinement = refine_advisor_decisions(current_user.family_id, agg, answers)
    return jsonify({
        'success': True,
        'refinement': refinement
    })


@app.route('/api/financial_advisor/apply_caps', methods=['POST'])
@login_required
def api_financial_advisor_apply_caps():
    if not is_family_manager(current_user):
        return jsonify({'error': 'Only family managers can update category limits'}), 403

    payload = request.get_json(silent=True) or {}
    caps = payload.get('category_caps') or {}
    if not caps:
        return jsonify({'error': 'No category limits provided'}), 400

    updated = 0
    for cat_id_str, new_lim in caps.items():
        try:
            cat_id = int(cat_id_str)
            cat = Category.query.filter_by(id=cat_id, family_id=current_user.family_id).first()
            if cat:
                cat.monthly_limit = max(float(new_lim), 0.0)
                updated += 1
        except (ValueError, TypeError):
            continue

    if updated > 0:
        db.session.commit()
        return jsonify({'success': True, 'updated_count': updated, 'message': f'Updated {updated} category limits successfully!'})

    return jsonify({'error': 'No valid categories found to update'}), 400


@app.route('/admin_panel', methods=['GET', 'POST'])
@login_required
def admin_panel():
    """Admin panel for family management"""
    if not is_family_manager(current_user):
        flash('Unauthorized: family manager only', 'error')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        member_username = request.form.get('member_username', '').strip()

        # Find user by username (case-insensitive) but NOT in the current family
        member = User.query.filter(
            func.lower(User.username) == func.lower(member_username)
        ).first()

        if not member:
            flash(f'User "{member_username}" not found in the system.', 'error')
        elif member.family_id == current_user.family_id:
            flash(f'User "{member_username}" is already in your family.', 'info')
        else:
            # Update the member's family_id to match the admin's family
            member.family_id = current_user.family_id
            db.session.commit()
            flash(f'User "{member_username}" has been added to your family!', 'success')

        return redirect(url_for('admin_panel'))

    # Fetch all members in this family
    family_members = User.query.filter_by(family_id=current_user.family_id).all()

    # Fetch family data
    family = current_user.family

    # Calculate spending stats
    current_date = datetime.utcnow()
    current_month_start, current_month_end = month_bounds(current_date.year, current_date.month)
    month_str = f"{current_date.month:02d}"
    year_str = str(current_date.year)

    monthly_spending = db.session.query(
        func.sum(Expense.amount).label('total')
    ).filter(
        func.strftime('%Y', Expense.date) == year_str,
        func.strftime('%m', Expense.date) == month_str,
        Expense.family_id == current_user.family_id
    ).scalar() or 0.0

    current_finance = monthly_finance_for(family, current_date.month, current_date.year)
    current_monthly_income = current_finance['monthly_income']
    current_monthly_budget = current_finance['monthly_budget']
    remaining_budget = current_monthly_budget - monthly_spending if current_monthly_budget else 0

    member_spending_rows = db.session.query(
        User.id,
        func.coalesce(func.sum(Expense.amount), 0).label('total'),
        func.count(Expense.id).label('count')
    ).outerjoin(
        Expense,
        (Expense.user_id == User.id) &
        (Expense.family_id == current_user.family_id) &
        (func.strftime('%Y', Expense.date) == year_str) &
        (func.strftime('%m', Expense.date) == month_str)
    ).filter(
        User.family_id == current_user.family_id
    ).group_by(User.id).all()
    member_stats = {
        user_id: {'total': float(total or 0), 'count': int(count or 0)}
        for user_id, total, count in member_spending_rows
    }

    categories = Category.query.filter_by(
        family_id=current_user.family_id
    ).order_by(Category.name.asc()).all()

    category_spending_rows = db.session.query(
        Expense.category_id,
        func.coalesce(func.sum(Expense.amount), 0).label('total')
    ).filter(
        func.strftime('%Y', Expense.date) == year_str,
        func.strftime('%m', Expense.date) == month_str,
        Expense.family_id == current_user.family_id
    ).group_by(Expense.category_id).all()
    category_spending = {
        category_id: float(total or 0)
        for category_id, total in category_spending_rows
    }

    planned_total = db.session.query(
        func.coalesce(func.sum(ExpectedExpense.amount), 0)
    ).filter_by(
        family_id=current_user.family_id,
        month=current_date.month,
        year=current_date.year
    ).scalar() or 0.0
    planned_paid_total = db.session.query(
        func.coalesce(func.sum(ExpectedExpense.amount), 0)
    ).filter_by(
        family_id=current_user.family_id,
        month=current_date.month,
        year=current_date.year,
        is_paid=True
    ).scalar() or 0.0
    ai_suggestions_count = BudgetSuggestion.query.filter_by(
        family_id=current_user.family_id
    ).count()
    latest_savings_forecast = AISavingsForecast.query.filter_by(
        family_id=current_user.family_id
    ).order_by(AISavingsForecast.created_at.desc(), AISavingsForecast.id.desc()).first()
    latest_savings_payload = savings_forecast_payload(latest_savings_forecast)
    savings_chart_data = savings_forecast_chart_data(family, latest_savings_forecast)

    # Fetch AI Expense Organizer exceptions
    from models import AIExpenseException
    ai_exceptions = AIExpenseException.query.filter_by(
        family_id=current_user.family_id
    ).order_by(AIExpenseException.created_at.desc()).all()

    return render_template('admin_panel.html',
                          user=current_user,
                          family=family,
                          family_members=family_members,
                          member_stats=member_stats,
                          categories=categories,
                          category_spending=category_spending,
                          monthly_spending=monthly_spending,
                          current_monthly_income=current_monthly_income,
                          current_monthly_budget=current_monthly_budget,
                          current_finance=current_finance,
                          remaining_budget=remaining_budget,
                          planned_total=float(planned_total),
                          planned_paid_total=float(planned_paid_total),
                          planned_unpaid_total=float(planned_total - planned_paid_total),
                          ai_suggestions_count=ai_suggestions_count,
                          latest_savings_forecast=latest_savings_forecast,
                          latest_savings_payload=latest_savings_payload,
                          savings_chart_data=savings_chart_data,
                          ai_exceptions=ai_exceptions,
                          ai_notes=family.ai_notes or '',
                          ai_organizer_configured=gemini_configured(),
                          ai_organizer_model=GEMINI_MODEL,
                          gemini_key_count=len(configured_gemini_keys()),
                          today=current_date.date(),
                          current_month_start=current_month_start,
                          current_month_end=current_month_end,
                          budget_percentage=(monthly_spending / current_monthly_budget * 100) if current_monthly_budget else 0,
                          year_range=range(2024, datetime.utcnow().year + 3))


@app.route('/admin_panel/budget', methods=['POST'])
@login_required
def update_budget():
    """Update default family monthly income and budget."""
    if not is_family_manager(current_user):
        flash('Unauthorized', 'error')
        return redirect(url_for('dashboard'))

    budget = request.form.get('monthly_budget')
    income = request.form.get('monthly_income')
    try:
        budget = float(budget)
        income = float(income or budget)
        if budget < 1000:
            flash('Budget must be at least 1000.', 'error')
            return redirect(url_for('admin_panel'))
        if income < 0:
            flash('Income cannot be negative.', 'error')
            return redirect(url_for('admin_panel'))

        current_user.family.monthly_income = income
        current_user.family.monthly_budget = budget
        db.session.commit()
        flash('Default family income and budget updated successfully!', 'success')
    except (ValueError, TypeError):
        flash('Please enter valid income and budget amounts.', 'error')

    return redirect(url_for('admin_panel', tab='budget'))


@app.route('/finance_settings/monthly', methods=['POST'])
@login_required
def update_monthly_finance_setting():
    if not is_family_manager(current_user):
        flash('Unauthorized', 'error')
        return redirect(url_for('dashboard'))

    today = datetime.utcnow().date()
    selected_month = request.form.get('month', today.month, type=int)
    selected_year = request.form.get('year', today.year, type=int)
    if not (1 <= selected_month <= 12) or selected_year < 2020 or selected_year > 2050:
        flash('Please choose a valid month and year.', 'error')
        return redirect(request.referrer or url_for('dashboard'))

    try:
        monthly_income = float(request.form.get('monthly_income') or 0)
        monthly_budget = float(request.form.get('monthly_budget') or 0)
    except (TypeError, ValueError):
        flash('Please enter valid income and budget numbers.', 'error')
        return redirect(request.referrer or url_for('dashboard'))

    if monthly_income < 0 or monthly_budget < 0:
        flash('Income and budget cannot be negative.', 'error')
        return redirect(request.referrer or url_for('dashboard'))

    setting = MonthlyFinanceSetting.query.filter_by(
        family_id=current_user.family_id,
        month=selected_month,
        year=selected_year
    ).first()
    if not setting:
        setting = MonthlyFinanceSetting(
            family_id=current_user.family_id,
            month=selected_month,
            year=selected_year
        )
        db.session.add(setting)

    setting.monthly_income = monthly_income
    setting.monthly_budget = monthly_budget
    setting.notes = request.form.get('notes', '').strip()
    setting.updated_by_user_id = current_user.id
    setting.updated_at = datetime.utcnow()
    db.session.commit()

    flash(f'{calendar.month_name[selected_month]} {selected_year} income and budget saved.', 'success')
    return redirect(request.form.get('next') or request.referrer or url_for(
        'dashboard',
        month=selected_month,
        year=selected_year
    ))


@app.route('/admin_panel/ai_organize', methods=['POST'])
@login_required
def admin_ai_organize_expenses():
    if not is_family_manager(current_user):
        flash('Unauthorized', 'error')
        return redirect(url_for('dashboard'))

    today = datetime.now().date()
    start_date = parse_date_arg(request.form.get('start_date'), today)
    end_date = parse_date_arg(request.form.get('end_date'), start_date)
    if end_date < start_date:
        start_date, end_date = end_date, start_date

    try:
        result = organize_expenses_for_family(current_user.family_id, start_date, end_date)
    except ValueError as exc:
        db.session.rollback()
        flash(f'AI organizer could not run: {exc}', 'error')
        return redirect(url_for('admin_panel', tab='ai-organizer'))
    except Exception as exc:
        db.session.rollback()
        app.logger.exception('AI organizer failed')
        flash(f'AI organizer failed unexpectedly: {exc}', 'error')
        return redirect(url_for('admin_panel', tab='ai-organizer'))

    flash(
        f"AI organizer scanned {result['scanned']} expense(s), changed {result['changed']}, "
        f"left {result['unchanged']} unchanged, skipped {result['skipped']}.",
        'success' if result['changed'] else 'info'
    )
    if result.get('message'):
        flash(result['message'], 'info')

    return redirect(url_for('admin_panel', tab='ai-organizer'))


@app.route('/admin_panel/ai_savings_forecast', methods=['POST'])
@login_required
def admin_ai_savings_forecast():
    if not is_family_manager(current_user):
        flash('Unauthorized', 'error')
        return redirect(url_for('dashboard'))

    admin_notes = request.form.get('ai_notes', '').strip()
    current_user.family.ai_notes = admin_notes

    try:
        forecast = create_ai_savings_forecast(
            current_user.family_id,
            current_user.id,
            admin_notes
        )
    except ValueError as exc:
        db.session.rollback()
        if current_user.family:
            current_user.family.ai_notes = admin_notes
            db.session.commit()
        flash(f'AI savings forecast could not run: {exc}', 'error')
        return redirect(url_for('admin_panel', tab='ai-forecast'))
    except Exception as exc:
        db.session.rollback()
        app.logger.exception('AI savings forecast failed')
        flash(f'AI savings forecast failed unexpectedly: {exc}', 'error')
        return redirect(url_for('admin_panel', tab='ai-forecast'))

    flash(
        f'AI forecast saved: expected month-end savings Rs. {forecast.expected_savings:,.0f}.',
        'success' if forecast.expected_savings >= 0 else 'warning'
    )
    return redirect(url_for('admin_panel', tab='ai-forecast'))


@app.route('/admin_panel/ai_expense_exception/add', methods=['POST'])
@login_required
def add_ai_expense_exception():
    """Add an exception rule for the AI Expense Organizer."""
    from models import AIExpenseException
    
    if not is_family_manager(current_user):
        flash('Unauthorized', 'error')
        return redirect(url_for('admin_panel'))

    if not current_user.family_id:
        flash('Please join a family first.', 'error')
        return redirect(url_for('admin_panel'))

    description = request.form.get('description', '').strip()
    match_keywords = request.form.get('match_keywords', '').strip()
    match_description = request.form.get('match_description_contains', '').strip()
    action = request.form.get('action', 'exclude').strip()

    if not description:
        flash('Please provide a description for this exception rule.', 'error')
        return redirect(url_for('admin_panel', tab='ai-exceptions'))

    if not (match_keywords or match_description):
        flash('Please provide at least one matching criteria (keywords or description).', 'error')
        return redirect(url_for('admin_panel', tab='ai-exceptions'))

    if action not in ('exclude', 'separate'):
        action = 'exclude'

    exception = AIExpenseException(
        family_id=current_user.family_id,
        description=description,
        match_keywords=match_keywords,
        match_description_contains=match_description,
        action=action,
        is_active=True
    )
    db.session.add(exception)
    db.session.commit()
    flash('Exception rule added successfully!', 'success')
    return redirect(url_for('admin_panel', tab='ai-exceptions'))


@app.route('/admin_panel/ai_expense_exception/<int:exc_id>/toggle', methods=['POST'])
@login_required
def toggle_ai_expense_exception(exc_id):
    """Enable/disable an exception rule."""
    from models import AIExpenseException
    
    if not is_family_manager(current_user):
        flash('Unauthorized', 'error')
        return redirect(url_for('admin_panel'))

    exception = AIExpenseException.query.filter_by(
        id=exc_id,
        family_id=current_user.family_id
    ).first_or_404()

    exception.is_active = not exception.is_active
    db.session.commit()
    status = "enabled" if exception.is_active else "disabled"
    flash(f'Exception rule {status}.', 'success')
    return redirect(url_for('admin_panel', tab='ai-exceptions'))


@app.route('/admin_panel/ai_expense_exception/<int:exc_id>/delete', methods=['POST'])
@login_required
def delete_ai_expense_exception(exc_id):
    """Delete an exception rule."""
    from models import AIExpenseException
    
    if not is_family_manager(current_user):
        flash('Unauthorized', 'error')
        return redirect(url_for('admin_panel'))

    exception = AIExpenseException.query.filter_by(
        id=exc_id,
        family_id=current_user.family_id
    ).first_or_404()

    description = exception.description
    db.session.delete(exception)
    db.session.commit()
    flash(f'Exception rule "{description}" deleted.', 'success')
    return redirect(url_for('admin_panel', tab='ai-exceptions'))


@app.route('/admin_panel/categories/<int:category_id>/limit', methods=['POST'])
@login_required
def update_category_limit(category_id):
    if not is_family_manager(current_user):
        flash('Unauthorized', 'error')
        return redirect(url_for('dashboard'))

    category = Category.query.filter_by(
        id=category_id,
        family_id=current_user.family_id
    ).first_or_404()

    try:
        monthly_limit = float(request.form.get('monthly_limit') or 0)
        if monthly_limit < 0:
            raise ValueError
    except (ValueError, TypeError):
        flash('Please enter a valid category limit.', 'error')
        return redirect(url_for('admin_panel'))

    category.monthly_limit = monthly_limit
    db.session.commit()
    flash(f'{category.name} limit updated.', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin_panel/members/<int:member_id>/promote', methods=['POST'])
@login_required
def promote_member(member_id):
    """Promote member to family manager"""
    if not is_family_manager(current_user):
        flash('Unauthorized', 'error')
        return redirect(url_for('dashboard'))

    member = User.query.filter_by(id=member_id, family_id=current_user.family_id).first_or_404()
    member.user_type = 'family_manager'
    db.session.commit()
    flash(f'{member.username} has been promoted to Family Manager!', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin_panel/members/<int:member_id>/demote', methods=['POST'])
@login_required
def demote_member(member_id):
    """Demote member from family manager"""
    if not is_family_manager(current_user):
        flash('Unauthorized', 'error')
        return redirect(url_for('dashboard'))

    member = User.query.filter_by(id=member_id, family_id=current_user.family_id).first_or_404()

    # Prevent demoting the creator
    if member.id == current_user.family.created_by_user_id and member.id != current_user.id:
        flash('Cannot demote the family creator.', 'error')
        return redirect(url_for('admin_panel'))

    member.user_type = 'family_member'
    db.session.commit()
    flash(f'{member.username} has been demoted to Family Member!', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin_panel/members/<int:member_id>/remove', methods=['POST'])
@login_required
def remove_member(member_id):
    """Remove member from family"""
    if not is_family_manager(current_user):
        flash('Unauthorized', 'error')
        return redirect(url_for('dashboard'))

    member = User.query.filter_by(id=member_id, family_id=current_user.family_id).first_or_404()

    # Prevent removing yourself
    if member.id == current_user.id:
        flash('You cannot remove yourself from the family.', 'error')
        return redirect(url_for('admin_panel'))

    member.family_id = None
    db.session.commit()
    flash(f'{member.username} has been removed from the family.', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin_panel/family/update', methods=['POST'])
@login_required
def update_family():
    """Update family settings"""
    if not is_family_manager(current_user):
        flash('Unauthorized', 'error')
        return redirect(url_for('dashboard'))

    family_name = request.form.get('family_name', '').strip()

    if not family_name or len(family_name) < 2:
        flash('Family name must be at least 2 characters long.', 'error')
        return redirect(url_for('admin_panel'))

    current_user.family.name = family_name
    db.session.commit()
    flash('Family name updated successfully!', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin_panel/family/regenerate_invite', methods=['POST'])
@login_required
def regenerate_invite_code():
    if not is_family_manager(current_user):
        flash('Unauthorized', 'error')
        return redirect(url_for('dashboard'))

    new_code = secrets.token_urlsafe(12)
    while Family.query.filter_by(invite_code=new_code).first():
        new_code = secrets.token_urlsafe(12)

    current_user.family.invite_code = new_code
    db.session.commit()
    flash('Invite code regenerated. Share the new code with future members.', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin_panel/family/archive', methods=['POST'])
@login_required
def archive_family():
    """Archive family"""
    if not is_family_manager(current_user):
        flash('Unauthorized', 'error')
        return redirect(url_for('dashboard'))

    current_user.family.is_archived = True
    db.session.commit()
    flash('Family has been archived.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/admin_panel/family/delete', methods=['POST'])
@login_required
def delete_family():
    """Delete family - PERMANENT"""
    if not is_family_manager(current_user):
        flash('Unauthorized', 'error')
        return redirect(url_for('dashboard'))

    family_id = current_user.family_id

    # Delete all expenses, categories, expected expenses
    Expense.query.filter_by(family_id=family_id).delete()
    Category.query.filter_by(family_id=family_id).delete()
    ExpectedExpense.query.filter_by(family_id=family_id).delete()
    MonthlyFinanceSetting.query.filter_by(family_id=family_id).delete()
    BudgetPlanApplication.query.filter_by(family_id=family_id).delete()
    BudgetSuggestion.query.filter_by(family_id=family_id).delete()
    AISavingsForecast.query.filter_by(family_id=family_id).delete()
    BudgetReport.query.filter_by(family_id=family_id).delete()

    # Remove all members from family
    User.query.filter_by(family_id=family_id).update({User.family_id: None})

    # Delete family
    Family.query.filter_by(id=family_id).delete()
    db.session.commit()

    flash('Family has been permanently deleted.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/admin_panel/family/erase_data', methods=['POST'])
@login_required
def erase_family_data():
    """Erase all expenses, budgets, plans, reports, and forecasts for current family, keeping workspace & users intact."""
    if not is_family_manager(current_user):
        flash('Unauthorized: Only the Family Manager can erase workspace data.', 'error')
        return redirect(url_for('admin_panel'))

    confirm_text = request.form.get('confirm_text', '').strip().upper()
    if confirm_text != 'ERASE':
        flash('Confirmation failed: You must type "ERASE" to wipe all financial data.', 'error')
        return redirect(url_for('admin_panel', tab='settings'))

    family_id = current_user.family_id
    family = Family.query.get(family_id)
    if not family:
        flash('Family not found.', 'error')
        return redirect(url_for('dashboard'))

    try:
        # Delete all expense items
        Expense.query.filter_by(family_id=family_id).delete()
        # Delete planned bills and buckets
        ExpectedExpense.query.filter_by(family_id=family_id).delete()
        # Delete monthly overrides
        MonthlyFinanceSetting.query.filter_by(family_id=family_id).delete()
        # Delete AI plan applications & suggestions
        BudgetPlanApplication.query.filter_by(family_id=family_id).delete()
        BudgetSuggestion.query.filter_by(family_id=family_id).delete()
        # Delete AI savings forecasts & daily reports
        AISavingsForecast.query.filter_by(family_id=family_id).delete()
        BudgetReport.query.filter_by(family_id=family_id).delete()
        # Delete AI expense exception rules
        AIExpenseException.query.filter_by(family_id=family_id).delete()
        # Delete chatbot history
        ChatbotMessage.query.filter_by(family_id=family_id).delete()
        # Reset category monthly limits
        Category.query.filter_by(family_id=family_id).update({Category.monthly_limit: 0.0})
        # Reset family ai notes
        family.ai_notes = ''

        db.session.add(AuditLog(
            user_id=current_user.id,
            family_id=family_id,
            action='ERASE_ALL_DATA',
            details=f'All transactions, plans, reports, and budgets erased by {current_user.username}'
        ))
        db.session.commit()
        flash(f'All financial data for {family.name} has been successfully erased. Workspace reset clean!', 'success')
    except Exception as exc:
        db.session.rollback()
        app.logger.exception('Failed to erase family data')
        flash(f'Failed to erase family data: {exc}', 'error')

    return redirect(url_for('admin_panel', tab='settings'))


def generate_monthly_expense_book_template(year=None, month=None, family=None):
    """
    Generate an in-memory .xlsx workbook for the requested month based on the master template.
    Pre-populates the Dashboard title, monthly calendar dates in Daily Summary, and the family's
    configured categories and members in Settings.
    """
    import zipfile
    import xml.etree.ElementTree as ET
    import io
    from datetime import date
    import calendar

    today = date.today()
    target_year = int(year or today.year)
    target_month = int(month or today.month)
    if not (1 <= target_month <= 12):
        target_month = today.month

    m_name = calendar.month_name[target_month]
    days_in_m = calendar.monthrange(target_year, target_month)[1]
    family_name = family.name if family else 'FAMILY'

    static_tpl_path = os.path.join(app.root_path, 'static', 'templates', 'VaultSync_Monthly_Expense_Book_Template.xlsx')
    if not os.path.exists(static_tpl_path):
        return None

    with open(static_tpl_path, 'rb') as f:
        src_bytes = f.read()

    ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    out_buf = io.BytesIO()

    with zipfile.ZipFile(io.BytesIO(src_bytes), 'r') as zin, zipfile.ZipFile(out_buf, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
        strings = []
        strings_root = None
        if 'xl/sharedStrings.xml' in zin.namelist():
            strings_root = ET.fromstring(zin.read('xl/sharedStrings.xml'))
            for si in strings_root.findall(f'.//{ns}si'):
                strings.append(''.join(t.text or '' for t in si.findall(f'.//{ns}t')))

        def add_string(s):
            if s in strings:
                return strings.index(s)
            strings.append(s)
            si_elem = ET.Element(f'{ns}si')
            t_elem = ET.SubElement(si_elem, f'{ns}t')
            t_elem.text = s
            if strings_root is not None:
                strings_root.append(si_elem)
            return len(strings) - 1

        for item in zin.infolist():
            if item.filename == 'xl/sharedStrings.xml':
                continue
            content = zin.read(item.filename)

            if item.filename == 'xl/worksheets/sheet2.xml':
                # Dashboard title in B1
                sroot = ET.fromstring(content)
                title_str = f"{family_name.upper()} EXPENSE DASHBOARD - {m_name.upper()} {target_year}"
                title_idx = add_string(title_str)
                for c in sroot.findall(f'.//{ns}c'):
                    if c.attrib.get('r') == 'B1':
                        c.attrib['t'] = 's'
                        v = c.find(f'{ns}v')
                        if v is None:
                            v = ET.SubElement(c, f'{ns}v')
                        v.text = str(title_idx)
                content = ET.tostring(sroot, encoding='utf-8')

            elif item.filename == 'xl/worksheets/sheet3.xml':
                # Daily Summary: title in A1 and serial dates in A8..A38
                sroot = ET.fromstring(content)
                d_title = f"DAILY EXPENSE & DATE-WISE SUMMARY – {m_name.upper()} {target_year}"
                d_title_idx = add_string(d_title)
                base_excel_date = date(1899, 12, 30)

                for c in sroot.findall(f'.//{ns}c'):
                    ref = c.attrib.get('r')
                    if ref == 'A1':
                        c.attrib['t'] = 's'
                        v = c.find(f'{ns}v')
                        if v is None:
                            v = ET.SubElement(c, f'{ns}v')
                        v.text = str(d_title_idx)
                    elif ref and ref.startswith('A') and ref[1:].isdigit():
                        row_idx = int(ref[1:])
                        if 8 <= row_idx <= 38:
                            day_num = row_idx - 7
                            v = c.find(f'{ns}v')
                            if day_num <= days_in_m:
                                target_d = date(target_year, target_month, day_num)
                                serial = (target_d - base_excel_date).days
                                if v is None:
                                    v = ET.SubElement(c, f'{ns}v')
                                v.text = str(serial)
                            else:
                                if v is not None:
                                    c.remove(v)

                content = ET.tostring(sroot, encoding='utf-8')

            zout.writestr(item.filename, content)

        if strings_root is not None:
            strings_root.attrib['count'] = str(len(strings))
            strings_root.attrib['uniqueCount'] = str(len(strings))
            zout.writestr('xl/sharedStrings.xml', ET.tostring(strings_root, encoding='utf-8'))

    return out_buf.getvalue()


@app.route('/download-book-template', methods=['GET'])
@app.route('/api/download-template', methods=['GET'])
@login_required
def download_expense_book_template():
    """Download the official monthly expense book Excel template (.xlsx) with automated formulas."""
    import io
    import calendar

    month = request.args.get('month')
    year = request.args.get('year')
    now = datetime.utcnow()
    m = int(month) if month and month.isdigit() else now.month
    y = int(year) if year and year.isdigit() else now.year
    m_name = calendar.month_name[m]

    family = current_user.family if hasattr(current_user, 'family') else None
    tpl_bytes = generate_monthly_expense_book_template(year=y, month=m, family=family)

    if not tpl_bytes:
        static_tpl = os.path.join(app.root_path, 'static', 'templates', 'VaultSync_Monthly_Expense_Book_Template.xlsx')
        if os.path.exists(static_tpl):
            return send_file(
                static_tpl,
                as_attachment=True,
                download_name=f'VaultSync_Expense_Book_Template_{m_name}_{y}.xlsx',
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
        return jsonify({'error': 'Template file not found'}), 404

    filename = f"VaultSync_Expense_Book_{family.name.replace(' ', '_') if family else 'Template'}_{m_name}_{y}.xlsx"
    return send_file(
        io.BytesIO(tpl_bytes),
        as_attachment=True,
        download_name=filename,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@app.route('/admin_panel/reports', methods=['GET'])
@login_required
def reports():
    """Redirect legacy admin reports route to unified financial reports."""
    return redirect(url_for('financial_reports'))


@app.route('/admin_panel/reports/data', methods=['GET'])
@login_required
def reports_data():
    """JSON endpoint for report charts"""
    if not is_family_manager(current_user):
        return jsonify({'error': 'Unauthorized'}), 403

    family = current_user.family
    current_date = datetime.utcnow()

    # Spending trends (last 6 months)
    trend_labels = []
    trend_data = []
    for i in range(5, -1, -1):
        date_Cursor = current_date - timedelta(days=30*i)
        month = date_Cursor.month
        year = date_Cursor.year
        month_str = f"{month:02d}"
        year_str = str(year)

        total = db.session.query(
            func.sum(Expense.amount).label('total')
        ).filter(
            func.strftime('%Y', Expense.date) == year_str,
            func.strftime('%m', Expense.date) == month_str,
            Expense.family_id == family.id
        ).scalar() or 0.0

        trend_labels.append(f"{calendar.month_abbr[month]} {year}")
        trend_data.append(float(total))

    return jsonify({
        'trends': {
            'labels': trend_labels,
            'data': trend_data
        }
    })


# --- DAILY REPORTS ROUTES ---
@app.route('/reports', methods=['GET'])
@login_required
def daily_reports():
    """View daily budget reports with AI suggestions."""
    if not current_user.family_id:
        flash('Please join a family before viewing reports.', 'error')
        return redirect(url_for('dashboard'))

    # Get all reports for this family, paginated
    page = request.args.get('page', 1, type=int)
    reports = BudgetReport.query.filter_by(
        family_id=current_user.family_id
    ).order_by(BudgetReport.report_date.desc()).paginate(page=page, per_page=10)

    # Mark older reports as unread if there are new ones
    unread_reports = BudgetReport.query.filter_by(
        family_id=current_user.family_id,
        is_read=False
    ).order_by(BudgetReport.report_date.desc()).all()

    # Get latest report for prominent display
    latest_report = BudgetReport.query.filter_by(
        family_id=current_user.family_id
    ).order_by(BudgetReport.report_date.desc()).first()
    latest_action_plan = None
    if latest_report:
        try:
            latest_suggestions = json.loads(latest_report.suggestions) if latest_report.suggestions else []
            latest_warnings = json.loads(latest_report.warnings) if latest_report.warnings else []
            latest_categories = json.loads(latest_report.category_breakdown) if latest_report.category_breakdown else {}
        except (json.JSONDecodeError, TypeError):
            latest_suggestions = []
            latest_warnings = []
            latest_categories = {}
        latest_action_plan = build_report_action_plan(
            latest_report,
            latest_suggestions,
            latest_warnings,
            latest_categories
        )

    return render_template('reports.html',
                          user=current_user,
                          reports=reports,
                          latest_report=latest_report,
                          latest_action_plan=latest_action_plan,
                          unread_count=len(unread_reports))


@app.route('/reports/<int:report_id>', methods=['GET'])
@login_required
def view_report(report_id):
    """View a specific daily budget report."""
    report = BudgetReport.query.filter_by(
        id=report_id,
        family_id=current_user.family_id
    ).first_or_404()

    # Mark as read
    if not report.is_read:
        report.is_read = True
        db.session.commit()

    # Parse JSON fields
    try:
        suggestions = json.loads(report.suggestions) if report.suggestions else []
        warnings = json.loads(report.warnings) if report.warnings else []
        insights = json.loads(report.insights) if report.insights else []
        category_breakdown = json.loads(report.category_breakdown) if report.category_breakdown else {}
    except (json.JSONDecodeError, TypeError):
        suggestions = []
        warnings = []
        insights = []
        category_breakdown = {}
    category_breakdown_items = sorted(
        category_breakdown.items(),
        key=lambda item: float(item[1] or 0),
        reverse=True
    )
    action_plan = build_report_action_plan(report, suggestions, warnings, category_breakdown)

    return render_template('report_detail.html',
                          user=current_user,
                          report=report,
                          suggestions=suggestions,
                          warnings=warnings,
                          insights=insights,
                          category_breakdown=category_breakdown,
                          category_breakdown_items=category_breakdown_items,
                          action_plan=action_plan)


@app.route('/reports/<int:report_id>/mark_read', methods=['POST'])
@login_required
def mark_report_read(report_id):
    """Mark a report as read."""
    report = BudgetReport.query.filter_by(
        id=report_id,
        family_id=current_user.family_id
    ).first_or_404()

    report.is_read = True
    db.session.commit()
    flash('Report marked as read.', 'success')
    return redirect(request.referrer or url_for('daily_reports'))


@app.route('/reports/<int:report_id>/delete', methods=['POST'])
@login_required
def delete_report(report_id):
    """Delete a daily budget report."""
    if not is_family_manager(current_user):
        flash('Unauthorized: family manager only', 'error')
        return redirect(url_for('daily_reports'))

    report = BudgetReport.query.filter_by(
        id=report_id,
        family_id=current_user.family_id
    ).first_or_404()

    report_date = report.report_date
    db.session.delete(report)
    db.session.commit()
    flash(f'Report for {report_date.strftime("%B %d, %Y")} deleted.', 'success')
    return redirect(url_for('daily_reports'))


@app.route('/reports/generate_now', methods=['POST'])
@login_required
def generate_report_now():
    """Generate a report immediately (for testing or manual trigger)."""
    if not is_family_manager(current_user):
        flash('Unauthorized: family manager only', 'error')
        return redirect(url_for('daily_reports'))

    try:
        report = create_daily_budget_report(current_user.family_id)
        flash('Daily budget report generated successfully!', 'success')
        return redirect(url_for('view_report', report_id=report.id))
    except Exception as exc:
        app.logger.exception('Failed to generate report')
        flash(f'Failed to generate report: {exc}', 'error')
        return redirect(url_for('daily_reports'))


from flask_apscheduler import APScheduler
scheduler = APScheduler()

@scheduler.task('cron', id='auto_pay_job', hour=1, minute=0)
def auto_pay_expected_expenses():
    with app.app_context():
        today = datetime.now().date()
        due_buckets = ExpectedExpense.query.filter_by(is_autopay=True, is_paid=False, month=today.month, year=today.year).filter(ExpectedExpense.due_day <= today.day).all()
        for bucket in due_buckets:
            target_user_id = default_family_ai_user_id(bucket.family) if bucket.family else None
            if not target_user_id:
                continue
            exp = Expense(
                date=today, 
                item_name=f'Auto-Pay: {bucket.name}', 
                amount=bucket.amount, 
                category_id=bucket.category_id, 
                user_id=target_user_id, 
                family_id=bucket.family_id, 
                bucket_id=bucket.id
            )
            bucket.is_paid = True
            bucket.paid_at = datetime.utcnow()
            db.session.add(exp)
        db.session.commit()

@scheduler.task('cron', id='chatbot_batch_summary', hour=2, minute=0)
def batch_chatbot_summaries():
    with app.app_context():
        today = datetime.now().date()
        families = Family.query.filter_by(is_archived=False).all()
        for family in families:
            month_start, month_end = month_bounds(today.year, today.month)
            total_spent = sum(float(e.amount) for e in Expense.query.filter(Expense.family_id == family.id, Expense.date >= month_start).all())
            finance = monthly_finance_for(family, today.month, today.year)
            family.ai_notes = f'Monthly Budget: {finance["monthly_budget"]}, Spent: {total_spent}, Remaining: {finance["monthly_budget"] - total_spent}.'
        db.session.commit()

scheduler.init_app(app)
scheduler.start()


import base64

@app.route('/api/scan-receipt', methods=['POST'])
@login_required
def scan_receipt():
    if 'receipt' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['receipt']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
        
    if file:
        file_data = file.read()
        mime_type = file.mimetype
        if not mime_type:
            mime_type = 'application/pdf' if file.filename.lower().endswith('.pdf') else 'image/jpeg'
            
        extracted_text = None
        base64_file = None
        
        if mime_type == 'application/pdf':
            try:
                import PyPDF2
                import io
                pdf_reader = PyPDF2.PdfReader(io.BytesIO(file_data))
                text_parts = []
                for page in pdf_reader.pages:
                    text_parts.append(page.extract_text() or '')
                extracted_text = "\n".join(text_parts)
            except ImportError:
                return jsonify({'error': 'PyPDF2 is not installed.'}), 500
            except Exception as e:
                return jsonify({'error': str(e)}), 500
        else:
            base64_file = base64.b64encode(file_data).decode('utf-8')

        api_keys = configured_gemini_keys()
        if not api_keys:
            return jsonify({'error': 'Gemini API key not configured'}), 500
            
        categories = [c.name for c in Category.query.filter_by(family_id=current_user.family_id).all()]
        categories_str = ", ".join(categories)
        
        prompt = f"Extract the total amount, the main item name/vendor, optional date (YYYY-MM-DD if visible), and suggest the best category from this list: {categories_str}. Return ONLY valid JSON with keys: 'amount' (number), 'item_name' (string), 'category' (string), 'date' (string, optional)."
        if extracted_text:
            prompt += f"\n\nDOCUMENT TEXT:\n{extracted_text}"
            
        parts = [{"text": prompt}]
        if base64_file:
            parts.append({"inlineData": {"mimeType": mime_type, "data": base64_file}})
            
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            }
        }
        
        last_error = 'Failed to analyze receipt'
        for api_key in api_keys:
            url = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}'
            try:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST'
                )
                with urllib.request.urlopen(req, timeout=30) as response:
                    result = json.loads(response.read().decode('utf-8'))
                    text_response = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
                    parsed = json.loads(text_response)
                    return jsonify(parsed)
            except Exception as e:
                last_error = str(e)
                continue
        return jsonify({'error': last_error}), 500


def parse_xlsx_rows(file_bytes):
    """Zero-dependency column-aligned .xlsx reader supporting multi-sheet Expense Books."""
    import io
    import zipfile
    import xml.etree.ElementTree as ET

    def col_to_idx(cell_ref):
        m = re.match(r'([A-Z]+)', cell_ref or '')
        if not m:
            return 0
        idx = 0
        for ch in m.group(1):
            idx = idx * 26 + (ord(ch) - ord('A') + 1)
        return idx - 1

    ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    all_sheets_rows = []
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
        strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            root = ET.fromstring(z.read('xl/sharedStrings.xml'))
            for si in root.findall(f'.//{ns}si'):
                strings.append(''.join(t.text or '' for t in si.findall(f'.//{ns}t')))

        sheet_files = sorted(
            [name for name in z.namelist() if name.startswith('xl/worksheets/sheet') and name.endswith('.xml')],
            key=lambda x: int(re.search(r'sheet(\d+)\.xml', x).group(1)) if re.search(r'sheet(\d+)\.xml', x) else 999
        )
        for sf in sheet_files:
            sroot = ET.fromstring(z.read(sf))
            sheet_rows = []
            for r in sroot.findall(f'.//{ns}row'):
                row_map = {}
                for c in r.findall(f'{ns}c'):
                    cidx = col_to_idx(c.attrib.get('r'))
                    t = c.attrib.get('t')
                    v = c.find(f'{ns}v')
                    is_el = c.find(f'{ns}is')
                    val = ''
                    if t == 's' and v is not None and v.text:
                        s_idx = int(v.text)
                        if 0 <= s_idx < len(strings):
                            val = strings[s_idx]
                    elif t == 'inlineStr' and is_el is not None:
                        val = ''.join(x.text or '' for x in is_el.findall(f'.//{ns}t'))
                    elif v is not None and v.text:
                        val = v.text
                    row_map[cidx] = str(val).strip()
                if row_map and any(row_map.values()):
                    max_c = max(row_map.keys())
                    sheet_rows.append([row_map.get(i, '') for i in range(max_c + 1)])
            if sheet_rows:
                all_sheets_rows.extend(sheet_rows)
                all_sheets_rows.append([])
    return all_sheets_rows


def parse_book_date_value(raw_val):
    """Parse ISO dates, DD/MM/YYYY, DD-MM-YYYY, or Excel serial dates (e.g. 46296 -> 2026-10-01)."""
    if raw_val is None:
        return None
    s = str(raw_val).strip()
    if not s or s in ('--', '-', 'N/A', 'None'):
        return None
    # Check Excel serial date (integer or float like '46296' or '46296.0')
    if re.match(r'^\d{5}(\.0+)?$', s):
        serial = int(float(s))
        if 36526 <= serial <= 60000:
            return date(1899, 12, 30) + timedelta(days=serial)
    # Try common date formats
    for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%Y/%m/%d', '%d %b %Y', '%d %B %Y', '%m/%d/%Y'):
        try:
            return datetime.strptime(s[:10] if fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%Y/%m/%d') else s, fmt).date()
        except ValueError:
            continue
    return None


def parse_book_amount_value(raw_val):
    if raw_val is None:
        return None
    s = str(raw_val).strip()
    if not s or s in ('--', '-', 'N/A'):
        return None
    cleaned = re.sub(r'(?i)(rs\.?|inr|₹|,|\s)', '', s)
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None


def extract_structured_book_data(rows):
    """
    Scan 2D rows (from CSV, TSV, pasted spreadsheet, or multi-sheet XLSX) and extract:
    - transactions: list of dicts
    - category_budgets: dict of {category_name: float_limit}
    - settings_categories: list of category names
    - settings_members: list of member names
    """
    transactions = []
    category_budgets = {}
    settings_categories = []
    settings_members = []

    n = len(rows)
    i = 0
    while i < n:
        row = [str(c or '').strip() for c in rows[i]]
        lower_row = [c.lower() for c in row]

        # 1. Check if this row contains a Category Monthly Budget sub-table header
        for c_idx in range(len(lower_row) - 1):
            if lower_row[c_idx] == 'category' and lower_row[c_idx + 1] in ('monthly budget', 'budget', 'monthly limit', 'allocated budget'):
                b_row_idx = i + 1
                while b_row_idx < n:
                    b_row = [str(c or '').strip() for c in rows[b_row_idx]]
                    if c_idx >= len(b_row):
                        b_row_idx += 1
                        continue
                    cat_cell = b_row[c_idx].strip()
                    amt_cell = b_row[c_idx + 1].strip() if (c_idx + 1) < len(b_row) else ''
                    if not cat_cell and not amt_cell:
                        b_row_idx += 1
                        continue
                    if cat_cell.lower() in ('total', 'total expenses', 'grand total', 'categories', 'date'):
                        break
                    amt_val = parse_book_amount_value(amt_cell)
                    if cat_cell and amt_val is not None and amt_val >= 0 and not parse_book_date_value(cat_cell):
                        category_budgets[cat_cell] = amt_val
                    elif cat_cell and amt_val is None and b_row_idx > i + 2:
                        break
                    b_row_idx += 1

        # 2. Check if this row is the Settings / Reference lists table header
        if 'categories' in lower_row and 'members' in lower_row:
            cat_col = lower_row.index('categories')
            mem_col = lower_row.index('members')
            s_idx = i + 1
            while s_idx < n:
                s_row = [str(c or '').strip() for c in rows[s_idx]]
                if not any(s_row):
                    break
                c_val = s_row[cat_col] if cat_col < len(s_row) else ''
                m_val = s_row[mem_col] if mem_col < len(s_row) else ''
                if c_val.lower() in ('date', 'category', 'total'):
                    break
                if c_val and c_val.lower() not in ('transfer/conversion', 'transfer', '--'):
                    if c_val not in settings_categories:
                        settings_categories.append(c_val)
                if m_val and m_val.lower() not in ('--', 'family', 'all'):
                    if m_val not in settings_members:
                        settings_members.append(m_val)
                s_idx += 1

        # 3. Check if this row is the Transaction Ledger Header
        has_date = 'date' in lower_row
        has_amount = any(k in lower_row for k in ('amount', 'debit', 'spent', 'cost', 'amount (rs)', 'amount (₹)'))
        has_desc = any(k in lower_row for k in ('expense description', 'description', 'item', 'item name', 'item_name', 'particulars', 'narration'))

        if has_date and has_amount and has_desc:
            def find_col(*names):
                for name in names:
                    if name in lower_row:
                        return lower_row.index(name)
                return None

            col_date = find_col('date')
            col_time = find_col('time')
            col_desc = find_col('expense description', 'item name', 'item_name', 'item', 'description', 'particulars', 'narration')
            col_cat = find_col('category', 'category name')
            col_amt = find_col('amount', 'debit', 'spent', 'cost', 'amount (rs)', 'amount (₹)')
            col_method = find_col('payment method', 'method', 'mode', 'payment mode')
            col_account = find_col('account / paid by', 'account', 'paid by', 'source account')
            col_payee = find_col('payee / beneficiary', 'payee', 'beneficiary', 'merchant', 'vendor')
            col_txid = find_col('transaction id', 'txn id', 'tx id', 'reference id')
            col_utr = find_col('utr no.', 'utr no', 'utr', 'upi ref')
            col_type = find_col('type', 'expense type', 'need/want')
            col_member = find_col('member', 'spender', 'user', 'paid by member', 'person')
            col_notes = find_col('notes / purpose', 'notes', 'purpose', 'remarks')

            t_idx = i + 1
            empty_streak = 0
            while t_idx < n:
                t_row = [str(c or '').strip() for c in rows[t_idx]]
                if not any(t_row):
                    empty_streak += 1
                    if empty_streak >= 2:
                        break
                    t_idx += 1
                    continue
                empty_streak = 0

                def get_cell(col_idx):
                    if col_idx is not None and col_idx < len(t_row):
                        val = t_row[col_idx].strip()
                        return '' if val in ('--', '-') else val
                    return ''

                raw_date = get_cell(col_date)
                tx_date = parse_book_date_value(raw_date)
                tx_amt = parse_book_amount_value(get_cell(col_amt))
                tx_desc = get_cell(col_desc)

                if not tx_date:
                    # Stop if we hit summary/dashboard section headers
                    first_non_empty = next((c.lower() for c in t_row if c), '')
                    if any(h in first_non_empty for h in ('family expense dashboard', 'total spent', 'daily expense', 'categories')):
                        break
                    t_idx += 1
                    continue

                if tx_amt is not None and tx_amt > 0 and tx_desc:
                    transactions.append({
                        'date': tx_date,
                        'time': get_cell(col_time),
                        'item_name': tx_desc[:150],
                        'category': get_cell(col_cat) or 'Other',
                        'amount': tx_amt,
                        'payment_method': get_cell(col_method),
                        'account': get_cell(col_account),
                        'payee': get_cell(col_payee),
                        'tx_id': get_cell(col_txid),
                        'utr': get_cell(col_utr),
                        'tx_type': get_cell(col_type) or 'Need',
                        'member': get_cell(col_member),
                        'notes': get_cell(col_notes),
                    })
                t_idx += 1
            i = t_idx
            continue

        i += 1

    return {
        'transactions': transactions,
        'category_budgets': category_budgets,
        'settings_categories': settings_categories,
        'settings_members': settings_members,
    }


def extract_ai_book_data(raw_text, base64_file, mime_type, instruction, family_id):
    """Fallback to Gemini AI to extract transactions, category budgets, and members from unstructured files/PDFs/images."""
    api_keys = configured_gemini_keys()
    if not api_keys:
        raise ValueError('Gemini API key not configured for unstructured document parsing.')

    existing_cats = [c.name for c in Category.query.filter_by(family_id=family_id).all()]
    prompt = (
        "You are VaultSync's Smart Expense Book & Ledger Importer.\n"
        "Extract ALL transactions, any Category Monthly Budget table, and family member names from the provided expense book or statement.\n"
        "For each transaction, identify:\n"
        "- date (YYYY-MM-DD)\n"
        "- item_name (short clean name of expense)\n"
        "- category (use book category or best match from: " + ", ".join(existing_cats) + ")\n"
        "- amount (number)\n"
        "- tx_type ('Need', 'Want', or 'Transfer' for ATM withdrawals/cash conversions/settled splits)\n"
        "- member (person name if mentioned, e.g., Arish, Nahid, Shakir, Family)\n"
        "- payment_method (e.g., UPI/ONLINE, CASH)\n"
        "- account (e.g., SBI, Axis Bank, Cash in Hand)\n"
        "- payee (merchant/beneficiary)\n"
        "- utr (UTR or reference number if present)\n"
        "- notes (purpose or details)\n"
    )
    if instruction:
        prompt += f"\nUSER INSTRUCTIONS: {instruction}\n"
    if raw_text:
        prompt += f"\nDOCUMENT CONTENT:\n{raw_text[:60000]}\n"

    schema = {
        'type': 'object',
        'properties': {
            'transactions': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'date': {'type': 'string'},
                        'item_name': {'type': 'string'},
                        'category': {'type': 'string'},
                        'amount': {'type': 'number'},
                        'tx_type': {'type': 'string'},
                        'member': {'type': 'string'},
                        'payment_method': {'type': 'string'},
                        'account': {'type': 'string'},
                        'payee': {'type': 'string'},
                        'utr': {'type': 'string'},
                        'notes': {'type': 'string'},
                    },
                    'required': ['date', 'item_name', 'category', 'amount']
                }
            },
            'category_budgets': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'category': {'type': 'string'},
                        'monthly_budget': {'type': 'number'}
                    },
                    'required': ['category', 'monthly_budget']
                }
            }
        },
        'required': ['transactions']
    }

    parts = [{'text': prompt}]
    if base64_file and mime_type:
        parts.append({'inlineData': {'mimeType': mime_type, 'data': base64_file}})

    payload = {
        'contents': [{'parts': parts}],
        'generationConfig': {
            'temperature': 0.1,
            'responseMimeType': 'application/json',
            'responseSchema': schema,
        }
    }

    last_error = 'Failed to parse document with AI'
    for api_key in api_keys:
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}'
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'}, method='POST')
            with urllib.request.urlopen(req, timeout=60) as response:
                result = json.loads(response.read().decode('utf-8'))
                text_response = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
                ai_data = json.loads(text_response)
                txs = []
                for item in (ai_data.get('transactions') or []):
                    d = parse_book_date_value(item.get('date')) or datetime.now().date()
                    amt = parse_book_amount_value(item.get('amount'))
                    if amt and amt > 0:
                        txs.append({
                            'date': d,
                            'time': '',
                            'item_name': str(item.get('item_name') or 'Expense').strip()[:150],
                            'category': str(item.get('category') or 'Other').strip(),
                            'amount': amt,
                            'payment_method': str(item.get('payment_method') or '').strip(),
                            'account': str(item.get('account') or '').strip(),
                            'payee': str(item.get('payee') or '').strip(),
                            'tx_id': '',
                            'utr': str(item.get('utr') or '').strip(),
                            'tx_type': str(item.get('tx_type') or 'Need').strip(),
                            'member': str(item.get('member') or '').strip(),
                            'notes': str(item.get('notes') or '').strip(),
                        })
                cat_budgets = {}
                for b in (ai_data.get('category_budgets') or []):
                    cname = str(b.get('category') or '').strip()
                    bval = parse_book_amount_value(b.get('monthly_budget'))
                    if cname and bval is not None and bval >= 0:
                        cat_budgets[cname] = bval
                return {
                    'transactions': txs,
                    'category_budgets': cat_budgets,
                    'settings_categories': list(cat_budgets.keys()),
                    'settings_members': [],
                }
        except Exception as e:
            last_error = str(e)
            continue
    raise ValueError(last_error)


def normalize_expense_date(d):
    """Normalize date object, datetime, string, or Excel serial to datetime.date."""
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if isinstance(d, str):
        parsed = parse_book_date_value(d)
        if parsed:
            return parsed
    return None


def normalize_text_key(text):
    """Normalize text for invariant comparison: lowercase, stripped of punctuation and whitespace."""
    if not text:
        return ''
    s = str(text).strip().lower()
    return re.sub(r'[^a-z0-9]', '', s)


def extract_utr_tokens(text):
    """Extract bank/UPI UTR, Txn ID, or 10-18 digit reference tokens from text."""
    if not text:
        return set()
    s = str(text).lower()
    tokens = set()
    for m in re.finditer(r'(?:utr|txn|ref|upi|rrn)[:\s\-\.]*([a-zA-Z0-9]{6,30})', s):
        tokens.add(m.group(1).lower())
    for d in re.findall(r'\b\d{10,18}\b', s):
        tokens.add(d.lower())
    return tokens


def extract_google_sheet_csv_url(url):
    """
    Convert any public or viewable Google Sheets URL into a direct CSV export URL.
    Supports:
    - https://docs.google.com/spreadsheets/d/{ID}/edit#gid={GID}
    - https://docs.google.com/spreadsheets/d/{ID}/pubhtml
    - https://docs.google.com/spreadsheets/d/{ID}/preview
    """
    if not url:
        return None
    url = url.strip()
    m_id = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', url)
    if not m_id:
        return None
    sheet_id = m_id.group(1)

    m_gid = re.search(r'[#&?]gid=([0-9]+)', url)
    gid = m_gid.group(1) if m_gid else '0'

    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def implement_parsed_book(parsed_data, family_id, default_user, apply_budgets=True, sync_members=True, skip_transfers=True):
    """
    Takes extracted book structure (transactions, category_budgets, settings_categories, settings_members)
    and implements everything in the database:
    - Auto-creates/updates Categories and their monthly_limit
    - Updates MonthlyFinanceSetting & Family.monthly_budget if category_budgets are present and apply_budgets=True
    - Auto-matches or creates Family Member profiles (e.g., Arish, Nahid, Shakir)
    - Inserts Expenses (skipping internal Transfers if skip_transfers=True, and deduplicating against existing rows)
    """
    from werkzeug.security import generate_password_hash
    from collections import Counter, defaultdict

    family = Family.query.get(family_id)
    transactions = parsed_data.get('transactions') or []
    category_budgets = parsed_data.get('category_budgets') or {}
    settings_categories = parsed_data.get('settings_categories') or []
    settings_members = parsed_data.get('settings_members') or []

    # 1. Sync Categories & Monthly Budgets
    existing_categories = Category.query.filter_by(family_id=family_id).all()
    cat_by_lower = {c.name.strip().lower(): c for c in existing_categories}

    categories_created = []
    budgets_updated = []

    all_cat_names = list(category_budgets.keys()) + [c for c in settings_categories if c not in category_budgets]
    for tx in transactions:
        c_name = (tx.get('category') or '').strip()
        tx_type = (tx.get('tx_type') or '').strip().lower()
        if c_name and tx_type != 'transfer' and 'transfer' not in c_name.lower() and c_name not in all_cat_names:
            all_cat_names.append(c_name)

    for raw_cname in all_cat_names:
        cname = raw_cname.strip()
        if not cname or 'transfer' in cname.lower():
            continue
        key = cname.lower()
        cat_obj = cat_by_lower.get(key)
        if not cat_obj:
            cat_obj = Category(
                name=cname[:100],
                color=get_next_auto_color(family_id),
                is_color_auto=True,
                is_fixed=True,
                family_id=family_id,
                monthly_limit=float(category_budgets.get(raw_cname, 0.0)) if apply_budgets else 0.0
            )
            db.session.add(cat_obj)
            db.session.flush()
            cat_by_lower[key] = cat_obj
            categories_created.append(cat_obj.name)
            if apply_budgets and raw_cname in category_budgets:
                budgets_updated.append(f"{cat_obj.name} (₹{category_budgets[raw_cname]:,.0f})")
        else:
            if apply_budgets and raw_cname in category_budgets:
                new_limit = float(category_budgets[raw_cname])
                if round(float(cat_obj.monthly_limit or 0), 2) != round(new_limit, 2):
                    cat_obj.monthly_limit = new_limit
                    budgets_updated.append(f"{cat_obj.name} (₹{new_limit:,.0f})")

    # Determine primary month/year of the book from transaction dates
    month_year_counts = Counter(
        (normalize_expense_date(tx.get('date')).month, normalize_expense_date(tx.get('date')).year)
        for tx in transactions
        if tx.get('date') and normalize_expense_date(tx.get('date'))
    )
    if month_year_counts:
        (book_month, book_year), _ = month_year_counts.most_common(1)[0]
    else:
        now_d = datetime.now().date()
        book_month, book_year = now_d.month, now_d.year

    total_book_budget = round(sum(float(v) for v in category_budgets.values()), 2) if category_budgets else 0.0
    if apply_budgets and total_book_budget > 0 and family:
        setting = MonthlyFinanceSetting.query.filter_by(
            family_id=family_id,
            month=book_month,
            year=book_year
        ).first()
        if not setting:
            setting = MonthlyFinanceSetting(
                family_id=family_id,
                month=book_month,
                year=book_year,
                monthly_budget=total_book_budget,
                monthly_income=max(float(family.monthly_income or 0), total_book_budget),
                notes=f'Auto-configured from Expense Book ({calendar.month_name[book_month]} {book_year})'
            )
            db.session.add(setting)
        else:
            setting.monthly_budget = total_book_budget
        family.monthly_budget = total_book_budget

    # 2. Sync Family Members
    family_users = User.query.filter_by(family_id=family_id).all()
    user_by_lower = {}
    for u in family_users:
        user_by_lower[u.username.strip().lower()] = u
        if '_' in u.username:
            user_by_lower[u.username.split('_')[0].strip().lower()] = u

    members_created = []
    all_member_names = list(settings_members)
    for tx in transactions:
        m_name = (tx.get('member') or '').strip()
        if m_name and m_name.lower() not in ('family', 'all', '--', '-') and m_name not in all_member_names:
            all_member_names.append(m_name)

    if sync_members:
        for m_name in all_member_names:
            m_clean = m_name.strip()
            if not m_clean or m_clean.lower() in ('family', 'all', '--', '-'):
                continue
            m_key = m_clean.lower()
            if m_key not in user_by_lower:
                candidate_username = m_clean[:45]
                if User.query.filter(db.func.lower(User.username) == candidate_username.lower()).first():
                    candidate_username = f"{m_clean[:35]}_{family_id}"
                candidate_email = f"{re.sub(r'[^a-z0-9]', '', candidate_username.lower())}_{family_id}@vaultsync.local"
                new_member = User(
                    username=candidate_username,
                    email=candidate_email,
                    password_hash=generate_password_hash('vaultsync123'),
                    user_type='family_member',
                    family_id=family_id
                )
                db.session.add(new_member)
                db.session.flush()
                user_by_lower[m_key] = new_member
                members_created.append(m_clean)

    # 3. Multi-Layer Deduplication Engine
    # Ensures that re-uploading an offline Excel sheet with previous month transactions
    # never creates duplicate entries.
    existing_expenses = Expense.query.filter_by(family_id=family_id).all()
    exact_signatures = set()
    existing_utr_tokens = set()
    date_amt_index = defaultdict(list)

    for exp in existing_expenses:
        exp_d = normalize_expense_date(exp.date)
        exp_d_str = exp_d.strftime('%Y-%m-%d') if exp_d else ''
        exp_amt = round(float(exp.amount), 2)
        exp_norm = normalize_text_key(exp.item_name)
        if exp_d_str:
            exact_signatures.add((exp_d_str, exp_norm, exp_amt))
            date_amt_index[(exp_d_str, exp_amt)].append({
                'norm_name': exp_norm,
                'name': exp.item_name,
                'cat_id': exp.category_id,
                'desc': (exp.description or '').lower(),
            })
        utrs = extract_utr_tokens(exp.description)
        existing_utr_tokens.update(utrs)

    imported_expenses = []
    skipped_transfers = []
    skipped_duplicates = 0

    for tx in transactions:
        tx_type = (tx.get('tx_type') or 'Need').strip()
        raw_cat = (tx.get('category') or 'Other').strip()

        is_transfer = (tx_type.lower() == 'transfer') or ('transfer' in raw_cat.lower()) or ('conversion' in raw_cat.lower())
        if is_transfer and skip_transfers:
            skipped_transfers.append(tx)
            continue

        raw_date = tx.get('date')
        tx_date = normalize_expense_date(raw_date) or datetime.utcnow().date()
        tx_d_str = tx_date.strftime('%Y-%m-%d')
        raw_name = (tx.get('item_name') or '').strip()[:150]
        norm_name = normalize_text_key(raw_name)
        amount = round(float(tx.get('amount') or 0), 2)

        if amount <= 0 or not raw_name:
            continue

        # Check 1: Canonical Signature Match (Exact Date + Normalized Item Name + Exact Amount)
        sig = (tx_d_str, norm_name, amount)
        if sig in exact_signatures:
            skipped_duplicates += 1
            continue

        # Check 2: Reference / UTR Unique Token Match
        cand_utrs = set()
        if tx.get('utr'):
            cand_utrs.update(extract_utr_tokens(str(tx['utr'])))
        if tx.get('tx_id'):
            cand_utrs.update(extract_utr_tokens(str(tx['tx_id'])))
        if tx.get('notes'):
            cand_utrs.update(extract_utr_tokens(str(tx['notes'])))

        if cand_utrs and (cand_utrs & existing_utr_tokens):
            skipped_duplicates += 1
            continue

        # Check 3: Date + Exact Amount with Substring/Token Match
        candidate_is_duplicate = False
        candidates_on_date_amt = date_amt_index.get((tx_d_str, amount), [])
        for ex in candidates_on_date_amt:
            ex_norm = ex['norm_name']
            if norm_name == ex_norm:
                candidate_is_duplicate = True
                break
            if len(norm_name) >= 3 and len(ex_norm) >= 3:
                if norm_name in ex_norm or ex_norm in norm_name:
                    candidate_is_duplicate = True
                    break
            raw_cat_lower = raw_cat.lower()
            if (raw_cat_lower in ex_norm or ex_norm in raw_cat_lower) and len(norm_name) >= 2:
                candidate_is_duplicate = True
                break

        if candidate_is_duplicate:
            skipped_duplicates += 1
            continue

        # Resolve category
        cat_obj = cat_by_lower.get(raw_cat.lower())
        if not cat_obj:
            cat_obj = cat_by_lower.get('other') or next(iter(cat_by_lower.values()), None)
            if not cat_obj:
                cat_obj = Category(
                    name=raw_cat[:100] or 'Other',
                    color=get_next_auto_color(family_id),
                    is_color_auto=True,
                    is_fixed=True,
                    family_id=family_id,
                    monthly_limit=0.0
                )
                db.session.add(cat_obj)
                db.session.flush()
                cat_by_lower[cat_obj.name.lower()] = cat_obj

        # Resolve member -> user_id
        raw_member = (tx.get('member') or '').strip()
        target_user = default_user
        if raw_member and raw_member.lower() not in ('family', 'all', '--', '-'):
            target_user = user_by_lower.get(raw_member.lower(), default_user)

        # Build rich structured description
        meta_tags = []
        if tx_type:
            meta_tags.append(tx_type)
        if tx.get('payment_method'):
            meta_tags.append(tx['payment_method'])
        if tx.get('account'):
            meta_tags.append(tx['account'])
        if tx.get('payee'):
            meta_tags.append(f"Payee: {tx['payee']}")
        if tx.get('utr'):
            meta_tags.append(f"UTR: {tx['utr']}")
        elif tx.get('tx_id'):
            meta_tags.append(f"Txn: {tx['tx_id']}")
        if raw_member.lower() == 'family':
            meta_tags.append('Shared Family')
        if tx.get('time'):
            meta_tags.append(tx['time'])

        prefix = f"[{' · '.join(meta_tags)}] " if meta_tags else ''
        full_desc = (prefix + (tx.get('notes') or '')).strip()[:500]

        bucket = find_budget_bucket(family_id, cat_obj.id, tx_date)
        new_exp = Expense(
            date=tx_date,
            item_name=raw_name,
            description=full_desc,
            amount=amount,
            category_id=cat_obj.id,
            user_id=target_user.id,
            family_id=family_id,
            bucket_id=bucket.id if bucket else None,
        )
        db.session.add(new_exp)

        # Register immediately into indexes so intra-batch duplicates in the same file are caught
        exact_signatures.add(sig)
        if cand_utrs:
            existing_utr_tokens.update(cand_utrs)
        date_amt_index[(tx_d_str, amount)].append({
            'norm_name': norm_name,
            'name': raw_name,
            'cat_id': cat_obj.id,
            'desc': full_desc.lower(),
        })
        imported_expenses.append(new_exp)

    db.session.commit()

    total_imported_amt = round(sum(float(e.amount) for e in imported_expenses), 2)
    total_transfer_amt = round(sum(float(t['amount']) for t in skipped_transfers), 2)

    summary_parts = [f"Imported {len(imported_expenses)} expenses (₹{total_imported_amt:,.2f})"]
    if skipped_transfers:
        summary_parts.append(f"filtered out {len(skipped_transfers)} internal transfers/conversions (₹{total_transfer_amt:,.2f})")
    if skipped_duplicates:
        summary_parts.append(f"skipped {skipped_duplicates} existing duplicates already in ledger")
    if budgets_updated:
        summary_parts.append(f"synced {len(budgets_updated)} category budgets (Total Cap: ₹{total_book_budget:,.0f})")
    if members_created:
        summary_parts.append(f"added family members: {', '.join(members_created)}")

    return {
        'success': True,
        'count': len(imported_expenses),
        'total_amount': total_imported_amt,
        'skipped_transfers_count': len(skipped_transfers),
        'skipped_transfers_amount': total_transfer_amt,
        'skipped_duplicates': skipped_duplicates,
        'categories_created': categories_created,
        'budgets_updated': budgets_updated,
        'total_book_budget': total_book_budget,
        'members_created': members_created,
        'book_month': f"{calendar.month_name[book_month]} {book_year}",
        'message': ' · '.join(summary_parts) + '.'
    }


@app.route('/api/import-book', methods=['POST'])
@login_required
def import_expense_book():
    """
    Smart Book & Ledger Importer endpoint.
    Supports .xlsx, .csv, .tsv, .txt, .pdf, images, OR raw pasted spreadsheet/CSV text.
    """
    file = request.files.get('book_file') or request.files.get('statement')
    pasted_text = (request.form.get('book_text') or '').strip()
    instruction = (request.form.get('instruction') or '').strip()

    apply_budgets = str(request.form.get('apply_budgets', 'true')).lower() in ('true', '1', 'yes', 'on')
    sync_members = str(request.form.get('sync_members', 'true')).lower() in ('true', '1', 'yes', 'on')
    skip_transfers = str(request.form.get('skip_transfers', 'true')).lower() in ('true', '1', 'yes', 'on')

    if (not file or file.filename == '') and not pasted_text:
        return jsonify({'error': 'Please upload an Expense Book (.xlsx, .csv, .pdf, image) or paste spreadsheet data.'}), 400

    try:
        import csv
        import io
        rows = []
        raw_text_for_ai = None
        base64_file = None
        mime_type = None

        if file and file.filename:
            fname = file.filename.lower()
            file_bytes = file.read()
            mime_type = file.mimetype or ''

            if fname.endswith('.xlsx') or file_bytes[:4] == b'PK\x03\x04':
                rows = parse_xlsx_rows(file_bytes)
            elif fname.endswith(('.csv', '.tsv', '.txt')) or 'csv' in mime_type or 'text' in mime_type:
                text_content = file_bytes.decode('utf-8-sig', errors='replace')
                raw_text_for_ai = text_content
                delimiter = '\t' if (fname.endswith('.tsv') or text_content.count('\t') > text_content.count(',')) else ','
                rows = list(csv.reader(io.StringIO(text_content), delimiter=delimiter))
            elif fname.endswith('.pdf') or mime_type == 'application/pdf':
                import PyPDF2
                pdf_reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
                raw_text_for_ai = "\n".join((page.extract_text() or '') for page in pdf_reader.pages)
                mime_type = 'application/pdf'
            else:
                base64_file = base64.b64encode(file_bytes).decode('utf-8')
                if not mime_type:
                    mime_type = 'image/jpeg'

        elif pasted_text:
            raw_text_for_ai = pasted_text
            first_line = pasted_text.splitlines()[0] if pasted_text.splitlines() else ''
            delimiter = '\t' if first_line.count('\t') > first_line.count(',') else ','
            rows = list(csv.reader(io.StringIO(pasted_text), delimiter=delimiter))

        parsed_data = extract_structured_book_data(rows) if rows else {'transactions': [], 'category_budgets': {}}

        # If structured parser found neither transactions nor category budgets, use Gemini AI extraction
        if not parsed_data.get('transactions') and not parsed_data.get('category_budgets'):
            parsed_data = extract_ai_book_data(
                raw_text_for_ai,
                base64_file,
                mime_type,
                instruction,
                current_user.family_id
            )

        if not parsed_data.get('transactions') and not parsed_data.get('category_budgets'):
            return jsonify({'success': False, 'message': 'No valid transactions or budget rows were found in the provided book.'})

        result = implement_parsed_book(
            parsed_data,
            family_id=current_user.family_id,
            default_user=current_user,
            apply_budgets=apply_budgets,
            sync_members=sync_members,
            skip_transfers=skip_transfers,
        )
        return jsonify(result)
    except Exception as e:
        db.session.rollback()
        app.logger.exception('Smart Book Import failed')
        return jsonify({'error': str(e)}), 500


@app.route('/api/scan-statement', methods=['POST'])
@login_required
def scan_statement():
    """Delegates to the unified Smart Book & Statement Importer so CSV, XLSX, PDF, and Images all work seamlessly."""
    return import_expense_book()


# ==============================================================================
# GOOGLE SHEETS 2-WAY LIVE SYNC BRIDGE
# ==============================================================================

@app.route('/api/sync/google-sheet', methods=['GET', 'POST'])
def api_sync_google_sheet():
    """
    Two-way live sync webhook receiver for Google Sheets.
    Called by Google Apps Script triggers (onEdit / onChange / manual Sync button).
    Supports token verification via query string ?token=... or request body.
    """
    token = request.args.get('token')
    payload = {}
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    elif request.form:
        payload = request.form.to_dict()

    if not token and isinstance(payload, dict):
        token = payload.get('token')

    if not token:
        return jsonify({'success': False, 'error': 'Missing authentication token.'}), 401

    family = Family.query.filter_by(google_sheet_sync_token=token).first()
    if not family:
        return jsonify({'success': False, 'error': 'Invalid or unrecognized sync token.'}), 403

    if request.method == 'GET':
        return jsonify({
            'success': True,
            'message': f'VaultSync Google Sheet live bridge connected for family: {family.name}',
            'family_id': family.id,
            'last_sync': family.last_sheet_sync_at.isoformat() if family.last_sheet_sync_at else None
        })

    try:
        raw_rows = payload.get('rows')
        transactions = payload.get('transactions')
        single_expense = payload.get('expense')

        default_user = family.users[0] if family.users else None
        if not default_user:
            return jsonify({'success': False, 'error': 'Family has no registered users.'}), 400

        parsed_data = {'transactions': [], 'category_budgets': {}}

        if raw_rows and isinstance(raw_rows, list):
            parsed_data = extract_structured_book_data(raw_rows)
        elif transactions and isinstance(transactions, list):
            parsed_data['transactions'] = transactions
        elif single_expense and isinstance(single_expense, dict):
            parsed_data['transactions'] = [single_expense]
        elif 'date' in payload and 'amount' in payload:
            parsed_data['transactions'] = [payload]

        if not parsed_data.get('transactions') and not parsed_data.get('category_budgets'):
            return jsonify({'success': True, 'message': 'No transaction rows received.', 'imported': 0, 'skipped_duplicates': 0})

        result = implement_parsed_book(
            parsed_data,
            family_id=family.id,
            default_user=default_user,
            apply_budgets=True,
            sync_members=True,
            skip_transfers=True
        )

        family.last_sheet_sync_at = datetime.utcnow()
        db.session.commit()

        return jsonify({
            'success': True,
            'imported': result['count'],
            'skipped_duplicates': result['skipped_duplicates'],
            'total_amount': result['total_amount'],
            'message': result['message']
        })
    except Exception as e:
        db.session.rollback()
        app.logger.exception('Google Sheet sync webhook error')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/sync/google-sheet/pull', methods=['POST'])
@login_required
def api_sync_google_sheet_pull():
    """
    Direct 1-Click Pull Sync: Fetches published or shared Google Sheet CSV,
    extracts all transactions and category budgets, and safely merges without duplicates.
    """
    family = current_user.family
    if not family:
        return jsonify({'success': False, 'error': 'Please join a family first.'}), 400

    sheet_url = (request.form.get('sheet_url') or family.google_sheet_url or '').strip()
    if not sheet_url:
        return jsonify({'success': False, 'error': 'Please provide a valid Google Sheet URL.'}), 400

    csv_url = extract_google_sheet_csv_url(sheet_url)
    if not csv_url:
        return jsonify({'success': False, 'error': 'Invalid Google Sheet URL. Please paste a link like https://docs.google.com/spreadsheets/d/.../edit'}), 400

    try:
        import urllib.request
        import csv
        import io

        req = urllib.request.Request(
            csv_url,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) VaultSync-GoogleSheetBridge/1.0'}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            content_bytes = resp.read()

        csv_text = content_bytes.decode('utf-8-sig', errors='replace')
        if not csv_text or '<html' in csv_text.lower()[:300]:
            return jsonify({
                'success': False,
                'error': 'Could not read Google Sheet as CSV. Please ensure your Google Sheet is shared with "Anyone with the link can view", or use File -> Share -> Publish to web.'
            }), 400

        rows = list(csv.reader(io.StringIO(csv_text)))
        parsed_data = extract_structured_book_data(rows) if rows else {'transactions': [], 'category_budgets': {}}

        if not parsed_data.get('transactions') and not parsed_data.get('category_budgets'):
            return jsonify({
                'success': False,
                'error': 'No readable transaction or category budget rows found in this sheet. Ensure column headers include Date, Item/Description, and Amount.'
            }), 400

        result = implement_parsed_book(
            parsed_data,
            family_id=family.id,
            default_user=current_user,
            apply_budgets=True,
            sync_members=True,
            skip_transfers=True
        )

        family.google_sheet_url = sheet_url
        family.last_sheet_sync_at = datetime.utcnow()
        db.session.commit()

        return jsonify({
            'success': True,
            'imported': result['count'],
            'skipped_duplicates': result['skipped_duplicates'],
            'total_amount': result['total_amount'],
            'message': result['message']
        })
    except Exception as e:
        db.session.rollback()
        app.logger.exception('Google Sheet pull sync failed')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/sync/google-sheet/settings', methods=['POST'])
@login_required
def api_sync_google_sheet_settings():
    """Update Google Sheet configuration URLs and regenerate sync tokens."""
    family = current_user.family
    if not family:
        return jsonify({'success': False, 'error': 'Please join a family first.'}), 400

    payload = request.get_json(silent=True) or request.form.to_dict()
    action = payload.get('action')

    if action == 'regenerate_token':
        family.google_sheet_sync_token = secrets.token_hex(16)
        db.session.commit()
        return jsonify({'success': True, 'token': family.google_sheet_sync_token, 'message': 'New sync token generated.'})

    sheet_url = payload.get('google_sheet_url')
    webhook_url = payload.get('google_sheet_webhook_url')

    if sheet_url is not None:
        family.google_sheet_url = sheet_url.strip()
    if webhook_url is not None:
        family.google_sheet_webhook_url = webhook_url.strip()

    db.session.commit()
    return jsonify({
        'success': True,
        'message': 'Google Sheet sync settings saved successfully!',
        'google_sheet_url': family.google_sheet_url,
        'google_sheet_webhook_url': family.google_sheet_webhook_url,
        'token': family.google_sheet_sync_token
    })


@app.route('/api/sync/google-sheet/script-template')
@login_required
def api_sync_google_sheet_script_template():
    """Generates ready-to-copy Google Apps Script code pre-filled with this family's token."""
    family = current_user.family
    if not family:
        return jsonify({'error': 'No family found'}), 400

    # Build server endpoint URL from request host
    server_root = request.host_url.rstrip('/')
    webhook_endpoint = f"{server_root}/api/sync/google-sheet"
    token = family.google_sheet_sync_token or 'YOUR_SECRET_TOKEN'

    script_code = f"""/**
 * ==============================================================================
 * VAULTSYNC 2-WAY LIVE SYNC SCRIPT
 * Family: {family.name}
 * ==============================================================================
 * INSTRUCTIONS:
 * 1. In your Google Sheet, click 'Extensions' -> 'Apps Script'
 * 2. Delete any existing code, paste this entire file, and click 'Save' (Ctrl+S)
 * 3. Click 'Deploy' -> 'New deployment' -> Select type 'Web app'
 *    - Description: VaultSync Bridge
 *    - Execute as: Me
 *    - Who has access: Anyone
 * 4. Click 'Deploy' and copy the resulting Web App URL.
 * 5. Paste the Web App URL into VaultSync under 'Google Sheets Live Sync' settings!
 * ==============================================================================
 */

const VAULTSYNC_WEBHOOK_URL = "{webhook_endpoint}";
const VAULTSYNC_SYNC_TOKEN = "{token}";

function onOpen() {{
  SpreadsheetApp.getUi()
    .createMenu('⚡ VaultSync')
    .addItem('🔄 Sync Sheet to VaultSync', 'syncSheetToVaultSync')
    .addItem('ℹ️ Test Connection', 'testConnection')
    .addToUi();
}}

// 1. RECEIVE FROM VAULTSYNC -> Append new expense to active Google Sheet
function doPost(e) {{
  try {{
    const data = JSON.parse(e.postData.contents);
    if (data.token !== VAULTSYNC_SYNC_TOKEN) {{
      return ContentService.createTextOutput(JSON.stringify({{error: "Unauthorized: Invalid token"}})).setMimeType(ContentService.MimeType.JSON);
    }}
    if (data.action === "add_expense" && data.expense) {{
      const exp = data.expense;
      const sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
      sheet.appendRow([
        exp.date,
        exp.item_name,
        exp.category || "Other",
        exp.amount,
        exp.member || "Family",
        exp.description || ""
      ]);
      return ContentService.createTextOutput(JSON.stringify({{success: true}})).setMimeType(ContentService.MimeType.JSON);
    }}
    return ContentService.createTextOutput(JSON.stringify({{success: false, message: "No action performed"}})).setMimeType(ContentService.MimeType.JSON);
  }} catch (err) {{
    return ContentService.createTextOutput(JSON.stringify({{error: err.message}})).setMimeType(ContentService.MimeType.JSON);
  }}
}}

// 2. PUSH FROM GOOGLE SHEET -> Sync all rows to VaultSync (deduplicated automatically)
function syncSheetToVaultSync() {{
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  const data = sheet.getDataRange().getValues();
  if (!data || data.length <= 1) {{
    SpreadsheetApp.getUi().alert("Your sheet is empty or contains only the header row.");
    return;
  }}
  const payload = {{
    token: VAULTSYNC_SYNC_TOKEN,
    rows: data
  }};
  const options = {{
    method: "post",
    contentType: "application/json",
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  }};
  try {{
    const response = UrlFetchApp.fetch(VAULTSYNC_WEBHOOK_URL + "?token=" + VAULTSYNC_SYNC_TOKEN, options);
    const result = JSON.parse(response.getContentText());
    if (result.success) {{
      SpreadsheetApp.getUi().alert("VaultSync Sync Successful!\\n\\n" + (result.message || "Ledger synchronized."));
    }} else {{
      SpreadsheetApp.getUi().alert("Sync Warning: " + (result.message || result.error));
    }}
  }} catch (err) {{
    SpreadsheetApp.getUi().alert("Failed to connect to VaultSync: " + err.message);
  }}
}}

function testConnection() {{
  try {{
    const response = UrlFetchApp.fetch(VAULTSYNC_WEBHOOK_URL + "?token=" + VAULTSYNC_SYNC_TOKEN, {{muteHttpExceptions: true}});
    const result = JSON.parse(response.getContentText());
    SpreadsheetApp.getUi().alert(result.success ? "Connected to VaultSync!\\n" + result.message : "Error: " + result.error);
  }} catch (err) {{
    SpreadsheetApp.getUi().alert("Connection failed: " + err.message);
  }}
}}
"""
    return jsonify({'success': True, 'code': script_code, 'token': token, 'webhook_url': webhook_endpoint})



if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        print("Database tables created successfully")

    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or os.environ.get('FLASK_DEBUG', '1') != '1':
        start_ai_organizer_scheduler()
        start_daily_report_scheduler()
        start_ai_plan_scheduler()

    app.run(
        host=os.environ.get('FLASK_RUN_HOST', '127.0.0.1'),
        port=int(os.environ.get('FLASK_RUN_PORT', '5000')),
        debug=os.environ.get('FLASK_DEBUG', '1') == '1'
    )
