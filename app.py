from flask import Flask, render_template, request, redirect, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin, LoginManager, login_required, current_user, login_user
import csv
from io import StringIO
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///vaultsync.db'
db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = 'dashboard'

# --- DATABASE MODELS ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    role = db.Column(db.String(20), default='Member') 

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
    return User.query.get(int(user_id))

# --- ROUTES ---
@app.route('/')
def dashboard():
    user = User.query.filter_by(username='Dad').first()
    if user and not current_user.is_authenticated:
        login_user(user)
    if not current_user.is_authenticated:
        return "Database is setting up, please refresh the page!"

    all_categories = Category.query.all()
    all_expenses = Expense.query.order_by(Expense.date.desc()).all()
    
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
                           chart_colors=chart_colors)

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
        db.create_all()
        if not User.query.first():
            db.session.add(User(username='Dad', role='Admin'))
            db.session.add(User(username='Mohd Arish', role='Member'))
            db.session.commit()
        if not Category.query.first():
            db.session.add_all([
                Category(name='Housing & Utilities', color='#3b82f6'),
                Category(name='Groceries & Food', color='#10b981'),
                Category(name='Healthcare', color='#ef4444'),
                Category(name='Transportation', color='#f59e0b'),
                Category(name='Debt Repayment', color='#8b5cf6'),
                Category(name='Miscellaneous', color='#64748b')
            ])
            db.session.commit()
    app.run(debug=True)