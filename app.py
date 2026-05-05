from flask import Flask, render_template, request, redirect, jsonify, Response, flash, url_for
from flask_login import LoginManager, login_required, current_user, login_user, logout_user
from flask_migrate import Migrate
import calendar
import csv
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from io import StringIO
from datetime import date, datetime, timedelta
from sqlalchemy import func, inspect, text
from models import db, User, Category, Expense, ExpectedExpense, Family, BudgetSuggestion, AISavingsForecast


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
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(basedir, 'vaultsync.db')
)
db.init_app(app)
migrate = Migrate(app, db)

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'info'

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

GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash-lite')
AI_ORGANIZER_BATCH_SIZE = 25
_ai_organizer_scheduler_started = False


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

    added = []
    with db.engine.begin() as connection:
        family_columns = {
            column['name']
            for column in inspector.get_columns('family')
        } if inspector.has_table('family') else set()
        if 'ai_notes' not in family_columns:
            connection.execute(text('ALTER TABLE family ADD COLUMN ai_notes TEXT'))
            added.append('family.ai_notes')
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

    if added:
        print(f"Added missing expected_expense columns: {', '.join(added)}")
    else:
        print('Schema already has the expected_expense budget-plan columns.')


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

def back_or(endpoint):
    """Redirect back to the submitting page, falling back to a known route."""
    return redirect(request.referrer or url_for(endpoint))


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
            'monthly_budget': float(current_user.family.monthly_budget or 0),
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
            'recommended_monthly_budget should be the ideal family monthly budget for the next cycle.',
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
            'recommended_monthly_budget': float(current_user.family.monthly_budget or 0),
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

    monthly_budget = float(current_user.family.monthly_budget or 0)
    suggested_monthly_budget = float(payload.get('recommended_monthly_budget') or 0)

    return {
        'payload': payload,
        'current_plan': current_plan,
        'current_total': round(current_total, 2),
        'suggested_total': round(suggested_total, 2),
        'plan_delta': round(suggested_total - current_total, 2),
        'current_monthly_budget': round(monthly_budget, 2),
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


def call_gemini_json(prompt, response_schema):
    api_key = os.environ.get('GEMINI_API_KEY', '').strip()
    if not api_key:
        raise ValueError('GEMINI_API_KEY is not set on the server.')

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
        with urllib.request.urlopen(request_obj, timeout=60) as response:
            response_payload = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode('utf-8', errors='replace')
        raise ValueError(f'Gemini API error {exc.code}: {error_body[:500]}') from exc
    except urllib.error.URLError as exc:
        raise ValueError(f'Could not reach Gemini API: {exc.reason}') from exc

    return parse_gemini_json_text(response_payload)


def build_expense_organization_prompt(expenses, categories):
    expense_rows = [
        {
            'expense_id': expense.id,
            'date': expense.date.isoformat(),
            'item_name': expense.item_name,
            'description': expense.description or '',
            'amount': round(float(expense.amount), 2),
            'current_category': expense.category.name if expense.category else '',
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
        'Never invent a category. If uncertain, choose the closest existing category and use lower confidence.\n\n'
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
    reasons = []

    for batch in chunks(expenses, AI_ORGANIZER_BATCH_SIZE):
        prompt = build_expense_organization_prompt(batch, categories)
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
                    f"({classification.get('reason', '').strip()})"
                )

    db.session.commit()
    return {
        'scanned': len(expenses),
        'changed': changed,
        'unchanged': unchanged,
        'skipped': skipped,
        'message': '; '.join(reasons)
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
    
    # BACKEND-FIRST MATH: Calculate totals directly in Python
    current_spent = round(sum(float(expense.amount) for expense in expenses), 2)
    simple_pace_total = round((current_spent / elapsed_days) * days_in_month, 2) if elapsed_days else current_spent
    
    # Backend-First Math: Filter ONLY unpaid ExpectedExpense items (is_paid == False)
    # Paid items are already recorded in Expense table, so we exclude them to avoid double-counting
    unpaid_expected = [
        item for item in expected_items
        if not item.is_paid  # Strictly filter: only send future obligations
    ]
    
    # BACKEND-FIRST MATH: Pre-calculate total unpaid expected expenses
    total_unpaid_expected = round(
        sum(float(expected_budget_amount(item)) for item in unpaid_expected),
        2
    )
    
    # BACKEND-FIRST MATH: Calculate savings in Python, not in AI
    monthly_budget = float(family.monthly_budget or 0)
    calculated_savings = round(monthly_budget - (current_spent + total_unpaid_expected), 2)
    
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
            },
            'instructions_for_ai': [
                'Analyze family spending patterns, forecast category spend, and provide strategic recommendations.',
                'CRITICAL: The exact calculated remaining savings for this month is Rs {calculated_savings}. The total spent so far is Rs {current_spent}. The total unpaid expected expenses are Rs {total_unpaid_expected}.',
                'You must treat these numbers as absolute facts. Do NOT recalculate them, and do NOT output any other numbers for expected_savings.',
                'Your job is to explain spending patterns and provide smart category forecasts, not to recalculate totals.',
                'Use actual expenses, unpaid planned items, bucket reserves, category limits, and admin notes to inform your analysis.',
                'Admin notes are important rules. Follow them when reasonable and mention how they affected your recommendations.',
                'Do not invent category names. Use only the provided categories in category_forecasts.',
            ]
        }
    }


