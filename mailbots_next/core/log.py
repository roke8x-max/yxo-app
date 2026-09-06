import logging
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import LOGS_DIR


class EmailLogger:
    def __init__(self, name: str = "mailbots_next"):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False

        if not self.logger.handlers:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)

            log_file = LOGS_DIR / f"mailbot_{datetime.now().strftime('%Y-%m-%d')}.log"
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)

            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(logging.INFO)

            formatter = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            file_handler.setFormatter(formatter)
            console_handler.setFormatter(formatter)

            self.logger.addHandler(file_handler)
            self.logger.addHandler(console_handler)

    def _mask_email(self, email: str) -> str:
        if not email or "@" not in email:
            return email
        local, domain = email.split("@", 1)
        if len(local) <= 2:
            masked_local = local[0] + "*" * (len(local) - 1)
        else:
            masked_local = local[:2] + "***" + local[-1]
        return f"{masked_local}@{domain}"

    def _mask_recipients(self, recipients: list) -> list:
        return [self._mask_email(r) for r in recipients]

    def info(self, msg: str, **kwargs):
        self.logger.info(msg, **kwargs)

    def debug(self, msg: str, **kwargs):
        self.logger.debug(msg, **kwargs)

    def warning(self, msg: str, **kwargs):
        self.logger.warning(msg, **kwargs)

    def error(self, msg: str, error_id: Optional[str] = None, **kwargs):
        if error_id is None:
            error_id = str(uuid.uuid4())[:8]
        extra = {"error_id": error_id}
        extra.update(kwargs)
        self.logger.error(f"[error_id={error_id}] {msg}", extra=extra)
        return error_id

    def exception(self, msg: str, error_id: Optional[str] = None, **kwargs):
        if error_id is None:
            error_id = str(uuid.uuid4())[:8]
        extra = {"error_id": error_id}
        extra.update(kwargs)
        self.logger.exception(f"[error_id={error_id}] {msg}", extra=extra)
        return error_id

    def log_email_received(self, message_id: str, folder: str, sender: str, subject: str, email_type: str = ""):
        masked_sender = self._mask_email(sender)
        self.info(
            f"Email received | type={email_type} | folder={folder} | from={masked_sender} | subject={subject[:100]} | msg_id={message_id[:50]}"
        )

    def log_type_identified(self, message_id: str, email_type: str, reason: str):
        self.info(f"Type identified | msg_id={message_id[:50]} | type={email_type} | reason={reason}")

    def log_type_skipped(self, message_id: str, email_type: str, reason: str):
        self.info(f"Type skipped (disabled) | msg_id={message_id[:50]} | type={email_type} | reason={reason}")

    def log_extraction(self, message_id: str, email_type: str, rows_count: int, keys: list):
        self.info(f"Extraction done | msg_id={message_id[:50]} | type={email_type} | rows={rows_count} | keys={keys}")

    def log_routing(self, message_id: str, row_idx: int, company: str, responsible_person: str, to_list: list, cc_list: list):
        self.info(
            f"Routing resolved | msg_id={message_id[:50]} | row={row_idx} | company={company} | responsible={self._mask_email(responsible_person)} | to={self._mask_recipients(to_list)} | cc={self._mask_recipients(cc_list)}"
        )

    def log_routing_failed(self, message_id: str, row_idx: int, reason: str):
        self.warning(f"Routing failed | msg_id={message_id[:50]} | row={row_idx} | reason={reason}")

    def log_decide(self, message_id: str, row_idx: int, tier: str, action: str, reason: str):
        self.info(f"Decision | msg_id={message_id[:50]} | row={row_idx} | tier={tier} | action={action} | reason={reason}")

    def log_action(self, message_id: str, row_idx: int, action: str, detail: str):
        self.info(f"Action | msg_id={message_id[:50]} | row={row_idx} | action={action} | {detail}")

    def log_action_failed(self, message_id: str, row_idx: int, action: str, error: str, error_id: str):
        self.error(f"Action failed | msg_id={message_id[:50]} | row={row_idx} | action={action} | error={error}", error_id=error_id)

    def log_dedup_claim(self, message_id: str, claimed: bool, row_key: str = ""):
        status = "claimed" if claimed else "duplicate"
        self.info(f"Dedup | msg_id={message_id[:50]} | row_key={row_key} | status={status}")

    def log_notify(self, message_id: str, notify_type: str, recipients: list, success: bool):
        masked = self._mask_recipients(recipients)
        level = "info" if success else "warning"
        getattr(self.logger, level)(f"Notify | msg_id={message_id[:50]} | type={notify_type} | to={masked} | success={success}")

    def log_store(self, message_id: str, row_idx: int, table: str, success: bool, detail: str = ""):
        level = "info" if success else "error"
        getattr(self.logger, level)(f"Store | msg_id={message_id[:50]} | row={row_idx} | table={table} | success={success} | {detail}")

    def log_imap(self, account: str, folder: str, action: str, detail: str = ""):
        masked_account = self._mask_email(account)
        self.info(f"IMAP | account={masked_account} | folder={folder} | action={action} | {detail}")

    def log_config_missing(self, config_name: str, detail: str, error_id: str):
        self.error(f"Config missing | config={config_name} | {detail}", error_id=error_id)

    def log_unclassified_email(self, message_id: str, folder: str, sender: str, subject: str):
        masked_sender = self._mask_email(sender)
        self.info(f"Unclassified email | folder={folder} | from={masked_sender} | subject={subject[:100]} | msg_id={message_id[:50]}")

    def log_manual_category(self, message_id: str, category: str, reason: str):
        self.info(f"Manual category | msg_id={message_id[:50]} | category={category} | reason={reason}")


_logger_instance: Optional[EmailLogger] = None


def get_logger(name: str = "mailbots_next") -> EmailLogger:
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = EmailLogger(name)
    return _logger_instance