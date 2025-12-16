from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
import os

SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30 * 24 * 60  # 30 days

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        token = credentials.credentials
        print(f"🔐 [AUTH] Verifying token: {token[:30]}...")
        print(f"🔐 [AUTH] Token length: {len(token)}")
        print(f"🔐 [AUTH] SECRET_KEY being used: {SECRET_KEY[:10]}...")
        print(f"🔐 [AUTH] Algorithm: {ALGORITHM}")
        
        # Check if token looks like a JWT
        parts = token.split('.')
        print(f"🔐 [AUTH] Token parts count: {len(parts)}")
        
        if len(parts) != 3:
            print(f"❌ [AUTH] Invalid JWT format - expected 3 parts, got {len(parts)}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token format"
            )
        
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        print(f"🔐 [AUTH] Extracted email from token: {email}")
        print(f"🔐 [AUTH] Token payload: {payload}")
        print(f"🔐 [AUTH] Token expiry: {payload.get('exp')}")
        
        if email is None:
            print(f"❌ [AUTH] No email found in token payload")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication credentials - no email in token"
            )
        return email
    except jwt.ExpiredSignatureError as e:
        print(f"❌ [AUTH] Token expired: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired"
        )
    except jwt.InvalidTokenError as e:
        print(f"❌ [AUTH] Invalid token: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )
    except JWTError as e:
        print(f"❌ [AUTH] JWT Error: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials"
        )