def build_savings_forecast_prompt(context_payload):
    backend_values = context_payload.get('backend_calculated_values', {})
    calculated_savings = backend_values.get('calculated_savings', 0)
    total_actual_spent = backend_values.get('total_actual_spent', 0)
    total_unpaid_expected = backend_values.get('total_unpaid_expected', 0)
    
    return (
        'You are VaultSync savings forecast AI.\n'
        'Analyze this family budget data and provide category forecasts and strategic recommendations.\n'
        'IMPORTANT: You must accept and use the backend-calculated values as absolute facts.\n\n'
        f'{json.dumps(context_payload, ensure_ascii=False)}\n\n'
        f'HARDCODED FACTS FOR THIS FORECAST:\n'
        f'- Exact calculated remaining savings: Rs {calculated_savings}\n'
        f'- Total actual spent to date: Rs {total_actual_spent}\n'
        f'- Total unpaid expected expenses: Rs {total_unpaid_expected}\n'
        f'\n'
        f'You MUST return these exact values in your response. Do NOT recalculate them.\n'
        f'Your analysis should focus on spending patterns, risks, and recommended actions.\n'
        f'Return only JSON matching the schema. No markdown, no prose outside JSON.'
    )


def normalize_savings_forecast_payload(payload, context):
    month_end = context['month_end']
    current_spent = context['current_spent']
    monthly_budget = float(context['family'].monthly_budget or 0)
    
    # Backend-First Math: Use the pre-calculated savings value
    backend_calculated_savings = context['calculated_savings']

    try:
        predicted_total = float(payload.get('predicted_total_spend'))
    except (TypeError, ValueError):
        predicted_total = context['simple_pace_total']

    predicted_total = max(predicted_total, current_spent)
    try:
        predicted_additional = float(payload.get('predicted_additional_spend'))
    except (TypeError, ValueError):
        predicted_additional = predicted_total - current_spent
    predicted_additional = max(predicted_additional, 0.0)

    try:
        expected_savings = float(payload.get('expected_savings'))
    except (TypeError, ValueError):
        # If AI didn't provide expected_savings, use backend calculation
        expected_savings = backend_calculated_savings
    
    # Backend-First Math: Ensure we use the correct pre-calculated savings
    expected_savings = backend_calculated_savings

    try:
        confidence_score = float(payload.get('confidence_score'))
    except (TypeError, ValueError):
        confidence_score = 0.5
    confidence_score = min(max(confidence_score, 0.0), 1.0)

    confidence_level = str(payload.get('confidence_level') or 'medium').strip().lower()
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
    normalized_category_forecasts = []
    for item in payload.get('category_forecasts') or []:
        if not isinstance(item, dict):
            continue
        category_name = str(item.get('category') or '').strip()
        if category_name not in category_names:
            continue
        try:
            current_category_spent = float(item.get('current_spent') or 0)
        except (TypeError, ValueError):
            current_category_spent = 0.0
        try:
            predicted_category_spend = float(item.get('predicted_month_end_spend') or 0)
        except (TypeError, ValueError):
            predicted_category_spend = current_category_spent
        normalized_category_forecasts.append({
            'category': category_name,
            'current_spent': round(max(current_category_spent, 0.0), 2),
            'predicted_month_end_spend': round(max(predicted_category_spend, 0.0), 2),
            'note': str(item.get('note') or '').strip()[:240],
        })
    payload['category_forecasts'] = normalized_category_forecasts
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
        return json.loads(forecast.raw_json)
    except (TypeError, ValueError):
        return {}


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
    if not os.environ.get('GEMINI_API_KEY', '').strip():
        return

    _ai_organizer_scheduler_started = True
    thread = threading.Thread(target=ai_organizer_scheduler_loop, daemon=True)
    thread.start()


