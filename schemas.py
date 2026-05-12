from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional, List, Dict, Any, Union
from datetime import datetime

class UserSignup(BaseModel):
    email: str
    password: str
    
    @field_validator('email')
    @classmethod
    def validate_email(cls, v):
        if '@' not in v:
            raise ValueError('Invalid email format')
        return v.lower()

class UserLogin(BaseModel):
    email: str
    password: str
    
    @field_validator('email')
    @classmethod
    def validate_email(cls, v):
        if '@' not in v:
            raise ValueError('Invalid email format')
        return v.lower()

class Token(BaseModel):
    access_token: str
    token_type: str

class UserResponse(BaseModel):
    id: int
    email: str
    role: str = "user"
    name: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    country: Optional[str] = None
    ai_agent_model: Optional[str] = None
    
    class Config:
        from_attributes = True

class UserProfileUpdate(BaseModel):
    name: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    country: Optional[str] = None

class SettingsUpdate(BaseModel):
    upwork_job_categories: Optional[List[str]] = None
    upwork_max_jobs: Optional[int] = None
    upwork_payment_verified: Optional[bool] = None
    freelancer_job_category: Optional[str] = None
    freelancer_max_jobs: Optional[int] = None
    ai_agent_min_score: Optional[int] = None
    ai_agent_max_score: Optional[int] = None
    ai_agent_model: Optional[str] = None
    ai_agent_max_bids_freelancer: Optional[int] = None
    ai_agent_max_connects_upwork: Optional[int] = None

class SettingsResponse(BaseModel):
    id: int
    upwork_job_categories: List[str]
    upwork_max_jobs: int
    upwork_payment_verified: bool
    freelancer_job_category: str
    freelancer_max_jobs: int
    ai_agent_min_score: int
    ai_agent_max_score: int
    ai_agent_model: str
    ai_agent_max_bids_freelancer: int
    ai_agent_max_connects_upwork: int
    
    class Config:
        from_attributes = True

class TalentCreate(BaseModel):
    name: str
    description: Optional[str] = None
    rate: Optional[float] = None
    rating: Optional[float] = None
    reviews: Optional[int] = None
    skills: Optional[List[str]] = []
    location: Optional[str] = None
    profile_url: Optional[str] = None
    image_url: Optional[str] = None

class TalentUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    rate: Optional[float] = None
    rating: Optional[float] = None
    reviews: Optional[int] = None
    skills: Optional[List[str]] = None
    location: Optional[str] = None
    profile_url: Optional[str] = None
    image_url: Optional[str] = None

class TalentResponse(BaseModel):
    id: int
    user_id: int
    name: str
    description: Optional[str] = None
    rate: Optional[float] = None
    rating: Optional[float] = None
    reviews: Optional[int] = None
    skills: List[str] = []
    location: Optional[str] = None
    profile_url: Optional[str] = None
    image_url: Optional[str] = None
    created_at: str
    updated_at: str
    
    class Config:
        from_attributes = True
class FreelancerCredentialsCreate(BaseModel):
    access_token: Optional[str] = None
    csrf_token: Optional[str] = None
    freelancer_user_id: Optional[Union[str, int]] = None
    auth_hash: Optional[str] = None
    cookies: Optional[Dict[str, Any]] = None
    validated_username: Optional[str] = None
    validated_email: Optional[str] = None
    selected_skills: Optional[List[str]] = []

class FreelancerCredentialsResponse(BaseModel):
    id: int
    user_id: int
    access_token: Optional[str] = None
    csrf_token: Optional[str] = None
    freelancer_user_id: Optional[str] = None
    auth_hash: Optional[str] = None
    cookies: Optional[Dict[str, Any]] = None
    is_validated: bool = False
    validated_username: Optional[str] = None
    validated_email: Optional[str] = None
    selected_skills: List[str] = []
    created_at: datetime
    updated_at: datetime
    last_validated: Optional[datetime] = None
    
    class Config:
        from_attributes = True

class FreelancerCredentialsUpdate(BaseModel):
    access_token: Optional[str] = None
    csrf_token: Optional[str] = None
    freelancer_user_id: Optional[Union[str, int]] = None
    auth_hash: Optional[str] = None
    cookies: Optional[Dict[str, Any]] = None
    is_validated: Optional[bool] = None
    validated_username: Optional[str] = None
    validated_email: Optional[str] = None
    selected_skills: Optional[List[str]] = None

class AutoBidSettings(BaseModel):
    enabled: Optional[bool] = None
    daily_bids: Optional[int] = None
    currencies: Optional[List[str]] = None
    frequency_minutes: Optional[int] = None
    max_project_bids: Optional[int] = None
    smart_bidding: Optional[bool] = None
    min_skill_match: Optional[int] = None
    proposal_type: Optional[int] = None
    commission_projects: Optional[bool] = None
    payment_verified: Optional[bool] = None
    min_hires: Optional[int] = None
    min_budget: Optional[float] = None

class ClosedDealCreate(BaseModel):
    bid_history_id: Optional[int] = None
    project_title: str
    project_url: Optional[str] = None
    platform: str
    client_payment: float
    outsource_cost: float
    platform_fee: float
    status: Optional[str] = "active"

class ClosedDealUpdate(BaseModel):
    project_title: Optional[str] = None
    project_url: Optional[str] = None
    client_payment: Optional[float] = None
    outsource_cost: Optional[float] = None
    platform_fee: Optional[float] = None
    status: Optional[str] = None
    completion_date: Optional[datetime] = None

class ClosedDealResponse(BaseModel):
    id: int
    user_id: int
    bid_history_id: Optional[int] = None
    project_title: str
    project_url: Optional[str] = None
    platform: str
    client_payment: float
    outsource_cost: float
    platform_fee: float
    profit: float
    status: str
    closed_date: datetime
    completion_date: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True

# ----- Extracted Requests -----
class BidRequest(BaseModel):
    access_token: str
    project_id: int
    bidder_id: int
    amount: float
    period: int = 7
    description: str
    milestone_percentage: int = 100
    freelancer_cookies: Optional[str] = None

class ProjectsRequest(BaseModel):
    access_token: str
    limit: int = 20
    freelancer_cookies: Optional[str] = None

class MessageRequest(BaseModel):
    thread_id: int
    message: str
    access_token: str
    freelancer_cookies: Optional[str] = None
