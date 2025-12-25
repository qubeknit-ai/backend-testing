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

class AutoBidSettings(BaseModel):
    enabled: Optional[bool] = None
    daily_bids: Optional[int] = None
    currencies: Optional[List[str]] = None
    frequency_minutes: Optional[int] = None
    max_project_bids: Optional[int] = None
    smart_bidding: Optional[bool] = None