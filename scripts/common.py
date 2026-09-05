import os
import json
import firebase_admin
from firebase_admin import credentials, firestore


def get_db():
    if not firebase_admin._apps:
        raw = os.environ["FIREBASE_SERVICE_ACCOUNT"]
        raw = raw.encode("utf-8").decode("utf-8-sig").strip()
        cred = credentials.Certificate(json.loads(raw))
        firebase_admin.initialize_app(cred)
    return firestore.client()


HEADERS = {"User-Agent": "Mozilla/5.0"}
