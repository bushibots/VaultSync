from flask import Flask, render_template, request, redirect, jsonify, Response, flash, url_for
from flask_login import LoginManager, login_required, current_user, login_user, logout_user
from flask_migrate import Migrate
import csv
from io import StringIO
from datetime import datetime
from sqlalchemy import func
from models import db, User, Category, Expense, ExpectedExpense, Family


app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here-change-in-production'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///vaultsync.db'
db.init_app(app)
migrate = Migrate(app, db)

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'info'

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# --- AUTH ROUTES ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    """User registration"""
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        password_confirm = request.form.get('password_confirm', '')
        
        # Validation
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
        
        # Create a new family for this user
        family = Family(name=f"{username}'s Family")
        user = User(username=username, email=email, role='member', family=family)
        user.set_password(password)
        db.session.add(family)
        db.session.add(user)
        db.session.commit()
        
        flash('Registration successful! Please log in.', 'success')
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
    monthly_income = 170000
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
                            month_name=calendar.month_name[selected_month])


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
    item_name = request.form.get('item_name')
    amount = request.form.get('amount')
    category_id = request.form.get('category_id')
    expense_date_str = request.form.get('expense_date')

    # Convert the date string from the form into a Python Date object
    if expense_date_str:
        expense_date = datetime.strptime(expense_date_str, '%Y-%m-%d').date()
    else:
        expense_date = datetime.utcnow().date()
    
    new_expense = Expense(
        date=expense_date, 
        item_name=item_name, 
        amount=float(amount), 
        category_id=int(category_id), 
        user_id=current_user.id,
        family_id=current_user.family_id
    )
    db.session.add(new_expense)
    db.session.commit()
    
    # Send them back to wherever they submitted the form from
    return redirect(request.referrer)


@app.route('/add_expected', methods=['POST'])
@login_required
def add_expected():
    # Admin-only access
    if not current_user.is_authenticated or (hasattr(current_user, 'role') and current_user.role != 'admin'):
        flash('Unauthorized: admin only', 'error')
        return redirect(request.referrer)

    name = request.form.get('name')
    amount = request.form.get('amount')
    category_id = request.form.get('category_id')
    month = request.form.get('month')
    year = request.form.get('year')

    if not (name and amount and category_id and month and year):
        flash('Please provide name, amount, category, month and year.', 'error')
        return redirect(request.referrer)

    new_expected = ExpectedExpense(
        name=name.strip(),
        amount=float(amount),
        category_id=int(category_id),
        month=int(month),
        year=int(year),
        family_id=current_user.family_id
    )
    db.session.add(new_expected)
    db.session.commit()
    return redirect(request.referrer)


@app.route('/toggle_expected/<int:id>')
@login_required
def toggle_expected(id):
    if not current_user.is_authenticated or (hasattr(current_user, 'role') and current_user.role != 'admin'):
        flash('Unauthorized: admin only', 'error')
        return redirect(request.referrer)
    ee = ExpectedExpense.query.filter_by(
        id=id, 
        family_id=current_user.family_id
    ).first_or_404()
    ee.is_paid = not ee.is_paid
    db.session.commit()
    return redirect(request.referrer)

@app.route('/add_category', methods=['POST'])
@login_required
def add_category():
    name = request.form.get('name')
    color = request.form.get('color')
    # Check if category already exists in this family
    if not Category.query.filter_by(name=name, family_id=current_user.family_id).first():
        new_cat = Category(name=name, color=color, family_id=current_user.family_id)
        db.session.add(new_cat)
        db.session.commit()
    return redirect(request.referrer)

@app.route('/edit_category/<int:id>', methods=['POST'])
@login_required
def edit_category(id):
    category = Category.query.filter_by(id=id, family_id=current_user.family_id).first_or_404()
    name = request.form.get('name')
    color = request.form.get('color')
    
    # Check if new name already exists in this family (but allow keeping same name)
    existing = Category.query.filter_by(name=name, family_id=current_user.family_id).first()
    if existing and existing.id != id:
        return redirect(request.referrer)
    
    category.name = name
    category.color = color
    db.session.commit()
    return redirect(request.referrer)

