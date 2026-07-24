import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from src.common.config import Settings
from src.common.logging import get_logger
from src.models.audit_log import AuditLogCreate, AuditLogRead
from src.repositories.audit_log_repository import audit_log_repository
from src.db_models.audit_log import AuditLogDb # Import the DB model
from src.common.database import get_session_factory # Import session factory

# Use the main logger configuration but add a specific handler for audit logs
file_audit_logger = logging.getLogger("audit_file")
# Prevent propagation to avoid duplicate logging if root logger has handlers
file_audit_logger.propagate = False 


class AuditManager:
    """Manages logging of user actions to file and database."""

    def __init__(
        self,
        settings: Settings,
        db_session: Optional[Session],
        overlay_store=None,
        uc_store=None,
    ):
        self.settings = settings
        self.db = db_session # Store session for potential direct use if needed, though repo is preferred
        self.repository = audit_log_repository
        self._uc_overlays = overlay_store
        self._uc_store = uc_store
        self._configure_file_logger()

    def set_uc_overlays(self, overlays) -> None:
        """Attach the UC-native overlay store after startup creates it."""
        self._uc_overlays = overlays

    def _get_uc_store(self):
        return self._uc_store or getattr(self._uc_overlays, "_store", None) or self._uc_overlays

    def _write_uc_audit_event(self, log_entry_data: Dict[str, Any]) -> None:
        store = self._get_uc_store()
        if not store or not hasattr(store, "merge_row"):
            return
        try:
            store.merge_row(
                "audit_events",
                {
                    "id": str(uuid.uuid4()),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "username": log_entry_data["username"],
                    "feature": log_entry_data["feature"],
                    "action": log_entry_data["action"],
                    "success": log_entry_data["success"],
                    "details_json": json.dumps(log_entry_data["details"], default=str),
                },
            )
        except Exception:
            get_logger(__name__).warning(
                "Failed to write UC-native audit event", exc_info=True
            )

    def _configure_file_logger(self):
        """Configures the file logger for audit trails."""
        log_dir_path = Path(self.settings.APP_AUDIT_LOG_DIR)
        try:
            log_dir_path.mkdir(parents=True, exist_ok=True)
            log_file = log_dir_path / "audit.log"

            # Use JSON formatter for structured logging
            formatter = logging.Formatter(
                '{"timestamp": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s"}',
                datefmt='%Y-%m-%dT%H:%M:%S%z' # ISO 8601 format
            )
            
            # Rotate logs hourly, keeping 24 * 7 = 168 hours (1 week)
            file_handler = TimedRotatingFileHandler(
                log_file, 
                when="H", 
                interval=1, 
                backupCount=168, # Keep 1 week of hourly logs
                encoding='utf-8'
            )
            file_handler.setFormatter(formatter)

            # Clear existing handlers to avoid duplication if re-initialized
            if file_audit_logger.hasHandlers():
                file_audit_logger.handlers.clear()

            file_audit_logger.addHandler(file_handler)
            file_audit_logger.setLevel(logging.INFO) # Log INFO level and above to file
            file_audit_logger.info(f"Audit file logger configured. Logging to: {log_file}")

        except Exception as e:
            # Fallback to standard logger if file logging fails
            main_logger = get_logger(__name__)
            main_logger.error(f"Failed to configure audit file logger at {log_dir_path}: {e}", exc_info=True)
            # Ensure logger is disabled if setup fails
            file_audit_logger.disabled = True 


    def _log_action_internal(
        self,
        db: Optional[Session],
        log_entry_data: Dict[str, Any]
    ):
        """Internal synchronous logic to log to file and optionally DB."""
        # 1. Log to file (structured as JSON string)
        if not file_audit_logger.disabled:
            try:
                file_log_message = {
                    "user": log_entry_data["username"],
                    "ip": log_entry_data["ip_address"],
                    "feature": log_entry_data["feature"],
                    "action": log_entry_data["action"],
                    "success": log_entry_data["success"],
                    "details": log_entry_data["details"]
                }
                file_audit_logger.info(str(file_log_message).replace("'", '"'))
            except Exception as e:
                main_logger = get_logger(__name__)
                main_logger.error(f"Failed to write audit log to file: {e}", exc_info=True)

        # 2. Log to database (optional when APP_AUDIT_VOLUME_ONLY or no session)
        if getattr(self.settings, "APP_AUDIT_VOLUME_ONLY", False) or db is None:
            self._write_uc_audit_event(log_entry_data)
            return
        try:
            log_entry = AuditLogCreate(**log_entry_data)
            self.repository.create(db=db, obj_in=log_entry)
            db.commit() # Commit this independent transaction
        except Exception as e:
            main_logger = get_logger(__name__)
            main_logger.error(f"Failed to write audit log to database: {e}", exc_info=True)
            db.rollback() # Rollback only the audit transaction on error
            self._write_uc_audit_event(log_entry_data)
            # Do not re-raise here, as it's a background task

    # Original log_action (now uses independent session and commits)
    def log_action(
        self,
        db: Session, # Ignored - kept for backwards compatibility
        *,
        username: str,
        ip_address: Optional[str],
        feature: str,
        action: str,
        success: bool,
        details: Optional[Dict[str, Any]] = None
    ):
        """Logs an action synchronously using an INDEPENDENT DB session with auto-commit."""
        # get_session_factory() raises when Postgres was never initialized (uc_native).
        # Treat that as "no DB" so volume/file audit still works.
        try:
            session_factory = get_session_factory()
        except RuntimeError:
            session_factory = None

        log_entry_data = {
            "username": username,
            "ip_address": ip_address,
            "feature": feature,
            "action": action,
            "success": success,
            "details": details or {},
        }

        if not session_factory or getattr(self.settings, "APP_AUDIT_VOLUME_ONLY", False):
            self._log_action_internal(db=None, log_entry_data=log_entry_data)
            return

        try:
            with session_factory() as independent_db:
                self._log_action_internal(db=independent_db, log_entry_data=log_entry_data)
        except Exception as e:
            main_logger = get_logger(__name__)
            main_logger.error(f"[SYNC] Failed to write audit log: {e}", exc_info=True)

    # New method for background task
    async def log_action_background(
        self,
        *,
        username: str,
        ip_address: Optional[str],
        feature: str,
        action: str,
        success: bool,
        details: Optional[Dict[str, Any]] = None
    ):
        """Logs an action in the background using an independent DB session."""
        try:
            session_factory = get_session_factory()
        except RuntimeError:
            session_factory = None
        if not session_factory:
            main_logger = get_logger(__name__)
            main_logger.error("Cannot log audit action in background: DB session factory not available.")
            return

        log_entry_data = {
            "username": username,
            "ip_address": ip_address,
            "feature": feature,
            "action": action,
            "success": success,
            "details": details or {},
        }
        
        db_session = None
        try:
            with session_factory() as db_session:
                # Run the internal logging logic (which now handles file+DB and commits)
                self._log_action_internal(db=db_session, log_entry_data=log_entry_data)
        except Exception as e:
            # Catch potential errors during session creation or the internal log call itself
            main_logger = get_logger(__name__)
            main_logger.error(f"Error during background audit logging process: {e}", exc_info=True)
            if db_session:
                 db_session.rollback() # Ensure rollback if session existed but internal log failed before commit
        # Session is automatically closed by the context manager

    async def get_audit_logs(
        self,
        db: Session,
        skip: int = 0,
        limit: int = 100,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        username: Optional[str] = None,
        feature: Optional[str] = None,
        action: Optional[str] = None,
        success: Optional[bool] = None,
    ) -> tuple[int, List[AuditLogRead]]:
        """Retrieves audit logs from the database with filtering and pagination."""
        if db is not None and db.__class__.__name__ != "NoOpSession":
            try:
                total_count = self.repository.get_multi_count(
                    db,
                    start_time=start_time,
                    end_time=end_time,
                    username=username,
                    feature=feature,
                    action=action,
                    success=success,
                )
                db_logs = self.repository.get_multi(
                    db,
                    skip=skip,
                    limit=limit,
                    start_time=start_time,
                    end_time=end_time,
                    username=username,
                    feature=feature,
                    action=action,
                    success=success,
                )
                return total_count, [AuditLogRead.model_validate(log) for log in db_logs]
            except Exception:
                get_logger(__name__).warning(
                    "Failed to retrieve audit logs from database; falling back to UC",
                    exc_info=True,
                )
        return self._get_uc_audit_logs(
            skip=skip,
            limit=limit,
            start_time=start_time,
            end_time=end_time,
            username=username,
            feature=feature,
            action=action,
            success=success,
        )

    def _get_uc_audit_logs(
        self,
        *,
        skip: int,
        limit: int,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        username: Optional[str],
        feature: Optional[str],
        action: Optional[str],
        success: Optional[bool],
    ) -> tuple[int, List[AuditLogRead]]:
        store = self._get_uc_store()
        if not store or not hasattr(store, "list_rows"):
            return 0, []
        try:
            try:
                rows = store.list_rows(
                    "audit_events", limit=2000, order_by="timestamp DESC"
                )
            except TypeError:
                rows = store.list_rows("audit_events", limit=2000)
            logs = []
            for row in rows:
                timestamp = row.get("timestamp")
                if isinstance(timestamp, str):
                    timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if not isinstance(timestamp, datetime):
                    timestamp = datetime.now(timezone.utc)
                if (
                    (start_time and timestamp < start_time)
                    or (end_time and timestamp > end_time)
                    or (username and row.get("username") != username)
                    or (feature and row.get("feature") != feature)
                    or (action and row.get("action") != action)
                    or (success is not None and bool(row.get("success")) != success)
                ):
                    continue
                details = row.get("details_json")
                if isinstance(details, str):
                    try:
                        details = json.loads(details)
                    except json.JSONDecodeError:
                        details = {}
                logs.append(
                    AuditLogRead(
                        id=row.get("id"),
                        timestamp=timestamp,
                        username=row.get("username", ""),
                        ip_address=None,
                        feature=row.get("feature", ""),
                        action=row.get("action", ""),
                        success=bool(row.get("success")),
                        details=details or {},
                    )
                )
            logs.sort(key=lambda log: log.timestamp, reverse=True)
            return len(logs), logs[skip : skip + limit]
        except Exception:
            get_logger(__name__).warning(
                "Failed to retrieve UC-native audit logs", exc_info=True
            )
            return 0, []