if os.environ.get('RUN_AI_ORGANIZER_SCHEDULER') == '1':
    start_ai_organizer_scheduler()


@app.before_request
def ensure_ai_organizer_scheduler_running():
    start_ai_organizer_scheduler()

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
            family = Family(name=family_name, monthly_budget=initial_budget)
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
    monthly_income = current_user.family.monthly_budget if current_user.family else 0

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
    planned_after_budget = monthly_income - expected_total
    standard_expected_total = expected_paid_total + expected_unpaid_total
    plan_paid_percentage = (expected_paid_total / standard_expected_total * 100) if standard_expected_total else 0

    return render_template('dashboard.html',
                            user=current_user,
                            expenses=all_expenses,
                            categories=all_categories,
                            expected_expenses=expected_expenses,
                            total_spent=total_spent,
                            income=monthly_income,
                            projected=projected_savings,
                            chart_labels=chart_labels,
                            chart_data=chart_data,
                            chart_colors=chart_colors,
                            category_spent=totals_map,
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

    return render_template(
        'daily_diary.html',
        user=current_user,
        expenses=expenses,
        categories=categories,
        daily_total=daily_total,
        selected_date=selected_date,
        today=today,
        previous_date=selected_date - timedelta(days=1),
        next_date=selected_date + timedelta(days=1)
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

    if family.monthly_budget and protected_total > family.monthly_budget:
        percentage = (protected_total / family.monthly_budget) * 100
        flash(f'Budget Alert: Your family has committed {percentage:.1f}% of the monthly budget.', 'warning')
    elif family.monthly_budget and protected_total > (family.monthly_budget * 0.8):
        percentage = (protected_total / family.monthly_budget) * 100
        flash(f"You are at {percentage:.1f}% of your monthly budget after reserved buckets.", 'info')

    # Send them back to wherever they submitted the form from
    return back_or('dashboard')


@app.route('/add_expected', methods=['POST'])
@login_required
def add_expected():
    # Admin-only access
    if not current_user.is_authenticated or current_user.user_type != 'family_manager':
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
    if not current_user.is_authenticated or current_user.user_type != 'family_manager':
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
        if current_user.user_type == 'family_manager' and user_id:
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
    if not current_user.is_authenticated or current_user.user_type != 'family_manager':
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
    if not current_user.is_authenticated or current_user.user_type != 'family_manager':
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
    if not current_user.is_authenticated or current_user.user_type != 'family_manager':
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

    if not (name and color):
        flash('Please provide a category name and color.', 'error')
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

    new_cat = Category(
        name=name,
        color=color,
        monthly_limit=monthly_limit,
        family_id=current_user.family_id
    )
    db.session.add(new_cat)
    db.session.commit()
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
    if not current_user.is_authenticated or current_user.user_type != 'family_manager':
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

        db.session.add(ExpectedExpense(
            name=name,
            amount=amount,
            due_day=item_date.day,
            category_id=category.id,
            month=item_date.month,
            year=item_date.year,
            family_id=current_user.family_id
        ))
        created_items += 1

    if request.form.get('update_monthly_budget') == 'yes' and suggestion.suggested_monthly_budget:
        current_user.family.monthly_budget = suggestion.suggested_monthly_budget

    suggestion.is_applied = True
    db.session.commit()
    flash(
        f'Applied {created_items} suggested planned items. '
        f'{skipped_items} duplicates skipped. {created_categories} new categories added.',
        'success'
    )
    return redirect(url_for(
        'dashboard',
        month=suggestion.target_start_date.month,
        year=suggestion.target_start_date.year
    ))

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
    if not current_user.is_authenticated or current_user.user_type != 'family_manager':
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

    remaining_budget = family.monthly_budget - monthly_spending if family.monthly_budget else 0

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

    return render_template('admin_panel.html',
                          user=current_user,
                          family=family,
                          family_members=family_members,
                          member_stats=member_stats,
                          categories=categories,
                          category_spending=category_spending,
                          monthly_spending=monthly_spending,
                          remaining_budget=remaining_budget,
                          planned_total=float(planned_total),
                          planned_paid_total=float(planned_paid_total),
                          planned_unpaid_total=float(planned_total - planned_paid_total),
                          ai_suggestions_count=ai_suggestions_count,
                          latest_savings_forecast=latest_savings_forecast,
                          latest_savings_payload=latest_savings_payload,
                          savings_chart_data=savings_chart_data,
                          ai_notes=family.ai_notes or '',
                          ai_organizer_configured=bool(os.environ.get('GEMINI_API_KEY', '').strip()),
                          ai_organizer_model=GEMINI_MODEL,
                          today=current_date.date(),
                          current_month_start=current_month_start,
                          current_month_end=current_month_end,
                          budget_percentage=(monthly_spending / family.monthly_budget * 100) if family.monthly_budget else 0,
                          year_range=range(2024, datetime.utcnow().year + 3))


@app.route('/admin_panel/budget', methods=['POST'])
@login_required
def update_budget():
    """Update family monthly budget"""
    if current_user.user_type != 'family_manager':
        flash('Unauthorized', 'error')
        return redirect(url_for('dashboard'))

    budget = request.form.get('monthly_budget')
    try:
        budget = float(budget)
        if budget < 1000:
            flash('Budget must be at least 1000.', 'error')
            return redirect(url_for('admin_panel'))

        current_user.family.monthly_budget = budget
        db.session.commit()
        flash('Family budget updated successfully!', 'success')
    except (ValueError, TypeError):
        flash('Please enter a valid budget amount.', 'error')

    return redirect(url_for('admin_panel'))


@app.route('/admin_panel/ai_organize', methods=['POST'])
@login_required
def admin_ai_organize_expenses():
    if current_user.user_type != 'family_manager':
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
    if current_user.user_type != 'family_manager':
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


@app.route('/admin_panel/categories/<int:category_id>/limit', methods=['POST'])
@login_required
def update_category_limit(category_id):
    if current_user.user_type != 'family_manager':
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
    if current_user.user_type != 'family_manager':
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
    if current_user.user_type != 'family_manager':
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
    if current_user.user_type != 'family_manager':
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
    if current_user.user_type != 'family_manager':
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
    if current_user.user_type != 'family_manager':
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
    if current_user.user_type != 'family_manager':
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
    if current_user.user_type != 'family_manager':
        flash('Unauthorized', 'error')
        return redirect(url_for('dashboard'))

    family_id = current_user.family_id

    # Delete all expenses, categories, expected expenses
    Expense.query.filter_by(family_id=family_id).delete()
    Category.query.filter_by(family_id=family_id).delete()
    ExpectedExpense.query.filter_by(family_id=family_id).delete()
    BudgetSuggestion.query.filter_by(family_id=family_id).delete()
    AISavingsForecast.query.filter_by(family_id=family_id).delete()

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
    if current_user.user_type != 'family_manager':
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
    if current_user.user_type != 'family_manager':
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

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        print("Database tables created successfully")

    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        start_ai_organizer_scheduler()

    app.run(
        host=os.environ.get('FLASK_RUN_HOST', '127.0.0.1'),
        port=int(os.environ.get('FLASK_RUN_PORT', '5000')),
        debug=os.environ.get('FLASK_DEBUG', '1') == '1'
    )
