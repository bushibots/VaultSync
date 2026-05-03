from flask import Flask, render_template, request, redirect, jsonify, Response, flash, url_for
from flask_login import LoginManager, login_required, current_user, login_user, logout_user
from flask_migrate import Migrate
import csv
import os
from io import StringIO
from datetime import datetime, timedelta
from sqlalchemy import func
from models import db, User, Category, Expense, ExpectedExpense, Family


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

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

def back_or(endpoint):
    """Redirect back to the submitting page, falling back to a known route."""
    return redirect(request.referrer or url_for(endpoint))

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
    import calendar

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

    if not (name and amount and category_id and month and year):
        flash('Please provide name, amount, category, month and year.', 'error')
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
    except (ValueError, TypeError):
        flash('Please provide a valid amount, month, and year.', 'error')
        return back_or('dashboard')

    new_expected = ExpectedExpense(
        name=name.strip(),
        amount=expected_amount,
        category_id=category.id,
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
    ee.is_paid = not ee.is_paid
    db.session.commit()
    return back_or('dashboard')

@app.route('/add_category', methods=['POST'])
@login_required
def add_category():
    name = request.form.get('name', '').strip()
    color = request.form.get('color', '').strip()
    if not current_user.family_id:
        flash('Please join a family before adding categories.', 'error')
        return back_or('dashboard')

    # Check if category already exists in this family
    if name and color and not Category.query.filter_by(name=name, family_id=current_user.family_id).first():
        new_cat = Category(name=name, color=color, family_id=current_user.family_id)
        db.session.add(new_cat)
        db.session.commit()
    return back_or('dashboard')

@app.route('/edit_category/<int:id>', methods=['POST'])
@login_required
def edit_category(id):
    category = Category.query.filter_by(id=id, family_id=current_user.family_id).first_or_404()
    name = request.form.get('name', '').strip()
    color = request.form.get('color', '').strip()
    if not (name and color):
        flash('Please provide a category name and color.', 'error')
        return back_or('dashboard')
    
    # Check if new name already exists in this family (but allow keeping same name)
    existing = Category.query.filter_by(name=name, family_id=current_user.family_id).first()
    if existing and existing.id != id:
        flash('A category with that name already exists.', 'error')
        return back_or('dashboard')
    
    category.name = name
    category.color = color
    db.session.commit()
    return back_or('dashboard')

@app.route('/delete_category/<int:id>')
@login_required
def delete_category(id):
    category = Category.query.filter_by(id=id, family_id=current_user.family_id).first_or_404()
    # Check if category has expenses in this family
    expenses_count = Expense.query.filter_by(category_id=id, family_id=current_user.family_id).count()
    if expenses_count == 0:
        db.session.delete(category)
        db.session.commit()
    else:
        flash('Cannot delete a category that has expenses.', 'error')
    return back_or('dashboard')

@app.route('/delete_expense/<int:id>')
@login_required
def delete_expense(id):
    expense_to_delete = Expense.query.filter_by(
        id=id, 
        family_id=current_user.family_id
    ).first_or_404()
    db.session.delete(expense_to_delete)
    db.session.commit()
    return back_or('dashboard')

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

    return render_template('admin_panel.html',
                          user=current_user,
                          family=family,
                          family_members=family_members,
                          monthly_spending=monthly_spending,
                          remaining_budget=remaining_budget,
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
