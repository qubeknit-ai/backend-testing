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
    visible = Column(Boolean, default=True)  # Whether the lead is visible to the user
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
    
    # Freelancer settings
    freelancer_job_category = Column(String, default="Web Development")
    freelancer_max_jobs = Column(Integer, default=3)
    
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

class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False, index=True)
    message = Column(Text, nullable=False)
    sender = Column(String, nullable=False)  # 'user' or 'ai'
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    user = relationship("User")
    lead = relationship("Lead")

class FreelancerCredentials(Base):
    __tablename__ = "freelancer_credentials"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    
    # Freelancer authentication data
    access_token = Column(Text, nullable=True)  # OAuth access token
    csrf_token = Column(String, nullable=True)  # CSRF token
    freelancer_user_id = Column(String, nullable=True)  # Freelancer user ID
    auth_hash = Column(Text, nullable=True)  # Auth hash from cookies
    
    # Session cookies (stored as JSON)
    cookies = Column(JSON, nullable=True)  # All Freelancer cookies
    
    # Validation status
    is_validated = Column(Boolean, default=False)  # Whether credentials are validated
    validated_username = Column(String, nullable=True)  # Freelancer username
    validated_email = Column(String, nullable=True)  # Freelancer email
    
    # Skills preferences (stored as JSON array)
    selected_skills = Column(JSON, default=list)  # List of selected skill names for filtering projects
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_validated = Column(DateTime, nullable=True)  # Last time credentials were validated
    
    # Relationship
    user = relationship("User")

class BidHistory(Base):
    __tablename__ = "bid_history"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    project_id = Column(String, nullable=False)  # Freelancer project ID
    project_title = Column(String, nullable=False)
    project_url = Column(String, nullable=True)
    bid_amount = Column(Float, nullable=False)
    proposal_text = Column(Text, nullable=True)
    status = Column(String, nullable=False)  # 'success', 'failed', 'pending'
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationship
    user = relationship("User")

class AutoBidSettings(Base):
    __tablename__ = "auto_bid_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    
    enabled = Column(Boolean, default=False)
    daily_bids = Column(Integer, default=10)  # Max bids per day
    currencies = Column(JSON, default=lambda: ["USD"])  # Supported currencies
    frequency_minutes = Column(Integer, default=10)
    max_project_bids = Column(Integer, default=50)  # Max existing bids on project
    smart_bidding = Column(Boolean, default=True)  # Use average of min/max
    min_skill_match = Column(Integer, default=1)  # Minimum number of skills that must match
    proposal_type = Column(Integer, default=1)  # Proposal type: 1, 2, or 3
    commission_projects = Column(Boolean, default=True)  # Include commission-based projects
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationship
    user = relationship("User")

class UpworkCredentials(Base):
    __tablename__ = "upwork_credentials"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    access_token = Column(Text, nullable=True)
    oauth_token = Column(Text, nullable=True)
    upwork_user_id = Column(String, nullable=True)
    is_validated = Column(Boolean, default=False)
    validated_username = Column(String, nullable=True)
    validated_email = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_validated = Column(DateTime, nullable=True)
    user = relationship("User")


class GuruCredentials(Base):
    __tablename__ = "guru_credentials"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    access_token = Column(Text, nullable=True)
    csrf_token = Column(String, nullable=True)
    guru_user_id = Column(String, nullable=True)
    is_validated = Column(Boolean, default=False)
    validated_username = Column(String, nullable=True)
    validated_email = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_validated = Column(DateTime, nullable=True)
    user = relationship("User")


class ClosedDeal(Base):
    __tablename__ = "closed_deals"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    bid_history_id = Column(Integer, ForeignKey("bid_history.id"), nullable=True, index=True)  # Link to bid history
    
    # Project details
    project_title = Column(String, nullable=False)
    project_url = Column(String, nullable=True)
    platform = Column(String, nullable=False)  # Upwork, Freelancer, etc.
    
    # Financial details
    client_payment = Column(Float, nullable=False)  # Amount client pays
    outsource_cost = Column(Float, nullable=False)  # Amount paid to freelancer
    platform_fee = Column(Float, nullable=False)  # Platform fees (calculated)
    profit = Column(Float, nullable=False)  # Net profit (calculated)
    
    # Status
    status = Column(String, default="active")  # active, completed, cancelled
    
    # Dates
    closed_date = Column(DateTime, default=datetime.utcnow)
    completion_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    user = relationship("User")
    bid_history = relationship("BidHistory")
