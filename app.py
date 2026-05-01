from flask import Flask, render_template, request, redirect, jsonify, Response, flash, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin, LoginManager, login_required, current_user, login_user, logout_user
from werkzeug.security import generate_password_hash, check_password_hash
import csv
from io import StringIO
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here-change-in-production'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///vaultsync.db'
db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'info'

# --- DATABASE MODELS ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default='Member')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def set_password(self, password):
        """Hash and set the user's password"""
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        """Check if the provided password matches the hash"""
        return check_password_hash(self.password_hash, password) 

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    color = db.Column(db.String(20), nullable=False) 

class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    item_name = db.Column(db.String(100), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    category = db.relationship('Category')
    
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    spender = db.relationship('User')

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
        
        # Create new user
        user = User(username=username, email=email, role='Member')
        user.set_password(password)
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
    from sqlalchemy import extract
    
    # Get month/year from URL args, default to current date if none provided
    current_date = datetime.utcnow()
    selected_month = request.args.get('month', current_date.month, type=int)
    selected_year = request.args.get('year', current_date.year, type=int)
    
    # Filter expenses by the selected month and year
    all_expenses = Expense.query.filter(
        extract('year', Expense.date) == selected_year,
        extract('month', Expense.date) == selected_month
    ).order_by(Expense.date.desc()).all()
    
    all_categories = Category.query.all()
    
    # Math is now isolated to the selected month!
    total_spent = sum(exp.amount for exp in all_expenses)
    monthly_income = 170000 
    projected_savings = monthly_income - total_spent
    
    chart_labels = []
    chart_data = []
    chart_colors = []
    
    for cat in all_categories:
        cat_total = sum(exp.amount for exp in all_expenses if exp.category_id == cat.id)
        if cat_total > 0:
            chart_labels.append(cat.name)
            chart_data.append(cat_total)
            chart_colors.append(cat.color)
            
    return render_template('dashboard.html', 
                            user=current_user,
                            expenses=all_expenses,
                            categories=all_categories,
                            total_spent=total_spent,
                            income=monthly_income,
                            projected=projected_savings,
                            chart_labels=chart_labels,
                            chart_data=chart_data,
                            chart_colors=chart_colors,
                            selected_month=selected_month,
                            selected_year=selected_year,
                            month_name=calendar.month_name[selected_month])

# --- NEW ROUTE: PERSONAL SPENDING TAB ---
@app.route('/my_spendings')
@login_required
def my_spendings():
    all_categories = Category.query.all()
    # Fetch ONLY the logged-in user's expenses
    my_expenses = Expense.query.filter_by(user_id=current_user.id).order_by(Expense.date.desc()).all()
    
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
        user_id=current_user.id
    )
    db.session.add(new_expense)
    db.session.commit()
    
    # Send them back to wherever they submitted the form from
    return redirect(request.referrer)

@app.route('/add_category', methods=['POST'])
@login_required
def add_category():
    name = request.form.get('name')
    color = request.form.get('color')
    if not Category.query.filter_by(name=name).first():
        new_cat = Category(name=name, color=color)
        db.session.add(new_cat)
        db.session.commit()
    return redirect(request.referrer)

@app.route('/edit_category/<int:id>', methods=['POST'])
@login_required
def edit_category(id):
    category = Category.query.get_or_404(id)
    name = request.form.get('name')
    color = request.form.get('color')
    
    # Check if new name already exists (but allow keeping same name)
    existing = Category.query.filter_by(name=name).first()
    if existing and existing.id != id:
        return redirect(request.referrer)
    
    category.name = name
    category.color = color
    db.session.commit()
    return redirect(request.referrer)

@app.route('/delete_category/<int:id>')
@login_required
def delete_category(id):
    category = Category.query.get_or_404(id)
    # Check if category has expenses
    expenses_count = Expense.query.filter_by(category_id=id).count()
    if expenses_count == 0:
        db.session.delete(category)
        db.session.commit()
    return redirect(request.referrer)

@app.route('/delete_expense/<int:id>')
@login_required
def delete_expense(id):
    expense_to_delete = Expense.query.get_or_404(id)
    db.session.delete(expense_to_delete)
    db.session.commit()
    return redirect(request.referrer)

@app.route('/export_ai_data')
@login_required
def export_ai_data():
    expenses = Expense.query.all()
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

if __name__ == '__main__':
    with app.app_context():
        # Drop all tables and recreate (for development - removes old schema)
        db.drop_all()
        db.create_all()
        
        # Seed default Users (with hashed passwords)
        dad = User(username='Dad', email='dad@family.com', role='Admin')
        dad.set_password('password123')
        
        arish = User(username='Mohd Arish', email='arish@family.com', role='Member')
        arish.set_password('password123')
        
        db.session.add(dad)
        db.session.add(arish)
        db.session.commit()
        
        # Seed default Universal Categories
        db.session.add_all([
            Category(name='Housing & Utilities', color='#3b82f6'),
            Category(name='Groceries & Food', color='#10b981'),
            Category(name='Healthcare', color='#ef4444'),
            Category(name='Transportation', color='#f59e0b'),
            Category(name='Debt Repayment', color='#8b5cf6'),
            Category(name='Miscellaneous', color='#64748b')
        ])
        db.session.commit()
        
        print("✓ Database initialized with demo users")
        print("  Username: Dad / Mohd Arish")
        print("  Password: password123")
    
    app.run(debug=True)