@app.route('/delete_category/<int:id>')
@login_required
def delete_category(id):
    category = Category.query.filter_by(id=id, family_id=current_user.family_id).first_or_404()
    # Check if category has expenses in this family
    expenses_count = Expense.query.filter_by(category_id=id, family_id=current_user.family_id).count()
    if expenses_count == 0:
        db.session.delete(category)
        db.session.commit()
    return redirect(request.referrer)

@app.route('/delete_expense/<int:id>')
@login_required
def delete_expense(id):
    expense_to_delete = Expense.query.filter_by(
        id=id, 
        family_id=current_user.family_id
    ).first_or_404()
    db.session.delete(expense_to_delete)
    db.session.commit()
    return redirect(request.referrer)

@app.route('/export_ai_data')
@login_required
def export_ai_data():
    expenses = Expense.query.filter_by(family_id=current_user.family_id).all()
    def generate():
        data = StringIO()
        writer = csv.writer(data)
        writer.writerow(('Date', 'Spender', 'Item Name', 'Category', 'Amount'))
        for exp in expenses:
            writer.writerow((exp.date.strftime("%Y-%m-%d"), exp.spender.username, exp.category.name, exp.amount))
            yield data.getvalue()
            data.seek(0)
            data.truncate(0)
    return Response(generate(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=family_data.csv'})

@app.route('/admin_panel', methods=['GET', 'POST'])
@login_required
def admin_panel():
    """Admin panel for family management"""
    if not current_user.is_authenticated or current_user.role != 'admin':
        flash('Unauthorized: admin only', 'error')
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        member_username = request.form.get('member_username', '').strip()
        
        # Find user by username in the same family
        member = User.query.filter_by(username=member_username, family_id=current_user.family_id).first()
        if not member:
            flash(f'User "{member_username}" not found in your family.', 'error')
        else:
            flash(f'User "{member_username}" is already in your family.', 'info')
        
        return redirect(url_for('admin_panel'))
    
    # Fetch all members in this family
    family_members = User.query.filter_by(family_id=current_user.family_id).all()
    
    return render_template('admin_panel.html', 
                          user=current_user,
                          family_members=family_members,
                          invite_code=current_user.family.invite_code)

if __name__ == '__main__':
    with app.app_context():
        # Drop all tables and recreate (for development - removes old schema)
        db.drop_all()
        db.create_all()
        
        # Create a Family
        family = Family(name='Demo Family')
        db.session.add(family)
        db.session.flush()  # Flush to get family.id
        
        # Seed default Users (with hashed passwords)
        dad = User(username='Dad', email='dad@family.com', role='admin', family_id=family.id)
        dad.set_password('password123')
        
        arish = User(username='Mohd Arish', email='arish@family.com', role='member', family_id=family.id)
        arish.set_password('password123')
        
        db.session.add(dad)
        db.session.add(arish)
        db.session.commit()
        
        # Seed default Categories for the family
        db.session.add_all([
            Category(name='Housing & Utilities', color='#3b82f6', family_id=family.id),
            Category(name='Groceries & Food', color='#10b981', family_id=family.id),
            Category(name='Healthcare', color='#ef4444', family_id=family.id),
            Category(name='Transportation', color='#f59e0b', family_id=family.id),
            Category(name='Debt Repayment', color='#8b5cf6', family_id=family.id),
            Category(name='Miscellaneous', color='#64748b', family_id=family.id)
        ])
        db.session.commit()
        
        print("✓ Database initialized with demo family and users")
        print("  Family Name: Demo Family")
        print("  Family Invite Code:", family.invite_code)
        print("  Username: Dad (admin) / Mohd Arish (member)")
        print("  Password: password123")
    
    app.run(debug=True)