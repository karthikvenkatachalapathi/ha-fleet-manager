from __future__ import annotations
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.orm import Session
from ..settings import get_master_key
from ..models import InstanceCredential, now

class CredentialService:
    def __init__(self):
        self.aes = AESGCM(get_master_key())

    def encrypt(self, token: str, aad: bytes) -> tuple[bytes, bytes, bytes]:
        nonce = os.urandom(12)
        sealed = self.aes.encrypt(nonce, token.encode(), aad)
        return nonce, sealed[:-16], sealed[-16:]

    def decrypt_parts(self, nonce: bytes, ciphertext: bytes, tag: bytes, aad: bytes) -> str:
        return self.aes.decrypt(nonce, ciphertext + tag, aad).decode()

    def set_instance_token(self, db: Session, instance_id: int, token: str) -> None:
        aad = f'instance:{instance_id}'.encode()
        nonce, ciphertext, tag = self.encrypt(token, aad)
        row = db.query(InstanceCredential).filter_by(instance_id=instance_id).one_or_none()
        if row is None:
            row = InstanceCredential(instance_id=instance_id, nonce=nonce, ciphertext=ciphertext, tag=tag)
            db.add(row)
        else:
            row.nonce, row.ciphertext, row.tag = nonce, ciphertext, tag
            row.updated_at = now()

    def get_instance_token(self, db: Session, instance_id: int) -> str:
        row = db.query(InstanceCredential).filter_by(instance_id=instance_id).one_or_none()
        if not row:
            raise RuntimeError('credential not configured')
        return self.decrypt_parts(row.nonce, row.ciphertext, row.tag, f'instance:{instance_id}'.encode())
