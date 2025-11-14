from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, ForeignKey, JSON
from datetime import datetime
from database import Base

class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)
    platform = Column(String, nullable=False)
    title = Column(String, nullable=False)
    category = Column(Text)
    budget = Column(String)
    bids = Column(Integer, default=0)
    cost = Column(Integer, default=0)  # For Upwork: 1-10 (low), 10-19 (mid), 20+ (high)
    posted = Column(String)
    posted_time = Column(DateTime)
    status = Column(String, default="Pending")
    score = Column(String)
    description = Column(Text)
    proposal = Column(Text)
    url = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class GlobalSettings(Base):
    __tablename__ = "global_settings"

    id = Column(Integer, primary_key=True, index=True)
    
    # Upwork settings
    upwork_job_categories = Column(JSON, default=lambda: ["Web Development"])
    upwork_max_jobs = Column(Integer, default=3)
    upwork_payment_verified = Column(Boolean, default=False)
    upwork_auto_fetch = Column(Boolean, default=False)
    upwork_auto_fetch_interval = Column(Integer, default=2)  # in minutes
    
    # Freelancer settings
    freelancer_job_category = Column(String, default="Web Development")
    freelancer_max_jobs = Column(Integer, default=3)
    freelancer_auto_fetch = Column(Boolean, default=False)
    freelancer_auto_fetch_interval = Column(Integer, default=3)  # in minutes
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    type = Column(String, nullable=False)  # success, info, warning, error
    title = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
