# VaultSync Multi-Tenant SaaS Upgrade - Migration Guide

## Overview
VaultSync has been upgraded to support **multi-tenant architecture** with multiple families, database migrations, and an admin panel. This guide walks you through the setup and migration process.

---

## Step 1: Install Flask-Migrate

Flask-Migrate is now integrated into the project. Ensure it's installed:

```bash
pip install flask-migrate
```

**Verify installation:**
```bash
pip show flask-migrate
```

---

## Step 2: Initialize Database Migrations

Since this is a **fresh schema with families**, you need to initialize migrations:

### Option A: Fresh Setup (Development)
If you're starting from scratch and don't have existing data:

```bash
cd E:\Project2027
flask db init
```

This creates a `migrations/` folder with Alembic configuration.

### Option B: Skip Initialization (Already Migrated)
If you've already run `flask db init`, skip to Step 3.

---

## Step 3: Create Initial Migration

Generate a migration file that captures the new schema (Family model, family_id foreign keys, etc.):

```bash
flask db migrate -m "Add Family model and family_id to User, Category, Expense, ExpectedExpense"
```

**What this does:**
- Compares current models with existing database state
- Generates a migration file in `migrations/versions/`
- The file contains SQL-like operations to update the schema

**Check the generated migration:**
```bash
# Review the file to ensure it looks correct
ls migrations/versions/
```

---

## Step 4: Apply Migrations to Database

Upgrade your database to the new schema:

```bash
flask db upgrade
```

**Expected output:**
```
INFO  [alembic.migration] Context impl SQLiteImpl.
INFO  [alembic.migration] Will assume non-transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade <version> -> <version>, Add Family model and family_id...
```

---

## Step 5: Initialize the Database with Demo Data

Now run the app to seed the initial data:

```bash
python app.py
```

**Expected output:**
```
✓ Database initialized with demo family and users
  Family Name: Demo Family
  Family Invite Code: <unique-code>
  Username: Dad (admin) / Mohd Arish (member)
  Password: password123
```

---

## Step 6: (Optional) Promote Additional Users to Admin

Use the `make_admin.py` script to promote any registered user to admin:

```bash
python make_admin.py
```

**Prompts:**
```
Enter the username of the user to promote to admin: <username>
```

**Output:**
```
✓ User '<username>' is now an admin
✓ Family ID: <id>
✓ Family Name: <family-name>
✓ Family Invite Code: <code>
```

---

## Step 7: Access the Admin Panel

1. **Start the Flask app:**
   ```bash
   python app.py
   ```

2. **Login with admin credentials:**
   - Username: `Dad`
   - Password: `password123`

3. **Navigate to Admin Panel:**
   - Visit: `http://localhost:5000/admin_panel`
   - You'll see:
     - **Family Invite Code**: Share this with family members to invite them
     - **Assign Members Form**: Add registered members to your family
     - **Family Members List**: View all members in the family

---

## Migration Workflow for Future Changes

When you modify models (e.g., add a new field), follow this workflow:

### 1. Update Your Model
```python
# In models.py
class Expense(db.Model):
    # ... existing fields ...
    new_field = db.Column(db.String(100))
```

### 2. Create Migration
```bash
flask db migrate -m "Add new_field to Expense"
```

### 3. Review the Migration
```bash
# Check migrations/versions/latest_migration.py
# Edit if needed
```

### 4. Apply the Migration
```bash
flask db upgrade
```

### 5. Verify in Database
The new schema is live and backward compatible!

---

## Important Architecture Notes

### Multi-Tenancy (Family Isolation)
- **Every user** belongs to exactly one `Family`
- **Every Expense, Category, and ExpectedExpense** has a `family_id`
- **All queries** are filtered by `current_user.family_id`
- This ensures **complete data isolation** between families

### Database URI
- **No changes** to the database URI (`sqlite:///vaultsync.db`)
- All migrations are applied to the same SQLite database
- Flask-Migrate uses **Alembic** under the hood for version tracking

### Admin Routes
- `/admin_panel` - Admin-only family management panel
- Only users with `role='admin'` can access this route

---

## Troubleshooting

### Issue: "No such table: family"
**Solution:** You haven't run `flask db upgrade` yet. Run it now:
```bash
flask db upgrade
```

### Issue: "Migration already applied"
**Solution:** Migrations are idempotent. Just run `flask db upgrade` again—it's safe.

### Issue: "Alembic revision error"
**Solution:** Check if `migrations/` folder exists. If not, run:
```bash
flask db init
flask db migrate -m "Initial migration"
flask db upgrade
```

### Issue: "User has no family_id"
**Solution:** Old users don't have a family. Assign them:
```bash
# In Python shell or script:
from app import app, db
from models import User, Family
with app.app_context():
    user = User.query.filter_by(username='username').first()
    if not user.family_id:
        family = Family(name=f"{user.username}'s Family")
        db.session.add(family)
        db.session.flush()
        user.family_id = family.id
        db.session.commit()
```

---

## Quick Reference Commands

| Command | Purpose |
|---------|---------|
| `flask db init` | Initialize migration system (one-time only) |
| `flask db migrate -m "description"` | Create a migration file |
| `flask db upgrade` | Apply all pending migrations |
| `flask db downgrade` | Rollback one migration (rarely used) |
| `flask db current` | Show current migration version |
| `flask db history` | Show all migrations history |

---

## Next Steps

1. ✅ Run migrations (`flask db upgrade`)
2. ✅ Initialize data (`python app.py`)
3. ✅ Login and visit `/admin_panel`
4. ✅ Share family invite code with members
5. ✅ Start tracking expenses with complete family isolation!

---

## Support

For issues or questions:
1. Check the troubleshooting section above
2. Review `models.py` to verify Family relationships
3. Ensure all routes use `family_id` filtering
4. Run `flask db current` to verify migration state

Happy budgeting! 🎉
