#!/usr/bin/env python
"""
Script to promote a user to admin and assign them to a family.
Usage: python make_admin.py
"""

from app import app, db
from models import User, Family

def main():
    with app.app_context():
        username = input("Enter the username of the user to promote to admin: ").strip()
        
        # Check if user exists
        user = User.query.filter_by(username=username).first()
        
        if not user:
            print(f"❌ User '{username}' not found.")
            return
        
        # If user doesn't have a family, create one for them
        if not user.family_id:
            family = Family(name=f"{username}'s Family")
            db.session.add(family)
            db.session.flush()  # Get the family ID
            user.family_id = family.id
            print(f"✓ Created new family '{family.name}' with invite code: {family.invite_code}")
        
        # Promote user to admin
        user.role = 'admin'
        db.session.commit()
        
        print(f"✓ User '{username}' is now an admin")
        print(f"✓ Family ID: {user.family_id}")
        print(f"✓ Family Name: {user.family.name}")
        print(f"✓ Family Invite Code: {user.family.invite_code}")

if __name__ == '__main__':
    main()
