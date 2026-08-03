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


_PAGE_META_KEYS = (
    "totalPages", "totalPage", "pageCount",
    "totalCount", "totalRecords", "totalResults", "total", "recordCount",
    "hasMore", "hasNext",
)


def _first_int(data: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        try:
            value = int(data[key])
        except (KeyError, TypeError, ValueError):
            continue
        return value
    return None


def _page_meta(data: dict[str, Any]) -> dict[str, Any]:
    """Best-effort extraction of pagination metadata from a payload's data dict."""
    return {key: data[key] for key in _PAGE_META_KEYS if key in data}


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
        rows = self._fetch_all_pages(
            "/exam-service/api/CourseResult/GetResult",
            {"courseId": course_id, "startDate": None, "endDate": None,
             "searchKey": "", "pageSize": 100},
            list_key="result",
        )
        return {"data": {"result": rows}}

    def appeared_results(self, result_id: int) -> dict[str, Any]:
        self.ensure_login()
        rows = self._fetch_all_pages(
            "/exam-service/api/ExaminationHall/GetAppearedResult",
            {"id": result_id, "pageSize": 20},
            list_key="result",
        )
        return {"data": {"result": rows}}

    def result_analysis(self, exam_id: int) -> dict[str, Any]:
        self.ensure_login()
        return self._request("GET", f"/exam-service/api/ExaminationHall/GetResultAnalysis/{exam_id}")

    def _fetch_all_pages(
        self, path: str, body: dict[str, Any], *, list_key: str,
    ) -> list[dict[str, Any]]:
        """POST ``path`` for every page of ``list_key`` and return all rows.

        These endpoints page by ``pageNumber``/``pageSize`` inside the request
        body and echo pagination metadata in the response's ``data`` object.
        Recognised signals: a page count (``totalPages``/``totalPage``/
        ``pageCount``), a row count (``totalCount``/``totalRecords``/
        ``total``/``recordCount``), or a ``hasMore``/``hasNext`` flag. With no
        signal it keeps fetching while a page comes back full. When the API
        exposes no pagination at all the caller's large ``pageSize`` makes the
        single page complete in practice; that fallback may still truncate,
        which is a known limitation of this client.
        """
        combined: list[dict[str, Any]] = []
        page_size = _first_int(body, "pageSize") or 0
        max_pages = 500  # defensive bound against a broken pagination echo
        page = 1
        while page <= max_pages:
            payload = self._request("POST", path, {**body, "pageNumber": page})
            data = payload.get("data") if isinstance(payload, dict) else None
            items = data.get(list_key) if isinstance(data, dict) else None
            if not isinstance(items, list):
                # Unexpected shape: return what we already have; callers that
                # read payload["data"][list_key] still work for a single page.
                return combined
            combined.extend(items)
            meta = _page_meta(data) if isinstance(data, dict) else {}
            total_pages = _first_int(meta, "totalPages", "totalPage", "pageCount")
            if total_pages is not None:
                if page >= total_pages:
                    break
                page += 1
                continue
            total_rows = _first_int(
                meta, "totalCount", "totalRecords", "totalResults", "total", "recordCount"
            )
            if total_rows is not None and len(combined) >= total_rows:
                break
            if meta.get("hasMore") is False or meta.get("hasNext") is False:
                break
            # Short-page fallback applies only when the API exposes no
            # pagination signal: a short page can still be followed by more
            # when an explicit totalPages / totalRows / hasMore is present.
            has_signal = bool(
                total_pages
                or total_rows
                or meta.get("hasMore") is not None
                or meta.get("hasNext") is not None
            )
            if not has_signal and (not items or (page_size and len(items) < page_size)):
                break
            page += 1
        return combined
