from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base

def now(): return datetime.now(timezone.utc)

class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    role: Mapped[str] = mapped_column(String(40), default='admin')
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Session(Base):
    __tablename__ = 'sessions'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    csrf_token: Mapped[str] = mapped_column(String(80))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    user = relationship('User')

class Instance(Base):
    __tablename__ = 'instances'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    friendly_name: Mapped[str] = mapped_column(String(160))
    url: Mapped[str] = mapped_column(String(500))
    environment: Mapped[str] = mapped_column(String(80), default='Production')
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tags: Mapped[str] = mapped_column(String(1000), default='')
    installation_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ha_core_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    supervisor_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ha_os_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    connectivity_state: Mapped[str] = mapped_column(String(40), default='unknown')
    health_state: Mapped[str] = mapped_column(String(40), default='unknown')
    last_successful_connection: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failed_connection: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_update_scan: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    available_updates: Mapped[int] = mapped_column(Integer, default=0)
    critical_updates: Mapped[int] = mapped_column(Integer, default=0)
    pending_approvals: Mapped[int] = mapped_column(Integer, default=0)
    last_successful_backup: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    backup_compliance_state: Mapped[str] = mapped_column(String(40), default='unknown')
    maintenance_window: Mapped[str | None] = mapped_column(String(200), nullable=True)
    update_policy: Mapped[str] = mapped_column(String(80), default='Manual')
    maintenance_hold: Mapped[bool] = mapped_column(Boolean, default=False)
    credential_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    credential_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_successful_auth: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_auth_failure: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

class InstanceCredential(Base):
    __tablename__ = 'instance_credentials'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey('instances.id'), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(80), default='encrypted_db')
    key_id: Mapped[str] = mapped_column(String(80), default='local')
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    tag: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

class UpdateRecord(Base):
    __tablename__ = 'update_records'
    __table_args__ = (UniqueConstraint('instance_id', 'entity_id', name='uq_update_instance_entity'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey('instances.id'), index=True)
    provider: Mapped[str] = mapped_column(String(80), default='ha_update_entity')
    entity_id: Mapped[str] = mapped_column(String(250))
    component: Mapped[str] = mapped_column(String(250))
    category: Mapped[str] = mapped_column(String(80), default='Other')
    installed_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    available_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    release_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    release_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    release_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    breaking_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = mapped_column(String(40), default='standard')
    risk_level: Mapped[str] = mapped_column(String(40), default='low')
    critical_state: Mapped[bool] = mapped_column(Boolean, default=False)
    breaking_state: Mapped[bool] = mapped_column(Boolean, default=False)
    restart_required: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_action_required: Mapped[bool] = mapped_column(Boolean, default=False)
    approval_state: Mapped[str] = mapped_column(String(40), default='not_required')
    installation_state: Mapped[str] = mapped_column(String(40), default='available')
    skip_state: Mapped[str] = mapped_column(String(40), default='none')
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_discovered: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    policy_decision: Mapped[str] = mapped_column(String(80), default='manual')
    policy_explanation: Mapped[str] = mapped_column(Text, default='Default manual policy')
    raw_json: Mapped[str] = mapped_column(Text, default='{}')

class AuditEvent(Base):
    __tablename__ = 'audit_events'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    actor_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    resource_type: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[str] = mapped_column(String(40), default='success')
    request_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    before_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default='{}')

class DeploymentPlan(Base):
    __tablename__ = 'deployment_plans'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(60), default='Draft')
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    summary_json: Mapped[str] = mapped_column(Text, default='{}')

class Operation(Base):
    __tablename__ = 'operations'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(80))
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deployment_plan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(80), default='Queued')
    status: Mapped[str] = mapped_column(String(40), default='queued')
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    details_json: Mapped[str] = mapped_column(Text, default='{}')

class Approval(Base):
    __tablename__ = 'approvals'
    __table_args__ = (UniqueConstraint('update_record_id', 'target_version', name='uq_approval_update_target'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    update_record_id: Mapped[int] = mapped_column(ForeignKey('update_records.id'), index=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey('instances.id'), index=True)
    target_version: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(40), default='pending')
    requested_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approved_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class Notification(Base):
    __tablename__ = 'notifications'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    severity: Mapped[str] = mapped_column(String(40), default='info')
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default='open')
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    update_record_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class BackupRecord(Base):
    __tablename__ = 'backup_records'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey('instances.id'), index=True)
    provider: Mapped[str] = mapped_column(String(80), default='home_assistant')
    status: Mapped[str] = mapped_column(String(40), default='unknown')
    backup_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    details_json: Mapped[str] = mapped_column(Text, default='{}')

class Schedule(Base):
    __tablename__ = 'schedules'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(80), default='monitor')
    cron: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class JobRun(Base):
    __tablename__ = 'job_runs'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(40), default='running')
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    details_json: Mapped[str] = mapped_column(Text, default='{}')


class PolicySetting(Base):
    __tablename__ = 'policy_settings'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    value_json: Mapped[str] = mapped_column(Text, default='{}')
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
