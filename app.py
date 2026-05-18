from flask import Flask, render_template, request, redirect, jsonify, Response, flash, url_for
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

app = Flask(__name__)
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


def build_budget_bucket_summaries(buckets, family_id, month_str, year_str):
    bucket_ids = [bucket.id for bucket in buckets]
    spent_map = {}
    if bucket_ids:
        spent_rows = db.session.query(
            Expense.bucket_id,
            func.coalesce(func.sum(Expense.amount), 0).label('total')
        ).filter(
            Expense.bucket_id.in_(bucket_ids),
            Expense.family_id == family_id,
            func.strftime('%Y', Expense.date) == year_str,
            func.strftime('%m', Expense.date) == month_str
        ).group_by(Expense.bucket_id).all()
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
        if 'monthly_income' not in family_columns:
            with db.engine.begin() as connection:
                connection.execute(text('ALTER TABLE family ADD COLUMN monthly_income FLOAT'))
                connection.execute(text('UPDATE family SET monthly_income = monthly_budget WHERE monthly_income IS NULL'))

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
def dashboard():
    if not current_user.is_authenticated:
        return redirect(url_for('login'))

    # --- NEW FILTERING LOGIC ---
    if current_user.family_id:
        created_presets = ensure_preset_categories(current_user.family_id)
        if created_presets:
            db.session.commit()

    # Get month/year from URL args, default to current date if none provided
    current_date = datetime.utcnow()
    selected_month = request.args.get('month', current_date.month, type=int)
    selected_year = request.args.get('year', current_date.year, type=int)

    # Validate month is within 1-12
    if not (1 <= selected_month <= 12):
        selected_month = current_date.month
    if selected_year < 2020 or selected_year > 2050:
        selected_year = current_date.year

    # Normalize strings for SQLite strftime comparisons
    month_str = f"{selected_month:02d}"
    year_str = str(selected_year)

    # Filter expenses by the selected month, year, AND family
    all_expenses = Expense.query.filter(
        func.strftime('%Y', Expense.date) == year_str,
        func.strftime('%m', Expense.date) == month_str,
        Expense.family_id == current_user.family_id
    ).order_by(Expense.date.desc()).all()

    all_categories = Category.query.filter_by(family_id=current_user.family_id).all()

    # Math is now isolated to the selected month.
    total_spent = sum(exp.amount for exp in all_expenses)
    finance = monthly_finance_for(current_user.family, selected_month, selected_year)
    monthly_income = finance['monthly_income']
    monthly_budget = finance['monthly_budget']

    # Compute per-category totals using a grouped query to ensure accuracy
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
            
    # Fetch expected expenses for the selected month/year and family
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
                            month_name=calendar.month_name[selected_month],
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
    if not is_family_manager(current_user):
        flash('Unauthorized: family manager only', 'error')
        return redirect(url_for('financial_reports'))
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
        return redirect(url_for('financial_reports', month=selected_month, year=selected_year))
    except Exception as exc:
        db.session.rollback()
        app.logger.exception('AI suggested plan failed')
        flash(f'AI suggested plan failed unexpectedly: {exc}', 'error')
        return redirect(url_for('financial_reports', month=selected_month, year=selected_year))

    flash(f'AI suggested plan saved: Rs. {suggestion.total_planned:,.0f} across the selected month.', 'success')
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

    if selected_date_str:
        try:
            selected_date = datetime.strptime(selected_date_str, '%Y-%m-%d').date()
        except ValueError:
            selected_date = today
            flash('Invalid date selected. Showing today instead.', 'info')
    else:
        selected_date = today

    expenses = Expense.query.filter_by(
        date=selected_date,
        family_id=current_user.family_id
    ).order_by(Expense.id.asc()).all()

    categories = Category.query.filter_by(
        family_id=current_user.family_id
    ).order_by(Category.name.asc()).all()

    daily_total = sum(exp.amount for exp in expenses)
    quick_dates = [
        {'label': 'Yesterday', 'date': today - timedelta(days=1)},
        {'label': 'Today', 'date': today},
        {'label': 'Tomorrow', 'date': today + timedelta(days=1)},
        {'label': 'Selected', 'date': selected_date},
    ]

    return render_template(
        'daily_diary.html',
        user=current_user,
        expenses=expenses,
        categories=categories,
        daily_total=daily_total,
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
    all_categories = Category.query.filter_by(family_id=current_user.family_id).all()
    # Fetch ONLY the logged-in user's expenses in their family
    my_expenses = Expense.query.filter_by(
        user_id=current_user.id, 
        family_id=current_user.family_id
    ).order_by(Expense.date.desc()).all()
    
    my_total = sum(exp.amount for exp in my_expenses)

    return render_template('my_spendings.html', 
                           user=current_user,
                           expenses=my_expenses,
                           categories=all_categories,
                           total_spent=my_total)

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


@app.route('/toggle_expected/<int:id>')
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
    return Response(generate(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=family_data.csv'})

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


@app.route('/admin_panel/reports', methods=['GET'])
@login_required
def reports():
    """Analytics and reports for family spending"""
    if not is_family_manager(current_user):
        flash('Unauthorized: family manager only', 'error')
        return redirect(url_for('dashboard'))

    import calendar

    family = current_user.family
    current_date = datetime.utcnow()

    # Get last 6 months of data for trends
    months_data = []
    for i in range(5, -1, -1):
        date = current_date - timedelta(days=30*i)
        month = date.month
        year = date.year
        month_str = f"{month:02d}"
        year_str = str(year)

        total = db.session.query(
            func.sum(Expense.amount).label('total')
        ).filter(
            func.strftime('%Y', Expense.date) == year_str,
            func.strftime('%m', Expense.date) == month_str,
            Expense.family_id == family.id
        ).scalar() or 0.0

        months_data.append({
            'month': calendar.month_abbr[month],
            'year': year,
            'amount': float(total)
        })

    # Get current month category breakdown
    month_str = f"{current_date.month:02d}"
    year_str = str(current_date.year)

    category_totals = db.session.query(
        Category.name,
        Category.monthly_limit,
        func.sum(Expense.amount).label('spent')
    ).join(Expense, Expense.category_id == Category.id).filter(
        func.strftime('%Y', Expense.date) == year_str,
        func.strftime('%m', Expense.date) == month_str,
        Expense.family_id == family.id
    ).group_by(Category.id).all()

    categories_data = []
    for cat_name, cat_limit, spent in category_totals:
        categories_data.append({
            'name': cat_name,
            'spent': float(spent),
            'limit': float(cat_limit) if cat_limit else None
        })

    # Member contribution
    member_totals = db.session.query(
        User.username,
        func.sum(Expense.amount).label('total')
    ).join(Expense, Expense.user_id == User.id).filter(
        func.strftime('%Y', Expense.date) == year_str,
        func.strftime('%m', Expense.date) == month_str,
        Expense.family_id == family.id
    ).group_by(User.id).all()

    members_data = [
        {'name': username, 'amount': float(total)}
        for username, total in member_totals
    ]

    # Current month stats
    current_month_total = sum(cat['spent'] for cat in categories_data)
    budget_percentage = (current_month_total / family.monthly_budget * 100) if family.monthly_budget else 0

    return render_template('reports.html',
                          user=current_user,
                          family=family,
                          months_data=months_data,
                          categories_data=categories_data,
                          members_data=members_data,
                          current_month_total=current_month_total,
                          budget_percentage=budget_percentage,
                          month_name=calendar.month_name[current_date.month],
                          year=current_date.year)


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
        date = current_date - timedelta(days=30*i)
        month = date.month
        year = date.year
        month_str = f"{month:02d}"
        year_str = str(year)

        total = db.session.query(
            func.sum(Expense.amount).label('total')
        ).filter(
            func.strftime('%Y', Expense.date) == year_str,
            func.strftime('%m', Expense.date) == month_str,
            Expense.family_id == family.id
        ).scalar() or 0.0

        import calendar
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
