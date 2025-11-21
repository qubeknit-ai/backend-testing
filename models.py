from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, ForeignKey, JSON, Float
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base

class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)  # Multi-user support
    platform = Column(String, nullable=False)
    title = Column(String, nullable=False)
    category = Column(Text)
    budget = Column(String)
    bids = Column(Integer, default=0)
    cost = Column(Integer, default=0)  # For Upwork: 1-10 (low), 10-19 (mid), 20+ (high)
    avg_bid_price = Column(String, nullable=True)  # Average bid price for the job
    posted = Column(String)
    posted_time = Column(DateTime)
    status = Column(String, default="Pending")
    score = Column(String)
    description = Column(Text)
    proposal = Column(Text)
    url = Column(String)
    revenue = Column(Integer, default=0)  # Revenue generated from this lead (in USD)
    proposal_sent = Column(Boolean, default=False)  # Whether proposal was sent
    proposal_accepted = Column(Boolean, default=False)  # Whether proposal was accepted
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationship
    user = relationship("User", back_populates="leads")

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    role = Column(String, default="user")  # "admin" or "user"
    name = Column(String, nullable=True)  # User's full name
    telegram_chat_id = Column(String, nullable=True)  # Telegram chat ID for notifications
    country = Column(String, nullable=True)  # User's country
    
    # Daily fetch limits
    upwork_fetch_count = Column(Integer, default=0)  # Daily fetch count for Upwork
    upwork_last_reset = Column(DateTime, nullable=True)  # Last time the counter was reset
    freelancer_fetch_count = Column(Integer, default=0)  # Daily fetch count for Freelancer
    freelancer_last_reset = Column(DateTime, nullable=True)  # Last time the counter was reset
    freelancer_plus_fetch_count = Column(Integer, default=0)  # Daily fetch count for Freelancer Plus
    freelancer_plus_last_reset = Column(DateTime, nullable=True)  # Last time the counter was reset
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    leads = relationship("Lead", back_populates="user", cascade="all, delete-orphan")
    settings = relationship("UserSettings", back_populates="user", uselist=False, cascade="all, delete-orphan")
    notifications = relationship("Notification", back_populates="user", cascade="all, delete-orphan")

class UserSettings(Base):
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)  # Multi-user support
    
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
    
    # AI Agent settings
    ai_agent_min_score = Column(Integer, default=2)  # Minimum score for auto-draft
    ai_agent_max_score = Column(Integer, default=8)  # Maximum score for auto-draft
    ai_agent_model = Column(String, default="gpt-4")  # AI model to use
    ai_agent_max_bids_freelancer = Column(Integer, default=30)  # Max bids for Freelancer jobs
    ai_agent_max_connects_upwork = Column(Integer, default=20)  # Max connects for Upwork jobs
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationship
    user = relationship("User", back_populates="settings")

class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)  # Multi-user support
    type = Column(String, nullable=False)  # success, info, warning, error
    title = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationship
    user = relationship("User", back_populates="notifications")

class SystemSettings(Base):
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True, index=True)
    
    # Default daily limits for new users
    default_upwork_limit = Column(Integer, default=5)
    default_freelancer_limit = Column(Integer, default=5)
    default_freelancer_plus_limit = Column(Integer, default=3)
    
    # Default max jobs to fetch per request
    default_upwork_max_jobs = Column(Integer, default=3)
    default_freelancer_max_jobs = Column(Integer, default=3)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Talent(Base):
    __tablename__ = "talents"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    rate = Column(Float, nullable=True)  # Hourly rate in USD
    rating = Column(Float, nullable=True)  # Rating 0-5
    reviews = Column(Integer, nullable=True)  # Number of reviews
    skills = Column(JSON, default=list)  # List of skills
    location = Column(String, nullable=True)
    profile_url = Column(String, nullable=True)
    image_url = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationship
    user = relationship("User")
