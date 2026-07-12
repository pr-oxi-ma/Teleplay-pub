"""
Pydantic schemas for API request/response validation.
"""
from datetime import datetime
from typing import Optional, List, Literal
from pydantic import BaseModel, ConfigDict, Field


# ============== User Schemas ==============

class UserBase(BaseModel):
    telegram_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None


class UserCreate(UserBase):
    pass


class UserResponse(UserBase):
    id: int
    created_at: datetime
    last_active: datetime
    
    model_config = ConfigDict(from_attributes=True)


# ============== Folder Schemas ==============

class FolderBase(BaseModel):
    name: str
    parent_id: Optional[int] = None


class FolderCreate(FolderBase):
    pass


class FolderUpdate(BaseModel):
    name: Optional[str] = None
    parent_id: Optional[int] = None


class FolderResponse(FolderBase):
    id: int
    user_id: int
    created_at: datetime
    updated_at: datetime
    file_count: int = 0
    deleted_at: Optional[datetime] = None
    purge_after: Optional[datetime] = None
    
    model_config = ConfigDict(from_attributes=True)


class FolderWithChildren(FolderResponse):
    children: List["FolderWithChildren"] = Field(default_factory=list)
    

# ============== File Schemas ==============

class FileBase(BaseModel):
    file_name: str
    file_size: int
    mime_type: Optional[str] = None
    file_type: str  # video, audio, document, image
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None


class FileCreate(FileBase):
    file_id: str
    file_unique_id: str
    channel_message_id: int
    thumbnail_file_id: Optional[str] = None
    folder_id: Optional[int] = None


class FileUpdate(BaseModel):
    file_name: Optional[str] = None
    folder_id: Optional[int] = None


class FileResponse(FileBase):
    id: int
    user_id: int
    folder_id: Optional[int] = None
    file_id: str
    file_unique_id: str
    channel_message_id: int
    thumbnail_file_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    thumbnail_url: Optional[str] = None
    stream_url: Optional[str] = None
    fallback_stream_url: Optional[str] = None
    download_url: Optional[str] = None
    public_hash: Optional[str] = None
    public_stream_url: Optional[str] = None
    last_pos: int = 0
    deleted_at: Optional[datetime] = None
    purge_after: Optional[datetime] = None
    
    model_config = ConfigDict(from_attributes=True)


class FileListResponse(BaseModel):
    files: List[FileResponse]
    total: int
    page: int
    per_page: int


# ============== Watch Progress Schemas ==============

class WatchProgressBase(BaseModel):
    position: int
    duration: Optional[float] = None
    completed: bool = False


class WatchProgressUpdate(BaseModel):
    position: int
    duration: Optional[float] = None


class WatchProgressResponse(WatchProgressBase):
    id: int
    file_id: int
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


# ============== Auth Schemas ==============

class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: Optional[int] = None


class RefreshTokenRequest(BaseModel):
    """Request body for token refresh. Optional for HttpOnly-cookie web sessions."""
    refresh_token: Optional[str] = Field(None, alias="refreshToken")

    model_config = ConfigDict(populate_by_name=True)


class PasswordLoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1, max_length=255)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(..., alias="currentPassword", min_length=1, max_length=255)
    new_password: str = Field(..., alias="newPassword", min_length=1, max_length=255)

    model_config = ConfigDict(populate_by_name=True)


class UsernameUpdateRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=16)


class UsernameCheckResponse(BaseModel):
    username: str
    available: bool
    valid: bool
    reason: Optional[str] = None


class WebCredentialResponse(BaseModel):
    username: Optional[str] = None
    has_password: bool = False


class MessageResponse(BaseModel):
    message: str


class TokenPayload(BaseModel):
    sub: int  # user telegram_id
    exp: datetime


class LoginCodeRequest(BaseModel):
    code: str


class LoginCodeResponse(BaseModel):
    code: str
    expires_at: datetime


class VerifyCodeRequest(BaseModel):
    code: str


class AuthResponse(Token):
    user: UserResponse




class PollCodeResponse(BaseModel):
    status: Literal["pending", "claimed"]
    message: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_type: str = "bearer"
    expires_in: Optional[int] = None
    user: Optional[UserResponse] = None


class AuthSessionResponse(BaseModel):
    session_id: str
    current: bool = False
    session_type: Literal["persistent", "temporary"] = "persistent"
    user_agent: Optional[str] = None
    created_at: datetime
    last_used_at: datetime
    last_seen_at: datetime
    expires_at: datetime

    model_config = ConfigDict(from_attributes=True)

class BotInfoResponse(BaseModel):
    username: str
    name: Optional[str] = None
    server_version: str = "1.0.0"


# ============== Recycle Bin Schemas ==============

class RecycleBinSettingsUpdate(BaseModel):
    enabled: bool
    retention_days: int = Field(..., ge=1, le=365)


class RecycleBinSettingsResponse(BaseModel):
    enabled: bool
    retention_days: int
    updated_items: int = 0


class TrashBreadcrumbResponse(BaseModel):
    id: int
    name: str


class TrashFolderResponse(BaseModel):
    id: int
    name: str
    parent_id: Optional[int] = None
    deleted_at: datetime
    purge_after: datetime
    item_count: int = 0


class TrashListResponse(BaseModel):
    files: List[FileResponse]
    folders: List[TrashFolderResponse]
    total: int


class TrashBrowseResponse(TrashListResponse):
    current_folder: TrashFolderResponse
    breadcrumbs: List[TrashBreadcrumbResponse]


class TrashBulkRequest(BaseModel):
    file_ids: List[int] = Field(default_factory=list, max_length=500)
    folder_ids: List[int] = Field(default_factory=list, max_length=500)


# Resolve forward references
FolderWithChildren.model_rebuild()
