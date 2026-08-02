"""Minimal authenticated client for the Narayana Talent portal."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any

from config import settings

BASE_URL = "https://ntsc.narayanatalent.com"
PUBLIC_KEY_DER_B64 = (
    "MIIBCgKCAQEA1PKx1sQNhJVUgha5WOGdiRC0i0Td71UEK9enVf71Tw+79R7mdkEWtE4Ybrsr8yiYi0ETB14RjruFwiLk82wcfbcg4gxHDLxaJoEjjNh1YtMsphOaSte+vNpFrVmpqG6/dvxUAgCdK1kQAM530SC+Dui/tjPr8hUoTPgRkQwVZW/ODf7+1+AT9dJjuJSINmC7Llf5ggAQMmxf24wt2S1L9IGBFTJjIdMGFcfNc2eZQMCmbnZsmNdyv/UubCucusesWIhXnqUXfGbwaxFg0cbiqfyiISuE8yywmkPMYEI96pWRuqCBrgympGMC0CNUK2OoJWG/BeFRJ+hccY5Lp6/+6QIDAQAB"
)


class NTSCError(RuntimeError):
    pass


def _encrypt_password(password: str) -> str:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:  # pragma: no cover - deployment dependency check
        raise NTSCError("cryptography is required for NTSC login") from exc
    key = serialization.load_der_public_key(base64.b64decode(PUBLIC_KEY_DER_B64))
    return base64.b64encode(key.encrypt(password.encode(), padding.PKCS1v15())).decode()


class Client:
    def __init__(self, username: str, password: str, *, base_url: str = BASE_URL):
        self.username = username
        self.password = password
        self.base_url = base_url.rstrip("/")
        self.token = ""
        self.login_data: dict[str, Any] = {}

    @classmethod
    def from_settings(cls) -> "Client":
        username = settings.ntsc_username()
        password = settings.ntsc_password()
        if not username or not password:
            raise NTSCError("NTSC_USERNAME and NTSC_PASSWORD are not configured")
        return cls(username, password)

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json", "User-Agent": "study-bot/1.0"}
        if self.token:
            headers["Authorization"] = "bearer " + self.token
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method, headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise NTSCError(f"NTSC {method} {path} failed ({exc.code}): {detail}") from exc
        except OSError as exc:
            raise NTSCError(f"NTSC {method} {path} failed: {exc}") from exc
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise NTSCError(f"NTSC {path} returned invalid JSON") from exc

    def login(self) -> dict[str, Any]:
        payload = self._request("POST", "/login-service/api/login", {
            "userName": self.username,
            "password": _encrypt_password(self.password),
            "deviceType": "Web",
            "browser": "study-bot",
            "deviceToken": "",
        })
        data = payload.get("data") or {}
        token = data.get("token")
        if not token:
            raise NTSCError(payload.get("message") or "NTSC login returned no token")
        self.token = str(token)
        self.login_data = dict(data)
        return payload

    def ensure_login(self) -> None:
        if not self.token:
            self.login()

    def profile(self) -> dict[str, Any]:
        self.ensure_login()
        return self._request("GET", "/student-service/api/Account/GetProfile")

    def batches(self, academic_year: int) -> dict[str, Any]:
        self.ensure_login()
        return self._request(
            "GET", f"/student-service/api/EnrolledCourse/GetStudentBatch?academicYear={academic_year}"
        )

    def classes(self, start_date: str, end_date: str) -> dict[str, Any]:
        self.ensure_login()
        return self._request("POST", "/classes-service/api/Dashboard/GetDaySchedules", {
            "startDate": start_date, "endDate": end_date,
        })

    def test_calendar(self) -> dict[str, Any]:
        self.ensure_login()
        return self._request("POST", "/exam-service/api/ExamCalendar/GetStudentCalendar", {})

    def tests(self) -> dict[str, Any]:
        self.ensure_login()
        return self._request("POST", "/exam-service/api/ExaminationHall/GetTests", {})

    def scheduled_exams(self, course_id: int) -> dict[str, Any]:
        self.ensure_login()
        return self._request("POST", "/exam-service/api/Course/GetScheduleExam", {
            "courseId": course_id,
        })

    def course_results(self, course_id: int) -> dict[str, Any]:
        self.ensure_login()
        return self._request("POST", "/exam-service/api/CourseResult/GetResult", {
            "courseId": course_id, "startDate": None, "endDate": None,
            "searchKey": "", "pageNumber": 1, "pageSize": 100,
        })

    def appeared_results(self, result_id: int) -> dict[str, Any]:
        self.ensure_login()
        return self._request("POST", "/exam-service/api/ExaminationHall/GetAppearedResult", {
            "id": result_id, "pageNumber": 1, "pageSize": 20,
        })

    def result_analysis(self, exam_id: int) -> dict[str, Any]:
        self.ensure_login()
        return self._request("GET", f"/exam-service/api/ExaminationHall/GetResultAnalysis/{exam_id}")
