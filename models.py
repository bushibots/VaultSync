from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import secrets

# Database instance - initialized in app.py
db = SQLAlchemy()

class Family(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, index=True)
    invite_code = db.Column(db.String(20), unique=True, nullable=False, default=lambda: secrets.token_urlsafe(12))
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    monthly_budget = db.Column(db.Float, default=170000)
    is_archived = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    users = db.relationship('User', backref='family', lazy=True, foreign_keys='User.family_id')
    categories = db.relationship('Category', backref='family', lazy=True)
    expenses = db.relationship('Expense', backref='family', lazy=True)
    expected_expenses = db.relationship('ExpectedExpense', backref='family', lazy=True)
    budget_suggestions = db.relationship('BudgetSuggestion', backref='family', lazy=True)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    user_type = db.Column(db.String(20), default='family_member')
    family_id = db.Column(db.Integer, db.ForeignKey('family.id'), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expenses = db.relationship('Expense', backref='user', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, index=True)
    color = db.Column(db.String(20), nullable=False)
    monthly_limit = db.Column(db.Float, default=0.0)
    is_fixed = db.Column(db.Boolean, default=False)
    family_id = db.Column(db.Integer, db.ForeignKey('family.id'), nullable=False)

class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    item_name = db.Column(db.String(100), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    category = db.relationship('Category')
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    family_id = db.Column(db.Integer, db.ForeignKey('family.id'), nullable=False)
    bucket_id = db.Column(db.Integer, db.ForeignKey('expected_expense.id'), nullable=True)
    bucket = db.relationship('ExpectedExpense', foreign_keys=[bucket_id], back_populates='bucket_expenses')


class ExpectedExpense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    is_bucket = db.Column(db.Boolean, default=False, nullable=False)
    allocated_amount = db.Column(db.Float, default=0.0, nullable=False)
    is_paid = db.Column(db.Boolean, default=False, nullable=False)
    due_day = db.Column(db.Integer, default=1, nullable=False)
    paid_at = db.Column(db.DateTime, nullable=True)
    linked_expense_id = db.Column(db.Integer, db.ForeignKey('expense.id'), nullable=True)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    category = db.relationship('Category')
    linked_expense = db.relationship('Expense', foreign_keys=[linked_expense_id])
    bucket_expenses = db.relationship('Expense', foreign_keys='Expense.bucket_id', back_populates='bucket')
    family_id = db.Column(db.Integer, db.ForeignKey('family.id'), nullable=False)
    month = db.Column(db.Integer, nullable=False)
    year = db.Column(db.Integer, nullable=False)


class BudgetSuggestion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    family_id = db.Column(db.Integer, db.ForeignKey('family.id'), nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_by = db.relationship('User')
    source_start_date = db.Column(db.Date, nullable=False)
    source_end_date = db.Column(db.Date, nullable=False)
    target_start_date = db.Column(db.Date, nullable=False)
    target_end_date = db.Column(db.Date, nullable=False)
    title = db.Column(db.String(160), nullable=False)
    suggested_monthly_budget = db.Column(db.Float, nullable=True)
    total_planned = db.Column(db.Float, default=0.0, nullable=False)
    risk_level = db.Column(db.String(20), default='medium', nullable=False)
    notes = db.Column(db.Text, default='')
    raw_json = db.Column(db.Text, nullable=False)
    is_applied = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    family_id = db.Column(db.Integer, db.ForeignKey('family.id'), nullable=False)
    action = db.Column(db.String(100), nullable=False)
    details = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
