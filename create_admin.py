"""
Script to create an admin user or promote an existing user to admin
Usage: python create_admin.py <email>
"""
import sys
from sqlalchemy.orm import Session
from database import SessionLocal, engine
from models import User, Base
from auth import get_password_hash

def create_admin(email: str, password: str = None):
    """Create a new admin user or promote existing user to admin"""
    db = SessionLocal()
    
    try:
        # Check if user exists
        user = db.query(User).filter(User.email == email.lower()).first()
        
        if user:
            # Promote existing user to admin
            user.role = "admin"
            db.commit()
            print(f"✅ User {email} promoted to admin!")
        else:
            # Create new admin user
            if not password:
                password = input("Enter password for new admin user: ")
            
            hashed_password = get_password_hash(password)
            new_user = User(
                email=email.lower(),
                hashed_password=hashed_password,
                role="admin",
                name="Admin User"
            )
            db.add(new_user)
            db.commit()
            print(f"✅ Admin user {email} created successfully!")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python create_admin.py <email> [password]")
        print("Example: python create_admin.py admin@example.com")
        sys.exit(1)
    
    email = sys.argv[1]
    password = sys.argv[2] if len(sys.argv) > 2 else None
    
    create_admin(email, password)
