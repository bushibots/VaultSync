from flask import Flask, render_template, request, redirect, jsonify, Response, flash, url_for
from flask_login import LoginManager, login_required, current_user, login_user, logout_user
from flask_migrate import Migrate
import calendar
import csv
import json
import os
import secrets
from io import StringIO
from datetime import date, datetime, timedelta
from sqlalchemy import func, inspect, text
from models import db, User, Category, Expense, ExpectedExpense, Family, BudgetSuggestion


app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_super_secret_static_key_here_use_this_for_production'
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'vaultsync.db')
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
    }

    added = []
    with db.engine.begin() as connection:
        for column_name, ddl in expected_expense_columns.items():
            if column_name in columns:
                continue
            connection.execute(text(f'ALTER TABLE expected_expense ADD COLUMN {ddl}'))
            added.append(column_name)

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

    # Math is now isolated to the selected month!
    total_spent = sum(exp.amount for exp in all_expenses)
    monthly_income = current_user.family.monthly_budget if current_user.family else 0
    projected_savings = monthly_income - total_spent

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

    expected_total = sum(exp.amount for exp in expected_expenses)
    expected_paid_total = sum(exp.amount for exp in expected_expenses if exp.is_paid)
    expected_unpaid_total = expected_total - expected_paid_total
    planned_after_budget = monthly_income - expected_total
    plan_paid_percentage = (expected_paid_total / expected_total * 100) if expected_total else 0

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
    except (ValueError, TypeError):
        flash('Please provide a valid expense amount and date.', 'error')
        return back_or('dashboard')

    new_expense = Expense(
        date=expense_date,
        item_name=item_name,
        amount=expense_amount,
        category_id=category.id,
        user_id=current_user.id,
        family_id=current_user.family_id
    )
    db.session.add(new_expense)
    db.session.commit()

    # Check budget after adding expense
    current_date = datetime.utcnow()
    month_str = f"{current_date.month:02d}"
    year_str = str(current_date.year)

    monthly_total = db.session.query(
        func.sum(Expense.amount).label('total')
    ).filter(
        func.strftime('%Y', Expense.date) == year_str,
        func.strftime('%m', Expense.date) == month_str,
        Expense.family_id == current_user.family_id
    ).scalar() or 0.0

    family = current_user.family
    if family.monthly_budget and monthly_total > family.monthly_budget:
        percentage = (monthly_total / family.monthly_budget) * 100
        flash(f'⚠️ Budget Alert: Your family has spent {percentage:.1f}% of the monthly budget!', 'warning')
    elif family.monthly_budget and monthly_total > (family.monthly_budget * 0.8):
        percentage = (monthly_total / family.monthly_budget) * 100
        flash(f'💡 You\'re at {percentage:.1f}% of your monthly budget.', 'info')

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

        category = Category.query.filter_by(
            id=category_id,
            family_id=current_user.family_id
        ).first()

        if not (name and amount and category and month and year and due_day):
            flash('Please provide every planned expense field.', 'error')
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
        writer.writerow(('Date', 'Spender', 'Item Name', 'Category', 'Amount'))
        for exp in expenses:
            writer.writerow((exp.date.strftime("%Y-%m-%d"), exp.user.username, exp.item_name, exp.category.name, exp.amount))
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

    app.run(debug=True)
